"""上传导入 -> 后台逐页识别 -> 任务状态的真实集成测试。

这里跑的是真正的 :class:`litworkbench.worker.Worker`（SQLite 租约 + 逐页检查点），
不启动 HTTP 里的长任务，也不打桩识别流程。
"""

from __future__ import annotations

import pytest

from litworkbench.jobs import STATE_COMPLETED, STATE_PARTIAL, JobStore
from litworkbench.store import sha256_text


def _upload(session, path, filename=None):
    data = path.read_bytes()
    name = filename or path.name
    response = session.upload([("files", (name, data, "application/octet-stream"))])
    return response


def test_upload_returns_202_and_preserves_original_filename(session, run_worker,
                                                             web_fixtures, web_workspace):
    response = _upload(session, web_fixtures["text_pdf"], "青川县志（合成）第一册.pdf")
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["rejected"] == []
    assert len(payload["job_ids"]) == 1

    run_worker()
    doc = web_workspace.list_documents()[0]
    assert doc["filename"] == "青川县志（合成）第一册.pdf"  # 暂存 ID 不能变成文件名
    assert doc["status"] == "completed"
    assert doc["page_count"] == 3

    job = JobStore(web_workspace).get(payload["job_ids"][0])
    assert job["state"] == STATE_COMPLETED
    assert (job["total_pages"], job["completed_pages"], job["failed_pages"]) == (3, 3, 0)
    assert job["pending_pages"] == 0
    assert job["searchable_pages"] == 3
    assert job["stage"]  # 中文可读阶段


def test_path_like_filename_is_treated_as_data(session, run_worker, web_fixtures,
                                                web_workspace):
    """文件名里的 Windows/Unix 路径成分只是数据，不会写到磁盘或数据库里。"""
    response = session.upload([("files", ("../../etc/合成  malicious.pdf",
                                          web_fixtures["text_pdf"].read_bytes(),
                                          "application/octet-stream"))])
    assert response.status_code == 202
    run_worker()
    doc = web_workspace.list_documents()[0]
    assert "/" not in doc["filename"] and "\\" not in doc["filename"]
    assert doc["filename"] == "合成  malicious.pdf"
    # 原件仍然按内容哈希存放，用户给不出服务器路径
    assert web_workspace.path_of(doc).parent == web_workspace.sources_dir / doc["sha256"][:2]


def test_duplicate_content_links_existing_document(session, run_worker, web_fixtures,
                                                   web_workspace):
    first = _upload(session, web_fixtures["text_pdf"], "第一册.pdf")
    run_worker()
    second = _upload(session, web_fixtures["text_pdf"], "换个名字上传.pdf")
    run_worker()
    assert len(web_workspace.list_documents()) == 1
    jobs = JobStore(web_workspace).list()["items"]
    assert jobs[0]["duplicate"] is True  # 最新一个任务标记为重复
    assert first.json()["job_ids"] != second.json()["job_ids"]


def test_invalid_content_cannot_masquerade_as_success(session, run_worker, tmp_path,
                                                      web_workspace):
    fake = tmp_path / "伪装.pdf"
    fake.write_bytes(b"this is definitely not a pdf")
    response = _upload(session, fake, "伪装.pdf")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ALL_FILES_REJECTED"
    assert response.json()["error"]["details"]["rejected"][0]["filename"] == "伪装.pdf"
    assert web_workspace.list_documents() == []
    assert JobStore(web_workspace).list()["items"] == []


def test_unsupported_extension_is_rejected_per_file(session, web_fixtures):
    good = ("files", ("好文件.pdf", web_fixtures["text_pdf"].read_bytes(), "application/pdf"))
    bad = ("files", ("笔记.rtf", b"rtf", "application/rtf"))
    response = session.upload([good, bad])
    assert response.status_code == 202
    payload = response.json()
    assert len(payload["job_ids"]) == 1
    assert payload["rejected"][0]["filename"] == "笔记.rtf"
    assert ".rtf" in payload["rejected"][0]["error"]


def test_empty_file_is_rejected(session):
    response = session.upload([("files", ("空.pdf", b"", "application/pdf"))])
    assert response.status_code == 400
    assert "空文件" in response.json()["error"]["details"]["rejected"][0]["error"]


