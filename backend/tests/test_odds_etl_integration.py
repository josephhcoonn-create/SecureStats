"""
Task 8.7 — odds ETL integration tests.

Covers:
- Scheduler registers the 5 expected jobs at startup
- fetch_daily_odds job is a no-op when THE_ODDS_API_KEY is unset
- fetch_odds_update job is gated on hour AND key presence
- generate_daily_picks job calls get_daily_picks (which auto-snapshots)
- POST /etl/trigger-odds requires admin, refuses when key is missing,
  surfaces quota on success, returns 502 on Odds API error
- OddsRefreshResult dataclass behaves correctly
"""
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import AsyncClient

from app.auth.jwt_handler import create_access_token
from app.models.game import Game
from app.services.odds_client import BASE_URL
from app.services.odds_persistence import OddsRefreshResult
from tests.conftest import TestSessionLocal

# ── Sample response (mirrors test_odds_client.py minimally) ─────────────────


_SAMPLE_RESPONSE = [
    {
        "id": "g1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-05-22T23:05:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "New York Yankees", "price": -150},
                            {"name": "Boston Red Sox", "price": 130},
                        ],
                    }
                ],
            }
        ],
    }
]


# ══════════════════════════════════════════════════════════════════════════════
# OddsRefreshResult dataclass
# ══════════════════════════════════════════════════════════════════════════════


class TestOddsRefreshResult:
    def test_bool_collapses_to_rows_inserted(self) -> None:
        assert bool(OddsRefreshResult(rows_inserted=0, quota_remaining=499, quota_used=1)) is False
        assert bool(OddsRefreshResult(rows_inserted=3, quota_remaining=499, quota_used=1)) is True

    def test_int_collapses_to_rows_inserted(self) -> None:
        assert int(OddsRefreshResult(rows_inserted=5, quota_remaining=None, quota_used=None)) == 5


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler job registration
# ══════════════════════════════════════════════════════════════════════════════


class TestSchedulerRegistration:
    """Drive start_scheduler() and confirm every job we expect is present.
    Then shut down so we don't leak background tasks into other tests."""

    def test_all_jobs_registered(self) -> None:
        from app.etl import scheduler as sched_mod

        # Use a fresh scheduler so prior test runs don't pollute
        sched_mod._scheduler = None

        scheduler = sched_mod.start_scheduler()
        try:
            ids = {job.id for job in scheduler.get_jobs()}
            assert ids == {
                "daily_etl",
                "live_update",
                "fetch_daily_odds",
                "fetch_odds_update",
                "update_matchup_data",
                "generate_daily_picks",
            }
        finally:
            sched_mod.stop_scheduler()
            sched_mod._scheduler = None


# ══════════════════════════════════════════════════════════════════════════════
# Scheduler job callables
# ══════════════════════════════════════════════════════════════════════════════


class TestSchedulerJobs:
    async def test_fetch_daily_odds_skips_when_no_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings
        from app.etl.scheduler import _fetch_daily_odds_job

        monkeypatch.setattr(settings, "the_odds_api_key", None)

        # Patch refresh_odds_for_date so we can assert it's NOT called
        with patch(
            "app.services.odds_persistence.refresh_odds_for_date",
            new_callable=AsyncMock,
        ) as mock_refresh:
            await _fetch_daily_odds_job()
        mock_refresh.assert_not_called()

    @respx.mock
    async def test_fetch_daily_odds_runs_when_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings
        from app.etl.scheduler import _fetch_daily_odds_job

        monkeypatch.setattr(settings, "the_odds_api_key", "fake-key")
        respx.get(BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_SAMPLE_RESPONSE,
                headers={"x-requests-remaining": "498", "x-requests-used": "2"},
            )
        )
        # Seed a matching game so persistence has something to attach to
        async with TestSessionLocal() as session:
            session.add(
                Game(
                    mlb_game_id=70001,
                    date=date.today(),
                    home_team="New York Yankees",
                    away_team="Boston Red Sox",
                    status="Scheduled",
                )
            )
            await session.commit()

        # Reroute commence_time so date(commence) matches today
        from app.services import odds_client as oc_mod

        original_parse = oc_mod.parse_odds_response

        def _parse_today(raw):
            rows = original_parse(raw)
            for r in rows:
                r["commence_time"] = datetime.combine(
                    date.today(), datetime.min.time()
                )
            return rows

        with patch.object(oc_mod, "parse_odds_response", _parse_today), \
             patch("app.services.odds_persistence.parse_odds_response", _parse_today):
            await _fetch_daily_odds_job()
        # No exception = success; the actual row count is exercised by the
        # persistence layer's own tests.

    async def test_fetch_odds_update_skips_outside_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import settings
        from app.etl import scheduler as sched_mod

        monkeypatch.setattr(settings, "the_odds_api_key", "fake-key")

        # Mock datetime so the hour is well outside 10-19
        class _FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return datetime(2026, 5, 22, 3, 0, 0)

        monkeypatch.setattr(sched_mod, "datetime", _FakeDT)

        with patch(
            "app.services.odds_persistence.refresh_odds_for_date",
            new_callable=AsyncMock,
        ) as mock_refresh:
            await sched_mod._fetch_odds_update_job()
        mock_refresh.assert_not_called()

    async def test_generate_daily_picks_calls_analytics(self) -> None:
        from app.etl.scheduler import _generate_daily_picks_job

        # Patch get_daily_picks so we can assert it was called WITHOUT
        # exercising the real model machinery (covered by 8.2 + 8.5 tests).
        with patch(
            "app.services.analytics.get_daily_picks",
            new_callable=AsyncMock,
            return_value={"picks": [], "games_considered": 0},
        ) as mock_picks:
            await _generate_daily_picks_job()
        mock_picks.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# POST /etl/trigger-odds endpoint
# ══════════════════════════════════════════════════════════════════════════════


class TestTriggerOddsEndpoint:
    async def test_viewer_blocked(
        self, client: AsyncClient, viewer_user: dict
    ) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": viewer_user["username"], "password": "Testpass123"},
        )
        token = login.json()["access_token"]
        resp = await client.post(
            "/api/v1/etl/trigger-odds",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    async def test_503_when_api_key_missing(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "the_odds_api_key", None)
        token = create_access_token({"sub": "1", "role": "admin"})
        resp = await client.post(
            "/api/v1/etl/trigger-odds",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
        assert "THE_ODDS_API_KEY" in resp.text

    @respx.mock
    async def test_admin_happy_path_surfaces_quota(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "the_odds_api_key", "fake-key")

        # No matching games seeded → matched=[] → quota still surfaces
        respx.get(BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=_SAMPLE_RESPONSE,
                headers={
                    "x-requests-remaining": "493",
                    "x-requests-used": "7",
                },
            )
        )
        token = create_access_token({"sub": "1", "role": "admin"})
        resp = await client.post(
            "/api/v1/etl/trigger-odds",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["target_date"] == str(date.today())
        assert body["rows_inserted"] == 0  # no games seeded for today
        assert body["quota_remaining"] == 493
        assert body["quota_used"] == 7
        assert body["success"] is True

    @respx.mock
    async def test_502_on_odds_api_error(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "the_odds_api_key", "fake-key")
        respx.get(BASE_URL).mock(
            return_value=httpx.Response(500, text="upstream blew up")
        )
        token = create_access_token({"sub": "1", "role": "admin"})
        resp = await client.post(
            "/api/v1/etl/trigger-odds",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 502
        assert "Odds API error" in resp.json()["detail"]
