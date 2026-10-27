"""网页页面 API：会话、CSRF、修订、幂等、树、块、资产与导出。"""

from __future__ import annotations

import base64
import io
import json
import uuid
import zipfile

import pytest

from litworkbench.evidence import save_evidence
from litworkbench.ingest import import_document

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def text_block(block_id: str, text: str = "", *, kind: str = "paragraph",
               props: dict | None = None) -> dict:
    return {
        "id": block_id,
        "type": kind,
        "props": dict(props or {}),
        "content": [{"type": "text", "text": text, "styles": {}}] if text else [],
        "children": [],
    }


def body(*items: dict) -> dict:
    return {"schema_version": 1, "blocks": list(items)}


def request_id(prefix: str = "req") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def create(session, title="页面", **extra):
    payload = {"title": title, "request_id": request_id("create"), **extra}
    response = session.post("/api/v1/pages", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- 会话与错误


def test_pages_require_session(client):
    response = client.get("/api/v1/pages")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_REQUIRED"


def test_page_write_requires_csrf(client):
    assert client.get("/api/v1/workspace").status_code == 200
    response = client.post("/api/v1/pages", json={
        "title": "无令牌", "request_id": "r1",
    })
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_unknown_api_path_is_json_404(session):
    response = session.get("/api/v1/pages-not-real")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 页面与引用


def test_create_read_patch_list_and_backlinks(session):
    target = create(session, "目标页")
    source = create(
        session, "引用页",
        body=body(text_block("ref", kind="pageRef", props={"pageId": target["id"]})),
        properties={"tags": ["甲"], "status": "in_progress"},
    )
    assert source["url"] == f"/pages/{source['id']}"
    assert source["body"]["blocks"][0]["props"]["title"] == "目标页"

    renamed = session.patch(f"/api/v1/pages/{target['id']}", json={
        "expected_revision": 1, "request_id": request_id("rename"),
        "title": "改过的目标页",
    })
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["revision"] == 2
    backlinks = session.get(f"/api/v1/pages/{target['id']}/backlinks")
    assert backlinks.status_code == 200
    assert backlinks.json()["total"] == 1
    assert backlinks.json()["items"][0]["page_id"] == source["id"]
    assert backlinks.json()["items"][0]["title"] == "引用页"

    listed = session.get("/api/v1/pages", params={"q": "目标", "page": 1, "page_size": 1})
    assert listed.status_code == 200
    payload = listed.json()
    assert set(payload) == {"items", "total", "page", "page_size"}
    assert payload["total"] == 1 and len(payload["items"]) == 1
    assert payload["items"][0]["url"] == f"/pages/{target['id']}"

    missing = session.get("/api/v1/pages/pg_missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_patch_revision_conflict_idempotency_and_restore(session):
    page = create(session, "第一版")
    request = request_id("once")
    first = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 1, "request_id": request, "title": "第二版",
        "body": body(text_block("p1", "第二版正文")),
    })
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 2
    replay = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 1, "request_id": request, "title": "第二版",
        "body": body(text_block("p1", "第二版正文")),
    })
    assert replay.status_code == 200
    assert replay.json() == first.json()

    stale = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 1, "request_id": request_id("stale"), "title": "过期",
    })
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

    reused = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 1, "request_id": request, "title": "换了载荷",
    })
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "REQUEST_ID_REUSE"

    history = session.get(f"/api/v1/pages/{page['id']}/history").json()
    assert history["total"] == 2
    restored = session.post(f"/api/v1/pages/{page['id']}/restore", json={
        "expected_revision": 2, "request_id": request_id("restore"), "revision": 1,
    })
    assert restored.status_code == 200, restored.text
    assert restored.json()["title"] == "第一版"
    assert restored.json()["revision"] == 3
    assert session.get(f"/api/v1/pages/{page['id']}/history").json()["total"] == 3


