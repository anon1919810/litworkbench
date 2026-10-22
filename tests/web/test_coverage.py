"""覆盖统计：已索引文字页 + 人工确认空白页；未知页数不造假。"""

from __future__ import annotations

from litworkbench import search_index
from litworkbench.jobs import JobStore


def _make_unit(workspace, doc_id, seq, text, status="completed"):
    unit_id = f"{doc_id}_page_{seq:04d}"
    workspace.save_unit({
        "id": unit_id, "doc_id": doc_id, "seq": seq, "kind": "page", "file_page": seq,
        "printed_page": "", "title_path": "", "method": "text", "status": status,
        "ocr_decision": "not_needed", "original_text": text, "corrected_text": None,
        "notes": "", "ocr_json": None,
    })
    search_index.index_unit(workspace, unit_id)
    return unit_id


def _make_doc(workspace, doc_id, *, pages, filetype="pdf"):
    workspace.save_document({
        "id": doc_id, "sha256": doc_id.ljust(64, "0")[:64], "original_path": "x",
        "stored_path": f"sources/{doc_id}.pdf", "filename": f"{doc_id}.pdf",
        "filetype": filetype, "title": doc_id, "author": "", "year": "", "edition": "",
        "volume": "", "status": "completed", "ocr_mode": "off", "page_count": pages,
        "unit_count": 0, "pending_count": 0,
    })


def test_empty_workspace_coverage_is_zero(session):
    body = session.get("/api/v1/coverage").json()
    assert body["known_pdf_pages"] == 0 and body["covered_pdf_pages"] == 0
    assert body["pending_pdf_pages"] == 0 and body["failed_pdf_pages"] == 0
    assert body["unknown_documents"] == 0
    assert body["indexed_docx"] == 0 and body["total_docx"] == 0


def test_pending_and_failed_pages_are_not_covered(web_workspace):
    _make_doc(web_workspace, "doc_cov", pages=4)   # 4 页，但只落了 3 个单元
    _make_unit(web_workspace, "doc_cov", 1, "有文字的一页")
    _make_unit(web_workspace, "doc_cov", 2, "", status="pending")
    _make_unit(web_workspace, "doc_cov", 3, "识别失败的文字", status="failed")
    with web_workspace.connect() as conn:
        cover = search_index.coverage(conn)
    assert cover["known_pdf_pages"] == 4
    assert cover["covered_pdf_pages"] == 1      # 只有真正索引成功的文字页
    assert cover["pending_pdf_pages"] == 2       # 1 页待处理 + 1 页还没落地
    assert cover["failed_pdf_pages"] == 1
    assert cover["covered_pdf_pages"] + cover["pending_pdf_pages"] \
        + cover["failed_pdf_pages"] == 4


def test_failed_page_with_text_is_neither_covered_nor_searchable(web_workspace):
    """失败页即使有文字层文本，也不能算覆盖，也不能当命中。"""
    _make_doc(web_workspace, "doc_failed", pages=1)
    unit_id = _make_unit(web_workspace, "doc_failed", 1, "白塔在失败页里",
                         status="failed")
    with web_workspace.connect() as conn:
        assert search_index.coverage(conn)["covered_pdf_pages"] == 0
        assert search_index.searchable_pages(conn, "doc_failed") == 0
    result = search_index.search_indexed(web_workspace, "白塔")
    assert unit_id not in {item["unit_id"] for item in result["items"]}


def test_manual_blank_confirmation_counts_as_covered(session, web_workspace):
    _make_doc(web_workspace, "doc_blank", pages=2)
    _make_unit(web_workspace, "doc_blank", 1, "白塔在这里被提到")
    _make_unit(web_workspace, "doc_blank", 2, "", status="pending")
    detail = session.get(f"/api/v1/units/doc_blank_page_0002").json()
    assert detail["text"] == ""

    body = session.post("/api/v1/units/doc_blank_page_0002/confirm-blank")
    assert body.status_code == 200
    assert body.json()["text"] == ""
    cover = session.get("/api/v1/coverage").json()
    assert cover["covered_pdf_pages"] == 2   # 文字页 + 人工确认的空白页
    assert cover["pending_pdf_pages"] == 0
    # 空白页不会污染检索结果
    assert search_index.search_indexed(web_workspace, "白塔")["total"] == 1


def test_blank_confirmation_rejects_pages_with_text(session, web_workspace):
    _make_doc(web_workspace, "doc_notblank", pages=1)
    _make_unit(web_workspace, "doc_notblank", 1, "这里有白塔")
    response = session.post("/api/v1/units/doc_notblank_page_0001/confirm-blank")
    assert response.status_code == 422
    assert "不能确认为空白" in response.json()["error"]["message"]


