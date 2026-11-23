"""维护编排测试的离线替身，标识符只对应临时测试资源。"""

import json

RUN_ID = "12d3a7ba-1758-41a2-bf0f-0765f64350a3"


class Cloud:
    def __init__(self):
        self.events = []
        self.target = None
        self.interrupt_creation = False
        self.timeout_creation = False

    async def status(self, source):
        return {"planned_maintenance": True}

    async def preflight(self, *args):
        self.events.append("preflight")

    async def db_url(self, instance):
        return instance

    async def find_target(self, run):
        return self.target

    async def create_target(self, run, *spec):
        self.events.append("create")
        self.target = "target"
        if self.interrupt_creation:
            raise KeyboardInterrupt()
        if self.timeout_creation:
            raise TimeoutError()
        return self.target

    async def wait_ready(self, target):
        self.events.append("ready")

    async def delete_source(self, source):
        self.events.append("delete:" + source)

    async def switch_backend(self, service, target, run):
        self.events.append("switch")
        return "dep-simulated"


class Backend:
    def __init__(self):
        self.fail_verify = False
        self.probes = 0

    async def preflight(self):
        pass

    async def verify(self, records, reference, summary, pending_probe=None):
        if self.fail_verify:
            raise RuntimeError("synthetic read mismatch")

    async def verify_probe(self, probe):
        self.probes += 1


class LocalData:
    def __init__(self):
        self.events = []
        self.blocked = {}
        self.probe_verified = False

    def preflight(self, source, scratch, spec):
        pass

    def lock(self, instance, run, initialize=False):
        self.blocked[instance] = run

    def backup(self, url, source, run, root):
        path = root / "backup.json"
        path.write_text(json.dumps({
            "source_instance_id": source, "records": [], "expected_summary": [],
            "reference_time": "2026-01-01T00:00:00+00:00",
        }), encoding="utf-8")
        return path

    def restore(self, backup, instance, run):
        self.events.append("restore:" + instance)
        return {"verified": True, "restored_count": 0, "summary_report": []}

    def pending_probe(self, instance, run):
        return None

    def stage_probe(self, instance, run):
        return {"id": 1, "verified": self.probe_verified}

    def finish_probe(self, instance, run, probe):
        self.probe_verified = True

    def unlock(self, instance, run):
        self.blocked[instance] = None
