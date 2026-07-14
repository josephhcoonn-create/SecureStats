import logging
import time
from datetime import date as _date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rbac import TokenPayload, require_role
from app.config import settings
from app.database import get_db
from app.models.user import UserRole
from app.schemas.etl import (
    ETLTriggerResponse,
    MatchupTriggerResponse,
    OddsTriggerResponse,
)
from app.services.etl import (
    run_daily_etl,
    run_live_update,
    update_todays_matchup_data,
)
from app.services.odds_client import OddsApiError
from app.services.odds_persistence import refresh_odds_for_date

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/etl", tags=["etl"])

_admin = Depends(require_role(UserRole.admin))


@router.post(
    "/trigger",
    response_model=ETLTriggerResponse,
    summary="Manually trigger the ETL pipeline (admin only)",
)
async def trigger_etl(
    live_only: Annotated[
        bool,
        Query(description="True → live-update only (in-progress games); False → full daily ETL"),
    ] = False,
    _: TokenPayload = _admin,
) -> ETLTriggerResponse:
    """
    Admin-only endpoint that synchronously runs the ETL pipeline and returns
    the full result so you can see exactly what was loaded.

    - **live_only=false** (default) — runs the full `run_daily_etl()` pipeline.
    - **live_only=true** — runs `run_live_update()` (faster, in-progress games only).
    """
    result = await run_live_update() if live_only else await run_daily_etl()

    return ETLTriggerResponse(
        status="success" if result.success else "partial_failure",
        run_date=str(result.run_date),
        games_processed=result.games_processed,
        players_upserted=result.players_upserted,
        stats_inserted=result.stats_inserted,
        stats_updated=result.stats_updated,
        errors=result.errors,
        duration_seconds=round(result.duration_seconds, 2),
        success=result.success,
    )


@router.post(
    "/trigger-odds",
    response_model=OddsTriggerResponse,
    summary="Manually fetch odds from The Odds API (admin only)",
)
async def trigger_odds(
    target_date: Annotated[
        _date | None,
        Query(description="Override target date; defaults to today"),
    ] = None,
    _: TokenPayload = _admin,
    db: AsyncSession = Depends(get_db),
) -> OddsTriggerResponse:
    """
    Force an out-of-band odds refresh. Useful right after setting
    ``THE_ODDS_API_KEY`` or when you want the dashboard to reflect
    line movement immediately rather than waiting for the next
    scheduled pull (10:00 ET daily + every 2 hours until 19:00 ET).

    Returns the quota_remaining / quota_used headers from the upstream
    response so you can keep an eye on the 500 calls/month free tier.
    """
    if not settings.the_odds_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="THE_ODDS_API_KEY is not set. Sign up at https://the-odds-api.com.",
        )

    when = target_date or _date.today()
    started = time.monotonic()

    try:
        result = await refresh_odds_for_date(
            db, api_key=settings.the_odds_api_key, target_date=when
        )
    except OddsApiError as exc:
        logger.warning("trigger_odds failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Odds API error: {exc}",
        ) from exc

    duration = round(time.monotonic() - started, 2)
    logger.info(
        "trigger_odds(%s): %d rows in %ss; quota remaining=%s used=%s",
        when, result.rows_inserted, duration,
        result.quota_remaining, result.quota_used,
    )

    return OddsTriggerResponse(
        target_date=str(when),
        rows_inserted=result.rows_inserted,
        quota_remaining=result.quota_remaining,
        quota_used=result.quota_used,
        duration_seconds=duration,
        success=True,
    )


@router.post(
    "/trigger-matchups",
    response_model=MatchupTriggerResponse,
    summary="Manually refresh H2H + recent-pitcher-form matchup data (admin only)",
)
async def trigger_matchups(
    target_date: Annotated[
        _date | None,
        Query(description="Override target date; defaults to today"),
    ] = None,
    _: TokenPayload = _admin,
) -> MatchupTriggerResponse:
    """
    Force an out-of-band refresh of the matchup factors — batter-vs-pitcher
    head-to-head history and each probable starter's recent game log — for
    the given date's slate. Normally runs on the scheduler at 11:30 and
    15:30 ET; trigger it here to populate data immediately (e.g. right after
    deploying these factors, or before a mid-day picks re-run).

    Runs synchronously and can take a minute or two for a full slate, since
    it fans out one API call per batter/pitcher pairing at a paced ~0.15s.
    """
    stats = await update_todays_matchup_data(target_date=target_date)

    return MatchupTriggerResponse(
        target_date=stats["target_date"],
        games=stats["games"],
        pitchers_logged=stats["pitchers_logged"],
        batters_updated=stats["batters_updated"],
        no_history=stats["no_history"],
        errors=stats["errors"],
        duration_seconds=stats["duration_seconds"],
        success=stats["errors"] == 0,
    )
