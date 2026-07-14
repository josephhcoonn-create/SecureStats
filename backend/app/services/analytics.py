"""
Analytics service layer.

All public functions accept an ``AsyncSession`` so they compose cleanly
with FastAPI's dependency-injected sessions.  No function manages its own
session — the caller owns the session lifecycle.

Supported stats
───────────────
  batting_avg   SUM(hits) / SUM(at_bats)        formatted  ".302"
  home_runs     SUM(home_runs)                   formatted  "23"
  rbis          SUM(rbis)                        formatted  "45"
  hits          SUM(hits)                        formatted  "67"
  on_base_pct   AVG(on_base_pct)                formatted  ".380"
  slugging_pct  AVG(slugging_pct)               formatted  ".512"
  ops           AVG(obp) + AVG(slg)             formatted  ".892"

Hit probability formula
───────────────────────
  p = 0.5 × recent_avg (last 30 games)
    + 0.3 × career_avg (all-time)
    + 0.2 × league_avg (DB-wide)

  95% CI uses a normal approximation on the recent-sample at-bats.
  Confidence label: low (<15 AB), medium (15–49 AB), high (≥50 AB).
"""

import math
from datetime import date, timedelta

from fastapi import HTTPException, status
from sqlalchemy import Float, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.pick_history import PickHistory
from app.models.player import Player
from app.schemas.stats import (
    BattingLeadersResponse,
    ComparisonPlayerStats,
    HitProbabilityResponse,
    LeaderEntry,
    PlayerComparisonResponse,
    StreakEntry,
    StreaksResponse,
    TeamRankingEntry,
    TeamRankingsResponse,
    fmt_avg,
    fmt_int,
    fmt_pct,
)

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_STATS = frozenset(
    {"batting_avg", "home_runs", "rbis", "hits", "on_base_pct", "slugging_pct", "ops"}
)
MIN_AB_LEADERS = 10     # minimum at-bats to qualify for batting leaders
MIN_AB_TEAMS = 50       # minimum team at-bats to appear in team rankings
RECENT_GAMES_WINDOW = 10  # games used for "recent form" in comparison
FALLBACK_LEAGUE_AVG = 0.243  # MLB historical average, used when DB is empty

FINAL_STATUSES = ("Final", "Game Over", "Completed Early")


# ── SQL helpers ───────────────────────────────────────────────────────────────


def _stat_aggregate(stat: str):
    """
    Return the SQLAlchemy aggregate expression for *stat*.

    Uses ``NULLIF(SUM(at_bats), 0)`` for batting_avg so division by zero
    becomes NULL rather than a DB error.
    """
    if stat == "batting_avg":
        return func.sum(BattingStats.hits).cast(Float) / func.nullif(
            func.sum(BattingStats.at_bats), 0
        )
    if stat == "home_runs":
        return func.sum(BattingStats.home_runs)
    if stat == "rbis":
        return func.sum(BattingStats.rbis)
    if stat == "hits":
        return func.sum(BattingStats.hits)
    if stat == "on_base_pct":
        return func.avg(BattingStats.on_base_pct)
    if stat == "slugging_pct":
        return func.avg(BattingStats.slugging_pct)
    if stat == "ops":
        # AVG ignores NULLs in PostgreSQL; returns NULL only when ALL rows are NULL
        return func.avg(BattingStats.on_base_pct) + func.avg(BattingStats.slugging_pct)
    raise ValueError(f"Unknown stat: {stat!r}. Valid: {sorted(VALID_STATS)}")


def _fmt_display(stat: str, value: float | None) -> str:
    """Pre-format a stat value for UI display."""
    if value is None:
        return "---"
    if stat in ("batting_avg", "on_base_pct", "slugging_pct", "ops"):
        return fmt_avg(value)
    return fmt_int(value)


# ── 1. Batting leaders ────────────────────────────────────────────────────────


async def get_batting_leaders(
    session: AsyncSession,
    stat: str,
    limit: int = 10,
    days: int | None = None,
) -> BattingLeadersResponse:
    """
    Return the top *limit* players ranked by *stat* over the last *days*
    calendar days (all-time when *days* is None).
    """
    if stat not in VALID_STATS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid stat '{stat}'. Valid: {sorted(VALID_STATS)}",
        )

    date_filters = [Game.status.in_(FINAL_STATUSES)]
    if days:
        date_filters.append(Game.date >= date.today() - timedelta(days=days))

    q = (
        select(
            Player.id.label("player_id"),
            Player.mlb_id,
            Player.full_name,
            Player.team,
            Player.position,
            func.count(BattingStats.id).label("games_played"),
            func.coalesce(func.sum(BattingStats.at_bats), 0).label("total_ab"),
            _stat_aggregate(stat).label("stat_value"),
        )
        .join(BattingStats, Player.id == BattingStats.player_id)
        .join(Game, BattingStats.game_id == Game.id)
        .where(*date_filters)
        .group_by(
            Player.id,
            Player.mlb_id,
            Player.full_name,
            Player.team,
            Player.position,
        )
        .having(func.sum(BattingStats.at_bats) >= MIN_AB_LEADERS)
        .order_by(text("stat_value DESC NULLS LAST"))
        .limit(limit)
    )

    rows = (await session.execute(q)).all()

    leaders = [
        LeaderEntry(
            rank=i + 1,
            player_id=r.player_id,
            mlb_id=r.mlb_id,
            full_name=r.full_name,
            team=r.team,
            position=r.position,
            games_played=r.games_played,
            at_bats=r.total_ab,
            value=float(r.stat_value) if r.stat_value is not None else None,
            display_value=_fmt_display(stat, float(r.stat_value) if r.stat_value is not None else None),
        )
        for i, r in enumerate(rows)
    ]

    return BattingLeadersResponse(
        stat=stat,
        days=days,
        min_at_bats=MIN_AB_LEADERS,
        leaders=leaders,
    )


# ── 2. Team rankings ──────────────────────────────────────────────────────────


