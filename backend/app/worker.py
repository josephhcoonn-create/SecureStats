"""
Scheduler worker — the dedicated background process that owns all
APScheduler jobs (daily ETL, live updates, matchup refresh, daily picks).

Kept separate from the FastAPI web process so the API can scale to
multiple replicas without cron jobs double-firing. Run it with::

    python -m app.worker

In docker-compose / Railway this is a distinct service from the web API,
built from the same image (SERVICE_ROLE=worker in entrypoint.sh).

The process:
  1. starts the scheduler on this event loop,
  2. blocks until SIGINT/SIGTERM,
  3. shuts the scheduler down cleanly so in-flight jobs aren't orphaned.
"""
import asyncio
import logging
import signal
import sys

# psycopg needs the SelectorEventLoop on Windows (matches app/main.py). No-op
# in the Linux container, but lets `python -m app.worker` run locally on Win.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.etl.scheduler import start_scheduler, stop_scheduler  # noqa: E402
from app.middleware.logging import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)


async def _run() -> None:
    configure_logging()
    logger.info("Scheduler worker starting…")
    start_scheduler()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # add_signal_handler isn't supported on Windows' event loop;
            # KeyboardInterrupt still breaks out of asyncio.run() there.
            pass

    logger.info("Scheduler worker ready — waiting for shutdown signal")
    await stop_event.wait()

    logger.info("Shutdown signal received — stopping scheduler")
    stop_scheduler()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        stop_scheduler()
    logger.info("Scheduler worker stopped")


if __name__ == "__main__":
    main()