def test_blank_confirmation_rejects_completed_page(session, web_workspace):
    _make_doc(web_workspace, "doc_done", pages=1)
    unit_id = _make_unit(web_workspace, "doc_done", 1, "", status="completed")
    response = session.post(f"/api/v1/units/{unit_id}/confirm-blank")
    assert response.status_code == 422


def test_recognised_but_not_yet_indexed_page_is_uncovered(session, web_workspace):
    """文字已经在库里、但索引任务还没跑：计入待处理而不是覆盖。"""
    _make_doc(web_workspace, "doc_dirty", pages=1)
    web_workspace.save_unit({
        "id": "doc_dirty_page_0001", "doc_id": "doc_dirty", "seq": 1, "kind": "page",
        "file_page": 1, "printed_page": "", "title_path": "", "method": "text",
        "status": "completed", "ocr_decision": "not_needed",
        "original_text": "白塔已识别但还没建索引", "corrected_text": None, "notes": "",
        "ocr_json": None,
    })
    with web_workspace.connect() as conn:
        cover = search_index.coverage(conn)
        assert cover["covered_pdf_pages"] == 0
        assert search_index.pending_count(conn) == 1

    search_index.index_unit(web_workspace, "doc_dirty_page_0001")
    with web_workspace.connect() as conn:
        assert search_index.coverage(conn)["covered_pdf_pages"] == 1
        assert search_index.pending_count(conn) == 0


def test_unknown_page_count_document_is_reported_not_faked(web_workspace):
    _make_doc(web_workspace, "doc_unknown", pages=None)
    with web_workspace.connect() as conn:
        cover = search_index.coverage(conn)
    assert cover["unknown_documents"] == 1
    assert cover["known_pdf_pages"] == 0      # 不知道就不假装知道


def test_docx_and_pdf_are_counted_separately(session, web_workspace):
    _make_doc(web_workspace, "doc_p", pages=2, filetype="pdf")
    _make_unit(web_workspace, "doc_p", 1, "PDF 有文字")
    _make_unit(web_workspace, "doc_p", 2, "PDF 第二页")
    _make_doc(web_workspace, "doc_w", pages=None, filetype="docx")
    web_workspace.save_unit({
        "id": "doc_w_para_0001", "doc_id": "doc_w", "seq": 1, "kind": "paragraph",
        "file_page": None, "printed_page": "", "title_path": "第一章", "method": "text",
        "status": "completed", "ocr_decision": "not_needed", "original_text": "Word 段落文字",
        "corrected_text": None, "notes": "", "ocr_json": None,
    })
    body = session.get("/api/v1/coverage").json()
    assert body["total_docx"] == 1 and body["indexed_docx"] == 0  # 还没建索引

    search_index.index_unit(web_workspace, "doc_w_para_0001")
    body = session.get("/api/v1/coverage").json()
    assert body["indexed_docx"] == 1
    assert body["known_pdf_pages"] == 2 and body["unknown_documents"] == 0


def test_correction_marks_page_uncovered_until_reindexed(session, web_workspace):
    _make_doc(web_workspace, "doc_corr", pages=1)
    unit_id = _make_unit(web_workspace, "doc_corr", 1, "白塔位于县城北侧")
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    response = session.patch(f"/api/v1/units/{unit_id}/correction", json={
        "text": "黑塔位于县城北侧", "reason": "校订", "expected_hash": detail["text_sha256"],
    })
    assert response.status_code == 200
    cover = session.get("/api/v1/coverage").json()
    # 接口保存后立即重建索引，覆盖不会长期缺一页
    assert cover["covered_pdf_pages"] == 1
    hits = session.post("/api/v1/search", json={"query": "白塔", "page": 1}).json()
    assert unit_id not in {item["unit_id"] for item in hits["items"]}


def test_index_job_rebuilds_pending_units(session, web_workspace, run_worker):
    _make_doc(web_workspace, "doc_job", pages=2)
    web_workspace.save_unit({
        "id": "doc_job_page_0001", "doc_id": "doc_job", "seq": 1, "kind": "page",
        "file_page": 1, "printed_page": "", "title_path": "", "method": "text",
        "status": "completed", "ocr_decision": "not_needed", "original_text": "白塔在这里",
        "corrected_text": None, "notes": "", "ocr_json": None,
    })
    job = JobStore(web_workspace).enqueue_index()
    assert job is not None
    run_worker()
    done = JobStore(web_workspace).get(job["id"])
    assert done["state"] == "completed" and done["completed_pages"] == 1
    assert session.get("/api/v1/coverage").json()["covered_pdf_pages"] == 1
    # 没有脏单元时不再排队
    assert JobStore(web_workspace).enqueue_index() is None