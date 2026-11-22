"""SQLAlchemy ORM models for personal expense tracking."""

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Float, Integer, String
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