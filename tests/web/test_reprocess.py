"""强制重新识别：只重做选中的页，保留校订、历史与证据，失败不毁旧结果。"""

from __future__ import annotations

from litworkbench import ocr
from litworkbench.errors import QuotationError
from litworkbench.evidence import correct_unit, get_evidence, save_evidence
from litworkbench.jobs import JobStore, STATE_COMPLETED, STATE_PARTIAL


def _import(session, run_worker, path, filename=None):
    data = path.read_bytes()
    response = session.upload([("files", (filename or path.name, data, "application/pdf"))])
    assert response.status_code == 202, response.text
    job_id = response.json()["job_ids"][0]
    run_worker()
    return job_id


def test_reprocess_of_completed_page_really_runs_ocr(session, run_worker, web_workspace,
                                                      web_fixtures, monkeypatch):
    job_id = _import(session, run_worker, web_fixtures["text_pdf"])
    doc_id = JobStore(web_workspace).get(job_id)["doc_id"]
    units = web_workspace.units_for_doc(doc_id)
    target = units[0]
    assert target["status"] == "completed" and target["method"] == "text"

    calls = {"n": 0}

    def recognized(image):
        calls["n"] += 1
        return {"engine": "rapidocr", "lines": [
            {"box": [[1, 1], [2, 1], [2, 2], [1, 2]], "text": "强制识别的白塔新文字",
             "confidence": 0.93}],
            "text": "强制识别的白塔新文字", "mean_confidence": 0.93,
            "render_width": image.width, "render_height": image.height}

    monkeypatch.setattr(ocr, "recognize", recognized)
    response = session.post(f"/api/v1/documents/{doc_id}/reprocess", json={
        "unit_ids": [target["id"]], "mode": "force", "keep_corrections": True,
    })
    assert response.status_code == 202, response.text
    reprocess_id = response.json()["job_ids"][0]
    run_worker()

    job = JobStore(web_workspace).get(reprocess_id)
    assert job["state"] == STATE_COMPLETED
    assert calls["n"] >= 1, "已完成的页必须真正执行 OCR"
    updated = web_workspace.get_unit(target["id"])
    assert updated["method"] == "ocr"
    assert "强制识别" in updated["original_text"]
    # 范围外的页不动
    other = web_workspace.units_for_doc(doc_id)[1]
    assert other["method"] == "text"


def test_reprocess_keeps_corrections_history_and_marks_old_evidence(session, run_worker,
                                                                     web_workspace,
                                                                     web_fixtures, monkeypatch):
    """重新 OCR 只改写原文版本；校订保留，校订版摘录不误伤，原文版摘录需复核。"""
    job_id = _import(session, run_worker, web_fixtures["text_pdf"])
    doc_id = JobStore(web_workspace).get(job_id)["doc_id"]
    unit = web_workspace.units_for_doc(doc_id)[0]
    # 先按「原文版本」留一条摘录，再加校订，再按「校订版本」留一条
    on_original = save_evidence(web_workspace, unit["id"], "白塔位于县城北侧",
                                version="original", category="文物")
    correct_unit(web_workspace, unit["id"], unit["original_text"].replace("北侧", "北郊"),
                 "校订地名")
    on_corrected = save_evidence(web_workspace, unit["id"], "白塔位于县城北郊", category="文物")
    revision_count = len(web_workspace.revisions_for_unit(unit["id"]))

    monkeypatch.setattr(ocr, "recognize", lambda image: {
        "engine": "rapidocr", "lines": [], "text": "重新识别得到的白塔文字",
        "mean_confidence": 0.91, "render_width": image.width,
        "render_height": image.height})
    reprocess_id = session.post(f"/api/v1/documents/{doc_id}/reprocess", json={
        "unit_ids": [unit["id"]], "mode": "force", "keep_corrections": True,
    }).json()["job_ids"][0]
    run_worker()

    updated = web_workspace.get_unit(unit["id"])
    assert updated["corrected_text"] is not None, "人工校订必须保留"
    assert updated["original_text"] == "重新识别得到的白塔文字"
    assert len(web_workspace.revisions_for_unit(unit["id"])) > revision_count, "旧识别进历史"
    # 原文被改写 -> 引用原文版本的摘录需复核
    assert get_evidence(web_workspace, on_original["id"])["review_status"] == "stale"
    # 校订文字没有被改动 -> 引用校订版的摘录仍然有效（不误伤）
    assert get_evidence(web_workspace, on_corrected["id"])["review_status"] == "pending"
    assert get_evidence(web_workspace, on_corrected["id"])["quote"] == "白塔位于县城北郊"
    assert JobStore(web_workspace).get(reprocess_id)["state"] == STATE_COMPLETED


