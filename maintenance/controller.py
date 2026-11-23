"""Maintenance controller for asynchronous DB status checks, backup/restore, and replacement."""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
import httpx
from sqlalchemy import Column, MetaData, String, Table, inspect, text
from sqlalchemy.engine import Engine
from database.models import Base, ExpenseRecord
from database.session import create_db_engine
from maintenance.storage import write_json_atomically
from processing.service import BudgetAdvisorService


logger = logging.getLogger("maintenance.controller")


def _utc_instant(value: str | datetime) -> datetime:
    """SQLite 的无偏移时间按 UTC 解释，带偏移时间换算为同一 UTC 时刻。"""
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _monthly_report(service, records, reference):
    instant = _utc_instant(reference)
    in_month = [
        row for row in records
        if _utc_instant(row["created_at"]).strftime("%Y-%m") == instant.strftime("%Y-%m")
    ]
    return service.build_category_report(in_month)


def should_trigger_replacement(
    status_info: dict[str, Any],
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether replacement should trigger from provider status/expiry info.

    Note: Connection failures or empty data do not trigger replacement.
    """
    reference_now = now or datetime.now(timezone.utc)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)

    completed = status_info.get("completed_maintenance") is True
    if status_info.get("planned_maintenance") is True and not completed:
        return True, "Planned maintenance flag is active."

    expires_at_raw = status_info.get("expires_at")
    if expires_at_raw:
        expires_dt = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        hours_remaining = (expires_dt - reference_now).total_seconds() / 3600.0
        threshold_hours = float(status_info.get("warning_hours_before_expiry", 72.0))
        if hours_remaining <= threshold_hours:
            return True, f"Database expires in {hours_remaining:.1f} hours."

    if completed:
        return False, "Maintenance request is already completed."
    return False, "No maintenance or expiry trigger condition met."


async def fetch_provider_status_async(
    client: httpx.AsyncClient,
    status_url: str,
    api_key: str,
    max_retries: int = 2,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Asynchronously query provider DB status with bounded retries and timeout."""
    headers = {"Authorization": f"Bearer {api_key}"}
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = await client.get(status_url, headers=headers, timeout=timeout_seconds)
            if response.status_code in (429, 502, 503, 504) and attempt < max_retries:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Provider status response must be a JSON object.")
            return data
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            logger.warning("Provider status check attempt %s failed (%s).", attempt + 1, type(exc).__name__)
            if attempt < max_retries:
                await asyncio.sleep(0.2 * (attempt + 1))

    raise RuntimeError(f"Provider status unavailable after {max_retries + 1} attempts.") from last_error


class DatabaseMaintenanceController:
    """Coordinates backup, restoration, verification, and controlled replacement."""

    def __init__(
        self,
        backup_dir: str | Path = "./backups",
        budget_service: BudgetAdvisorService | None = None,
    ) -> None:
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.budget_service = budget_service or BudgetAdvisorService()
        self.writes_blocked: bool = False

    def set_writes_blocked(self, blocked: bool) -> None:
        """Toggle application write lock during maintenance operations."""
        self.writes_blocked = blocked
        logger.info("Maintenance write block set to %s", blocked)

    def create_backup(
        self,
        source_engine: Engine,
        source_instance_id: str,
        backup_name: str | None = None,
    ) -> Path:
        """Export schema metadata and current records from source DB into a JSON backup file."""
        if not source_instance_id.strip():
            raise ValueError("Source database identifier is required for backup.")

        inspector = inspect(source_engine)
        tables = inspector.get_table_names()
        if "expenses" not in tables:
            raise RuntimeError("Source database is missing required 'expenses' table.")

        with source_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, category, amount, note, alert_status, created_at "
                    "FROM expenses ORDER BY id ASC"
                )
            ).fetchall()

        records: list[dict[str, Any]] = []
        for row in rows:
            created_val = row[5]
            records.append(
                {
                    "id": int(row[0]),
                    "category": str(row[1]),
                    "amount": float(row[2]),
                    "note": str(row[3] or ""),
                    "alert_status": str(row[4]),
                    "created_at": created_val.isoformat() if hasattr(created_val, "isoformat") else str(created_val),
                }
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = backup_name or f"backup_{source_instance_id}_{timestamp}.json"
        backup_path = self.backup_dir / filename

        reference = datetime.now(timezone.utc).isoformat()
        payload = {
            "source_instance_id": source_instance_id,
            "created_at": reference,
            "reference_time": reference,
            "budget_settings": {
                "default_budget": self.budget_service.default_budget,
                "warning_ratio": self.budget_service.warning_ratio,
                "category_budgets": self.budget_service.category_budgets,
            },
            "expected_summary": _monthly_report(self.budget_service, records, reference),
            "tables": ["expenses"],
            "records": records,
        }
        write_json_atomically(backup_path, payload)
        logger.info("Backup created at %s with %s records.", backup_path, len(records))
        return backup_path

    def restore_and_verify(
        self,
        backup_path: Path,
        target_database_url: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore backup into an empty target DB and verify records, IDs, and domain rules."""
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup file does not exist: {backup_path}")

        raw_text = backup_path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
        if not isinstance(payload, dict) or "records" not in payload or not isinstance(payload["records"], list):
            raise ValueError("Invalid backup file structure: missing 'records' list.")

        target_engine = create_db_engine(target_database_url)
        records = payload["records"]
        expenses = ExpenseRecord.__table__
        receipt = Table("maintenance_restore_receipt", MetaData(),
                        Column("run_id", String(36), primary_key=True),
                        Column("digest", String(64), nullable=False))
        with ExitStack() as resources:
            resources.callback(target_engine.dispose)
            Base.metadata.create_all(bind=target_engine)
            conn = resources.enter_context(target_engine.begin())
            restored_before = False
            if run_id:
                digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
                receipt.create(conn, checkfirst=True)
                existing = conn.scalar(receipt.select().with_only_columns(receipt.c.digest)
                                       .where(receipt.c.run_id == run_id))
                if existing is not None:
                    if existing != digest:
                        raise RuntimeError("Already restored using a different backup.")
                    restored_before = True
            existing_count = conn.execute(text("SELECT COUNT(*) FROM expenses")).scalar_one()
            if int(existing_count) != 0 and not restored_before:
                raise RuntimeError("Target database must contain no application data before restoration.")

            for item in ([] if restored_before else records):
                conn.execute(
                    expenses.insert(),
                    {
                        "id": int(item["id"]),
                        "category": str(item["category"]),
                        "amount": float(item["amount"]),
                        "note": str(item.get("note", "")),
                        "alert_status": str(item["alert_status"]),
                        "created_at": _utc_instant(item["created_at"]),
                    },
                )

            # Ensure PostgreSQL sequence advances past max restored ID to prevent collisions.
            if target_engine.dialect.name == "postgresql" and records and not restored_before:
                max_id = max(int(r["id"]) for r in records)
                conn.execute(
                    text("SELECT setval(pg_get_serial_sequence('expenses', 'id'), :max_id, true)"),
                    {"max_id": max_id},
                )

            # 校验全部字段后再提交，失败时目标库可以重新执行还原。
            restored_rows = conn.execute(
                expenses.select().order_by(expenses.c.id)
            ).mappings()
            restored_dicts = [dict(row) for row in restored_rows]
            if len(restored_dicts) != len(records):
                raise RuntimeError("Restored record count mismatch.")

            for expected, actual in zip(records, restored_dicts):
                if (
                    int(expected["id"]) != actual["id"]
                    or expected["category"] != actual["category"]
                    or float(expected["amount"]) != actual["amount"]
                    or expected.get("note", "") != actual["note"]
                    or expected["alert_status"] != actual["alert_status"]
                    or _utc_instant(expected["created_at"]) != _utc_instant(actual["created_at"])
                ):
                    raise RuntimeError(f"Restored record content mismatch for id={expected['id']}.")

            if "expected_summary" in payload:
                service = BudgetAdvisorService(**payload["budget_settings"])
                summary_report = _monthly_report(service, restored_dicts, payload["reference_time"])
                if summary_report != payload["expected_summary"]:
                    raise RuntimeError("Restored monthly budget report differs from source.")
            else:
                summary_report = self.budget_service.build_category_report(restored_dicts)
            result = {"restored_count": len(restored_dicts), "source_instance_id": payload.get("source_instance_id"),
                      "summary_report": summary_report, "verified": True}
            if restored_before:
                return result

            # 回滚试写行，保留所有真实记录，包括备注与试写行相同的记录。
            probe_transaction = conn.begin_nested()
            probe = conn.execute(
                expenses.insert().values(
                    category="Other",
                    amount=1.0,
                    note="post-restore-id-check",
                    alert_status="NORMAL",
                    created_at=datetime.now(timezone.utc),
                )
            )
            last_restored_id = restored_dicts[-1]["id"] if restored_dicts else 0
            if probe.inserted_primary_key[0] <= last_restored_id:
                raise RuntimeError("Generated primary key did not advance past restored records.")
            probe_transaction.rollback()
            if run_id:
                conn.execute(receipt.insert().values(run_id=run_id, digest=digest))

        return result
