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