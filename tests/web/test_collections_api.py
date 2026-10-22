"""集合、分类/集合筛选与批量整理。

覆盖：名称与说明校验、跨页大数据（65 条）、组合筛选取交集、
批量幂等、未知 ID 整批回滚、原摘录与校订历史保留、旧库迁移。
"""

from __future__ import annotations

import sqlite3

import pytest

from litworkbench import collections as core
from litworkbench import migrations
from litworkbench.evidence import correct_unit, get_evidence, review_evidence
from litworkbench.store import sha256_text, utcnow


@pytest.fixture
def seeded(session, run_worker, web_workspace, web_fixtures):
    """导入一份合成 PDF，建立若干单元与摘录。"""
    response = session.upload([("files", ("县志.pdf", web_fixtures["text_pdf"].read_bytes(),
                                         "application/pdf"))])
    assert response.status_code == 202
    run_worker()
    doc_id = web_workspace.list_documents()[0]["id"]
    units = web_workspace.units_for_doc(doc_id)
    return {"doc_id": doc_id, "units": units}


def make_evidence(workspace, unit, index: int, *, category: str = "", note: str = "") -> str:
    """直接写入一条摘录（避开 HTTP，批量数据准备更快）。"""
    text = unit["text"]
    quote = text[0:4]
    evidence_id = f"ev_seed_{index:04d}"
    snapshot = {
        "version": "original",
        "unit_text_sha256": sha256_text(text),
        "unit_text_length": len(text),
        "method": "text",
        "captured_at": utcnow(),
    }
    import json

    with workspace.connect() as conn:
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, task_id, quote, start_offset, "
            "end_offset, version, version_snapshot, category, note, review_status, "
            "review_note, created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, 0, 4, 'original', ?, ?, '', 'pending', '', ?, ?)",
            (evidence_id, unit["id"], unit["doc_id"], quote,
             json.dumps(snapshot, ensure_ascii=False), category, utcnow(), utcnow()),
        )
    return evidence_id


@pytest.fixture
def many(session, seeded, web_workspace):
    """65 条跨分页（每页 30）的摘录 + 1 个集合。

    摘录挂在真实存在的 3 个单元上：一条摘录只是原文的一个精确切片，
    同一页可以有多条摘录，页数与摘录数本来就不是一一对应。
    """
    units = seeded["units"]
    ids = [make_evidence(web_workspace, units[index % len(units)], index)
           for index in range(65)]
    collection = session.post("/api/v1/collections",
                              json={"name": "文物建筑资料", "description": "合成测试"}).json()
    return {"doc_id": seeded["doc_id"], "ids": ids, "collection": collection,
            "units": units}


# ------------------------------------------------------------------ 集合 CRUD


