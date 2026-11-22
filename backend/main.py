"""FastAPI backend for personal expense tracking and budget alerts."""

from contextlib import asynccontextmanager
import logging
from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from backend.dependencies import get_budget_service
from backend.schemas import (
    CategorySummaryItem,
    ExpenseCreate,
    ExpenseEvaluationResponse,
    ExpenseResponse,
)
from database.models import ExpenseRecord
from database.session import get_db, init_db
from processing.service import BudgetAdvisorService


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("backend.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize database tables on application startup."""
    try:
        init_db()
        logger.info("Database tables initialized successfully.")
    except SQLAlchemyError as exc:
        logger.error("Failed to initialize database tables: %s", exc)
        raise
    yield


app = FastAPI(
    title="Expense & Budget Alert API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Lightweight health check endpoint."""
    return {"status": "ok"}


@app.post(
    "/expenses",
    response_model=ExpenseEvaluationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_expense(
    payload: ExpenseCreate,
    db: Session = Depends(get_db),
    service: BudgetAdvisorService = Depends(get_budget_service),
) -> ExpenseEvaluationResponse:
    """Create an expense record, evaluate budget rule, and persist to DB."""
    try:
        previous_spent = float(
            db.query(func.coalesce(func.sum(ExpenseRecord.amount), 0.0))
            .filter(ExpenseRecord.category == payload.category)
            .scalar()
        )
        evaluation = service.assess_new_expense(
            category=payload.category,
            amount=payload.amount,
            previous_spent=previous_spent,
        )

        record = ExpenseRecord(
            category=payload.category,
            amount=round(payload.amount, 2),
            note=payload.note.strip(),
            alert_status=evaluation["status"],
        )
        db.add(record)
        db.flush()
        db.refresh(record)
        logger.info(
            "Created expense id=%s category=%s status=%s",
            record.id,
            record.category,
            record.alert_status,
        )
        return ExpenseEvaluationResponse(
            expense=ExpenseResponse.model_validate(record),
            updated_spent=evaluation["updated_spent"],
            monthly_budget=evaluation["monthly_budget"],
            usage_ratio=evaluation["usage_ratio"],
            status=evaluation["status"],
            over_amount=evaluation["over_amount"],
        )
    except ValueError as exc:
        db.rollback()
        logger.warning("Invalid expense input: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Database error while creating expense: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save expense record.",
        ) from exc


@app.get("/expenses", response_model=list[ExpenseResponse])
def list_expenses(
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[ExpenseResponse]:
    """List persisted expense records, optionally filtered by category."""
    try:
        query = db.query(ExpenseRecord)
        if category:
            query = query.filter(ExpenseRecord.category == category)
        records = query.order_by(ExpenseRecord.id.desc()).all()
        return [ExpenseResponse.model_validate(item) for item in records]
    except SQLAlchemyError as exc:
        logger.error("Database error while listing expenses: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve expense records.",
        ) from exc


@app.get("/expenses/{expense_id}", response_model=ExpenseResponse)
def get_expense(
    expense_id: int,
    db: Session = Depends(get_db),
) -> ExpenseResponse:
    """Retrieve a single expense record by ID."""
    try:
        record = db.query(ExpenseRecord).filter(ExpenseRecord.id == expense_id).first()
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Expense record {expense_id} not found.",
            )
        return ExpenseResponse.model_validate(record)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.error("Database error while fetching expense id=%s: %s", expense_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve expense detail.",
        ) from exc


@app.get("/summary", response_model=list[CategorySummaryItem])
def get_summary(
    db: Session = Depends(get_db),
    service: BudgetAdvisorService = Depends(get_budget_service),
) -> list[CategorySummaryItem]:
    """Return category spending totals, budget ratios, and alert statuses."""
    try:
        rows = db.query(ExpenseRecord).all()
        payload = [
            {"category": row.category, "amount": float(row.amount)}
            for row in rows
        ]
        report = service.build_category_report(payload)
        return [CategorySummaryItem.model_validate(item) for item in report]
    except SQLAlchemyError as exc:
        logger.error("Database error while generating summary: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to compute budget summary.",
        ) from exc