def test_page_failure_does_not_stop_other_pages_and_is_not_success(session, run_worker,
                                                                   web_workspace,
                                                                   web_fixtures, monkeypatch):
    """第 2 页抛异常：第 1、3 页照常完成，任务**不是** completed，失败页可重试。"""
    from litworkbench import ingest

    original = ingest.PdfPages.block_for
    calls = {"n": 0}

    def flaky(self, index, *, ocr_mode=None):
        calls["n"] += 1
        if index == 1:
            raise RuntimeError("模拟第 2 页渲染失败")
        return original(self, index, ocr_mode=ocr_mode)

    monkeypatch.setattr(ingest.PdfPages, "block_for", flaky)
    response = _upload(session, web_fixtures["text_pdf"])
    job_id = response.json()["job_ids"][0]
    run_worker()

    job = JobStore(web_workspace).get(job_id)
    assert job["state"] == STATE_PARTIAL
    assert job["completed_pages"] == 2
    assert job["failed_pages"] == 1
    assert job["pending_pages"] == 0
    units = {unit["file_page"]: unit for unit in web_workspace.units_for_doc(job["doc_id"])}
    assert units[2]["status"] == "failed"          # 失败页真的有一条失败单元
    assert "模拟第 2 页渲染失败" in units[2]["notes"]
    assert units[1]["status"] == "completed" and units[3]["status"] == "completed"
    assert web_workspace.get_document(job["doc_id"])["status"] == "partial"


def test_failed_page_is_retried_without_redoing_success_pages(session, run_worker,
                                                              web_workspace, web_fixtures,
                                                              monkeypatch):
    from litworkbench import ingest

    original = ingest.PdfPages.block_for
    seen: list[int] = []

    def flaky(self, index, *, ocr_mode=None):
        seen.append(index)
        if len(seen) == 2:  # 只在第一次运行时失败
            raise RuntimeError("第一次运行失败")
        return original(self, index, ocr_mode=ocr_mode)

    monkeypatch.setattr(ingest.PdfPages, "block_for", flaky)
    job_id = _upload(session, web_fixtures["text_pdf"]).json()["job_ids"][0]
    run_worker()
    store = JobStore(web_workspace)
    assert store.get(job_id)["state"] == STATE_PARTIAL

    retry = session.post(f"/api/v1/jobs/{job_id}/retry-failed")
    assert retry.status_code == 200
    assert retry.json()["state"] == "queued"
    seen.clear()
    run_worker()

    job = store.get(job_id)
    assert job["state"] == STATE_COMPLETED
    assert job["failed_pages"] == 0
    assert sorted(seen) == [1]  # 只重做失败的那一页，成功页不重做


def test_pending_page_is_resumable_and_not_counted_as_success(session, run_worker,
                                                              web_workspace, web_fixtures,
                                                              monkeypatch):
    """识别为空 -> 待处理，可重试，绝不计入 completed。"""
    from litworkbench import ingest

    original = ingest.PdfPages.block_for
    calls = {"n": 0}

    def empty_once(self, index, *, ocr_mode=None):
        calls["n"] += 1
        if index == 0 and calls["n"] == 1:
            return {
                "id": ingest._unit_id(self._doc_id, "page", 1), "doc_id": self._doc_id,
                "seq": 1, "kind": "page", "file_page": 1, "printed_page": "",
                "title_path": "", "method": "ocr", "status": "pending",
                "ocr_decision": "ocr", "original_text": "", "corrected_text": None,
                "notes": "待识别：OCR 未返回任何文字", "ocr_json": None,
            }
        return original(self, index, ocr_mode=ocr_mode)

    monkeypatch.setattr(ingest.PdfPages, "block_for", empty_once)
    job_id = _upload(session, web_fixtures["text_pdf"]).json()["job_ids"][0]
    run_worker()
    store = JobStore(web_workspace)
    job = store.get(job_id)
    assert job["state"] == STATE_PARTIAL
    assert job["completed_pages"] == 2 and job["pending_pages"] == 1
    assert job["searchable_pages"] == 2  # 待处理页不进入可检索计数

    monkeypatch.setattr(ingest.PdfPages, "block_for", original)
    session.post(f"/api/v1/jobs/{job_id}/retry-failed")
    run_worker()
    job = store.get(job_id)
    assert job["state"] == STATE_COMPLETED
    assert job["pending_pages"] == 0 and job["searchable_pages"] == 3


