"""Unit tests for pure budget rules, service logic, and maintenance decisions."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
import pytest
import asyncio
from maintenance.controller import (
    DatabaseMaintenanceController,
    fetch_provider_status_async,
    should_trigger_replacement,
)
from processing.rules import evaluate_budget_status, summarize_expenses_by_category
from processing.service import BudgetAdvisorService


@pytest.mark.parametrize(
    "amount,previous_spent,monthly_budget,warning_ratio,expected_status,expected_spent,expected_over",
    [
        # Normal cases
        (100.0, 200.0, 1000.0, 0.8, "NORMAL", 300.0, 0.0),
        (50.0, 0.0, 500.0, 0.8, "NORMAL", 50.0, 0.0),
        # Boundary cases (exact 80% warning threshold and exact 100% budget limit)
        (100.0, 700.0, 1000.0, 0.8, "WARNING", 800.0, 0.0),
        (200.0, 800.0, 1000.0, 0.8, "WARNING", 1000.0, 0.0),
        # Exceeded cases
        (10.0, 1000.0, 1000.0, 0.8, "EXCEEDED", 1010.0, 10.0),
        (25.0, 0.0, 0.0, 0.8, "EXCEEDED", 25.0, 25.0),
    ],
)
def test_evaluate_budget_status_valid_and_boundary_cases(
    amount: float,
    previous_spent: float,
    monthly_budget: float,
    warning_ratio: float,
    expected_status: str,
    expected_spent: float,
    expected_over: float,
) -> None:
    """Verify pure rule outputs across normal, boundary, and exceeded inputs."""
    result = evaluate_budget_status(
        amount=amount,
        previous_spent=previous_spent,
        monthly_budget=monthly_budget,
        warning_ratio=warning_ratio,
    )
    assert result["status"] == expected_status
    assert result["updated_spent"] == expected_spent
    assert result["over_amount"] == expected_over


@pytest.mark.parametrize(
    "amount,previous_spent,monthly_budget,warning_ratio",
    [
        (0.0, 100.0, 1000.0, 0.8),
        (-20.0, 100.0, 1000.0, 0.8),
        (50.0, -10.0, 1000.0, 0.8),
        (50.0, 100.0, -500.0, 0.8),
        (50.0, 100.0, 1000.0, 1.5),
    ],
)
def test_evaluate_budget_status_invalid_inputs_raise_value_error(
    amount: float,
    previous_spent: float,
    monthly_budget: float,
    warning_ratio: float,
) -> None:
    """Verify invalid amounts, budgets, or thresholds raise ValueError."""
    with pytest.raises(ValueError):
        evaluate_budget_status(
            amount=amount,
            previous_spent=previous_spent,
            monthly_budget=monthly_budget,
            warning_ratio=warning_ratio,
        )


@pytest.mark.parametrize(
    ("amount", "budget", "expected_status", "expected_over"),
    [
        (1199.99, 1500.0, "NORMAL", 0.0),
        (1200.00, 1500.0, "WARNING", 0.0),
        (1500.00, 1500.0, "WARNING", 0.0),
        (1500.01, 1500.0, "EXCEEDED", 0.01),
        (0.04, 0.05, "WARNING", 0.0),
    ],
)
def test_one_cent_budget_boundaries(amount, budget, expected_status, expected_over):
    result = evaluate_budget_status(amount, 0.0, budget)
    report = summarize_expenses_by_category(
        [{"category": "Food", "amount": amount}], {"Food": budget}
    )
    food = next(item for item in report if item["category"] == "Food")
    assert (result["status"], food["status"]) == (expected_status, expected_status)
    assert result["over_amount"] == expected_over


def test_summarize_expenses_does_not_mutate_input_and_service_evaluates() -> None:
    """Verify input records are untouched and BudgetAdvisorService returns expected report."""
    original_records = [{"category": "Food", "amount": 120.0, "note": "Lunch"}]
    snapshot = [dict(item) for item in original_records]

    report = summarize_expenses_by_category(
        records=original_records,
        category_budgets={"Food": 1000.0},
        default_budget=1500.0,
        warning_ratio=0.8,
    )
    assert original_records == snapshot
    food_item = next(item for item in report if item["category"] == "Food")
    assert food_item["total_spent"] == 120.0
    assert food_item["status"] == "NORMAL"

    service = BudgetAdvisorService(category_budgets={"Food": 200.0}, warning_ratio=0.8)
    assessed = service.assess_new_expense(category="Food", amount=170.0, previous_spent=0.0)
    assert assessed["status"] == "WARNING"
    assert assessed["updated_spent"] == 170.0


@pytest.mark.parametrize(
    "status_payload,expected_trigger",
    [
        ({"planned_maintenance": True, "completed_maintenance": False}, True),
        ({"planned_maintenance": False, "completed_maintenance": True}, False),
        (
            {
                "completed_maintenance": True,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            },
            True,
        ),
        (
            {
                "planned_maintenance": False,
                "completed_maintenance": False,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                "warning_hours_before_expiry": 72.0,
            },
            True,
        ),
        (
            {
                "planned_maintenance": False,
                "completed_maintenance": False,
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=20)).isoformat(),
                "warning_hours_before_expiry": 72.0,
            },
            False,
        ),
    ],
)
def test_maintenance_decision_rules(
    status_payload: dict,
    expected_trigger: bool,
) -> None:
    """Verify replacement trigger decisions for maintenance flags and expiry timestamps."""
    triggered, _ = should_trigger_replacement(status_payload, now=datetime.now(timezone.utc))
    assert triggered is expected_trigger


def test_scheduled_run_survives_completed_provider_maintenance():
    from maintenance.workflow import _due

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    status = {"completed_maintenance": True, "planned_maintenance": False}
    assert _due(status, past, "source", "source")
    assert not _due(status, past, "replacement", "source")


def test_maintenance_async_status_and_missing_backup_handling(tmp_path: Path) -> None:
    """Test async provider status retrieval with simulated responses and missing backup error."""
    async def run_async_check() -> dict:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={"planned_maintenance": False, "completed_maintenance": False},
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await fetch_provider_status_async(
                client=client,
                status_url="https://mock-provider.local/status",
                api_key="mock-token",
            )

    status_data = asyncio.run(run_async_check())
    assert status_data["planned_maintenance"] is False

    controller = DatabaseMaintenanceController(backup_dir=tmp_path / "backups")
    missing_file = tmp_path / "backups" / "nonexistent.json"
    with pytest.raises(FileNotFoundError):
        controller.restore_and_verify(
            backup_path=missing_file,
            target_database_url=f"sqlite:///{tmp_path / 'target.db'}",
        )


@pytest.mark.parametrize("slot_limit", [1, 2])
def test_replacement_resumes_after_uncertain_creation(tmp_path, slot_limit):
    from maintenance.workflow import RunSpec, StateStore, replace
    from tests.workflow_fakes import Cloud, Backend, LocalData, RUN_ID

    store = StateStore(tmp_path)
    spec = RunSpec("source", "service", "owner", "free", "region", "16", slot_limit)
    cloud, backend, data = Cloud(), Backend(), LocalData()
    args = (store, spec, RUN_ID, cloud, backend, data)
    cloud.interrupt_creation = True
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(replace(*args, trial_url="scratch"))
    assert data.blocked["source"] == RUN_ID
    assert store.read()["stage"] == "creating"
    assert cloud.events.count("create") == 1
    assert ("delete:source" in cloud.events) == (slot_limit == 1)
    assert ("restore:scratch" in data.events) == (slot_limit == 1)
    asyncio.run(replace(*args, trial_url="scratch"))
    asyncio.run(replace(*args, trial_url="scratch"))
    assert store.read()["stage"] == "complete"
    assert store.read()["active_id"] == "target"
    assert data.blocked["target"] is None
    assert cloud.events.count("create") == cloud.events.count("delete:source") == 1
    assert data.events.count("restore:target") == backend.probes == 1


def test_post_switch_failure_preserves_write_blocks_and_backup(tmp_path):
    from maintenance.workflow import RunSpec, StateStore, replace
    from tests.workflow_fakes import Cloud, Backend, LocalData, RUN_ID

    store = StateStore(tmp_path)
    spec = RunSpec("source", "service", "owner", "free", "region", "16", 2)
    cloud, backend, data = Cloud(), Backend(), LocalData()
    backend.fail_verify = True
    with pytest.raises(RuntimeError, match="synthetic read mismatch"):
        asyncio.run(replace(store, spec, RUN_ID, cloud, backend, data))
    assert store.read()["stage"] == "switched"
    assert data.blocked == {"source": RUN_ID, "target": RUN_ID}
    assert Path(store.read()["backup"]).exists()
    assert "delete:source" not in cloud.events
    backend.fail_verify = False
    asyncio.run(replace(store, spec, RUN_ID, cloud, backend, data))
    assert store.read()["stage"] == "complete"
    assert backend.probes == 1 and data.events.count("restore:target") == 1


def test_resume_after_controller_stops_before_sending_create(tmp_path, monkeypatch):
    from maintenance.workflow import RunSpec, StateStore, replace
    from tests.workflow_fakes import Cloud, Backend, LocalData, RUN_ID

    store = StateStore(tmp_path)
    spec = RunSpec("source", "service", "owner", "free", "region", "16", 1)
    cloud, backend, data = Cloud(), Backend(), LocalData()
    args = (store, spec, RUN_ID, cloud, backend, data)
    advance = store.advance

    def interrupted(state, stage, **changes):
        advance(state, stage, **changes)
        if stage == "creating":
            raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        patch.setattr(store, "advance", interrupted)
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(replace(*args, trial_url="scratch"))

    assert "create" not in cloud.events and "delete:source" in cloud.events
    state = asyncio.run(replace(*args, trial_url="scratch"))
    assert state["stage"] == "complete" and cloud.events.count("create") == 1


def test_timed_out_create_is_claimed_without_a_second_post(tmp_path):
    from maintenance.workflow import RunSpec, StateStore, replace
    from tests.workflow_fakes import Cloud, Backend, LocalData, RUN_ID

    cloud = Cloud()
    cloud.timeout_creation = True
    store = StateStore(tmp_path)
    spec = RunSpec("source", "service", "owner", "free", "region", "16", 2)
    asyncio.run(replace(store, spec, RUN_ID, cloud, Backend(), LocalData()))
    assert store.read()["stage"] == "complete"
    assert cloud.events.count("create") == 1


def test_controller_process_lock_and_provider_failure_are_safe(tmp_path):
    from maintenance.workflow import RunSpec, StateStore, WorkflowError, replace
    from tests.workflow_fakes import Cloud, Backend, LocalData, RUN_ID

    store = StateStore(tmp_path)
    with store.lock("watch"), pytest.raises(WorkflowError, match="watch"):
        with StateStore(tmp_path).lock("watch"):
            pytest.fail("Two schedulers acquired the same lock.")

    class Unavailable(Cloud):
        async def status(self, source):
            raise TimeoutError("secret-test-marker")

    cloud, data = Unavailable(), LocalData()
    spec = RunSpec("source", "service", "owner", "free", "region", "16", 1)
    with pytest.raises(TimeoutError):
        asyncio.run(replace(store, spec, RUN_ID, cloud, Backend(), data, trial_url="scratch"))
    assert not cloud.events and not data.blocked
    assert store.read()["stage"] == "checking"
    assert "secret-test-marker" not in (tmp_path / (RUN_ID + ".jsonl")).read_text()


def test_unknown_database_provider_stops_before_cloud_requests(tmp_path, monkeypatch):
    from argparse import Namespace
    from maintenance.workflow import WorkflowError, _command

    monkeypatch.setenv("MAINTENANCE_PROVIDER", "unconfirmed-database-provider")
    monkeypatch.setenv("MAINTENANCE_STATE_DIR", str(tmp_path / "unused"))
    with pytest.raises(WorkflowError, match="actual database provider"):
        asyncio.run(_command(Namespace(command="run")))
    assert not (tmp_path / "unused").exists()
