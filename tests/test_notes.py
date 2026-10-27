"""页面领域：事务、修订、幂等、树、归档与引用快照。"""

from __future__ import annotations

import uuid

import pytest

from litworkbench import migrations
from litworkbench.errors import LitError, NotFoundError
from litworkbench.evidence import correct_unit, review_evidence, save_evidence
from litworkbench.ingest import import_document
from litworkbench.notes import (
    create_page,
    edit_page_blocks,
    get_backlinks,
    get_page,
    get_page_history,
    list_pages,
    restore_page,
    update_page,
)


def text_block(block_id: str, text: str = "", *, kind: str = "paragraph",
               props: dict | None = None, children: list | None = None) -> dict:
    return {
        "id": block_id,
        "type": kind,
        "props": dict(props or {}),
        "content": [{"type": "text", "text": text, "styles": {}}] if text else [],
        "children": list(children or []),
    }


def body(*items: dict) -> dict:
    return {"schema_version": 1, "blocks": list(items)}


def rid(prefix: str = "req") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def make_page(workspace, title="页面", **kwargs):
    kwargs.setdefault("request_id", rid("create"))
    return create_page(workspace, title=title, parent_id=None, body=body(), properties=None,
                       **kwargs)


def test_create_and_reopen_page(workspace):
    page = create_page(
        workspace,
        title="白塔研究",
        parent_id=None,
        body=body(text_block("b1", "第一段")),
        properties={"tags": ["文物", "县志"], "status": "in_progress", "favorite": True,
                    "position": 3},
        request_id=rid("create"),
    )
    assert set(page) >= {
        "id", "parent_id", "title", "body", "schema_version", "revision", "tags",
        "status", "archived", "effective_archived", "position", "favorite",
        "document_ids", "created_at", "updated_at", "url",
    }
    assert page["revision"] == 1
    assert page["body"]["blocks"][0]["content"][0]["text"] == "第一段"
    assert page["tags"] == ["文物", "县志"]
    assert page["status"] == "in_progress" and page["favorite"] is True
    assert page["url"] == f"/pages/{page['id']}"

    reopened = get_page(workspace, page["id"])
    assert reopened == page

    listed = list_pages(workspace, query="白塔")
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == page["id"]
    assert listed["items"][0]["url"].endswith(page["id"])


def test_document_ids_update_uses_relation_table_and_advances_revision(workspace, fixtures):
    document = import_document(workspace, fixtures["text_pdf"])
    page = make_page(workspace, "关联文献")
    linked = update_page(workspace, page["id"], expected_revision=1,
                         request_id=rid("link-doc"), changes={"document_ids": [document["id"]]})
    assert linked["document_ids"] == [document["id"]]
    assert linked["revision"] == 2
    assert get_page_history(workspace, page["id"])["total"] == 2
    cleared = update_page(workspace, page["id"], expected_revision=2,
                          request_id=rid("clear-doc"), changes={"document_ids": []})
    assert cleared["document_ids"] == []
    assert cleared["revision"] == 3
    assert get_page_history(workspace, page["id"])["total"] == 3


def test_create_insert_before_bumps_shifted_sibling_revision(workspace):
    parent = make_page(workspace, "父")
    first = create_page(workspace, title="先建", parent_id=parent["id"], body=body(),
                        properties={"position": 0}, request_id=rid("first"))
    assert first["revision"] == 1
    assert get_page_history(workspace, first["id"])["total"] == 1

    second = create_page(workspace, title="插到前面", parent_id=parent["id"], body=body(),
                         properties={"position": 0}, request_id=rid("second"))
    shifted = get_page(workspace, first["id"])
    assert shifted["position"] == 1
    assert shifted["revision"] == 2
    history = get_page_history(workspace, first["id"])
    assert history["total"] == 2
    assert history["items"][0]["snapshot"]["position"] == 1
    assert history["items"][1]["snapshot"]["position"] == 0
    assert second["position"] == 0 and second["revision"] == 1


