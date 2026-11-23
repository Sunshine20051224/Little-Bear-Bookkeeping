"""离线验证备份还原与供应商暂时不可用时的处理。"""

import asyncio
from datetime import datetime
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from database.models import Base, ExpenseRecord
from database.session import create_db_engine
from maintenance.controller import DatabaseMaintenanceController, fetch_provider_status_async


def test_restore_keeps_real_expense_with_probe_note(tmp_path: Path) -> None:
    """验证试写不会删除同备注的真实支出，源记录的所有字段均保留。"""
    source = create_db_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    target = create_db_engine(target_url)
    table = ExpenseRecord.__table__
    Base.metadata.create_all(source)
    with source.begin() as conn:
        conn.execute(
            table.insert().values(
                id=42,
                category="Food",
                amount=15.50,
                note="post-restore-id-check",
                alert_status="WARNING",
                created_at=datetime(2026, 1, 15, 9, 30),
            )
        )
        original = conn.execute(select(table)).one()

    controller = DatabaseMaintenanceController(backup_dir=tmp_path / "backups")
    backup = controller.create_backup(source, "local-source")
    result = controller.restore_and_verify(backup, target_url)

    assert result["verified"] and result["restored_count"] == 1
    with target.connect() as conn:
        assert conn.execute(select(table)).all() == [original]
    source.dispose()
    target.dispose()


def test_invalid_backup_stops_before_opening_target(tmp_path: Path) -> None:
    """损坏的备份必须在创建目标库前报告错误。"""
    controller = DatabaseMaintenanceController(backup_dir=tmp_path)
    backup = tmp_path / "corrupt.json"
    backup.write_text('{"records": {}}', encoding="utf-8")
    target_path = tmp_path / "target.db"
    with pytest.raises(ValueError, match="records"):
        controller.restore_and_verify(backup, f"sqlite:///{target_path}")
    assert not target_path.exists()


def test_restore_preserves_utc_month_boundaries(tmp_path: Path) -> None:
    """整秒和带偏移的旧备份时间均可通过 ORM 按 UTC 月份查询。"""
    stamps = [
        "2026-11-01 00:00:00",
        "2026-12-01 00:00:00",
        "2026-12-01T02:00:00+03:00",
    ]
    records = [
        {"id": i, "category": "Daily", "amount": 10, "note": "",
         "alert_status": "NORMAL", "created_at": stamp}
        for i, stamp in enumerate(stamps, start=1)
    ]
    backup = tmp_path / "legacy-backup.json"
    backup.write_text(json.dumps({"records": records}), encoding="utf-8")
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    controller = DatabaseMaintenanceController(backup_dir=tmp_path)
    assert controller.restore_and_verify(backup, target_url)["verified"]

    target = create_db_engine(target_url)
    with target.connect() as conn:
        query = select(ExpenseRecord.id).where(
            ExpenseRecord.created_at >= datetime(2026, 11, 1),
            ExpenseRecord.created_at < datetime(2026, 12, 1),
        ).order_by(ExpenseRecord.id)
        assert conn.execute(query).scalars().all() == [1, 3]
        assert conn.scalar(
            select(ExpenseRecord.created_at).where(ExpenseRecord.id == 3)
        ) == datetime(2026, 11, 30, 23)
    target.dispose()


def test_restart_recognises_committed_restore_and_rechecks_contents(tmp_path):
    source = create_db_engine(f"sqlite:///{tmp_path / 'source.db'}")
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    Base.metadata.create_all(source)
    table = ExpenseRecord.__table__
    with source.begin() as conn:
        conn.execute(table.insert().values(category="Daily", amount=10))
    controller = DatabaseMaintenanceController(tmp_path)
    backup = controller.create_backup(source, "source")
    run = "8a7868d6-e9a0-48f9-a947-816ec86022ae"
    first = controller.restore_and_verify(backup, target_url, run_id=run)
    assert controller.restore_and_verify(backup, target_url, run_id=run) == first
    target = create_db_engine(target_url)
    with target.begin() as conn:
        conn.execute(table.update().values(amount=20))
    with pytest.raises(RuntimeError, match="content mismatch"):
        controller.restore_and_verify(backup, target_url, run_id=run)
    source.dispose()
    target.dispose()


@pytest.mark.parametrize(
    "changed_column",
    ["alert_status = 'EXCEEDED'", "created_at = '2000-01-01 00:00:00'"],
)
def test_restore_detects_altered_status_or_timestamp(tmp_path: Path, changed_column: str) -> None:
    """目标库如改写了历史状态或时间，校验应当失败。"""
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    target = create_db_engine(target_url)
    Base.metadata.create_all(target)
    with target.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE TRIGGER change_record AFTER INSERT ON expenses BEGIN "
            f"UPDATE expenses SET {changed_column} WHERE id = NEW.id; END"
        )
    target.dispose()
    record = {
        "id": 7, "category": "Daily", "amount": 10, "note": "demo",
        "alert_status": "NORMAL", "created_at": "2026-01-15T09:30:00",
    }
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"records": [record]}), encoding="utf-8")

    controller = DatabaseMaintenanceController(backup_dir=tmp_path)
    with pytest.raises(RuntimeError, match="content mismatch"):
        controller.restore_and_verify(backup, target_url)
    with target.begin() as conn:
        assert conn.execute(select(ExpenseRecord.id)).all() == []
        conn.exec_driver_sql("DROP TRIGGER change_record")
    assert controller.restore_and_verify(backup, target_url)["restored_count"] == 1
    target.dispose()


def test_status_check_recovers_from_temporary_503() -> None:
    """一次暂时不可用后，状态检查重试并返回第二次取得的状态。"""
    responses = iter([503, 200])
    expected = {"planned_maintenance": False, "completed_maintenance": False}

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(next(responses), json=expected)

    async def fetch() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_provider_status_async(client, "https://provider.test/status", "test")

    assert asyncio.run(fetch()) == expected
