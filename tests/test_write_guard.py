"""Exercise maintenance protection through independent database connections."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import asyncio
from datetime import datetime, timezone
import json
import os
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from backend.main import app
from database import session as database
from database.models import Base, ExpenseRecord


_ENGINES = ["sqlite"]
if os.getenv("MAINTENANCE_TEST_POSTGRES_URL"):
    _ENGINES.append("postgresql")


@pytest.fixture(params=_ENGINES)
def guarded_db(request, tmp_path, monkeypatch):
    admin_engine = None
    if request.param == "postgresql":
        test_url = make_url(os.environ["MAINTENANCE_TEST_POSTGRES_URL"])
        if test_url.get_backend_name() != "postgresql" or test_url.host not in {"127.0.0.1", "::1", "localhost"}:
            pytest.fail("Maintenance tests require an isolated loopback PostgreSQL service.")
        schema = f"bookkeeping_test_{uuid4().hex}"
        admin_engine = database.create_db_engine(test_url.render_as_string(hide_password=False))
        with admin_engine.begin() as conn:
            conn.execute(CreateSchema(schema))
        url = test_url.update_query_dict({
            "options": f"-csearch_path={schema} -cTimeZone=Europe/Budapest"
        }).render_as_string(hide_password=False)
    else:
        url = f"sqlite:///{tmp_path / 'guarded.db'}"
    engine = database.create_db_engine(url)
    try:
        Base.metadata.create_all(engine)
        monkeypatch.setattr(database, "engine", engine)
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
        yield url, engine
    finally:
        engine.dispose()
        if admin_engine is not None:
            try:
                with admin_engine.begin() as conn:
                    conn.execute(DropSchema(schema, cascade=True))
            finally:
                admin_engine.dispose()


def test_health_discloses_only_dialect_and_valid_deployed_commit(guarded_db, monkeypatch):
    _, engine = guarded_db
    with TestClient(app) as client:
        for value, expected in [("a" * 40, "a" * 40), ("wrong", None), ("", None)]:
            monkeypatch.setenv("RENDER_GIT_COMMIT", value)
            response = client.get("/health")
            assert response.json() == {"status": "ok"}
            assert response.headers["X-Bookkeeping-Database"] == engine.dialect.name
            assert response.headers.get("X-Bookkeeping-Commit") == expected


def test_guard_survives_backend_restart_and_only_its_owner_can_release(guarded_db):
    url, _ = guarded_db
    with TestClient(app) as client:
        assert client.get("/health").headers.get("X-Bookkeeping-Write-Guard") == "1"
    from maintenance import db_guard
    db_guard.lock_database(url, "demo-a")
    db_guard.lock_database(url, "demo-a")
    with TestClient(app) as client:
        assert client.get("/expenses").json() == []
        assert client.post("/expenses", json={"category": "Food", "amount": 1}).status_code == 503
        with pytest.raises(RuntimeError):
            db_guard.unlock_database(url, "different-owner")
        with pytest.raises(RuntimeError):
            db_guard.lock_database(url, "different-owner")
        assert client.get("/summary").status_code == 200
        db_guard.unlock_database(url, "demo-a")
        db_guard.unlock_database(url, "demo-a")
        assert client.post("/expenses", json={"category": "Food", "amount": 1}).status_code == 201


def test_maintenance_waits_for_admitted_transaction_and_its_last_write(guarded_db):
    from maintenance import db_guard
    url, engine = guarded_db
    entering = Event()

    def start_maintenance():
        entering.set()
        db_guard.lock_database(url, "drain")

    with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as db:
        db_guard.require_app_write(db)
        worker = pool.submit(start_maintenance)
        assert entering.wait(2)
        with pytest.raises(TimeoutError):
            worker.result(timeout=0.15)
        db.add(ExpenseRecord(category="Food", amount=3))
        db.commit()
        worker.result(timeout=4)
    with Session(engine) as db:
        assert db.query(ExpenseRecord).one().amount == 3
        with pytest.raises(db_guard.WritesBlocked):
            db_guard.require_app_write(db)


def test_sqlite_busy_admission_is_retryable(tmp_path):
    from maintenance import db_guard
    engine = database.create_db_engine(f"sqlite:///{tmp_path / 'busy.db'}")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as admitted, Session(engine) as waiting:
            db_guard.require_app_write(admitted)
            waiting.execute(text("PRAGMA busy_timeout = 25"))
            with pytest.raises(db_guard.WritesBlocked, match="retry"):
                db_guard.require_app_write(waiting)
    finally:
        engine.dispose()


def test_probe_survives_retries_and_cleans_only_its_own_row(guarded_db):
    from maintenance import db_guard
    url, engine = guarded_db
    with Session(engine) as db:
        db.add(ExpenseRecord(category="Other", amount=0.01, note="keep"))
        db.commit()
    db_guard.lock_database(url, "probe-a")
    with pytest.raises(RuntimeError):
        db_guard.stage_probe(url, "wrong-owner")
    probe = db_guard.stage_probe(url, "probe-a")
    assert db_guard.stage_probe(url, "probe-a") == probe
    assert db_guard.pending_probe(url, "probe-a") == probe
    assert probe["verified"] is False
    with pytest.raises(RuntimeError):
        db_guard.finish_probe(url, "probe-a", {**probe, "note": "different"})
    db_guard.finish_probe(url, "probe-a", probe)
    db_guard.finish_probe(url, "probe-a", probe)
    assert db_guard.pending_probe(url, "probe-a") is None
    assert db_guard.stage_probe(url, "probe-a")["verified"] is True
    with Session(engine) as db:
        assert db.query(ExpenseRecord).one().note == "keep"


def test_deployed_backend_reads_all_fields_and_the_target_probe(guarded_db):
    from backend.dependencies import get_budget_service
    from maintenance import db_guard
    from maintenance.backend_client import BackendClient

    url, engine = guarded_db
    historical_time = datetime(2001, 12, 31, 20, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add(ExpenseRecord(category="Food", amount=2, note="before switch",
                             alert_status="NORMAL", created_at=historical_time))
        db.commit()
    records = [dict(id=1, category="Food", amount=2, note="before switch",
                    alert_status="NORMAL", created_at=historical_time.isoformat())]
    summary = get_budget_service().build_category_report(records)
    db_guard.lock_database(url, "readback")
    probe = db_guard.stage_probe(url, "readback")

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            backend = BackendClient(http, base_url="http://testserver")
            await backend.preflight()
            await backend.verify(records, historical_time, summary, pending_probe=probe)
            await backend.verify_probe(probe)
            with pytest.raises(RuntimeError):
                await backend.verify_probe({**probe, "note": "wrong-instance"})
            with pytest.raises(RuntimeError):
                await backend.verify([{**records[0], "note": "changed"}], historical_time, summary, probe)
            with pytest.raises(RuntimeError):
                await backend.verify(records, historical_time, [], probe)

    asyncio.run(verify())


def test_restore_keeps_target_blocked_and_advances_generated_ids(guarded_db, tmp_path):
    from maintenance import db_guard
    from maintenance.controller import DatabaseMaintenanceController

    url, engine = guarded_db
    # This fixture owns only a new SQLite file or a unique local PostgreSQL schema.
    Base.metadata.drop_all(engine)
    db_guard.lock_database(url, "restore", initialize=True)
    backup = tmp_path / "synthetic.json"
    record = dict(id=42, category="Food", amount=8.75, note="synthetic",
                  alert_status="NORMAL", created_at="2026-10-01T02:30:00+02:00")
    backup.write_text(json.dumps({"source_instance_id": "local-test", "records": [record]}))
    controller = DatabaseMaintenanceController(backup_dir=tmp_path)
    result = controller.restore_and_verify(backup, url)
    assert result["verified"] and result["restored_count"] == 1
    with TestClient(app) as client:
        assert client.get("/expenses/42").json()["amount"] == 8.75
        assert client.post("/expenses", json={"category": "Food", "amount": 1}).status_code == 503
    assert db_guard.stage_probe(url, "restore")["id"] > 42
