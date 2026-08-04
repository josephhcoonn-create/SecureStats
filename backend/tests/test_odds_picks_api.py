"""
Tests for /odds/* and /picks/* endpoints (Task 8.4).

Covers:
- Persistence: refresh_odds_for_date inserts matched rows + is idempotent
- /odds/today returns games grouped by sportsbook (lazy-refresh path NOT
  exercised here — see test_odds_client.py for the HTTP mock tests)
- /odds/game/{id} returns line-movement history newest-first
- /picks/today envelope + odds attached per pick
- /picks/player/{id} resolves next game + probable pitcher
- /picks/history aggregates accuracy across past dates
- RBAC: viewer can hit /odds/*, viewer is BLOCKED from /picks/*
"""
from datetime import date, datetime, timedelta

import httpx
import pytest
import respx
from httpx import AsyncClient

from app.auth.jwt_handler import create_access_token
from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.odds import GameOdds
from app.models.pitcher_stats import PitcherStats
from app.models.player import Player
from app.services.odds_client import BASE_URL
from app.services.odds_persistence import (
    date_has_odds,
    get_latest_odds_for_games,
    refresh_odds_for_date,
)
from tests.conftest import TestSessionLocal

# ── Helpers ───────────────────────────────────────────────────────────────────


def _viewer_token() -> str:
    return create_access_token({"sub": "1", "role": "viewer"})


def _analyst_token() -> str:
    return create_access_token({"sub": "1", "role": "analyst"})


_SAMPLE_ODDS_RESPONSE = [
    {
        "id": "abc",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "commence_time": "2026-05-22T23:05:00Z",
        "bookmakers": [
            {
                "key": "draftkings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "New York Yankees", "price": -150},
                            {"name": "Boston Red Sox", "price": 130},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 8.5},
                            {"name": "Under", "price": -110, "point": 8.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "New York Yankees", "price": -145},
                            {"name": "Boston Red Sox", "price": 125},
                        ],
                    }
                ],
            },
        ],
    }
]


# ══════════════════════════════════════════════════════════════════════════════
# Persistence layer
# ══════════════════════════════════════════════════════════════════════════════


class TestRefreshOddsForDate:
    @respx.mock
    async def test_inserts_matched_rows(self) -> None:
        async with TestSessionLocal() as session:
            session.add(
                Game(
                    mlb_game_id=80001,
                    date=date(2026, 5, 22),
                    home_team="New York Yankees",
                    away_team="Boston Red Sox",
                    status="Scheduled",
                )
            )
            await session.commit()

        respx.get(BASE_URL).mock(
            return_value=httpx.Response(
                200, json=_SAMPLE_ODDS_RESPONSE, headers={"x-requests-remaining": "499"}
            )
        )

        async with TestSessionLocal() as session:
            written = await refresh_odds_for_date(
                session, api_key="fake", target_date=date(2026, 5, 22)
            )
        assert written.rows_inserted == 2  # one row per bookmaker (DK + FD)
        # Quota header surfaced from the upstream response
        assert written.quota_remaining == 499

        # And date_has_odds now reports True
        async with TestSessionLocal() as session:
            assert await date_has_odds(session, date(2026, 5, 22)) is True

    @respx.mock
    async def test_idempotent_when_rerun_same_minute(self) -> None:
        async with TestSessionLocal() as session:
            session.add(
                Game(
                    mlb_game_id=80002,
                    date=date(2026, 5, 22),
                    home_team="New York Yankees",
                    away_team="Boston Red Sox",
                    status="Scheduled",
                )
            )
            await session.commit()

        # Pin fetched_at to a known minute so both calls collide on the
        # unique constraint (game_id, sportsbook, fetched_at).
        fixed_now = datetime(2026, 5, 22, 12, 0, 0)
        respx.get(BASE_URL).mock(
            return_value=httpx.Response(200, json=_SAMPLE_ODDS_RESPONSE)
        )

        with respx.mock:
            respx.get(BASE_URL).mock(
                return_value=httpx.Response(200, json=_SAMPLE_ODDS_RESPONSE)
            )
            import app.services.odds_client as oc_mod

            async def _fixed_parse(raw):  # type: ignore[no-untyped-def]
                rows = oc_mod.parse_odds_response.__wrapped__(raw) if hasattr(oc_mod.parse_odds_response, "__wrapped__") else oc_mod.parse_odds_response(raw)
                for r in rows:
                    r["fetched_at"] = fixed_now
                return rows

            # Monkeypatch parse so fetched_at is identical across calls
            orig_parse = oc_mod.parse_odds_response

            async def _async_parse(raw):
                return await _fixed_parse(raw)

            import app.services.odds_persistence as op_mod

            def _sync_fixed(raw):
                rows = orig_parse(raw)
                for r in rows:
                    r["fetched_at"] = fixed_now
                return rows

            op_mod.parse_odds_response = _sync_fixed  # type: ignore[attr-defined]
            try:
                async with TestSessionLocal() as session:
                    first = await refresh_odds_for_date(session, "fake", date(2026, 5, 22))
                async with TestSessionLocal() as session:
                    second = await refresh_odds_for_date(session, "fake", date(2026, 5, 22))
            finally:
                op_mod.parse_odds_response = orig_parse  # restore

        assert first.rows_inserted == 2
        assert second.rows_inserted == 0  # unique constraint -> nothing new

    async def test_no_matching_games_returns_zero(self) -> None:
        # No Game rows seeded for the dates in the response
        with respx.mock:
            respx.get(BASE_URL).mock(
                return_value=httpx.Response(200, json=_SAMPLE_ODDS_RESPONSE)
            )
            async with TestSessionLocal() as session:
                written = await refresh_odds_for_date(
                    session, api_key="fake", target_date=date(2026, 5, 22)
                )
        assert written.rows_inserted == 0