def test_create_list_and_shape(session):
    response = session.post("/api/v1/collections", json={"name": "  文物建筑资料  ",
                                                         "description": "待核对"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "文物建筑资料"          # 名称 trim
    assert set(body) == {"id", "name", "description", "evidence_count", "created_at",
                         "updated_at"}
    assert body["evidence_count"] == 0
    listed = session.get("/api/v1/collections")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [body["id"]]


@pytest.mark.parametrize("payload,fragment", [
    ({"name": "   "}, "不能为空"),
    ({"name": "长" * 81}, "最多 80"),
    ({"name": "含控制字符\x01"}, "控制字符"),
    ({"name": "说明", "description": "长" * 1001}, "最多 1000"),
])
def test_collection_validation(session, payload, fragment):
    response = session.post("/api/v1/collections", json=payload)
    assert response.status_code == 422
    assert fragment in response.json()["error"]["message"]


def test_duplicate_name_conflicts(session):
    session.post("/api/v1/collections", json={"name": "重复集"})
    again = session.post("/api/v1/collections", json={"name": " 重复集 "})
    assert again.status_code == 422
    assert "已存在同名集合" in again.json()["error"]["message"]


def test_rename_and_describe(session):
    created = session.post("/api/v1/collections", json={"name": "旧名字"}).json()
    patched = session.patch(f"/api/v1/collections/{created['id']}",
                            json={"name": "新名字", "description": "已整理"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "新名字"
    assert patched.json()["description"] == "已整理"
    # 改名后旧名可以被再次使用
    assert session.post("/api/v1/collections", json={"name": "旧名字"}).status_code == 201
    # 改成别人的名字仍然冲突
    assert session.patch(f"/api/v1/collections/{created['id']}",
                         json={"name": "旧名字"}).status_code == 422
    # 改成自己的名字是允许的（幂等改名，不是冲突）
    assert session.patch(f"/api/v1/collections/{created['id']}",
                         json={"name": "新名字"}).status_code == 200
    assert session.patch(f"/api/v1/collections/{created['id']}", json={}).status_code == 422
    assert session.patch("/api/v1/collections/col_missing", json={"name": "x"}).status_code == 404
    # 未知字段被拒绝
    assert session.patch(f"/api/v1/collections/{created['id']}",
                         json={"evidence_count": 9}).status_code == 422


def test_collections_are_never_deleted(session, many):
    collection_id = many["collection"]["id"]
    session.post("/api/v1/evidence/batch", json={
        "evidence_ids": many["ids"][:3], "add_to_collection_id": collection_id,
    })
    response = session.request("DELETE", f"/api/v1/collections/{collection_id}",
                               headers=session.write_headers())
    assert response.status_code in (403, 404, 405, 422)
    assert session.get("/api/v1/collections").json()["items"][0]["id"] == collection_id
    assert session.get("/api/v1/collections").json()["items"][0]["evidence_count"] == 3


def test_collections_require_session_and_csrf(app):
    """新会话之外不能读集合；写操作必须带正确 CSRF 且同源。"""
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://127.0.0.1:8765") as fresh:
        assert fresh.get("/api/v1/collections").status_code == 401
        token = fresh.get("/api/v1/workspace").json()["csrf_token"]
        assert fresh.get("/api/v1/collections").status_code == 200
        cross_site = fresh.post("/api/v1/collections", json={"name": "跨站"},
                                headers={"X-CSRF-Token": token,
                                         "Origin": "http://evil.example"})
        assert cross_site.status_code == 403
        assert cross_site.json()["error"]["code"] == "ORIGIN_REJECTED"
        no_csrf = fresh.post("/api/v1/collections", json={"name": "无令牌"})
        assert no_csrf.status_code == 403
        assert no_csrf.json()["error"]["code"] == "CSRF_INVALID"
        assert fresh.get("/api/v1/collections").json()["items"] == []   # 两次都没写入


# ------------------------------------------------------------------ 筛选


def test_collection_and_category_filters(session, seeded, web_workspace):
    unit_a, unit_b = seeded["units"][0], seeded["units"][1]
    first = make_evidence(web_workspace, unit_a, 1, category="文物")
    second = make_evidence(web_workspace, unit_b, 2, category="古迹")
    third = make_evidence(web_workspace, unit_b, 3, category="文物")
    collection = session.post("/api/v1/collections", json={"name": "集甲"}).json()
    other = session.post("/api/v1/collections", json={"name": "集乙"}).json()
    session.post("/api/v1/evidence/batch", json={
        "evidence_ids": [first, third], "add_to_collection_id": collection["id"],
    })
    session.post("/api/v1/evidence/batch", json={
        "evidence_ids": [third], "add_to_collection_id": other["id"],
    })

    assert session.get(f"/api/v1/evidence?collection_id={collection['id']}").json()["total"] == 2
    assert session.get("/api/v1/evidence?category=文物").json()["total"] == 2
    # 各条件取交集
    intersection = session.get(
        f"/api/v1/evidence?collection_id={collection['id']}&category=文物"
    ).json()
    assert intersection["total"] == 2
    assert session.get(
        f"/api/v1/evidence?collection_id={other['id']}&category=文物"
    ).json()["total"] == 1
    assert session.get(
        f"/api/v1/evidence?collection_id={collection['id']}&category=古迹"
    ).json()["total"] == 0
    assert session.get("/api/v1/evidence?collection_id=col_missing").json()["total"] == 0


def test_filters_still_paginate_across_65_items(session, many):
    page1 = session.get("/api/v1/evidence?page=1&page_size=30").json()
    page3 = session.get("/api/v1/evidence?page=3&page_size=30").json()
    assert page1["total"] == 65 and len(page1["items"]) == 30
    assert page3["total"] == 65 and len(page3["items"]) == 5
    ids = {item["id"] for item in page1["items"]} | {item["id"] for item in page3["items"]}
    assert len(ids) == 35

    session.post("/api/v1/evidence/batch", json={
        "evidence_ids": many["ids"], "add_to_collection_id": many["collection"]["id"],
    })
    filtered = session.get(
        f"/api/v1/evidence?collection_id={many['collection']['id']}&page_size=100"
    ).json()
    assert filtered["total"] == 65
    assert {item["id"] for item in filtered["items"]} == set(many["ids"])


# ------------------------------------------------------------------ 批量


def test_batch_is_idempotent_and_reports_real_counts(session, many):
    collection_id = many["collection"]["id"]
    ids = many["ids"][:5]
    first = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": ids, "category": "文物", "add_to_collection_id": collection_id,
    })
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["updated"] == 5 and body["added"] == 5 and body["removed"] == 0
    assert body["evidence_ids"] == ids

    again = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": ids, "add_to_collection_id": collection_id,
    }).json()
    assert again["added"] == 0                    # 重复加入幂等
    assert again["updated"] == 0                  # 没有给分类时不改分类
    assert get_evidence(many_ws(session), ids[0])["category"] == "文物"

    listed = session.get(f"/api/v1/collections").json()["items"][0]
    assert listed["evidence_count"] == 5


