"""Object-oriented service coordinating budget evaluation and category summaries."""

from typing import Any
from processing.rules import (
    VALID_CATEGORIES,
    evaluate_budget_status,
    summarize_expenses_by_category,
)


class BudgetAdvisorService:
    """Service class encapsulating budget configuration and expense evaluation."""

    def __init__(
        self,
        category_budgets: dict[str, float] | None = None,
        default_budget: float = 1500.0,
        warning_ratio: float = 0.8,
    ) -> None:
        self.default_budget = default_budget
        self.warning_ratio = warning_ratio
        self.category_budgets: dict[str, float] = {
            cat: default_budget for cat in VALID_CATEGORIES
        }
        if category_budgets:
            self.category_budgets.update(category_budgets)

    def get_budget_for_category(self, category: str) -> float:
        """Return configured monthly budget for a given category."""
        return float(self.category_budgets.get(category, self.default_budget))

    def assess_new_expense(
        self,
        category: str,
        amount: float,
        previous_spent: float,
    ) -> dict[str, Any]:
        """Validate category and evaluate budget impact of a new expense."""
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"Unsupported category '{category}'. Allowed: {', '.join(VALID_CATEGORIES)}."
            )

        budget = self.get_budget_for_category(category)
        evaluation = evaluate_budget_status(
            amount=amount,
            previous_spent=previous_spent,
            monthly_budget=budget,
            warning_ratio=self.warning_ratio,
        )
        return {
            "category": category,
            "monthly_budget": budget,
            **evaluation,
        }

    def build_category_report(
        self,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Generate category-level spending and alert status report."""
        return summarize_expenses_by_category(
            records=records,
            category_budgets=self.category_budgets,
            default_budget=self.default_budget,
            warning_ratio=self.warning_ratio,
        )