"""
PitcherGameLog — one row per pitcher per game from the MLB Stats API
game log (see ``MLBClient.get_pitcher_recent_games``).

Used to compute a pitcher's *recent form* (last-N-starts ERA/WHIP vs.
their season line). ``game_id`` is nullable because the MLB game log
covers every game a pitcher appeared in — including ones outside our
``games`` table's window — so we key de-duplication on
(pitcher_id, game_date) rather than on our internal game id.

``era`` / ``whip`` here are the stats for that *individual* game,
recomputed from parsed innings by the client.
"""
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PitcherGameLog(Base):
    __tablename__ = "pitcher_game_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pitcher_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: the MLB game log may reference games not in our table.
    game_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="SET NULL"),
        nullable=True,
    )
    game_date: Mapped[date] = mapped_column(Date, nullable=False)
    opponent: Mapped[str] = mapped_column(String(100), nullable=False)

    # Per-game counting stats
    innings_pitched: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hits_allowed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    earned_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    walks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strikeouts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-game rate stats — nullable so "no data" ≠ a real 0.00
    era: Mapped[float | None] = mapped_column(Float, nullable=True)
    whip: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    pitcher: Mapped["Player"] = relationship("Player", lazy="selectin")  # noqa: F821
    game: Mapped["Game | None"] = relationship("Game", lazy="selectin")  # noqa: F821

    __table_args__ = (
        # One line per (pitcher, game_date) — re-fetches upsert in place.
        UniqueConstraint(
            "pitcher_id", "game_date", name="uq_pitcher_game_log_pitcher_date"
        ),
    )
