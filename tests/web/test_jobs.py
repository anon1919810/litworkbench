"""任务队列语义：租约、暂停/继续/重试幂等、计数诚实。"""

from __future__ import annotations

import time

import pytest

from litworkbench.jobs import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_PAUSED,
    STATE_QUEUED,
    JobStore,
)
from litworkbench.worker import Worker, run_worker, stop_marker_path


@pytest.fixture
def staged_pdf(session, web_workspace, web_fixtures, run_worker):
    response = session.upload([("files", ("任务样例.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    assert response.status_code == 202
    return response.json()["job_ids"][0]


def test_job_list_is_paginated_and_sorted(session, staged_pdf, run_worker, web_workspace):
    run_worker()
    body = session.get("/api/v1/jobs?page=1&page_size=10").json()
    assert set(body) == {"items", "total", "page", "page_size"}
    item = body["items"][0]
    assert set(item) == {
        "id", "filename", "doc_id", "kind", "state", "stage", "total_pages",
        "processed_pages", "completed_pages", "failed_pages", "pending_pages",
        "searchable_pages", "review_pending_pages", "duplicate", "error", "pause_requested", "created_at",
        "updated_at",
    }
    assert item["filename"] == "任务样例.pdf"
    assert item["state"] == STATE_COMPLETED
    assert session.get("/api/v1/jobs?page=2&page_size=10").json()["total"] == 1


def test_job_stage_is_readable_chinese(session, staged_pdf, web_workspace):
    job = JobStore(web_workspace).get(staged_pdf)
    assert job["stage"]
    assert any("一" <= ch <= "鿿" for ch in job["stage"])


def test_pause_resume_retry_are_idempotent(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg_x", "x.pdf", "auto")["id"]
    first = store.request_pause(job)
    second = store.request_pause(job)
    assert first["state"] == second["state"] == STATE_PAUSED
    resumed = store.resume(job)
    assert resumed["state"] == STATE_QUEUED
    assert store.resume(job)["state"] == STATE_QUEUED
    retried = store.retry_failed(job)
    assert retried["state"] == STATE_QUEUED
    assert store.retry_failed(job)["state"] == STATE_QUEUED


def test_control_of_finished_job_does_not_lose_results(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg_x", "x.pdf", "auto")["id"]
    store.finish(job, STATE_COMPLETED)
    assert store.request_pause(job)["state"] == STATE_COMPLETED
    assert store.get(job)["state"] == STATE_COMPLETED


def test_missing_job_returns_not_found(session):
    for action in ("pause", "resume", "retry-failed"):
        response = session.post(f"/api/v1/jobs/job_missing/{action}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
    assert session.get("/api/v1/jobs/job_missing").status_code == 404


def test_only_one_worker_can_hold_a_job(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg_x", "x.pdf", "auto")["id"]
    assert store.claim_next("worker-a")["id"] == job
    assert store.claim_next("worker-b") is None
    store.renew_lease(job)
    store.finish(job, STATE_COMPLETED)


def test_queue_order_is_creation_order(web_workspace):
    store = JobStore(web_workspace)
    first = store.enqueue_import("stg_1", "1.pdf")["id"]
    second = store.enqueue_import("stg_2", "2.pdf")["id"]
    assert store.claim_next("worker-a")["id"] == first
    assert store.claim_next("worker-a")["id"] == second


def test_counters_are_refreshed_from_database(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg_x", "x.pdf")["id"]
    counts = store.refresh_counters(job)
    assert counts["total_pages"] is None and counts["completed_pages"] == 0
    with web_workspace.connect() as conn:
        conn.execute("UPDATE jobs SET doc_id = 'doc_missing' WHERE id = ?", (job,))
    assert store.refresh_counters(job)["completed_pages"] == 0


def test_worker_stops_on_marker_file(web_workspace, web_fixtures, session, tmp_path):
    """停止标记让后台执行者优雅退出（服务关闭路径）。"""
    session.upload([("files", ("样例.pdf", web_fixtures["text_pdf"].read_bytes(),
                              "application/pdf"))])
    marker = stop_marker_path(web_workspace.root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("stop", encoding="utf-8")
    result = run_worker(web_workspace.root, max_jobs=3, poll_interval=0.01)
    assert result["count"] == 0  # 看到标记立刻退出，不领取任务
    marker.unlink()


def test_worker_subprocess_starts_and_stops_gracefully(web_workspace):
    """真实子进程：启动 -> 看到停止标记后退出，并且清理标记文件。"""
    from litworkbench.web.app import WorkerProcess

    handle = WorkerProcess(web_workspace.root)
    handle.start()
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not handle.alive():
            time.sleep(0.1)
        assert handle.alive(), "后台识别进程没有启动"
        # 排队一个任务，确认执行者真的在工作（领取后状态会变成 running）
        JobStore(web_workspace).enqueue_import("stg_x", "x.pdf")
    finally:
        handle.stop(timeout=20.0)
    assert handle.process is None
    assert not handle.marker.exists(), "退出后要清理停止标记"
    # 只验证队列里留下了恰好一条记录：真实 worker 可能已经领取（running），
    # 也可能因为这条伪造的暂存文件而失败（failed）。本用例只负责「进程能起、能干净退出」。
    items = JobStore(web_workspace).list()["items"]
    assert len(items) == 1
    assert items[0]["state"] in {"queued", "running", "preparing", "failed"}


def test_worker_process_object_is_reusable(web_workspace):
    """Worker.tick 在没有任务时返回 None，不会抛异常。"""
    assert Worker(web_workspace).tick() is None


def test_worker_reports_unreadable_staging_as_failed(session, web_workspace,
                                                      run_worker, monkeypatch):
    """暂存文件不见了：任务失败并给出中文原因，不会假装成功。"""
    response = session.upload([("files", ("样例.pdf", b"%PDF-1.4 fake", "application/pdf"))])
    job_id = response.json()["job_ids"][0]
    store = JobStore(web_workspace)
    staging = store.staging(_staging_id(web_workspace, job_id))
    (web_workspace.root / staging["stored_path"]).unlink()
    run_worker()
    job = store.get(job_id)
    assert job["state"] == STATE_FAILED
    assert "上传文件已丢失" in job["error"]


def _staging_id(workspace, job_id: str) -> str:
    with workspace.connect() as conn:
        return conn.execute("SELECT staging_id FROM jobs WHERE id = ?",
                            (job_id,)).fetchone()["staging_id"]


def test_corrupt_pdf_fails_the_job_without_creating_document(session, web_workspace,
                                                              run_worker):
    """内容损坏的 PDF 进入队列后失败，不会产出一个「完成」的假文献。"""
    data = b"%PDF-1.4\n" + b"0" * 200  # 有 PDF 头但无法解析
    response = session.upload([("files", ("损坏.pdf", data, "application/pdf"))])
    assert response.status_code == 202
    job_id = response.json()["job_ids"][0]
    run_worker()
    job = JobStore(web_workspace).get(job_id)
    assert job["state"] in {STATE_FAILED, "partial"}
    docs = [doc for doc in web_workspace.list_documents() if doc["filename"] == "损坏.pdf"]
    if docs:
        assert docs[0]["status"] != "completed"
        assert docs[0]["unit_count"] == 0 or docs[0]["pending_count"] > 0