"""Durable workspace OCR gate. Unknown process state never proves cleanup."""
from __future__ import annotations
import time
import uuid
import json
from contextlib import contextmanager
from litworkbench.errors import LitError
from litworkbench.jobs import JobStore, ClaimLost, _in_seconds
from litworkbench.ocr_process import _load_psutil, _probe_process, _is_absent_exception
from litworkbench.store import utcnow

class GateBusy(LitError):
    pass

class GateUnavailable(LitError):
    pass

def _registered_child_alive(pid, create_time):
    if pid is None or create_time is None:
        return False
    return _probe_process(_load_psutil(), pid, create_time) != "absent"

def _reap_registered_child(pid, create_time):
    if pid is None:
        return True
    module = _load_psutil()
    state = _probe_process(module, pid, create_time)
    if state == "absent":
        return True
    if state != "alive":
        return False
    try:
        root = module.Process(pid)
        entries = [(p.pid, float(p.create_time())) for p in root.children(recursive=True)]
        entries.append((pid, create_time))
    except Exception:
        return False
    for child_pid, child_time in entries:
        if _probe_process(module, child_pid, child_time) == "alive":
            try:
                module.Process(child_pid).kill()
            except Exception:
                pass
    deadline = time.monotonic() + 10
    while True:
        states = [_probe_process(module, p, ct) for p, ct in entries]
        if all(s == "absent" for s in states):
            return True
        if "unknown" in states or time.monotonic() >= deadline:
            return False
        time.sleep(.05)

