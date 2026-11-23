"""用两份临时 SQLite 和真实 FastAPI 走一遍双实例路径，云 API 使用替身。"""

import asyncio
import json

import httpx
import pytest
from sqlalchemy.orm import Session

from backend.dependencies import get_budget_service
from backend.main import app
from database.models import ExpenseRecord
from database.session import create_db_engine, get_db, init_db
from maintenance.backend_client import BackendClient
from maintenance.workflow import DatabaseSteps, RunSpec, StateStore, replace
from tests.workflow_fakes import Cloud, RUN_ID


def test_real_application_remains_consistent_through_local_replacement(tmp_path, monkeypatch):
    urls = {k: f"sqlite:///{tmp_path / (k + '.db')}" for k in ("source", "target")}
    engines = {key: create_db_engine(url) for key, url in urls.items()}
    active = ["source"]
    init_db(engines["source"])

    def connection():
        with Session(engines[active[0]]) as session, session.begin():
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, connection)

    class LocalCloud(Cloud):
        async def db_url(self, resource):
            return urls[resource]

        async def switch_backend(self, service, target, run):
            active[0] = target
            request = await client.post("/expenses", json={"category": "Food", "amount": 1})
            assert request.status_code == 503
            return "dep-offline"

    async def exercise():
        nonlocal client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test",
        ) as client:
            created = await client.post(
                "/expenses", json={"category": "Food", "amount": 15, "note": "private-note-test-marker"}
            )
            assert created.status_code == 201
            expected = created.json()["expense"]
            cloud = LocalCloud()
            state = await replace(
                StateStore(tmp_path / "state"), RunSpec("source", "service", "owner", "dev", "local", "16", 2),
                RUN_ID, cloud, BackendClient(client, "http://local.test"), DatabaseSteps(get_budget_service()),
            )
            assert state["stage"] == "complete"
            assert (await client.get("/expenses")).json() == [expected]
            assert (await client.post("/expenses", json={"category": "Food", "amount": 5})).status_code == 201
            log = (tmp_path / "state" / (RUN_ID + ".jsonl")).read_text(encoding="utf-8")
            events = {row["event"]: row for row in map(json.loads, log.splitlines())}
            assert events["restored"]["restored_count"] == 1
            assert events["restored"]["monthly_rule_verified"] and events["restored"]["identifier_verified"]
            assert events["switched"]["deploy_id"] == "dep-offline"
            assert events["verified"]["backend_read_verified"] and events["verified"]["probe_id"] > expected["id"]
            assert events["complete"]["active_id"] == "target" and not events["complete"]["maintenance_mode"]
            assert all(s not in log for s in ("private-note-test-marker", "sqlite://", "maintenance-probe-"))

    client = None
    try:
        asyncio.run(exercise())
        with Session(engines["source"]) as source:
            assert source.query(ExpenseRecord).count() == 1
    finally:
        for engine in engines.values():
            engine.dispose()


def test_preflight_rejects_unrelated_data_before_locking(tmp_path):
    from maintenance.workflow import WorkflowError

    url = f"sqlite:///{tmp_path / 'shared.db'}"
    engine = create_db_engine(url)
    init_db(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE another_application (id INTEGER)")
    with pytest.raises(WorkflowError, match="dedicated"):
        DatabaseSteps(get_budget_service()).preflight(
            url, None, RunSpec("source", "service", "owner", "dev", "local", "16", 2)
        )
    engine.dispose()
