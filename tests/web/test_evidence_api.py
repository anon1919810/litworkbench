"""单元详情、校订、摘录保存：并发哈希、精确切片、版本冲突与真实坐标。"""

from __future__ import annotations

import pytest

from litworkbench.errors import EditConflictError, QuotationError
from litworkbench.evidence import correct_unit, get_evidence, save_evidence
from litworkbench.jobs import JobStore
from litworkbench.store import sha256_text


@pytest.fixture
def imported(session, run_worker, web_workspace, web_fixtures):
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    assert response.status_code == 202
    job_id = response.json()["job_ids"][0]
    run_worker()
    doc_id = JobStore(web_workspace).get(job_id)["doc_id"]
    return web_workspace.get_document(doc_id)


# ------------------------------------------------------------------ 详情


def test_unit_detail_shape(imported, session):
    unit = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    assert set(detail) == {
        "unit_id", "doc_id", "title", "filename", "kind", "seq", "file_page",
        "printed_page", "printed_page_status", "title_path", "method", "status",
        "notes", "text", "original_text", "corrected_text", "version", "text_sha256",
        "content_revision", "ocr", "revisions",
    }
    assert detail["text_sha256"] == sha256_text(detail["text"])
    assert detail["version"] == "original"
    assert detail["ocr"] is None  # 文字页不伪造行框


