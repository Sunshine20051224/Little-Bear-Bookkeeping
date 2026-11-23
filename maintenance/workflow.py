"""在后端和数据库外运行的只读监测与显式授权的数据库替换流程。"""

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from uuid import UUID
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

from maintenance.controller import should_trigger_replacement
from maintenance.storage import write_json_atomically


class WorkflowError(RuntimeError):
    """可安全显示的运行状态或配置错误。"""


def _trial_id(value):
    if not value:
        return None
    address = urlsplit(value)
    locator = (address.scheme, address.hostname, address.port, address.path)
    return hashlib.sha256(json.dumps(locator).encode()).hexdigest()


@dataclass(frozen=True)
class RunSpec:
    source_id: str
    backend_id: str
    workspace_id: str
    plan: str
    region: str
    version: str
    slot_limit: int

    def __post_init__(self):
        if self.slot_limit not in (1, 2):
            raise WorkflowError("DB_INSTANCE_LIMIT must be 1 or 2.")
        for name, value in asdict(self).items():
            if name != "slot_limit" and not re.fullmatch(r"[\w.-]{1,100}", value, re.ASCII):
                raise WorkflowError("Invalid resource, plan, region or version identifier.")


class StateStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "state.json"

    def read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    @contextmanager
    def lock(self, name):
        # 操作系统持有进程锁，进程意外终止也会自动释放。
        with (self.root / (name + ".lock")).open("a+b") as f:
            f.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkflowError("Another controller already holds the " + name + " lock.") from exc
            yield

    def log(self, state, event, **facts):
        # 仅接受内部生成的事件与非敏感指标，不记录远端响应、账目、URL、异常文本。
        line = dict(time=datetime.now(timezone.utc).isoformat(), run_id=state["run_id"],
                    event=event, **facts)
        encoded = json.dumps(line, ensure_ascii=True, allow_nan=False)
        with (self.root / (state["run_id"] + ".jsonl")).open("a", encoding="utf-8") as out:
            out.write(encoded + "\n")
            out.flush()
            os.fsync(out.fileno())
        print(encoded, flush=True)

    def advance(self, state, stage, **changes):
        state.update(changes, stage=stage)
        write_json_atomically(self.path, state)
        facts = {"source_id": state["spec"]["source_id"], "backend_id": state["spec"]["backend_id"],
                 "active_id": state["active_id"], "maintenance_mode": state["maintenance_mode"]}
        for key in ("restored_count", "reference_time", "contents_verified", "monthly_rule_verified",
                    "identifier_verified", "deploy_id", "probe_id", "backend_read_verified",
                    "controlled_write_verified", "source_deleted", "trial_verified"):
            if key in changes:
                facts[key] = changes[key]
        if state.get("target_id"):
            facts["target_id"] = state["target_id"]
        self.log(state, stage, **facts)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _due(status, planned_at, source, configured_source):
    if status.get("completed_maintenance") is True:
        return False
    if planned_at and source == configured_source:
        at = datetime.fromisoformat(planned_at)
        if at.tzinfo is None:
            raise WorkflowError("MAINTENANCE_AT_UTC must include an explicit UTC offset.")
        if datetime.now(timezone.utc) >= at:
            status = {**status, "planned_maintenance": True}
    return should_trigger_replacement(status)[0]


async def _claim_target(provider, run_id):
    for delay in (0, 1, 2):
        if delay:
            await asyncio.sleep(delay)
        target = await provider.find_target(run_id)
        if target:
            return target
    raise WorkflowError("Creation outcome unresolved. Inspect the run-labelled instance before resuming; no second creation was sent.")