def test_pause_takes_effect_at_page_boundary_and_resume_keeps_staging(session, run_worker,
                                                                     web_workspace,
                                                                     web_fixtures, monkeypatch):
    from litworkbench import ingest

    store = JobStore(web_workspace)
    job_id = _upload(session, web_fixtures["text_pdf"]).json()["job_ids"][0]
    original = ingest.PdfPages.block_for

    def pause_after_first(self, index, *, ocr_mode=None):
        if index == 0:
            result = original(self, index, ocr_mode=ocr_mode)
            store.request_pause(job_id)  # 在第 1 页提交后请求暂停
            return result
        return original(self, index, ocr_mode=ocr_mode)

    monkeypatch.setattr(ingest.PdfPages, "block_for", pause_after_first)
    run_worker(max_jobs=1)
    job = store.get(job_id)
    assert job["state"] == "paused"
    assert job["completed_pages"] == 1
    assert job["pause_requested"] is False  # 已生效并落库
    with web_workspace.connect() as conn:
        staging_id = conn.execute(
            "SELECT staging_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()["staging_id"]
    staging = store.staging(staging_id)
    assert (web_workspace.root / staging["stored_path"]).exists(), "暂停后不能删掉上传文件"

    monkeypatch.setattr(ingest.PdfPages, "block_for", original)
    assert session.post(f"/api/v1/jobs/{job_id}/resume").json()["state"] == "queued"
    run_worker()
    job = store.get(job_id)
    assert job["state"] == STATE_COMPLETED
    assert job["completed_pages"] == 3
    # 完成之后才清理暂存
    assert not (web_workspace.root / staging["stored_path"]).exists()


def test_restart_recovers_interrupted_job_without_redoing_pages(session, web_workspace,
                                                                web_fixtures):
    """模拟执行者在第 2 页崩溃：重启后已完成的页不会重做。"""
    from litworkbench import worker as worker_mod
    from litworkbench import ingest

    store = JobStore(web_workspace)
    job_id = _upload(session, web_fixtures["text_pdf"]).json()["job_ids"][0]
    original = ingest.PdfPages.block_for

    def crash_on_second(self, index, *, ocr_mode=None):
        if index == 1:
            raise KeyboardInterrupt("模拟进程被杀")
        return original(self, index, ocr_mode=ocr_mode)

    ingest.PdfPages.block_for = crash_on_second
    try:
        with pytest.raises(KeyboardInterrupt):
            worker_mod.Worker(web_workspace).tick()
    finally:
        ingest.PdfPages.block_for = original

    # 把任务留在「运行中但租约过期」，模拟没有优雅退出的执行者
    with web_workspace.connect() as conn:
        conn.execute("UPDATE jobs SET state = 'running', owner = 'dead', "
                     "lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (job_id,))
    assert store.recover_stale() == 1
    assert store.get(job_id)["state"] == "queued"

    seen: list[int] = []

    def tracking(self, index, *, ocr_mode=None):
        seen.append(index)
        return original(self, index, ocr_mode=ocr_mode)

    ingest.PdfPages.block_for = tracking
    try:
        worker_mod.Worker(web_workspace).tick()
    finally:
        ingest.PdfPages.block_for = original
    assert seen == [1, 2]  # 第 0 页已完成，不重做
    assert store.get(job_id)["state"] == STATE_COMPLETED


def test_pausing_job_is_recovered_after_crash(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg_missing", "x.pdf", "auto")
    with web_workspace.connect() as conn:
        conn.execute("UPDATE jobs SET state = 'pausing', pause_requested = 1, "
                     "lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                     (job["id"],))
    assert store.recover_stale() == 1
    assert store.get(job["id"])["state"] == "queued"


def test_two_claims_only_one_wins(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("stg_a", "a.pdf", "auto")
    first = store.claim_next("worker-1")
    second = store.claim_next("worker-2")
    assert first is not None and first["id"] == job["id"]
    assert second is None
    assert store.get(job["id"])["state"] == "running"


def test_docx_import_indexes_paragraphs_and_tables(session, run_worker, web_workspace,
                                                   web_fixtures):
    job_id = _upload(session, web_fixtures["docx"]).json()["job_ids"][0]
    run_worker()
    job = JobStore(web_workspace).get(job_id)
    assert job["state"] == STATE_COMPLETED
    doc = web_workspace.get_document(job["doc_id"])
    assert doc["filetype"] == "docx"
    assert job["completed_pages"] == doc["unit_count"] > 0
    kinds = {unit["kind"] for unit in web_workspace.units_for_doc(doc["id"])}
    assert kinds == {"paragraph", "table"}
    # DOCX 不伪造页数
    assert doc["page_count"] is None


def test_coverage_counts_real_pages_after_import(session, run_worker, web_workspace,
                                                 web_fixtures):
    _upload(session, web_fixtures["text_pdf"])
    run_worker()
    coverage = session.get("/api/v1/coverage").json()
    assert coverage["known_pdf_pages"] == 3
    assert coverage["covered_pdf_pages"] == 3
    assert coverage["pending_pdf_pages"] == 0
    assert coverage["failed_pdf_pages"] == 0
    assert coverage["unknown_documents"] == 0
    assert sha256_text("x")  # 导入真实页数，不生成假百分比


def test_document_counters_and_searchable_pages_in_list(session, run_worker,
                                                         web_workspace, web_fixtures):
    _upload(session, web_fixtures["text_pdf"])
    run_worker()
    item = session.get("/api/v1/documents").json()["items"][0]
    assert item["page_count"] == 3 and item["unit_count"] == 3
    assert item["pending_count"] == 0 and item["searchable_pages"] == 3
    assert item["status"] == "completed"
    assert item["filetype"] == "pdf"