# ══════════════════════════════════════════════════════════════════════════════
# get_latest_odds_for_games
# ══════════════════════════════════════════════════════════════════════════════


class TestGetLatestOdds:
    async def test_returns_latest_per_book_per_game(self) -> None:
        async with TestSessionLocal() as session:
            g1 = Game(
                mlb_game_id=81001,
                date=date(2026, 5, 22),
                home_team="A",
                away_team="B",
                status="Final",
            )
            g2 = Game(
                mlb_game_id=81002,
                date=date(2026, 5, 22),
                home_team="C",
                away_team="D",
                status="Final",
            )
            session.add_all([g1, g2])
            await session.flush()

            # Two snapshots for g1/draftkings (newer must win), one for g1/fanduel
            session.add_all(
                [
                    GameOdds(
                        game_id=g1.id,
                        sportsbook="draftkings",
                        home_moneyline=-140,
                        away_moneyline=120,
                        fetched_at=datetime(2026, 5, 22, 9, 0, 0),
                    ),
                    GameOdds(
                        game_id=g1.id,
                        sportsbook="draftkings",
                        home_moneyline=-155,  # latest line
                        away_moneyline=135,
                        fetched_at=datetime(2026, 5, 22, 11, 0, 0),
                    ),
                    GameOdds(
                        game_id=g1.id,
                        sportsbook="fanduel",
                        home_moneyline=-148,
                        away_moneyline=125,
                        fetched_at=datetime(2026, 5, 22, 10, 0, 0),
                    ),
                ]
            )
            await session.commit()

            latest = await get_latest_odds_for_games(session, [g1.id, g2.id])

        assert g1.id in latest
        assert g2.id not in latest  # no rows -> not in dict
        by_book = {r.sportsbook: r for r in latest[g1.id]}
        assert by_book["draftkings"].home_moneyline == -155  # newest wins
        assert by_book["fanduel"].home_moneyline == -148


# ══════════════════════════════════════════════════════════════════════════════
# /odds endpoints (HTTP layer)
# ══════════════════════════════════════════════════════════════════════════════


