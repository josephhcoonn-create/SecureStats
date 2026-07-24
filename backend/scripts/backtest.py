"""
Model backtest + per-factor calibration harness.

Reads every graded PickHistory row (actual_result in hit/no_hit) and its
factors_snapshot — the full factor breakdown captured at prediction time —
and reports:

  1. Reconstruction check — recompute each pick's probability from its
     snapshot using the live model weights, and confirm it matches the
     stored value (proves the snapshots are a faithful, re-scoreable record).
  2. Calibration — do the predicted probabilities match reality? Overall
     gap + a bucketed calibration curve (a 78% pick should hit ~78%).
  3. Threshold sweep — picks/day and accuracy at various min-probability
     cutoffs (answers "how do we get to a sane pick count?").
  4. Per-factor attribution — hit rate + predicted-vs-actual gap for the
     pitcher_trending signal and the H2H weight tiers, plus how often the
     pitcher season ERA was a fallback (making "trending" meaningless).
  5. Counterfactual — re-score with the recent-pitcher-form signal
     neutralised and compare discrimination (does it help or hurt?).

Run:  docker compose exec backend python scripts/backtest.py [--days N]

This is an offline analysis tool — it never writes to the DB.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.game import Game
from app.models.pick_history import PickHistory
from app.services.analytics import (
    _EXPECTED_AB_PER_GAME,
    _FALLBACK_ERA,
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
    _pitcher_composite,
)

_FALLBACK_WHIP = 1.30  # mirror analytics._FALLBACK_WHIP


def _or(value, fallback):
    return value if value is not None else fallback


def _score(f: dict, *, recent_form: bool = True) -> float:
    """Recompute the game-level probability from a factors_snapshot.

    Faithfully mirrors calculate_enhanced_hit_probability's blend. When
    recent_form=False the recent-pitcher-form term is neutralised (replaced
    by the season-vs-league fallback) so we can measure its contribution.
    """
    league = f["league_avg"]
    era = _or(f.get("pitcher_era"), _FALLBACK_ERA)
    whip = _or(f.get("pitcher_whip"), _FALLBACK_WHIP)
    season_ref = max(era, 0.5)

    # Batter factors with the model's fallback chain
    eff_recent = _or(f.get("recent_avg"), _or(f.get("season_avg"), _or(f.get("career_avg"), league)))
    eff_season = _or(f.get("season_avg"), _or(f.get("career_avg"), league))
    eff_career = _or(f.get("career_avg"), league)
    eff_home_away = _or(f.get("home_away_split"), _or(f.get("career_avg"), league))

    # Dynamic H2H weight + proportional redistribution to batter factors
    h2h_weight = f.get("h2h_weight_applied", 0.0) or 0.0
    scale = 1.0 + (_W_H2H - h2h_weight) / _W_BATTER_TOTAL
    w_recent, w_season = _W_RECENT * scale, _W_SEASON * scale
    w_career, w_home = _W_CAREER * scale, _W_HOME_AWAY * scale

    # Pitcher terms
    season_term = _pitcher_composite(era, whip, league, handedness=0.0)
    recent_era = f.get("pitcher_recent_era")
    if recent_form and recent_era is not None:
        recent_term = (recent_era / season_ref) * league
    else:
        recent_term = (season_ref / _FALLBACK_ERA) * league
    handed_term = league + (f.get("handedness_matchup") or 0.0)

    h2h_val = _or(f.get("h2h_avg"), league)

    per_ab = (
        w_recent * eff_recent
        + w_season * eff_season
        + w_career * eff_career
        + w_home * eff_home_away
        + h2h_weight * h2h_val
        + _W_PITCHER_RECENT * recent_term
        + _W_PITCHER_SEASON * season_term
        + _W_HANDEDNESS * handed_term
        + _W_LEAGUE * league
    )
    per_ab = max(0.001, min(0.999, per_ab))
    per_game = 1.0 - (1.0 - per_ab) ** _EXPECTED_AB_PER_GAME
    return max(_PROB_MIN, min(_PROB_MAX, per_game))


def _auc(scores_hits: list[float], scores_misses: list[float]) -> float | None:
    """Probability a random hit outranks a random miss (Mann-Whitney AUC).
    0.5 = no discrimination; 1.0 = perfect. None if a class is empty."""
    if not scores_hits or not scores_misses:
        return None
    wins = ties = 0
    for h in scores_hits:
        for m in scores_misses:
            if h > m:
                wins += 1
            elif h == m:
                ties += 1
    return (wins + 0.5 * ties) / (len(scores_hits) * len(scores_misses))


async def main(days: int) -> None:
    cutoff = date.today() - timedelta(days=days)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Game.date,
                    PickHistory.predicted_probability,
                    PickHistory.confidence,
                    PickHistory.actual_result,
                    PickHistory.factors_snapshot,
                )
                .join(Game, Game.id == PickHistory.game_id)
                .where(
                    Game.date >= cutoff,
                    PickHistory.actual_result.in_(("hit", "no_hit")),
                )
                .order_by(Game.date)
            )
        ).all()

    picks = [r for r in rows if r.factors_snapshot]
    n = len(picks)
    if n == 0:
        print(f"No graded picks with snapshots in the last {days} days.")
        return

    dates = sorted({r.date for r in picks})
    hits = sum(1 for r in picks if r.actual_result == "hit")

    print("=" * 68)
    print(f"  BACKTEST — {n} graded picks over {len(dates)} days "
          f"({dates[0]} … {dates[-1]})")
    print("=" * 68)

    # ── 1. Reconstruction check ──────────────────────────────────────────────
    errs = [abs(_score(r.factors_snapshot) - r.predicted_probability) for r in picks]
    print(f"\n[1] Snapshot re-score fidelity: mean |Δ| = {sum(errs)/n:.4f}, "
          f"max = {max(errs):.4f}  (want ≈ 0 — confirms we can re-score offline)")

    # ── 2. Calibration ───────────────────────────────────────────────────────
    mean_pred = sum(r.predicted_probability for r in picks) / n
    actual = hits / n
    print(f"\n[2] CALIBRATION")
    print(f"    Overall: predicted {mean_pred*100:.1f}%  vs actual {actual*100:.1f}%"
          f"  →  gap {(mean_pred-actual)*100:+.1f} pts "
          f"({'OVERconfident' if mean_pred>actual else 'underconfident'})")
    buckets = [(0.72, 0.75), (0.75, 0.80), (0.80, 0.85), (0.85, 0.96)]
    print(f"    {'prob bucket':<14}{'n':>5}{'pred':>8}{'actual':>8}{'gap':>8}")
    for lo, hi in buckets:
        b = [r for r in picks if lo <= r.predicted_probability < hi]
        if not b:
            continue
        mp = sum(r.predicted_probability for r in b) / len(b)
        ac = sum(1 for r in b if r.actual_result == "hit") / len(b)
        print(f"    {f'{lo:.2f}-{hi:.2f}':<14}{len(b):>5}{mp*100:>7.1f}%"
              f"{ac*100:>7.1f}%{(mp-ac)*100:>+7.1f}")

    # ── 3. Threshold sweep ───────────────────────────────────────────────────
    print(f"\n[3] THRESHOLD SWEEP  (picks/day + accuracy at each min-probability)")
    print(f"    {'min prob':>9}{'picks':>7}{'per day':>9}{'accuracy':>10}")
    for thr in (0.72, 0.75, 0.78, 0.80, 0.83, 0.85):
        sub = [r for r in picks if r.predicted_probability >= thr]
        if not sub:
            continue
        acc = sum(1 for r in sub if r.actual_result == "hit") / len(sub)
        print(f"    {thr:>9.2f}{len(sub):>7}{len(sub)/len(dates):>9.1f}{acc*100:>9.1f}%")

    # ── 4. Per-factor attribution ────────────────────────────────────────────
    print(f"\n[4] FACTOR ATTRIBUTION")

    print(f"    pitcher_trending   {'n':>5}{'pred':>8}{'actual':>8}{'gap':>8}")
    tg = defaultdict(list)
    for r in picks:
        tg[r.factors_snapshot.get("pitcher_trending") or "(none)"].append(r)
    for trend in ("struggling", "steady", "locked_in", "(none)"):
        b = tg.get(trend)
        if not b:
            continue
        mp = sum(r.predicted_probability for r in b) / len(b)
        ac = sum(1 for r in b if r.actual_result == "hit") / len(b)
        print(f"    {trend:<18}{len(b):>5}{mp*100:>7.1f}%{ac*100:>7.1f}%{(mp-ac)*100:>+7.1f}")

    print(f"    h2h weight tier    {'n':>5}{'pred':>8}{'actual':>8}{'gap':>8}")
    hg = defaultdict(list)
    for r in picks:
        w = r.factors_snapshot.get("h2h_weight_applied", 0.0) or 0.0
        tier = "full 0.15" if w >= 0.15 else ("half 0.075" if w > 0 else "none 0")
        hg[tier].append(r)
    for tier in ("full 0.15", "half 0.075", "none 0"):
        b = hg.get(tier)
        if not b:
            continue
        mp = sum(r.predicted_probability for r in b) / len(b)
        ac = sum(1 for r in b if r.actual_result == "hit") / len(b)
        print(f"    {tier:<18}{len(b):>5}{mp*100:>7.1f}%{ac*100:>7.1f}%{(mp-ac)*100:>+7.1f}")

    # Fallback season-ERA prevalence — "struggling" vs a fake 4.20 baseline
    strug = tg.get("struggling", [])
    fb = sum(1 for r in strug if abs((r.factors_snapshot.get("pitcher_season_era") or 0) - _FALLBACK_ERA) < 1e-6)
    if strug:
        print(f"    ⚠ {fb}/{len(strug)} 'struggling' picks judged vs the FALLBACK "
              f"season ERA ({_FALLBACK_ERA}) — no real season baseline, so the "
              f"trend is comparing recent form to a guess.")

    # ── 5. Counterfactual: recent-form neutralised ───────────────────────────
    real_hits = [r.predicted_probability for r in picks if r.actual_result == "hit"]
    real_miss = [r.predicted_probability for r in picks if r.actual_result == "no_hit"]
    cf = [(_score(r.factors_snapshot, recent_form=False), r.actual_result) for r in picks]
    cf_hits = [p for p, a in cf if a == "hit"]
    cf_miss = [p for p, a in cf if a == "no_hit"]
    auc_real, auc_cf = _auc(real_hits, real_miss), _auc(cf_hits, cf_miss)
    print(f"\n[5] COUNTERFACTUAL — recent-pitcher-form signal ON vs OFF")
    print(f"    Discrimination (AUC, higher=better at separating hits from misses):")
    print(f"      current model      : {auc_real:.3f}" if auc_real else "      current: n/a")
    print(f"      recent-form OFF    : {auc_cf:.3f}" if auc_cf else "      off: n/a")
    if auc_real is not None and auc_cf is not None:
        verdict = ("HURTS — removing it improves discrimination"
                   if auc_cf > auc_real else
                   "helps — keeping it improves discrimination"
                   if auc_cf < auc_real else "neutral")
        print(f"      → recent-form is: {verdict}")
    print("\n    Note: evaluated on picks the CURRENT model surfaced (selection")
    print("    bias). Definitive proof needs full-slate re-scoring vs batting_stats;")
    print("    this is a strong directional read on the picks we actually made.")
    print("=" * 68)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="look back this many days")
    args = ap.parse_args()
    asyncio.run(main(args.days))
