"""
A/B harness — current model vs. {playing-time gate + dynamic AB-count}.

Re-scores every graded FIXED-model pick (Jul 28+) from its factors_snapshot
and compares two variants against the actual hit/no_hit outcomes:

  BASELINE : the shipped model — flat 4 ABs, no playing-time floor.
  VARIANT  : drop batters with < MIN_AB season ABs (gate), and convert
             per-AB → per-game using each batter's REAL season ABs/game
             instead of a flat 4.

For each we report the pick set at the 0.77 threshold: count, hit rate,
mean predicted probability, and the calibration gap (predicted − actual).

Caveat: season AB / ABs-per-game are taken from current totals (a small
look-ahead vs. as-of-pick-date), and this evaluates on picks the CURRENT
model surfaced (the gate can only remove, not add). It's a strong
directional read, not a full re-simulation.

Run: docker compose exec backend python scripts/ab_gate.py
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.models.batting_stats import BattingStats
from app.models.game import Game
from app.models.pick_history import PickHistory
from app.services.analytics import (
    _EXPECTED_AB_MAX,
    _EXPECTED_AB_MIN,
    _FALLBACK_ERA,
    _MIN_SEASON_AB,
    _PROB_MAX,
    _PROB_MIN,
    _W_BATTER_TOTAL,
    _W_CAREER,
    _W_H2H,
    _W_HANDEDNESS,
    _W_HOME_AWAY,
    _W_LEAGUE,
    _W_PITCHER_RECENT,
    _W_PITCHER_SEASON,
    _W_RECENT,
    _W_SEASON,
    DAILY_PICK_THRESHOLD,
    _pitcher_composite,
)

_FALLBACK_WHIP = 1.30


def _or(v, fb):
    return v if v is not None else fb


def _per_ab(f: dict) -> float:
    """Reconstruct the per-AB blend from a factors_snapshot (mirrors
    calculate_enhanced_hit_probability, pre per-game transform)."""
    league = f["league_avg"]
    era = _or(f.get("pitcher_era"), _FALLBACK_ERA)
    whip = _or(f.get("pitcher_whip"), _FALLBACK_WHIP)
    season_ref = max(era, 0.5)
    eff_recent = _or(f.get("recent_avg"), _or(f.get("season_avg"), _or(f.get("career_avg"), league)))
    eff_season = _or(f.get("season_avg"), _or(f.get("career_avg"), league))
    eff_career = _or(f.get("career_avg"), league)
    eff_home = _or(f.get("home_away_split"), _or(f.get("career_avg"), league))
    h2h_w = f.get("h2h_weight_applied", 0.0) or 0.0
    scale = 1.0 + (_W_H2H - h2h_w) / _W_BATTER_TOTAL
    season_term = _pitcher_composite(era, whip, league, handedness=0.0)
    recent_era = f.get("pitcher_recent_era")
    recent_term = (recent_era / season_ref) * league if recent_era is not None else (season_ref / _FALLBACK_ERA) * league
    handed = league + (f.get("handedness_matchup") or 0.0)
    per_ab = (
        _W_RECENT * scale * eff_recent
        + _W_SEASON * scale * eff_season
        + _W_CAREER * scale * eff_career
        + _W_HOME_AWAY * scale * eff_home
        + h2h_w * _or(f.get("h2h_avg"), league)
        + _W_PITCHER_RECENT * recent_term
        + _W_PITCHER_SEASON * season_term
        + _W_HANDEDNESS * handed
        + _W_LEAGUE * league
    )
    return max(0.001, min(0.999, per_ab))


def _to_game(per_ab: float, n_abs: float) -> float:
    return max(_PROB_MIN, min(_PROB_MAX, 1.0 - (1.0 - per_ab) ** n_abs))


def _report(name: str, rows: list[tuple[float, str]]) -> None:
    """rows = list of (predicted_prob, actual_result) for the pick set."""
    n = len(rows)
    if n == 0:
        print(f"  {name:<26} 0 picks")
        return
    hits = sum(1 for _, a in rows if a == "hit")
    mp = sum(p for p, _ in rows) / n
    ac = hits / n
    print(f"  {name:<26} {n:>3} picks   {hits:>3}/{n} = {ac*100:>4.1f}%   "
          f"pred {mp*100:>4.1f}%   gap {(mp-ac)*100:>+5.1f}")


async def main() -> None:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                select(
                    PickHistory.player_id,
                    PickHistory.actual_result,
                    PickHistory.predicted_probability,
                    PickHistory.factors_snapshot,
                )
                .join(Game, Game.id == PickHistory.game_id)
                .where(
                    Game.date >= date(2026, 7, 28),
                    PickHistory.actual_result.in_(("hit", "no_hit")),
                )
            )
        ).all()

        # Season AB + games played per candidate (current totals).
        pids = {r.player_id for r in rows}
        pt = (
            await s.execute(
                select(
                    BattingStats.player_id,
                    func.count().label("g"),
                    func.coalesce(func.sum(BattingStats.at_bats), 0).label("ab"),
                )
                .join(Game, BattingStats.game_id == Game.id)
                .where(
                    BattingStats.player_id.in_(pids),
                    Game.status.in_(("Final", "Game Over", "Completed Early")),
                    func.extract("year", Game.date) == 2026,
                )
                .group_by(BattingStats.player_id)
            )
        ).all()
        season = {r.player_id: (r.g or 0, r.ab or 0) for r in pt}

    base_set, var_set = [], []
    dropped_gate = dropped_ab = 0
    for r in rows:
        if not r.factors_snapshot:
            continue
        g, ab = season.get(r.player_id, (0, 0))
        per_ab = _per_ab(r.factors_snapshot)

        base_p = _to_game(per_ab, 4.0)
        if base_p >= DAILY_PICK_THRESHOLD:
            base_set.append((base_p, r.actual_result))

        # Variant: gate + dynamic ABs
        if ab < _MIN_SEASON_AB:
            dropped_gate += 1
            continue
        n_abs = max(_EXPECTED_AB_MIN, min(_EXPECTED_AB_MAX, ab / g)) if g else 4.0
        var_p = _to_game(per_ab, n_abs)
        if var_p >= DAILY_PICK_THRESHOLD:
            var_set.append((var_p, r.actual_result))
        else:
            dropped_ab += 1

    days = 6  # Jul 28,29,30,31, Aug 2 (Aug 1 skipped) — graded days
    print("=" * 72)
    print(f"  A/B — current model  vs  gate(≥{_MIN_SEASON_AB} AB) + dynamic ABs")
    print(f"  (graded fixed-model picks, Jul 28 – Aug 2)")
    print("=" * 72)
    _report("BASELINE (current)", base_set)
    _report("VARIANT (gate+dynamic)", var_set)
    print(f"\n  Removed by playing-time gate : {dropped_gate}")
    print(f"  Removed by dynamic-AB (< 0.77): {dropped_ab}")
    if base_set and var_set:
        b_acc = sum(1 for _, a in base_set if a == "hit") / len(base_set)
        v_acc = sum(1 for _, a in var_set if a == "hit") / len(var_set)
        b_gap = sum(p for p, _ in base_set) / len(base_set) - b_acc
        v_gap = sum(p for p, _ in var_set) / len(var_set) - v_acc
        print(f"\n  Accuracy:   {b_acc*100:.1f}%  →  {v_acc*100:.1f}%   ({(v_acc-b_acc)*100:+.1f} pts)")
        print(f"  Calib gap:  {b_gap*100:+.1f}  →  {v_gap*100:+.1f}   (closer to 0 = better)")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
