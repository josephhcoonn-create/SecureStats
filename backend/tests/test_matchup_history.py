"""
Task 6 — head-to-head (batter-vs-pitcher) coverage.

  1. get_batter_vs_pitcher parses a mocked vsPlayerTotal response
  2. get_batter_vs_pitcher returns None when they've never faced each other
  3. MatchupHistory upsert — create then update-in-place
  4. Dynamic H2H weight by sample size (20 / 8 / 3 AB → 0.15 / 0.075 / 0)
  5. A strong "owns the pitcher" H2H lifts the model's probability
"""
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.matchup_history import MatchupHistory
from app.models.player import Player
from app.services.analytics import calculate_enhanced_hit_probability
from app.services.etl import update_matchup_history
from app.services.mlb_client import MLBClient
from tests.conftest import TestSessionLocal
from tests.fixtures import load_json

TODAY = date.today()

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _seed_batter(session, *, mlb_id, recent_avg=0.270, ab_per_game=4):
    """Seed a batter with 5 recent final games at ~recent_avg."""
    batter = Player(
        mlb_id=mlb_id, full_name=f"B{mlb_id}", team="AAA", position="LF", bats="R",
    )
    session.add(batter)
    await session.flush()
    hits = round(ab_per_game * recent_avg)
    for i in range(1, 6):
        g = Game(
            mlb_game_id=mlb_id * 10 + i, date=TODAY - timedelta(days=i),
            home_team="AAA", away_team="BBB", status="Final",
            home_score=3, away_score=2,
        )
        session.add(g)
        await session.flush()
        session.add(
            BattingStats(
                player_id=batter.id, game_id=g.id, at_bats=ab_per_game,
                hits=hits, home_runs=0, rbis=1, batting_avg=recent_avg,
            )
        )
    return batter.id


async def _seed_pitcher(session, *, mlb_id):
    pitcher = Player(
        mlb_id=mlb_id, full_name=f"P{mlb_id}", team="BBB", position="P", throws="R",
    )
    session.add(pitcher)
    await session.flush()
    return pitcher.id


def _add_h2h(session, batter_id, pitcher_id, *, at_bats, avg):
    session.add(
        MatchupHistory(
            batter_id=batter_id, pitcher_id=pitcher_id, at_bats=at_bats,
            hits=round(at_bats * avg), home_runs=0, strikeouts=0, batting_avg=avg,
        )
    )


# ── 1 & 2. mlb_client.get_batter_vs_pitcher parsing ──────────────────────────


