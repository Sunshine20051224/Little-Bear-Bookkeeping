"""Database engine and SQLAlchemy Session management helpers."""

import os
from collections.abc import Generator
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from database.models import Base


load_dotenv()


def get_database_url() -> str:
    """Resolve database URL from environment, defaulting to local SQLite."""
    return os.getenv("DATABASE_URL", "sqlite:///./expense_tracker.db")


def create_db_engine(database_url: str | None = None):
    """Create SQLAlchemy engine with SQLite-friendly connect arguments when needed."""
    url = database_url or get_database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db(target_engine=None) -> None:
    """Create all database tables if they do not yet exist."""
    active_engine = target_engine or engine
    Base.metadata.create_all(bind=active_engine)


def get_db() -> Generator[Session, None, None]:
    """Yield a database session with automatic commit/rollback and close."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()