async def replace(store, spec, run_id, provider, backend, data, *, trial_url=None,
                  planned_at=None, interval=60.0, wait_seconds=0.0):
    """只能经已授权的命令调用，未就绪、校验失败或创建结果不明时安全停机。"""
    try:
        if str(UUID(run_id)) != run_id:
            raise ValueError
    except ValueError as exc:
        raise WorkflowError("Supply a canonical UUID as run ID.") from exc
    if planned_at:
        instant = datetime.fromisoformat(planned_at)
        if instant.tzinfo is None:
            raise WorkflowError("MAINTENANCE_AT_UTC must include an explicit UTC offset.")
        planned_at = instant.astimezone(timezone.utc).isoformat()
    with store.lock("replacement"):
        state = store.read()
        already_complete = state.get("stage") == "complete" and state.get("run_id") == run_id
        if spec.slot_limit == 1 and not trial_url and not already_complete:
            raise WorkflowError("Set TRIAL_DATABASE_URL before authorising a single-slot run.")
        if state and state["run_id"] != run_id:
            if state["stage"] != "complete" or state["active_id"] != spec.source_id:
                raise WorkflowError("Existing run or active source differs. Resume the recorded run.")
            write_json_atomically(store.root / (state["run_id"] + ".json"), state)
            if (store.root / (run_id + ".json")).exists():
                raise WorkflowError("This run ID is already archived.")
            state = {}
        if not state:
            state = {"run_id": run_id, "spec": asdict(spec), "active_id": spec.source_id,
                     "target_id": None, "maintenance_mode": False, "planned_at": planned_at,
                     "trial_id": _trial_id(trial_url) if spec.slot_limit == 1 else None}
            store.advance(state, "checking")
        elif state["spec"] != asdict(spec):
            raise WorkflowError("Run settings differ from the saved resource authorisation.")
        if state["stage"] == "complete":
            return state
        if spec.slot_limit == 1 and state["trial_id"] != _trial_id(trial_url):
            raise WorkflowError("The dedicated trial database differs from the saved run.")
        try:
            await _advance_run(store, state, spec, provider, backend, data, trial_url, interval, wait_seconds)
        except Exception as exc:
            store.log(state, "stopped", stage=state["stage"], error_type=type(exc).__name__,
                      maintenance_mode=state["maintenance_mode"])
            raise
        return state