class OcrGate:
    def __init__(self, workspace):
        self.workspace = workspace
        self.jobs = JobStore(workspace)

    @contextmanager
    def _write(self):
        with self.workspace.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO ocr_gate (id,state,updated_at) VALUES (1,'free',?)", (utcnow(),))
            yield conn

    def _claim(self, conn, job_id, owner, generation):
        self.jobs._require_claim(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(), job_id, owner, generation)

    def acquire(self, *, job_id, owner, generation, lease_seconds=300,
                wait_timeout=None, poll_interval=.1, renew_own_lease=None):
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        while True:
            recovery = None
            with self._write() as conn:
                self._claim(conn, job_id, owner, generation)
                row = conn.execute("SELECT * FROM ocr_gate WHERE id=1").fetchone()
                if row["state"] == "free":
                    return self._grant(conn, job_id, owner, generation, lease_seconds)
                expired = not row["lease_expires_at"] or row["lease_expires_at"] <= utcnow()
                if expired:
                    # Revoke the exact old execution BEFORE reaping; never revoke a new generation.
                    if row["job_id"] and (row["job_id"], row["owner"], row["generation"]) != (job_id, owner, generation):
                        changed = conn.execute(
                            "UPDATE jobs SET state='queued', owner=NULL, lease_expires_at=NULL, pause_requested=0, updated_at=? "
                            "WHERE id=? AND owner=? AND claim_generation=? AND state IN ('running','preparing','pausing')",
                            (utcnow(), row["job_id"], row["owner"], row["generation"])).rowcount
                        if changed:
                            conn.execute("UPDATE ocr_candidates SET state='failed', error_kind='interrupted', "
                                         "error_message='识别闸门租约已失效', updated_at=? WHERE job_id=? AND owner=? AND generation=? "
                                         "AND state IN ('queued','running')", (utcnow(), row["job_id"], row["owner"], row["generation"]))
                    recovery = uuid.uuid4().hex
                    child = (row["child_pid"], row["child_create_time"])
                    children = json.loads(row["child_tree_json"])
                    conn.execute("UPDATE ocr_gate SET state='recovering',token=NULL,recovery_token=?, "
                                 "recovery_state='reaping',lease_expires_at=?,updated_at=? WHERE id=1",
                                 (recovery, _in_seconds(30), utcnow()))
            if recovery:
                try:
                    reaped = not _registered_child_alive(*child) or _reap_registered_child(*child)
                    for saved in children:
                        # Retain identities even when a Windows launcher already exited.
                        reaped = _reap_registered_child(saved[0], saved[1]) and reaped
                except Exception:
                    reaped = False
                with self._write() as conn:
                    row = conn.execute("SELECT * FROM ocr_gate WHERE id=1").fetchone()
                    if row["recovery_token"] != recovery:
                        raise GateUnavailable("识别闸门恢复权已转移")
                    if reaped:
                        self._clear(conn)
                    else:
                        conn.execute("UPDATE ocr_gate SET recovery_state='unavailable',lease_expires_at=NULL,updated_at=? WHERE id=1", (utcnow(),))
                if not reaped:
                    raise GateUnavailable("无法确认上一个识别进程树已回收，闸门保持不可用。")
                continue
            if deadline is not None and time.monotonic() >= deadline:
                raise GateBusy("工作区正在识别其他页，请稍候。")
            if renew_own_lease:
                renew_own_lease()
            time.sleep(max(0, poll_interval))

    def _grant(self, conn, job_id, owner, generation, seconds):
        self._claim(conn, job_id, owner, generation)
        token = uuid.uuid4().hex
        conn.execute("UPDATE ocr_gate SET state='held',job_id=?,owner=?,generation=?,token=?,lease_expires_at=?, "
                     "child_pid=NULL,child_create_time=NULL,child_tree_json='[]',recovery_token=NULL,recovery_state=NULL,updated_at=? WHERE id=1",
                     (job_id, owner, generation, token, _in_seconds(seconds), utcnow()))
        return {"token": token, "job_id": job_id, "owner": owner, "generation": generation}

    def _held(self, conn, token):
        row = conn.execute("SELECT * FROM ocr_gate WHERE id=1").fetchone()
        if row["state"] != "held" or not token or row["token"] != token:
            raise ClaimLost("识别闸门令牌已失效")
        self._claim(conn, row["job_id"], row["owner"], row["generation"])
        return row

    def register_child(self, token, *, pid, create_time):
        with self._write() as conn:
            self._held(conn, token)
            conn.execute("UPDATE ocr_gate SET child_pid=?,child_create_time=?,updated_at=? WHERE id=1", (int(pid), float(create_time), utcnow()))
            self._remember_tree(conn)

    def _remember_tree(self, conn):
        row = conn.execute("SELECT * FROM ocr_gate WHERE id=1").fetchone()
        module = _load_psutil()
        if row["child_pid"] is None:
            return
        state = _probe_process(module, row["child_pid"], row["child_create_time"])
        if state == "unknown":
            raise GateUnavailable("无法核实识别进程树身份")
        if state == "absent":
            return
        entries = {int(pid): ct for pid, ct in json.loads(row["child_tree_json"])}
        try:
            for child in module.Process(row["child_pid"]).children(recursive=True):
                try:
                    entries[child.pid] = float(child.create_time())
                except Exception as exc:
                    if not _is_absent_exception(module, exc):
                        raise
        except Exception as exc:
            if _probe_process(module, row["child_pid"], row["child_create_time"]) != "absent":
                raise GateUnavailable("无法登记识别后代进程身份") from exc
        conn.execute("UPDATE ocr_gate SET child_tree_json=? WHERE id=1", (json.dumps(list(entries.items())),))

    def renew(self, token, *, lease_seconds=300):
        with self._write() as conn:
            row = self._held(conn, token)
            self._remember_tree(conn)
            self.jobs.renew_lease(row["job_id"], owner=row["owner"], generation=row["generation"], lease_seconds=lease_seconds, connection=conn)
            conn.execute("UPDATE ocr_gate SET lease_expires_at=?,updated_at=? WHERE id=1", (_in_seconds(lease_seconds), utcnow()))

    @staticmethod
    def _clear(conn):
        conn.execute("UPDATE ocr_gate SET state='free',job_id=NULL,owner=NULL,generation=NULL,token=NULL,lease_expires_at=NULL, "
                     "child_pid=NULL,child_create_time=NULL,child_tree_json='[]',recovery_token=NULL,recovery_state=NULL,updated_at=? WHERE id=1", (utcnow(),))

    def release(self, token, *, reaped):
        # A lost job claim may still release its own confirmed-dead process by exact gate token.
        with self._write() as conn:
            row = conn.execute("SELECT * FROM ocr_gate WHERE id=1").fetchone()
            if not token or row["token"] != token:
                raise ClaimLost("识别闸门已转交，不能释放他人的闸门")
            if reaped:
                self._clear(conn)
            else:
                conn.execute("UPDATE ocr_gate SET state='recovering',token=NULL,lease_expires_at=NULL,recovery_state='unavailable',updated_at=? WHERE id=1", (utcnow(),))

    def inspect(self):
        with self._write() as conn:
            return dict(conn.execute("SELECT * FROM ocr_gate WHERE id=1").fetchone())