def many_ws(session):
    from litworkbench.store import Workspace

    return Workspace(session.client.app.state.workspace.root)


def test_batch_can_clear_category_and_remove_membership(session, many):
    collection_id = many["collection"]["id"]
    ids = many["ids"][:4]
    session.post("/api/v1/evidence/batch", json={
        "evidence_ids": ids, "category": "临时", "add_to_collection_id": collection_id,
    })
    cleared = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": ids[:2], "category": "",
    }).json()
    assert cleared["updated"] == 2
    assert [get_evidence(many_ws(session), value)["category"] for value in ids[:2]] == ["", ""]
    assert get_evidence(many_ws(session), ids[2])["category"] == "临时"

    removed = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": ids, "remove_from_collection_id": collection_id,
    }).json()
    assert removed["removed"] == 4
    assert session.get(f"/api/v1/collections").json()["items"][0]["evidence_count"] == 0
    # 移出集合不删除摘录
    assert session.get("/api/v1/evidence?page_size=100").json()["total"] == 65


def test_batch_requires_at_least_one_operation(session, many):
    response = session.post("/api/v1/evidence/batch", json={"evidence_ids": many["ids"][:1]})
    assert response.status_code == 422
    assert "至少选择一种操作" in response.json()["error"]["message"]


@pytest.mark.parametrize("payload,fragment", [
    ({"evidence_ids": []}, "0 条"),
    ({}, "0 条"),                       # 字段缺省同样是「没选」
    ({"evidence_ids": ["ev_seed_0000", ""]}, "非空"),
])
def test_batch_rejects_empty_selection(session, many, payload, fragment):
    response = session.post("/api/v1/evidence/batch", json={**payload, "category": "文物"})
    assert response.status_code == 422
    assert fragment in response.json()["error"]["message"]


def test_batch_rejects_null_selection(session, many):
    """null 不是空数组：同样必须被拒绝，且不能退化成「全部」。"""
    response = session.post("/api/v1/evidence/batch",
                            json={"evidence_ids": None, "category": "文物"})
    assert response.status_code == 422
    assert get_evidence(many_ws(session), many["ids"][0])["category"] == ""


