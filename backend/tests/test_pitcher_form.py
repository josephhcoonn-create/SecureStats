"""
Task 6 — recent-pitcher-form coverage.

  1. get_pitcher_recent_games parses a mocked game log
  2. Recent ERA aggregated across the last 3 starts
  3. trending classification: struggling / steady / locked_in
  4. Fallback when the pitcher has only 1-2 logged starts
  5. A struggling pitcher boosts hit probability vs a steady one
"""
from datetime import date, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.pitcher_game_log import PitcherGameLog
from app.models.pitcher_stats import PitcherStats
from app.models.player import Player
from app.services.analytics import calculate_enhanced_hit_probability
from app.services.mlb_client import MLBClient
from tests.conftest import TestSessionLocal
from tests.fixtures import load_json

TODAY = date.today()

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _seed_batter(session, *, mlb_id, recent_avg=0.270):
    batter = Player(
        mlb_id=mlb_id, full_name=f"B{mlb_id}", team="AAA", position="LF", bats="R",
    )
    session.add(batter)
    await session.flush()
    for i in range(1, 6):
        g = Game(
            mlb_game_id=mlb_id * 10 + i, date=TODAY - timedelta(days=i),
            home_team="AAA", away_team="BBB", status="Final",
        )
        session.add(g)
        await session.flush()
        session.add(
            BattingStats(
                player_id=batter.id, game_id=g.id, at_bats=4,
                hits=round(4 * recent_avg), home_runs=0, rbis=0, batting_avg=recent_avg,
            )
        )
    return batter.id


async def _seed_pitcher(session, *, mlb_id, season_era=3.50, season_whip=1.15):
    pitcher = Player(
        mlb_id=mlb_id, full_name=f"P{mlb_id}", team="BBB", position="P", throws="R",
    )
    session.add(pitcher)
    await session.flush()
    session.add(
        PitcherStats(
            player_id=pitcher.id, season=TODAY.year, games=20,
            innings_pitched=120.0, hits_allowed=110, earned_runs=47,
            walks_allowed=35, strikeouts=120, era=season_era, whip=season_whip,
        )
    )
    return pitcher.id


def _add_starts(session, pitcher_id, er_per_game, *, ip=6.0):
    """Seed len(er_per_game) game-log rows, each `ip` innings. With ip=6 across
    3 games (18 IP total), recent ERA = ΣER / 18 × 9 = ΣER (nice round math)."""
    for i, er in enumerate(er_per_game, start=1):
        session.add(
            PitcherGameLog(
                pitcher_id=pitcher_id, game_id=None,
                game_date=TODAY - timedelta(days=i * 5),
                opponent="BBB", innings_pitched=ip, hits_allowed=5,
                earned_runs=er, walks=1, strikeouts=6, era=None, whip=None,
            )
        )


# ── 1. Parsing ────────────────────────────────────────────────────────────────


class TestGetPitcherRecentGames:
    async def test_parses_game_log(self) -> None:
        fixture = load_json("mock_pitcher_game_log.json")
        with respx.mock(assert_all_called=False) as router:
            router.get("https://statsapi.mlb.com/api/v1/people/543037/stats").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            async with MLBClient() as mlb:
                form = await mlb.get_pitcher_recent_games(543037, num_games=3)

        assert form is not None
        assert form["num_games"] == 3
        # Most-recent-first: 2026-07-05, 06-30, 06-24
        assert [g["date"] for g in form["games"]] == [
            "2026-07-05", "2026-06-30", "2026-06-24",
        ]
        assert form["games"][0]["opponent"] == "Tampa Bay Rays"
        # '7.0' parses to 7.0 innings on the 06-24 start
        assert form["games"][2]["innings_pitched"] == pytest.approx(7.0, abs=0.01)

    # ── 2. Recent ERA aggregation ──
    async def test_recent_era_across_three_starts(self) -> None:
        fixture = load_json("mock_pitcher_game_log.json")
        with respx.mock(assert_all_called=False) as router:
            router.get("https://statsapi.mlb.com/api/v1/people/543037/stats").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            async with MLBClient() as mlb:
                form = await mlb.get_pitcher_recent_games(543037, num_games=3)
        # last 3: (2+3+1) ER over (6.0+6.0+7.0)=19.0 IP → 6/19*9 = 2.84
        assert form["recent_era"] == pytest.approx(2.84, abs=0.02)

    async def test_struggling_fixture_recent_era_over_six(self) -> None:
        fixture = load_json("mock_pitcher_struggling.json")
        with respx.mock(assert_all_called=False) as router:
            router.get("https://statsapi.mlb.com/api/v1/people/543037/stats").mock(
                return_value=httpx.Response(200, json=fixture)
            )
            async with MLBClient() as mlb:
                form = await mlb.get_pitcher_recent_games(543037, num_games=3)
        assert form["num_games"] == 3
        assert form["recent_era"] > 6.00  # last 3 starts are a meltdown