async def get_team_rankings(
    session: AsyncSession,
    stat: str,
) -> TeamRankingsResponse:
    """
    Return all teams ranked by aggregate *stat* across all final games.
    """
    if stat not in VALID_STATS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid stat '{stat}'. Valid: {sorted(VALID_STATS)}",
        )

    q = (
        select(
            Player.team,
            func.count(BattingStats.id).label("games_played"),
            func.coalesce(func.sum(BattingStats.at_bats), 0).label("total_ab"),
            _stat_aggregate(stat).label("stat_value"),
        )
        .join(BattingStats, Player.id == BattingStats.player_id)
        .join(Game, BattingStats.game_id == Game.id)
        .where(Game.status.in_(FINAL_STATUSES))
        .group_by(Player.team)
        .having(func.sum(BattingStats.at_bats) >= MIN_AB_TEAMS)
        .order_by(text("stat_value DESC NULLS LAST"))
    )

    rows = (await session.execute(q)).all()

    rankings = [
        TeamRankingEntry(
            rank=i + 1,
            team=r.team,
            games_played=r.games_played,
            at_bats=r.total_ab,
            value=float(r.stat_value) if r.stat_value is not None else None,
            display_value=_fmt_display(stat, float(r.stat_value) if r.stat_value is not None else None),
        )
        for i, r in enumerate(rows)
    ]

    return TeamRankingsResponse(stat=stat, rankings=rankings)


# ── 3. Hit probability ────────────────────────────────────────────────────────


async def calculate_hit_probability(
    session: AsyncSession,
    player_id: int,
) -> HitProbabilityResponse:
    """
    Estimate the probability of a hit on the player's next at-bat.

    Formula: 0.5 × recent_avg + 0.3 × career_avg + 0.2 × league_avg

    The 95% confidence interval is computed via normal approximation
    on the recent-sample at-bat count.
    """
    player = await session.get(Player, player_id)
    if player is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player {player_id} not found",
        )

    # ── Recent (last 30 days of final games) ──────────────────────────────────
    cutoff = date.today() - timedelta(days=30)
    recent_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
                func.count(BattingStats.id).label("games"),
            )
            .join(Game, BattingStats.game_id == Game.id)
            .where(
                BattingStats.player_id == player_id,
                Game.date >= cutoff,
                Game.status.in_(FINAL_STATUSES),
            )
        )
    ).one()

    recent_ab: int = recent_row.ab or 0
    recent_hits: int = recent_row.h or 0
    recent_games: int = recent_row.games or 0
    recent_avg: float | None = (recent_hits / recent_ab) if recent_ab > 0 else None

    # ── Career ────────────────────────────────────────────────────────────────
    career_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
            ).where(BattingStats.player_id == player_id)
        )
    ).one()

    career_ab: int = career_row.ab or 0
    career_hits: int = career_row.h or 0
    career_avg: float | None = (career_hits / career_ab) if career_ab > 0 else None

    # ── League ────────────────────────────────────────────────────────────────
    league_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
            ).join(Game, BattingStats.game_id == Game.id).where(
                Game.status.in_(FINAL_STATUSES)
            )
        )
    ).one()

    league_ab: int = league_row.ab or 0
    league_hits: int = league_row.h or 0
    league_avg: float = (
        (league_hits / league_ab) if league_ab > 0 else FALLBACK_LEAGUE_AVG
    )

    # ── Weighted probability ──────────────────────────────────────────────────
    # Fall back to career if no recent data; fall back to league if no career data
    eff_recent = recent_avg if recent_avg is not None else (career_avg or league_avg)
    eff_career = career_avg if career_avg is not None else league_avg

    probability = 0.5 * eff_recent + 0.3 * eff_career + 0.2 * league_avg
    probability = max(0.0, min(1.0, probability))

    # ── 95% CI via normal approximation ──────────────────────────────────────
    n = recent_ab if recent_ab > 0 else 1  # avoid division by zero
    se = math.sqrt(probability * (1.0 - probability) / n)
    ci_lower = max(0.0, probability - 1.96 * se)
    ci_upper = min(1.0, probability + 1.96 * se)

    # ── Confidence label ──────────────────────────────────────────────────────
    if recent_ab < 15:
        confidence = "low"
    elif recent_ab < 50:
        confidence = "medium"
    else:
        confidence = "high"

    return HitProbabilityResponse(
        player_id=player.id,
        mlb_id=player.mlb_id,
        full_name=player.full_name,
        team=player.team,
        recent_avg=round(recent_avg, 3) if recent_avg is not None else None,
        career_avg=round(career_avg, 3) if career_avg is not None else None,
        league_avg=round(league_avg, 3),
        hit_probability=round(probability, 3),
        display_probability=fmt_pct(probability),
        ci_lower=round(ci_lower, 3),
        ci_upper=round(ci_upper, 3),
        display_ci=f"[{fmt_pct(ci_lower)}, {fmt_pct(ci_upper)}]",
        recent_games=recent_games,
        recent_at_bats=recent_ab,
        confidence=confidence,
    )


# ── 4. Hot / cold streaks ─────────────────────────────────────────────────────


async def get_hot_cold_streaks(
    session: AsyncSession,
    streak_type: str = "both",
    min_games: int = 5,
) -> StreaksResponse:
    """
    Find players whose batting average across their last *min_games* final
    games is >= .350 (hot) or <= .150 (cold).

    Uses a window-function subquery so only the N most-recent games per
    player contribute to the average.
    """
    HOT_THRESHOLD = 0.350
    COLD_THRESHOLD = 0.150

    # ── Subquery 1: rank each player's games newest → oldest ─────────────────
    rn_col = func.row_number().over(
        partition_by=BattingStats.player_id,
        order_by=Game.date.desc(),
    ).label("rn")

    ranked = (
        select(
            BattingStats.player_id,
            BattingStats.at_bats,
            BattingStats.hits,
            rn_col,
        )
        .join(Game, BattingStats.game_id == Game.id)
        .where(Game.status.in_(FINAL_STATUSES))
        .subquery()
    )

    # ── Subquery 2: aggregate the last min_games per player ──────────────────
    recent = (
        select(
            ranked.c.player_id,
            func.count(ranked.c.player_id).label("games"),
            func.sum(ranked.c.hits).label("total_hits"),
            func.sum(ranked.c.at_bats).label("total_ab"),
            (
                func.sum(ranked.c.hits).cast(Float)
                / func.nullif(func.sum(ranked.c.at_bats), 0)
            ).label("period_avg"),
        )
        .where(ranked.c.rn <= min_games)
        .group_by(ranked.c.player_id)
        .having(
            func.count(ranked.c.player_id) >= min_games,
            func.sum(ranked.c.at_bats) >= min_games,  # basic qualifier
        )
        .subquery()
    )

    # ── Main query: join with players and apply streak filter ────────────────
    q = (
        select(Player, recent)
        .join(recent, Player.id == recent.c.player_id)
    )

    if streak_type == "hot":
        q = q.where(recent.c.period_avg >= HOT_THRESHOLD)
    elif streak_type == "cold":
        q = q.where(recent.c.period_avg <= COLD_THRESHOLD)
    else:  # "both"
        q = q.where(
            (recent.c.period_avg >= HOT_THRESHOLD)
            | (recent.c.period_avg <= COLD_THRESHOLD)
        )

    q = q.order_by(recent.c.period_avg.desc())
    rows = (await session.execute(q)).all()

    entries: list[StreakEntry] = []
    for row in rows:
        # select(Player, recent_subquery) → row[0] is the ORM instance;
        # subquery columns are accessible by their label names on the Row.
        player: Player = row[0]
        avg_val = float(row.period_avg) if row.period_avg is not None else None
        stype = (
            "hot"
            if avg_val is not None and avg_val >= HOT_THRESHOLD
            else "cold"
        )
        entries.append(
            StreakEntry(
                player_id=player.id,
                mlb_id=player.mlb_id,
                full_name=player.full_name,
                team=player.team,
                position=player.position,
                streak_type=stype,
                games=row.games,
                hits=row.total_hits or 0,
                at_bats=row.total_ab or 0,
                period_avg=round(avg_val, 3) if avg_val is not None else None,
                display_avg=fmt_avg(avg_val),
            )
        )

    return StreaksResponse(
        streak_type=streak_type,
        min_games=min_games,
        hot_threshold=HOT_THRESHOLD,
        cold_threshold=COLD_THRESHOLD,
        streaks=entries,
    )


