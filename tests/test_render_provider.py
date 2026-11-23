"""Render 公共 API 合同，仅向内存中的 MockTransport 发请求。"""

import asyncio
import json

import httpx
import pytest

from maintenance.render_provider import RenderError, RenderProvider

SOURCE = "dpg-source"
TARGET = "dpg-target"
SERVICE = "srv-backend"
OWNER = "tea-workspace"
SOURCE_URI = "postgresql://student:fake-password@dpg-source/ledger"


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for key, value in {
        "RENDER_SERVICE_ID": SERVICE,
        "RENDER_OWNER_ID": OWNER,
        "TARGET_DB_PLAN": "free",
        "TARGET_DB_VERSION": "16",
        "TARGET_DB_REGION": "frankfurt",
        "MAINTENANCE_STATE_DIR": str(tmp_path),
        "DB_PROVIDER_API_KEY": "fake-key",
        "SOURCE_DB_ID": SOURCE,
    }.items():
        monkeypatch.setenv(key, value)


def record(dbid=SOURCE, **fields):
    return {
        "id": dbid,
        "name": "source",
        "owner": {"id": OWNER},
        "plan": "free",
        "region": "frankfurt",
        "version": "16",
        "role": "primary",
        "ipAllowList": [{"cidrBlock": "192.0.2.1/32", "description": "controller"}],
        "readReplicas": [],
        "status": "available",
        **fields,
    }


def connection(dbid):
    return {
        "externalConnectionString": f"postgresql://student:fake-password@{dbid}.example/ledger",
        "internalConnectionString": f"postgresql://student:fake-password@{dbid}/ledger",
    }


def run(handler, action):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = RenderProvider(client, poll_interval=0.001, wait_timeout=0.2)
            return await action(provider)

    return asyncio.run(check())


def test_status_maps_provider_and_requires_tls(settings):
    def handler(r):
        assert r.url.host == "api.render.com"
        assert r.headers["Authorization"] == "Bearer fake-key"
        if r.url.path.endswith("connection-info"):
            return httpx.Response(200, json=connection(SOURCE))
        return httpx.Response(
            200,
            json=record(
                maintenance={"state": "scheduled"}, expiresAt="2027-02-01T00:00:00Z"
            ),
        )

    async def action(p):
        status = await p.status(SOURCE)
        assert status["planned_maintenance"] and not status["completed_maintenance"]
        assert status["expires_at"] == "2027-02-01T00:00:00Z"
        assert ".example/ledger?sslmode=require" in await p.db_url(SOURCE)
        assert "@dpg-source/ledger?sslmode=require" in await p.db_url(
            SOURCE, internal=True
        )

    run(handler, action)


def test_preflight_and_single_create_preserves_source_network(settings):
    posts = []

    def handler(r):
        path = r.url.path
        if path == f"/v1/services/{SERVICE}":
            return httpx.Response(
                200,
                json={
                    "id": SERVICE,
                    "ownerId": OWNER,
                    "type": "web_service",
                    "serviceDetails": {"region": "frankfurt"},
                },
            )
        if path.endswith("env-vars/DATABASE_URL"):
            return httpx.Response(
                200, json={"key": "DATABASE_URL", "value": SOURCE_URI}
            )
        if path.endswith("connection-info"):
            return httpx.Response(200, json=connection(SOURCE))
        if path == "/v1/postgres" and r.method == "POST":
            body = json.loads(r.content)
            posts.append(body)
            assert body["ipAllowList"][0]["cidrBlock"] == "192.0.2.1/32"
            assert body["ownerId"] == OWNER
            assert body["version"] == "16"
            return httpx.Response(201, json=record(TARGET, name=body["name"]))
        if path == "/v1/postgres":
            if r.url.params.get("name") or r.url.params.get("cursor"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"cursor": "a", "postgres": record()}])
        return httpx.Response(200, json=record())

    async def action(p):
        await p.preflight(SOURCE, SERVICE, OWNER, "free", "frankfurt", "16", 1)
        assert await p.create_target("run-one", "free", "frankfurt", "16") == TARGET

    run(handler, action)
    assert len(posts) == 1


