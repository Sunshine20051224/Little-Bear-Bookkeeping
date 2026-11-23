"""Database write gate and private, resumable post-switch test writes."""

from contextlib import contextmanager
from datetime import datetime, timezone
from secrets import token_hex

from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database.models import ExpenseRecord, WriteGuard
from database.session import create_db_engine


class WritesBlocked(RuntimeError):
    """The controller has closed the database to ordinary application writes."""


def _guard_row(db: Session, *, exclusive: bool) -> WriteGuard:
    statement = select(WriteGuard).where(WriteGuard.id == 1)
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '8s'"))
        statement = statement.with_for_update(read=not exclusive)
    elif dialect == "sqlite":
        # Acquire the writer reservation before reading the flag or any ledger row.
        db.execute(update(WriteGuard).where(WriteGuard.id == 1).values(id=1))
    else:
        raise RuntimeError("Write protection supports only PostgreSQL and SQLite.")
    return db.execute(statement).scalar_one()


def require_app_write(db: Session) -> None:
    """Hold admission until the caller's expense transaction commits or rolls back."""
    try:
        state = _guard_row(db, exclusive=False)
    except OperationalError as exc:
        # SQLite BUSY/LOCKED (including extended codes), PostgreSQL lock timeout.
        sqlite_code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xff
        if sqlite_code in (5, 6) or getattr(exc.orig, "pgcode", None) == "55P03":
            raise WritesBlocked("Database writes are temporarily busy. Please retry later.") from exc
        raise
    if state.owner is not None:
        raise WritesBlocked("Database maintenance is in progress. Please retry later.")


@contextmanager
def _control(database_url: str, run_id: str, *, initialize: bool = False):
    if not database_url or not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128:
        raise ValueError("A database connection and a non-empty maintenance run ID are required.")
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as db, db.begin():
            if initialize:
                # Called only for the authorised, newly allocated replacement target.
                WriteGuard.__table__.create(db.connection(), checkfirst=True)
            yield db, _guard_row(db, exclusive=True)
    finally:
        engine.dispose()


def _require_owner(state: WriteGuard, run_id: str) -> None:
    if state.owner != run_id:
        raise RuntimeError("This run does not own the database write guard.")


def lock_database(database_url: str, run_id: str, *, initialize: bool = False) -> None:
    """Drain admitted writes, then durably block later writes, including after restarts."""
    with _control(database_url, run_id, initialize=initialize) as (_, state):
        if state.owner == run_id:
            return
        if state.owner is not None or state.last_released_owner == run_id:
            raise RuntimeError("The guard is held by another run or this run already finished.")
        state.owner = run_id
        state.probe_id = state.probe_note = state.probe_time = None
        state.probe_verified = False


def unlock_database(database_url: str, run_id: str) -> None:
    with _control(database_url, run_id) as (_, state):
        if state.owner is None and state.last_released_owner == run_id:
            return
        _require_owner(state, run_id)
        if state.probe_id is not None and not state.probe_verified:
            raise RuntimeError("An unverified post-switch test write remains pending.")
        state.owner = None
        state.last_released_owner = run_id


def _probe_data(state: WriteGuard) -> dict:
    stamp = state.probe_time
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return dict(id=state.probe_id, category="Other", amount=0.01, note=state.probe_note,
                alert_status="NORMAL", created_at=stamp.astimezone(timezone.utc).isoformat(),
                verified=state.probe_verified)


def pending_probe(database_url: str, run_id: str) -> dict | None:
    with _control(database_url, run_id) as (_, state):
        _require_owner(state, run_id)
        if state.probe_id is not None and not state.probe_verified:
            return _probe_data(state)
        return None


def stage_probe(database_url: str, run_id: str) -> dict:
    """Commit exactly one nonce-tagged controlled write and remember its row identity."""
    with _control(database_url, run_id) as (db, state):
        _require_owner(state, run_id)
        if state.probe_id is None:
            state.probe_time = datetime.now(timezone.utc)
            state.probe_note = f"maintenance-probe-{token_hex(24)}"
            record = ExpenseRecord(category="Other", amount=0.01, note=state.probe_note,
                                   alert_status="NORMAL", created_at=state.probe_time)
            db.add(record)
            db.flush()
            state.probe_id = record.id
        return _probe_data(state)


def finish_probe(database_url: str, run_id: str, verified_probe: dict) -> None:
    """Remove only the exact probe that the deployed backend successfully returned."""
    with _control(database_url, run_id) as (db, state):
        _require_owner(state, run_id)
        if state.probe_id is None:
            raise RuntimeError("No post-switch test write has been staged.")
        expected = _probe_data(state)
        if any(verified_probe.get(key) != value for key, value in expected.items() if key != "verified"):
            raise RuntimeError("The verified test write does not match the guarded run.")
        if state.probe_verified:
            return
        record = db.get(ExpenseRecord, state.probe_id)
        if (record is None or record.note != state.probe_note or record.amount != 0.01
                or record.category != "Other" or record.alert_status != "NORMAL"
                or record.created_at != state.probe_time):
            raise RuntimeError("The recorded test write has changed; cleanup stopped.")
        db.delete(record)
        state.probe_verified = True
