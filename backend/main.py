"""FastAPI backend for personal expense tracking and budget alerts."""

from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import logging
import os
import re
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from sqlalchemy import case, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query as DatabaseQuery, Session
from backend.dependencies import get_budget_service
from backend.schemas import (
    CategorySummaryItem,
    ExpenseCreate,
    ExpenseEvaluationResponse,
    ExpenseResponse,
)
from database.models import ExpenseRecord
from database.session import get_db, init_db
from maintenance.db_guard import WritesBlocked, require_app_write
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


def _expenses_in_month(db: Session, reference: datetime) -> DatabaseQuery[ExpenseRecord]:
    """Select expenses in the UTC calendar month containing the reference time."""
    start = reference.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if db.get_bind().dialect.name == "sqlite":
        # 旧版 SQLite 备份可带时差或省略微秒。先截到秒，再换算 UTC，
        # 避免 SQLite 把月末 23:59:59.999999 四舍五入到次月。
        stamp = ExpenseRecord.created_at
        zone = case(
            (func.substr(stamp, -6, 1).in_(("+", "-")), func.substr(stamp, -6)),
            else_="",
        )
        month = func.strftime("%Y-%m", func.substr(stamp, 1, 19).concat(zone))
        return db.query(ExpenseRecord).filter(month == start.strftime("%Y-%m"))
    days_in_month = monthrange(start.year, start.month)[1]
    end = start + timedelta(days=days_in_month)
    return db.query(ExpenseRecord).filter(
        ExpenseRecord.created_at >= start, ExpenseRecord.created_at < end
    )


@app.get("/health")
def health_check(response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    """Lightweight health check endpoint."""
    response.headers["X-Bookkeeping-Write-Guard"] = "1"
    response.headers["X-Bookkeeping-Database"] = db.get_bind().dialect.name
    commit = os.getenv("RENDER_GIT_COMMIT", "")
    if re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        response.headers["X-Bookkeeping-Commit"] = commit
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
        require_app_write(db)
        created_at = datetime.now(timezone.utc)
        previous_spent = float(
            _expenses_in_month(db, created_at)
            .filter(ExpenseRecord.category == payload.category)
            .with_entities(func.coalesce(func.sum(ExpenseRecord.amount), 0.0))
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
            created_at=created_at,
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
    except WritesBlocked as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
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
    """Return spending, budget ratios and alert statuses for the current UTC month."""
    try:
        rows = _expenses_in_month(db, datetime.now(timezone.utc)).all()
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
