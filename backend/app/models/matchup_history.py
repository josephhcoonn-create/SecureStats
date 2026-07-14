"""
MatchupHistory — cached career head-to-head batting line for one
batter against one specific pitcher.

Sourced from the MLB Stats API ``vsPlayerTotal`` split (see
``MLBClient.get_batter_vs_pitcher``) and refreshed by the ETL. One row
per (batter, pitcher) pair; ``last_updated`` records when the cached
line was last pulled so the ETL can decide whether to re-fetch.

Rate stats (``batting_avg`` / ``on_base_pct`` / ``slugging_pct``) are
nullable so "no data yet" is distinguishable from a genuine ``.000``.
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MatchupHistory(Base):
    __tablename__ = "matchup_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batter_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pitcher_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Counting stats
    at_bats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    home_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strikeouts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Rate stats — nullable so "no data yet" ≠ a real .000
    batting_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    on_base_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    slugging_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Both FKs point at players.id, so disambiguate the join with foreign_keys.
    batter: Mapped["Player"] = relationship(  # noqa: F821
        "Player", foreign_keys=[batter_id], lazy="selectin"
    )
    pitcher: Mapped["Player"] = relationship(  # noqa: F821
        "Player", foreign_keys=[pitcher_id], lazy="selectin"
    )

    __table_args__ = (
        # One cached H2H line per (batter, pitcher) — ETL refreshes in place.
        UniqueConstraint(
            "batter_id", "pitcher_id", name="uq_matchup_history_batter_pitcher"
        ),
    )
