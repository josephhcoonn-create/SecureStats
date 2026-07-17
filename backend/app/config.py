from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Locate .env relative to this file so pytest and uvicorn both find it
# regardless of the working directory.
# config.py lives at  <root>/backend/app/config.py
# .env lives at       <root>/.env
_ROOT_ENV = Path(__file__).parent.parent.parent / ".env"
_LOCAL_ENV = Path(".env")  # fallback for Docker / any CWD-based usage


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    algorithm: str = "HS256"
    # Default 60 so the 30-min /auth/refresh window has room to do
    # something useful (refresh allowed in the *last* 30 min of life).
    access_token_expire_minutes: int = 60
    environment: str = "development"

    # Rate limiting — kill switch + threshold knobs so tests can disable
    # the limiter and ops can tune limits without code changes.
    rate_limit_enabled: bool = True
    rate_limit_login: str = "5/minute"
    rate_limit_register: str = "3/minute"
    rate_limit_default: str = "60/minute"

    # CORS — comma-separated list of additional allowed origins (the
    # environment-driven defaults below cover the common dev/compose cases).
    cors_extra_origins: str = ""

    # Logging — "json" for production, "text" for local readability.
    log_format: str = "text"

    # Background scheduler. The APScheduler jobs (daily ETL, live updates,
    # matchup refresh, daily picks) run in a DEDICATED worker process
    # (app/worker.py), not inside the web process — so scaling the API to
    # multiple replicas can't double-fire cron jobs. Default False means the
    # web app never schedules; set RUN_SCHEDULER=true only for a single
    # all-in-one process (e.g. local dev without a separate worker).
    run_scheduler: bool = False

    # The Odds API (https://the-odds-api.com) — free tier ships 500
    # requests/month, which comfortably covers a daily MLB pull.
    # If unset, OddsClient refuses to construct and the daily picks
    # feature degrades gracefully to "odds unavailable".
    the_odds_api_key: str | None = None

    @property
    def cors_origins(self) -> list[str]:
        """Resolved CORS allowlist for this environment."""
        if self.environment.lower() == "production":
            # Production: ONLY the explicit allowlist; no localhost fallbacks.
            return [o.strip() for o in self.cors_extra_origins.split(",") if o.strip()]
        # Dev: vite + the docker-compose nginx + any extras configured.
        defaults = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ]
        extras = [o.strip() for o in self.cors_extra_origins.split(",") if o.strip()]
        return defaults + extras

    model_config = SettingsConfigDict(
        env_file=[str(_ROOT_ENV), str(_LOCAL_ENV)],
        extra="ignore",
    )


settings = Settings()
