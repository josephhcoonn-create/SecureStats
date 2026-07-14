"""
MLB Data ETL Pipeline
======================

Flow per date
─────────────
  EXTRACT  →  MLB Stats API (schedule + boxscores)
  TRANSFORM →  clean dicts → SQLAlchemy model values
  LOAD     →  PostgreSQL upserts inside savepoint-per-game transactions

Key design choices
──────────────────
- Each game is wrapped in a savepoint so a single bad game never
  rolls back an entire run.
- Players and Games are upserted (INSERT … ON CONFLICT DO UPDATE)
  so re-running the ETL is always safe / idempotent.
- BattingStats uses a select-then-update/insert pattern because
  SQLAlchemy's PostgreSQL upsert needs a unique constraint that
  requires a migration (deferred to a future phase).
- run_daily_etl()          — public entry point for the scheduler.
- backfill_date_range()    — bulk-loads historical data.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.matchup_history import MatchupHistory
from app.models.pitcher_game_log import PitcherGameLog
from app.models.pitcher_stats import PitcherStats
from app.models.player import Player
from app.services.mlb_client import (
    BattingStatsInfo,
    GameInfo,
    MLBClient,
    PitchingLineInfo,
    ProbablePitcherInfo,
)

logger = logging.getLogger(__name__)

# ── Game statuses that have boxscore data worth ingesting ─────────────────────
PROCESSABLE_STATUSES: frozenset[str] = frozenset(
    {
        "In Progress",
        "Game Over",
        "Final",
        "Completed Early",
        "Manager challenge",
    }
)


# ── ETL result container ──────────────────────────────────────────────────────


@dataclass
class ETLResult:
    run_date: date
    games_processed: int = 0
    players_upserted: int = 0
    stats_inserted: int = 0
    stats_updated: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        return (
            f"ETL {self.run_date}  games={self.games_processed}  "
            f"players={self.players_upserted}  "
            f"stats_new={self.stats_inserted}  stats_upd={self.stats_updated}  "
            f"errors={len(self.errors)}  {self.duration_seconds:.1f}s"
        )


# ── DB helpers ────────────────────────────────────────────────────────────────


async def _upsert_player(session: AsyncSession, line: BattingStatsInfo) -> int:
    """
    Upsert a Player row by ``mlb_id`` and return the DB primary key.

    If the player already exists, full_name / team / position are refreshed.
    """
    team = line["team"] or "Unknown"
    position = line["position"] or "N/A"

    stmt = pg_insert(Player).values(
        mlb_id=line["player_id"],
        full_name=line["player_name"],
        team=team,
        position=position,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["mlb_id"],
        set_={
            "full_name": stmt.excluded.full_name,
            "team": stmt.excluded.team,
            "position": stmt.excluded.position,
        },
    ).returning(Player.id)

    result = await session.execute(stmt)
    return result.scalar_one()


async def _upsert_pitcher_player(
    session: AsyncSession, line: PitchingLineInfo
) -> int:
    """
    Upsert a Player row from a pitching line. Same as _upsert_player but
    keyed off PitchingLineInfo (which has `throws` instead of batting
    fields). Updates `throws` only when the upstream value is non-null
    so partial info from one game doesn't wipe out a previous good value.
    """
    team = line["team"] or "Unknown"
    set_fields = {
        "full_name": pg_insert(Player).excluded.full_name,
        "team": pg_insert(Player).excluded.team,
        "position": pg_insert(Player).excluded.position,
    }
    if line.get("throws"):
        set_fields["throws"] = pg_insert(Player).excluded.throws

    stmt = pg_insert(Player).values(
        mlb_id=line["player_id"],
        full_name=line["player_name"],
        team=team,
        position="P",
        throws=line.get("throws"),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["mlb_id"],
        set_=set_fields,
    ).returning(Player.id)
    return (await session.execute(stmt)).scalar_one()


async def _upsert_pitcher_game_stats(
    session: AsyncSession,
    player_db_id: int,
    game_db_id: int,
    season: int,
    line: PitchingLineInfo,
) -> str:
    """
    Insert or update a per-game PitcherStats row (is_season_aggregate=False).
    Returns 'inserted' or 'updated' for the result counter.
    """
    existing = (
        await session.execute(
            select(PitcherStats).where(
                PitcherStats.player_id == player_db_id,
                PitcherStats.game_id == game_db_id,
                ~PitcherStats.is_season_aggregate,
            )
        )
    ).scalar_one_or_none()

    fields = {
        "innings_pitched": line["innings_pitched"],
        "hits_allowed": line["hits_allowed"],
        "earned_runs": line["earned_runs"],
        "walks_allowed": line["walks_allowed"],
        "strikeouts": line["strikeouts"],
        "era": line.get("era"),
        "whip": line.get("whip"),
    }

    if existing is None:
        session.add(
            PitcherStats(
                player_id=player_db_id,
                game_id=game_db_id,
                season=season,
                is_season_aggregate=False,
                games=1,
                **fields,
            )
        )
        return "inserted"

    for k, v in fields.items():
        setattr(existing, k, v)
    return "updated"


async def _upsert_game(session: AsyncSession, info: GameInfo) -> int:
    """
    Upsert a Game row by ``mlb_game_id`` and return the DB primary key.

    Scores and status are always refreshed so live-game re-runs stay current.
    """
    stmt = pg_insert(Game).values(
        mlb_game_id=info["game_id"],
        date=info["date"],
        home_team=info["home_team"],
        away_team=info["away_team"],
        home_score=info["home_score"],
        away_score=info["away_score"],
        status=info["status"],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["mlb_game_id"],
        set_={
            "home_score": stmt.excluded.home_score,
            "away_score": stmt.excluded.away_score,
            "status": stmt.excluded.status,
        },
    ).returning(Game.id)

    result = await session.execute(stmt)
    return result.scalar_one()


async def _upsert_batting_stats(
    session: AsyncSession,
    player_db_id: int,
    game_db_id: int,
    line: BattingStatsInfo,
) -> str:
    """
    Insert or update a BattingStats row for (player_db_id, game_db_id).

    Returns ``"inserted"`` or ``"updated"`` so the caller can track counts.
    """
    result = await session.execute(
        select(BattingStats).where(
            BattingStats.player_id == player_db_id,
            BattingStats.game_id == game_db_id,
        )
    )
    existing: BattingStats | None = result.scalar_one_or_none()

    if existing:
        existing.at_bats = line["at_bats"]
        existing.hits = line["hits"]
        existing.home_runs = line["home_runs"]
        existing.rbis = line["rbis"]
        existing.batting_avg = line["batting_avg"]
        existing.on_base_pct = line["on_base_pct"]
        existing.slugging_pct = line["slugging_pct"]
        return "updated"
    else:
        session.add(
            BattingStats(
                player_id=player_db_id,
                game_id=game_db_id,
                at_bats=line["at_bats"],
                hits=line["hits"],
                home_runs=line["home_runs"],
                rbis=line["rbis"],
                batting_avg=line["batting_avg"],
                on_base_pct=line["on_base_pct"],
                slugging_pct=line["slugging_pct"],
            )
        )
        return "inserted"


# ── Core pipeline ─────────────────────────────────────────────────────────────


async def _process_game(
    session: AsyncSession,
    mlb: MLBClient,
    game_info: GameInfo,
    result: ETLResult,
) -> None:
    """
    Process a single game inside a savepoint.

    Failures roll back only this game's writes; outer transaction stays open.
    """
    game_id = game_info["game_id"]
    logger.info("Processing game %d — %s @ %s [%s]",
                game_id, game_info["away_team"], game_info["home_team"], game_info["status"])

    async with session.begin_nested():  # savepoint
        # Upsert game record
        game_db_id = await _upsert_game(session, game_info)
        season = game_info["date"][:4]
        try:
            season_int = int(season)
        except (ValueError, TypeError):
            season_int = date.today().year

        # ── Batting lines ────────────────────────────────────────────────────
        batting_lines = await mlb.get_game_boxscore(game_id)
        for line in batting_lines:
            try:
                player_db_id = await _upsert_player(session, line)
                result.players_upserted += 1

                action = await _upsert_batting_stats(session, player_db_id, game_db_id, line)
                if action == "inserted":
                    result.stats_inserted += 1
                else:
                    result.stats_updated += 1

            except Exception as exc:  # noqa: BLE001
                msg = f"game {game_id} player {line['player_id']}: {exc}"
                logger.warning("  stat error — %s", msg)
                result.errors.append(msg)

        # ── Pitching lines ───────────────────────────────────────────────────
        # Reuses the same /boxscore endpoint — MLBClient's rate limiter
        # smooths the doubled call rate.
        try:
            pitching_lines = await mlb.get_game_pitching_lines(game_id)
        except Exception as exc:  # noqa: BLE001
            msg = f"game {game_id} pitching fetch: {exc}"
            logger.warning("  %s", msg)
            result.errors.append(msg)
            pitching_lines = []

        for pline in pitching_lines:
            try:
                pitcher_db_id = await _upsert_pitcher_player(session, pline)
                await _upsert_pitcher_game_stats(
                    session, pitcher_db_id, game_db_id, season_int, pline
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"game {game_id} pitcher {pline['player_id']}: {exc}"
                logger.warning("  pitching error — %s", msg)
                result.errors.append(msg)

        if not batting_lines and not pitching_lines:
            logger.info(
                "  game %d: no batting or pitching lines (game may not have started)",
                game_id,
            )
            return

    result.games_processed += 1
    logger.info(
        "  game %d done — %d batting, %d pitching",
        game_id,
        len(batting_lines),
        len(pitching_lines),
    )


async def run_etl_for_date(target_date: date) -> ETLResult:
    """
    Full ETL for a single calendar date.

    Fetches the schedule, filters for games with playable data, and
    processes each game inside its own DB savepoint.
    """
    result = ETLResult(run_date=target_date)
    start = time.monotonic()

    logger.info("═══ ETL START  date=%s ═══", target_date)

    try:
        async with MLBClient() as mlb, AsyncSessionLocal() as session:
            async with session.begin():
                # ── EXTRACT ──────────────────────────────────────────────────
                all_games = await mlb.get_todays_schedule(target_date)
                processable = [g for g in all_games if g["status"] in PROCESSABLE_STATUSES]

                logger.info(
                    "Schedule: %d total games, %d processable",
                    len(all_games),
                    len(processable),
                )

                # ── Always upsert Game rows for the WHOLE schedule ───────────
                # Scheduled (pre-game) entries still need DB rows so the
                # daily picks engine + probable-pitcher upsert have
                # something to attach to. Batting / pitching ingestion
                # only runs on processable statuses (next loop).
                for game_info in all_games:
                    try:
                        await _upsert_game(session, game_info)
                    except Exception as exc:  # noqa: BLE001
                        msg = f"game {game_info['game_id']} skeleton upsert failed: {exc}"
                        logger.warning("  %s", msg)
                        result.errors.append(msg)

                if not processable:
                    logger.info(
                        "Schedule upserted (%d games) but none processable yet "
                        "— skipping batting/pitching ingestion",
                        len(all_games),
                    )

                # ── TRANSFORM + LOAD (per game) ───────────────────────────────
                for game_info in processable:
                    try:
                        await _process_game(session, mlb, game_info, result)
                    except Exception as exc:  # noqa: BLE001
                        msg = f"game {game_info['game_id']} failed: {exc}"
                        logger.error("  %s", msg)
                        result.errors.append(msg)

                # ── Season aggregate roll-up + probable pitchers ──────────────
                # Rolled into the same transaction so an aggregation
                # failure rolls back consistently with the per-game data.
                try:
                    season_year = target_date.year
                    await recalc_season_pitching_aggregates(session, season_year)
                except Exception as exc:  # noqa: BLE001
                    msg = f"season aggregate recalc failed: {exc}"
                    logger.warning("  %s", msg)
                    result.errors.append(msg)

                try:
                    await upsert_probable_pitchers(session, mlb, target_date)
                except Exception as exc:  # noqa: BLE001
                    msg = f"probable pitchers upsert failed: {exc}"
                    logger.warning("  %s", msg)
                    result.errors.append(msg)

                # Grade any pending PickHistory rows whose games went
                # final today. Yesterday's picks are graded by the next
                # day's ETL run; that's why we grade target_date AND
                # the day before — covers the common slate-spanning case.
                try:
                    from app.services.analytics import (
                        get_model_accuracy,
                        grade_pending_picks,
                    )
                    graded_today = await grade_pending_picks(session, target_date)
                    graded_yesterday = await grade_pending_picks(
                        session, target_date - timedelta(days=1)
                    )
                    if graded_today or graded_yesterday:
                        logger.info(
                            "PickHistory: graded %d picks for %s, %d for %s",
                            graded_today, target_date,
                            graded_yesterday, target_date - timedelta(days=1),
                        )

                    # Snapshot of model performance over the rolling window —
                    # gives ops a daily heartbeat for "how is the model doing?"
                    accuracy = await get_model_accuracy(session, days=30)
                    if accuracy["total_picks"] > 0:
                        # Pull confidence breakdown into a single line for log greppability
                        by_conf = " ".join(
                            f"{row['tier']}={row['correct']}/{row['total']}"
                            for row in accuracy["by_confidence"]
                        )
                        logger.info(
                            "ModelAccuracy[30d]: %d/%d correct (%s%%), pending=%d, "
                            "avg_prob_correct=%s avg_prob_incorrect=%s | %s",
                            accuracy["correct_predictions"],
                            accuracy["total_picks"],
                            accuracy["accuracy_pct"],
                            accuracy["pending_picks"],
                            accuracy["avg_prob_correct"],
                            accuracy["avg_prob_incorrect"],
                            by_conf,
                        )
                    else:
                        logger.info(
                            "ModelAccuracy[30d]: no graded picks yet (%d pending)",
                            accuracy["pending_picks"],
                        )
                except Exception as exc:  # noqa: BLE001
                    msg = f"pick grading failed: {exc}"
                    logger.warning("  %s", msg)
                    result.errors.append(msg)

    except Exception as exc:  # noqa: BLE001
        msg = f"Pipeline-level failure: {exc}"
        logger.exception(msg)
        result.errors.append(msg)

    result.duration_seconds = time.monotonic() - start
    logger.info("═══ ETL END    %s ═══", result.summary())
    return result


# ── Season aggregate recalc + probable-pitcher upsert ─────────────────────────


async def recalc_season_pitching_aggregates(
    session: AsyncSession, season: int
) -> int:
    """
    Walk every per-game PitcherStats row for *season*, sum the counting
    stats per pitcher, derive ERA and WHIP, and UPSERT the result into
    the matching season-aggregate row.

    Returns the number of pitchers whose aggregate rows were written.

    Formulas (standard sabermetrics):
        ERA  = (earned_runs * 9) / innings_pitched
        WHIP = (walks + hits) / innings_pitched
    """
    from sqlalchemy import func as _f

    per_game = (
        await session.execute(
            select(
                PitcherStats.player_id,
                _f.count().label("games"),
                _f.coalesce(_f.sum(PitcherStats.innings_pitched), 0).label("ip"),
                _f.coalesce(_f.sum(PitcherStats.hits_allowed), 0).label("h"),
                _f.coalesce(_f.sum(PitcherStats.earned_runs), 0).label("er"),
                _f.coalesce(_f.sum(PitcherStats.walks_allowed), 0).label("bb"),
                _f.coalesce(_f.sum(PitcherStats.strikeouts), 0).label("so"),
            )
            .where(
                PitcherStats.season == season,
                ~PitcherStats.is_season_aggregate,
            )
            .group_by(PitcherStats.player_id)
        )
    ).all()

    written = 0
    for row in per_game:
        ip = float(row.ip or 0.0)
        era = round((row.er * 9.0) / ip, 2) if ip > 0 else None
        whip = round((row.bb + row.h) / ip, 3) if ip > 0 else None

        existing = (
            await session.execute(
                select(PitcherStats).where(
                    PitcherStats.player_id == row.player_id,
                    PitcherStats.season == season,
                    PitcherStats.is_season_aggregate,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(
                PitcherStats(
                    player_id=row.player_id,
                    game_id=None,
                    season=season,
                    is_season_aggregate=True,
                    games=int(row.games or 0),
                    innings_pitched=ip,
                    hits_allowed=int(row.h or 0),
                    earned_runs=int(row.er or 0),
                    walks_allowed=int(row.bb or 0),
                    strikeouts=int(row.so or 0),
                    era=era,
                    whip=whip,
                )
            )
        else:
            existing.games = int(row.games or 0)
            existing.innings_pitched = ip
            existing.hits_allowed = int(row.h or 0)
            existing.earned_runs = int(row.er or 0)
            existing.walks_allowed = int(row.bb or 0)
            existing.strikeouts = int(row.so or 0)
            existing.era = era
            existing.whip = whip
        written += 1

    logger.info(
        "Season aggregate recalc: %d pitchers updated for season %d", written, season
    )
    return written


async def upsert_probable_pitchers(
    session: AsyncSession, mlb: MLBClient, target_date: date
) -> int:
    """
    Pull probable pitchers for *target_date* and write them to the matching
    Game rows (home_probable_pitcher_id / away_probable_pitcher_id).

    Skips probable pitchers whose Player row hasn't been seen yet — they'll
    populate naturally once those pitchers throw a game and the boxscore
    upsert creates them.

    Returns the number of Game rows updated.
    """
    probables: list[ProbablePitcherInfo] = await mlb.get_probable_pitchers(target_date)
    if not probables:
        return 0

    updated = 0
    for pp in probables:
        # Find the local Game by mlb_game_id
        game = (
            await session.execute(
                select(Game).where(Game.mlb_game_id == pp["game_id"])
            )
        ).scalar_one_or_none()
        if game is None:
            continue

        async def _resolve(mlb_pid: int | None) -> int | None:
            if mlb_pid is None:
                return None
            row = (
                await session.execute(select(Player).where(Player.mlb_id == mlb_pid))
            ).scalar_one_or_none()
            return row.id if row else None

        home_id = await _resolve(pp.get("home_pitcher_id"))
        away_id = await _resolve(pp.get("away_pitcher_id"))

        if home_id != game.home_probable_pitcher_id or away_id != game.away_probable_pitcher_id:
            game.home_probable_pitcher_id = home_id
            game.away_probable_pitcher_id = away_id
            updated += 1

    logger.info(
        "Probable pitchers: %d/%d games updated for %s",
        updated,
        len(probables),
        target_date,
    )
    return updated


# ── Matchup factors ETL: H2H history + recent pitcher form ────────────────────
#
# These feed the enhanced hit-probability model's two new factors. All three
# take ``session`` (and ``mlb``) first to match this file's convention — the
# orchestrator shares one session + client across the whole batch instead of
# opening dozens. IDs passed in are MLB ids (what the schedule/roster endpoints
# return); the new tables FK to our internal ``players.id``, so we resolve and
# skip any player we haven't ingested yet — same policy as
# ``upsert_probable_pitchers``. Resolving before the API call also avoids
# burning requests on bench / minor-league roster names we can't store.

# Spacing between upstream matchup calls (task requirement). The client's
# token-bucket already caps at ≤10/s; this is extra politeness for the
# hundreds-of-calls H2H sweep.
_MATCHUP_CALL_DELAY = 0.15


async def _resolve_player_id(session: AsyncSession, mlb_id: int) -> int | None:
    """Map an MLB player id to our internal ``players.id`` (or None if unseen)."""
    return (
        await session.execute(select(Player.id).where(Player.mlb_id == mlb_id))
    ).scalar_one_or_none()


async def update_matchup_history(
    session: AsyncSession,
    mlb: MLBClient,
    batter_mlb_id: int,
    pitcher_mlb_id: int,
) -> str:
    """
    Fetch and upsert the career head-to-head line for one batter vs one
    pitcher into ``matchup_history``.

    Returns a status string the orchestrator uses for counting / pacing:
      - ``"stored"``           — H2H line fetched and upserted (API call made)
      - ``"no_history"``       — they've never faced each other (API call made)
      - ``"unknown_batter"``   — batter not in our players table (no API call)
      - ``"unknown_pitcher"``  — pitcher not in our players table (no API call)

    Never creates a record when there's no shared history.
    """
    batter_db_id = await _resolve_player_id(session, batter_mlb_id)
    if batter_db_id is None:
        return "unknown_batter"
    pitcher_db_id = await _resolve_player_id(session, pitcher_mlb_id)
    if pitcher_db_id is None:
        return "unknown_pitcher"

    h2h = await mlb.get_batter_vs_pitcher(batter_mlb_id, pitcher_mlb_id)
    if h2h is None:
        logger.debug(
            "H2H batter %d vs pitcher %d: no history — skipping",
            batter_mlb_id,
            pitcher_mlb_id,
        )
        return "no_history"

    stmt = pg_insert(MatchupHistory).values(
        batter_id=batter_db_id,
        pitcher_id=pitcher_db_id,
        at_bats=h2h["at_bats"],
        hits=h2h["hits"],
        home_runs=h2h["home_runs"],
        strikeouts=h2h["strikeouts"],
        batting_avg=h2h["batting_avg"],
        on_base_pct=h2h["on_base_pct"],
        slugging_pct=h2h["slugging_pct"],
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_matchup_history_batter_pitcher",
        set_={
            "at_bats": stmt.excluded.at_bats,
            "hits": stmt.excluded.hits,
            "home_runs": stmt.excluded.home_runs,
            "strikeouts": stmt.excluded.strikeouts,
            "batting_avg": stmt.excluded.batting_avg,
            "on_base_pct": stmt.excluded.on_base_pct,
            "slugging_pct": stmt.excluded.slugging_pct,
            "last_updated": func.now(),
        },
    )
    await session.execute(stmt)
    logger.info(
        "H2H stored: batter %d vs pitcher %d — %d-for-%d (.%s)",
        batter_mlb_id,
        pitcher_mlb_id,
        h2h["hits"],
        h2h["at_bats"],
        f"{h2h['batting_avg']:.3f}"[2:] if h2h["batting_avg"] is not None else "---",
    )
    return "stored"


async def update_pitcher_game_log(
    session: AsyncSession,
    mlb: MLBClient,
    pitcher_mlb_id: int,
    num_games: int = 3,
) -> int:
    """
    Fetch a pitcher's recent game log and insert any games not already
    stored (keyed on the ``(pitcher_id, game_date)`` unique constraint).

    Returns the number of *new* games inserted, or ``-1`` when the pitcher
    isn't in our players table (unknown — nothing stored).
    """
    pitcher_db_id = await _resolve_player_id(session, pitcher_mlb_id)
    if pitcher_db_id is None:
        logger.debug(
            "Pitcher %d not in players table — skipping game log", pitcher_mlb_id
        )
        return -1

    form = await mlb.get_pitcher_recent_games(pitcher_mlb_id, num_games=num_games)
    if form is None:
        return 0

    new_games = 0
    for g in form["games"]:
        if not g["date"]:
            continue
        try:
            game_date = date.fromisoformat(g["date"])
        except ValueError:
            continue

        stmt = (
            pg_insert(PitcherGameLog)
            .values(
                pitcher_id=pitcher_db_id,
                game_id=None,  # MLB game log may reference games outside our table
                game_date=game_date,
                opponent=g["opponent"],
                innings_pitched=g["innings_pitched"],
                hits_allowed=g["hits_allowed"],
                earned_runs=g["earned_runs"],
                walks=g["walks"],
                strikeouts=g["strikeouts"],
                era=g["era"],
                whip=g["whip"],
            )
            .on_conflict_do_nothing(
                constraint="uq_pitcher_game_log_pitcher_date"
            )
        )
        result = await session.execute(stmt)
        if result.rowcount and result.rowcount > 0:
            new_games += 1

    logger.info(
        "Pitcher game log %d: %d new of %d recent games",
        pitcher_mlb_id,
        new_games,
        form["num_games"],
    )
    return new_games


async def update_todays_matchup_data(target_date: date | None = None) -> dict:
    """
    Orchestrator — refresh recent-pitcher form + batter-vs-pitcher H2H for
    every probable-pitcher matchup on *target_date* (default: today). Meant
    to run shortly before daily picks are generated.

    For each game with a probable starter:
      1. update_pitcher_game_log() for that starter, then
      2. for each position player on the *opposing* team's roster,
         update_matchup_history(batter, starter).

    The opposing lineup uses the full team roster (filtered to position
    players) as a stand-in until real lineups post — matching the model's
    existing ``_likely_starters`` philosophy. Batters we haven't ingested
    yet are skipped without an API call.

    Every individual fetch is wrapped in try/except and committed in its own
    transaction so one bad matchup never rolls back the batch. A
    ``_MATCHUP_CALL_DELAY`` (0.15s) pause follows each upstream call.

    Returns a stats dict for the caller / trigger endpoint.
    """
    when = target_date or date.today()
    stats = {
        "target_date": str(when),
        "games": 0,
        "pitchers_logged": 0,
        "batters_updated": 0,
        "no_history": 0,
        "errors": 0,
    }
    start = time.monotonic()
    logger.info("=== Matchup data update START for %s ===", when)

    async with MLBClient() as mlb, AsyncSessionLocal() as session:
        # Both are API calls (no DB txn needed yet).
        schedule = await mlb.get_todays_schedule(when)
        probables = await mlb.get_probable_pitchers(when)

        # game_id → (home_team_id, away_team_id); ProbablePitcherInfo lacks ids.
        team_ids: dict[int, tuple[int, int]] = {
            g["game_id"]: (g["home_team_id"], g["away_team_id"]) for g in schedule
        }

        games_seen: set[int] = set()
        for pp in probables:
            ids = team_ids.get(pp["game_id"])
            if ids is None:
                continue
            home_team_id, away_team_id = ids

            # Each starter faces the OPPOSING team's lineup.
            pairings = [
                (pp.get("home_pitcher_id"), away_team_id),
                (pp.get("away_pitcher_id"), home_team_id),
            ]
            for pitcher_mlb_id, opp_team_id in pairings:
                if pitcher_mlb_id is None:
                    continue
                games_seen.add(pp["game_id"])

                # 1. Pitcher recent form
                try:
                    async with session.begin():
                        added = await update_pitcher_game_log(
                            session, mlb, pitcher_mlb_id
                        )
                    if added >= 0:
                        stats["pitchers_logged"] += 1
                        await asyncio.sleep(_MATCHUP_CALL_DELAY)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "matchup: pitcher log %d failed: %s", pitcher_mlb_id, exc
                    )
                    stats["errors"] += 1

                # 2. Opposing lineup roster (position players only)
                try:
                    roster = await mlb.get_team_roster(opp_team_id)
                    await asyncio.sleep(_MATCHUP_CALL_DELAY)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "matchup: roster %d failed: %s", opp_team_id, exc
                    )
                    stats["errors"] += 1
                    continue

                for entry in roster:
                    if entry["position"] == "P":
                        continue  # skip pitchers as batters
                    try:
                        async with session.begin():
                            outcome = await update_matchup_history(
                                session, mlb, entry["player_id"], pitcher_mlb_id
                            )
                        if outcome == "stored":
                            stats["batters_updated"] += 1
                            await asyncio.sleep(_MATCHUP_CALL_DELAY)
                        elif outcome == "no_history":
                            stats["no_history"] += 1
                            await asyncio.sleep(_MATCHUP_CALL_DELAY)
                        # unknown_batter / unknown_pitcher → no API call, no pause
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "matchup: H2H %d vs %d failed: %s",
                            entry["player_id"],
                            pitcher_mlb_id,
                            exc,
                        )
                        stats["errors"] += 1

        stats["games"] = len(games_seen)

    stats["duration_seconds"] = round(time.monotonic() - start, 1)
    logger.info(
        "Updated matchup data for %d batters across %d games "
        "(%d pitchers logged, %d no-history, %d errors, %.1fs)",
        stats["batters_updated"],
        stats["games"],
        stats["pitchers_logged"],
        stats["no_history"],
        stats["errors"],
        stats["duration_seconds"],
    )
    return stats


# ── Public entry points ───────────────────────────────────────────────────────


async def run_daily_etl() -> ETLResult:
    """Run the full ETL pipeline for today. Called by the APScheduler daily job."""
    return await run_etl_for_date(date.today())


# Live-game statuses worth refreshing every 15 min
_LIVE_STATUSES: frozenset[str] = frozenset(
    {"In Progress", "Manager challenge", "Delay", "Delayed"}
)


async def run_live_update() -> ETLResult:
    """
    Lightweight update — only processes games currently *in progress*.

    Called by the APScheduler live-update job every 15 min during game hours.
    Much faster than run_daily_etl() because it skips completed / scheduled games.
    """
    result = ETLResult(run_date=date.today())
    start = time.monotonic()

    logger.info("--- Live update START ---")
    try:
        async with MLBClient() as mlb, AsyncSessionLocal() as session:
            async with session.begin():
                games = await mlb.get_todays_schedule()
                live_games = [g for g in games if g["status"] in _LIVE_STATUSES]

                logger.info(
                    "Live update: %d total games, %d in progress",
                    len(games),
                    len(live_games),
                )

                for game_info in live_games:
                    try:
                        await _process_game(session, mlb, game_info, result)
                    except Exception as exc:  # noqa: BLE001
                        msg = f"game {game_info['game_id']} live-update failed: {exc}"
                        logger.error("  %s", msg)
                        result.errors.append(msg)

    except Exception as exc:  # noqa: BLE001
        msg = f"Live update pipeline failure: {exc}"
        logger.exception(msg)
        result.errors.append(msg)

    result.duration_seconds = time.monotonic() - start
    logger.info("--- Live update END  %s ---", result.summary())
    return result


async def backfill_date_range(
    start_date: date,
    end_date: date,
) -> list[ETLResult]:
    """
    Load historical data for every date in [start_date, end_date].

    Dates are processed sequentially to respect the MLB API rate limit.
    Returns one :class:`ETLResult` per date.
    """
    if start_date > end_date:
        raise ValueError(f"start_date {start_date} must be ≤ end_date {end_date}")

    total_days = (end_date - start_date).days + 1
    logger.info("Backfill: %d days from %s to %s", total_days, start_date, end_date)

    results: list[ETLResult] = []
    current = start_date

    while current <= end_date:
        logger.info("Backfill progress: %s (%d/%d)", current, len(results) + 1, total_days)
        result = await run_etl_for_date(current)
        results.append(result)
        current += timedelta(days=1)

    success_count = sum(1 for r in results if r.success)
    total_stats = sum(r.stats_inserted + r.stats_updated for r in results)
    logger.info(
        "Backfill complete: %d/%d days OK, %d total stat lines",
        success_count,
        total_days,
        total_stats,
    )
    return results
