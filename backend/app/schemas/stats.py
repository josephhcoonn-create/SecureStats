"""
Pydantic response schemas for the /stats analytics endpoints.

Every numeric stat has a parallel ``display_*`` field pre-formatted for
the frontend:
  • batting averages / OBP / SLG / OPS  →  ".302" or "1.023"
  • counting stats (HR, RBI, H)         →  "23"
  • probabilities                        →  "28.4%"
"""

from pydantic import BaseModel, Field

# ── Helpers ───────────────────────────────────────────────────────────────────


def fmt_avg(value: float | None) -> str:
    """Format a rate stat as '.302'. Returns '---' for None."""
    if value is None:
        return "---"
    formatted = f"{value:.3f}"
    # Strip leading '0' for values < 1.0: "0.302" → ".302"
    return formatted[1:] if value < 1.0 and formatted.startswith("0") else formatted


def fmt_int(value: int | float | None) -> str:
    """Format a counting stat as a plain integer string."""
    return "0" if value is None else str(int(value))


def fmt_pct(value: float | None) -> str:
    """Format a probability as '28.4%'. Returns '---' for None."""
    return "---" if value is None else f"{value * 100:.1f}%"


# ── Batting leaders ───────────────────────────────────────────────────────────


class LeaderEntry(BaseModel):
    rank: int
    player_id: int
    mlb_id: int
    full_name: str
    team: str
    position: str
    games_played: int
    at_bats: int
    value: float | None
    display_value: str = Field(..., description="Pre-formatted for UI display")


class BattingLeadersResponse(BaseModel):
    stat: str
    days: int | None = Field(None, description="Rolling window; None = all-time")
    min_at_bats: int
    leaders: list[LeaderEntry]


# ── Team rankings ─────────────────────────────────────────────────────────────


class TeamRankingEntry(BaseModel):
    rank: int
    team: str
    games_played: int
    at_bats: int
    value: float | None
    display_value: str


class TeamRankingsResponse(BaseModel):
    stat: str
    rankings: list[TeamRankingEntry]


# ── Hit probability ───────────────────────────────────────────────────────────


class HitProbabilityResponse(BaseModel):
    player_id: int
    mlb_id: int
    full_name: str
    team: str
    # Component averages
    recent_avg: float | None = Field(None, description="Last-30-game batting average")
    career_avg: float | None = Field(None, description="All-time batting average")
    league_avg: float = Field(..., description="League-wide batting average")
    # Estimate
    hit_probability: float
    display_probability: str
    ci_lower: float = Field(..., description="95% confidence interval lower bound")
    ci_upper: float = Field(..., description="95% confidence interval upper bound")
    display_ci: str = Field(..., description="'[22.1%, 34.7%]'")
    # Sample metadata
    recent_games: int
    recent_at_bats: int
    confidence: str = Field(
        ..., description="'low' | 'medium' | 'high' based on sample size"
    )


# ── Hot / cold streaks ────────────────────────────────────────────────────────


class StreakEntry(BaseModel):
    player_id: int
    mlb_id: int
    full_name: str
    team: str
    position: str
    streak_type: str = Field(..., description="'hot' or 'cold'")
    games: int = Field(..., description="Number of recent games in the window")
    hits: int
    at_bats: int
    period_avg: float | None
    display_avg: str


class StreaksResponse(BaseModel):
    streak_type: str = Field(..., description="'hot', 'cold', or 'both'")
    min_games: int
    hot_threshold: float = 0.350
    cold_threshold: float = 0.150
    streaks: list[StreakEntry]


# ── Player comparison ─────────────────────────────────────────────────────────


class ComparisonPlayerStats(BaseModel):
    player_id: int
    mlb_id: int
    full_name: str
    team: str
    position: str
    # Career totals
    games_played: int
    at_bats: int
    hits: int
    home_runs: int
    rbis: int
    batting_avg: float | None
    on_base_pct: float | None
    slugging_pct: float | None
    ops: float | None
    # Recent form (last 10 games)
    recent_games: int
    recent_avg: float | None
    # Display values
    display_avg: str
    display_ops: str
    display_recent_avg: str


class PlayerComparisonResponse(BaseModel):
    players: list[ComparisonPlayerStats]
    leaders: dict[str, int | None] = Field(
        ...,
        description="Maps stat name → player_id of the leader among compared players",
    )


# ── Enhanced hit probability (Phase 8) ───────────────────────────────────────


class EnhancedHitProbFactors(BaseModel):
    """Per-factor breakdown returned alongside the headline probability."""

    recent_avg: float | None = Field(None, description="Last 15 games batting avg")
    season_avg: float | None = Field(None, description="Current season batting avg")
    career_avg: float | None = Field(None, description="All-loaded games batting avg")
    home_away_split: float | None = Field(
        None, description="Player's split for this game's home/away context"
    )
    pitcher_era: float | None = Field(None, description="Opposing starter season ERA")
    pitcher_whip: float | None = Field(None, description="Opposing starter season WHIP")
    handedness_matchup: float = Field(
        ..., description="+0.015 opposite hand, -0.010 same hand, 0 unknown"
    )
    league_avg: float = Field(..., description="League-wide baseline batting avg")
    # ── Head-to-head + recent-pitcher-form factors ──
    h2h_avg: float | None = Field(
        None, description="Batter's career avg vs this pitcher (null if no data)"
    )
    h2h_at_bats: int = Field(
        0, description="H2H sample size — drives the dynamic H2H weight"
    )
    h2h_weight_applied: float = Field(
        0.0, description="Weight actually given to H2H: 0, 0.075, or 0.15"
    )
    pitcher_recent_era: float | None = Field(
        None, description="Pitcher's ERA over last 3 starts (null if <3 logged)"
    )
    pitcher_season_era: float | None = Field(
        None, description="Pitcher's season ERA, for the recent-vs-season comparison"
    )
    pitcher_trending: str | None = Field(
        None, description="'struggling' | 'steady' | 'locked_in' (null if <3 starts)"
    )


class EnhancedHitProbabilityResponse(BaseModel):
    """Multi-factor hit probability for a batter vs an opposing pitcher."""

    player_id: int
    player_name: str
    game_id: int | None = Field(None, description="The game this projection is for")
    pitcher_id: int | None = Field(
        None, description="Opposing starter used (None = league baseline)"
    )
    pitcher_name: str | None = None
    probability: float = Field(..., description="Clamped to [0.05, 0.95]")
    display_probability: str = Field(..., description="e.g. '32.7%'")
    confidence: int = Field(..., description="0-100 — based on AB + pitcher innings sample")
    threshold_met: bool = Field(
        ..., description="True iff probability ≥ 0.80 (the daily-picks bar)"
    )
    factors: EnhancedHitProbFactors


class DailyPickEntry(BaseModel):
    player_id: int
    player_name: str
    team: str
    opponent: str
    game_id: int
    probability: float
    display_probability: str
    confidence: int
    pitcher_name: str | None = None


class DailyPicksResponse(BaseModel):
    target_date: str
    min_probability: float
    min_confidence: int
    games_considered: int
    candidates_evaluated: int
    picks: list[DailyPickEntry]