def test_same_revision_conflict_keeps_history(workspace):
    page = make_page(workspace)
    updated = update_page(workspace, page["id"], expected_revision=1,
                          request_id=rid("update"), changes={"title": "第一次修改"})
    assert updated["revision"] == 2
    with pytest.raises(LitError) as error:
        update_page(workspace, page["id"], expected_revision=1,
                    request_id=rid("stale"), changes={"title": "过期修改"})
    assert getattr(error.value, "code", "") == "REVISION_CONFLICT"
    current = get_page(workspace, page["id"])
    assert current["title"] == "第一次修改" and current["revision"] == 2
    history = get_page_history(workspace, page["id"])
    assert history["total"] == 2
    assert [item["revision"] for item in history["items"]] == [2, 1]


def test_replayed_request_is_idempotent_before_revision_check(workspace):
    request_id = rid("once")
    page = make_page(workspace)
    first = update_page(workspace, page["id"], expected_revision=1,
                        request_id=request_id, changes={"title": "只写一次"},
                        actor="owner", origin="web")
    replay = update_page(workspace, page["id"], expected_revision=1,
                         request_id=request_id, changes={"title": "只写一次"},
                         actor="owner", origin="web")
    assert replay == first and replay["revision"] == 2
    assert get_page_history(workspace, page["id"])["total"] == 2

    create_id = rid("create-once")
    created = create_page(workspace, title="幂等新建", parent_id=None, body=body(),
                          properties=None, request_id=create_id)
    replay_created = create_page(workspace, title="幂等新建", parent_id=None, body=body(),
                                 properties=None, request_id=create_id)
    assert replay_created["id"] == created["id"]
    assert list_pages(workspace, query="幂等新建")["total"] == 1


def test_reused_request_id_with_changed_payload_rejected(workspace):
    page = make_page(workspace)
    request_id = rid("reuse")
    update_page(workspace, page["id"], expected_revision=1,
                request_id=request_id, changes={"title": "原载荷"})
    with pytest.raises(LitError) as error:
        update_page(workspace, page["id"], expected_revision=1,
                    request_id=request_id, changes={"title": "换了载荷"})
    assert getattr(error.value, "code", "") == "REQUEST_ID_REUSE"
    assert get_page(workspace, page["id"])["title"] == "原载荷"


def test_failed_mutation_does_not_consume_request_id(workspace):
    page = make_page(workspace)
    request_id = rid("retry")
    with pytest.raises(LitError):
        update_page(workspace, page["id"], expected_revision=1, request_id=request_id,
                    changes={"body": body(text_block(
                        "bad", kind="pageRef", props={"pageId": "pg_missing"}))})
    assert get_page_history(workspace, page["id"])["total"] == 1
    saved = update_page(workspace, page["id"], expected_revision=1,
                        request_id=request_id, changes={"title": "失败后重试成功"})
    assert saved["revision"] == 2 and saved["title"] == "失败后重试成功"


def test_moves_cannot_create_cycle(workspace):
    parent = make_page(workspace, "父")
    child = make_page(workspace, "子")
    child = update_page(workspace, child["id"], expected_revision=1,
                         request_id=rid("parent"), changes={"parent_id": parent["id"]})
    with pytest.raises(LitError):
        update_page(workspace, parent["id"], expected_revision=1,
                    request_id=rid("cycle1"), changes={"parent_id": child["id"]})
    with pytest.raises(LitError):
        update_page(workspace, child["id"], expected_revision=child["revision"],
                    request_id=rid("cycle2"), changes={"parent_id": child["id"]})
    with pytest.raises(NotFoundError):
        update_page(workspace, child["id"], expected_revision=child["revision"],
                    request_id=rid("missing-parent"), changes={"parent_id": "pg_missing"})
    assert get_page(workspace, parent["id"])["parent_id"] is None


