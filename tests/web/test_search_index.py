"""中文索引检索：码点 token、跨行短语、标点、分页、代次 409、校订后不再命中。"""

from __future__ import annotations

import pytest

from litworkbench import search_index
from litworkbench.jobs import JobStore
from litworkbench.search_index import IndexRevisionChanged


def _import(session, run_worker, path, filename=None):
    response = session.upload([("files", (filename or path.name, path.read_bytes(),
                                          "application/pdf"))])
    assert response.status_code == 202, response.text
    run_worker()
    return response.json()["job_ids"][0]


def _search(session, query, **kwargs):
    payload = {"query": query, "page": 1, "page_size": 30}
    payload.update(kwargs)
    return session.post("/api/v1/search", json=payload)


@pytest.fixture
def library(session, run_worker, web_fixtures, web_workspace):
    """导入文字版 PDF 与 DOCX，索引建立完成。"""
    for name, path in (("text_pdf", web_fixtures["text_pdf"]),
                       ("docx", web_fixtures["docx"])):
        response = session.upload([("files", (name + path.suffix, path.read_bytes(),
                                              "application/octet-stream"))])
        assert response.status_code == 202
        run_worker()
    return web_workspace


def test_encode_characters_is_codepoint_positions():
    assert search_index.encode_characters("灯 会\n") == "u0070 灯".replace(
        "u0070 灯", "u00706f u004f1a")
    assert search_index.encode_characters("灯会") == "u00706f u004f1a"
    # 标点同样有明确索引路径
    assert "u00ff0c" in search_index.encode_characters("，")


def test_single_and_double_character_queries(library, session):
    single = _search(session, "塔").json()
    assert single["total"] >= 3
    double = _search(session, "白塔").json()
    assert double["total"] >= 3
    # 两字查询命中的一定也含单字
    single_units = {item["unit_id"] for item in single["items"]}
    double_units = {item["unit_id"] for item in double["items"]}
    assert double_units <= single_units
    assert all(item["start_offset"] >= 0 and item["end_offset"] > item["start_offset"]
               for item in single["items"])


def test_phrase_across_whitespace_and_newlines(library, session):
    """第 3 页把「白塔建于道光/十四年」拆成两行，跨行短语仍可检索。"""
    body = _search(session, "白塔建于道光十四年").json()
    assert body["total"] == 1
    hit = body["items"][0]
    assert hit["file_page"] == 3
    assert hit["start_offset"] > 0
    text = library.get_unit(hit["unit_id"])["original_text"]
    # 偏移落在真实原文的码点上
    assert "白塔建于道光十四年" in text.replace("\n", "")


def test_punctuation_and_symbol_queries(library, session):
    assert _search(session, "%，").status_code == 200
    assert _search(session, "%").json()["total"] >= 0
    assert _search(session, "_").status_code == 200
    assert _search(session, '"').status_code == 200


def test_modes_all_terms_and_any_terms(session, web_workspace):
    saved = web_workspace.save_document({
        "id": "doc_manual", "sha256": "0" * 64, "original_path": "x",
        "stored_path": "sources/x.pdf", "filename": "手写件.pdf", "filetype": "pdf",
        "title": "手写件", "author": "", "year": "", "edition": "", "volume": "",
        "status": "completed", "ocr_mode": "off", "page_count": 1,
        "unit_count": 0, "pending_count": 0,
    })
    web_workspace.save_unit({
        "id": "doc_manual_page_0001", "doc_id": "doc_manual", "seq": 1, "kind": "page",
        "file_page": 1, "printed_page": "", "title_path": "", "method": "text",
        "status": "completed", "ocr_decision": "not_needed",
        "original_text": "白塔在北侧，残碑另存。", "corrected_text": None, "notes": "",
        "ocr_json": None,
    })
    search_index.index_unit(web_workspace, "doc_manual_page_0001")

    assert _search(session, "白塔 残碑", mode="all_terms").json()["total"] == 1
    assert _search(session, "白塔 无此词", mode="all_terms").json()["total"] == 0
    assert _search(session, "白塔 无此词", mode="any_terms").json()["total"] == 1
    assert _search(session, "白 碑", mode="phrase").json()["total"] == 0


def test_empty_query_is_rejected_with_chinese_message(session):
    response = _search(session, "   ")
    assert response.status_code == 422
    assert "检索词" in response.json()["error"]["message"]


def test_doc_ids_scope_limits_results(session, run_worker, web_fixtures, web_workspace):
    first = session.upload([("files", ("甲.pdf", web_fixtures["text_pdf"].read_bytes(),
                                       "application/pdf"))]).json()["job_ids"][0]
    run_worker()
    second = session.upload([("files", ("乙.pdf", web_fixtures["mixed_pdf"].read_bytes(),
                                        "application/pdf"))]).json()["job_ids"][0]
    run_worker()
    store = JobStore(web_workspace)
    doc_a, doc_b = store.get(first)["doc_id"], store.get(second)["doc_id"]
    scoped = _search(session, "白塔", doc_ids=[doc_a]).json()
    assert scoped["total"] > 0
    assert {item["doc_id"] for item in scoped["items"]} == {doc_a}
    other = _search(session, "白塔", doc_ids=[doc_b]).json()
    assert all(item["doc_id"] == doc_b for item in other["items"])


