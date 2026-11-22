"""Dependency providers for FastAPI routes."""

import os
from dotenv import load_dotenv
from processing.service import BudgetAdvisorService


load_dotenv()


def get_budget_service() -> BudgetAdvisorService:
    """Build a BudgetAdvisorService using environment settings."""
    default_budget = float(os.getenv("DEFAULT_MONTHLY_BUDGET", "1500.0"))
    warning_ratio = float(os.getenv("BUDGET_WARNING_RATIO", "0.8"))
    category_budgets = {
        "Food": float(os.getenv("BUDGET_FOOD", str(default_budget))),
        "Transport": float(os.getenv("BUDGET_TRANSPORT", str(default_budget))),
        "Daily": float(os.getenv("BUDGET_DAILY", str(default_budget))),
        "Entertainment": float(os.getenv("BUDGET_ENTERTAINMENT", str(default_budget))),
        "Other": float(os.getenv("BUDGET_OTHER", str(default_budget))),
    }
    return BudgetAdvisorService(
        category_budgets=category_budgets,
        default_budget=default_budget,
        warning_ratio=warning_ratio,
    )