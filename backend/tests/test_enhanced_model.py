"""
Task 6 — full enhanced-model integration across the new + existing factors.

  1. Effective weights sum to 1.0 in every H2H regime (full / half / none)
  2. No H2H → weight redistributes to batter factors, still sums to 1.0
  3. Limited H2H (8 AB) → half weight applied
  4. Confidence boosts from H2H (+15) and recent pitcher form (+10)
  5. get_daily_picks() still applies the probability threshold correctly
  6. Edge: a 4-for-4 (1.000) H2H over only 4 AB is ignored, not inflated

Note: the brief references an "80% threshold", but the shipped
DAILY_PICK_THRESHOLD is a pick-count knob (currently 0.74). These tests
assert the *filtering behavior* against the real constant and explicit
thresholds rather than any hard-coded value.
"""
from datetime import date, timedelta

import pytest

from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.matchup_history import MatchupHistory
from app.models.pitcher_game_log import PitcherGameLog
from app.models.pitcher_stats import PitcherStats
from app.models.player import Player
from app.services.analytics import (
    DAILY_PICK_THRESHOLD,
    _W_BATTER_TOTAL,
    _W_HANDEDNESS,
    _W_H2H,
    _W_LEAGUE,
    _W_PITCHER_RECENT,
    _W_PITCHER_SEASON,
    calculate_enhanced_hit_probability,
    get_daily_picks,
)
from tests.conftest import TestSessionLocal