def test_sibling_order_and_move(workspace):
    parent = make_page(workspace, "父")
    first = create_page(workspace, title="一", parent_id=parent["id"], body=body(),
                        properties={"position": 0}, request_id=rid("one"))
    second = create_page(workspace, title="二", parent_id=parent["id"], body=body(),
                         properties={"position": 1}, request_id=rid("two"))
    moved = update_page(workspace, second["id"], expected_revision=1,
                        request_id=rid("reorder"), changes={"position": 0})
    assert moved["position"] == 0
    shifted = get_page(workspace, first["id"])
    assert shifted["position"] == 1
    assert shifted["revision"] == 2
    shifted_history = get_page_history(workspace, first["id"])
    assert shifted_history["total"] == 2
    assert shifted_history["items"][0]["snapshot"]["position"] == 1
    listed = list_pages(workspace, page_size=100)["items"]
    children = [item for item in listed if item["parent_id"] == parent["id"]]
    assert [item["id"] for item in children] == [second["id"], first["id"]]

    rooted = update_page(workspace, second["id"], expected_revision=moved["revision"],
                         request_id=rid("root"), changes={"parent_id": None, "position": 0})
    assert rooted["parent_id"] is None


def test_ancestor_archive_preserves_child_own_archive(workspace):
    parent = make_page(workspace, "父")
    child = create_page(workspace, title="子", parent_id=parent["id"], body=body(),
                        properties=None, request_id=rid("child"))
    grandchild = create_page(workspace, title="孙", parent_id=child["id"], body=body(),
                             properties=None, request_id=rid("grandchild"))
    sibling = create_page(workspace, title="另一个子页", parent_id=parent["id"], body=body(),
                          properties=None, request_id=rid("sibling"))

    update_page(workspace, child["id"], expected_revision=1, request_id=rid("arch-child"),
                changes={"archived": True})
    assert get_page(workspace, child["id"])["archived"] is True
    assert get_page(workspace, grandchild["id"])["effective_archived"] is True

    update_page(workspace, parent["id"], expected_revision=1, request_id=rid("arch-parent"),
                changes={"archived": True})
    assert get_page(workspace, sibling["id"])["effective_archived"] is True
    assert list_pages(workspace)["total"] == 0
    assert list_pages(workspace, include_archived=True)["total"] == 4

    update_page(workspace, parent["id"], expected_revision=2, request_id=rid("restore-parent"),
                changes={"archived": False})
    assert get_page(workspace, child["id"])["archived"] is True
    assert get_page(workspace, child["id"])["effective_archived"] is True
    assert get_page(workspace, grandchild["id"])["effective_archived"] is True
    assert get_page(workspace, sibling["id"])["effective_archived"] is False
    listed = list_pages(workspace)["items"]
    assert {item["id"] for item in listed} == {parent["id"], sibling["id"]}


def test_evidence_reference_uses_snapshot_and_review_status(workspace, fixtures):
    document = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(document["id"])[0]
    evidence = save_evidence(workspace, unit["id"], "白塔位于县城北侧", category="文物")
    page = create_page(
        workspace, title="证据引用", parent_id=None,
        body=body(
            text_block("ev", kind="evidenceRef", props={
                "evidenceId": evidence["id"], "quote": "伪造的出处",
                "review_status": "accepted", "filename": "伪造.pdf",
            }),
            text_block("doc", kind="documentRef", props={
                "documentId": document["id"], "title": "伪造标题",
            }),
        ),
        properties=None, request_id=rid("evidence"),
    )
    reference = page["body"]["blocks"][0]["props"]
    assert reference["quote"] == "白塔位于县城北侧"
    assert reference["review_status"] == "pending"
    assert reference["doc_id"] == document["id"]
    assert reference["filename"] == document["filename"]
    assert page["body"]["blocks"][1]["props"]["title"] == document["title"]

    review_evidence(workspace, evidence["id"], "accepted")
    accepted = get_page(workspace, page["id"])["body"]["blocks"][0]["props"]
    assert accepted["quote"] == "白塔位于县城北侧"
    assert accepted["review_status"] == "accepted"

    corrected = unit["original_text"].replace("北侧", "北郊")
    correct_unit(workspace, unit["id"], corrected, "校订地名")
    stale = get_page(workspace, page["id"])["body"]["blocks"][0]["props"]
    assert stale["quote"] == "白塔位于县城北侧"
    assert stale["review_status"] == "stale"