async def _advance_run(store, state, spec, provider, backend, data, trial_url, interval, wait_seconds):
    run = state["run_id"]
    source = spec.source_id
    if state["stage"] == "checking":
        deadline = time.monotonic() + wait_seconds
        while True:
            status = await provider.status(source)
            due = _due(status, state["planned_at"], source, source)
            store.log(state, "scheduled_status", source_id=source, due=due, interval_seconds=interval,
                      provider_planned=status.get("planned_maintenance") is True,
                      configured_time=state["planned_at"])
            if due:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkflowError("Maintenance is not due. Resume the same run at its planned time.")
            await asyncio.sleep(min(interval, remaining))
        await provider.preflight(source, spec.backend_id, spec.workspace_id, spec.plan, spec.region,
                                spec.version, spec.slot_limit)
        await backend.preflight()
        # 在阻断业务前确认源可读、单配额试恢复库位置与版本符合要求。
        source_url = await provider.db_url(source)
        await asyncio.to_thread(data.preflight, source_url, trial_url, spec)
        store.log(state, "preflight_passed", source_id=source, workspace_id=spec.workspace_id,
                  backend_id=spec.backend_id, target_plan=spec.plan, target_region=spec.region,
                  target_version=spec.version, simultaneous_limit=spec.slot_limit)
        store.advance(state, "locking", maintenance_mode="lock_pending")
    if state["stage"] == "locking":
        await asyncio.to_thread(data.lock, await provider.db_url(source), run)
        store.advance(state, "source_locked", maintenance_mode=True)
    if state["stage"] == "source_locked":
        backup = await asyncio.to_thread(data.backup, await provider.db_url(source), source, run, store.root)
        store.advance(state, "backed_up", backup=str(backup), sha256=_digest(backup))
    backup = Path(state["backup"])
    if not backup.is_file() or _digest(backup) != state["sha256"]:
        raise WorkflowError("Final backup is missing or its checksum changed; keep writes blocked.")
    payload = json.loads(backup.read_text(encoding="utf-8"))
    if payload["source_instance_id"] != source:
        raise WorkflowError("Backup source ID differs from the authorised source.")
    trial_checked = False
    if state["stage"] == "backed_up":
        store.log(state, "backup_saved", records=len(payload["records"]), sha256=state["sha256"],
                  file=backup.name, reference_time=payload["reference_time"])
        if spec.slot_limit == 1:
            if not trial_url:
                raise WorkflowError("Single-slot operation requires TRIAL_DATABASE_URL.")
            await backend.verify(payload["records"], datetime.fromisoformat(payload["reference_time"]),
                                 payload["expected_summary"])
            proof = await asyncio.to_thread(data.restore, backup, trial_url, run)
            _require_restore(proof, payload)
            trial_checked = True
            store.advance(state, "trial_verified", trial_verified=True, restored_count=proof["restored_count"],
                          reference_time=payload["reference_time"], contents_verified=True,
                          identifier_verified=True, monthly_rule_verified=True)
        else:
            store.advance(state, "ready_to_create")
    if state["stage"] == "trial_verified":
        if not trial_checked:
            # 中断后在删除旧库前重新验证试恢复数据；同一 run 的收据避免重复插入。
            proof = await asyncio.to_thread(data.restore, backup, trial_url, run)
            _require_restore(proof, payload)
        await provider.delete_source(source)
        store.advance(state, "ready_to_create", source_deleted=True)
    if state["stage"] == "ready_to_create":
        if await provider.find_target(run):
            raise WorkflowError("A run-labelled target already exists before the first creation request.")
        store.advance(state, "creating")
        try:
            target = await provider.create_target(run, spec.plan, spec.region, spec.version)
        except Exception:
            target = await _claim_target(provider, run)
        _record_target(store, state, target)
    elif state["stage"] == "creating":
        _record_target(store, state, await _claim_target(provider, run))
    target = state["target_id"]
    if state["stage"] == "target_created":
        await provider.wait_ready(target)
        target_url = await provider.db_url(target)
        await asyncio.to_thread(data.lock, target_url, run, initialize=True)
        proof = await asyncio.to_thread(data.restore, backup, target_url, run)
        _require_restore(proof, payload)
        store.advance(state, "restored", restored_count=proof["restored_count"],
                      reference_time=payload["reference_time"], contents_verified=True,
                      identifier_verified=True, monthly_rule_verified=True)
    if state["stage"] == "restored":
        deployment = await provider.switch_backend(spec.backend_id, target, run)
        if not isinstance(deployment, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", deployment):
            raise WorkflowError("Live backend deployment ID could not be verified.")
        store.advance(state, "switched", deploy_id=deployment)
    if state["stage"] == "switched":
        target_url = await provider.db_url(target)
        pending = await asyncio.to_thread(data.pending_probe, target_url, run)
        await backend.verify(payload["records"], datetime.fromisoformat(payload["reference_time"]),
                             payload["expected_summary"], pending_probe=pending)
        probe = await asyncio.to_thread(data.stage_probe, target_url, run)
        if probe["id"] <= max((row["id"] for row in payload["records"]), default=0):
            raise WorkflowError("Controlled write did not acquire a new identifier after restoration.")
        if not probe["verified"]:
            await backend.verify_probe(probe)
            await asyncio.to_thread(data.finish_probe, target_url, run, probe)
        store.advance(state, "verified", active_id=target, probe_id=probe["id"], verified=True,
                      backend_read_verified=True, controlled_write_verified=True,
                      restored_count=len(payload["records"]), reference_time=payload["reference_time"])
    if state["stage"] == "verified":
        if not state.get("source_deleted"):
            await provider.delete_source(source)
        store.advance(state, "releasing", source_deleted=True)
    if state["stage"] == "releasing":
        await asyncio.to_thread(data.unlock, await provider.db_url(target), run)
        store.advance(state, "complete", maintenance_mode=False)


def _record_target(store, state, target):
    if not isinstance(target, str) or not re.fullmatch(r"[\w-]{1,100}", target, re.ASCII):
        raise WorkflowError("Provider did not return a usable target identifier.")
    if target == state["spec"]["source_id"]:
        raise WorkflowError("The recorded target must be distinct from the source.")
    store.advance(state, "target_created", target_id=target)


def _require_restore(proof, payload):
    if (not proof["verified"] or proof["restored_count"] != len(payload["records"])
            or proof["summary_report"] != payload["expected_summary"]):
        raise WorkflowError("Restored record count or frozen budget result was not verified.")


def _check_dedicated_source(conn):
    from sqlalchemy import inspect, text
    from database.models import ExpenseRecord
    inspector = inspect(conn)
    allowed = {"expenses", "bookkeeping_write_guard", "maintenance_restore_receipt"}
    if set(inspector.get_table_names()) - allowed or inspector.get_view_names():
        raise WorkflowError("Replacement requires a dedicated ledger database with no unrelated data.")
    if set(column["name"] for column in inspector.get_columns("expenses")) != set(ExpenseRecord.__table__.c.keys()):
        raise WorkflowError("Expense schema differs from the supported backup format.")
    if conn.dialect.name == "postgresql":
        other_database = conn.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE NOT datistemplate "
            "AND datname <> current_database() AND datname <> 'postgres')"
        ))
        other_relation = conn.scalar(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                  AND c.relkind IN ('r','p','f','v','m','S')
                  AND NOT (n.nspname='public' AND c.relkind='r' AND c.relname IN
                      ('expenses','bookkeeping_write_guard','maintenance_restore_receipt'))
                  AND c.oid IS DISTINCT FROM pg_get_serial_sequence('public.expenses','id')::regclass
            )
        """))
        if other_database or other_relation:
            raise WorkflowError("The instance is not dedicated to the supported database and tables.")


class DatabaseSteps:
    """通过显式连接操作已授权的源、目标与隔离恢复库。"""

    def __init__(self, budget_service):
        from maintenance import db_guard
        self.budget_service = budget_service
        self.lock = db_guard.lock_database
        self.unlock = db_guard.unlock_database
        self.pending_probe = db_guard.pending_probe
        self.stage_probe = db_guard.stage_probe
        self.finish_probe = db_guard.finish_probe

    def preflight(self, source_url, trial_url, spec):
        from database.session import create_db_engine
        from sqlalchemy import text
        from sqlalchemy.engine import make_url
        engine = create_db_engine(source_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT id FROM expenses LIMIT 1"))
                _check_dedicated_source(conn)
        finally:
            engine.dispose()
        if spec.slot_limit == 1:
            if not trial_url:
                raise WorkflowError("Set TRIAL_DATABASE_URL to a dedicated local PostgreSQL database.")
            url = make_url(trial_url)
            if url.get_backend_name() != "postgresql" or url.host not in ("localhost", "127.0.0.1", "::1"):
                raise WorkflowError("This single-slot implementation needs an isolated local PostgreSQL trial database.")
            if url.database in (None, "", "postgres", "template0", "template1") or source_url == trial_url:
                raise WorkflowError("Supply a dedicated trial database.")
            trial = create_db_engine(trial_url)
            try:
                with trial.connect() as conn:
                    major = int(conn.scalar(text("SHOW server_version_num"))) // 10000
                    if str(major) != spec.version.split(".")[0]:
                        raise WorkflowError("Trial and target PostgreSQL major versions differ.")
                    from sqlalchemy import inspect
                    if inspect(conn).get_table_names():
                        raise WorkflowError("The designated trial database must start empty.")
            finally:
                trial.dispose()

    def backup(self, url, source, run, directory):
        from database.session import create_db_engine
        from maintenance.controller import DatabaseMaintenanceController
        engine = create_db_engine(url)
        try:
            controller = DatabaseMaintenanceController(directory / "backups", self.budget_service)
            return controller.create_backup(engine, source, run + ".json")
        finally:
            engine.dispose()

    def restore(self, path, url, run):
        from maintenance.controller import DatabaseMaintenanceController
        controller = DatabaseMaintenanceController(path.parent, self.budget_service)
        return controller.restore_and_verify(path, url, run_id=run)


def _seconds(key, default, *, positive=True):
    value = float(os.environ.get(key, default))
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise WorkflowError("Invalid interval for " + key)
    return value


async def _command(args):
    provider_name = os.getenv("MAINTENANCE_PROVIDER", "").strip().lower()
    if provider_name != "render-postgres":
        raise WorkflowError("Set MAINTENANCE_PROVIDER to the actual database provider. This adapter supports render-postgres only; no cloud request was sent.")
    from maintenance.render_provider import RenderProvider
    root = Path(os.getenv("MAINTENANCE_STATE_DIR") or Path.home() / ".little-bear-maintenance")
    store = StateStore(root)
    source = os.environ["SOURCE_DB_ID"]
    at = os.getenv("MAINTENANCE_AT_UTC") or None
    interval = _seconds("CHECK_INTERVAL_SECONDS", "60")
    async with httpx.AsyncClient(timeout=httpx.Timeout(10), follow_redirects=False) as client:
        provider = RenderProvider(client)
        if args.command in ("check", "watch"):
            with store.lock("watch") if args.command == "watch" else nullcontext():
                while True:
                    state = store.read()
                    active = state.get("active_id", source)
                    try:
                        result = await provider.status(active)
                        due = _due(result, at, active, source)
                        store.log({"run_id": "monitor"}, "scheduled_status", source_id=active,
                                  due=due, interval_seconds=interval, mode="read_only")
                    except Exception as exc:
                        store.log({"run_id": "monitor"}, "status_unavailable", error_type=type(exc).__name__)
                        if args.command == "check":
                            raise
                    if args.command == "check":
                        return
                    await asyncio.sleep(interval)
        else:
            from backend.dependencies import get_budget_service
            from maintenance.backend_client import BackendClient
            spec = RunSpec(source, os.environ["RENDER_SERVICE_ID"], os.environ["RENDER_OWNER_ID"],
                           os.environ["TARGET_DB_PLAN"], os.environ["TARGET_DB_REGION"],
                           os.environ["TARGET_DB_VERSION"], int(os.environ["DB_INSTANCE_LIMIT"]))
            if (args.authorize_source, args.authorize_backend, args.authorize_workspace,
                args.authorize_delete) != (source, spec.backend_id, spec.workspace_id, source):
                raise WorkflowError("Explicit authorisations must match the configured source, service and workspace.")
            await replace(store, spec, args.run_id, provider, BackendClient(client),
                          DatabaseSteps(get_budget_service()), trial_url=os.getenv("TRIAL_DATABASE_URL"),
                          planned_at=at, interval=interval,
                          wait_seconds=_seconds("MAINTENANCE_WAIT_SECONDS", "0", positive=False))


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="single read-only provider status check")
    commands.add_parser("watch", help="scheduled read-only provider status checks")
    run = commands.add_parser("run", help="run or resume a specifically authorised maintenance request")
    for flag in ("run-id", "authorize-source", "authorize-backend", "authorize-workspace", "authorize-delete"):
        run.add_argument("--" + flag, required=True)
    args = parser.parse_args()
    # 依赖库日志可能包含 URL、数据库语句参数或响应体。证据文件仅写上述已知字段。
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(_command(args))
    except WorkflowError as exc:
        parser.exit(2, str(exc) + "\n")
    except Exception as exc:
        parser.exit(1, "Maintenance stopped (" + type(exc).__name__ +
                    "). Check configured resources and the saved stage; sensitive error details omitted.\n")


if __name__ == "__main__":
    main()
