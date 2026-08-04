"""
Tests for the Phase 8 multi-factor hit probability model.

Covers:
- Handedness modifier (pure function — fast unit tests)
- Confidence buckets (pure function)
- Pitcher composite (pure function)
- calculate_enhanced_hit_probability against seeded DB with + without pitcher
- Probability clamp to [0.05, 0.95]
- Graceful degradation: missing batter data, missing pitcher data
- threshold_met flag
- get_daily_picks: empty days, threshold filtering, sort order
- /stats/hit-probability-v2 + /stats/daily-picks endpoints (analyst RBAC)
"""
from datetime import date, timedelta

import pytest
from httpx import AsyncClient

from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.pitcher_stats import PitcherStats
from app.models.player import Player
from app.services.analytics import (
    DAILY_PICK_THRESHOLD,
    _calculate_confidence,
    _handedness_modifier,
    _pitcher_composite,
    calculate_enhanced_hit_probability,
    get_daily_picks,
)
from tests.conftest import TestSessionLocal

TODAY = date.today()


# ══════════════════════════════════════════════════════════════════════════════
# Pure-function unit tests (no DB)
# ══════════════════════════════════════════════════════════════════════════════


class TestHandednessModifier:
    @pytest.mark.parametrize(
        "bats,throws,expected",
        [
            ("L", "R", 0.015),  # opposite-hand → boost
            ("R", "L", 0.015),
            ("R", "R", -0.010),  # same-hand → penalty
            ("L", "L", -0.010),
            ("S", "R", 0.015),  # switch hitter always favorable
            ("S", "L", 0.015),
            (None, "R", 0.0),  # missing data → no modifier
            ("R", None, 0.0),
            (None, None, 0.0),
        ],
    )
    def test_modifier_matrix(
        self, bats: str | None, throws: str | None, expected: float
    ) -> None:
        assert _handedness_modifier(bats, throws) == expected


class TestConfidence:
    @pytest.mark.parametrize(
        "season_ab,pitcher_ip,expected",
        [
            (0, 0, 30),           # no data → low
            (29, 0, 30),
            (30, 0, 60),          # → medium
            (99, 0, 60),
            (100, 0, 85),         # → high
            (250, 0, 85),
            (100, 50, 95),        # + pitcher boost
            (10, 60, 40),         # boost stacks even on low
            (250, 1000, 95),      # capped at 100 (would be 95)
        ],
    )
    def test_confidence_buckets(
        self, season_ab: int, pitcher_ip: float, expected: int
    ) -> None:
        assert _calculate_confidence(season_ab, pitcher_ip) == expected


class TestPitcherComposite:
    def test_neutral_pitcher_returns_near_league_avg(self) -> None:
        # ERA = 4.20, WHIP = 1.30 (the league baselines) → no shift
        result = _pitcher_composite(era=4.20, whip=1.30, league_avg=0.250, handedness=0)
        assert result == pytest.approx(0.250, abs=0.001)

    def test_better_pitcher_lowers_hit_rate(self) -> None:
        # ERA = 2.00 (much better than league 4.20) → era_term shrinks
        # WHIP = 1.00 (better than league 1.30) → whip_term shrinks
        # Math:
        #   era_term  = (2.0 / 4.2)  * 0.25 = 0.119
        #   whip_term = (1.0 / 1.3)  * 0.25 = 0.192
        #   composite = (0.119 + 0.192) / 2 = 0.156
        # → well below league_avg (.250) as expected for an elite pitcher.
        result = _pitcher_composite(era=2.00, whip=1.00, league_avg=0.250, handedness=0)
        assert result < 0.250  # ace pitcher depresses batter's hit-rate

    def test_handedness_added_after_blend(self) -> None:
        baseline = _pitcher_composite(4.20, 1.30, 0.250, handedness=0)
        boosted = _pitcher_composite(4.20, 1.30, 0.250, handedness=0.015)
        assert boosted == pytest.approx(baseline + 0.015, abs=0.001)


