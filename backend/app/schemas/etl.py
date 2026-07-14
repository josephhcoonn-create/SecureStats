from pydantic import BaseModel, Field


class ETLTriggerResponse(BaseModel):
    status: str
    run_date: str
    games_processed: int
    players_upserted: int
    stats_inserted: int
    stats_updated: int
    errors: list[str]
    duration_seconds: float
    success: bool


class OddsTriggerResponse(BaseModel):
    """Result envelope for POST /etl/trigger-odds."""

    target_date: str
    rows_inserted: int = Field(
        ...,
        description="GameOdds rows created. Repeated runs within the same minute return 0 because of the (game, book, fetched_at) unique constraint.",
    )
    quota_remaining: int | None = Field(
        None,
        description="x-requests-remaining from The Odds API after this call.",
    )
    quota_used: int | None = Field(
        None,
        description="x-requests-used from The Odds API after this call.",
    )
    duration_seconds: float
    success: bool


class MatchupTriggerResponse(BaseModel):
    """Result envelope for POST /etl/trigger-matchups."""

    target_date: str
    games: int = Field(..., description="Games with a probable starter processed.")
    pitchers_logged: int = Field(
        ..., description="Probable starters whose recent game log was refreshed."
    )
    batters_updated: int = Field(
        ..., description="Batter-vs-pitcher H2H rows upserted (had shared history)."
    )
    no_history: int = Field(
        ..., description="Batter/pitcher pairs with no shared history (nothing stored)."
    )
    errors: int = Field(..., description="Individual fetches that raised and were skipped.")
    duration_seconds: float
    success: bool