class TestGetBatterVsPitcher:
    async def test_parses_vsplayertotal(self) -> None:
        fixture = load_json("mock_h2h_response.json")
        with respx.mock(assert_all_called=False) as router:
            router.get("https://statsapi.mlb.com/api/v1/people/545361/stats").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            async with MLBClient() as mlb:
                h2h = await mlb.get_batter_vs_pitcher(545361, 543037)

        assert h2h is not None
        assert h2h["at_bats"] == 25
        assert h2h["hits"] == 8
        assert h2h["home_runs"] == 2
        assert h2h["strikeouts"] == 5
        assert h2h["batting_avg"] == pytest.approx(0.320, abs=0.001)
        assert h2h["on_base_pct"] == pytest.approx(0.393, abs=0.001)
        assert h2h["slugging_pct"] == pytest.approx(0.560, abs=0.001)

    async def test_never_faced_returns_none(self) -> None:
        fixture = load_json("mock_h2h_empty.json")
        with respx.mock(assert_all_called=False) as router:
            router.get("https://statsapi.mlb.com/api/v1/people/545361/stats").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            async with MLBClient() as mlb:
                h2h = await mlb.get_batter_vs_pitcher(545361, 999999)
        assert h2h is None

    async def test_404_returns_none(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            router.get("https://statsapi.mlb.com/api/v1/people/1/stats").mock(
                return_value=httpx.Response(404, json={"message": "not found"})
            )
            async with MLBClient() as mlb:
                assert await mlb.get_batter_vs_pitcher(1, 543037) is None


# ── 3. MatchupHistory upsert: create then update in place ─────────────────────


class TestMatchupUpsert:
    async def test_create_then_update_in_place(self) -> None:
        async with TestSessionLocal() as session:
            bid = await _seed_batter(session, mlb_id=61001)
            pid = await _seed_pitcher(session, mlb_id=61002)
            await session.commit()

            mlb = MagicMock()
            # First fetch: 12 AB, .250
            mlb.get_batter_vs_pitcher = AsyncMock(
                return_value={
                    "at_bats": 12, "hits": 3, "home_runs": 0, "strikeouts": 4,
                    "batting_avg": 0.250, "on_base_pct": 0.300, "slugging_pct": 0.400,
                }
            )
            outcome = await update_matchup_history(session, mlb, 61001, 61002)
            await session.commit()
            assert outcome == "stored"

            count = (
                await session.execute(select(func.count()).select_from(MatchupHistory))
            ).scalar_one()
            row = (await session.execute(select(MatchupHistory))).scalar_one()
            assert count == 1
            assert row.at_bats == 12 and row.batting_avg == 0.250
            first_updated = row.last_updated  # capture scalar before the next txn
            await session.rollback()  # close the read transaction

            # Second fetch for the SAME pair with new numbers → update in place
            mlb.get_batter_vs_pitcher = AsyncMock(
                return_value={
                    "at_bats": 18, "hits": 6, "home_runs": 1, "strikeouts": 5,
                    "batting_avg": 0.333, "on_base_pct": 0.380, "slugging_pct": 0.520,
                }
            )
            outcome2 = await update_matchup_history(session, mlb, 61001, 61002)
            await session.commit()
            assert outcome2 == "stored"

            count2 = (
                await session.execute(select(func.count()).select_from(MatchupHistory))
            ).scalar_one()
            row2 = (await session.execute(select(MatchupHistory))).scalar_one()
            assert count2 == 1  # updated, not duplicated
            assert row2.at_bats == 18
            assert row2.batting_avg == pytest.approx(0.333, abs=0.001)
            assert row2.last_updated >= first_updated

    async def test_no_history_stores_nothing(self) -> None:
        async with TestSessionLocal() as session:
            await _seed_batter(session, mlb_id=61011)
            await _seed_pitcher(session, mlb_id=61012)
            await session.commit()

            mlb = MagicMock()
            mlb.get_batter_vs_pitcher = AsyncMock(return_value=None)  # never faced
            async with session.begin():
                outcome = await update_matchup_history(session, mlb, 61011, 61012)
            assert outcome == "no_history"
            count = (
                await session.execute(select(func.count()).select_from(MatchupHistory))
            ).scalar_one()
            assert count == 0


# ── 4. Dynamic H2H weight by sample size ──────────────────────────────────────


class TestH2HWeightScaling:
    @pytest.mark.parametrize(
        "at_bats,expected_weight",
        [(20, 0.15), (8, 0.075), (3, 0.0)],
    )
    async def test_weight_by_sample_size(
        self, at_bats: int, expected_weight: float
    ) -> None:
        async with TestSessionLocal() as session:
            bid = await _seed_batter(session, mlb_id=62000 + at_bats)
            pid = await _seed_pitcher(session, mlb_id=62500 + at_bats)
            _add_h2h(session, bid, pid, at_bats=at_bats, avg=0.300)
            await session.commit()
            result = await calculate_enhanced_hit_probability(
                session, player_id=bid, pitcher_id=pid
            )
        assert result["factors"]["h2h_weight_applied"] == expected_weight
        assert result["factors"]["h2h_at_bats"] == at_bats


# ── 5. "Owns the pitcher" lifts the projection ────────────────────────────────


class TestOwnsPitcherLiftsProbability:
    async def test_strong_h2h_beats_no_h2h(self) -> None:
        async with TestSessionLocal() as session:
            # Identical modest batters + pitchers; only difference is the H2H row.
            bid_owns = await _seed_batter(session, mlb_id=63001, recent_avg=0.270)
            pid_owns = await _seed_pitcher(session, mlb_id=63002)
            bid_none = await _seed_batter(session, mlb_id=63011, recent_avg=0.270)
            pid_none = await _seed_pitcher(session, mlb_id=63012)
            _add_h2h(session, bid_owns, pid_owns, at_bats=24, avg=0.417)  # 10-for-24
            await session.commit()

            owns = await calculate_enhanced_hit_probability(
                session, player_id=bid_owns, pitcher_id=pid_owns
            )
            baseline = await calculate_enhanced_hit_probability(
                session, player_id=bid_none, pitcher_id=pid_none
            )
        assert owns["factors"]["h2h_weight_applied"] == 0.15
        assert owns["probability"] > baseline["probability"]
