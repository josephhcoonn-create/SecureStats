"""
APScheduler integration for SecureStats.

Jobs
────
daily_etl           — runs run_daily_etl() every day at 06:00 ET
                      (most MLB games finish by ~2 AM ET the night before).

live_update         — runs run_live_update() every 15 minutes; the job itself
                      checks whether the current ET time falls inside game
                      hours (12:00 PM – 01:00 AM) and exits early if not.

fetch_daily_odds    — runs refresh_odds_for_date() every day at 10:00 ET
                      (after most sportsbooks have posted opening lines).

fetch_odds_update   — runs refresh_odds_for_date() every 2 hours between
                      10:00 and 19:00 ET to capture line movement.

update_matchup_data  — runs update_todays_matchup_data() at 11:30 ET and
                      15:30 ET, 30 min before each generate_daily_picks run,
                      so H2H + recent-pitcher-form factors are fresh when the
                      model scores picks.

generate_daily_picks — runs get_daily_picks() at 12:00 ET (after starting
                      lineups typically post) and again at 16:00 ET (to
                      pick up late lineup updates).  The snapshot inside
                      get_daily_picks writes to PickHistory automatically.

All jobs are registered at startup via start_scheduler() and cancelled
cleanly via stop_scheduler(). These run in a dedicated worker process
(app/worker.py), NOT the web process — so the API can scale to multiple
replicas without double-firing cron jobs. (Set RUN_SCHEDULER=true to run
the scheduler inside the web process instead, for an all-in-one setup.)
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import AsyncSessionLocal
from app.services.etl import (
    run_daily_etl,
    run_live_update,
    update_todays_matchup_data,
)

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ── Singleton scheduler ───────────────────────────────────────────────────────
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=ET)
    return _scheduler


# ── Game-hours guard ──────────────────────────────────────────────────────────


def _is_game_hours() -> bool:
    """
    Return True if current Eastern Time is within typical MLB game hours.
    Window: 12:00 PM – 01:00 AM ET  (hour 12-23 or hour 0).
    """
    hour = datetime.now(ET).hour
    return hour >= 12 or hour == 0


# ── Job callables ─────────────────────────────────────────────────────────────


async def _daily_etl_job() -> None:
    logger.info("Scheduler: daily ETL job fired")
    try:
        result = await run_daily_etl()
        logger.info("Scheduler: daily ETL finished — %s", result.summary())
    except Exception:
        logger.exception("Scheduler: daily ETL job raised an unhandled exception")


async def _live_update_job() -> None:
    if not _is_game_hours():
        logger.debug("Scheduler: live-update skipped (outside game hours)")
        return
    logger.info("Scheduler: live-update job fired")
    try:
        result = await run_live_update()
        logger.info("Scheduler: live-update finished — %s", result.summary())
    except Exception:
        logger.exception("Scheduler: live-update job raised an unhandled exception")


async def _fetch_daily_odds_job() -> None:
    """Daily odds pull — runs at 10:00 ET right after opening lines drop."""
    if not settings.the_odds_api_key:
        logger.warning("Scheduler: fetch_daily_odds skipped — THE_ODDS_API_KEY not set")
        return
    # Lazy import so missing optional deps don't break scheduler startup
    from app.services.odds_persistence import refresh_odds_for_date

    logger.info("Scheduler: fetch_daily_odds fired")
    try:
        async with AsyncSessionLocal() as session:
            result = await refresh_odds_for_date(
                session, api_key=settings.the_odds_api_key, target_date=date.today()
            )
        logger.info(
            "Scheduler: fetch_daily_odds done — %d rows; quota remaining=%s used=%s",
            result.rows_inserted, result.quota_remaining, result.quota_used,
        )
    except Exception:
        logger.exception("Scheduler: fetch_daily_odds raised an unhandled exception")


async def _fetch_odds_update_job() -> None:
    """
    Line-movement snapshot — runs every 2 hours between 10:00 and 19:00 ET.
    The cron expression already gates the hour window; the early-exit
    here is belt-and-suspenders against drift.
    """
    if not settings.the_odds_api_key:
        return
    hour = datetime.now(ET).hour
    if hour < 10 or hour > 19:
        return

    from app.services.odds_persistence import refresh_odds_for_date

    logger.info("Scheduler: fetch_odds_update fired (hour=%d ET)", hour)
    try:
        async with AsyncSessionLocal() as session:
            result = await refresh_odds_for_date(
                session, api_key=settings.the_odds_api_key, target_date=date.today()
            )
        logger.info(
            "Scheduler: fetch_odds_update done — %d snapshots; quota remaining=%s used=%s",
            result.rows_inserted, result.quota_remaining, result.quota_used,
        )
    except Exception:
        logger.exception("Scheduler: fetch_odds_update raised an unhandled exception")


async def _update_matchup_data_job() -> None:
    """
    Refresh H2H + recent-pitcher-form factors for today's slate.

    Runs at 11:30 and 15:30 ET — 30 minutes ahead of each daily-picks
    generation — so the model scores against fresh matchup data.
    update_todays_matchup_data() owns its own session + client and wraps
    each fetch in try/except, so a few upstream failures won't abort it.
    """
    logger.info("Scheduler: update_matchup_data fired")
    try:
        stats = await update_todays_matchup_data()
        logger.info(
            "Scheduler: update_matchup_data done — %d batters across %d games "
            "(%d pitchers, %d errors)",
            stats["batters_updated"],
            stats["games"],
            stats["pitchers_logged"],
            stats["errors"],
        )
    except Exception:
        logger.exception("Scheduler: update_matchup_data raised an unhandled exception")


async def _generate_daily_picks_job() -> None:
    """
    Snapshot today's picks to PickHistory.

    Runs twice — 12:00 ET for the first wave of lineups, then 16:00 ET
    for late lineup adjustments. get_daily_picks() is idempotent on
    (player_id, game_id), so the 16:00 run won't dupe rows; it only
    adds picks for players who weren't surfaced earlier (e.g. a hot
    bat added to the lineup at the last minute).
    """
    from app.services.analytics import get_daily_picks

    logger.info("Scheduler: generate_daily_picks fired")
    try:
        async with AsyncSessionLocal() as session:
            result = await get_daily_picks(session)
        logger.info(
            "Scheduler: generate_daily_picks done — %d picks across %d games",
            len(result.get("picks", [])),
            result.get("games_considered", 0),
        )
    except Exception:
        logger.exception("Scheduler: generate_daily_picks raised an unhandled exception")


# ── Public start / stop ───────────────────────────────────────────────────────


def start_scheduler() -> AsyncIOScheduler:
    """
    Register all jobs and start the scheduler.
    Called from FastAPI's lifespan startup.
    """
    scheduler = get_scheduler()

    # ── Daily full ETL — 06:00 ET ─────────────────────────────────────────────
    scheduler.add_job(
        _daily_etl_job,
        CronTrigger(hour=6, minute=0, timezone=ET),
        id="daily_etl",
        name="Daily MLB ETL",
        replace_existing=True,
        misfire_grace_time=3_600,   # tolerate up to 1-hr startup delay
    )

    # ── Live update — every 15 min (game-hours gate is inside the callable) ───
    scheduler.add_job(
        _live_update_job,
        IntervalTrigger(minutes=15),
        id="live_update",
        name="Live Game Update",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # ── Daily odds pull — 10:00 ET ────────────────────────────────────────────
    scheduler.add_job(
        _fetch_daily_odds_job,
        CronTrigger(hour=10, minute=0, timezone=ET),
        id="fetch_daily_odds",
        name="Fetch Daily Odds",
        replace_existing=True,
        misfire_grace_time=3_600,
    )

    # ── Odds line-movement snapshots — every 2 hrs from 10:00 to 19:00 ET ────
    # Cron `hour="10-19/2"` fires at 10, 12, 14, 16, 18. The 10:00 run
    # overlaps with fetch_daily_odds but the per-(game,book,fetched_at)
    # uniqueness makes simultaneous runs no-ops on the second arrival.
    scheduler.add_job(
        _fetch_odds_update_job,
        CronTrigger(hour="10-19/2", minute=30, timezone=ET),
        id="fetch_odds_update",
        name="Fetch Odds Update",
        replace_existing=True,
        misfire_grace_time=900,
    )

    # ── Matchup data refresh — 11:30 ET + 15:30 ET ───────────────────────────
    # 30 min before each picks run so H2H + recent-pitcher-form factors are
    # fresh when the model scores.
    scheduler.add_job(
        _update_matchup_data_job,
        CronTrigger(hour="11,15", minute=30, timezone=ET),
        id="update_matchup_data",
        name="Update Matchup Data (H2H + pitcher form)",
        replace_existing=True,
        misfire_grace_time=1_800,
    )

    # ── Daily picks snapshots — 12:00 ET + 16:00 ET ───────────────────────────
    scheduler.add_job(
        _generate_daily_picks_job,
        CronTrigger(hour="12,16", minute=0, timezone=ET),
        id="generate_daily_picks",
        name="Generate Daily Picks",
        replace_existing=True,
        misfire_grace_time=1_800,
    )

    scheduler.start()
    logger.info(
        "Scheduler started — daily ETL @ 06:00 ET, live update q15m, "
        "odds @ 10:00 + q2h until 19:00, matchups @ 11:30 + 15:30, "
        "picks @ 12:00 + 16:00"
    )
    return scheduler


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler. Called from FastAPI lifespan shutdown."""
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
