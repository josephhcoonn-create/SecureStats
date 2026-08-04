"""
/picks endpoints — analyst+ access.

  GET /picks/today                 daily high-confidence picks + odds per pick
  GET /picks/player/{player_id}    enhanced model for one player's next game
  GET /picks/history?days=N        retrospective accuracy (re-runs the model
                                   on past dates, joins with actual hits)
"""
from datetime import date as _date
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import TokenPayload, require_role
from app.database import get_db
from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.player import Player
from app.models.user import UserRole
from app.schemas.odds import BookmakerSnapshot
from app.schemas.picks import (
    ModelAccuracyResponse,
    PickEntry,
    PickHistoryEntry,
    PickHistoryResponse,
    PickPlayerResponse,
    PicksTodayResponse,
)
from app.schemas.stats import EnhancedHitProbFactors
from app.services.analytics import (
    DAILY_PICK_THRESHOLD,
    _MIN_SEASON_AB,
    calculate_enhanced_hit_probability,
    get_daily_picks,
    get_model_accuracy,
)
from app.services.odds_persistence import get_latest_odds_for_games

router = APIRouter(prefix="/picks", tags=["picks"])
_analyst = Depends(require_role(UserRole.analyst))

_MAX_HISTORY_DAYS = 14


# ── 1. /picks/today ──────────────────────────────────────────────────────────


@router.get(
    "/today",
    response_model=PicksTodayResponse,
    summary="High-confidence picks for today + the latest odds for each pick's game",
)
async def picks_today(
    _: Annotated[TokenPayload, _analyst],
    db: Annotated[AsyncSession, Depends(get_db)],
    min_probability: Annotated[
        float, Query(ge=0.0, le=1.0)
    ] = DAILY_PICK_THRESHOLD,
    min_confidence: Annotated[int, Query(ge=0, le=100)] = 50,
    min_season_ab: Annotated[
        int,
        Query(ge=0, description="Playing-time floor: exclude batters below this "
              "many season ABs (0 disables the gate)."),
    ] = _MIN_SEASON_AB,
) -> PicksTodayResponse:
    raw = await get_daily_picks(
        db,
        min_probability=min_probability,
        min_confidence=min_confidence,
        min_season_ab=min_season_ab,
        include_factors=True,
    )

    # Attach the latest odds snapshot per pick's game (one query for all picks)
    pick_game_ids = {p["game_id"] for p in raw["picks"]}
    odds_by_game = await get_latest_odds_for_games(db, list(pick_game_ids))

    picks_payload: list[PickEntry] = []
    for p in raw["picks"]:
        snapshots = odds_by_game.get(p["game_id"], [])
        odds = {
            s.sportsbook: BookmakerSnapshot(
                sportsbook=s.sportsbook,
                home_moneyline=s.home_moneyline,
                away_moneyline=s.away_moneyline,
                spread_home=s.spread_home,
                spread_away=s.spread_away,
                over_under=s.over_under,
                fetched_at=s.fetched_at,
            )
            for s in snapshots
        }
        picks_payload.append(
            PickEntry(
                player_id=p["player_id"],
                player_name=p["player_name"],
                team=p["team"],
                opponent=p["opponent"],
                game_id=p["game_id"],
                probability=p["probability"],
                display_probability=p["display_probability"],
                confidence=p["confidence"],
                pitcher_name=p.get("pitcher_name"),
                factors=EnhancedHitProbFactors(**p["factors"]),
                odds=odds,
            )
        )

    return PicksTodayResponse(
        target_date=raw["target_date"],
        min_probability=raw["min_probability"],
        min_confidence=raw["min_confidence"],
        games_considered=raw["games_considered"],
        candidates_evaluated=raw["candidates_evaluated"],
        pick_count=len(picks_payload),
        picks=picks_payload,
    )


# ── 2. /picks/player/{player_id} ─────────────────────────────────────────────


