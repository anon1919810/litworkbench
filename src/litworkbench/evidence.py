"""摘录（证据）、校订与审核。

规则：

* 引文必须逐字出现在所选版本的原文中，否则拒绝（不做任何「大概意思」匹配）；
* 摘录记录精确偏移与版本快照（原文哈希），便于日后核对；
* 校订另存为 ``corrected_text``，不会覆盖原始识别结果；
* 校订后，旧摘录自动标记为 ``stale``（待复核），由人工决定是否重新审核。
"""

from __future__ import annotations

import uuid
import sqlite3
from contextlib import nullcontext
from typing import Any

from litworkbench.errors import EditConflictError, NotFoundError, QuotationError
from litworkbench.store import (
    Workspace,
    ensure_write_transaction,
    sha256_text,
    utcnow,
)
from litworkbench.text import find_quote

VERSION_ORIGINAL = "original"
VERSION_CORRECTED = "corrected"

REVIEW_PENDING = "pending"
REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"
REVIEW_STALE = "stale"
REVIEW_STATUSES = (REVIEW_PENDING, REVIEW_ACCEPTED, REVIEW_REJECTED, REVIEW_STALE)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def effective_text(unit: dict[str, Any]) -> tuple[str, str]:
    """返回 ``(文本, 版本)``；有校订时默认使用校订版。"""
    if unit.get("corrected_text") is not None:
        return unit["corrected_text"], VERSION_CORRECTED
    return unit.get("original_text") or "", VERSION_ORIGINAL


def available_versions(unit: dict[str, Any]) -> list[str]:
    versions = [VERSION_ORIGINAL]
    if unit.get("corrected_text") is not None:
        versions.append(VERSION_CORRECTED)
    return versions


def save_evidence(workspace: Workspace, unit_id: str, quote: str, category: str = "",
                  note: str = "", task_id: str | None = None,
                  version: str | None = None, start_offset: int | None = None,
                  expected_hash: str | None = None, strict: bool = False,
                  connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """保存一条摘录；引文不属于原文时抛 :class:`QuotationError`。

    网页端传入 ``expected_hash`` + ``strict=True`` + ``connection``：

    * 先 ``BEGIN IMMEDIATE``（自己拥有连接时）拿写锁，再**读当前文字**校验哈希，
      「校验 -> 写入」之间不会被后台 OCR 或并发校订插入（没有 TOCTOU）；
    * ``strict=True`` 时要求 ``quote`` 正好是「从 ``start_offset`` 开始的切片」，
      不接受跨空白折叠的近似匹配（命令行保持原来的宽松定位行为）；
    * INSERT 之后用**同一个连接**读回记录，避免读不到未提交数据。
    """
    owned = connection is None
    del owned  # 仅用于文档说明：自己拥有连接时由 workspace.connect() 负责提交
    with (nullcontext(connection) if connection is not None else workspace.connect()) as conn:
        # 关键顺序：先取写锁，再读当前文字。
        ensure_write_transaction(conn)
        unit = workspace.get_unit(unit_id, connection=conn)
        if version is None:
            _, version = effective_text(unit)
        if version not in available_versions(unit):
            raise QuotationError(
                f"单元 {unit_id} 没有「{version}」版本，可用版本：{'、'.join(available_versions(unit))}"
            )
        text = unit["corrected_text"] if version == VERSION_CORRECTED else unit["original_text"]
        text = text or ""
        _check_expected_hash(unit_id, text, expected_hash)
        if start_offset is None:
            span = find_quote(text, quote)
        else:
            if type(start_offset) is not int or not 0 <= start_offset <= len(text):
                raise QuotationError("引文起始偏移无效。")
            if strict:
                # 严格模式：必须是「从该偏移开始的精确切片」。
                if text[start_offset:start_offset + len(quote)] != quote:
                    raise QuotationError(
                        f"引文与单元 {unit_id} 偏移 {start_offset} 处的原文不一致，"
                        f"拒绝保存：{quote[:30]!r}"
                    )
                span = (start_offset, start_offset + len(quote))
            else:
                local_span = find_quote(text[start_offset:], quote)
                span = (start_offset, start_offset + local_span[1]) if local_span and local_span[0] == 0 else None
        if span is None:
            raise QuotationError(
                f"引文未在单元 {unit_id} 的「{version}」文本中从偏移 {start_offset} 处找到，拒绝保存：{quote[:30]!r}"
            )
        start, end = span
        # 存原文的精确切片：跨行匹配时原文里含有换行，quote 必须与 offsets 自洽
        exact = text[start:end]
        if task_id is not None:
            _require_task(workspace, task_id, connection=conn)
        now = utcnow()
        evidence_id = new_id("ev")
        snapshot = {
            "version": version,
            "unit_text_sha256": sha256_text(text),
            "unit_text_length": len(text),
            "method": unit["method"],
            "requested_quote": quote,
            "captured_at": now,
        }
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, task_id, quote, start_offset, "
            "end_offset, version, version_snapshot, category, note, review_status, "
            "review_note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
            (evidence_id, unit_id, unit["doc_id"], task_id, exact, start, end, version,
             _dumps(snapshot), category, note, REVIEW_PENDING, now, now),
        )
        # 必须在同一个连接上读回：未提交的行对别的连接不可见。
        row = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        return _decorate(workspace, dict(row))


