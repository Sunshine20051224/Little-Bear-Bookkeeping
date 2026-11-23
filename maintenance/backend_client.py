"""Compare restored data with public, read-only responses from the deployed backend."""

import asyncio
from datetime import datetime, timezone
import os

import httpx

from backend.dependencies import get_budget_service


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _record(row: dict) -> tuple:
    return (
        row["id"], row["category"], row["amount"], row["note"], row["alert_status"],
        _utc(row["created_at"]),
    )


def _summary_at(records: list[dict], reference: datetime) -> list[dict]:
    reference = _utc(reference)
    rows = []
    for row in records:
        stamp = _utc(row["created_at"])
        if (stamp.year, stamp.month) == (reference.year, reference.month):
            rows.append(row)
    return get_budget_service().build_category_report(rows)


def _compare_summary(actual: list[dict], expected: list[dict]) -> None:
    if sorted(actual, key=lambda row: row["category"]) != sorted(expected, key=lambda row: row["category"]):
        raise RuntimeError("The backend budget summary differs from the verified restoration.")


class BackendClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str | None = None):
        self.client = client
        self.base_url = (base_url or os.getenv("BACKEND_URL") or str(client.base_url)).rstrip("/")
        if not self.base_url:
            raise ValueError("Set BACKEND_URL to the authorised deployed application.")

    async def _get(self, path: str) -> httpx.Response:
        # Render 免费 Web Service 从休眠恢复通常需要约一分钟。
        async with asyncio.timeout(95):
            while True:
                try:
                    response = await self.client.get(
                        f"{self.base_url}{path}", timeout=httpx.Timeout(75, connect=10)
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    pass
                else:
                    if response.status_code not in (502, 503, 504):
                        response.raise_for_status()
                        return response
                await asyncio.sleep(3)

    async def preflight(self) -> None:
        response = await self._get("/health")
        if response.headers.get("X-Bookkeeping-Write-Guard") != "1" or response.json().get("status") != "ok":
            raise RuntimeError("Deploy the backend with database write protection before maintenance.")

    async def verify(
        self, records: list[dict], reference_time: datetime, expected_summary: list[dict],
        pending_probe: dict | None = None,
    ) -> None:
        all_rows = (await self._get("/expenses")).json()
        if not isinstance(all_rows, list):
            raise RuntimeError("The backend expense response was not a list.")
        restored_rows = all_rows
        if pending_probe is not None:
            probe = _record(pending_probe)
            restored_rows = [row for row in all_rows if _record(row) != probe]
            if len(restored_rows) != len(all_rows) - 1:
                raise RuntimeError("The deployed ledger does not contain the recorded pending test write.")
        if sorted(map(_record, restored_rows)) != sorted(map(_record, records)):
            raise RuntimeError("The deployed ledger differs from the restored record contents.")
        _compare_summary(_summary_at(restored_rows, reference_time), expected_summary)

        # An interrupted run may resume in a later UTC month. Verify the fixed reference
        # above and the actual live summary, including any still-present probe, separately.
        before = datetime.now(timezone.utc)
        current = (await self._get("/summary")).json()
        after = datetime.now(timezone.utc)
        if (before.year, before.month) != (after.year, after.month):
            raise RuntimeError("The UTC month changed during the verification read; retry the run.")
        _compare_summary(current, _summary_at(all_rows, before))

    async def verify_probe(self, probe: dict) -> None:
        response = await self._get(f"/expenses/{int(probe['id'])}")
        if _record(response.json()) != _record(probe):
            raise RuntimeError("The deployed backend did not return the target's unique test write.")