def test_block_operations_are_scoped_to_one_page_and_atomic(session):
    first = create(session, "甲页",
                   body=body(text_block("a", "A"), text_block("b", "B")))
    second = create(session, "乙页", body=body(text_block("x", "X")))

    cross = session.post(f"/api/v1/pages/{second['id']}/blocks", json={
        "expected_revision": 1, "request_id": request_id("cross"),
        "operations": [
            {"op": "move", "block_id": "a", "parent_block_id": None, "index": 0},
        ],
    })
    assert cross.status_code == 422
    assert session.get(f"/api/v1/pages/{second['id']}").json()["revision"] == 1

    changed = session.post(f"/api/v1/pages/{first['id']}/blocks", json={
        "expected_revision": 1, "request_id": request_id("ops"),
        "operations": [
            {"op": "insert", "parent_block_id": "a", "index": 0,
             "block": text_block("c", "C")},
            {"op": "replace", "block_id": "b", "block": text_block("b", "B 新")},
        ],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    a = changed.json()["body"]["blocks"][0]
    assert [item["id"] for item in a["children"]] == ["c"]
    assert changed.json()["body"]["blocks"][1]["content"][0]["text"] == "B 新"

    too_many = session.post(f"/api/v1/pages/{first['id']}/blocks", json={
        "expected_revision": 2, "request_id": request_id("many"),
        "operations": [
            {"op": "insert", "parent_block_id": None, "index": index,
             "block": text_block(f"n{index}")}
            for index in range(101)
        ],
    })
    assert too_many.status_code == 422
    assert session.get(f"/api/v1/pages/{first['id']}").json()["revision"] == 2


def test_patch_document_ids_only_replaces_relation_and_history(session, web_workspace,
                                                              web_fixtures):
    document = import_document(web_workspace, web_fixtures["text_pdf"])
    page = create(session, "关联文献")
    linked = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 1, "request_id": request_id("doc-link"),
        "document_ids": [document["id"]],
    })
    assert linked.status_code == 200, linked.text
    assert linked.json()["document_ids"] == [document["id"]]
    assert linked.json()["revision"] == 2
    assert session.get(f"/api/v1/pages/{page['id']}/history").json()["total"] == 2

    cleared = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 2, "request_id": request_id("doc-clear"),
        "document_ids": [],
    })
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["document_ids"] == []
    assert cleared.json()["revision"] == 3


def test_archive_is_visible_in_list_and_no_delete_endpoint(session):
    page = create(session, "归档页")
    archived = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 1, "request_id": request_id("archive"), "archived": True,
    })
    assert archived.status_code == 200 and archived.json()["effective_archived"] is True
    assert session.get("/api/v1/pages").json()["total"] == 0
    assert session.get("/api/v1/pages", params={"include_archived": True}).json()["total"] == 1
    restored = session.patch(f"/api/v1/pages/{page['id']}", json={
        "expected_revision": 2, "request_id": request_id("unarchive"), "archived": False,
    })
    assert restored.status_code == 200 and restored.json()["effective_archived"] is False
    deleted = session.client.delete(f"/api/v1/pages/{page['id']}",
                                    headers=session.write_headers())
    assert deleted.status_code in {404, 405}
    assert session.get(f"/api/v1/pages/{page['id']}").status_code == 200


# ---------------------------------------------------------------- 资产


def upload_png(session, data=PNG_1X1, filename="图.png"):
    return session.post("/api/v1/note-assets", files={"file": (filename, data, "image/png")})