class TestOddsEndpoints:
    async def test_odds_today_groups_by_book(self, client: AsyncClient) -> None:
        today = date.today()
        async with TestSessionLocal() as session:
            game = Game(
                mlb_game_id=82001,
                date=today,
                home_team="A",
                away_team="B",
                status="Scheduled",
            )
            session.add(game)
            await session.flush()
            session.add_all(
                [
                    GameOdds(
                        game_id=game.id,
                        sportsbook="draftkings",
                        home_moneyline=-150,
                        away_moneyline=130,
                        over_under=8.5,
                        fetched_at=datetime.utcnow(),
                    ),
                    GameOdds(
                        game_id=game.id,
                        sportsbook="fanduel",
                        home_moneyline=-145,
                        away_moneyline=125,
                        fetched_at=datetime.utcnow(),
                    ),
                ]
            )
            await session.commit()

        resp = await client.get(
            "/api/v1/odds/today",
            headers={"Authorization": f"Bearer {_viewer_token()}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["games_with_odds"] == 1
        assert body["games_without_odds"] == 0
        assert len(body["games"]) == 1
        game_payload = body["games"][0]
        assert set(game_payload["bookmakers"].keys()) == {"draftkings", "fanduel"}
        assert game_payload["bookmakers"]["draftkings"]["home_moneyline"] == -150

    async def test_odds_today_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/odds/today")
        assert resp.status_code == 403  # no bearer

    async def test_odds_game_history_newest_first(self, client: AsyncClient) -> None:
        async with TestSessionLocal() as session:
            game = Game(
                mlb_game_id=83001,
                date=date(2026, 5, 22),
                home_team="A",
                away_team="B",
                status="Final",
            )
            session.add(game)
            await session.flush()
            session.add_all(
                [
                    GameOdds(
                        game_id=game.id,
                        sportsbook="draftkings",
                        home_moneyline=-140,
                        fetched_at=datetime(2026, 5, 22, 9, 0, 0),
                    ),
                    GameOdds(
                        game_id=game.id,
                        sportsbook="draftkings",
                        home_moneyline=-155,
                        fetched_at=datetime(2026, 5, 22, 11, 0, 0),
                    ),
                ]
            )
            await session.commit()
            game_id = game.id

        resp = await client.get(
            f"/api/v1/odds/game/{game_id}",
            headers={"Authorization": f"Bearer {_viewer_token()}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["snapshot_count"] == 2
        snapshots = body["snapshots"]
        # Newest first → -155 then -140
        assert snapshots[0]["home_moneyline"] == -155
        assert snapshots[1]["home_moneyline"] == -140

    async def test_odds_game_404_for_missing(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/odds/game/999999",
            headers={"Authorization": f"Bearer {_viewer_token()}"},
        )
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# /picks endpoints
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def picks_world() -> dict:
    """Seed a today-game with a hot batter and an opposing pitcher."""
    today = date.today()
    async with TestSessionLocal() as session:
        batter = Player(
            mlb_id=84001,
            full_name="Hot Batter",
            team="Phillies",
            position="LF",
            bats="L",
        )
        pitcher = Player(
            mlb_id=84002,
            full_name="Opposing Ace",
            team="Yankees",
            position="P",
            throws="R",
        )
        session.add_all([batter, pitcher])
        await session.flush()

        session.add(
            PitcherStats(
                player_id=pitcher.id,
                game_id=None,
                season=today.year,
                is_season_aggregate=True,
                games=20,
                innings_pitched=120.0,
                hits_allowed=90,
                earned_runs=40,
                walks_allowed=25,
                strikeouts=130,
                era=3.00,
                whip=0.96,
            )
        )

        # Today's game with probable pitchers set
        game_today = Game(
            mlb_game_id=84101,
            date=today,
            home_team="Phillies",
            away_team="Yankees",
            status="Scheduled",
            home_probable_pitcher_id=None,
            away_probable_pitcher_id=pitcher.id,
        )
        session.add(game_today)
        await session.flush()

        # Past 5 games so _likely_starters picks Hot Batter as a starter
        for i in range(1, 6):
            g = Game(
                mlb_game_id=84200 + i,
                date=today - timedelta(days=i),
                home_team="Phillies",
                away_team="Yankees",
                status="Final",
                home_score=5,
                away_score=4,
            )
            session.add(g)
            await session.flush()
            session.add(
                BattingStats(
                    player_id=batter.id,
                    game_id=g.id,
                    at_bats=5,
                    hits=4,  # .800 recent
                    home_runs=0,
                    rbis=2,
                    batting_avg=0.800,
                    on_base_pct=0.800,
                    slugging_pct=1.200,
                )
            )

        # Latest odds for the today game
        session.add(
            GameOdds(
                game_id=game_today.id,
                sportsbook="draftkings",
                home_moneyline=-130,
                away_moneyline=110,
                over_under=8.5,
                fetched_at=datetime.utcnow(),
            )
        )

        await session.commit()
        return {
            "today": today,
            "game_id": game_today.id,
            "batter_id": batter.id,
            "pitcher_id": pitcher.id,
        }


class TestPicksToday:
    async def test_returns_hot_batter_with_factors_and_odds(
        self, client: AsyncClient, picks_world: dict
    ) -> None:
        resp = await client.get(
            "/api/v1/picks/today",
            # min_season_ab=0 disables the playing-time gate — the fixture
            # seeds a small-sample batter below the production floor.
            params={"min_probability": 0.85, "min_confidence": 30, "min_season_ab": 0},
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pick_count"] >= 1
        names = [p["player_name"] for p in body["picks"]]
        assert "Hot Batter" in names

        hot = next(p for p in body["picks"] if p["player_name"] == "Hot Batter")
        # Factors envelope populated
        assert hot["factors"]["recent_avg"] is not None
        assert hot["factors"]["league_avg"] is not None
        # Latest odds attached for the pick's game
        assert "draftkings" in hot["odds"]
        assert hot["odds"]["draftkings"]["home_moneyline"] == -130

    async def test_picks_today_requires_analyst(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(
            "/api/v1/picks/today",
            headers={"Authorization": f"Bearer {_viewer_token()}"},
        )
        assert resp.status_code == 403


class TestPickForPlayer:
    async def test_resolves_next_game_and_pitcher(
        self, client: AsyncClient, picks_world: dict
    ) -> None:
        resp = await client.get(
            f"/api/v1/picks/player/{picks_world['batter_id']}",
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["player_name"] == "Hot Batter"
        assert body["pitcher_name"] == "Opposing Ace"
        assert body["opponent"] == "Yankees"
        assert body["game_id"] == picks_world["game_id"]
        # Hot batter facing a real pitcher → factors include real ERA/WHIP
        assert body["factors"]["pitcher_era"] == 3.00
        assert body["factors"]["pitcher_whip"] == 0.96

    async def test_404_for_unknown_player(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/picks/player/999999",
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )
        assert resp.status_code == 404


class TestPicksHistory:
    async def test_aggregates_accuracy_across_days(
        self, client: AsyncClient, picks_world: dict
    ) -> None:
        # Look back 7 days — picks_world has 5 past games, all with Hot Batter
        # going 4-for-5 (a hit, so accuracy should be 100%)
        resp = await client.get(
            "/api/v1/picks/history",
            params={"days": 7, "min_probability": 0.5, "min_confidence": 30},
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["days"] == 7
        assert isinstance(body["by_date"], list)
        # by_date should be sorted newest first
        if len(body["by_date"]) >= 2:
            d0 = body["by_date"][0]["target_date"]
            d1 = body["by_date"][1]["target_date"]
            assert d0 > d1

        # If any past day had picks, accuracy on Hot Batter must be 100%
        for day in body["by_date"]:
            if day["pick_count"] > 0:
                assert day["hits"] == day["pick_count"]
                assert day["accuracy_pct"] == 100.0

    async def test_history_caps_at_14_days(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/picks/history",
            params={"days": 99},
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )
        assert resp.status_code == 422  # query validation

    async def test_history_requires_analyst(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/picks/history",
            headers={"Authorization": f"Bearer {_viewer_token()}"},
        )
        assert resp.status_code == 403