def test_docx_unit_detail_has_no_fake_page_numbers(session, run_worker, web_workspace,
                                                    web_fixtures):
    response = session.upload([("files", ("样例.docx", web_fixtures["docx"].read_bytes(),
                                          "application/octet-stream"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    assert detail["file_page"] is None
    assert detail["printed_page"] == ""
    assert detail["kind"] in {"paragraph", "table"}


def test_units_pagination_and_document_filter(imported, session):
    body = session.get(f"/api/v1/documents/{imported['id']}/units?page=1&page_size=2").json()
    assert body["total"] == 3 and len(body["items"]) == 2
    assert set(body["items"][0]) >= {
        "id", "doc_id", "seq", "kind", "file_page", "printed_page", "status", "method",
        "title_path",
    }
    second = session.get(f"/api/v1/documents/{imported['id']}/units?page=2&page_size=2").json()
    assert len(second["items"]) == 1
    assert body["items"][0]["id"] != second["items"][0]["id"]


def test_source_download_uses_stable_id(imported, session, web_workspace):
    response = session.get(f"/api/v1/documents/{imported['id']}/source")
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"
    assert response.headers["content-disposition"].startswith("attachment")


def test_render_rejects_disallowed_scale(imported, session):
    unit = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]
    assert session.get(f"/api/v1/units/{unit['id']}/render?scale=1.7").status_code == 422
    assert session.get(f"/api/v1/units/{unit['id']}/render?scale=abc").status_code == 422


@pytest.mark.slow
def test_render_runs_in_isolated_process(imported, session):
    unit = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]
    response = session.get(f"/api/v1/units/{unit['id']}/render?scale=1.5")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert response.headers["x-render-scale"] == "1.5"


def test_docx_units_are_not_renderable(session, run_worker, web_workspace, web_fixtures):
    response = session.upload([("files", ("样例.docx", web_fixtures["docx"].read_bytes(),
                                          "application/octet-stream"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    result = session.get(f"/api/v1/units/{unit['id']}/render?scale=2")
    assert result.status_code == 422
    assert "Word" in result.json()["error"]["message"]


# ------------------------------------------------------------------ 校订


def test_correction_updates_text_and_revisions(imported, session):
    unit_id = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]["id"]
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    new_text = detail["text"].replace("北侧", "北郊")
    response = session.patch(f"/api/v1/units/{unit_id}/correction", json={
        "text": new_text, "reason": "校订地名", "expected_hash": detail["text_sha256"],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == new_text and body["version"] == "corrected"
    assert body["original_text"] == detail["original_text"]   # 原始识别不变
    assert body["text_sha256"] != detail["text_sha256"]
    assert body["revisions"][-1]["reason"] == "校订地名"


def test_correction_requires_reason(imported, session):
    unit_id = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]["id"]
    response = session.patch(f"/api/v1/units/{unit_id}/correction",
                             json={"text": "x", "reason": "   "})
    assert response.status_code == 422


def test_stale_expected_hash_returns_409_and_keeps_text(imported, session, web_workspace):
    unit_id = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]["id"]
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    response = session.patch(f"/api/v1/units/{unit_id}/correction", json={
        "text": "并发写入的草稿", "reason": "并发测试",
        "expected_hash": "0" * 64,
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EDIT_CONFLICT"
    assert web_workspace.get_unit(unit_id)["corrected_text"] is None
    assert web_workspace.get_unit(unit_id)["original_text"] == detail["original_text"]


def test_correction_hash_check_is_atomic_against_concurrent_write(imported, session,
                                                                   web_workspace):
    """读当前文字 -> 校验 -> 写入 必须在同一写事务里，不能被并发写入插队。"""
    from litworkbench.store import ensure_write_transaction

    unit_id = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]["id"]
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    stale_hash = detail["text_sha256"]

    # 先在别的事务里把文字改掉
    correct_unit(web_workspace, unit_id, "别人先写入的版本", "先到先得")

    with web_workspace.connect() as conn:
        ensure_write_transaction(conn)
        with pytest.raises(EditConflictError):
            correct_unit(web_workspace, unit_id, "我的草稿", "并发测试",
                         expected_hash=stale_hash, connection=conn)
        conn.rollback()

    assert web_workspace.get_unit(unit_id)["corrected_text"] == "别人先写入的版本"


# ------------------------------------------------------------------ 摘录


def test_evidence_requires_exact_slice_at_offset(imported, session, web_workspace):
    unit_id = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]["id"]
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    text = detail["text"]
    offset = text.index("白塔位于县城北侧")

    ok = session.post("/api/v1/evidence", json={
        "unit_id": unit_id, "quote": "白塔位于县城北侧", "start_offset": offset,
        "expected_hash": detail["text_sha256"], "category": "文物", "note": "页内定位",
    })
    assert ok.status_code == 201, ok.text
    body = ok.json()
    assert body["start_offset"] == offset
    assert text[body["start_offset"]:body["end_offset"]] == "白塔位于县城北侧"
    assert body["version"] == "original" and body["review_status"] == "pending"

    # 同样的引文，错误的偏移 -> 拒绝
    wrong = session.post("/api/v1/evidence", json={
        "unit_id": unit_id, "quote": "白塔位于县城北侧", "start_offset": offset + 1,
    })
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "QUOTATION_INVALID"


def test_evidence_strict_slice_rejects_whitespace_variant(imported, session, web_workspace):
    """显式偏移 + 精确切片：跨空白的近似引文不能保存（命令行宽松模式不受影响）。"""
    web_workspace.save_unit({
        "id": "doc_spaces_page_0001", "doc_id": imported["id"], "seq": 99, "kind": "page",
        "file_page": 99, "printed_page": "", "title_path": "", "method": "text",
        "status": "completed", "ocr_decision": "not_needed",
        "original_text": "第一行AB\nCD第二行", "corrected_text": None, "notes": "",
        "ocr_json": None,
    })
    start = "第一行AB\nCD第二行".index("AB")
    strict = session.post("/api/v1/evidence", json={
        "unit_id": "doc_spaces_page_0001", "quote": "AB CD", "start_offset": start,
    })
    assert strict.status_code == 422
    assert "偏移" in strict.json()["error"]["message"]

    # 精确切片可以保存
    exact = session.post("/api/v1/evidence", json={
        "unit_id": "doc_spaces_page_0001", "quote": "AB\nCD", "start_offset": start,
    })
    assert exact.status_code == 201
    assert exact.json()["quote"] == "AB\nCD"

    # 命令行保持原来的宽松定位行为
    from litworkbench.store import Workspace

    relaxed = save_evidence(Workspace(web_workspace.root), "doc_spaces_page_0001",
                            "AB CD", start_offset=start)
    assert relaxed["quote"] == "AB\nCD"


def test_evidence_rejects_stale_expected_hash(imported, session, web_workspace):
    unit_id = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]["id"]
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    response = session.post("/api/v1/evidence", json={
        "unit_id": unit_id, "quote": "白塔位于县城北侧",
        "start_offset": detail["text"].index("白塔位于县城北侧"),
        "expected_hash": "f" * 64,
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EDIT_CONFLICT"
    assert list_evidence_count(session, unit_id) == 0


def list_evidence_count(session, unit_id) -> int:
    body = session.get("/api/v1/evidence").json()
    return len([item for item in body["items"] if item["unit_id"] == unit_id])


def test_evidence_uses_corrected_version_after_correction(imported, session):
    unit_id = session.get(f"/api/v1/documents/{imported['id']}/units").json()["items"][0]["id"]
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    corrected = detail["text"].replace("北侧", "北郊")
    session.patch(f"/api/v1/units/{unit_id}/correction", json={
        "text": corrected, "reason": "校订", "expected_hash": detail["text_sha256"],
    })
    refreshed = session.get(f"/api/v1/units/{unit_id}").json()
    response = session.post("/api/v1/evidence", json={
        "unit_id": unit_id, "quote": "白塔位于县城北郊",
        "start_offset": refreshed["text"].index("白塔位于县城北郊"),
        "expected_hash": refreshed["text_sha256"],
    })
    assert response.status_code == 201
    assert response.json()["version"] == "corrected"


def test_evidence_list_filters_and_source(session, run_worker, web_workspace, web_fixtures):
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    created = session.post("/api/v1/evidence", json={
        "unit_id": unit["id"], "quote": "白塔位于县城北侧",
        "start_offset": detail["text"].index("白塔位于县城北侧"),
        "expected_hash": detail["text_sha256"], "category": "文物",
    }).json()

    listed = session.get("/api/v1/evidence").json()
    assert listed["total"] == 1
    item = listed["items"][0]
    assert set(item) == {"id", "unit_id", "doc_id", "quote", "start_offset", "end_offset",
                         "version", "category", "note", "review_status", "review_note",
                         "created_at", "source"}
    assert item["source"]["filename"] == "县志.pdf"
    assert item["source"]["file_page"] == 1
    assert "printed_page" in item["source"] and "title_path" in item["source"]

    assert session.get(f"/api/v1/evidence?doc_id={doc_id}").json()["total"] == 1
    assert session.get(f"/api/v1/evidence?doc_id=doc_other").json()["total"] == 0
    assert session.get("/api/v1/evidence?review_status=pending").json()["total"] == 1
    assert session.get("/api/v1/evidence?review_status=accepted").json()["total"] == 0

    patched = session.patch(f"/api/v1/evidence/{created['id']}",
                            json={"review_status": "accepted", "review_note": "人工看过",
                                  "note": "分析备注"})
    assert patched.status_code == 200
    body = patched.json()
    assert body["review_status"] == "accepted" and body["review_note"] == "人工看过"
    assert body["note"] == "分析备注"
    assert body["quote"] == created["quote"] and body["start_offset"] == created["start_offset"]


def test_evidence_patch_rejects_unknown_status(session, run_worker, web_workspace,
                                               web_fixtures):
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    created = session.post("/api/v1/evidence", json={
        "unit_id": unit["id"], "quote": "白塔位于县城北侧",
        "start_offset": detail["text"].index("白塔位于县城北侧"),
    }).json()
    bad = session.patch(f"/api/v1/evidence/{created['id']}", json={"review_status": "good"})
    assert bad.status_code == 422
    assert session.get(f"/api/v1/evidence?review_status=bogus").status_code == 422


def test_correction_marks_accepted_evidence_for_review(session, run_worker, web_workspace,
                                                        web_fixtures):
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    created = session.post("/api/v1/evidence", json={
        "unit_id": unit["id"], "quote": "白塔位于县城北侧",
        "start_offset": detail["text"].index("白塔位于县城北侧"),
    }).json()
    session.patch(f"/api/v1/evidence/{created['id']}", json={"review_status": "accepted"})

    session.patch(f"/api/v1/units/{unit['id']}/correction", json={
        "text": detail["text"].replace("北侧", "北郊"), "reason": "校订地名",
        "expected_hash": detail["text_sha256"],
    })
    refreshed = session.get(f"/api/v1/evidence/{created['id']}").json()
    assert refreshed["review_status"] == "stale"
    assert refreshed["quote"] == "白塔位于县城北侧"
    assert session.get("/api/v1/evidence?review_status=stale").json()["total"] == 1


def test_duplicate_import_does_not_delete_evidence(session, run_worker, web_workspace,
                                                    web_fixtures):
    data = web_fixtures["text_pdf"].read_bytes()
    session.upload([("files", ("县志.pdf", data, "application/pdf"))])
    run_worker()
    doc_id = web_workspace.list_documents()[0]["id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    created = session.post("/api/v1/evidence", json={
        "unit_id": unit["id"], "quote": "白塔位于县城北侧",
        "start_offset": detail["text"].index("白塔位于县城北侧"),
    }).json()

    session.upload([("files", ("县志 副本.pdf", data, "application/pdf"))])
    run_worker()
    assert len(web_workspace.list_documents()) == 1
    listed = session.get("/api/v1/evidence").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created["id"]
    assert web_workspace.revisions_for_unit(unit["id"]) is not None


def test_evidence_for_unknown_unit_is_404(session):
    response = session.post("/api/v1/evidence", json={
        "unit_id": "u_missing", "quote": "任意文字", "start_offset": 0,
    })
    assert response.status_code == 404


def test_evidence_quote_must_exist(session, run_worker, web_workspace, web_fixtures):
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    response = session.post("/api/v1/evidence", json={
        "unit_id": unit["id"], "quote": "完全不在原文里的一句话", "start_offset": 0,
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "QUOTATION_INVALID"


def test_save_evidence_is_visible_to_its_own_transaction(session, run_worker, web_workspace,
                                                          web_fixtures):
    """回归：写事务里插入的摘录必须能被同一次调用读到（不回滚）。"""
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    detail = session.get(f"/api/v1/units/{unit['id']}").json()
    created = session.post("/api/v1/evidence", json={
        "unit_id": unit["id"], "quote": "白塔位于县城北侧",
        "start_offset": detail["text"].index("白塔位于县城北侧"),
    })
    assert created.status_code == 201
    evidence_id = created.json()["id"]
    assert get_evidence(web_workspace, evidence_id)["id"] == evidence_id


def test_no_permanent_delete_endpoint(session):
    response = session.request("DELETE", "/api/v1/evidence/ev_anything",
                               headers=session.write_headers())
    assert response.status_code in (403, 404, 405)
    assert response.status_code != 204  # 永远不提供永久删除


def test_quoted_text_is_returned_as_text_not_html(imported, session, web_workspace):
    from litworkbench import search_index

    web_workspace.save_unit({
        "id": "doc_html_page_0001", "doc_id": imported["id"],
        "seq": 50, "kind": "page", "file_page": 50, "printed_page": "", "title_path": "",
        "method": "text", "status": "completed", "ocr_decision": "not_needed",
        "original_text": "<script>alert(1)</script> 白塔", "corrected_text": None,
        "notes": "", "ocr_json": None,
    })
    search_index.index_unit(web_workspace, "doc_html_page_0001")
    hit = session.post("/api/v1/search", json={"query": "白塔", "page": 1}).json()["items"][0]
    assert "<script>" in hit["snippet"]          # 原文原样返回（前端用文本节点渲染）
    assert json_escape_ok(hit)


def json_escape_ok(hit) -> bool:
    import json

    return json.loads(json.dumps(hit, ensure_ascii=False))["snippet"] == hit["snippet"]


def test_evidence_save_with_quotation_error_type(session, run_worker, web_workspace,
                                                  web_fixtures):
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    run_worker()
    doc_id = JobStore(web_workspace).get(response.json()["job_ids"][0])["doc_id"]
    unit = session.get(f"/api/v1/documents/{doc_id}/units").json()["items"][0]
    with pytest.raises(QuotationError):
        save_evidence(web_workspace, unit["id"], "不在原文的引文")