def test_pagination_returns_true_total_and_stable_order(session, web_workspace):
    saved = web_workspace.save_document({
        "id": "doc_many", "sha256": "1" * 64, "original_path": "x",
        "stored_path": "sources/y.pdf", "filename": "长文档.pdf", "filetype": "pdf",
        "title": "长文档", "author": "", "year": "", "edition": "", "volume": "",
        "status": "completed", "ocr_mode": "off", "page_count": 40,
        "unit_count": 0, "pending_count": 0,
    })
    for page in range(1, 41):
        unit_id = f"doc_many_page_{page:04d}"
        web_workspace.save_unit({
            "id": unit_id, "doc_id": saved["id"], "seq": page, "kind": "page", "file_page": page,
            "printed_page": "", "title_path": "", "method": "text", "status": "completed",
            "ocr_decision": "not_needed",
            "original_text": f"第 {page} 页记载白塔相关内容。", "corrected_text": None,
            "notes": "", "ocr_json": None,
        })
        search_index.index_unit(web_workspace, unit_id)

    first = _search(session, "白塔", page=1, page_size=30).json()
    assert first["total"] == 40 and len(first["items"]) == 30
    second = _search(session, "白塔", page=2, page_size=30).json()
    assert second["total"] == 40 and len(second["items"]) == 10
    ids = [item["unit_id"] for item in first["items"] + second["items"]]
    assert len(set(ids)) == 40  # 翻页能取全，不重不漏
    assert ids == sorted(ids, key=lambda value: int(value.rsplit("_", 1)[1]))


def test_expected_index_revision_conflict_returns_409(session, library):
    first = _search(session, "白塔").json()
    revision = first["index_revision"]
    assert _search(session, "白塔", expected_index_revision=revision).status_code == 200

    unit = library.units_for_doc(library.list_documents()[0]["id"])[0]
    session.patch(f"/api/v1/units/{unit['id']}/printed-page",
                  json={"printed_page": "775", "status": "candidate"})

    stale = _search(session, "白塔", expected_index_revision=revision)
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "SEARCH_INDEX_CHANGED"
    assert error["details"]["current_revision"] > revision
    assert "coverage" in error["details"]


def test_revision_conflict_is_raised_as_exception_for_core_callers(library):
    with library.connect() as conn:
        conn.execute("BEGIN")
        revision = search_index.current_revision(conn)
        conn.commit()
    with pytest.raises(IndexRevisionChanged):
        search_index.search_indexed(library, "白塔", expected_index_revision=revision - 1)


def test_correction_removes_old_text_hits_and_updates_index(session, library):
    before = _search(session, "白塔").json()
    assert before["total"] >= 1
    unit_id = before["items"][0]["unit_id"]
    detail = session.get(f"/api/v1/units/{unit_id}").json()
    new_text = detail["text"].replace("白塔", "黑塔")
    response = session.patch(f"/api/v1/units/{unit_id}/correction", json={
        "text": new_text, "reason": "校订塔名", "expected_hash": detail["text_sha256"],
    })
    assert response.status_code == 200
    assert response.json()["version"] == "corrected"

    after = _search(session, "白塔", doc_ids=[detail["doc_id"]]).json()
    assert unit_id not in {item["unit_id"] for item in after["items"]}
    fresh = _search(session, "黑塔", doc_ids=[detail["doc_id"]]).json()
    assert unit_id in {item["unit_id"] for item in fresh["items"]}
    hit = next(item for item in fresh["items"] if item["unit_id"] == unit_id)
    assert hit["version"] == "corrected"
    assert hit["text_sha256"] == response.json()["text_sha256"]


def test_dirty_mark_bumps_revision_atomically(library):
    """把单元标脏会同时改变可检索结果，因此必须同一事务增加代次。"""
    with library.connect() as conn:
        conn.execute("BEGIN")
        before = search_index.current_revision(conn)
        conn.commit()
    unit = library.units_for_doc(library.list_documents()[0]["id"])[0]
    with library.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        search_index.mark_dirty(conn, unit["id"])
        after = search_index.current_revision(conn)
        conn.commit()
    assert after == before + 1


