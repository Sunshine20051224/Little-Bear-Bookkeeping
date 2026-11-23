"""Render REST API 适配层。连接串仅在内存中传给数据库或后端配置接口。"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

# 由 Render 公共 OpenAPI 的 postgresPOSTInput.plan 和 region 值核对。
POSTGRES_PLANS = frozenset(
    [
        "free",
        "starter",
        "standard",
        "pro",
        "pro_plus",
        "custom",
        "basic_256mb",
        "basic_1gb",
        "basic_4gb",
        "pro_4gb",
        "pro_8gb",
        "pro_16gb",
        "pro_32gb",
        "pro_64gb",
        "pro_128gb",
        "pro_192gb",
        "pro_256gb",
        "pro_384gb",
        "pro_512gb",
        "accelerated_16gb",
        "accelerated_32gb",
        "accelerated_64gb",
        "accelerated_128gb",
        "accelerated_256gb",
        "accelerated_384gb",
        "accelerated_512gb",
        "accelerated_768gb",
        "accelerated_1024gb",
        "0.1c-256mb",
        "0.5c-1g",
        "1c-2g",
        "1c-4g",
        "2c-4g",
        "2c-8g",
        "2c-16g",
        "4c-16g",
        "4c-32g",
        "8c-32g",
        "8c-64g",
        "16c-64g",
        "16c-128g",
        "32c-128g",
        "32c-256g",
        "48c-192g",
        "48c-384g",
        "64c-256g",
        "64c-512g",
        "96c-384g",
        "96c-768g",
        "128c-512g",
        "128c-1024g",
    ]
)
POSTGRES_REGIONS = {"frankfurt", "oregon", "ohio", "singapore", "virginia"}


class RenderError(RuntimeError):
    """只包含可写入运行日志的错误类别或 HTTP 状态码。"""


class RenderRequestRejected(RenderError):
    """Render 明确拒绝了请求，可以在同一运行中再次提交。"""


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise RenderError("Invalid Render resource ID.")
    return value


def _tag(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _locator(value: str) -> tuple[str, str, int, str]:
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.split("+")[0] not in ("postgres", "postgresql")
            or not parts.hostname
            or parts.path in ("", "/")
        ):
            raise ValueError
        return parts.hostname, parts.path, parts.port or 5432, parts.username or ""
    except (TypeError, ValueError):
        raise RenderError("Invalid PostgreSQL connection locator.") from None


def _tls_url(value: str, internal: bool) -> str:
    _locator(value)
    parts = urlsplit(value)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if internal or query.get("sslmode") not in {"verify-full", "verify-ca"}:
        query["sslmode"] = "require"
    return urlunsplit(("postgresql", parts.netloc, parts.path, urlencode(query), ""))


def classify_database(value):
    """只返回预定义标签，配置值中的地址、用户名和路径不会出现在结果中。"""
    try:
        parts = urlsplit(value)
        kind = parts.scheme.lower().split("+", 1)[0]
        hostname = (parts.hostname or "").lower().rstrip(".")
    except (ValueError, TypeError):
        return {"database_type": "unknown", "provider": "unknown"}
    database_type = {
        "postgres": "PostgreSQL",
        "postgresql": "PostgreSQL",
        "sqlite": "SQLite",
        "mysql": "MySQL",
        "mariadb": "MariaDB",
        "mongodb": "MongoDB",
    }.get(kind, "unknown")
    if database_type == "SQLite":
        return {"database_type": database_type, "provider": "local-file"}
    provider = "unknown"
    if database_type == "PostgreSQL":
        if hostname.endswith(".neon.tech"):
            provider = "Neon"
        elif hostname.endswith((".supabase.co", ".pooler.supabase.com")):
            provider = "Supabase"
        elif hostname.startswith("dpg-") and hostname.endswith("-postgres.render.com"):
            provider = "Render"
    return {"database_type": database_type, "provider": provider}


class RenderBackend:
    """独立的 Render Web Service 读取与部署适配器，数据库供应商由调用者决定。"""

    def __init__(self, client, *, poll_interval=3.0, wait_timeout=600.0):
        self._configure(
            client, os.getenv("RENDER_API_KEY"), poll_interval, wait_timeout
        )

    def _configure(self, client, api_key, poll_interval, wait_timeout):
        if not api_key:
            raise RenderError("A Render API key is required.")
        self.client = client
        self.api_key = api_key
        self.owner = os.getenv("RENDER_OWNER_ID")
        self.backend = os.getenv("RENDER_SERVICE_ID")
        if self.owner:
            _id(self.owner)
        if self.backend:
            _id(self.backend)
        self.state_dir = (
            Path(
                os.getenv("MAINTENANCE_STATE_DIR")
                or Path.home() / ".little-bear-maintenance"
            )
            .expanduser()
            .resolve()
        )
        self.poll_interval = poll_interval
        self.wait_timeout = wait_timeout

    async def diagnose(self):
        _id(self.backend)
        row = await self._api(
            "GET", f"/services/{self.backend}/env-vars/DATABASE_URL", missing=True
        )
        if row is None:
            return {"database_type": "not-found-on-service", "provider": "unknown"}
        if (
            not isinstance(row, dict)
            or row.get("key") != "DATABASE_URL"
            or not isinstance(row.get("value"), str)
        ):
            raise RenderError("Unexpected DATABASE_URL response from Render.")
        return classify_database(row["value"])

    async def preflight_connection(
        self, backend_id, workspace_id, source_urls, *, region=None
    ):
        if (
            not self.backend
            or not self.owner
            or backend_id != self.backend
            or workspace_id != self.owner
        ):
            raise RenderError("Unauthorized backend or workspace.")
        backend = await self._api("GET", f"/services/{self.backend}")
        if (
            backend.get("id") != self.backend
            or backend.get("ownerId") != self.owner
            or backend.get("type") != "web_service"
            or (region and backend.get("serviceDetails", {}).get("region") != region)
        ):
            raise RenderError(
                "Backend ID, workspace, type or private network region did not match."
            )
        permitted = {_locator(url) for url in source_urls}
        if _locator(await self._env_url()) not in permitted:
            raise RenderError(
                "Backend DATABASE_URL does not point to the authorized source."
            )
        self._save(
            self._snapshot("backend-source"),
            {"sourceLocators": sorted(_tag(repr(locator)) for locator in permitted)},
        )

    async def _api(self, method, path, *, params=None, body=None, missing=False):
        for attempt in range(3):
            try:
                response = await self.client.request(
                    method,
                    "https://api.render.com/v1" + path,
                    params=params,
                    json=body,
                    follow_redirects=False,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=httpx.Timeout(12.0, connect=5.0),
                )
            except httpx.RequestError:
                if method != "GET" or attempt == 2:
                    raise RenderError(
                        f"Render {method} network failure; outcome unconfirmed."
                    ) from None
            else:
                status = response.status_code
                if status == 404 and missing:
                    return None
                if 200 <= status < 300:
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        raise RenderError("Render returned invalid JSON.") from None
                if method == "POST" and status in (400, 401, 403, 404, 422, 429):
                    raise RenderRequestRejected(f"Render {method} rejected (HTTP {status}).")
                if (
                    method != "GET"
                    or status not in (429, 500, 502, 503, 504)
                    or attempt == 2
                ):
                    raise RenderError(f"Render {method} failed (HTTP {status}).")
            await asyncio.sleep(min(self.poll_interval, 2**attempt))
        raise RenderError("Render read retries exhausted.")

    async def _pages(self, path, key, filters):
        seen = set()
        params = {**filters, "limit": 100}
        async with asyncio.timeout(self.wait_timeout):
            while True:
                page = await self._api("GET", path, params=params)
                if not isinstance(page, list):
                    raise RenderError("Unexpected Render list response.")
                if not page:
                    break
                for entry in page:
                    if not isinstance(entry, dict) or not isinstance(
                        entry.get(key), dict
                    ):
                        raise RenderError("Unexpected Render pagination entry.")
                    yield entry[key]
                cursor = page[-1].get("cursor")
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise RenderError("Invalid Render pagination cursor.")
                seen.add(cursor)
                params["cursor"] = cursor

    def _snapshot(self, name):
        return (
            self.state_dir
            / f"render-{_tag(self.owner + '/' + self.backend)}-{name}.json"
        )

    def _save(self, path, data):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.state_dir, prefix=".render-", delete=False
        ) as out:
            tmp = Path(out.name)
            try:
                json.dump(data, out)
                out.flush()
                os.fsync(out.fileno())
            except BaseException:
                out.close()
                tmp.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    async def _env_url(self):
        row = await self._api("GET", f"/services/{self.backend}/env-vars/DATABASE_URL")
        if not isinstance(row, dict) or row.get("key") != "DATABASE_URL":
            raise RenderError(
                "The backend needs its own DATABASE_URL environment variable."
            )
        return row["value"]

    async def _latest_deploy(self):
        page = await self._api(
            "GET", f"/services/{self.backend}/deploys", params={"limit": 1}
        )
        if (
            not isinstance(page, list)
            or not page
            or not isinstance(page[0].get("deploy"), dict)
        ):
            raise RenderError("Cannot establish the backend's most recent deployment.")
        return page[0]["deploy"]

    async def switch_to_url(self, backend_id, target_id, target_url, run_id):
        """由已授权的数据库适配器提供目标 PostgreSQL 连接，等待 Render 部署进入 live。"""
        if not self.owner or backend_id != self.backend or not backend_id:
            raise RenderError("Unauthorized backend or workspace.")
        target_id = _id(target_id)
        target_url = _tls_url(target_url, False)
        path = self._snapshot("deploy-" + _tag(run_id))
        if path.exists():
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            if checkpoint["target"] != target_id or checkpoint.get("locator") != _tag(
                repr(_locator(target_url))
            ):
                raise RenderError(
                    "Stored deployment checkpoint belongs to another database."
                )
        else:
            previous = await self._latest_deploy()
            if previous.get("status") != "live":
                raise RenderError(
                    "The latest backend deployment must finish successfully before switching."
                )
            checkpoint = {
                "target": target_id,
                "locator": _tag(repr(_locator(target_url))),
                "previous": _id(previous["id"]),
                "issued": False,
            }
            self._save(path, checkpoint)
        current_url = await self._env_url()
        if current_url != target_url:
            source_file = self._snapshot("backend-source")
            template = (
                json.loads(source_file.read_text(encoding="utf-8"))
                if source_file.exists()
                else {}
            )
            if checkpoint["issued"] or _tag(
                repr(_locator(current_url))
            ) not in template.get("sourceLocators", []):
                raise RenderError(
                    "Backend database changed after preflight; switch stopped."
                )
            await self._api(
                "PUT",
                f"/services/{self.backend}/env-vars/DATABASE_URL",
                body={"value": target_url},
            )
        if await self._env_url() != target_url:
            raise RenderError(
                "Render did not retain the requested backend DATABASE_URL."
            )
        deploys_path = f"/services/{self.backend}/deploys"
        if not checkpoint["issued"]:
            checkpoint["issued"] = True
            self._save(path, checkpoint)
            try:
                response = await self._api(
                    "POST", deploys_path, body={"deployMode": "deploy_only"}
                )
            except RenderRequestRejected:
                checkpoint["issued"] = False
                self._save(path, checkpoint)
                raise
            if response:
                checkpoint["deploy"] = _id(response.get("id"))
                self._save(path, checkpoint)
        async with asyncio.timeout(self.wait_timeout):
            while True:
                deploy_id = checkpoint.get("deploy")
                if deploy_id:
                    deployment = await self._api(
                        "GET", f"{deploys_path}/{_id(deploy_id)}"
                    )
                else:
                    deployment = await self._latest_deploy()
                    if deployment.get("id") == checkpoint["previous"]:
                        await asyncio.sleep(self.poll_interval)
                        continue
                    checkpoint["deploy"] = _id(deployment.get("id"))
                    self._save(path, checkpoint)
                if deployment.get("status") == "live":
                    return _id(deployment.get("id"))
                if deployment.get("status") in {
                    "deactivated",
                    "build_failed",
                    "update_failed",
                    "pre_deploy_failed",
                    "canceled",
                }:
                    raise RenderError("Backend deployment did not become live.")
                await asyncio.sleep(self.poll_interval)


class RenderProvider(RenderBackend):
    """仅用于明确选用 Render Postgres 的工作流。"""

    def __init__(self, client, *, poll_interval=3.0, wait_timeout=600.0):
        key = os.getenv("RENDER_API_KEY") or os.getenv("DB_PROVIDER_API_KEY")
        self._configure(client, key, poll_interval, wait_timeout)
        _id(self.owner)
        self.plan = os.getenv("TARGET_DB_PLAN")
        self.region = os.getenv("TARGET_DB_REGION")
        self.version = os.getenv("TARGET_DB_VERSION")

    def _identity(self, row, instance_id=None):
        if (
            not isinstance(row, dict)
            or row.get("owner", {}).get("id") != self.owner
            or row.get("role") != "primary"
        ):
            raise RenderError("Postgres ownership or primary role did not match.")
        found = _id(row.get("id"))
        if instance_id is not None and instance_id != found:
            raise RenderError("Render returned an unexpected Postgres ID.")
        return found

    def _spec(self, plan, region, version):
        if (
            plan not in POSTGRES_PLANS
            or region not in POSTGRES_REGIONS
            or (plan, region, str(version)) != (self.plan, self.region, self.version)
        ):
            raise RenderError("Configured and authorized target specifications differ.")

    def _target(self, row, name):
        found = self._identity(row)
        if row.get("name") != name:
            raise RenderError("Created database has an unexpected run marker.")
        self._spec(row.get("plan"), row.get("region"), row.get("version"))
        return found

    async def _database(self, instance_id, *, missing=False):
        row = await self._api("GET", f"/postgres/{_id(instance_id)}", missing=missing)
        if row is not None:
            self._identity(row, instance_id)
        elif not missing:
            raise RenderError("Render returned an empty database description.")
        return row

    async def status(self, instance_id):
        row = await self._database(instance_id)
        maintenance = row.get("maintenance") or {}
        state = maintenance.get("state")
        current = row.get("status")
        pending = state in {"scheduled", "in_progress"} or current in {
            "maintenance_scheduled",
            "maintenance_in_progress",
        }
        return {
            "planned_maintenance": pending,
            "completed_maintenance": state == "succeeded" and not pending,
            "expires_at": row.get("expiresAt"),
        }

    async def _connections(self, instance_id):
        info = await self._api("GET", f"/postgres/{_id(instance_id)}/connection-info")
        if not isinstance(info, dict):
            raise RenderError("Missing database connection information.")
        return info

    async def db_url(self, instance_id, *, internal=False):
        await self._database(instance_id)
        info = await self._connections(instance_id)
        key = "internalConnectionString" if internal else "externalConnectionString"
        return _tls_url(info.get(key, ""), internal)

    async def preflight(
        self, source_id, backend_id, workspace_id, plan, region, version, slot_limit
    ):
        self._spec(plan, region, version)
        if backend_id != self.backend or workspace_id != self.owner or slot_limit < 1:
            raise RenderError(
                "Authorization does not match the backend, workspace or capacity."
            )
        source = await self._database(source_id)
        if str(source.get("version")) != self.version:
            raise RenderError(
                "Source and authorized target PostgreSQL major versions differ."
            )
        if source.get("readReplicas"):
            raise RenderError(
                "Source has additional replicas outside the single-instance workflow."
            )
        info = await self._connections(source_id)
        await self.preflight_connection(
            backend_id,
            workspace_id,
            (info["internalConnectionString"], info["externalConnectionString"]),
            region=region,
        )

        databases = [
            row
            async for row in self._pages(
                "/postgres",
                "postgres",
                {"ownerId": self.owner, "includeReplicas": "true"},
            )
        ]
        if any(row.get("owner", {}).get("id") != self.owner for row in databases):
            raise RenderError("Database inventory contained a different workspace.")
        if source_id not in {db.get("id") for db in databases}:
            raise RenderError("Authorized source was missing from workspace inventory.")
        if len(databases) + (0 if slot_limit == 1 else 1) > slot_limit:
            raise RenderError(
                "Workspace inventory exceeds the authorized simultaneous instance limit."
            )
        if plan == "free" and any(
            db.get("plan") == "free" and (slot_limit != 1 or db.get("id") != source_id)
            for db in databases
        ):
            raise RenderError(
                "A free Postgres instance already occupies this workspace's free slot."
            )
        allowed = source.get("ipAllowList")
        if not isinstance(allowed, list) or any(
            not isinstance(x, dict) or "cidrBlock" not in x for x in allowed
        ):
            raise RenderError("Cannot preserve the source network allowlist.")
        # 不持久化服务商描述字段，描述可能包含私人备注。
        template = {
            "source": source_id,
            "ipAllowList": [
                {
                    "cidrBlock": item["cidrBlock"],
                    "description": "preserved source access",
                }
                for item in allowed
            ],
        }
        environment = source.get("environmentId")
        if environment:
            template["environmentId"] = _id(environment)
        self._save(self._snapshot("source"), template)

    async def find_target(self, run_id):
        name = "bear-" + _tag(run_id)
        matches = set()
        async for row in self._pages(
            "/postgres", "postgres", {"name": name, "ownerId": self.owner}
        ):
            if row.get("name") == name:
                matches.add(self._target(row, name))
        if len(matches) > 1:
            raise RenderError("More than one Postgres database has this run marker.")
        return next(iter(matches), None)

    async def create_target(self, run_id, plan, region, version):
        self._spec(plan, region, version)
        found = await self.find_target(run_id)
        if found:
            return found
        template_path = self._snapshot("source")
        if not template_path.exists():
            raise RenderError("Source networking was not recorded by preflight.")
        template = json.loads(template_path.read_text(encoding="utf-8"))
        marker = self._snapshot("create-" + _tag(run_id))
        if marker.exists():
            raise RenderError(
                "Create previously issued; reconcile the same run marker before retrying."
            )
        name = "bear-" + _tag(run_id)
        body = {
            "name": name,
            "ownerId": self.owner,
            "plan": plan,
            "region": region,
            "version": str(version),
            "ipAllowList": template["ipAllowList"],
        }
        if "environmentId" in template:
            body["environmentId"] = template["environmentId"]
        self._save(marker, {"issued": True})
        try:
            created = await self._api("POST", "/postgres", body=body)
        except RenderRequestRejected:
            marker.unlink()
            raise
        return self._target(created, name)

    async def wait_ready(self, instance_id):
        async with asyncio.timeout(self.wait_timeout):
            while True:
                row = await self._database(instance_id)
                state = row["status"]
                if state == "available":
                    return
                if state in {"recovery_failed", "suspended"}:
                    raise RenderError(
                        "Target Postgres entered a terminal or suspended state."
                    )
                await asyncio.sleep(self.poll_interval)

    async def delete_source(self, instance_id):
        row = await self._database(instance_id, missing=True)
        if row is None:
            return
        source_file = self._snapshot("source")
        if (
            not source_file.exists()
            or json.loads(source_file.read_text(encoding="utf-8")).get("source")
            != instance_id
        ):
            raise RenderError(
                "Deletion does not match the source verified by preflight."
            )
        if row.get("readReplicas"):
            raise RenderError("Additional source replicas appeared after preflight.")
        await self._api("DELETE", f"/postgres/{_id(instance_id)}", missing=True)
        async with asyncio.timeout(self.wait_timeout):
            while await self._database(instance_id, missing=True) is not None:
                await asyncio.sleep(self.poll_interval)

    async def switch_backend(self, backend_id, target_id, run_id):
        target = await self._database(target_id)
        self._target(target, "bear-" + _tag(run_id))
        info = await self._connections(target_id)
        target_url = _tls_url(info.get("internalConnectionString", ""), True)
        return await self.switch_to_url(backend_id, target_id, target_url, run_id)


async def _diagnose():
    from dotenv import load_dotenv

    load_dotenv()
    async with httpx.AsyncClient() as client:
        return await RenderBackend(client).diagnose()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect the type and provider of a Render backend database without printing credentials."
    )
    parser.add_argument("action", choices=["diagnose"])
    parser.parse_args()
    try:
        print(json.dumps(asyncio.run(_diagnose()), ensure_ascii=False))
    except RenderError as error:
        parser.exit(1, str(error) + "\n")