def test_pagination_identity_and_spec(settings):
    def handler(r):
        cursor = r.url.params.get("cursor")
        row = record("dpg-another", name="different")
        if cursor == "first":
            row = record(TARGET, name=r.url.params["name"])
        if cursor == "second":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200, json=[{"cursor": "second" if cursor else "first", "postgres": row}]
        )

    assert run(handler, lambda p: p.find_target("rerun")) == TARGET


def test_refused_delete_is_sanitized_and_never_means_absent(settings):
    def handler(r):
        return httpx.Response(403, json={"message": SOURCE_URI + " fake-key"})

    with pytest.raises(RenderError) as err:
        run(handler, lambda p: p.delete_source(SOURCE))
    assert "403" in str(err.value)
    assert "fake-password" not in str(err.value) and "fake-key" not in str(err.value)


def test_create_timeout_reclaims_target_without_reposting(settings):
    posts = []

    def handler(r):
        if r.method == "POST":
            posts.append(json.loads(r.content))
            raise httpx.ReadTimeout("remote message contains fake-password", request=r)
        name = r.url.params.get("name")
        if posts and name and not r.url.params.get("cursor"):
            return httpx.Response(
                200, json=[{"cursor": "only", "postgres": record(TARGET, name=name)}]
            )
        return httpx.Response(200, json=[])

    async def action(p):
        p._save(p._snapshot("source"), {"source": SOURCE, "ipAllowList": []})
        with pytest.raises(RenderError) as err:
            await p.create_target("lost", "free", "frankfurt", "16")
        assert "fake-password" not in str(err.value)

    run(handler, action)
    assert (
        run(handler, lambda p: p.create_target("lost", "free", "frankfurt", "16"))
        == TARGET
    )
    assert len(posts) == 1


def test_ambiguous_create_stops_if_marker_not_yet_visible(settings):
    posts = []

    def handler(r):
        if r.method == "POST":
            posts.append(r)
            return httpx.Response(503, json={"message": "private contents"})
        return httpx.Response(200, json=[])

    async def action(p):
        p._save(p._snapshot("source"), {"source": SOURCE, "ipAllowList": []})
        with pytest.raises(RenderError, match="503"):
            await p.create_target("uncertain", "free", "frankfurt", "16")
        with pytest.raises(RenderError, match="previously issued"):
            await p.create_target("uncertain", "free", "frankfurt", "16")

    run(handler, action)
    assert len(posts) == 1


def test_explicitly_rate_limited_create_can_resume_with_same_marker(settings):
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            return httpx.Response(429)
        return httpx.Response(201, json=record(TARGET, name=posts[-1]["name"]))

    async def action(provider):
        provider._save(provider._snapshot("source"), {"source": SOURCE, "ipAllowList": []})
        with pytest.raises(RenderError, match="429"):
            await provider.create_target("rate-limited", "free", "frankfurt", "16")
        assert await provider.create_target("rate-limited", "free", "frankfurt", "16") == TARGET

    run(handler, action)
    assert len(posts) == 2 and posts[0] == posts[1]


@pytest.mark.parametrize("change", ["owner", "plan", "duplicate"])
def test_find_rejects_incorrect_or_ambiguous_target(settings, change):
    def handler(r):
        if r.url.params.get("cursor"):
            return httpx.Response(200, json=[])
        target = record(TARGET, name=r.url.params["name"])
        if change == "owner":
            target["owner"] = {"id": "tea-elsewhere"}
        if change == "plan":
            target["plan"] = "pro"
        rows = [{"cursor": "a", "postgres": target}]
        if change == "duplicate":
            rows.append({"cursor": "b", "postgres": dict(target, id="dpg-other")})
        return httpx.Response(200, json=rows)

    with pytest.raises(RenderError):
        run(handler, lambda p: p.find_target("ambiguous"))