def test_reprocess_failure_keeps_previous_good_result(session, run_worker, web_workspace,
                                                      web_fixtures, monkeypatch):
    job_id = _import(session, run_worker, web_fixtures["text_pdf"])
    doc_id = JobStore(web_workspace).get(job_id)["doc_id"]
    unit = web_workspace.units_for_doc(doc_id)[0]
    before = web_workspace.get_unit(unit["id"])["original_text"]

    def boom(image):
        raise RuntimeError("模拟引擎崩溃")

    monkeypatch.setattr(ocr, "recognize", boom)
    reprocess_id = session.post(f"/api/v1/documents/{doc_id}/reprocess", json={
        "unit_ids": [unit["id"]], "mode": "force", "keep_corrections": True,
    }).json()["job_ids"][0]
    run_worker()

    job = JobStore(web_workspace).get(reprocess_id)
    assert job["state"] == STATE_PARTIAL
    assert job["failed_pages"] == 1 and job["completed_pages"] == 0
    after = web_workspace.get_unit(unit["id"])
    assert after["original_text"] == before, "失败不能毁掉旧的可用结果"
    assert after["status"] == "completed"


def test_reprocess_rejects_units_from_other_documents(session, run_worker, web_workspace,
                                                       web_fixtures):
    first = _import(session, run_worker, web_fixtures["text_pdf"], "甲.pdf")
    second = _import(session, run_worker, web_fixtures["mixed_pdf"], "乙.pdf")
    store = JobStore(web_workspace)
    doc_a, doc_b = store.get(first)["doc_id"], store.get(second)["doc_id"]
    foreign = web_workspace.units_for_doc(doc_b)[0]["id"]
    response = session.post(f"/api/v1/documents/{doc_a}/reprocess",
                            json={"unit_ids": [foreign], "mode": "force"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_reprocess_requires_selected_pages(session, run_worker, web_workspace, web_fixtures):
    job_id = _import(session, run_worker, web_fixtures["text_pdf"])
    doc_id = JobStore(web_workspace).get(job_id)["doc_id"]
    response = session.post(f"/api/v1/documents/{doc_id}/reprocess",
                            json={"unit_ids": [], "mode": "force"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reprocess_rejects_non_force_mode(session, run_worker, web_workspace, web_fixtures):
    job_id = _import(session, run_worker, web_fixtures["text_pdf"])
    doc_id = JobStore(web_workspace).get(job_id)["doc_id"]
    unit_id = web_workspace.units_for_doc(doc_id)[0]["id"]
    response = session.post(f"/api/v1/documents/{doc_id}/reprocess",
                            json={"unit_ids": [unit_id], "mode": "auto"})
    assert response.status_code == 422


def test_job_units_endpoint_exposes_real_checkpoints(session, run_worker, web_workspace,
                                                     web_fixtures, monkeypatch):
    from litworkbench import ingest

    original = ingest.PdfPages.block_for

    def flaky(self, index, *, ocr_mode=None):
        if index == 0:
            raise RuntimeError("模拟第 1 页失败")
        return original(self, index, ocr_mode=ocr_mode)

    monkeypatch.setattr(ingest.PdfPages, "block_for", flaky)
    job_id = _import(session, run_worker, web_fixtures["text_pdf"])
    monkeypatch.setattr(ingest.PdfPages, "block_for", original)

    body = session.get(f"/api/v1/jobs/{job_id}/units").json()
    assert body["total"] == 3
    states = {item["position"]: item["state"] for item in body["items"]}
    assert states == {0: "failed", 1: "done", 2: "done"}
    errors = [item for item in body["items"] if item["state"] == "failed"]
    assert "模拟第 1 页失败" in errors[0]["error"]


def test_reprocess_of_blank_result_is_not_success(session, run_worker, web_workspace,
                                                   web_fixtures, monkeypatch):
    """强制 OCR 认不出字时不算成功，也不覆盖旧结果。"""
    job_id = _import(session, run_worker, web_fixtures["text_pdf"])
    doc_id = JobStore(web_workspace).get(job_id)["doc_id"]
    unit = web_workspace.units_for_doc(doc_id)[0]
    before = web_workspace.get_unit(unit["id"])["original_text"]

    monkeypatch.setattr(ocr, "recognize", lambda image: {
        "engine": "rapidocr", "lines": [], "text": "", "mean_confidence": 0.0,
        "render_width": image.width, "render_height": image.height})
    reprocess_id = session.post(f"/api/v1/documents/{doc_id}/reprocess", json={
        "unit_ids": [unit["id"]], "mode": "force",
    }).json()["job_ids"][0]
    run_worker()

    job = JobStore(web_workspace).get(reprocess_id)
    assert job["state"] == STATE_PARTIAL and job["failed_pages"] == 1
    assert web_workspace.get_unit(unit["id"])["original_text"] == before