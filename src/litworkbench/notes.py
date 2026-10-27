"""页面领域：页面树、块正文、引用索引、不可变历史与幂等写入。

所有写入口都走同一条事务边界：``BEGIN IMMEDIATE`` 之后先查成功的
``mutation_requests``（相同 request_id 返回原结果，换载荷报错），再校验
``expected_revision``，最后把页面、属性、引用索引、历史快照和幂等记录一起提交。
失败事务不会留下可被误认为成功的请求记录。

正文是权威结构；``page_links``、标签与文献关联都在保存的同一事务里更新。
引用块只接受稳定目标 ID，出处文字与核验状态由数据库当前记录填充。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from typing import Any

from litworkbench import note_blocks
from litworkbench.errors import (
    EditConflictError,
    NotFoundError,
    ValidationError,
)
from litworkbench.store import Workspace, ensure_write_transaction, sha256_text, utcnow

PAGE_STATUSES = ("not_started", "in_progress", "done")
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100
MAX_TITLE_LENGTH = 500
MAX_TAGS = 100
MAX_REQUEST_ID_LENGTH = 200

_PROPERTY_KEYS = frozenset({
    "icon", "tags", "status", "favorite", "document_ids", "position", "archived",
})
_UPDATE_KEYS = _PROPERTY_KEYS | {"title", "parent_id", "body"}


class RevisionConflictError(EditConflictError):
    """``expected_revision`` 与当前页面修订号不一致。"""

    code = "REVISION_CONFLICT"
    status_code = 409


class RequestIdReuseError(EditConflictError):
    """同一操作者把同一个 request_id 用于不同载荷。"""

    code = "REQUEST_ID_REUSE"
    status_code = 409


class NoteReferenceError(ValidationError):
    """正文引用了不存在或不可用的页面/文献/摘录/图片。"""

    code = "NOTE_REFERENCE_INVALID"
    status_code = 422


# ------------------------------------------------------------------ 小工具


def _new_page_id() -> str:
    return f"pg_{uuid.uuid4().hex[:12]}"


def _loads(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def _loads_body(value: str | None) -> dict[str, Any]:
    try:
        return json.loads(value) if value else note_blocks.empty_body()
    except (json.JSONDecodeError, TypeError) as exc:  # pragma: no cover - 损坏库
        raise ValidationError("页面正文 JSON 已损坏，请从备份恢复。") from exc


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_str(value: Any, label: str, *, allow_empty: bool = False,
                 max_length: int | None = None) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} 必须是字符串。")
    text = value if allow_empty else value.strip()
    if not allow_empty and not text:
        raise ValidationError(f"{label} 不能为空。")
    if max_length is not None and len(text) > max_length:
        raise ValidationError(f"{label} 不能超过 {max_length} 个字符。")
    return text


def _require_request_id(value: Any) -> str:
    return _require_str(value, "request_id", max_length=MAX_REQUEST_ID_LENGTH)


def _require_actor(value: Any) -> str:
    return _require_str(value, "actor", max_length=100)


def _require_origin(value: Any) -> str:
    return _require_str(value, "origin", max_length=50)


def _require_revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError("expected_revision 必须是非负整数。")
    return value


def _require_position(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValidationError("position 必须是非负整数。")
    return value


def _normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("tags 必须是字符串数组。")
    tags: list[str] = []
    for item in raw:
        tag = _require_str(item, "标签", max_length=100)
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValidationError(f"标签最多 {MAX_TAGS} 个。")
    return tags


def _normalize_document_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("document_ids 必须是字符串数组。")
    ids: list[str] = []
    for item in raw:
        value = _require_str(item, "文献 ID", max_length=200)
        if value not in ids:
            ids.append(value)
    return ids


def _normalize_properties(properties: dict[str, Any] | None,
                          *, for_create: bool) -> dict[str, Any]:
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        raise ValidationError("properties 必须是对象。")
    unknown = set(properties) - _PROPERTY_KEYS
    if unknown:
        raise ValidationError(f"页面属性包含未知字段：{'、'.join(sorted(unknown))}。")
    result: dict[str, Any] = {}
    if "icon" in properties:
        result["icon"] = _require_str(properties["icon"], "图标", allow_empty=True,
                                      max_length=200)
    if "tags" in properties:
        result["tags"] = _normalize_tags(properties["tags"])
    if "status" in properties:
        status = properties["status"]
        if status not in PAGE_STATUSES:
            raise ValidationError(
                f"未知的页面状态：{status!r}（可选 {'/'.join(PAGE_STATUSES)}）"
            )
        result["status"] = status
    if "favorite" in properties:
        if not isinstance(properties["favorite"], bool):
            raise ValidationError("favorite 必须是布尔值。")
        result["favorite"] = properties["favorite"]
    if "document_ids" in properties:
        result["document_ids"] = _normalize_document_ids(properties["document_ids"])
    if "position" in properties:
        result["position"] = _require_position(properties["position"])
    if "archived" in properties:
        if not isinstance(properties["archived"], bool):
            raise ValidationError("archived 必须是布尔值。")
        result["archived"] = properties["archived"]
    if not for_create:
        result.pop("parent_id", None)
    return result


def _page_row(conn: sqlite3.Connection, page_id: str,
              *, include_archived: bool = True) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到页面：{page_id}")
    if not include_archived and row["archived"]:
        raise NotFoundError(f"找不到页面：{page_id}")
    return row


def _tags_for(conn: sqlite3.Connection, page_id: str) -> list[str]:
    return [row["tag"] for row in conn.execute(
        "SELECT tag FROM page_tags WHERE page_id = ? ORDER BY position, tag",
        (page_id,),
    )]


def _documents_for(conn: sqlite3.Connection, page_id: str) -> list[str]:
    return [row["doc_id"] for row in conn.execute(
        "SELECT doc_id FROM page_documents WHERE page_id = ? ORDER BY position, doc_id",
        (page_id,),
    )]


def _effective_archived(conn: sqlite3.Connection, page_id: str,
                        cache: dict[str, bool] | None = None) -> bool:
    if cache is not None and page_id in cache:
        return cache[page_id]
    seen: set[str] = set()
    current: str | None = page_id
    archived = False
    while current is not None:
        if current in seen:  # 数据损坏时的防御，不无限循环
            break
        seen.add(current)
        row = conn.execute(
            "SELECT parent_id, archived FROM pages WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            break
        if row["archived"]:
            archived = True
            break
        current = row["parent_id"]
    if cache is not None:
        cache[page_id] = archived
    return archived


def _assert_valid_parent(conn: sqlite3.Connection, page_id: str | None,
                         parent_id: str | None) -> None:
    if parent_id is None:
        return
    if page_id is not None and parent_id == page_id:
        raise ValidationError("页面不能以自己为父页面。")
    row = conn.execute("SELECT id FROM pages WHERE id = ?", (parent_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到父页面：{parent_id}")
    if _effective_archived(conn, parent_id):
        raise ValidationError("不能在已归档页面下创建或移入页面，请先恢复父页面。")
    seen: set[str] = set()
    current: str | None = parent_id
    while current is not None:
        if current in seen:
            raise ValidationError("页面父链存在环，已拒绝保存。")
        seen.add(current)
        if page_id is not None and current == page_id:
            raise ValidationError("页面不能移动到自己的后代之下。")
        parent_row = conn.execute(
            "SELECT parent_id FROM pages WHERE id = ?", (current,)
        ).fetchone()
        current = parent_row["parent_id"] if parent_row is not None else None


def _validate_documents(conn: sqlite3.Connection, document_ids: Iterable[str]) -> None:
    for doc_id in document_ids:
        row = conn.execute("SELECT id FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"找不到文献：{doc_id}")


def _siblings(conn: sqlite3.Connection, parent_id: str | None,
              exclude_id: str | None = None) -> list[str]:
    sql = ("SELECT id FROM pages WHERE parent_id IS ?"
           + (" AND id != ?" if exclude_id else "")
           + " ORDER BY position, created_at, id")
    params: list[Any] = [parent_id]
    if exclude_id:
        params.append(exclude_id)
    return [row["id"] for row in conn.execute(sql, params)]


def _next_position(conn: sqlite3.Connection, parent_id: str | None) -> int:
    row = conn.execute(
        "SELECT MAX(position) AS value FROM pages WHERE parent_id IS ?", (parent_id,)
    ).fetchone()
    return int(row["value"] + 1) if row and row["value"] is not None else 0


def _bump_sibling_positions(conn: sqlite3.Connection,
                            assignments: Iterable[tuple[str, int]], *, origin: str,
                            actor: str, request_id: str) -> None:
    """把同级页面真正变化的 position 作为页面变更推进 revision 与历史。

    排序不是可以悄悄改写的内部元数据：任何被移动/挤开的页面都必须留下
    ``page_revisions`` 快照，客户端才能用 ``expected_revision`` 发现变化。
    这些历史与主页面共享同一次请求的 origin / actor / request_id。
    """
    changed: list[str] = []
    now = utcnow()
    for sibling_id, new_position in assignments:
        row = conn.execute(
            "SELECT position FROM pages WHERE id = ?", (sibling_id,)
        ).fetchone()
        if row is None or int(row["position"]) == new_position:
            continue
        conn.execute(
            "UPDATE pages SET position = ?, revision = revision + 1, updated_at = ? "
            "WHERE id = ?",
            (new_position, now, sibling_id),
        )
        changed.append(sibling_id)
    for sibling_id in changed:
        _record_revision(conn, sibling_id, origin=origin, actor=actor,
                         request_id=request_id)


def _create_position(conn: sqlite3.Connection, parent_id: str | None, page_id: str,
                     position: int | None, *, origin: str, actor: str,
                     request_id: str) -> None:
    if position is None:
        conn.execute("UPDATE pages SET position = ? WHERE id = ?",
                     (_next_position(conn, parent_id), page_id))
        return
    rows = conn.execute(
        "SELECT id, position FROM pages WHERE parent_id IS ? AND id != ? "
        "AND position >= ? ORDER BY position, created_at, id",
        (parent_id, page_id, position),
    ).fetchall()
    _bump_sibling_positions(
        conn,
        ((row["id"], int(row["position"]) + 1) for row in rows),
        origin=origin,
        actor=actor,
        request_id=request_id,
    )
    conn.execute("UPDATE pages SET position = ? WHERE id = ?", (position, page_id))


def _place_existing(conn: sqlite3.Connection, parent_id: str | None, page_id: str,
                    position: int | None) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT id, position FROM pages WHERE parent_id IS ? AND id != ? "
        "ORDER BY position, created_at, id",
        (parent_id, page_id),
    ).fetchall()
    sibling_ids = [row["id"] for row in rows]
    target = len(sibling_ids) if position is None else position
    if target > len(sibling_ids):
        conn.execute("UPDATE pages SET position = ? WHERE id = ?", (target, page_id))
        return []
    ordered = list(sibling_ids)
    ordered.insert(target, page_id)
    assignments: list[tuple[str, int]] = []
    for index, sibling_id in enumerate(ordered):
        if sibling_id == page_id:
            conn.execute("UPDATE pages SET position = ? WHERE id = ?", (index, page_id))
        else:
            assignments.append((sibling_id, index))
    return assignments


def _replace_tags(conn: sqlite3.Connection, page_id: str, tags: list[str]) -> None:
    conn.execute("DELETE FROM page_tags WHERE page_id = ?", (page_id,))
    for index, tag in enumerate(tags):
        conn.execute(
            "INSERT INTO page_tags(page_id, tag, position) VALUES (?, ?, ?)",
            (page_id, tag, index),
        )


def _replace_documents(conn: sqlite3.Connection, page_id: str,
                       document_ids: list[str]) -> None:
    conn.execute("DELETE FROM page_documents WHERE page_id = ?", (page_id,))
    for index, doc_id in enumerate(document_ids):
        conn.execute(
            "INSERT INTO page_documents(page_id, doc_id, position) VALUES (?, ?, ?)",
            (page_id, doc_id, index),
        )


def _locator(filename: str, unit: sqlite3.Row, doc: sqlite3.Row) -> str:
    if unit["kind"] == "page":
        position = f"PDF 第 {unit['file_page']} 页"
    else:
        position = (f"DOCX 文本单元 {unit['seq']}"
                    f"（{'段落' if unit['kind'] == 'paragraph' else '表格'}）")
    printed = f"，印刷页码 {unit['printed_page']}" if unit["printed_page"] else ""
    return f"{filename} · {position}{printed} · 单元 {unit['id']}"


def _canonicalize_body(workspace: Workspace, conn: sqlite3.Connection,
                       body: dict[str, Any]) -> dict[str, Any]:
    """结构校验后，用当前数据库记录填充所有引用的权威快照。"""
    normalized = note_blocks.normalize_body(body)
    cache: dict[str, bool] = {}
    for block in note_blocks.walk_blocks(normalized):
        kind = block["type"]
        props = block["props"]
        if kind == "image":
            asset = conn.execute(
                "SELECT * FROM note_assets WHERE id = ?", (props["assetId"],)
            ).fetchone()
            if asset is None:
                raise NoteReferenceError(f"找不到笔记图片：{props['assetId']}")
            props.update({
                "sha256": asset["sha256"],
                "media_type": asset["media_type"],
                "byte_size": int(asset["byte_size"]),
                "src": f"/api/v1/note-assets/{asset['id']}",
            })
            props.pop("missing", None)
        elif kind == "pageRef":
            target = conn.execute(
                "SELECT id, title, icon, archived FROM pages WHERE id = ?",
                (props["pageId"],),
            ).fetchone()
            if target is None:
                raise NoteReferenceError(f"找不到引用页面：{props['pageId']}")
            props.update({
                "title": target["title"],
                "icon": target["icon"],
                "archived": bool(target["archived"]),
                "effective_archived": _effective_archived(conn, target["id"], cache),
            })
            props.pop("missing", None)
        elif kind == "documentRef":
            document = conn.execute(
                "SELECT id, title, filename, author, year FROM documents WHERE id = ?",
                (props["documentId"],),
            ).fetchone()
            if document is None:
                raise NoteReferenceError(f"找不到引用文献：{props['documentId']}")
            props.update({
                "title": document["title"],
                "filename": document["filename"],
                "author": document["author"],
                "year": document["year"],
            })
            props.pop("missing", None)
        elif kind == "evidenceRef":
            evidence = conn.execute(
                "SELECT * FROM evidence WHERE id = ?", (props["evidenceId"],)
            ).fetchone()
            if evidence is None:
                raise NoteReferenceError(f"找不到引用摘录：{props['evidenceId']}")
            unit = conn.execute(
                "SELECT * FROM units WHERE id = ?", (evidence["unit_id"],)
            ).fetchone()
            document = conn.execute(
                "SELECT id, title, filename FROM documents WHERE id = ?",
                (evidence["doc_id"],),
            ).fetchone()
            if unit is None or document is None:  # pragma: no cover - 外键保护
                raise NoteReferenceError(f"摘录出处不完整：{props['evidenceId']}")
            props.update({
                "quote": evidence["quote"],
                "review_status": evidence["review_status"],
                "doc_id": evidence["doc_id"],
                "unit_id": evidence["unit_id"],
                "version": evidence["version"],
                "locator": _locator(document["filename"], unit, document),
                "filename": document["filename"],
                "title": document["title"],
                "file_page": unit["file_page"],
                "printed_page": unit["printed_page"],
                "printed_page_status": unit["printed_page_status"] or "none",
            })
            props.pop("missing", None)
    # 填充后的快照可能变大；再跑一遍完整限制校验。
    normalized = note_blocks.normalize_body(normalized)
    if note_blocks.body_size(normalized) > note_blocks.MAX_BODY_BYTES:
        raise ValidationError("页面正文含引用出处后超过 1 MiB，请缩短正文。")
    return normalized


def _present_body(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    """读取时补齐当前引用状态；目标缺失时标 ``missing``，不失败。"""
    present = json.loads(json.dumps(body, ensure_ascii=False))
    cache: dict[str, bool] = {}
    for block in note_blocks.walk_blocks(present):
        kind = block["type"]
        props = block["props"]
        if kind == "pageRef":
            target = conn.execute(
                "SELECT id, title, icon, archived FROM pages WHERE id = ?",
                (props.get("pageId"),),
            ).fetchone()
            if target is None:
                props["missing"] = True
            else:
                props.update({
                    "title": target["title"],
                    "icon": target["icon"],
                    "archived": bool(target["archived"]),
                    "effective_archived": _effective_archived(conn, target["id"], cache),
                })
                props.pop("missing", None)
        elif kind == "documentRef":
            document = conn.execute(
                "SELECT id, title, filename, author, year FROM documents WHERE id = ?",
                (props.get("documentId"),),
            ).fetchone()
            if document is None:
                props["missing"] = True
            else:
                props.update({
                    "title": document["title"],
                    "filename": document["filename"],
                    "author": document["author"],
                    "year": document["year"],
                })
                props.pop("missing", None)
        elif kind == "evidenceRef":
            evidence = conn.execute(
                "SELECT * FROM evidence WHERE id = ?", (props.get("evidenceId"),)
            ).fetchone()
            if evidence is None:
                props["missing"] = True
                continue
            unit = conn.execute(
                "SELECT * FROM units WHERE id = ?", (evidence["unit_id"],)
            ).fetchone()
            document = conn.execute(
                "SELECT id, title, filename FROM documents WHERE id = ?",
                (evidence["doc_id"],),
            ).fetchone()
            props["review_status"] = evidence["review_status"]
            if unit is not None and document is not None:
                props.update({
                    "quote": evidence["quote"],
                    "doc_id": evidence["doc_id"],
                    "unit_id": evidence["unit_id"],
                    "version": evidence["version"],
                    "locator": _locator(document["filename"], unit, document),
                    "filename": document["filename"],
                    "title": document["title"],
                    "file_page": unit["file_page"],
                    "printed_page": unit["printed_page"],
                    "printed_page_status": unit["printed_page_status"] or "none",
                })
            props.pop("missing", None)
        elif kind == "image":
            asset = conn.execute(
                "SELECT * FROM note_assets WHERE id = ?", (props.get("assetId"),)
            ).fetchone()
            if asset is None:
                props["missing"] = True
            else:
                props.update({
                    "sha256": asset["sha256"],
                    "media_type": asset["media_type"],
                    "byte_size": int(asset["byte_size"]),
                    "src": f"/api/v1/note-assets/{asset['id']}",
                })
                props.pop("missing", None)
    return present


def _page_response(conn: sqlite3.Connection, row: sqlite3.Row,
                   *, present: bool = True) -> dict[str, Any]:
    body = _loads_body(row["body_json"])
    if present:
        body = _present_body(conn, body)
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "title": row["title"],
        "icon": row["icon"],
        "body": body,
        "schema_version": int(row["schema_version"]),
        "revision": int(row["revision"]),
        "tags": _tags_for(conn, row["id"]),
        "status": row["status"],
        "archived": bool(row["archived"]),
        "effective_archived": _effective_archived(conn, row["id"]),
        "position": int(row["position"]),
        "favorite": bool(row["favorite"]),
        "document_ids": _documents_for(conn, row["id"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "url": f"/pages/{row['id']}",
    }


def _replace_links(conn: sqlite3.Connection, page_id: str,
                   body: dict[str, Any]) -> None:
    conn.execute("DELETE FROM page_links WHERE page_id = ?", (page_id,))
    now = utcnow()
    for reference in note_blocks.extract_references(body):
        conn.execute(
            "INSERT INTO page_links(page_id, block_id, kind, target_id, snapshot_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (page_id, reference["block_id"], reference["kind"],
             reference["target_id"], _dumps(reference["props"]), now),
        )


def _record_revision(conn: sqlite3.Connection, page_id: str, *, origin: str,
                     actor: str, request_id: str) -> None:
    row = _page_row(conn, page_id)
    snapshot = _page_response(conn, row, present=False)
    conn.execute(
        "INSERT INTO page_revisions(page_id, revision, snapshot_json, origin, actor, "
        "request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (page_id, int(row["revision"]), _dumps(snapshot), origin, actor, request_id,
         row["updated_at"]),
    )


def _check_mutation_request(conn: sqlite3.Connection, *, actor: str, request_id: str,
                            operation: str, digest: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT operation, digest, result_json FROM mutation_requests "
        "WHERE owner = ? AND request_id = ?",
        (actor, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["operation"] != operation or row["digest"] != digest:
        raise RequestIdReuseError(
            "这个 request_id 已用于不同的页面操作；请用新的 request_id 重试。"
        )
    return json.loads(row["result_json"])


def _record_mutation_request(conn: sqlite3.Connection, *, actor: str, request_id: str,
                             operation: str, digest: str, result: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO mutation_requests(owner, request_id, operation, digest, "
        "result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (actor, request_id, operation, digest, _dumps(result), utcnow()),
    )


def _digest(operation: str, payload: dict[str, Any]) -> str:
    return sha256_text(_dumps({"operation": operation, "payload": payload}))


def _search_text(title: str, body: dict[str, Any]) -> str:
    return f"{title}\n{note_blocks.extract_text(body)}".strip()


# ------------------------------------------------------------------ 读


def get_page(workspace: Workspace, page_id: str) -> dict[str, Any]:
    with workspace.connect() as conn:
        conn.execute("BEGIN")
        row = _page_row(conn, page_id)
        return _page_response(conn, row)


def _normalize_page_args(page: Any, page_size: Any) -> tuple[int, int]:
    try:
        number = int(page) if page not in (None, "") else 1
    except (TypeError, ValueError) as exc:
        raise ValidationError("page 必须是正整数。") from exc
    try:
        size = int(page_size) if page_size not in (None, "") else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError) as exc:
        raise ValidationError("page_size 必须是正整数。") from exc
    if number < 1:
        raise ValidationError("page 从 1 开始。")
    if size < 1 or size > MAX_PAGE_SIZE:
        raise ValidationError(f"page_size 必须在 1–{MAX_PAGE_SIZE} 之间。")
    return number, size


def list_pages(workspace: Workspace, *, query: str = "", include_archived: bool = False,
               page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    page, page_size = _normalize_page_args(page, page_size)
    needle = (query or "").strip()
    clauses: list[str] = []
    params: list[Any] = []
    if not include_archived:
        clauses.append(
            "p.id NOT IN (WITH RECURSIVE archived(id) AS ("
            "SELECT id FROM pages WHERE archived = 1 "
            "UNION SELECT child.id FROM pages child JOIN archived parent "
            "ON child.parent_id = parent.id) SELECT id FROM archived)"
        )
    if needle:
        clauses.append(
            "(instr(p.title, ?) > 0 OR instr(p.search_text, ?) > 0 "
            "OR EXISTS (SELECT 1 FROM page_tags t WHERE t.page_id = p.id "
            "AND instr(t.tag, ?) > 0))"
        )
        params.extend([needle, needle, needle])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with workspace.connect() as conn:
        conn.execute("BEGIN")
        total = int(conn.execute(
            f"SELECT COUNT(*) AS n FROM pages p {where}", params
        ).fetchone()["n"])
        rows = conn.execute(
            f"SELECT p.* FROM pages p {where} "
            f"ORDER BY p.position, p.created_at, p.id LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        items = [_page_response(conn, row) for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_page_history(workspace: Workspace, page_id: str, *, page: int = 1,
                     page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    page, page_size = _normalize_page_args(page, page_size)
    with workspace.connect() as conn:
        conn.execute("BEGIN")
        _page_row(conn, page_id)
        total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM page_revisions WHERE page_id = ?", (page_id,)
        ).fetchone()["n"])
        rows = conn.execute(
            "SELECT * FROM page_revisions WHERE page_id = ? "
            "ORDER BY revision DESC LIMIT ? OFFSET ?",
            (page_id, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [{
            "revision": int(row["revision"]),
            "origin": row["origin"],
            "actor": row["actor"],
            "request_id": row["request_id"],
            "created_at": row["created_at"],
            "snapshot": _loads(row["snapshot_json"]),
        } for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_backlinks(workspace: Workspace, page_id: str, *, page: int = 1,
                  page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    page, page_size = _normalize_page_args(page, page_size)
    with workspace.connect() as conn:
        conn.execute("BEGIN")
        _page_row(conn, page_id)
        total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM page_links WHERE kind = 'page' AND target_id = ?",
            (page_id,),
        ).fetchone()["n"])
        rows = conn.execute(
            "SELECT pl.block_id, pl.page_id, p.title, p.archived, p.updated_at "
            "FROM page_links pl JOIN pages p ON p.id = pl.page_id "
            "WHERE pl.kind = 'page' AND pl.target_id = ? "
            "ORDER BY p.updated_at DESC, p.id, pl.block_id LIMIT ? OFFSET ?",
            (page_id, page_size, (page - 1) * page_size),
        ).fetchall()
        cache: dict[str, bool] = {}
        items = [{
            "page_id": row["page_id"],
            "title": row["title"],
            "block_id": row["block_id"],
            "archived": bool(row["archived"]),
            "effective_archived": _effective_archived(conn, row["page_id"], cache),
            "updated_at": row["updated_at"],
            "url": f"/pages/{row['page_id']}",
        } for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ------------------------------------------------------------------ 写


def create_page(workspace: Workspace, *, title: str, parent_id: str | None,
                body: dict | None, properties: dict | None, request_id: str,
                actor: str = "owner", origin: str = "web") -> dict[str, Any]:
    title = _require_str(title, "标题", max_length=MAX_TITLE_LENGTH)
    request_id = _require_request_id(request_id)
    actor = _require_actor(actor)
    origin = _require_origin(origin)
    if parent_id is not None:
        parent_id = _require_str(parent_id, "parent_id", max_length=200)
    normalized_body = note_blocks.normalize_body(
        note_blocks.empty_body() if body is None else body
    )
    props = _normalize_properties(properties, for_create=True)
    digest = _digest("create_page", {
        "title": title, "parent_id": parent_id, "body": normalized_body,
        "properties": props,
    })
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        replayed = _check_mutation_request(
            conn, actor=actor, request_id=request_id,
            operation="create_page", digest=digest,
        )
        if replayed is not None:
            return replayed
        if parent_id is not None:
            _assert_valid_parent(conn, None, parent_id)
        document_ids = props.get("document_ids")
        if document_ids is not None:
            _validate_documents(conn, document_ids)
        canonical = _canonicalize_body(workspace, conn, normalized_body)
        page_id = _new_page_id()
        now = utcnow()
        conn.execute(
            "INSERT INTO pages(id, parent_id, title, icon, status, position, favorite, "
            "body_json, schema_version, revision, archived, search_text, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?, ?, ?)",
            (page_id, parent_id, title, props.get("icon", ""),
             props.get("status", "not_started"), int(bool(props.get("favorite", False))),
             _dumps(canonical), note_blocks.SCHEMA_VERSION,
             int(bool(props.get("archived", False))), _search_text(title, canonical),
             now, now),
        )
        _create_position(conn, parent_id, page_id, props.get("position"),
                         origin=origin, actor=actor, request_id=request_id)
        _replace_tags(conn, page_id, props.get("tags", []))
        _replace_documents(conn, page_id, props.get("document_ids", []))
        _replace_links(conn, page_id, canonical)
        _record_revision(conn, page_id, origin=origin, actor=actor, request_id=request_id)
        row = _page_row(conn, page_id)
        result = _page_response(conn, row)
        _record_mutation_request(conn, actor=actor, request_id=request_id,
                                 operation="create_page", digest=digest, result=result)
        return result


def update_page(workspace: Workspace, page_id: str, *, expected_revision: int,
                request_id: str, changes: dict, actor: str = "owner",
                origin: str = "web") -> dict[str, Any]:
    request_id = _require_request_id(request_id)
    expected_revision = _require_revision(expected_revision)
    actor = _require_actor(actor)
    origin = _require_origin(origin)
    if not isinstance(changes, dict):
        raise ValidationError("changes 必须是对象。")
    unknown = set(changes) - _UPDATE_KEYS
    if unknown:
        raise ValidationError(f"页面更新包含未知字段：{'、'.join(sorted(unknown))}。")
    normalized: dict[str, Any] = {}
    if "title" in changes:
        normalized["title"] = _require_str(changes["title"], "标题",
                                           max_length=MAX_TITLE_LENGTH)
    if "icon" in changes:
        normalized["icon"] = _require_str(changes["icon"], "图标", allow_empty=True,
                                          max_length=200)
    if "tags" in changes:
        normalized["tags"] = _normalize_tags(changes["tags"])
    if "status" in changes:
        if changes["status"] not in PAGE_STATUSES:
            raise ValidationError(
                f"未知的页面状态：{changes['status']!r}"
                f"（可选 {'/'.join(PAGE_STATUSES)}）"
            )
        normalized["status"] = changes["status"]
    if "favorite" in changes:
        if not isinstance(changes["favorite"], bool):
            raise ValidationError("favorite 必须是布尔值。")
        normalized["favorite"] = changes["favorite"]
    if "document_ids" in changes:
        normalized["document_ids"] = _normalize_document_ids(changes["document_ids"])
    if "position" in changes:
        normalized["position"] = _require_position(changes["position"])
    if "archived" in changes:
        if not isinstance(changes["archived"], bool):
            raise ValidationError("archived 必须是布尔值。")
        normalized["archived"] = changes["archived"]
    if "parent_id" in changes:
        parent_id = changes["parent_id"]
        if parent_id is not None:
            parent_id = _require_str(parent_id, "parent_id", max_length=200)
        normalized["parent_id"] = parent_id
    if "body" in changes:
        normalized["body"] = note_blocks.normalize_body(changes["body"])
    digest = _digest("update_page", {
        "page_id": page_id, "expected_revision": expected_revision,
        "changes": normalized,
    })
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        replayed = _check_mutation_request(
            conn, actor=actor, request_id=request_id,
            operation="update_page", digest=digest,
        )
        if replayed is not None:
            return replayed
        row = _page_row(conn, page_id)
        current_revision = int(row["revision"])
        if current_revision != expected_revision:
            raise RevisionConflictError(
                f"页面 {page_id} 的当前修订号是 {current_revision}，"
                f"不是提交时的 {expected_revision}；请保留草稿并基于最新版本重试。"
            )
        if not normalized:
            result = _page_response(conn, row)
            _record_mutation_request(conn, actor=actor, request_id=request_id,
                                     operation="update_page", digest=digest, result=result)
            return result

        if _effective_archived(conn, page_id) and (
            set(normalized) - {"archived", "parent_id", "position"}
        ):
            raise ValidationError("已归档页面的内容只读，请先恢复页面或其父页面。")

        document_ids = normalized.get("document_ids")
        if document_ids is not None:
            _validate_documents(conn, document_ids)
        parent_id = normalized.get("parent_id", row["parent_id"])
        parent_changed = "parent_id" in normalized and parent_id != row["parent_id"]
        position = normalized.get("position")
        body_changed = "body" in normalized
        canonical = None
        if body_changed:
            canonical = _canonicalize_body(workspace, conn, normalized["body"])

        fields: dict[str, Any] = {}
        for field in ("title", "icon", "status"):
            if field in normalized:
                fields[field] = normalized[field]
        if "favorite" in normalized:
            fields["favorite"] = int(normalized["favorite"])
        if "archived" in normalized:
            fields["archived"] = int(normalized["archived"])
        if parent_changed:
            _assert_valid_parent(conn, page_id, parent_id)
            fields["parent_id"] = parent_id
        if canonical is not None:
            fields["body_json"] = _dumps(canonical)
        if "title" in normalized or canonical is not None:
            fields["search_text"] = _search_text(
                normalized.get("title", row["title"]),
                canonical if canonical is not None else _loads_body(row["body_json"]),
            )

        current_docs = _documents_for(conn, page_id)
        docs_changed = document_ids is not None and document_ids != current_docs
        current_tags = _tags_for(conn, page_id)
        tags_changed = "tags" in normalized and normalized["tags"] != current_tags
        order_changed = parent_changed or (position is not None
                                           and position != int(row["position"]))
        body_changed_effectively = (
            "body" in normalized and canonical != _loads_body(row["body_json"])
        )
        metadata_changed = any(key not in {"body_json", "search_text"} for key in fields)
        effective_change = bool(
            metadata_changed or tags_changed or docs_changed or order_changed
            or body_changed_effectively
        )
        if not effective_change:
            result = _page_response(conn, row)
            _record_mutation_request(conn, actor=actor, request_id=request_id,
                                     operation="update_page", digest=digest, result=result)
            return result

        if parent_changed:
            conn.execute("UPDATE pages SET parent_id = ? WHERE id = ?", (parent_id, page_id))
        if order_changed:
            assignments = _place_existing(
                conn, parent_id, page_id,
                position if position is not None else None,
            )
            _bump_sibling_positions(conn, assignments, origin=origin, actor=actor,
                                    request_id=request_id)
            fields["position"] = conn.execute(
                "SELECT position FROM pages WHERE id = ?", (page_id,)
            ).fetchone()["position"]
        fields["revision"] = current_revision + 1
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(
            f"UPDATE pages SET {assignments} WHERE id = ?",
            [*fields.values(), page_id],
        )
        if tags_changed:
            _replace_tags(conn, page_id, normalized["tags"])
        if docs_changed:
            _replace_documents(conn, page_id, document_ids)
        if canonical is not None:
            _replace_links(conn, page_id, canonical)
        _record_revision(conn, page_id, origin=origin, actor=actor, request_id=request_id)
        updated_row = _page_row(conn, page_id)
        result = _page_response(conn, updated_row)
        _record_mutation_request(conn, actor=actor, request_id=request_id,
                                 operation="update_page", digest=digest, result=result)
        return result


def restore_page(workspace: Workspace, page_id: str, *, expected_revision: int,
                 request_id: str, revision: int, actor: str = "owner",
                 origin: str = "web") -> dict[str, Any]:
    request_id = _require_request_id(request_id)
    expected_revision = _require_revision(expected_revision)
    actor = _require_actor(actor)
    origin = _require_origin(origin)
    if type(revision) is not int or revision < 1:
        raise ValidationError("revision 必须是正整数。")
    digest = _digest("restore_page", {
        "page_id": page_id, "expected_revision": expected_revision,
        "revision": revision,
    })
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        replayed = _check_mutation_request(
            conn, actor=actor, request_id=request_id,
            operation="restore_page", digest=digest,
        )
        if replayed is not None:
            return replayed
        row = _page_row(conn, page_id)
        current_revision = int(row["revision"])
        if current_revision != expected_revision:
            raise RevisionConflictError(
                f"页面 {page_id} 的当前修订号是 {current_revision}，"
                f"不是提交时的 {expected_revision}。"
            )
        target = conn.execute(
            "SELECT snapshot_json FROM page_revisions WHERE page_id = ? AND revision = ?",
            (page_id, revision),
        ).fetchone()
        if _effective_archived(conn, page_id):
            raise ValidationError("已归档页面不能恢复历史内容，请先取消归档。")
        if target is None:
            raise NotFoundError(f"页面 {page_id} 没有修订 {revision}。")
        snapshot = _loads(target["snapshot_json"])
        parent_id = snapshot.get("parent_id")
        _assert_valid_parent(conn, page_id, parent_id)
        document_ids = _normalize_document_ids(snapshot.get("document_ids") or [])
        _validate_documents(conn, document_ids)
        canonical = _canonicalize_body(
            workspace, conn,
            snapshot.get("body") or note_blocks.empty_body(),
        )
        parent_changed = parent_id != row["parent_id"]
        if parent_changed:
            conn.execute("UPDATE pages SET parent_id = ? WHERE id = ?", (parent_id, page_id))
        assignments = _place_existing(conn, parent_id, page_id, snapshot.get("position"))
        _bump_sibling_positions(conn, assignments, origin=origin, actor=actor,
                                request_id=request_id)
        new_revision = current_revision + 1
        now = utcnow()
        conn.execute(
            "UPDATE pages SET title = ?, icon = ?, status = ?, favorite = ?, archived = ?, "
            "body_json = ?, schema_version = ?, revision = ?, search_text = ?, "
            "updated_at = ? WHERE id = ?",
            (snapshot.get("title") or row["title"], snapshot.get("icon", ""),
             snapshot.get("status", "not_started"),
             int(bool(snapshot.get("favorite", False))),
             int(bool(snapshot.get("archived", False))), _dumps(canonical),
             note_blocks.SCHEMA_VERSION, new_revision,
             _search_text(snapshot.get("title") or row["title"], canonical),
             now, page_id),
        )
        _replace_tags(conn, page_id, _normalize_tags(snapshot.get("tags") or []))
        _replace_documents(conn, page_id, document_ids)
        _replace_links(conn, page_id, canonical)
        _record_revision(conn, page_id, origin=origin, actor=actor, request_id=request_id)
        restored = _page_row(conn, page_id)
        result = _page_response(conn, restored)
        _record_mutation_request(conn, actor=actor, request_id=request_id,
                                 operation="restore_page", digest=digest, result=result)
        return result


def edit_page_blocks(workspace: Workspace, page_id: str, *, expected_revision: int,
                     request_id: str, operations: Any, actor: str = "owner",
                     origin: str = "web") -> dict[str, Any]:
    request_id = _require_request_id(request_id)
    expected_revision = _require_revision(expected_revision)
    actor = _require_actor(actor)
    origin = _require_origin(origin)
    digest = _digest("edit_page_blocks", {
        "page_id": page_id, "expected_revision": expected_revision,
        "operations": operations,
    })
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        replayed = _check_mutation_request(
            conn, actor=actor, request_id=request_id,
            operation="edit_page_blocks", digest=digest,
        )
        if replayed is not None:
            return replayed
        row = _page_row(conn, page_id)
        current_revision = int(row["revision"])
        if current_revision != expected_revision:
            raise RevisionConflictError(
                f"页面 {page_id} 的当前修订号是 {current_revision}，"
                f"不是提交时的 {expected_revision}。"
            )
        current_body = _loads_body(row["body_json"])
        if _effective_archived(conn, page_id):
            raise ValidationError("已归档页面的内容只读，请先恢复页面或其父页面。")
        changed_body = note_blocks.apply_operations(current_body, operations)
        if changed_body == current_body:
            result = _page_response(conn, row)
            _record_mutation_request(conn, actor=actor, request_id=request_id,
                                     operation="edit_page_blocks", digest=digest, result=result)
            return result
        canonical = _canonicalize_body(workspace, conn, changed_body)
        new_revision = current_revision + 1
        now = utcnow()
        conn.execute(
            "UPDATE pages SET body_json = ?, schema_version = ?, revision = ?, "
            "search_text = ?, updated_at = ? WHERE id = ?",
            (_dumps(canonical), note_blocks.SCHEMA_VERSION, new_revision,
             _search_text(row["title"], canonical), now, page_id),
        )
        _replace_links(conn, page_id, canonical)
        _record_revision(conn, page_id, origin=origin, actor=actor, request_id=request_id)
        updated = _page_row(conn, page_id)
        result = _page_response(conn, updated)
        _record_mutation_request(conn, actor=actor, request_id=request_id,
                                 operation="edit_page_blocks", digest=digest, result=result)
        return result


__all__ = [
    "NoteReferenceError",
    "PAGE_STATUSES",
    "RequestIdReuseError",
    "RevisionConflictError",
    "create_page",
    "edit_page_blocks",
    "get_backlinks",
    "get_page",
    "get_page_history",
    "list_pages",
    "restore_page",
    "update_page",
]