# ══════════════════════════════════════════════════════════════════════════════
# Fixture: a deterministic mini-league
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def mini_league() -> dict:
    """
    Seed two teams, one game today, batters with predictable splits,
    plus one pitcher with full PitcherStats.

    Returns a dict of {game_id, batter_id, pitcher_id, ...} for assertions.
    """
    async with TestSessionLocal() as session:
        # ── Players ──
        batter = Player(
            mlb_id=10001,
            full_name="Hot Hitter",
            team="Phillies",
            position="LF",
            bats="L",
            throws="L",
        )
        pitcher = Player(
            mlb_id=10002,
            full_name="Ace Starter",
            team="Yankees",
            position="P",
            bats="R",
            throws="R",
        )
        # Same-hand opponent — pulls handedness modifier negative
        opposite_pitcher = Player(
            mlb_id=10003,
            full_name="Lefty Reliever",
            team="Yankees",
            position="P",
            bats="L",
            throws="L",
        )
        # Cold hitter — should never clear the 0.80 threshold
        cold_batter = Player(
            mlb_id=10004,
            full_name="Cold Bat",
            team="Phillies",
            position="2B",
            bats="R",
            throws="R",
        )
        session.add_all([batter, pitcher, opposite_pitcher, cold_batter])
        await session.flush()

        # ── Pitcher's season stats — well above league avg for both rates ──
        session.add(
            PitcherStats(
                player_id=pitcher.id,
                season=TODAY.year,
                games=15,
                innings_pitched=95.0,  # > 50 → confidence boost
                hits_allowed=70,
                walks_allowed=18,
                strikeouts=110,
                era=2.85,
                whip=0.93,
            )
        )

        # ── Today's game ──
        game_today = Game(
            mlb_game_id=99001,
            date=TODAY,
            home_team="Phillies",
            away_team="Yankees",
            status="Scheduled",
        )
        session.add(game_today)
        await session.flush()

        # ── Past 5 games for "Phillies vs Yankees" so _likely_starters
        #     picks up both batters as today's lineup candidates ─────────
        past_games = []
        for i in range(1, 6):
            g = Game(
                mlb_game_id=99100 + i,
                date=TODAY - timedelta(days=i),
                home_team="Phillies",
                away_team="Yankees",
                status="Final",
                home_score=5,
                away_score=4,
            )
            session.add(g)
            past_games.append(g)
        await session.flush()

        # Batting stats for Hot Hitter — appears in all 5 past games,
        # 4-for-5 each = .800 recent avg (well above the 0.80 threshold)
        for g in past_games:
            session.add(
                BattingStats(
                    player_id=batter.id,
                    game_id=g.id,
                    at_bats=5,
                    hits=4,
                    home_runs=0,
                    rbis=2,
                    batting_avg=0.800,
                    on_base_pct=0.800,
                    slugging_pct=1.200,
                )
            )
            # Cold bat: 0-for-4 every game
            session.add(
                BattingStats(
                    player_id=cold_batter.id,
                    game_id=g.id,
                    at_bats=4,
                    hits=0,
                    home_runs=0,
                    rbis=0,
                    batting_avg=0.000,
                    on_base_pct=0.000,
                    slugging_pct=0.000,
                )
            )

        await session.commit()
        return {
            "game_id": game_today.id,
            "batter_id": batter.id,
            "cold_batter_id": cold_batter.id,
            "pitcher_id": pitcher.id,
            "opposite_pitcher_id": opposite_pitcher.id,
        }


# ══════════════════════════════════════════════════════════════════════════════
# calculate_enhanced_hit_probability
# ══════════════════════════════════════════════════════════════════════════════


class TestEnhancedHitProbability:
    async def test_hot_hitter_clears_threshold(self, mini_league: dict) -> None:
        async with TestSessionLocal() as session:
            result = await calculate_enhanced_hit_probability(
                session,
                player_id=mini_league["batter_id"],
                game_id=mini_league["game_id"],
                pitcher_id=mini_league["pitcher_id"],
            )
        assert 0.05 <= result["probability"] <= 0.95
        assert result["threshold_met"] is True
        assert result["probability"] >= DAILY_PICK_THRESHOLD
        # Pitcher boost (95 IP > 50) + season AB (5*5=25 < 30) →
        # low (30) + boost (10) = 40
        assert result["confidence"] == 40
        assert result["pitcher_name"] == "Ace Starter"
        assert result["factors"]["pitcher_era"] == 2.85
        assert result["factors"]["pitcher_whip"] == 0.93
        # L vs R = opposite hand → +0.015
        assert result["factors"]["handedness_matchup"] == 0.015

    async def test_cold_hitter_does_not_clear_threshold(
        self, mini_league: dict
    ) -> None:
        async with TestSessionLocal() as session:
            result = await calculate_enhanced_hit_probability(
                session,
                player_id=mini_league["cold_batter_id"],
                game_id=mini_league["game_id"],
                pitcher_id=mini_league["pitcher_id"],
            )
        assert result["threshold_met"] is False
        assert result["probability"] < DAILY_PICK_THRESHOLD

    async def test_same_hand_matchup_applies_penalty(
        self, mini_league: dict
    ) -> None:
        async with TestSessionLocal() as session:
            result = await calculate_enhanced_hit_probability(
                session,
                player_id=mini_league["batter_id"],  # L
                game_id=mini_league["game_id"],
                pitcher_id=mini_league["opposite_pitcher_id"],  # L → same
            )
        assert result["factors"]["handedness_matchup"] == -0.010

    async def test_no_pitcher_falls_back_to_league_baseline(
        self, mini_league: dict
    ) -> None:
        async with TestSessionLocal() as session:
            result = await calculate_enhanced_hit_probability(
                session,
                player_id=mini_league["batter_id"],
                game_id=mini_league["game_id"],
                pitcher_id=None,
            )
        # No pitcher → no pitcher_name, no era/whip surfaced
        assert result["pitcher_id"] is None
        assert result["pitcher_name"] is None
        assert result["factors"]["pitcher_era"] is None
        assert result["factors"]["pitcher_whip"] is None
        assert result["factors"]["handedness_matchup"] == 0.0
        # Still produces a valid probability
        assert 0.05 <= result["probability"] <= 0.95

    async def test_no_batter_data_returns_clamped_probability(self) -> None:
        # Fresh player with zero batting stats → all factors fall back to league avg.
        async with TestSessionLocal() as session:
            ghost = Player(
                mlb_id=10999,
                full_name="No-stats Newbie",
                team="Mets",
                position="C",
            )
            session.add(ghost)
            await session.commit()
            await session.refresh(ghost)

            result = await calculate_enhanced_hit_probability(
                session, player_id=ghost.id, game_id=None, pitcher_id=None
            )
        assert 0.05 <= result["probability"] <= 0.95
        assert result["factors"]["recent_avg"] is None
        assert result["factors"]["season_avg"] is None
        assert result["factors"]["career_avg"] is None

    async def test_unknown_player_raises_404(self) -> None:
        from fastapi import HTTPException

        async with TestSessionLocal() as session:
            with pytest.raises(HTTPException) as exc:
                await calculate_enhanced_hit_probability(
                    session, player_id=999_999, game_id=None
                )
        assert exc.value.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# get_daily_picks