def test_delete_checks_until_absent_and_can_resume(settings):
    verbs = []

    def handler(r):
        verbs.append(r.method)
        if r.method == "DELETE":
            return httpx.Response(204)
        if verbs.count("GET") <= 2:
            return httpx.Response(200, json=record())
        return httpx.Response(404)

    async def action(p):
        p._save(p._snapshot("source"), {"source": SOURCE, "ipAllowList": []})
        await p.delete_source(SOURCE)
        await p.delete_source(SOURCE)

    run(handler, action)
    assert verbs == ["GET", "DELETE", "GET", "GET", "GET"]


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("rate_limited", [False, True])
def test_switch_changes_only_database_url_and_confirms_new_deploy(
    settings, tmp_path, queued, rate_limited
):
    import hashlib

    tag = "bear-" + hashlib.sha256(b"switch").hexdigest()[:24]
    writes = []
    url = SOURCE_URI

    def handler(r):
        nonlocal url
        path = r.url.path
        if r.method == "PUT":
            assert path == f"/v1/services/{SERVICE}/env-vars/DATABASE_URL"
            url = json.loads(r.content)["value"]
            assert "@dpg-target/ledger?sslmode=require" in url
            writes.append("PUT")
            return httpx.Response(200, json={"key": "DATABASE_URL", "value": url})
        if r.method == "POST":
            assert path.endswith("/deploys")
            assert json.loads(r.content) == {"deployMode": "deploy_only"}
            if rate_limited and "REJECTED" not in writes:
                writes.append("REJECTED")
                return httpx.Response(429)
            writes.append("POST")
            return (
                httpx.Response(202)
                if queued
                else httpx.Response(201, json={"id": "dep-new"})
            )
        if path.endswith("/env-vars/DATABASE_URL"):
            return httpx.Response(200, json={"key": "DATABASE_URL", "value": url})
        if path.endswith("/deploys"):
            dep = "dep-new" if "POST" in writes else "dep-old"
            return httpx.Response(
                200, json=[{"cursor": "end", "deploy": {"id": dep, "status": "live"}}]
            )
        if path.endswith("/deploys/dep-new"):
            return httpx.Response(200, json={"id": "dep-new", "status": "live"})
        if path.endswith("/connection-info"):
            return httpx.Response(200, json=connection(TARGET))
        return httpx.Response(200, json=record(TARGET, name=tag))

    async def switch(p):
        locator = repr((SOURCE, "/ledger", 5432, "student")).encode()
        p._save(
            p._snapshot("backend-source"),
            {
                "source": SOURCE,
                "sourceLocators": [hashlib.sha256(locator).hexdigest()[:24]],
            },
        )
        return await p.switch_backend(SERVICE, TARGET, "switch")

    if rate_limited:
        with pytest.raises(RenderError, match="429"):
            run(handler, switch)
    for _ in range(2):
        assert run(handler, switch) == "dep-new"
    assert writes == (["PUT", "REJECTED", "POST"] if rate_limited else ["PUT", "POST"])
    assert all(
        "fake-password" not in path.read_text() for path in tmp_path.glob("*.json")
    )


def test_wait_ready_is_bounded_and_retries_transient_reads(settings):
    responses = iter([503, 200, 200])

    def handler(r):
        status = next(responses)
        if status == 503:
            return httpx.Response(503)
        row = record(
            TARGET, status="creating" if not hasattr(handler, "seen") else "available"
        )
        handler.seen = True
        return httpx.Response(200, json=row)

    run(handler, lambda p: p.wait_ready(TARGET))

    def stuck(r):
        return httpx.Response(200, json=record(TARGET, status="creating"))

    with pytest.raises(TimeoutError):
        run(stuck, lambda p: p.wait_ready(TARGET))


def test_readonly_check_needs_no_target_spec_and_default_state_dir(
    settings, monkeypatch
):
    from pathlib import Path

    for name in (
        "TARGET_DB_REGION",
        "TARGET_DB_PLAN",
        "TARGET_DB_VERSION",
        "RENDER_SERVICE_ID",
        "MAINTENANCE_STATE_DIR",
    ):
        monkeypatch.delenv(name)

    def handler(request):
        return httpx.Response(200, json=record())

    async def action(p):
        assert p.state_dir == (Path.home() / ".little-bear-maintenance").resolve()
        await p.status(SOURCE)

    run(handler, action)


