"""Task B1：候选任务执行、执行代次栅栏与工作区 OCR 闸门的 RED 测试。

只使用 ``demo`` 生成的**合成**夹具与真实临时 SQLite；注入式接缝代替真实模型，
只有既有监督器已经覆盖的短子进程行为才依赖真实进程（``tests/test_ocr_process.py``）。
凡是本轮**尚不存在**的新能力，测试都会失败——这是刻意保留的 RED，不改任何产品代码。

冻结的新接口（本轮由测试固定，供 GREEN 实现）：

``JobStore.execution(job_id, owner, generation)``
    上下文管理器：``BEGIN IMMEDIATE`` 后校验「任务存在 + 活动状态 + owner 匹配 +
    generation 匹配 + 租约未过期」，然后 ``yield`` 这个连接；所有权威执行写入
    必须走同一个连接（不是「先检查再另开事务」）。任一校验失败抛 ``ClaimLost``。

``ClaimLost``（``litworkbench.jobs``，``LitError`` 子类）
    执行权丢失：过期代次/他人执行者/任务已终结/租约过期。

``JobStore`` 执行写入 ``renew_lease / set_stage / plan_units / finish_unit /
    refresh_counters / finish``
    新增可选 ``connection=None, owner=None, generation=None``；提供 owner 与
    generation 时按当前租约校验，过期/不匹配抛 ``ClaimLost``（不写入）；
    不提供时保留旧的管理/CLI 语义。

``Worker(workspace, *, owner_id=None, lease_seconds=300.0, run_page=None,
    ocr_page=None)``
    * ``run_page(workspace, unit, *, profile_id, checkpoint=None) -> dict``
      候选页识别回调（unit 是单元视图字典），默认 ``ocr_process.run_page``；
    * ``ocr_page(workspace, path, index, text, scale) -> dict``
      既有 import/reprocess 的 OCR 回调（与 ``ingest._ocr_page`` 同形），
      默认是受控页进程，经 ``open_pdf_pages(..., ocr_page=)`` /
      ``force_page_block(..., ocr_page=)`` 透传；``PdfPages.block_for`` 的
      签名保持 ``block_for(self, index, *, ocr_mode=None)`` 不变。

``litworkbench.ocr_gate``（新模块）
    ``class OcrGate(workspace)``，持久化单例闸门（SQLite）：
    * ``acquire(*, job_id, owner, generation, lease_seconds=..., wait_timeout=None,
      poll_interval=0.1, renew_own_lease=None) -> ticket``：校验该 job 的
      owner+generation 是当前有效领取；闸门忙且旧持有者仍存活时抛 ``GateBusy``；
      等待期间每个轮询调用 ``renew_own_lease()``（续租自己的任务租约）；
    * ``register_child(token, *, pid, create_time)``：只允许当前 token 持有者
      登记自己子进程的 pid+创建时间（GO 之前）；
    * ``release(token, *, reaped)``：只释放仍属当前 token 的闸门；
    * ``inspect() -> dict``：``{state: free|held|recovering, job_id, owner,
      generation, child_pid, child_create_time, lease_expires_at}``；
    * 窄注入接缝 ``_registered_child_alive(pid, create_time) -> bool`` 与
      ``_reap_registered_child(pid, create_time) -> bool``（返回是否确认回收）。
    过期且旧子进程仍活时必须先按 pid+创建时间确认回收再转交；无法确认回收则
    闸门保持不可用（``GateUnavailable``）；PID 已回收（身份不符）绝不被终止。
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from litworkbench import jobs as jobs_mod
from litworkbench import ocr, ocr_process, search_index
from litworkbench.errors import LitError, ValidationError
from litworkbench.ingest import PdfPages, force_page_block, import_document, open_pdf_pages
from litworkbench.jobs import JobStore
from litworkbench.ocr_candidates import CandidateStore
from litworkbench.text import UnitStatus
from litworkbench.worker import Worker

PROFILE = "small-lowmem-v1"

RESULT = {
    "text": "候选识别：白塔位于城北。",
    "lines": [
        {"box": [[1, 1], [2, 1], [2, 2], [1, 2]], "text": "候选识别：白塔位于城北。",
         "confidence": 0.9}
    ],
    "engine": "rapidocr",
    "model_fingerprints": [{"name": "rec", "sha256": "a" * 16}],
    "config": {"profile_id": PROFILE, "render_scale": 2},
    "timing": {"init_seconds": 0.1, "recognize_seconds": 0.2},
    "memory": {"rss_bytes": 1000, "private_bytes": 2000},
    "coordinate_system": "render_pixels",
    "render_width": 100,
    "render_height": 100,
}

EXPIRED = "1970-01-01T00:00:00+00:00"


# ------------------------------------------------------------------ 夹具 / 助手


@pytest.fixture
def jobs_store(web_workspace):
    return JobStore(web_workspace)


@pytest.fixture
def candidate_store(web_workspace):
    return CandidateStore(web_workspace)


@pytest.fixture
def pdf_doc(web_workspace, web_fixtures):
    return import_document(web_workspace, web_fixtures["text_pdf"], ocr_mode="off")


def _gate_module():
    """延迟导入新增模块：缺失时每个用例各自失败，而不是整份模块收集失败。"""
    return importlib.import_module("litworkbench.ocr_gate")


def _generation(workspace, job_id: str) -> int:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT claim_generation FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row is not None, job_id
    return int(row["claim_generation"])


def _expire_lease(workspace, job_id: str) -> None:
    with workspace.connect() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?", (EXPIRED, job_id)
        )


def _unit_states(workspace, job_id: str) -> dict:
    with workspace.connect() as conn:
        rows = conn.execute(
            "SELECT unit_id, state FROM job_units WHERE job_id = ?", (job_id,)
        ).fetchall()
    return {row["unit_id"]: row["state"] for row in rows}


def _candidate_row(workspace, job_id: str) -> dict:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT * FROM ocr_candidates WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _claimed_candidate_job(workspace, candidate_store, jobs_store, pdf_doc,
                           owner: str, *, unit_index: int = 0):
    unit_id = workspace.units_for_doc(pdf_doc["id"])[unit_index]["id"]
    job_id = candidate_store.create_job(pdf_doc["id"], [unit_id], profile_id=PROFILE)["id"]
    claimed = jobs_store.claim_next(owner, lease_seconds=60)
    assert claimed is not None and claimed["id"] == job_id, claimed
    return job_id, owner, _generation(workspace, job_id)


def _ok_run_page(workspace, unit, *, profile_id, checkpoint=None):  # noqa: ARG001
    return dict(RESULT)


# ------------------------------------------------------------------ 领取代次 / 执行栅栏


def test_claim_next_bumps_claim_generation_even_for_reused_owner(web_workspace):
    store = JobStore(web_workspace)
    first = store.enqueue_import("stg_1", "一.pdf")["id"]
    second = store.enqueue_import("stg_2", "二.pdf")["id"]
    assert _generation(web_workspace, first) == 0

    claimed = store.claim_next("worker-a", lease_seconds=60)
    assert claimed["id"] == first
    assert claimed["claim_generation"] == 1
    assert _generation(web_workspace, first) == 1

    assert store.claim_next("worker-a", lease_seconds=60)["id"] == second
    assert _generation(web_workspace, second) == 1

    # 同一执行者过期后重新领取**同一个任务**：代次继续自增。
    _expire_lease(web_workspace, first)
    assert store.recover_stale() >= 1
    reclaimed = store.claim_next("worker-a", lease_seconds=60)
    assert reclaimed["id"] == first
    assert _generation(web_workspace, first) == 2


def test_execution_yields_a_committing_connection(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg", "x.pdf")["id"]
    store.claim_next("owner-a", lease_seconds=60)
    generation = _generation(web_workspace, job)

    with store.execution(job, "owner-a", generation) as conn:
        assert conn.execute("SELECT id FROM jobs WHERE id = ?", (job,)).fetchone() is not None
        conn.execute("UPDATE jobs SET stage = ? WHERE id = ?", ("已执行写入", job))

    assert store.get(job)["stage"] == "已执行写入"


def test_execution_rejects_wrong_owner_generation_and_terminal(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg", "x.pdf")["id"]
    store.claim_next("owner-a", lease_seconds=60)
    generation = _generation(web_workspace, job)

    with pytest.raises(jobs_mod.ClaimLost):
        with store.execution(job, "owner-b", generation):
            pass
    with pytest.raises(jobs_mod.ClaimLost):
        with store.execution(job, "owner-a", generation + 1):
            pass

    store.finish(job, "completed")
    with pytest.raises(jobs_mod.ClaimLost):
        with store.execution(job, "owner-a", generation):
            pass


def test_execution_rejects_expired_lease_before_another_claimant_exists(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg", "x.pdf")["id"]
    store.claim_next("owner-a", lease_seconds=60)
    generation = _generation(web_workspace, job)
    _expire_lease(web_workspace, job)

    with pytest.raises(jobs_mod.ClaimLost):
        with store.execution(job, "owner-a", generation):
            pass
    # 没有新执行者接替时，过期租约同样不能写入。
    assert store.get(job)["state"] == "running"


def test_execution_rolls_back_when_the_body_raises(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg", "x.pdf")["id"]
    store.claim_next("owner-a", lease_seconds=60)
    generation = _generation(web_workspace, job)

    with pytest.raises(RuntimeError):
        with store.execution(job, "owner-a", generation) as conn:
            conn.execute("UPDATE jobs SET stage = 'TAINT' WHERE id = ?", (job,))
            raise RuntimeError("模拟页内失败")

    assert store.get(job)["stage"] != "TAINT"


def test_old_worker_cannot_finish_a_newer_owners_job(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg", "x.pdf")["id"]
    store.claim_next("owner-a", lease_seconds=60)
    _expire_lease(web_workspace, job)
    store.recover_stale()
    store.claim_next("owner-b", lease_seconds=60)
    assert _generation(web_workspace, job) == 2

    with pytest.raises(jobs_mod.ClaimLost):
        store.finish(job, "failed", error="旧执行者迟到", owner="owner-a", generation=1)

    with web_workspace.connect() as conn:
        row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone())
    assert row["state"] == "running"
    assert row["owner"] == "owner-b"


def test_execution_writes_reject_stale_token_and_accept_current(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg", "x.pdf")["id"]
    store.claim_next("owner-a", lease_seconds=60)
    _expire_lease(web_workspace, job)
    store.recover_stale()
    store.claim_next("owner-a", lease_seconds=60)
    current = _generation(web_workspace, job)
    assert current == 2
    stale = current - 1

    operations = {
        "renew": lambda owner, gen: store.renew_lease(job, owner=owner, generation=gen),
        "stage": lambda owner, gen: store.set_stage(job, "阶段", owner=owner, generation=gen),
        "plan": lambda owner, gen: store.plan_units(job, ["u_missing"], owner=owner, generation=gen),
        "counters": lambda owner, gen: store.refresh_counters(job, owner=owner, generation=gen),
        "finish_unit": lambda owner, gen: store.finish_unit(
            job, "u_missing", "done", owner=owner, generation=gen
        ),
        "finish": lambda owner, gen: store.finish(job, "partial", owner=owner, generation=gen),
    }
    for name, operation in operations.items():
        with pytest.raises(jobs_mod.ClaimLost):
            operation("owner-a", stale)

    # 每个过期写入都没有副作用。
    assert store.get(job)["state"] == "running"
    assert _unit_states(web_workspace, job) == {}

    # 现用代次可用。
    store.renew_lease(job, owner="owner-a", generation=current)
    store.set_stage(job, "现用阶段", owner="owner-a", generation=current)
    assert store.get(job)["stage"].endswith("现用阶段")


# ------------------------------------------------------------------ 候选 worker


def test_candidate_worker_marks_ready_without_mutating_unit_index_or_evidence(
    web_workspace, pdf_doc, candidate_store, jobs_store, monkeypatch
):
    unit = web_workspace.units_for_doc(pdf_doc["id"])[0]
    job_id = candidate_store.create_job(
        pdf_doc["id"], [unit["id"]], profile_id=PROFILE
    )["id"]

    engine_calls = []
    monkeypatch.setattr(ocr, "get_engine", lambda: engine_calls.append("engine"))
    before_unit = web_workspace.get_unit(unit["id"])
    with web_workspace.connect() as conn:
        before_revision = search_index.current_revision(conn)
        before_evidence = conn.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"]

    calls = []

    def fake_run_page(workspace, unit_arg, *, profile_id, checkpoint=None):  # noqa: ARG001
        calls.append({"unit_id": unit_arg["id"], "profile_id": profile_id,
                      "file_page": unit_arg.get("file_page")})
        return dict(RESULT)

    worker = Worker(web_workspace, owner_id="worker-cand", run_page=fake_run_page)
    processed = worker.tick()
    assert processed is not None and processed["id"] == job_id
    assert processed["completed_pages"] == 1
    assert processed["state"] in {"completed", "partial"}

    candidates = candidate_store.list_for_unit(unit["id"])["items"]
    assert len(candidates) == 1
    candidate = candidate_store.get(candidates[0]["id"])
    assert candidate["state"] == "ready"
    assert candidate["profile_id"] == PROFILE
    assert candidate["result"]["text"] == RESULT["text"]
    assert calls and calls[0]["unit_id"] == unit["id"]
    assert calls[0]["profile_id"] == PROFILE

    after_unit = web_workspace.get_unit(unit["id"])
    assert after_unit["original_text"] == before_unit["original_text"]
    assert after_unit["content_revision"] == before_unit["content_revision"]
    with web_workspace.connect() as conn:
        assert search_index.current_revision(conn) == before_revision
        assert conn.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"] == before_evidence

    assert jobs_store.get(job_id)["review_pending_pages"] == 1
    assert _unit_states(web_workspace, job_id)[unit["id"]] == "done"

    # 候选尝试写入的 owner/generation 与当前领取一致（执行权确实来自栅栏）。
    row = _candidate_row(web_workspace, job_id)
    assert row["owner"] == "worker-cand"
    assert int(row["generation"]) == _generation(web_workspace, job_id)
    # 父进程绝不初始化 RapidOCR 单例。
    assert engine_calls == []


def test_candidate_worker_page_failure_marks_attempt_failed_and_keeps_unit(
    web_workspace, pdf_doc, candidate_store, jobs_store
):
    unit = web_workspace.units_for_doc(pdf_doc["id"])[0]
    job_id = candidate_store.create_job(
        pdf_doc["id"], [unit["id"]], profile_id=PROFILE
    )["id"]
    before = web_workspace.get_unit(unit["id"])

    def boom(workspace, unit_arg, *, profile_id, checkpoint=None):  # noqa: ARG001
        raise RuntimeError("模拟引擎崩溃")

    worker = Worker(web_workspace, owner_id="worker-fail", run_page=boom)
    worker.tick()

    candidates = candidate_store.list_for_unit(unit["id"])["items"]
    assert len(candidates) == 1
    failed = candidate_store.get(candidates[0]["id"])
    assert failed["state"] == "failed"
    assert failed["error"] and failed["error"]["message"]

    after = web_workspace.get_unit(unit["id"])
    assert after["original_text"] == before["original_text"]
    assert after["content_revision"] == before["content_revision"]
    assert jobs_store.get(job_id)["failed_pages"] == 1


def test_recovery_terminalizes_active_attempt_and_allows_a_new_one(
    web_workspace, pdf_doc, candidate_store, jobs_store
):
    units = [unit["id"] for unit in web_workspace.units_for_doc(pdf_doc["id"])]
    job_id = candidate_store.create_job(
        pdf_doc["id"], units[:2], profile_id=PROFILE
    )["id"]
    owner = "worker-crash"
    claimed = jobs_store.claim_next(owner, lease_seconds=60)
    assert claimed["id"] == job_id
    generation = _generation(web_workspace, job_id)

    ready = candidate_store.begin_attempt(job_id, units[0], owner=owner, generation=generation)
    candidate_store.complete_attempt(ready["id"], dict(RESULT), owner=owner, generation=generation)
    running = candidate_store.begin_attempt(job_id, units[1], owner=owner, generation=generation)

    _expire_lease(web_workspace, job_id)
    jobs_store.recover_stale()

    # 成功的 ready 不重做；活动尝试被终结为 interrupted。
    assert candidate_store.get(ready["id"])["state"] == "ready"
    recovered = candidate_store.get(running["id"])
    assert recovered["state"] == "failed"
    assert recovered["error"]["kind"] == "interrupted"
    assert jobs_store.get(job_id)["state"] == "queued"

    reclaimed = jobs_store.claim_next("worker-2", lease_seconds=60)
    assert reclaimed["id"] == job_id
    new_generation = _generation(web_workspace, job_id)
    assert new_generation > generation

    fresh = candidate_store.begin_attempt(
        job_id, units[1], owner="worker-2", generation=new_generation
    )
    assert fresh["id"] != running["id"]
    # 旧失败尝试的审计不被覆盖。
    assert candidate_store.get(running["id"])["state"] == "failed"
    assert candidate_store.list_for_unit(units[1])["total"] == 2


def test_recovery_does_not_rerun_finished_candidates(
    web_workspace, pdf_doc, candidate_store, jobs_store
):
    units = [unit["id"] for unit in web_workspace.units_for_doc(pdf_doc["id"])]
    job_id = candidate_store.create_job(
        pdf_doc["id"], units[:2], profile_id=PROFILE
    )["id"]
    owner = "worker-done"
    jobs_store.claim_next(owner, lease_seconds=60)
    generation = _generation(web_workspace, job_id)

    first = candidate_store.begin_attempt(job_id, units[0], owner=owner, generation=generation)
    candidate_store.complete_attempt(first["id"], dict(RESULT), owner=owner, generation=generation)
    second = candidate_store.begin_attempt(job_id, units[1], owner=owner, generation=generation)
    candidate_store.complete_attempt(second["id"], dict(RESULT), owner=owner, generation=generation)
    candidate_store.reject(second["id"], reason="先保留现用")

    _expire_lease(web_workspace, job_id)
    jobs_store.recover_stale()

    assert jobs_store.todo_units(job_id) == []
    assert candidate_store.get(first["id"])["state"] == "ready"
    assert candidate_store.get(second["id"])["state"] == "rejected"


def test_stale_worker_process_raises_claim_lost_and_cannot_finish_newer_job(
    web_workspace, pdf_doc, candidate_store, jobs_store
):
    unit = web_workspace.units_for_doc(pdf_doc["id"])[0]
    job_id = candidate_store.create_job(
        pdf_doc["id"], [unit["id"]], profile_id=PROFILE
    )["id"]
    stale_job = jobs_store.claim_next("worker-old", lease_seconds=60)
    assert stale_job["id"] == job_id

    _expire_lease(web_workspace, job_id)
    jobs_store.recover_stale()
    jobs_store.claim_next("worker-new", lease_seconds=60)
    assert _generation(web_workspace, job_id) == 2

    worker_old = Worker(web_workspace, owner_id="worker-old", run_page=_ok_run_page)
    with pytest.raises(jobs_mod.ClaimLost):
        worker_old.process(stale_job)

    with web_workspace.connect() as conn:
        row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    assert row["state"] == "running"
    assert row["owner"] == "worker-new"


# ------------------------------------------------------------------ 工作区 OCR 闸门

def test_retry_failed_rejects_active_candidate_without_orphaning_attempt(web_workspace, pdf_doc, candidate_store, jobs_store):
    from litworkbench.errors import EditConflictError
    unit = web_workspace.units_for_doc(pdf_doc["id"])[0]
    job = candidate_store.create_job(pdf_doc["id"], [unit["id"]], profile_id=PROFILE)["id"]
    claim = jobs_store.claim_next("owner")
    attempt = candidate_store.begin_attempt(job, unit["id"], owner="owner", generation=claim["claim_generation"])
    with pytest.raises(EditConflictError):
        jobs_store.retry_failed(job)
    assert jobs_store.get(job)["state"] == "running"
    candidate_store.complete_attempt(attempt["id"], dict(RESULT), owner="owner", generation=claim["claim_generation"])


@pytest.mark.parametrize("missing", [True, False])
def test_controlled_import_failure_preserves_extracted_text(web_workspace, session, web_fixtures, missing):
    def fail(workspace, unit, *, profile_id, checkpoint=None):
        if missing:
            raise ocr.OcrUnavailable("本机 OCR 不可用")
        return {"ok": False, "error": {"kind": "page_timeout", "message": "超时"},
                "process": {"reaped": True}}
    response = session.upload([("files", ("文本.pdf", web_fixtures["text_pdf"].read_bytes(), "application/pdf"))],
                              data={"ocr_mode": "force"})
    assert response.status_code == 202
    worker = Worker(web_workspace, run_page=fail)
    job = worker.tick()
    units = web_workspace.units_for_doc(job["doc_id"])
    assert units and all(unit["original_text"].strip() for unit in units)
    assert all(unit["status"] == ("pending" if missing else "failed") for unit in units)


def test_two_jobs_share_one_durable_workspace_gate(
    web_workspace, pdf_doc, candidate_store, jobs_store
):
    gate_mod = _gate_module()
    job1, owner1, gen1 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g1"
    )
    job2, owner2, gen2 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g2"
    )

    gate = gate_mod.OcrGate(web_workspace)
    ticket = gate.acquire(job_id=job1, owner=owner1, generation=gen1, lease_seconds=60)
    assert ticket["token"]

    durable = gate_mod.OcrGate(web_workspace).inspect()
    assert durable["state"] == "held"
    assert durable["job_id"] == job1

    with pytest.raises(gate_mod.GateBusy):
        gate_mod.OcrGate(web_workspace).acquire(
            job_id=job2, owner=owner2, generation=gen2, wait_timeout=0
        )

    gate.release(ticket["token"], reaped=True)
    assert gate_mod.OcrGate(web_workspace).inspect()["state"] == "free"

    ticket2 = gate_mod.OcrGate(web_workspace).acquire(
        job_id=job2, owner=owner2, generation=gen2, lease_seconds=60
    )
    assert ticket2["token"]
    gate.release(ticket2["token"], reaped=True)


def test_waiting_for_gate_renews_own_job_lease(
    web_workspace, pdf_doc, candidate_store, jobs_store
):
    gate_mod = _gate_module()
    job1, owner1, gen1 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g1"
    )
    job2, owner2, gen2 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g2"
    )

    gate = gate_mod.OcrGate(web_workspace)
    holder = gate.acquire(job_id=job1, owner=owner1, generation=gen1, lease_seconds=60)

    renewals = []

    def renew_and_release():
        renewals.append(1)
        if len(renewals) == 1:
            gate.release(holder["token"], reaped=True)

    ticket = gate_mod.OcrGate(web_workspace).acquire(
        job_id=job2, owner=owner2, generation=gen2,
        wait_timeout=3.0, poll_interval=0.02, renew_own_lease=renew_and_release,
    )
    assert renewals, "等待闸门期间必须续租自己的任务租约"
    assert ticket["token"]
    gate.release(ticket["token"], reaped=True)


def test_expired_gate_with_live_child_is_reaped_before_handover(
    web_workspace, pdf_doc, candidate_store, jobs_store, monkeypatch
):
    gate_mod = _gate_module()
    job1, owner1, gen1 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g1"
    )
    job2, owner2, gen2 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g2"
    )

    gate = gate_mod.OcrGate(web_workspace)
    holder = gate.acquire(job_id=job1, owner=owner1, generation=gen1, lease_seconds=0)
    gate.register_child(holder["token"], pid=41001, create_time=111.5)

    monkeypatch.setattr(gate_mod, "_registered_child_alive", lambda pid, ct: True)
    reaped = []
    monkeypatch.setattr(
        gate_mod, "_reap_registered_child",
        lambda pid, ct: (reaped.append((pid, ct)), True)[1],
    )

    ticket = gate_mod.OcrGate(web_workspace).acquire(
        job_id=job2, owner=owner2, generation=gen2, wait_timeout=0.5, poll_interval=0.02
    )
    assert reaped == [(41001, 111.5)]
    assert ticket["token"]
    gate.release(ticket["token"], reaped=True)


def test_unconfirmable_cleanup_leaves_gate_unavailable(
    web_workspace, pdf_doc, candidate_store, jobs_store, monkeypatch
):
    gate_mod = _gate_module()
    job1, owner1, gen1 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g1"
    )
    job2, owner2, gen2 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g2"
    )

    gate = gate_mod.OcrGate(web_workspace)
    holder = gate.acquire(job_id=job1, owner=owner1, generation=gen1, lease_seconds=0)
    gate.register_child(holder["token"], pid=41002, create_time=222.0)

    monkeypatch.setattr(gate_mod, "_registered_child_alive", lambda pid, ct: True)
    monkeypatch.setattr(gate_mod, "_reap_registered_child", lambda pid, ct: False)

    with pytest.raises(gate_mod.GateUnavailable):
        gate_mod.OcrGate(web_workspace).acquire(
            job_id=job2, owner=owner2, generation=gen2, wait_timeout=0
        )

    # 清理无法确认 -> 不得把闸门转交给新的执行者。
    still = gate_mod.OcrGate(web_workspace).inspect()
    assert still["job_id"] != job2


def test_recycled_child_pid_is_not_killed(
    web_workspace, pdf_doc, candidate_store, jobs_store, monkeypatch
):
    gate_mod = _gate_module()
    job1, owner1, gen1 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g1"
    )
    job2, owner2, gen2 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g2"
    )

    gate = gate_mod.OcrGate(web_workspace)
    holder = gate.acquire(job_id=job1, owner=owner1, generation=gen1, lease_seconds=0)
    gate.register_child(holder["token"], pid=41003, create_time=333.0)

    # 身份不符（PID 已回收）：绝不是我们的子进程。
    monkeypatch.setattr(gate_mod, "_registered_child_alive", lambda pid, ct: False)
    killed = []
    monkeypatch.setattr(
        gate_mod, "_reap_registered_child",
        lambda pid, ct: (killed.append(pid), True)[1],
    )

    ticket = gate_mod.OcrGate(web_workspace).acquire(
        job_id=job2, owner=owner2, generation=gen2, wait_timeout=0.5, poll_interval=0.02
    )
    assert killed == []
    assert ticket["token"]
    gate.release(ticket["token"], reaped=True)


def test_release_is_fenced_to_current_gate_token(
    web_workspace, pdf_doc, candidate_store, jobs_store
):
    gate_mod = _gate_module()
    job1, owner1, gen1 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g1"
    )
    job2, owner2, gen2 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g2"
    )
    job3, owner3, gen3 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g3"
    )

    gate = gate_mod.OcrGate(web_workspace)
    first = gate.acquire(job_id=job1, owner=owner1, generation=gen1, lease_seconds=60)
    gate.release(first["token"], reaped=True)

    second = gate.acquire(job_id=job2, owner=owner2, generation=gen2, lease_seconds=60)
    try:
        gate.release(first["token"], reaped=True)  # 过期 token 不得放行
    except LitError:
        pass

    with pytest.raises(gate_mod.GateBusy):
        gate.acquire(job_id=job3, owner=owner3, generation=gen3, wait_timeout=0)

    gate.release(second["token"], reaped=True)
    assert gate.inspect()["state"] == "free"


# ------------------------------------------------------------------ 既有 OCR 路径


def test_pdfpages_block_for_signature_preserved_and_callback_seam_added():
    signature = inspect.signature(PdfPages.block_for)
    parameters = list(signature.parameters.values())
    assert parameters[0].name == "self"
    assert parameters[1].name == "index"
    assert "ocr_mode" in signature.parameters
    assert signature.parameters["ocr_mode"].kind is inspect.Parameter.KEYWORD_ONLY

    # 既有 import/reprocess 的 OCR 回调接缝：默认仍是旧 _ocr_page（CLI/同步行为不变）。
    assert "ocr_page" in inspect.signature(open_pdf_pages).parameters
    assert "ocr_page" in inspect.signature(force_page_block).parameters


def test_text_only_import_does_not_need_gate(
    web_workspace, pdf_doc, candidate_store, jobs_store, session, web_fixtures
):
    gate_mod = _gate_module()
    job1, owner1, gen1 = _claimed_candidate_job(
        web_workspace, candidate_store, jobs_store, pdf_doc, "g-holder"
    )
    gate = gate_mod.OcrGate(web_workspace)
    holder = gate.acquire(job_id=job1, owner=owner1, generation=gen1, lease_seconds=120)

    response = session.upload([
        ("files", ("纯文字.pdf", web_fixtures["text_pdf"].read_bytes(), "application/pdf")),
    ])
    assert response.status_code == 202, response.text
    job_id = response.json()["job_ids"][0]

    worker = Worker(web_workspace, owner_id="importer")
    for _ in range(10):
        if worker.tick() is None:
            break

    assert jobs_store.get(job_id)["state"] in {"completed", "partial"}
    # 纯文字导入不占闸门：原持有者仍在。
    assert gate_mod.OcrGate(web_workspace).inspect()["job_id"] == job1
    gate.release(holder["token"], reaped=True)


def test_background_reprocess_uses_injected_executor_within_gate(
    web_workspace, session, web_fixtures, jobs_store
):
    gate_mod = _gate_module()

    response = session.upload([
        ("files", ("重识别.pdf", web_fixtures["text_pdf"].read_bytes(), "application/pdf")),
    ])
    import_job = response.json()["job_ids"][0]
    importer = Worker(web_workspace, owner_id="importer")
    for _ in range(10):
        if importer.tick() is None:
            break
    doc_id = jobs_store.get(import_job)["doc_id"]
    unit = web_workspace.units_for_doc(doc_id)[0]

    gate_seen = []

    def legacy_ocr(workspace, path, index, text, scale):  # noqa: ARG001
        gate_seen.append(gate_mod.OcrGate(workspace).inspect())
        return {
            "method": "ocr",
            "status": UnitStatus.COMPLETED,
            "notes": "",
            "original_text": "强制识别白塔文字",
            "ocr_json": {"engine": "injected", "lines": [], "text": "强制识别白塔文字"},
        }

    reprocess = session.post(
        f"/api/v1/documents/{doc_id}/reprocess",
        json={"unit_ids": [unit["id"]], "mode": "force", "keep_corrections": True},
    )
    assert reprocess.status_code == 202, reprocess.text
    reprocess_id = reprocess.json()["job_ids"][0]

    worker = Worker(web_workspace, owner_id="reprocess", ocr_page=legacy_ocr)
    for _ in range(10):
        if worker.tick() is None:
            break

    assert jobs_store.get(reprocess_id)["state"] in {"completed", "partial"}
    assert web_workspace.get_unit(unit["id"])["original_text"] == "强制识别白塔文字"
    assert gate_seen, "既有后台重新识别也必须经闸门执行"
    assert gate_seen[0]["state"] == "held"
    assert gate_seen[0]["job_id"] == reprocess_id