def test_page_reference_tracks_rename_and_archive(workspace):
    target = make_page(workspace, "旧标题")
    source = create_page(
        workspace, title="引用页", parent_id=None,
        body=body(text_block("ref", kind="pageRef", props={"pageId": target["id"]})),
        properties=None, request_id=rid("source"),
    )
    assert source["body"]["blocks"][0]["props"]["title"] == "旧标题"
    update_page(workspace, target["id"], expected_revision=1,
                request_id=rid("rename"), changes={"title": "新标题"})
    update_page(workspace, target["id"], expected_revision=2,
                request_id=rid("archive"), changes={"archived": True})
    reference = get_page(workspace, source["id"])["body"]["blocks"][0]["props"]
    assert reference["title"] == "新标题"
    assert reference["archived"] is True and reference["effective_archived"] is True
    backlinks = get_backlinks(workspace, target["id"])
    assert backlinks["total"] == 1
    assert backlinks["items"][0]["page_id"] == source["id"]
    assert backlinks["items"][0]["title"] == "引用页"
    assert backlinks["items"][0]["block_id"] == "ref"


def test_edit_page_blocks_is_atomic_and_requires_real_block_ids(workspace):
    page = make_page(workspace)
    update_page(workspace, page["id"], expected_revision=1, request_id=rid("seed"),
                changes={"body": body(text_block("a", "A"), text_block("b", "B"))})
    operations = [
        {"op": "insert", "parent_block_id": "a", "index": 0,
         "block": text_block("c", "C")},
        {"op": "move", "block_id": "b", "parent_block_id": "a", "index": 1},
        {"op": "replace", "block_id": "c", "block": text_block("c", "C 新")},
    ]
    updated = edit_page_blocks(workspace, page["id"], expected_revision=2,
                               request_id=rid("ops"), operations=operations)
    assert updated["revision"] == 3
    assert updated["body"]["blocks"][0]["id"] == "a"
    assert [item["id"] for item in updated["body"]["blocks"][0]["children"]] == ["c", "b"]

    original = updated["body"]
    with pytest.raises(LitError):
        edit_page_blocks(workspace, page["id"], expected_revision=3,
                         request_id=rid("bad-ops"), operations=[
                             {"op": "insert", "parent_block_id": None, "index": 0,
                              "block": text_block("d", "D")},
                             {"op": "move", "block_id": "missing", "parent_block_id": None,
                              "index": 0},
                         ])
    assert get_page(workspace, page["id"])["body"] == original
    assert get_page(workspace, page["id"])["revision"] == 3


def test_restore_page_creates_a_new_revision(workspace):
    page = make_page(workspace, "第一版")
    update_page(workspace, page["id"], expected_revision=1, request_id=rid("v2"),
                changes={"title": "第二版", "body": body(text_block("x", "第二版正文"))})
    restored = restore_page(workspace, page["id"], expected_revision=2,
                            request_id=rid("restore"), revision=1)
    assert restored["title"] == "第一版" and restored["revision"] == 3
    history = get_page_history(workspace, page["id"])
    assert [item["revision"] for item in history["items"]] == [3, 2, 1]
    assert history["items"][-1]["snapshot"]["title"] == "第一版"
    assert get_page(workspace, page["id"])["body"]["blocks"] == []


def test_old_evidence_survives_notes_migration(workspace, fixtures):
    document = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(document["id"])[0]
    evidence = save_evidence(workspace, unit["id"], "白塔位于县城北侧")
    with workspace.connect() as conn:
        for table in ("page_exports", "mutation_requests", "page_links", "page_revisions",
                      "page_tags", "page_documents", "note_assets", "pages"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
    result = migrations.ensure_schema(workspace)
    assert result["from"] == 9 and result["to"] == migrations.SCHEMA_VERSION
    assert "notes-workspace" in result["applied"]
    assert result["backup"]
    with workspace.connect() as conn:
        row = conn.execute("SELECT quote FROM evidence WHERE id = ?", (evidence["id"],)).fetchone()
        tables = {item["name"] for item in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]
    assert row["quote"] == "白塔位于县城北侧"
    assert {"pages", "page_revisions", "page_links", "note_assets",
            "mutation_requests"} <= tables
    assert int(version) == migrations.SCHEMA_VERSION