def test_preflight_rejects_cross_major_version_before_mutation(settings):
    def handler(r):
        assert r.method == "GET"
        assert r.url.path == "/v1/postgres/dpg-source"
        return httpx.Response(200, json=record(version="15"))

    with pytest.raises(RenderError, match="version"):
        run(
            handler,
            lambda p: p.preflight(SOURCE, SERVICE, OWNER, "free", "frankfurt", "16", 1),
        )


def test_unknown_plan_cannot_pass_preflight(settings, monkeypatch):
    monkeypatch.setenv("TARGET_DB_PLAN", "fre_typo")

    def handler(r):
        pytest.fail("Unsupported plan should not perform a provider request.")

    with pytest.raises(RenderError, match="specification"):
        run(
            handler,
            lambda p: p.preflight(
                SOURCE, SERVICE, OWNER, "fre_typo", "frankfurt", "16", 1
            ),
        )


@pytest.mark.parametrize("case", ["backend_owner", "wrong_source_url", "free_slot"])
def test_preflight_refuses_mismatched_backend_or_capacity(settings, case):
    def handler(r):
        assert r.method == "GET"
        path = r.url.path
        if path == f"/v1/services/{SERVICE}":
            return httpx.Response(
                200,
                json={
                    "id": SERVICE,
                    "type": "web_service",
                    "serviceDetails": {"region": "frankfurt"},
                    "ownerId": "tea-other" if case == "backend_owner" else OWNER,
                },
            )
        if path.endswith("/connection-info"):
            return httpx.Response(200, json=connection(SOURCE))
        if path.endswith("/env-vars/DATABASE_URL"):
            value = (
                SOURCE_URI.replace(SOURCE, "dpg-other")
                if case == "wrong_source_url"
                else SOURCE_URI
            )
            return httpx.Response(200, json={"key": "DATABASE_URL", "value": value})
        if path == "/v1/postgres":
            page = (
                []
                if "cursor" in r.url.params
                else [{"cursor": "last", "postgres": record()}]
            )
            return httpx.Response(200, json=page)
        return httpx.Response(200, json=record())

    with pytest.raises(RenderError):
        run(
            handler,
            lambda p: p.preflight(SOURCE, SERVICE, OWNER, "free", "frankfurt", "16", 2),
        )


def test_switch_stops_if_backend_url_was_redirected_since_preflight(settings):
    import hashlib

    tag = "bear-" + hashlib.sha256(b"redirected").hexdigest()[:24]

    def handler(r):
        assert r.method == "GET"
        if r.url.path.endswith("/deploys"):
            return httpx.Response(
                200,
                json=[{"cursor": "end", "deploy": {"id": "dep-old", "status": "live"}}],
            )
        if r.url.path.endswith("/env-vars/DATABASE_URL"):
            return httpx.Response(
                200,
                json={
                    "key": "DATABASE_URL",
                    "value": SOURCE_URI.replace(SOURCE, "another"),
                },
            )
        if r.url.path.endswith("/connection-info"):
            return httpx.Response(200, json=connection(TARGET))
        return httpx.Response(200, json=record(TARGET, name=tag))

    async def switch(p):
        locator = repr((SOURCE, "/ledger", 5432, "student")).encode()
        p._save(
            p._snapshot("backend-source"),
            {
                "source": SOURCE,
                "sourceLocators": [hashlib.sha256(locator).hexdigest()[:24]],
            },
        )
        await p.switch_backend(SERVICE, TARGET, "redirected")

    with pytest.raises(RenderError, match="changed after preflight"):
        run(handler, switch)