def test_batch_rejects_more_than_500_raw_ids(session, many, web_workspace):
    ids = [f"ev_pad_{i:04d}" for i in range(501)]
    response = session.post("/api/v1/evidence/batch",
                            json={"evidence_ids": ids, "category": "文物"})
    assert response.status_code == 422
    assert "500" in response.json()["error"]["message"]
    # 上限按原始条目数判断：重复 ID 也不能绕过
    duplicates = [many["ids"][0]] * 501
    assert session.post("/api/v1/evidence/batch",
                        json={"evidence_ids": duplicates, "category": "文物"}
                        ).status_code == 422


def test_batch_deduplicates_ids_by_first_occurrence(session, many, web_workspace):
    ids = many["ids"][:3]
    payload = {"evidence_ids": [ids[2], ids[0], ids[2], ids[1], ids[0]], "category": "文物"}
    body = session.post("/api/v1/evidence/batch", json=payload).json()
    assert body["evidence_ids"] == [ids[2], ids[0], ids[1]]
    assert body["updated"] == 3


def test_unknown_id_rolls_back_everything(session, many, web_workspace):
    collection_id = many["collection"]["id"]
    ids = many["ids"][:6]
    response = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": [*ids, "ev_not_exists"],
        "category": "应该被回滚",
        "add_to_collection_id": collection_id,
    })
    assert response.status_code == 404
    assert "没有任何改动" in response.json()["error"]["message"]
    # 整批回滚：分类与成员关系一个都没写入
    for value in ids:
        assert get_evidence(web_workspace, value)["category"] == ""
    assert session.get("/api/v1/collections").json()["items"][0]["evidence_count"] == 0

    # 未知集合同样整批拒绝
    bad_collection = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": ids, "category": "仍然被回滚", "add_to_collection_id": "col_missing",
    })
    assert bad_collection.status_code == 404
    for value in ids:
        assert get_evidence(web_workspace, value)["category"] == ""


def test_batch_keeps_original_excerpt_and_revisions(session, many, web_workspace):
    unit = many["units"][0]
    evidence_id = many["ids"][0]
    before = get_evidence(web_workspace, evidence_id)
    revisions_before = web_workspace.revisions_for_unit(unit["id"])
    detail_before = web_workspace.get_unit(unit["id"])

    response = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": [evidence_id], "category": "文物", "add_to_collection_id":
            many["collection"]["id"],
    })
    assert response.status_code == 200

    after = get_evidence(web_workspace, evidence_id)
    assert after["quote"] == before["quote"]
    assert (after["start_offset"], after["end_offset"]) == (before["start_offset"],
                                                            before["end_offset"])
    assert after["version"] == before["version"]
    assert after["review_status"] == "pending"
    assert web_workspace.get_unit(unit["id"])["original_text"] == detail_before["original_text"]
    assert web_workspace.revisions_for_unit(unit["id"]) == revisions_before
    assert web_workspace.get_unit(unit["id"])["corrected_text"] is None


def test_batch_has_no_review_shortcut(session, many):
    ids = many["ids"][:3]
    response = session.post("/api/v1/evidence/batch", json={
        "evidence_ids": ids, "category": "文物", "review_status": "accepted",
    })
    assert response.status_code == 422      # 未知字段被拒绝
    for value in ids:
        assert get_evidence(many_ws(session), value)["review_status"] == "pending"


def test_correction_after_batch_still_marks_stale(session, seeded, web_workspace):
    unit = seeded["units"][0]
    evidence_id = make_evidence(web_workspace, unit, 7)
    session.post("/api/v1/evidence/batch", json={"evidence_ids": [evidence_id],
                                                 "category": "文物"})
    review_evidence(web_workspace, evidence_id, "accepted")
    correct_unit(web_workspace, unit["id"], unit["text"] + "（已校订）", "补字")
    refreshed = get_evidence(web_workspace, evidence_id)
    assert refreshed["review_status"] == "stale"
    assert refreshed["category"] == "文物"
    assert refreshed["quote"] == unit["text"][0:4]