def test_note_asset_upload_download_and_image_block(session, web_workspace):
    response = upload_png(session)
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["media_type"] == "image/png"
    assert asset["byte_size"] == len(PNG_1X1)
    assert asset["url"] == f"/api/v1/note-assets/{asset['id']}"
    assert "stored_path" not in asset
    assert str(web_workspace.root) not in json.dumps(asset, ensure_ascii=False)

    download = session.get(asset["url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"
    assert download.content == PNG_1X1

    page = create(session, "图片页", body=body(text_block(
        "img", kind="image", props={"assetId": asset["id"], "caption": "白塔"})))
    image = page["body"]["blocks"][0]["props"]
    assert image["sha256"] == asset["sha256"]
    assert image["media_type"] == "image/png"
    assert image["src"] == asset["url"]
    assert str(web_workspace.root) not in json.dumps(page, ensure_ascii=False)
    assert session.get("/api/v1/note-assets/asset_missing").status_code == 404


def test_note_asset_rejects_fake_type_oversize_and_bad_path(session):
    fake = upload_png(session, data=b"not really a png", filename="伪造.png")
    assert fake.status_code == 415
    assert fake.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    oversize = upload_png(session, data=b"GIF89a" + b"\0" * (20 * 1024 * 1024),
                          filename="过大.gif")
    assert oversize.status_code == 413
    assert oversize.json()["error"]["code"] == "FILE_TOO_LARGE"

    missing = session.get("/api/v1/note-assets/asset_nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_note_asset_download_requires_session(client):
    assert client.get("/api/v1/note-assets/asset_x").status_code == 401


def test_image_block_rejects_unknown_asset(session):
    response = session.post("/api/v1/pages", json={
        "title": "坏图片", "request_id": request_id("bad-image"),
        "body": body(text_block("img", kind="image", props={"assetId": "asset_missing"})),
    })
    assert response.status_code == 422


# ---------------------------------------------------------------- 导出


def test_page_exports_json_markdown_zip_include_assets_and_references(
        session, web_workspace, web_fixtures):
    document = import_document(web_workspace, web_fixtures["text_pdf"])
    unit = web_workspace.units_for_doc(document["id"])[0]
    evidence = save_evidence(web_workspace, unit["id"], "白塔位于县城北侧")
    asset = upload_png(session).json()
    target = create(session, "引用目标")
    page = create(
        session, "导出页",
        body=body(
            text_block("p", "<script>alert(1)</script> 正文"),
            text_block("img", kind="image", props={"assetId": asset["id"]}),
            text_block("ev", kind="evidenceRef", props={"evidenceId": evidence["id"]}),
            text_block("doc", kind="documentRef", props={"documentId": document["id"]}),
            text_block("ref", kind="pageRef", props={"pageId": target["id"]}),
        ),
        properties={"tags": ["导出"], "status": "done", "document_ids": [document["id"]]},
    )

    def export(fmt):
        response = session.post(f"/api/v1/pages/{page['id']}/exports",
                                json={"format": fmt})
        assert response.status_code == 201, response.text
        return response.json()

    json_export = export("json")
    archive = session.get(json_export["download_url"])
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        names = set(package.namelist())
        assert {"page.json", "references.json", "manifest.json"} <= names
        assert any(name.startswith("assets/") for name in names)
        payload = json.loads(package.read("page.json").decode("utf-8"))
        references = json.loads(package.read("references.json").decode("utf-8"))
        manifest = json.loads(package.read("manifest.json").decode("utf-8"))
        assert payload["page"]["id"] == page["id"]
        assert references["items"] and any(
            item["target_id"] == evidence["id"] for item in references["items"])
        assert any(item["target_id"] == document["id"] for item in references["items"])
        assert manifest["asset_count"] == 1
        assert manifest["limitations"]
        text = "\n".join(package.read(name).decode("utf-8", "ignore")
                         for name in names if not name.startswith("assets/"))
        assert str(web_workspace.root) not in text

    markdown_export = export("markdown")
    markdown_archive = session.get(markdown_export["download_url"])
    with zipfile.ZipFile(io.BytesIO(markdown_archive.content)) as package:
        names = set(package.namelist())
        assert {"page.md", "references.json", "manifest.json"} <= names
        markdown = package.read("page.md").decode("utf-8")
        assert "白塔位于县城北侧" in markdown
        assert "<script>" not in markdown
        assert "assets/" in markdown
        assert str(web_workspace.root) not in markdown

    zip_export = export("zip")
    zip_archive = session.get(zip_export["download_url"])
    with zipfile.ZipFile(io.BytesIO(zip_archive.content)) as package:
        assert {"page.json", "page.md", "references.json", "manifest.json"} <= set(
            package.namelist())

    invalid = session.post(f"/api/v1/pages/{page['id']}/exports", json={"format": "pdf"})
    assert invalid.status_code == 422
