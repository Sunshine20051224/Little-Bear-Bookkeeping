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
        print(f"[maintenance] Status check warning (non-fatal on startup): {exc}")


def main() -> None:
    """Start FastAPI backend and Streamlit frontend, and shut down cleanly on exit."""
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

    print(f"[start] Starting backend on {BACKEND_URL} ...")
    backend_proc = subprocess.Popen(backend_cmd, cwd=ROOT_DIR)

    time.sleep(1.5)

    print(f"[start] Starting frontend on http://localhost:{FRONTEND_PORT} ...")
    frontend_proc = subprocess.Popen(frontend_cmd, cwd=ROOT_DIR)

    def _shutdown(*_args) -> None:
        print("\n[start] Stopping child processes...")
        for proc in (frontend_proc, backend_proc):
            if proc.poll() is None:
                proc.terminate()
        for proc in (frontend_proc, backend_proc):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while True:
            if backend_proc.poll() is not None:
                print("[start] Backend process exited.")
                _shutdown()
            if frontend_proc.poll() is not None:
                print("[start] Frontend process exited.")
                _shutdown()
            time.sleep(1.0)
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()