# ------------------------------------------------------------------ 核心层与迁移


def test_core_helpers_are_reusable(web_workspace):
    created = core.create_collection(web_workspace, "核心层", "说明")
    assert core.get_collection(web_workspace, created["id"])["name"] == "核心层"
    core.update_collection(web_workspace, created["id"], description="改过的说明")
    assert core.get_collection(web_workspace, created["id"])["description"] == "改过的说明"
    assert [item["id"] for item in core.list_collections(web_workspace)] == [created["id"]]
    with pytest.raises(Exception):
        core.update_collection(web_workspace, created["id"])


def test_migration_adds_tables_and_keeps_evidence(tmp_path):
    """旧库（schema 5）升级后集合表可用，旧摘录与校订历史原样保留。"""
    from litworkbench.store import Workspace

    root = tmp_path / "旧 集合 工作区"
    workspace = Workspace(root)
    workspace.initialize()
    with workspace.connect() as conn:
        for table in ("collections", "collection_evidence", "exports"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT INTO documents (id, sha256, original_path, stored_path, filename, "
            "filetype, status, ocr_mode, imported_at, updated_at) "
            "VALUES ('doc_old', :sha, 'x', 'sources/x.pdf', '旧.pdf', 'pdf', 'completed', "
            "'off', :now, :now)", {"sha": "e" * 64, "now": utcnow()})
        conn.execute(
            "INSERT INTO units (id, doc_id, seq, kind, file_page, method, status, "
            "ocr_decision, original_text, corrected_text, created_at, updated_at) "
            "VALUES ('doc_old_page_0001', 'doc_old', 1, 'page', 1, 'text', 'completed', "
            "'not_needed', '旧原文', '校订后的原文', :now, :now)", {"now": utcnow()})
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, quote, start_offset, end_offset, "
            "version, version_snapshot, created_at, updated_at) "
            "VALUES ('ev_old', 'doc_old_page_0001', 'doc_old', '旧原文', 0, 3, 'corrected', "
            "'{}', :now, :now)", {"now": utcnow()})

    result = migrations.ensure_schema(workspace)
    assert result["from"] == 5 and result["to"] == migrations.SCHEMA_VERSION
    assert "collections-and-exports" in result["applied"]

    with workspace.connect() as conn:
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        row = conn.execute("SELECT * FROM evidence WHERE id = 'ev_old'").fetchone()
    assert {"collections", "collection_evidence", "exports"} <= tables
    assert row["quote"] == "旧原文"
    assert workspace.get_unit("doc_old_page_0001")["corrected_text"] == "校订后的原文"
    # 新表可用
    created = core.create_collection(workspace, "升级后的集合")
    with workspace.connect() as conn:
        conn.execute(
            "INSERT INTO collection_evidence (collection_id, evidence_id, added_at) "
            "VALUES (?, 'ev_old', ?)", (created["id"], utcnow()))
    assert core.get_collection(workspace, created["id"])["evidence_count"] == 1


def test_export_tables_registered_in_migration_test_workspace(web_workspace):
    with web_workspace.connect() as conn:
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]
    assert {"collections", "collection_evidence", "exports"} <= tables
    assert int(version) == migrations.SCHEMA_VERSION


def test_collection_names_case_insensitive_unique(web_workspace):
    core.create_collection(web_workspace, "Heritage")
    with pytest.raises(Exception):
        core.create_collection(web_workspace, "heritage")
    with pytest.raises(sqlite3.IntegrityError):
        with web_workspace.connect() as conn:
            conn.execute(
                "INSERT INTO collections (id, name, name_key, description, created_at, "
                "updated_at) VALUES ('col_dup', 'HERITAGE', 'heritage', '', '', '')")