"""Integration tests against the real API, budget rules and an isolated SQLite file."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend import main
from backend.dependencies import get_budget_service
from database import session as database
from database.models import ExpenseRecord
from processing.service import BudgetAdvisorService


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = database.create_db_engine(f"sqlite:///{tmp_path / 'expenses.db'}")
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setitem(
        main.app.dependency_overrides,
        get_budget_service,
        lambda: BudgetAdvisorService(default_budget=100.0, warning_ratio=0.8),
    )
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield client, sessions
    finally:
        engine.dispose()


def test_create_filter_and_retrieve_persisted_expenses(api) -> None:
    client, sessions = api
    assert client.get("/expenses").json() == []
    response = client.post(
        "/expenses", json={"category": "Food", "amount": 80, "note": "  Lunch  "}
    )
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "WARNING"
    assert result["updated_spent"] == 80.0
    record = result["expense"]
    with sessions() as db:
        stored = db.get(ExpenseRecord, record["id"])
        assert (stored.category, stored.amount, stored.note, stored.alert_status) == (
            "Food", 80.0, "Lunch", "WARNING"
        )

    second = client.post("/expenses", json={"category": "Daily", "amount": 5})
    assert second.status_code == 201
    detail = client.get(f"/expenses/{record['id']}")
    assert detail.status_code == 200
    assert detail.json() == record
    filtered = client.get("/expenses", params={"category": "Food"})
    assert filtered.status_code == 200
    assert filtered.json() == [record]
    missing = client.get("/expenses/9999")
    assert missing.status_code == 404
    assert "9999" in missing.json()["detail"]
    invalid = client.post("/expenses", json={"category": "Food", "amount": 0})
    assert invalid.status_code == 422
    with sessions() as db:
        assert db.query(ExpenseRecord).count() == 2


def test_summary_reads_legacy_sqlite_timestamps(api, monkeypatch):
    """旧版恢复留下的整秒和带时差文本都应归入正确的 UTC 月。"""
    client, sessions = api
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 11, 23, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "datetime", clock)
    rows = [
        (10, "2026-11-01 00:00:00"),
        (20, "2026-12-01 00:00:00"),
        (40, "2026-12-01 00:15:00.999999+01:00"),
        (80, "2026-11-30 23:30:00.999999-01:00"),
        (160, "2026-11-30 23:59:59.999999+00:00"),
    ]
    with sessions.begin() as db:
        db.connection().exec_driver_sql(
            "INSERT INTO expenses (category, amount, note, alert_status, created_at) "
            "VALUES ('Food', ?, '', 'NORMAL', ?)", rows
        )
    report = client.get("/summary")
    assert report.status_code == 200
    food = next(row for row in report.json() if row["category"] == "Food")
    assert food["total_spent"] == 210


@pytest.mark.parametrize(
    ("month_start", "next_month"),
    [
        (
            datetime(2026, 12, 1, tzinfo=timezone.utc),
            datetime(2027, 1, 1, tzinfo=timezone.utc),
        ),
        (
            datetime(2028, 2, 1, tzinfo=timezone.utc),
            datetime(2028, 3, 1, tzinfo=timezone.utc),
        ),
    ],
    ids=["year-end", "leap-february"],
)
def test_budget_includes_only_the_current_utc_month(
    api, monkeypatch, month_start, next_month
):
    client, sessions = api
    last_in_month = next_month - timedelta(microseconds=1)
    clock = Mock(wraps=datetime)
    clock.now.return_value = last_in_month
    monkeypatch.setattr(main, "datetime", clock, raising=False)
    with sessions.begin() as db:
        for stamp, amount in [
            (month_start - timedelta(microseconds=1), 200),
            (month_start, 30),
            (last_in_month, 40),
            (next_month, 300),
        ]:
            db.add(ExpenseRecord(category="Food", amount=amount, created_at=stamp))

    response = client.post("/expenses", json={"category": "Food", "amount": 10})
    assert response.status_code == 201
    result = response.json()
    report = client.get("/summary")
    assert report.status_code == 200
    food = next(item for item in report.json() if item["category"] == "Food")
    assert (result["updated_spent"], food["total_spent"]) == (80, 80)
    assert result["status"] == "WARNING"
    assert food == {
        "category": "Food",
        "total_spent": 80,
        "monthly_budget": 100,
        "usage_ratio": 0.8,
        "status": "WARNING",
    }
    with sessions() as db:
        stored = db.get(ExpenseRecord, result["expense"]["id"])
        # SQLite returns offset-free timestamps. The application stores them in UTC.
        assert stored.created_at == last_in_month.replace(tzinfo=None)
    history = client.get("/expenses", params={"category": "Food"})
    assert history.status_code == 200
    assert len(history.json()) == 5


def test_zero_category_budget_returns_null_usage_and_persists_the_expense(api, monkeypatch):
    client, sessions = api
    monkeypatch.setitem(
        main.app.dependency_overrides,
        get_budget_service,
        lambda: BudgetAdvisorService(category_budgets={"Food": 0.0}),
    )
    empty = client.get("/summary")
    assert empty.status_code == 200
    food = next(item for item in empty.json() if item["category"] == "Food")
    assert food["status"] == "NORMAL"
    assert food["usage_ratio"] is None

    created = client.post("/expenses", json={"category": "Food", "amount": 10})
    assert created.status_code == 201
    item = created.json()
    assert item["status"] == "EXCEEDED"
    assert item["usage_ratio"] is None
    assert item["over_amount"] == item["updated_spent"] == 10
    with sessions() as db:
        assert db.get(ExpenseRecord, item["expense"]["id"]).alert_status == "EXCEEDED"
    summary = client.get("/summary")
    assert summary.status_code == 200
    food = next(row for row in summary.json() if row["category"] == "Food")
    assert food["status"] == "EXCEEDED"
    assert food["usage_ratio"] is None
    assert food["total_spent"] == 10


def test_expenses_accept_only_whole_cents(api):
    client, sessions = api
    full = client.post("/expenses", json={"category": "Food", "amount": 100})
    assert full.status_code == 201
    for amount in (0.005, 1.231, 0.000000001):
        rejected = client.post("/expenses", json={"category": "Food", "amount": amount})
        assert rejected.status_code == 422
        assert rejected.json()["detail"][0]["loc"] == ["body", "amount"]
    with sessions() as db:
        assert db.query(ExpenseRecord).count() == 1

    accepted = client.post(
        "/expenses",
        headers={"Content-Type": "application/json"},
        content='{"category":"Food","amount":1.230}',
    )
    assert accepted.status_code == 201
    result = accepted.json()
    assert result["updated_spent"] == 101.23
    assert result["over_amount"] == 1.23
    with sessions() as db:
        assert db.get(ExpenseRecord, result["expense"]["id"]).amount == 1.23
    food = next(row for row in client.get("/summary").json() if row["category"] == "Food")
    assert food["total_spent"] == result["updated_spent"]
    assert food["status"] == result["status"] == "EXCEEDED"