# ── 3. Trending classification through the model ──────────────────────────────


class TestPitcherTrending:
    @pytest.mark.parametrize(
        "er_per_game,expected",
        [
            ([6, 5, 5], "struggling"),   # 16/18*9 = 8.0 vs season 3.50 → >4.55
            ([2, 3, 2], "steady"),        # 7/18*9  = 3.50 vs season 3.50
            ([1, 2, 1], "locked_in"),     # 4/18*9  = 2.00 vs season 3.50 → <2.45
        ],
    )
    async def test_trend_bands(self, er_per_game, expected) -> None:
        async with TestSessionLocal() as session:
            base = sum(er_per_game) * 1000 + 100
            bid = await _seed_batter(session, mlb_id=base + 1)
            pid = await _seed_pitcher(session, mlb_id=base + 2, season_era=3.50)
            _add_starts(session, pid, er_per_game)
            await session.commit()
            result = await calculate_enhanced_hit_probability(
                session, player_id=bid, pitcher_id=pid
            )
        assert result["factors"]["pitcher_trending"] == expected
        assert result["factors"]["pitcher_recent_era"] is not None
        assert result["factors"]["pitcher_season_era"] == pytest.approx(3.50, abs=0.01)


# ── 4. Fallback with fewer than 3 starts ──────────────────────────────────────


class TestInsufficientStarts:
    async def test_only_two_starts_no_recent_form(self) -> None:
        async with TestSessionLocal() as session:
            bid = await _seed_batter(session, mlb_id=64101)
            pid = await _seed_pitcher(session, mlb_id=64102, season_era=3.50)
            _add_starts(session, pid, [3, 4])  # only 2 logged starts
            await session.commit()
            result = await calculate_enhanced_hit_probability(
                session, player_id=bid, pitcher_id=pid
            )
        f = result["factors"]
        assert f["pitcher_trending"] is None
        assert f["pitcher_recent_era"] is None  # not surfaced with < 3 starts
        # Still produces a valid probability (falls back to season ERA term)
        assert 0.05 <= result["probability"] <= 0.95


# ── 5. Struggling pitcher boosts hit probability vs steady ─────────────────────


class TestStrugglingBoostsProbability:
    async def test_struggling_over_steady(self) -> None:
        async with TestSessionLocal() as session:
            # Same batter profile, same SEASON ERA for both pitchers; only the
            # recent form differs.
            bid_a = await _seed_batter(session, mlb_id=64201)
            pid_struggling = await _seed_pitcher(session, mlb_id=64202, season_era=3.50)
            _add_starts(session, pid_struggling, [6, 6, 6])  # recent 9.0 → struggling

            bid_b = await _seed_batter(session, mlb_id=64211)
            pid_steady = await _seed_pitcher(session, mlb_id=64212, season_era=3.50)
            _add_starts(session, pid_steady, [2, 3, 2])  # recent 3.5 → steady
            await session.commit()

            vs_struggling = await calculate_enhanced_hit_probability(
                session, player_id=bid_a, pitcher_id=pid_struggling
            )
            vs_steady = await calculate_enhanced_hit_probability(
                session, player_id=bid_b, pitcher_id=pid_steady
            )
        assert vs_struggling["factors"]["pitcher_trending"] == "struggling"
        assert vs_steady["factors"]["pitcher_trending"] == "steady"
        assert vs_struggling["probability"] > vs_steady["probability"]
