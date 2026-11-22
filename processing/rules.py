"""Pure functional domain rules for personal expense and budget evaluation."""

from typing import Any


VALID_CATEGORIES = ("Food", "Transport", "Daily", "Entertainment", "Other")


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

    if monthly_budget == 0:
        usage_ratio = float("inf")
        status = "EXCEEDED"
        over_amount = updated_spent
    else:
        usage_ratio = round(updated_spent / monthly_budget, 4)
        if usage_ratio > 1.0:
            status = "EXCEEDED"
            over_amount = round(updated_spent - monthly_budget, 2)
        elif usage_ratio >= warning_ratio:
            status = "WARNING"
            over_amount = 0.0
        else:
            status = "NORMAL"
            over_amount = 0.0

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
        if budget == 0:
            ratio = float("inf") if total_spent > 0 else 0.0
            status = "EXCEEDED" if total_spent > 0 else "NORMAL"
        else:
            ratio = round(total_spent / budget, 4)
            if ratio > 1.0:
                status = "EXCEEDED"
            elif ratio >= warning_ratio:
                status = "WARNING"
            else:
                status = "NORMAL"

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