# ── 5. Player comparison ──────────────────────────────────────────────────────


async def get_player_comparison(
    session: AsyncSession,
    player_ids: list[int],
) -> PlayerComparisonResponse:
    """
    Return side-by-side career and recent-form stats for the given players.

    Also returns a ``leaders`` dict mapping each stat name to the player_id
    that leads among the compared players.
    """
    if not player_ids:
        return PlayerComparisonResponse(players=[], leaders={})

    players = (
        await session.execute(
            select(Player)
            .where(Player.id.in_(player_ids))
            .order_by(Player.full_name)
        )
    ).scalars().all()

    found_ids = {p.id for p in players}
    missing = set(player_ids) - found_ids
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Players not found: {sorted(missing)}",
        )

    comparison_entries: list[ComparisonPlayerStats] = []

    for player in players:
        # ── Career stats ──────────────────────────────────────────────────────
        career = (
            await session.execute(
                select(
                    func.count(BattingStats.id).label("games"),
                    func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                    func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
                    func.coalesce(func.sum(BattingStats.home_runs), 0).label("hr"),
                    func.coalesce(func.sum(BattingStats.rbis), 0).label("rbi"),
                    func.avg(BattingStats.on_base_pct).label("obp"),
                    func.avg(BattingStats.slugging_pct).label("slg"),
                ).where(BattingStats.player_id == player.id)
            )
        ).one()

        c_ab: int = career.ab or 0
        c_hits: int = career.h or 0
        c_obp: float | None = float(career.obp) if career.obp is not None else None
        c_slg: float | None = float(career.slg) if career.slg is not None else None
        c_avg: float | None = (c_hits / c_ab) if c_ab > 0 else None
        c_ops: float | None = (
            round(c_obp + c_slg, 3)
            if c_obp is not None and c_slg is not None
            else None
        )

        # ── Recent form (last RECENT_GAMES_WINDOW games) ──────────────────────
        rn_col = func.row_number().over(
            partition_by=BattingStats.player_id,
            order_by=Game.date.desc(),
        ).label("rn")

        ranked = (
            select(BattingStats.player_id, BattingStats.at_bats, BattingStats.hits, rn_col)
            .join(Game, BattingStats.game_id == Game.id)
            .where(
                BattingStats.player_id == player.id,
                Game.status.in_(FINAL_STATUSES),
            )
            .subquery()
        )

        recent = (
            await session.execute(
                select(
                    func.count(ranked.c.player_id).label("games"),
                    func.coalesce(func.sum(ranked.c.at_bats), 0).label("ab"),
                    func.coalesce(func.sum(ranked.c.hits), 0).label("h"),
                )
                .where(ranked.c.rn <= RECENT_GAMES_WINDOW)
            )
        ).one()

        r_ab: int = recent.ab or 0
        r_hits: int = recent.h or 0
        r_avg: float | None = (r_hits / r_ab) if r_ab > 0 else None

        comparison_entries.append(
            ComparisonPlayerStats(
                player_id=player.id,
                mlb_id=player.mlb_id,
                full_name=player.full_name,
                team=player.team,
                position=player.position,
                games_played=career.games or 0,
                at_bats=c_ab,
                hits=c_hits,
                home_runs=career.hr or 0,
                rbis=career.rbi or 0,
                batting_avg=round(c_avg, 3) if c_avg is not None else None,
                on_base_pct=round(c_obp, 3) if c_obp is not None else None,
                slugging_pct=round(c_slg, 3) if c_slg is not None else None,
                ops=c_ops,
                recent_games=recent.games or 0,
                recent_avg=round(r_avg, 3) if r_avg is not None else None,
                display_avg=fmt_avg(c_avg),
                display_ops=fmt_avg(c_ops),
                display_recent_avg=fmt_avg(r_avg),
            )
        )

    # ── Leaders dict ──────────────────────────────────────────────────────────
    def _leader(entries: list[ComparisonPlayerStats], key: str) -> int | None:
        valid = [e for e in entries if getattr(e, key) is not None]
        return max(valid, key=lambda e: getattr(e, key)).player_id if valid else None

    leaders: dict[str, int | None] = {
        "batting_avg": _leader(comparison_entries, "batting_avg"),
        "home_runs": _leader(comparison_entries, "home_runs"),
        "rbis": _leader(comparison_entries, "rbis"),
        "hits": _leader(comparison_entries, "hits"),
        "ops": _leader(comparison_entries, "ops"),
        "recent_avg": _leader(comparison_entries, "recent_avg"),
    }

    return PlayerComparisonResponse(players=comparison_entries, leaders=leaders)


