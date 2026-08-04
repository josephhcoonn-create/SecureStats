"""
Task 8.5 — pick accuracy tracking tests.

Covers:
- _snapshot_picks: idempotent UPSERT into PickHistory
- grade_pending_picks: hit / no_hit / leaves-pending semantics
- get_model_accuracy: headline accuracy + confidence-tier breakdown +
  empty-window safety
- GET /picks/accuracy endpoint (analyst RBAC)
- get_daily_picks snapshots automatically on call
- ETL grading hook still runs cleanly when no pending rows exist
"""
from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.auth.jwt_handler import create_access_token
from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.pick_history import PickHistory
from app.models.player import Player
from app.services.analytics import (
    _snapshot_picks,
    get_daily_picks,
    get_model_accuracy,
    grade_pending_picks,
)
from tests.conftest import TestSessionLocal

TODAY = date.today()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def graded_history() -> dict:
    """
    Seed a mix of graded + pending picks across the last few days so
    accuracy math has something to chew on.

    Layout:
      day -2  → 3 final games, 4 picks: 3 hit, 1 no_hit  (75%)
      day -1  → 2 final games, 2 picks: 1 hit, 1 no_hit  (50%)
      today   → 1 scheduled game, 1 pending pick
    """
    async with TestSessionLocal() as session:
        # Three batters
        batters = [
            Player(mlb_id=110000 + i, full_name=f"Batter {i}", team="Phillies", position="LF")
            for i in range(5)
        ]
        session.add_all(batters)
        await session.flush()

        # Three final games (day -2), two final (day -1), one scheduled today
        games_day_minus_2 = [
            Game(
                mlb_game_id=210000 + i,
                date=TODAY - timedelta(days=2),
                home_team="Phillies",
                away_team="Mets",
                status="Final",
                home_score=5,
                away_score=3,
            )
            for i in range(3)
        ]
        games_day_minus_1 = [
            Game(
                mlb_game_id=220000 + i,
                date=TODAY - timedelta(days=1),
                home_team="Phillies",
                away_team="Mets",
                status="Final",
                home_score=4,
                away_score=2,
            )
            for i in range(2)
        ]
        game_today = Game(
            mlb_game_id=230000,
            date=TODAY,
            home_team="Phillies",
            away_team="Mets",
            status="Scheduled",
        )
        session.add_all(games_day_minus_2 + games_day_minus_1 + [game_today])
        await session.flush()

        # Picks + graded results
        # day -2: 4 picks across 3 games (2 in first game), 3 of 4 hit
        session.add_all(
            [
                # high-confidence: 2 picks, both hit
                PickHistory(
                    player_id=batters[0].id, game_id=games_day_minus_2[0].id,
                    predicted_probability=0.92, confidence=85,
                    actual_result="hit",
                ),
                PickHistory(
                    player_id=batters[1].id, game_id=games_day_minus_2[0].id,
                    predicted_probability=0.88, confidence=80,
                    actual_result="hit",
                ),
                # medium-confidence: 2 picks, 1 hit + 1 no_hit
                PickHistory(
                    player_id=batters[2].id, game_id=games_day_minus_2[1].id,
                    predicted_probability=0.82, confidence=60,
                    actual_result="hit",
                ),
                PickHistory(
                    player_id=batters[3].id, game_id=games_day_minus_2[2].id,
                    predicted_probability=0.81, confidence=65,
                    actual_result="no_hit",
                ),
                # day -1: 1 hit + 1 no_hit, both low confidence
                PickHistory(
                    player_id=batters[0].id, game_id=games_day_minus_1[0].id,
                    predicted_probability=0.85, confidence=40,
                    actual_result="hit",
                ),
                PickHistory(
                    player_id=batters[1].id, game_id=games_day_minus_1[1].id,
                    predicted_probability=0.86, confidence=45,
                    actual_result="no_hit",
                ),
                # today: 1 pending
                PickHistory(
                    player_id=batters[2].id, game_id=game_today.id,
                    predicted_probability=0.83, confidence=55,
                    actual_result="pending",
                ),
            ]
        )
        await session.commit()
        return {
            "batter_ids": [b.id for b in batters],
            "game_today_id": game_today.id,
            "games_day_minus_2_ids": [g.id for g in games_day_minus_2],
            "games_day_minus_1_ids": [g.id for g in games_day_minus_1],
        }


# ── _snapshot_picks ──────────────────────────────────────────────────────────