def _check_expected_hash(unit_id: str, text: str, expected_hash: str | None) -> None:
    """乐观并发校验；不匹配时抛错，调用方事务随即回滚，不会写入任何东西。"""
    if not expected_hash:
        return
    digest = sha256_text(text or "")
    if digest != expected_hash:
        raise EditConflictError(
            f"单元 {unit_id} 的文字已被其他操作改动（期望 {expected_hash[:12]}…，"
            f"当前 {digest[:12]}…），请重新核对后再保存。"
        )


def get_evidence(workspace: Workspace, evidence_id: str) -> dict[str, Any]:
    with workspace.connect() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到摘录：{evidence_id}")
    return _decorate(workspace, dict(row))


def list_evidence(workspace: Workspace, task_id: str | None = None,
                  doc_id: str | None = None, unit_id: str | None = None,
                  review_status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    clauses, params = [], []
    if task_id:
        clauses.append("e.task_id = ?")
        params.append(task_id)
    if doc_id:
        clauses.append("e.doc_id = ?")
        params.append(doc_id)
    if unit_id:
        clauses.append("e.unit_id = ?")
        params.append(unit_id)
    if review_status:
        if review_status not in REVIEW_STATUSES:
            raise QuotationError(
                f"未知的核验状态：{review_status}（可选 {'/'.join(REVIEW_STATUSES)}）"
            )
        clauses.append("e.review_status = ?")
        params.append(review_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with workspace.connect() as conn:
        rows = conn.execute(
            f"SELECT e.* FROM evidence e {where} ORDER BY e.created_at DESC, e.id LIMIT ?",
            params,
        ).fetchall()
    return [_decorate(workspace, dict(row)) for row in rows]


def review_evidence(workspace: Workspace, evidence_id: str, status: str,
                    note: str = "") -> dict[str, Any]:
    if status not in REVIEW_STATUSES:
        raise QuotationError(
            f"未知的核验状态：{status}（可选 {'/'.join(REVIEW_STATUSES)}）"
        )
    get_evidence(workspace, evidence_id)
    with workspace.connect() as conn:
        conn.execute(
            "UPDATE evidence SET review_status = ?, review_note = ?, updated_at = ? WHERE id = ?",
            (status, note, utcnow(), evidence_id),
        )
    return get_evidence(workspace, evidence_id)


def correct_unit(workspace: Workspace, unit_id: str, text: str, reason: str, *,
                 expected_hash: str | None = None,
                 connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """写入人工校订：原始识别结果不变，相关旧摘录标记为待复核。

    ``expected_hash`` 与 ``connection`` 供网页端使用：``BEGIN IMMEDIATE`` 拿写锁
    之后，「读当前文字 -> 校验哈希 -> 写入 -> 标记旧摘录需复核」在同一个事务里，
    后台识别或并发校订无法插进中间造成 TOCTOU。
    """
    if not reason.strip():
        raise QuotationError("校订必须填写原因（--reason），以便追溯。")
    with (nullcontext(connection) if connection is not None else workspace.connect()) as conn:
        # 先取写锁再读当前文字：没有这一步，哈希校验就是 TOCTOU。
        ensure_write_transaction(conn)
        unit = workspace.get_unit(unit_id, connection=conn)
        previous, _ = effective_text(unit)
        _check_expected_hash(unit_id, previous, expected_hash)
        unit["corrected_text"] = text
        unit["status"] = "completed" if text.strip() else "pending"
        unit["notes"] = unit.get("notes", "")
        saved = workspace.save_unit(unit, connection=conn)
        workspace.add_revision(unit_id, "correction", previous, text, reason, connection=conn)
        _mark_stale_evidence(workspace, unit_id, connection=conn)
    return saved


def clear_correction(workspace: Workspace, unit_id: str, reason: str) -> dict[str, Any]:
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        unit = workspace.get_unit(unit_id, connection=conn)
        if unit.get("corrected_text") is None:
            return unit
        if not reason.strip():
            raise QuotationError("撤销校订必须填写原因。")
        previous = unit["corrected_text"]
        unit["corrected_text"] = None
        unit["status"] = unit["extraction_status"]
        saved = workspace.save_unit(unit, connection=conn)
        workspace.add_revision(unit_id, "correction-revert", previous, unit["original_text"], reason, connection=conn)
        _mark_stale_evidence(workspace, unit_id, connection=conn)
    return saved


def _mark_stale_evidence(workspace: Workspace, unit_id: str, *,
                         connection: sqlite3.Connection | None = None) -> None:
    """按**记录时所用的那个版本**的当前文字判断摘录是否仍然成立。

    * 记录版本已经不是当前有效版本（例如后来加了校订）-> 需复核；
    * 记录版本的文字变了（例如重新 OCR 改写了原文）-> 需复核；
    * 其余情况保持原状态：保留的校订文字没有被新 OCR 改动时，
      引用校订版的摘录**不会**被无谓地标成需复核。
    """
    with (nullcontext(connection) if connection is not None else workspace.connect()) as conn:
        unit_row = conn.execute(
            "SELECT original_text, corrected_text FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
        if unit_row is None:
            return
        effective_version = (
            VERSION_CORRECTED if unit_row["corrected_text"] is not None else VERSION_ORIGINAL
        )
        versions = {
            VERSION_ORIGINAL: unit_row["original_text"] or "",
            VERSION_CORRECTED: unit_row["corrected_text"] or "",
        }
        rows = conn.execute(
            "SELECT id, version, version_snapshot, review_status FROM evidence WHERE unit_id = ?",
            (unit_id,),
        ).fetchall()
        for row in rows:
            if row["review_status"] in {REVIEW_REJECTED, REVIEW_STALE}:
                continue
            snapshot = _loads(row["version_snapshot"])
            recorded_version = snapshot.get("version") or row["version"] or VERSION_ORIGINAL
            if recorded_version != effective_version:
                digest = None  # 记录版本不再是当前版本，需要复核
            else:
                digest = sha256_text(versions.get(recorded_version, ""))
            if digest is not None and snapshot.get("unit_text_sha256") == digest:
                continue
            conn.execute(
                "UPDATE evidence SET review_status = ?, review_note = ?, updated_at = ? "
                "WHERE id = ?",
                (REVIEW_STALE, "校订后需要复核", utcnow(), row["id"]),
            )


def _decorate(workspace: Workspace, row: dict[str, Any]) -> dict[str, Any]:
    import json

    document = workspace.get_document(row["doc_id"])
    unit = workspace.get_unit(row["unit_id"])
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "unit_id": row["unit_id"],
        "doc_id": row["doc_id"],
        "filename": document["filename"],
        "title": document["title"],
        "kind": unit["kind"],
        "file_page": unit["file_page"],
        "printed_page": unit["printed_page"],
        "quote": row["quote"],
        "start_offset": row["start_offset"],
        "end_offset": row["end_offset"],
        "version": row["version"],
        "version_snapshot": _loads(row["version_snapshot"]),
        "category": row["category"],
        "note": row["note"],
        "review_status": row["review_status"],
        "review_note": row["review_note"],
        "locator": locator(workspace, row["unit_id"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def locator(workspace: Workspace, unit_id: str, *,
            connection: sqlite3.Connection | None = None) -> str:
    """可核验的定位串：文件名 + 页/段 + 单元 ID。

    ``connection`` 供导出使用：定位串与书目、页码来自同一个读事务。
    """
    unit = workspace.get_unit(unit_id, connection=connection)
    document = workspace.get_document(unit["doc_id"], connection=connection)
    if unit["kind"] == "page":
        position = f"PDF 第 {unit['file_page']} 页"
    else:
        position = f"DOCX 文本单元 {unit['seq']}（{'段落' if unit['kind'] == 'paragraph' else '表格'}）"
    printed = f"，印刷页码 {unit['printed_page']}" if unit["printed_page"] else ""
    return f"{document['filename']} · {position}{printed} · 单元 {unit['id']}"


def _require_task(workspace: Workspace, task_id: str,
                  *, connection: sqlite3.Connection | None = None) -> None:
    with (nullcontext(connection) if connection is not None else workspace.connect()) as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到研究任务：{task_id}")


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> dict[str, Any]:
    import json

    try:
        return json.loads(value) if value else {}
    except json.JSONDecodeError:  # pragma: no cover - 容错
        return {}