def test_printed_page_change_bumps_revision_without_dirtying_index(session, library):
    """页码会出现在结果里：必须换代次，但文字索引保持有效（不重扫、不掉覆盖）。"""
    doc_id = next(doc["id"] for doc in library.list_documents() if doc["filetype"] == "pdf")
    unit = library.units_for_doc(doc_id)[0]
    with library.connect() as conn:
        conn.execute("BEGIN")
        revision = search_index.current_revision(conn)
        pending = search_index.pending_count(conn)
        searchable = search_index.searchable_pages(conn, doc_id)
        conn.commit()
    session.patch(f"/api/v1/units/{unit['id']}/printed-page",
                  json={"printed_page": "775", "status": "confirmed"})
    with library.connect() as conn:
        conn.execute("BEGIN")
        assert search_index.current_revision(conn) > revision   # 结果变了 -> 换代次
        assert search_index.pending_count(conn) == pending      # 文字索引没被标脏
        assert search_index.searchable_pages(conn, doc_id) == searchable
        conn.commit()
    refreshed = session.get(f"/api/v1/units/{unit['id']}").json()
    assert refreshed["printed_page"] == "775"
    assert refreshed["printed_page_status"] == "confirmed"
    assert refreshed["file_page"] == 1  # 文件页序与印刷页码分开
    hit = _search(session, "白塔", doc_ids=[doc_id]).json()["items"][0]
    assert hit["printed_page"] == "775"


def test_cli_style_printed_page_write_does_not_dirty_index(library):
    """``lit label`` 走 save_unit 全列 upsert：没有 WHEN 触发器就会误标脏。"""
    doc_id = next(doc["id"] for doc in library.list_documents() if doc["filetype"] == "pdf")
    unit = library.units_for_doc(doc_id)[0]
    with library.connect() as conn:
        conn.execute("BEGIN")
        pending = search_index.pending_count(conn)
        searchable = search_index.searchable_pages(conn, doc_id)
        conn.commit()
    library.set_printed_page(unit["id"], "317")
    with library.connect() as conn:
        conn.execute("BEGIN")
        assert search_index.pending_count(conn) == pending
        assert search_index.searchable_pages(conn, doc_id) == searchable
        conn.commit()
    assert library.get_unit(unit["id"])["printed_page"] == "317"


def test_repeated_occurrence_reports_the_exact_offset(session, web_workspace):
    saved = web_workspace.save_document({
        "id": "doc_rep", "sha256": "2" * 64, "original_path": "x",
        "stored_path": "sources/z.pdf", "filename": "重复引文.pdf", "filetype": "pdf",
        "title": "重复引文", "author": "", "year": "", "edition": "", "volume": "",
        "status": "completed", "ocr_mode": "off", "page_count": 1,
        "unit_count": 0, "pending_count": 0,
    })
    text = "白塔记述。白塔记述。"
    unit_id = "doc_rep_page_0001"
    web_workspace.save_unit({
        "id": unit_id, "doc_id": saved["id"], "seq": 1, "kind": "page", "file_page": 1,
        "printed_page": "", "title_path": "", "method": "text", "status": "completed",
        "ocr_decision": "not_needed", "original_text": text, "corrected_text": None,
        "notes": "", "ocr_json": None,
    })
    search_index.index_unit(web_workspace, unit_id)

    first = session.post("/api/v1/evidence", json={
        "unit_id": unit_id, "quote": "白塔记述", "start_offset": text.index("白塔记述"),
    })
    assert first.status_code == 201
    second = session.post("/api/v1/evidence", json={
        "unit_id": unit_id, "quote": "白塔记述", "start_offset": text.rindex("白塔记述"),
    })
    assert second.status_code == 201
    assert first.json()["start_offset"] == 0
    assert second.json()["start_offset"] == text.rindex("白塔记述")
    assert first.json()["end_offset"] == 4
    assert second.json()["end_offset"] == text.rindex("白塔记述") + 4
    # 保存的引文就是该偏移处的精确切片
    assert web_workspace.get_unit(unit_id)["original_text"][
        second.json()["start_offset"]:second.json()["end_offset"]] == "白塔记述"


def test_search_never_falls_back_to_like_fullscan(session, web_workspace, monkeypatch):
    """索引被清空后，检索必须返回 0 条，而不是偷偷扫原文。"""
    saved = web_workspace.save_document({
        "id": "doc_like", "sha256": "3" * 64, "original_path": "x",
        "stored_path": "sources/l.pdf", "filename": "不索引.pdf", "filetype": "pdf",
        "title": "不索引", "author": "", "year": "", "edition": "", "volume": "",
        "status": "completed", "ocr_mode": "off", "page_count": 1,
        "unit_count": 0, "pending_count": 0,
    })
    web_workspace.save_unit({
        "id": "doc_like_page_0001", "doc_id": saved["id"], "seq": 1, "kind": "page", "file_page": 1,
        "printed_page": "", "title_path": "", "method": "text", "status": "completed",
        "ocr_decision": "not_needed", "original_text": "白塔就在此处被提到", "corrected_text": None,
        "notes": "", "ocr_json": None,
    })

    def explode(*args, **kwargs):  # pragma: no cover - 只要被调用就说明退化了
        raise AssertionError("检索退化为 LIKE 全扫描")

    monkeypatch.setattr("litworkbench.text.like_pattern", explode)
    assert _search(session, "白塔").json()["total"] == 0
    search_index.index_unit(web_workspace, "doc_like_page_0001")
    assert _search(session, "白塔").json()["total"] == 1