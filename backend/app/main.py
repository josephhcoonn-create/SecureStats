import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

# On Windows, psycopg requires SelectorEventLoop (not the default ProactorEventLoop)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402

from app.api.router import api_router  # noqa: E402
from app.config import settings  # noqa: E402
from app.etl.scheduler import start_scheduler, stop_scheduler  # noqa: E402
from app.middleware.logging import AccessLogMiddleware, configure_logging  # noqa: E402
from app.middleware.rate_limit import limiter, rate_limit_handler  # noqa: E402
from app.middleware.security_headers import SecurityHeadersMiddleware  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The scheduler normally runs in a dedicated worker process
    # (app/worker.py). Only start it here when explicitly opted in via
    # RUN_SCHEDULER=true (single all-in-one process, e.g. local dev).
    if settings.run_scheduler:
        logger.info("RUN_SCHEDULER=true — starting scheduler in the web process")
        start_scheduler()
        try:
            yield
        finally:
            stop_scheduler()
    else:
        logger.info(
            "RUN_SCHEDULER=false — scheduler runs in the worker process, not here"
        )
        yield


app = FastAPI(
    title="SecureStats API",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Rate limiting ────────────────────────────────────────────────────────────
# Limiter is registered as app state and as middleware; the @limiter.limit
# decorators on individual routes use the shared limiter instance.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)

# ── CORS ─────────────────────────────────────────────────────────────────────
# Origins are environment-driven (see Settings.cors_origins). Methods +
# headers are explicitly listed instead of `*` so unexpected verbs/headers
# don't sneak through.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Defense-in-depth security headers and structured access logging ─────────
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AccessLogMiddleware)


app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.environment}