# ══════════════════════════════════════════════════════════════════════════════


class TestDailyPicks:
    async def test_returns_empty_when_no_games_today(self) -> None:
        async with TestSessionLocal() as session:
            result = await get_daily_picks(session)
        assert result["games_considered"] == 0
        assert result["picks"] == []

    async def test_returns_hot_hitter_above_threshold(
        self, mini_league: dict
    ) -> None:
        async with TestSessionLocal() as session:
            # Threshold high enough to filter out the .000 cold bat
            # (whose game-level prob is dragged up by the pitcher term
            # but still well under a real hit hitter's number).
            # min_season_ab=0 disables the playing-time gate — this fixture
            # seeds only 5 games, so the batters are below the production floor.
            result = await get_daily_picks(
                session, min_probability=0.85, min_confidence=30, min_season_ab=0
            )
        assert result["games_considered"] == 1
        assert result["candidates_evaluated"] >= 1
        # Hot Hitter must be in the picks; cold bat must NOT be.
        picks_names = [p["player_name"] for p in result["picks"]]
        assert "Hot Hitter" in picks_names
        assert "Cold Bat" not in picks_names

    async def test_picks_sorted_descending(self, mini_league: dict) -> None:  # noqa: ARG002
        async with TestSessionLocal() as session:
            result = await get_daily_picks(
                session, min_probability=0.0, min_confidence=0
            )
        probs = [p["probability"] for p in result["picks"]]
        assert probs == sorted(probs, reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# API endpoints (analyst RBAC)
# ══════════════════════════════════════════════════════════════════════════════


class TestEnhancedEndpoints:
    async def test_v2_endpoint_returns_response(
        self,
        client: AsyncClient,
        analyst_user: dict,  # noqa: ARG002
        mini_league: dict,
    ) -> None:
        from app.auth.jwt_handler import create_access_token

        token = create_access_token({"sub": "1", "role": "analyst"})
        resp = await client.get(
            f"/api/v1/stats/hit-probability-v2/{mini_league['batter_id']}",
            params={
                "game_id": mini_league["game_id"],
                "pitcher_id": mini_league["pitcher_id"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["player_name"] == "Hot Hitter"
        assert body["pitcher_name"] == "Ace Starter"
        assert 0.05 <= body["probability"] <= 0.95
        assert "factors" in body and "league_avg" in body["factors"]

    async def test_daily_picks_endpoint_requires_analyst(
        self, client: AsyncClient, viewer_user: dict
    ) -> None:
        # viewer_user fixture already registered a viewer; log them in
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": viewer_user["username"], "password": "Testpass123"},
        )
        token = login.json()["access_token"]
        resp = await client.get(
            "/api/v1/stats/daily-picks",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    async def test_daily_picks_endpoint_returns_envelope(
        self, client: AsyncClient, mini_league: dict  # noqa: ARG002
    ) -> None:
        from app.auth.jwt_handler import create_access_token

        token = create_access_token({"sub": "1", "role": "analyst"})
        resp = await client.get(
            "/api/v1/stats/daily-picks",
            params={"min_probability": 0.50, "min_confidence": 30},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "target_date" in body
        assert "picks" in body
        assert body["games_considered"] >= 1