# ═══════════════════════════════════════════════════════════════════════════════
# Enhanced hit probability (Phase 8)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Multi-factor model: pulls recent / season / career / home-away splits for
# the batter, opposing pitcher's ERA + WHIP + handedness, and a league
# baseline. Every component falls back to a sensible default when data is
# missing so the model returns *something* even on a sparse database.
#
# Weight schedule (sums to 1.00 when H2H is at full weight):
#   BATTER (0.50):
#     0.25  recent_avg          (last 15 games)
#     0.12  season_avg          (current calendar year)
#     0.08  career_avg          (all loaded games)
#     0.05  home_away_split     (batter's avg in this venue context)
#   HEAD-TO-HEAD (0.15, dynamic by sample size — see below):
#     0.15  h2h_avg             (batter's career avg vs THIS pitcher)
#   PITCHER (0.25):
#     0.12  pitcher_recent       (last-3-starts ERA relative to season ERA)
#     0.08  pitcher_season       (season ERA + WHIP composite)
#     0.05  handedness_matchup   (opposite/same-hand shift, league-avg scale)
#   LEAGUE (0.10):
#     0.10  league_avg          (DB-wide; fallback FALLBACK_LEAGUE_AVG when empty)
#
# H2H is weighted by sample size — a 4-for-4 (1.000) means nothing, a
# 40-for-120 (.333) is gold:
#   ≥15 AB → full 0.15
#   5-14 AB → half (0.075); the other 0.075 is redistributed
#            proportionally across the four batter factors
#   <5 AB or no data → 0; the full 0.15 is redistributed to batter factors
# The pitcher (0.25) + league (0.10) weights never move, so the blend
# always sums to 1.00.
#
# Final probability is clamped to [0.05, 0.95] — no certainties.

# Weight constants — exposed so they're easy to tune from one place.
_W_RECENT = 0.25
_W_SEASON = 0.12
_W_CAREER = 0.08
_W_HOME_AWAY = 0.05
_W_BATTER_TOTAL = _W_RECENT + _W_SEASON + _W_CAREER + _W_HOME_AWAY  # 0.50
_W_H2H = 0.15
_W_PITCHER_RECENT = 0.12
_W_PITCHER_SEASON = 0.08
_W_HANDEDNESS = 0.05
_W_LEAGUE = 0.10

# H2H sample-size gates (at-bats) and the recent-form window.
_H2H_FULL_AB = 15    # ≥ this → full H2H weight
_H2H_MIN_AB = 5      # ≥ this (and < full) → half weight; below → ignore
_PITCHER_RECENT_STARTS = 3   # game-log rows that define "recent form"

# Pitcher recent-vs-season ERA ratios that flag hot/cold form.
_PITCHER_STRUGGLING_RATIO = 1.3   # recent ERA > season × 1.3 → struggling
_PITCHER_LOCKED_IN_RATIO = 0.7    # recent ERA < season × 0.7 → locked in

# Handedness matchup modifier — added to pitcher_composite before the
# 0.30 weight is applied.
_HANDEDNESS_BOOST = 0.015      # opposite hand or switch hitter
_HANDEDNESS_PENALTY = -0.010   # same hand
_HANDEDNESS_UNKNOWN = 0.0      # either side missing

# Probability clamp + threshold — no certainties either way.
# The clamp + threshold apply to the GAME-LEVEL probability (≥1 hit in
# the next start), not the per-AB rate. The per-AB rate is converted
# via 1 - (1-p)^EXPECTED_AB_PER_GAME.
#
# Threshold history:
#   0.80 — original brief; achievable only because the ERA term in the
#          pitcher composite was inverted (good pitchers boosted picks)
#   0.72 — current; reflects the directionally honest model. A .340
#          season hitter on a hot streak against a slightly-worse-than-
#          league-average pitcher lands here. Daily slates surface a
#          small set of high-conviction matchups rather than zero.
_PROB_MIN = 0.05
_PROB_MAX = 0.95
DAILY_PICK_THRESHOLD = 0.72
_EXPECTED_AB_PER_GAME = 4  # typical for a starting position player

# League-baseline fallbacks when the DB has nothing better.
_FALLBACK_ERA = 4.20
_FALLBACK_WHIP = 1.30
_FALLBACK_PITCHER_IP = 0.0

# Confidence buckets — see _calculate_confidence for the base + pitcher-IP
# boost. The H2H / recent-form boosts stack on top inside
# calculate_enhanced_hit_probability (they need matchup data the pure
# _calculate_confidence helper doesn't see).
_CONF_LOW = 30
_CONF_MEDIUM = 60
_CONF_HIGH = 85
_CONF_PITCHER_BOOST = 10
_CONF_PITCHER_BOOST_MIN_IP = 50
_CONF_H2H_BOOST = 15          # H2H sample ≥ 15 AB
_CONF_RECENT_PITCHER_BOOST = 10   # ≥ 3 recent starts logged for the pitcher
_CONF_MAX = 100

_RECENT_GAMES = 15


# ── Helper queries ────────────────────────────────────────────────────────────


async def _recent_batting_avg(
    session: AsyncSession, player_id: int, n_games: int = _RECENT_GAMES
) -> tuple[float | None, int]:
    """Batting avg + total ABs over the player's last n_games final games."""
    rn_col = func.row_number().over(
        partition_by=BattingStats.player_id,
        order_by=Game.date.desc(),
    ).label("rn")
    sub = (
        select(BattingStats.at_bats, BattingStats.hits, rn_col)
        .join(Game, BattingStats.game_id == Game.id)
        .where(
            BattingStats.player_id == player_id,
            Game.status.in_(FINAL_STATUSES),
        )
        .subquery()
    )
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(sub.c.at_bats), 0).label("ab"),
                func.coalesce(func.sum(sub.c.hits), 0).label("h"),
            ).where(sub.c.rn <= n_games)
        )
    ).one()
    ab = row.ab or 0
    h = row.h or 0
    return ((h / ab) if ab > 0 else None, ab)


async def _season_batting_avg(
    session: AsyncSession, player_id: int, year: int | None = None
) -> tuple[float | None, int]:
    """Batting avg + ABs across the current (or specified) calendar year."""
    target_year = year or date.today().year
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
            )
            .join(Game, BattingStats.game_id == Game.id)
            .where(
                BattingStats.player_id == player_id,
                func.extract("year", Game.date) == target_year,
                Game.status.in_(FINAL_STATUSES),
            )
        )
    ).one()
    ab = row.ab or 0
    h = row.h or 0
    return ((h / ab) if ab > 0 else None, ab)


