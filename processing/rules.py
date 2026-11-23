"""Pure functional domain rules for personal expense and budget evaluation."""

from decimal import Decimal
from typing import Any


VALID_CATEGORIES = ("Food", "Transport", "Daily", "Entertainment", "Other")


def _classify_spending(
    spent: float, budget: float, warning_ratio: float
) -> tuple[float | None, str]:
    """Classify the monetary amount before rounding the usage ratio for display."""
    if budget == 0:
        return None, "EXCEEDED" if spent > 0 else "NORMAL"

    spent_amount = Decimal(str(spent))
    budget_amount = Decimal(str(budget))
    if spent_amount > budget_amount:
        status = "EXCEEDED"
    elif spent_amount >= budget_amount * Decimal(str(warning_ratio)):
        status = "WARNING"
    else:
        status = "NORMAL"
    return round(spent / budget, 4), status


def evaluate_budget_status(
    amount: float,
    previous_spent: float,
    monthly_budget: float,
    warning_ratio: float = 0.8,
) -> dict[str, Any]:
    """Evaluate budget usage after adding a new expense (pure function).

    Inputs:
        amount: Positive expense amount (> 0).
        previous_spent: Non-negative amount already spent in the category this month (>= 0).
        monthly_budget: Non-negative monthly budget limit (>= 0).
        warning_ratio: Threshold ratio for warning state, in (0, 1].

    Output:
        Dictionary with updated total spent, usage ratio, alert status, and over-budget amount.
        A zero budget has no defined usage ratio.
    """
    if amount <= 0:
        raise ValueError("Expense amount must be greater than 0.")
    if previous_spent < 0:
        raise ValueError("Previous spent amount cannot be negative.")
    if monthly_budget < 0:
        raise ValueError("Monthly budget cannot be negative.")
    if not (0 < warning_ratio <= 1.0):
        raise ValueError("Warning ratio must be between 0 (exclusive) and 1 (inclusive).")

    updated_spent = round(previous_spent + amount, 2)

    usage_ratio, status = _classify_spending(updated_spent, monthly_budget, warning_ratio)
    over_amount = max(0.0, round(updated_spent - monthly_budget, 2))

    return {
        "updated_spent": updated_spent,
        "usage_ratio": usage_ratio,
        "status": status,
        "over_amount": over_amount,
    }


def summarize_expenses_by_category(
    records: list[dict[str, Any]],
    category_budgets: dict[str, float],
    default_budget: float = 1500.0,
    warning_ratio: float = 0.8,
) -> list[dict[str, Any]]:
    """Aggregate expense records by category without mutating input collections."""
    totals: dict[str, float] = {cat: 0.0 for cat in VALID_CATEGORIES}

    for item in records:
        cat = item.get("category", "Other")
        amt = float(item.get("amount", 0.0))
        if amt <= 0:
            continue
        totals[cat] = round(totals.get(cat, 0.0) + amt, 2)

    summaries: list[dict[str, Any]] = []
    for cat, total_spent in totals.items():
        budget = float(category_budgets.get(cat, default_budget))
        ratio, status = _classify_spending(total_spent, budget, warning_ratio)

        summaries.append(
            {
                "category": cat,
                "total_spent": total_spent,
                "monthly_budget": budget,
                "usage_ratio": ratio,
                "status": status,
            }
        )

    return summaries