class TestSnapshotPicks:
    async def test_inserts_then_no_dupes(self) -> None:
        async with TestSessionLocal() as session:
            session.add_all(
                [
                    Player(mlb_id=130001, full_name="P1", team="X", position="LF"),
                    Game(mlb_game_id=240001, date=TODAY, home_team="X", away_team="Y", status="Scheduled"),
                ]
            )
            await session.flush()
            pid = (await session.execute(select(Player).where(Player.mlb_id == 130001))).scalar_one().id
            gid = (await session.execute(select(Game).where(Game.mlb_game_id == 240001))).scalar_one().id
            await session.commit()

        picks = [{
            "player_id": pid, "game_id": gid,
            "probability": 0.91, "confidence": 70,
            "factors": {"recent_avg": 0.330, "league_avg": 0.250},
        }]

        async with TestSessionLocal() as session:
            inserted = await _snapshot_picks(session, picks)
        assert inserted == 1

        async with TestSessionLocal() as session:
            inserted2 = await _snapshot_picks(session, picks)
        # Second call hits the unique constraint → 0 new rows
        assert inserted2 == 0

        async with TestSessionLocal() as session:
            rows = (await session.execute(select(PickHistory).where(PickHistory.player_id == pid))).scalars().all()
            assert len(rows) == 1
            assert rows[0].actual_result == "pending"
            assert rows[0].factors_snapshot == {"recent_avg": 0.330, "league_avg": 0.250}


# ── grade_pending_picks ──────────────────────────────────────────────────────


class TestGradePendingPicks:
    async def test_marks_hit_and_no_hit_from_batting_stats(self) -> None:
        async with TestSessionLocal() as session:
            batter_a = Player(mlb_id=140001, full_name="Hot Bat",  team="X", position="LF")
            batter_b = Player(mlb_id=140002, full_name="Cold Bat", team="X", position="2B")
            game = Game(
                mlb_game_id=250001,
                date=TODAY,
                home_team="X", away_team="Y",
                status="Final",
                home_score=5, away_score=2,
            )
            session.add_all([batter_a, batter_b, game])
            await session.flush()

            # Hot Bat got 2 hits, Cold Bat got 0
            session.add_all(
                [
                    BattingStats(player_id=batter_a.id, game_id=game.id, at_bats=4, hits=2, home_runs=0, rbis=1),
                    BattingStats(player_id=batter_b.id, game_id=game.id, at_bats=4, hits=0, home_runs=0, rbis=0),
                    PickHistory(player_id=batter_a.id, game_id=game.id,
                                predicted_probability=0.92, confidence=80,
                                actual_result="pending"),
                    PickHistory(player_id=batter_b.id, game_id=game.id,
                                predicted_probability=0.87, confidence=60,
                                actual_result="pending"),
                ]
            )
            await session.commit()

            graded = await grade_pending_picks(session, TODAY)
            assert graded == 2
            await session.commit()

            updated = (await session.execute(select(PickHistory).where(PickHistory.game_id == game.id))).scalars().all()
            results = {u.player_id: u.actual_result for u in updated}
            assert results[batter_a.id] == "hit"
            assert results[batter_b.id] == "no_hit"

    async def test_player_who_did_not_play_stays_pending(self) -> None:
        async with TestSessionLocal() as session:
            ghost = Player(mlb_id=140003, full_name="Bench Warmer", team="X", position="C")
            game = Game(
                mlb_game_id=250002,
                date=TODAY,
                home_team="X", away_team="Y",
                status="Final",
            )
            session.add_all([ghost, game])
            await session.flush()
            # No BattingStats for ghost — they were a healthy scratch
            session.add(
                PickHistory(player_id=ghost.id, game_id=game.id,
                            predicted_probability=0.85, confidence=50,
                            actual_result="pending")
            )
            await session.commit()

            graded = await grade_pending_picks(session, TODAY)
            assert graded == 0  # nothing to grade

            row = (await session.execute(select(PickHistory).where(PickHistory.player_id == ghost.id))).scalar_one()
            assert row.actual_result == "pending"

    async def test_no_final_games_returns_zero(self) -> None:
        async with TestSessionLocal() as session:
            graded = await grade_pending_picks(session, TODAY)
        assert graded == 0


# ── get_model_accuracy ───────────────────────────────────────────────────────


