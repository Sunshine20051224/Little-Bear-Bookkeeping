"""SQLAlchemy ORM models for personal expense tracking."""

from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, event
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class ExpenseRecord(Base):
    """Persistent record of a single personal expense entry."""

    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    category = Column(String(50), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    note = Column(String(200), nullable=False, default="")
    alert_status = Column(String(20), nullable=False, default="NORMAL")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class WriteGuard(Base):
    """One database-local row coordinates admitted writes and a maintenance run."""

    __tablename__ = "bookkeeping_write_guard"

    id = Column(Integer, primary_key=True, autoincrement=False)
    owner = Column(String(128))
    last_released_owner = Column(String(128))
    probe_id = Column(Integer)
    probe_note = Column(String(100))
    probe_time = Column(DateTime(timezone=True))
    probe_verified = Column(Boolean, nullable=False, default=False)


@event.listens_for(WriteGuard.__table__, "after_create")
def _seed_guard(table, connection, **_kwargs):
    # App restarts leave an existing row and its maintenance state intact.
    connection.execute(table.insert().values(id=1, probe_verified=False))