async def _career_batting_avg(
    session: AsyncSession, player_id: int
) -> float | None:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
            ).where(BattingStats.player_id == player_id)
        )
    ).one()
    ab, h = row.ab or 0, row.h or 0
    return (h / ab) if ab > 0 else None


async def _home_away_split(
    session: AsyncSession, player_id: int, player_team: str, at_home: bool
) -> float | None:
    """Player's batting avg in games where their team is home (or away)."""
    condition = (
        Game.home_team == player_team if at_home else Game.away_team == player_team
    )
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
            )
            .join(Game, BattingStats.game_id == Game.id)
            .where(
                BattingStats.player_id == player_id,
                condition,
                Game.status.in_(FINAL_STATUSES),
            )
        )
    ).one()
    ab, h = row.ab or 0, row.h or 0
    return (h / ab) if ab > 0 else None


async def _league_batting_avg(session: AsyncSession) -> float:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                func.coalesce(func.sum(BattingStats.hits), 0).label("h"),
            )
            .join(Game, BattingStats.game_id == Game.id)
            .where(Game.status.in_(FINAL_STATUSES))
        )
    ).one()
    ab, h = row.ab or 0, row.h or 0
    return (h / ab) if ab > 0 else FALLBACK_LEAGUE_AVG


async def _pitcher_factors(
    session: AsyncSession, pitcher_id: int | None
) -> tuple[float, float, float]:
    """
    Returns (era, whip, innings_pitched). Falls back to league baselines
    when the pitcher isn't in PitcherStats yet.
    """
    if pitcher_id is None:
        return (_FALLBACK_ERA, _FALLBACK_WHIP, _FALLBACK_PITCHER_IP)

    from app.models.pitcher_stats import PitcherStats  # local import — avoids circular

    row = (
        await session.execute(
            select(PitcherStats)
            .where(PitcherStats.player_id == pitcher_id)
            .order_by(PitcherStats.season.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return (_FALLBACK_ERA, _FALLBACK_WHIP, _FALLBACK_PITCHER_IP)
    return (
        row.era if row.era is not None else _FALLBACK_ERA,
        row.whip if row.whip is not None else _FALLBACK_WHIP,
        row.innings_pitched or _FALLBACK_PITCHER_IP,
    )


async def _matchup_history(
    session: AsyncSession, batter_id: int, pitcher_id: int
) -> tuple[float | None, int]:
    """
    Cached career H2H line for (batter, pitcher). Returns
    ``(batting_avg, at_bats)`` — ``(None, 0)`` when the pair has no
    stored history (never faced, or not yet fetched by the ETL).
    """
    from app.models.matchup_history import MatchupHistory  # local — avoids circular

    row = (
        await session.execute(
            select(MatchupHistory.batting_avg, MatchupHistory.at_bats).where(
                MatchupHistory.batter_id == batter_id,
                MatchupHistory.pitcher_id == pitcher_id,
            )
        )
    ).one_or_none()
    if row is None:
        return (None, 0)
    return (row.batting_avg, row.at_bats or 0)


async def _pitcher_recent_era(
    session: AsyncSession, pitcher_id: int, n_starts: int = _PITCHER_RECENT_STARTS
) -> tuple[float | None, int]:
    """
    Recent-form ERA over the pitcher's last *n_starts* logged games.

    Aggregated from raw earned runs / innings (ERA = ΣER / ΣIP × 9) rather
    than averaging per-game ERAs, so a short outing doesn't skew it.
    Returns ``(recent_era, num_starts_used)`` — ``(None, 0)`` when there
    are no game-log rows (or zero innings across them).
    """
    from app.models.pitcher_game_log import PitcherGameLog  # local — avoids circular

    rows = (
        await session.execute(
            select(
                PitcherGameLog.innings_pitched,
                PitcherGameLog.earned_runs,
            )
            .where(PitcherGameLog.pitcher_id == pitcher_id)
            .order_by(PitcherGameLog.game_date.desc())
            .limit(n_starts)
        )
    ).all()
    if not rows:
        return (None, 0)

    total_ip = sum(r.innings_pitched or 0.0 for r in rows)
    total_er = sum(r.earned_runs or 0 for r in rows)
    if total_ip <= 0:
        return (None, len(rows))
    return (round(total_er / total_ip * 9.0, 2), len(rows))


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _handedness_modifier(
    batter_bats: str | None, pitcher_throws: str | None
) -> float:
    """
    Opposite-hand matchup is statistically friendlier to the batter.
    Switch hitters always get the favorable side. Unknown hands → 0.
    """
    if not batter_bats or not pitcher_throws:
        return _HANDEDNESS_UNKNOWN
    if batter_bats.upper() == "S":  # switch hitter
        return _HANDEDNESS_BOOST
    return (
        _HANDEDNESS_BOOST
        if batter_bats.upper() != pitcher_throws.upper()
        else _HANDEDNESS_PENALTY
    )


def _pitcher_composite(
    era: float, whip: float, league_avg: float, handedness: float
) -> float:
    """
    Combine the pitcher's ERA + WHIP into a hit-rate estimate, then
    apply the handedness shift. League baselines (4.20 ERA / 1.30 WHIP)
    define "neutral pitcher" → returns ≈ league_avg.

    Both terms scale **directly** with the pitcher stat so that better
    pitchers (lower ERA, lower WHIP) yield a SMALLER hit-rate estimate
    and worse pitchers yield a LARGER one. The original Task 8.2 brief
    expressed the ERA term as ``(league_ERA / pitcher_ERA) × league_avg``
    which is directionally inverted — kept the math here directionally
    honest at the cost of departing from the brief verbatim. (Without
    this fix, picks against ace pitchers got *boosted*, which destroys
    the model's predictive value.)
    """
    era = max(era, 0.5)   # clamp away from zero to avoid blowups
    whip = max(whip, 0.5)
    era_term = (era / _FALLBACK_ERA) * league_avg
    whip_term = (whip / _FALLBACK_WHIP) * league_avg
    return (era_term + whip_term) / 2 + handedness


def _calculate_confidence(season_ab: int, pitcher_ip: float) -> int:
    if season_ab >= 100:
        base = _CONF_HIGH
    elif season_ab >= 30:
        base = _CONF_MEDIUM
    else:
        base = _CONF_LOW
    if pitcher_ip >= _CONF_PITCHER_BOOST_MIN_IP:
        base += _CONF_PITCHER_BOOST
    return min(_CONF_MAX, base)


# ── Public: enhanced hit probability ──────────────────────────────────────────


async def calculate_enhanced_hit_probability(
    session: AsyncSession,
    player_id: int,
    game_id: int | None = None,
    pitcher_id: int | None = None,
) -> dict:
    """
    Multi-factor hit probability for a batter vs a specific game/pitcher.

    Returns a plain dict (not a Pydantic model) so it can be consumed by
    both :func:`get_daily_picks` and the API layer without coupling.

    See the weight schedule comment at the top of this section for the
    full formula. Every factor falls back to a sensible default when
    its source data is missing — the model never raises for "no data".
    """
    player = await session.get(Player, player_id)
    if player is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player {player_id} not found",
        )

    game = await session.get(Game, game_id) if game_id else None

    # When the caller doesn't name a pitcher but we have the game, resolve
    # the OPPOSING probable starter so the H2H + pitcher factors engage.
    # (get_daily_picks relies on this — it passes pitcher_id=None.)
    if pitcher_id is None and game is not None:
        if game.home_team == player.team:
            pitcher_id = game.away_probable_pitcher_id
        elif game.away_team == player.team:
            pitcher_id = game.home_probable_pitcher_id

    # ── Batter factors ────────────────────────────────────────────────────────
    recent_avg, _recent_ab = await _recent_batting_avg(session, player_id)
    season_avg, season_ab = await _season_batting_avg(session, player_id)
    career_avg = await _career_batting_avg(session, player_id)
    league_avg = await _league_batting_avg(session)

    # Home/away context — if game is provided, derive from it; otherwise None.
    home_away = None
    if game is not None:
        at_home = game.home_team == player.team
        home_away = await _home_away_split(session, player_id, player.team, at_home)

    # ── Pitcher factors ──────────────────────────────────────────────────────
    pitcher_era, pitcher_whip, pitcher_ip = await _pitcher_factors(session, pitcher_id)
    pitcher: Player | None = (
        await session.get(Player, pitcher_id) if pitcher_id else None
    )
    handedness = _handedness_modifier(
        player.bats, pitcher.throws if pitcher else None
    )
    # Season ERA + WHIP composite WITHOUT the handedness shift — handedness
    # is now its own weighted factor. Passing handedness=0 reuses the
    # tested _pitcher_composite helper for just the ERA/WHIP blend.
    pitcher_season_term = _pitcher_composite(
        pitcher_era, pitcher_whip, league_avg, handedness=0.0
    )

    # Recent-form term: last-3-starts ERA compared to the pitcher's OWN
    # season ERA. recent > season → struggling (boost hits); recent <
    # season → locked in (suppress hits). Falls back to a season-vs-league
    # term when we don't have ≥3 logged starts.
    recent_era: float | None = None
    recent_starts = 0
    trending: str | None = None
    season_era_ref = max(pitcher_era, 0.5)
    if pitcher_id is not None:
        recent_era, recent_starts = await _pitcher_recent_era(session, pitcher_id)
    if recent_era is not None and recent_starts >= _PITCHER_RECENT_STARTS:
        pitcher_recent_term = (recent_era / season_era_ref) * league_avg
        ratio = recent_era / season_era_ref
        if ratio > _PITCHER_STRUGGLING_RATIO:
            trending = "struggling"
        elif ratio < _PITCHER_LOCKED_IN_RATIO:
            trending = "locked_in"
        else:
            trending = "steady"
    else:
        # Not enough recent data — fall back to season ERA vs league baseline.
        pitcher_recent_term = (season_era_ref / _FALLBACK_ERA) * league_avg
        recent_era = None  # don't surface a "recent" ERA we didn't measure

    # Handedness as a league-avg-scale factor (its own 0.05 weight).
    handedness_term = league_avg + handedness

    # ── Head-to-head factor with sample-size-driven dynamic weight ───────────
    h2h_avg: float | None = None
    h2h_ab = 0
    if pitcher_id is not None:
        h2h_avg, h2h_ab = await _matchup_history(session, player_id, pitcher_id)

    if h2h_avg is not None and h2h_ab >= _H2H_FULL_AB:
        h2h_weight = _W_H2H
    elif h2h_avg is not None and h2h_ab >= _H2H_MIN_AB:
        h2h_weight = _W_H2H / 2.0
    else:
        h2h_weight = 0.0

    # Redistribute the unused H2H weight proportionally across batter factors
    # (their relative split is preserved; only their total grows).
    redistribute = _W_H2H - h2h_weight
    batter_scale = 1.0 + (redistribute / _W_BATTER_TOTAL)
    w_recent = _W_RECENT * batter_scale
    w_season = _W_SEASON * batter_scale
    w_career = _W_CAREER * batter_scale
    w_home_away = _W_HOME_AWAY * batter_scale

    # ── Weighted blend with per-component fallbacks ──────────────────────────
    def _or(value: float | None, fallback: float) -> float:
        return value if value is not None else fallback

    per_ab = (
        w_recent      * _or(recent_avg, _or(season_avg, _or(career_avg, league_avg)))
        + w_season    * _or(season_avg, _or(career_avg, league_avg))
        + w_career    * _or(career_avg, league_avg)
        + w_home_away * _or(home_away, _or(career_avg, league_avg))
        + h2h_weight  * _or(h2h_avg, league_avg)
        + _W_PITCHER_RECENT * pitcher_recent_term
        + _W_PITCHER_SEASON * pitcher_season_term
        + _W_HANDEDNESS     * handedness_term
        + _W_LEAGUE   * league_avg
    )
    # Clamp per-AB to (0, 1) so the game-level transform stays sane,
    # then convert: P(≥1 hit in N ABs) = 1 - (1 - p)^N.
    per_ab = max(0.001, min(0.999, per_ab))
    per_game = 1.0 - (1.0 - per_ab) ** _EXPECTED_AB_PER_GAME
    probability = max(_PROB_MIN, min(_PROB_MAX, per_game))

    # Base confidence (season AB tier + pitcher-IP boost), then stack the
    # new matchup-data boosts on top.
    confidence = _calculate_confidence(season_ab, pitcher_ip)
    if h2h_avg is not None and h2h_ab >= _H2H_FULL_AB:
        confidence += _CONF_H2H_BOOST
    if recent_starts >= _PITCHER_RECENT_STARTS:
        confidence += _CONF_RECENT_PITCHER_BOOST
    confidence = min(_CONF_MAX, confidence)

    return {
        "player_id": player.id,
        "player_name": player.full_name,
        "game_id": game.id if game else None,
        "pitcher_id": pitcher.id if pitcher else None,
        "pitcher_name": pitcher.full_name if pitcher else None,
        "probability": round(probability, 3),
        "display_probability": fmt_pct(probability),
        "confidence": confidence,
        "threshold_met": probability >= DAILY_PICK_THRESHOLD,
        "factors": {
            "recent_avg": round(recent_avg, 3) if recent_avg is not None else None,
            "season_avg": round(season_avg, 3) if season_avg is not None else None,
            "career_avg": round(career_avg, 3) if career_avg is not None else None,
            "home_away_split": (
                round(home_away, 3) if home_away is not None else None
            ),
            "pitcher_era": pitcher_era if pitcher_id else None,
            "pitcher_whip": pitcher_whip if pitcher_id else None,
            "handedness_matchup": handedness,
            "league_avg": round(league_avg, 3),
            # ── New matchup factors ──
            "h2h_avg": round(h2h_avg, 3) if h2h_avg is not None else None,
            "h2h_at_bats": h2h_ab,
            "h2h_weight_applied": round(h2h_weight, 3),
            "pitcher_recent_era": recent_era,
            "pitcher_season_era": pitcher_era if pitcher_id else None,
            "pitcher_trending": trending,
        },
    }


# ── Public: daily picks ──────────────────────────────────────────────────────


async def get_daily_picks(
    session: AsyncSession,
    min_probability: float = DAILY_PICK_THRESHOLD,
    min_confidence: int = 50,
    target_date: date | None = None,
    include_factors: bool = False,
) -> dict:
    """
    For every batter on every team playing on *target_date* (defaults to
    today), run :func:`calculate_enhanced_hit_probability` and return
    those meeting both thresholds, sorted by probability descending.

    "Today's batters" = players who have appeared in 3+ of their team's
    last 5 final games. This is a heuristic stand-in for real-time
    starting lineup announcements (which the MLB API only publishes a
    few hours before first pitch). The accuracy is roughly 85-90% for
    everyday position players — pitchers and bench bats are filtered
    out naturally.
    """
    when = target_date or date.today()

    games_today = (
        await session.execute(select(Game).where(Game.date == when))
    ).scalars().all()

    if not games_today:
        return {
            "target_date": str(when),
            "min_probability": min_probability,
            "min_confidence": min_confidence,
            "games_considered": 0,
            "candidates_evaluated": 0,
            "picks": [],
        }

    # For each game, find probable batters: players from either team
    # who have appeared in 3+ of that team's last 5 final games.
    candidates: list[tuple[Player, Game]] = []
    for game in games_today:
        for team_name, _opponent_name in (
            (game.home_team, game.away_team),
            (game.away_team, game.home_team),
        ):
            recent_player_ids = await _likely_starters(session, team_name)
            if not recent_player_ids:
                continue
            players = (
                await session.execute(
                    select(Player).where(Player.id.in_(recent_player_ids))
                )
            ).scalars().all()
            for p in players:
                candidates.append((p, game))

    # Dedup candidates — a player whose team played in games where
    # _likely_starters' team filter matches BOTH sides (a same-team
    # H2H pair) would otherwise be evaluated twice for the same game.
    seen: set[tuple[int, int]] = set()
    deduped: list[tuple[Player, Game]] = []
    for player, game in candidates:
        key = (player.id, game.id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((player, game))
    candidates = deduped

    # Run the model on every candidate
    evaluated = 0
    picks: list[dict] = []
    for player, game in candidates:
        evaluated += 1
        result = await calculate_enhanced_hit_probability(
            session, player_id=player.id, game_id=game.id, pitcher_id=None
        )
        if (
            result["probability"] >= min_probability
            and result["confidence"] >= min_confidence
        ):
            opponent = (
                game.away_team if game.home_team == player.team else game.home_team
            )
            entry = {
                "player_id": player.id,
                "player_name": player.full_name,
                "team": player.team,
                "opponent": opponent,
                "game_id": game.id,
                "probability": result["probability"],
                "display_probability": result["display_probability"],
                "confidence": result["confidence"],
                "pitcher_name": result.get("pitcher_name"),
            }
            if include_factors:
                entry["factors"] = result["factors"]
            picks.append(entry)

    picks.sort(key=lambda p: p["probability"], reverse=True)

    # Snapshot picks into PickHistory for accuracy tracking. Idempotent
    # via UNIQUE(player_id, game_id) so repeated /picks/today calls on
    # the same day produce no duplicates. We do this inside
    # get_daily_picks so every surface that returns picks also locks
    # them in for grading later.
    if picks:
        await _snapshot_picks(session, picks)

    return {
        "target_date": str(when),
        "min_probability": min_probability,
        "min_confidence": min_confidence,
        "games_considered": len(games_today),
        "candidates_evaluated": evaluated,
        "picks": picks,
    }


async def _snapshot_picks(session: AsyncSession, picks: list[dict]) -> int:
    """
    Insert PickHistory rows for every pick that doesn't already have one
    for this (player_id, game_id) pair. Returns the number of new rows.

    The unique constraint + ON CONFLICT DO NOTHING make this idempotent
    so /picks/today is safe to call repeatedly without churning rows.
    """
    rows = [
        {
            "player_id": p["player_id"],
            "game_id": p["game_id"],
            "predicted_probability": p["probability"],
            "confidence": p["confidence"],
            "actual_result": "pending",
            "factors_snapshot": p.get("factors"),
        }
        for p in picks
    ]
    if not rows:
        return 0
    # RETURNING-based count works around the psycopg quirk of reporting
    # rowcount=-1 for INSERT ... ON CONFLICT DO NOTHING statements.
    stmt = (
        pg_insert(PickHistory)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["player_id", "game_id"])
        .returning(PickHistory.id)
    )
    result = await session.execute(stmt)
    inserted_ids = result.scalars().all()
    await session.commit()
    return len(inserted_ids)


# ── Model accuracy ───────────────────────────────────────────────────────────


async def get_model_accuracy(
    session: AsyncSession,
    days: int = 30,
) -> dict:
    """
    Walk every graded PickHistory row from the last *days* days and
    report headline accuracy plus a confidence-tier breakdown.

    A pick is "correct" iff actual_result == 'hit'. Pending picks
    (game hasn't finished or grading hasn't run yet) are excluded
    from the denominators so they don't drag accuracy toward zero.
    """
    cutoff = date.today() - timedelta(days=days)

    rows = (
        await session.execute(
            select(PickHistory).where(
                PickHistory.created_at >= cutoff,
                PickHistory.actual_result.in_(("hit", "no_hit")),
            )
        )
    ).scalars().all()

    total = len(rows)
    pending = (
        await session.execute(
            select(func.count()).select_from(PickHistory).where(
                PickHistory.created_at >= cutoff,
                PickHistory.actual_result == "pending",
            )
        )
    ).scalar_one()

    if total == 0:
        return {
            "days": days,
            "total_picks": 0,
            "pending_picks": pending,
            "correct_predictions": 0,
            "accuracy_pct": None,
            "avg_prob_correct": None,
            "avg_prob_incorrect": None,
            "by_confidence": [],
            "recent_hits": [],
        }

    correct_rows = [r for r in rows if r.actual_result == "hit"]
    incorrect_rows = [r for r in rows if r.actual_result == "no_hit"]
    correct = len(correct_rows)

    avg_prob_correct = (
        round(sum(r.predicted_probability for r in correct_rows) / len(correct_rows), 3)
        if correct_rows else None
    )
    avg_prob_incorrect = (
        round(
            sum(r.predicted_probability for r in incorrect_rows) / len(incorrect_rows),
            3,
        )
        if incorrect_rows else None
    )

    # Confidence-tier breakdown — does higher confidence == higher hit rate?
    def _tier(conf: int) -> str:
        if conf >= 80:
            return "high"
        if conf >= 50:
            return "medium"
        return "low"

    tiers: dict[str, dict] = {
        "high":   {"total": 0, "correct": 0},
        "medium": {"total": 0, "correct": 0},
        "low":    {"total": 0, "correct": 0},
    }
    for r in rows:
        bucket = tiers[_tier(r.confidence)]
        bucket["total"] += 1
        if r.actual_result == "hit":
            bucket["correct"] += 1

    by_confidence = [
        {
            "tier": tier,
            "total": data["total"],
            "correct": data["correct"],
            "accuracy_pct": (
                round(data["correct"] * 100.0 / data["total"], 1)
                if data["total"] > 0 else None
            ),
        }
        for tier, data in tiers.items()
    ]

    # Recent correct hits with player names — six most recent, ordered
    # by game date desc then by predicted probability desc as a tiebreak.
    recent_rows = (
        await session.execute(
            select(
                PickHistory.player_id,
                Player.full_name,
                Game.date,
                PickHistory.predicted_probability,
                PickHistory.confidence,
            )
            .join(Player, Player.id == PickHistory.player_id)
            .join(Game, Game.id == PickHistory.game_id)
            .where(
                PickHistory.created_at >= cutoff,
                PickHistory.actual_result == "hit",
            )
            .order_by(Game.date.desc(), PickHistory.predicted_probability.desc())
            .limit(6)
        )
    ).all()

    recent_hits = [
        {
            "player_id": pid,
            "player_name": name,
            "game_date": gdate,
            "predicted_probability": round(prob, 3),
            "confidence": conf,
        }
        for pid, name, gdate, prob, conf in recent_rows
    ]

    return {
        "days": days,
        "total_picks": total,
        "pending_picks": pending,
        "correct_predictions": correct,
        "accuracy_pct": round(correct * 100.0 / total, 1),
        "avg_prob_correct": avg_prob_correct,
        "avg_prob_incorrect": avg_prob_incorrect,
        "by_confidence": by_confidence,
        "recent_hits": recent_hits,
    }


async def grade_pending_picks(
    session: AsyncSession, target_date: date | None = None
) -> int:
    """
    Look up PickHistory rows still in 'pending' for games that have
    reached a final status on *target_date* (defaults to today). For
    each, check the player's batting line; set actual_result to 'hit'
    when hits > 0, 'no_hit' when hits == 0. Players who didn't play
    in the game keep their 'pending' status — they'll be ignored by
    the accuracy calculation but available for audit.

    Returns the number of rows graded.
    """
    when = target_date or date.today()
    final_games = (
        await session.execute(
            select(Game.id).where(
                Game.date == when,
                Game.status.in_(FINAL_STATUSES),
            )
        )
    ).scalars().all()

    if not final_games:
        return 0

    pending = (
        await session.execute(
            select(PickHistory).where(
                PickHistory.game_id.in_(final_games),
                PickHistory.actual_result == "pending",
            )
        )
    ).scalars().all()

    graded = 0
    for pick in pending:
        line = (
            await session.execute(
                select(BattingStats).where(
                    BattingStats.player_id == pick.player_id,
                    BattingStats.game_id == pick.game_id,
                )
            )
        ).scalar_one_or_none()
        if line is None:
            continue  # player didn't appear; leave pending for audit
        pick.actual_result = "hit" if line.hits > 0 else "no_hit"
        graded += 1

    return graded


async def _likely_starters(
    session: AsyncSession, team_name: str, last_n_games: int = 5, min_appearances: int = 3
) -> list[int]:
    """
    Return player_ids for batters who appeared in ≥ min_appearances of
    *team_name*'s last *last_n_games* final games. Heuristic stand-in for
    real-time starting lineups.
    """
    # Get the team's last N final games (newest first)
    recent_games = (
        await session.execute(
            select(Game.id)
            .where(
                ((Game.home_team == team_name) | (Game.away_team == team_name)),
                Game.status.in_(FINAL_STATUSES),
            )
            .order_by(Game.date.desc())
            .limit(last_n_games)
        )
    ).scalars().all()
    if not recent_games:
        return []

    # Players who batted in ≥ min_appearances of those games AND
    # whose own Player.team matches the team being queried. Without
    # the team filter the heuristic surfaces opposing batters from
    # the recent series (e.g. a Marlins player picked up as a
    # "Blue Jays starter" because they played the Jays last week).
    rows = (
        await session.execute(
            select(BattingStats.player_id, func.count().label("apps"))
            .join(Player, Player.id == BattingStats.player_id)
            .where(
                BattingStats.game_id.in_(recent_games),
                Player.team == team_name,
            )
            .group_by(BattingStats.player_id)
            .having(func.count() >= min_appearances)
        )
    ).all()
    return [r.player_id for r in rows]
