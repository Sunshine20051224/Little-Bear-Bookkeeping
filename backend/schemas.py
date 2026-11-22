"""Pydantic schemas for request validation and API responses."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


CategoryType = Literal["Food", "Transport", "Daily", "Entertainment", "Other"]
AlertStatusType = Literal["NORMAL", "WARNING", "EXCEEDED"]


class ExpenseCreate(BaseModel):
    """Request body schema for creating a new expense record."""

    category: CategoryType
    amount: float = Field(..., gt=0, description="Expense amount must be greater than 0")
    note: str = Field(default="", max_length=200)


class ExpenseResponse(BaseModel):
    """Response schema for a persisted expense record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    amount: float
    note: str
    alert_status: str
    created_at: datetime


class ExpenseEvaluationResponse(BaseModel):
    """Combined response returned after creating and evaluating an expense."""

    expense: ExpenseResponse
    updated_spent: float
    monthly_budget: float
    usage_ratio: float
    status: AlertStatusType
    over_amount: float


class CategorySummaryItem(BaseModel):
    """Summary statistics and alert status for one spending category."""

    category: str
    total_spent: float
    monthly_budget: float
    usage_ratio: float
    status: AlertStatusType