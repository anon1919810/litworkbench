"""Real process lifecycle plus upgrade and fencing regressions, no OCR model calls."""
import subprocess
import sys
import time

import psutil
import pytest
from types import SimpleNamespace

from litworkbench import migrations
from litworkbench.store import Workspace
from litworkbench.jobs import JobStore, ClaimLost
from litworkbench.ocr_gate import OcrGate
from litworkbench.web.app import WorkerProcess


@pytest.mark.parametrize("error_type,should_fail", [(psutil.NoSuchProcess, False), (psutil.AccessDenied, True)])
def test_disappearing_descendant_is_distinct_from_unknown_identity(workspace, monkeypatch, error_type, should_fail):
    from litworkbench import ocr_gate as mod
    jobs = JobStore(workspace)
    job = jobs.enqueue_import("s", "test.pdf")["id"]
    claim = jobs.claim_next("owner")
    gate = OcrGate(workspace)
    token = gate.acquire(job_id=job, owner="owner", generation=claim["claim_generation"])["token"]
    class Child:
        pid = 12345
        def create_time(self):
            raise error_type(self.pid)
    module = SimpleNamespace(Process=lambda pid: SimpleNamespace(children=lambda recursive: [Child()]),
                             NoSuchProcess=psutil.NoSuchProcess, ZombieProcess=psutil.ZombieProcess)
    monkeypatch.setattr(mod, "_load_psutil", lambda: module)
    monkeypatch.setattr(mod, "_probe_process", lambda *args: "alive")
    if should_fail:
        with pytest.raises(mod.GateUnavailable):
            gate.register_child(token, pid=9999, create_time=1.0)
    else:
        gate.register_child(token, pid=9999, create_time=1.0)


def test_v7_upgrade_creates_gate_without_rewriting_content(tmp_path, monkeypatch):
    full = migrations.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", tuple(m for m in full if m.version <= 7))
        patch.setattr(migrations, "SCHEMA_VERSION", 7)
        ws = Workspace(tmp_path / "library")
        ws.initialize()
    result = migrations.ensure_schema(ws)
    assert result["from"] == 7 and result["to"] == 8
    assert OcrGate(ws).inspect()["state"] == "free"
    assert migrations.ensure_schema(ws)["applied"] == []


def test_field_and_staging_writes_are_fenced(workspace):
    store = JobStore(workspace)
    store.add_staging("s", "test.pdf", "staging/test.pdf", "x", 1)
    job = store.enqueue_import("s", "test.pdf")["id"]
    store.claim_next("owner")
    with pytest.raises(ClaimLost):
        store.update_fields(job, owner="old", generation=1, filename="bad.pdf")
    with pytest.raises(ClaimLost):
        store.drop_staging("s", job_id=job, owner="old", generation=1)
    assert store.get(job)["filename"] == "test.pdf"
    assert store.staging("s")["status"] == "pending"


def test_force_stop_reaps_owned_descendant(workspace, tmp_path):
    pid_file = tmp_path / "child.txt"
    code = ("import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)")
    process = subprocess.Popen([sys.executable, "-c", code, str(pid_file)])
    handle = WorkerProcess(workspace.root)
    handle.process = process
    handle.create_time = psutil.Process(process.pid).create_time()
    child = None
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        child = psutil.Process(int(pid_file.read_text()))
        handle.stop(timeout=0)
        assert process.poll() is not None
        assert not child.is_running(), "强制停止遗留了自有 OCR 后代进程"
    finally:
        if child and child.is_running():
            child.kill()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def test_gate_recovery_reaps_recorded_children_when_launcher_is_gone(workspace, tmp_path):
    store = JobStore(workspace)
    first = store.enqueue_import("s1", "one.pdf")["id"]
    second = store.enqueue_import("s2", "two.pdf")["id"]
    a = store.claim_next("a")
    b = store.claim_next("b")
    gate = OcrGate(workspace)
    ticket = gate.acquire(job_id=first, owner="a", generation=a["claim_generation"], lease_seconds=0)
    pid_file = tmp_path / "orphan.txt"
    code = ("import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)")
    root = subprocess.Popen([sys.executable, "-c", code, str(pid_file)])
    child = None
    saved = []
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        child = psutil.Process(int(pid_file.read_text()))
        saved = psutil.Process(root.pid).children(recursive=True)
        gate.register_child(ticket["token"], pid=root.pid, create_time=psutil.Process(root.pid).create_time())
        root.kill()
        root.wait(timeout=10)
        new = OcrGate(workspace).acquire(job_id=second, owner="b", generation=b["claim_generation"], wait_timeout=0)
        assert not child.is_running(), "旧启动器消失后不能遗留登记过的识别后代"
        gate.release(new["token"], reaped=True)
    finally:
        for proc in saved + ([child] if child else []):
            try:
                if proc.is_running():
                    proc.kill()
            except psutil.NoSuchProcess:
                pass
        if root.poll() is None:
            root.kill()
        root.wait(timeout=10)