TODAY = date.today()

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _seed_batter(session, *, mlb_id, team="AAA", recent_avg=0.280, n_games=5):
    batter = Player(
        mlb_id=mlb_id, full_name=f"B{mlb_id}", team=team, position="LF", bats="R",
    )
    session.add(batter)
    await session.flush()
    for i in range(1, n_games + 1):
        g = Game(
            mlb_game_id=mlb_id * 100 + i, date=TODAY - timedelta(days=i),
            home_team=team, away_team="BBB", status="Final",
            home_score=3, away_score=2,
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


async def _seed_pitcher(session, *, mlb_id, season_era=3.50, ip=120.0):
    pitcher = Player(
        mlb_id=mlb_id, full_name=f"P{mlb_id}", team="BBB", position="P", throws="R",
    )
    session.add(pitcher)
    await session.flush()
    session.add(
        PitcherStats(
            player_id=pitcher.id, season=TODAY.year, games=20, innings_pitched=ip,
            hits_allowed=110, earned_runs=47, walks_allowed=35, strikeouts=120,
            era=season_era, whip=1.15,
        )
    )
    return pitcher.id


def _add_h2h(session, batter_id, pitcher_id, *, at_bats, avg):
    session.add(
        MatchupHistory(
            batter_id=batter_id, pitcher_id=pitcher_id, at_bats=at_bats,
            hits=round(at_bats * avg), home_runs=0, strikeouts=0, batting_avg=avg,
        )
    )


def _add_starts(session, pitcher_id, er_per_game, *, ip=6.0):
    for i, er in enumerate(er_per_game, start=1):
        session.add(
            PitcherGameLog(
                pitcher_id=pitcher_id, game_id=None,
                game_date=TODAY - timedelta(days=i * 5), opponent="BBB",
                innings_pitched=ip, hits_allowed=5, earned_runs=er,
                walks=1, strikeouts=6, era=None, whip=None,
            )
        )


# ── 1 & 2. Effective weights sum to 1.0 in every regime ───────────────────────


class TestWeightsSumToOne:
    @staticmethod
    def _effective_sum(h2h_weight: float) -> float:
        # Mirrors calculate_enhanced_hit_probability's redistribution math.
        redistribute = _W_H2H - h2h_weight
        scale = 1.0 + redistribute / _W_BATTER_TOTAL
        batter = _W_BATTER_TOTAL * scale
        return (
            batter + h2h_weight + _W_PITCHER_RECENT + _W_PITCHER_SEASON
            + _W_HANDEDNESS + _W_LEAGUE
        )

    @pytest.mark.parametrize("h2h_weight", [0.15, 0.075, 0.0])
    def test_effective_weights_sum_to_one(self, h2h_weight: float) -> None:
        assert self._effective_sum(h2h_weight) == pytest.approx(1.0, abs=1e-9)

    async def test_full_model_all_factors_present(self) -> None:
        async with TestSessionLocal() as session:
            bid = await _seed_batter(session, mlb_id=81001)
            pid = await _seed_pitcher(session, mlb_id=81002, season_era=3.50)
            _add_h2h(session, bid, pid, at_bats=25, avg=0.320)
            _add_starts(session, pid, [2, 3, 2])
            # a game so the home/away factor also engages
            g = Game(
                mlb_game_id=810_999, date=TODAY, home_team="AAA", away_team="BBB",
                status="Scheduled",
            )
            session.add(g)
            await session.flush()
            gid = g.id
            await session.commit()
            result = await calculate_enhanced_hit_probability(
                session, player_id=bid, game_id=gid, pitcher_id=pid
            )
        f = result["factors"]
        # Every new + existing factor surfaced
        assert f["h2h_weight_applied"] == 0.15
        assert f["h2h_avg"] == pytest.approx(0.320, abs=0.001)
        assert f["pitcher_trending"] in {"struggling", "steady", "locked_in"}
        assert f["recent_avg"] is not None
        assert 0.05 <= result["probability"] <= 0.95


# ── 3. Limited H2H → half weight ──────────────────────────────────────────────


class TestLimitedH2H:
    async def test_eight_ab_half_weight(self) -> None:
        async with TestSessionLocal() as session:
            bid = await _seed_batter(session, mlb_id=82001)
            pid = await _seed_pitcher(session, mlb_id=82002)
            _add_h2h(session, bid, pid, at_bats=8, avg=0.375)
            await session.commit()
            result = await calculate_enhanced_hit_probability(
                session, player_id=bid, pitcher_id=pid
            )
        assert result["factors"]["h2h_weight_applied"] == 0.075


# ── 4. Confidence boosts stack ────────────────────────────────────────────────


class TestConfidenceBoosting:
    async def test_h2h_and_recent_form_boosts(self) -> None:
        async with TestSessionLocal() as session:
            # Small season sample (20 AB → base 30) + pitcher IP 40 (no IP boost)
            # isolates the two NEW boosts: +15 (H2H ≥15 AB) and +10 (3 starts).
            bid = await _seed_batter(session, mlb_id=83001, n_games=5)
            pid = await _seed_pitcher(session, mlb_id=83002, ip=40.0)
            _add_h2h(session, bid, pid, at_bats=20, avg=0.300)
            _add_starts(session, pid, [3, 3, 3])
            await session.commit()
            result = await calculate_enhanced_hit_probability(
                session, player_id=bid, pitcher_id=pid
            )
        # base 30 (20 AB) + 0 (IP<50) + 15 (H2H) + 10 (3 starts) = 55
        assert result["confidence"] == 55

    async def test_no_matchup_data_no_boost(self) -> None:
        async with TestSessionLocal() as session:
            bid = await _seed_batter(session, mlb_id=83011)
            pid = await _seed_pitcher(session, mlb_id=83012, ip=40.0)
            await session.commit()
            result = await calculate_enhanced_hit_probability(
                session, player_id=bid, pitcher_id=pid
            )
        # base 30 only — no H2H, no recent starts logged
        assert result["confidence"] == 30


# ── 5. get_daily_picks threshold filtering ────────────────────────────────────


class TestDailyPicksThreshold:
    async def test_threshold_filters_cold_bat(self) -> None:
        async with TestSessionLocal() as session:
            await _seed_batter(session, mlb_id=84001, team="Hots", recent_avg=0.450)
            await _seed_batter(session, mlb_id=84002, team="Colds", recent_avg=0.050)
            # A game today between the two teams so both are surfaced.
            session.add(
                Game(
                    mlb_game_id=840_999, date=TODAY, home_team="Hots",
                    away_team="Colds", status="Scheduled",
                )
            )
            await session.commit()

            # min_season_ab=0 disables the playing-time gate — the seed helper
            # creates only 5 games, below the production floor.
            picks = await get_daily_picks(
                session, min_probability=DAILY_PICK_THRESHOLD, min_confidence=0,
                min_season_ab=0,
            )
        names = {p["player_name"] for p in picks["picks"]}
        assert "B84001" in names        # hot bat clears the threshold
        assert "B84002" not in names    # cold bat filtered out
        # Every surfaced pick is at or above the threshold
        assert all(p["probability"] >= DAILY_PICK_THRESHOLD for p in picks["picks"])

    async def test_playing_time_gate_excludes_small_sample(self) -> None:
        """A batter below the season-AB floor is gated out by default, but
        surfaces when min_season_ab=0."""
        async with TestSessionLocal() as session:
            # 5 games × 4 AB = 20 season ABs — well under the 150 floor.
            await _seed_batter(session, mlb_id=86001, team="Gaters", recent_avg=0.450)
            session.add(
                Game(
                    mlb_game_id=860_999, date=TODAY, home_team="Gaters",
                    away_team="Foes", status="Scheduled",
                )
            )
            await session.commit()

            gated = await get_daily_picks(
                session, min_probability=0.50, min_confidence=0
            )
            ungated = await get_daily_picks(
                session, min_probability=0.50, min_confidence=0, min_season_ab=0
            )
        assert "B86001" not in {p["player_name"] for p in gated["picks"]}
        assert "B86001" in {p["player_name"] for p in ungated["picks"]}

    async def test_raising_threshold_shrinks_set(self) -> None:
        async with TestSessionLocal() as session:
            await _seed_batter(session, mlb_id=84101, team="Hots", recent_avg=0.400)
            session.add(
                Game(
                    mlb_game_id=841_999, date=TODAY, home_team="Hots",
                    away_team="Colds", status="Scheduled",
                )
            )
            await session.commit()

            low = await get_daily_picks(session, min_probability=0.50, min_confidence=0)
            high = await get_daily_picks(session, min_probability=0.95, min_confidence=0)
        assert len(high["picks"]) <= len(low["picks"])


# ── 6. Edge: 4-for-4 (1.000) over 4 AB must be ignored ────────────────────────


class TestTinySampleIgnored:
    async def test_four_for_four_does_not_inflate(self) -> None:
        async with TestSessionLocal() as session:
            # Two identical batters/pitchers; one carries a 4-for-4 H2H row.
            bid_tiny = await _seed_batter(session, mlb_id=85001, recent_avg=0.280)
            pid_tiny = await _seed_pitcher(session, mlb_id=85002)
            bid_none = await _seed_batter(session, mlb_id=85011, recent_avg=0.280)
            pid_none = await _seed_pitcher(session, mlb_id=85012)
            _add_h2h(session, bid_tiny, pid_tiny, at_bats=4, avg=1.000)  # 4-for-4
            await session.commit()

            tiny = await calculate_enhanced_hit_probability(
                session, player_id=bid_tiny, pitcher_id=pid_tiny
            )
            baseline = await calculate_enhanced_hit_probability(
                session, player_id=bid_none, pitcher_id=pid_none
            )
        # Weight 0 → the gaudy 1.000 average contributes nothing…
        assert tiny["factors"]["h2h_weight_applied"] == 0.0
        # …and the projection is identical to the no-H2H baseline.
        assert tiny["probability"] == pytest.approx(baseline["probability"], abs=1e-9)