def test_diagnose_identifies_only_type_and_provider_without_database_auth(
    settings, monkeypatch
):
    from maintenance.render_provider import RenderBackend

    monkeypatch.setenv("RENDER_API_KEY", "render-only")
    monkeypatch.setenv("DB_PROVIDER_API_KEY", "external-db-secret")
    monkeypatch.delenv("RENDER_OWNER_ID")

    def handler(r):
        assert r.method == "GET"
        assert r.url.path == f"/v1/services/{SERVICE}/env-vars/DATABASE_URL"
        assert r.headers["Authorization"] == "Bearer render-only"
        return httpx.Response(
            200,
            json={
                "key": "DATABASE_URL",
                "value": "postgresql://secret:userpass@ep-private.a.neon.tech/privatedb?sslmode=require",
            },
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await RenderBackend(c).diagnose()

    assert asyncio.run(check()) == {"database_type": "PostgreSQL", "provider": "Neon"}


@pytest.mark.parametrize(
    ("dsn", "database_type", "provider"),
    [
        ("sqlite:///private-path.db", "SQLite", "local-file"),
        ("postgresql://x:y@db.private.supabase.co/postgres", "PostgreSQL", "Supabase"),
        (
            "postgresql://x:y@aws-1.pooler.supabase.com/postgres",
            "PostgreSQL",
            "Supabase",
        ),
        ("postgresql://x:y@neon.tech.unrelated.example/other", "PostgreSQL", "unknown"),
        (
            "postgresql://x:y@dpg-example.oregon-postgres.render.com/db",
            "PostgreSQL",
            "Render",
        ),
        ("mysql://x:y@example.test/another", "MySQL", "unknown"),
        ("suspicious+private://x:pass@another/x", "unknown", "unknown"),
        ("postgresql://x:y@[invalid", "unknown", "unknown"),
    ],
)
def test_provider_detection_does_not_expose_unrecognized_hostname(
    dsn, database_type, provider
):
    from maintenance.render_provider import classify_database

    assert classify_database(dsn) == {
        "database_type": database_type,
        "provider": provider,
    }


def test_external_database_uses_only_service_api_for_preflight_and_deploy(
    settings, monkeypatch
):
    from maintenance.render_provider import RenderBackend

    monkeypatch.setenv("RENDER_API_KEY", "render-only")
    source = "postgresql://user:example@source.neon.tech/ledger"
    target = "postgresql://user:example@target.neon.tech/ledger?sslmode=verify-full"
    current = source
    writes = []

    def handler(r):
        nonlocal current
        path = r.url.path
        assert path.startswith(f"/v1/services/{SERVICE}")
        assert r.headers["Authorization"] == "Bearer render-only"
        if r.method == "PUT":
            current = json.loads(r.content)["value"]
            writes.append("config")
            assert current == target
            return httpx.Response(200, json={"key": "DATABASE_URL", "value": current})
        if r.method == "POST":
            writes.append("deploy")
            return httpx.Response(201, json={"id": "dep-new"})
        if path.endswith("/env-vars/DATABASE_URL"):
            return httpx.Response(200, json={"key": "DATABASE_URL", "value": current})
        if path.endswith("/deploys/dep-new"):
            return httpx.Response(200, json={"id": "dep-new", "status": "live"})
        if path.endswith("/deploys"):
            return httpx.Response(
                200,
                json=[{"cursor": "end", "deploy": {"id": "dep-old", "status": "live"}}],
            )
        return httpx.Response(
            200, json={"id": SERVICE, "ownerId": OWNER, "type": "web_service"}
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            backend = RenderBackend(c)
            await backend.preflight_connection(SERVICE, OWNER, [source])
            assert (
                await backend.switch_to_url(
                    SERVICE, "external-target-id", target, "outside"
                )
                == "dep-new"
            )

    asyncio.run(check())
    assert writes == ["config", "deploy"]


def test_diagnosis_missing_direct_env_does_not_assume_sqlite(settings, monkeypatch):
    from maintenance.render_provider import RenderBackend

    monkeypatch.setenv("RENDER_API_KEY", "render-only")

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ) as c:
            return await RenderBackend(c).diagnose()

    assert asyncio.run(check()) == {
        "database_type": "not-found-on-service",
        "provider": "unknown",
    }


def test_external_database_secret_is_never_used_for_render_diagnosis(
    settings, monkeypatch
):
    from maintenance.render_provider import RenderBackend

    monkeypatch.delenv("RENDER_API_KEY", raising=False)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: pytest.fail("unexpected IO"))
        ) as c:
            await RenderBackend(c).diagnose()

    with pytest.raises(RenderError, match="Render API key"):
        asyncio.run(check())