@router.get(
    "/player/{player_id}",
    response_model=PickPlayerResponse,
    summary="Enhanced hit probability for a player's next scheduled game",
)
async def pick_for_player(
    player_id: int,
    _: Annotated[TokenPayload, _analyst],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PickPlayerResponse:
    player = await db.get(Player, player_id)
    if player is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Player {player_id} not found",
        )

    # Find the player's next scheduled game (today or later, soonest first)
    today = _date.today()
    next_game = (
        await db.execute(
            select(Game)
            .where(
                ((Game.home_team == player.team) | (Game.away_team == player.team)),
                Game.date >= today,
            )
            .order_by(Game.date.asc(), Game.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    pitcher_id = None
    opponent = None
    game_date = None
    game_id = None

    if next_game is not None:
        game_id = next_game.id
        game_date = next_game.date
        if next_game.home_team == player.team:
            opponent = next_game.away_team
            pitcher_id = next_game.away_probable_pitcher_id  # opposing starter
        else:
            opponent = next_game.home_team
            pitcher_id = next_game.home_probable_pitcher_id

    result = await calculate_enhanced_hit_probability(
        db,
        player_id=player.id,
        game_id=game_id,
        pitcher_id=pitcher_id,
    )

    return PickPlayerResponse(
        player_id=result["player_id"],
        player_name=result["player_name"],
        game_id=result["game_id"],
        pitcher_id=result["pitcher_id"],
        pitcher_name=result["pitcher_name"],
        probability=result["probability"],
        display_probability=result["display_probability"],
        confidence=result["confidence"],
        threshold_met=result["threshold_met"],
        factors=EnhancedHitProbFactors(**result["factors"]),
        game_date=game_date,
        opponent=opponent,
    )


# ── 3. /picks/history?days=N ─────────────────────────────────────────────────


@router.get(
    "/history",
    response_model=PickHistoryResponse,
    summary="Retrospective accuracy — re-runs picks for past N days, joins to actual hits",
)
async def picks_history(
    _: Annotated[TokenPayload, _analyst],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_HISTORY_DAYS,
            description=f"Look back this many days (max {_MAX_HISTORY_DAYS})",
        ),
    ] = 7,
    min_probability: Annotated[float, Query(ge=0.0, le=1.0)] = DAILY_PICK_THRESHOLD,
    min_confidence: Annotated[int, Query(ge=0, le=100)] = 50,
) -> PickHistoryResponse:
    by_date: list[PickHistoryEntry] = []
    total_picks = total_hits = 0

    today = _date.today()
    for offset in range(1, days + 1):
        when = today - timedelta(days=offset)
        snapshot = await get_daily_picks(
            db,
            min_probability=min_probability,
            min_confidence=min_confidence,
            target_date=when,
        )
        picks = snapshot["picks"]
        pick_count = len(picks)

        # Did each picked player actually get a hit in their picked game?
        hits = ab = 0
        if picks:
            # One query per date — group by (player_id, game_id) over batting_stats
            pairs = [(p["player_id"], p["game_id"]) for p in picks]
            player_ids = [pid for pid, _ in pairs]
            game_ids = [gid for _, gid in pairs]
            rows = (
                await db.execute(
                    select(
                        BattingStats.player_id,
                        BattingStats.game_id,
                        func.coalesce(BattingStats.hits, 0).label("h"),
                        func.coalesce(BattingStats.at_bats, 0).label("ab"),
                    ).where(
                        BattingStats.player_id.in_(player_ids),
                        BattingStats.game_id.in_(game_ids),
                    )
                )
            ).all()
            lookup = {(r.player_id, r.game_id): (r.h, r.ab) for r in rows}
            for pid, gid in pairs:
                h, this_ab = lookup.get((pid, gid), (0, 0))
                ab += this_ab
                if h > 0:
                    hits += 1

        accuracy = round(100.0 * hits / pick_count, 1) if pick_count else None
        by_date.append(
            PickHistoryEntry(
                target_date=when,
                pick_count=pick_count,
                hits=hits,
                plate_appearances=ab,
                accuracy_pct=accuracy,
            )
        )
        total_picks += pick_count
        total_hits += hits

    by_date.sort(key=lambda d: d.target_date, reverse=True)

    overall = round(100.0 * total_hits / total_picks, 1) if total_picks else None
    return PickHistoryResponse(
        days=days,
        total_picks=total_picks,
        total_hits=total_hits,
        overall_accuracy_pct=overall,
        by_date=by_date,
    )


# ── GET /picks/accuracy ──────────────────────────────────────────────────────


@router.get(
    "/accuracy",
    response_model=ModelAccuracyResponse,
    summary="Model accuracy from snapshotted PickHistory (graded by daily ETL)",
)
async def picks_accuracy(
    _: Annotated[TokenPayload, _analyst],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[
        int,
        Query(ge=1, le=365, description="Look back this many days"),
    ] = 30,
) -> ModelAccuracyResponse:
    """
    Unlike /picks/history (which re-runs the model retrospectively),
    this endpoint reads from the PickHistory table — picks captured at
    prediction time and graded once their games went final. Apples-to-
    apples for tracking real-world model performance over time.
    """
    result = await get_model_accuracy(db, days=days)
    return ModelAccuracyResponse(**result)
