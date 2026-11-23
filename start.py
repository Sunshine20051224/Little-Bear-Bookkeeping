"""Startup script for running backend, frontend, and maintenance status check."""

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from dotenv import load_dotenv
import httpx
from maintenance.controller import fetch_provider_status_async, should_trigger_replacement


load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_PORT = os.getenv("BACKEND_PORT", "8001")
FRONTEND_PORT = os.getenv("FRONTEND_PORT", "8501")
BACKEND_URL = os.getenv("BACKEND_URL", f"http://127.0.0.1:{BACKEND_PORT}")


async def run_startup_maintenance_check() -> None:
    """Run a non-destructive status check without authorizing DB deletion or replacement."""
    status_url = os.getenv("DB_PROVIDER_STATUS_URL", "").strip()
    api_key = os.getenv("DB_PROVIDER_API_KEY", "").strip()
    if not status_url or not api_key or status_url.startswith("https://api.example-db-provider"):
        print("[maintenance] Provider status URL not configured; skipping live check.")
        return

    try:
        async with httpx.AsyncClient() as client:
            info = await fetch_provider_status_async(
                client=client,
                status_url=status_url,
                api_key=api_key,
            )
        should_replace, reason = should_trigger_replacement(info)
        print(f"[maintenance] Status check done | trigger_replacement={should_replace} | reason={reason}")
    except Exception as exc:
        print(f"[maintenance] Status check unavailable ({type(exc).__name__}).")


def main() -> None:
    """Start FastAPI backend and Streamlit frontend, and shut down cleanly on exit."""
    scheduled = (
        os.getenv("MAINTENANCE_PROVIDER", "").strip().lower() == "render-postgres"
        and (os.getenv("RENDER_API_KEY") or os.getenv("DB_PROVIDER_API_KEY"))
        and os.getenv("SOURCE_DB_ID") and os.getenv("RENDER_OWNER_ID")
    )
    if not scheduled:
        asyncio.run(run_startup_maintenance_check())

    backend_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(BACKEND_PORT),
    ]
    frontend_cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "frontend/app.py",
        "--server.port",
        str(FRONTEND_PORT),
    ]

    children = []
    def _shutdown(*_args) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        print(f"[start] Starting backend on {BACKEND_URL} ...")
        backend_proc = subprocess.Popen(backend_cmd, cwd=ROOT_DIR)
        children.append(backend_proc)
        time.sleep(1.5)
        print(f"[start] Starting frontend on http://localhost:{FRONTEND_PORT} ...")
        frontend_proc = subprocess.Popen(frontend_cmd, cwd=ROOT_DIR)
        children.append(frontend_proc)
        monitor = None
        if scheduled:
            # 只允许启动监测，不向子进程提供任何替换授权。
            monitor = subprocess.Popen([sys.executable, "-m", "maintenance.workflow", "watch"], cwd=ROOT_DIR)
            children.append(monitor)
        while backend_proc.poll() is None and frontend_proc.poll() is None:
            if monitor is not None and monitor.poll() is not None:
                print("[maintenance] Read-only monitor stopped. See its status message.")
                monitor = None
            time.sleep(1.0)
    finally:
        print("\n[start] Stopping child processes...")
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
        for proc in children:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    main()