class TestGetModelAccuracy:
    async def test_empty_window(self) -> None:
        async with TestSessionLocal() as session:
            result = await get_model_accuracy(session, days=30)
        assert result["total_picks"] == 0
        assert result["accuracy_pct"] is None
        assert result["by_confidence"] == []

    async def test_headline_and_confidence_breakdown(
        self, graded_history: dict  # noqa: ARG002
    ) -> None:
        async with TestSessionLocal() as session:
            result = await get_model_accuracy(session, days=30)

        # 6 graded (4 hits + 2 no_hits) + 1 pending
        assert result["total_picks"] == 6
        assert result["pending_picks"] == 1
        assert result["correct_predictions"] == 4
        assert result["accuracy_pct"] == round(4 * 100.0 / 6, 1)

        # Average probabilities of correct vs incorrect groups
        # correct probs: 0.92 + 0.88 + 0.82 + 0.85 = 3.47 / 4 = 0.8675
        # incorrect probs: 0.81 + 0.86 = 1.67 / 2 = 0.835
        # (banker's rounding may land on 0.867 or 0.868 — tolerate both)
        assert result["avg_prob_correct"] == pytest.approx(0.8675, abs=0.001)
        assert result["avg_prob_incorrect"] == pytest.approx(0.835, abs=0.001)

        by_tier = {row["tier"]: row for row in result["by_confidence"]}

        # High (≥80): 2 picks (confidences 85 + 80), both hits → 100%
        assert by_tier["high"]["total"] == 2
        assert by_tier["high"]["correct"] == 2
        assert by_tier["high"]["accuracy_pct"] == 100.0

        # Medium (50–79): 2 picks (confidences 60 hit + 65 no_hit) → 50%
        assert by_tier["medium"]["total"] == 2
        assert by_tier["medium"]["correct"] == 1
        assert by_tier["medium"]["accuracy_pct"] == 50.0

        # Low (<50): 2 picks (confidences 40 hit + 45 no_hit) → 50%
        assert by_tier["low"]["total"] == 2
        assert by_tier["low"]["correct"] == 1
        assert by_tier["low"]["accuracy_pct"] == 50.0


# ── /picks/accuracy endpoint ─────────────────────────────────────────────────


class TestPicksAccuracyEndpoint:
    async def test_viewer_blocked(
        self, client: AsyncClient, viewer_user: dict
    ) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": viewer_user["username"], "password": "Testpass123"},
        )
        token = login.json()["access_token"]
        resp = await client.get(
            "/api/v1/picks/accuracy",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    async def test_analyst_envelope(
        self,
        client: AsyncClient,
        analyst_user: dict,  # noqa: ARG002
        graded_history: dict,  # noqa: ARG002
    ) -> None:
        token = create_access_token({"sub": "1", "role": "analyst"})
        resp = await client.get(
            "/api/v1/picks/accuracy?days=30",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["days"] == 30
        assert body["total_picks"] == 6
        assert body["correct_predictions"] == 4
        assert body["pending_picks"] == 1
        assert len(body["by_confidence"]) == 3


# ── get_daily_picks auto-snapshots ───────────────────────────────────────────


class TestDailyPicksSnapshots:
    async def test_get_daily_picks_creates_pick_history_rows(self) -> None:
        """Smoke test that calling get_daily_picks today writes
        PickHistory entries (idempotently, on repeat calls)."""
        async with TestSessionLocal() as session:
            # Minimal world: 1 game today, 1 hot batter who shows up
            # in 3+ of last 5 final games so _likely_starters picks
            # them up, then has very high recent avg so probability
            # clears default threshold.
            batter = Player(mlb_id=150001, full_name="Auto-Snap", team="X", position="LF")
            session.add(batter)

            game_today = Game(
                mlb_game_id=260001,
                date=TODAY,
                home_team="X", away_team="Y",
                status="Scheduled",
            )
            session.add(game_today)
            await session.flush()

            past_games = []
            for i in range(1, 6):
                g = Game(
                    mlb_game_id=260100 + i,
                    date=TODAY - timedelta(days=i),
                    home_team="X", away_team="Y",
                    status="Final",
                    home_score=4, away_score=3,
                )
                session.add(g)
                past_games.append(g)
            await session.flush()

            for g in past_games:
                session.add(
                    BattingStats(
                        player_id=batter.id, game_id=g.id,
                        at_bats=5, hits=4, home_runs=0, rbis=2,
                        batting_avg=0.800,
                    )
                )
            await session.commit()

            picks_result = await get_daily_picks(
                session, min_probability=0.50, min_confidence=0, min_season_ab=0
            )
            assert picks_result["picks"], "expected at least one daily pick"

            rows = (
                await session.execute(
                    select(PickHistory).where(PickHistory.game_id == game_today.id)
                )
            ).scalars().all()
            assert len(rows) == len(picks_result["picks"])
            assert all(r.actual_result == "pending" for r in rows)

            # Second call — no new rows
            await get_daily_picks(session, min_probability=0.50, min_confidence=0, min_season_ab=0)
            rows2 = (
                await session.execute(
                    select(PickHistory).where(PickHistory.game_id == game_today.id)
                )
            ).scalars().all()
            assert len(rows2) == len(rows)
