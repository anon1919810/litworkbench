"""中文全文索引：FTS5 字符码点位置编码 + 当前原文回查。

编码规则（与 ``docs/superpowers/specs/2026-10-15-web-workbench-design.md`` 一致）：

* 对去空白后的每一个 Unicode 字符产生 ``u`` + 六位十六进制码点的 token，
  用空格连接后交给 FTS5 的 ``unicode61`` 分词器；
* 查询按同一规则编码，单字、双字、标点、跨行短语都走同一条索引路径；
* 命中后再回到**当前版本原文**上计算偏移与片段，索引里永远不保存展示文本。

一致性：

* ``units`` 上的触发器在文字写入的同一事务里把该单元标为 ``dirty`` 并增加
  ``meta.index_revision``；``index_unit`` 重建后标为 ``indexed`` 并再增加代次；
* 检索会过滤掉 ``dirty`` 或哈希对不上的单元，因此**校订后旧文字不会继续命中**；
* 检索永远通过 FTS5 取候选，不会退化成整表 ``LIKE`` 全扫描。
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from typing import Any, Iterable, Sequence

from litworkbench.errors import ValidationError
from litworkbench.store import Workspace, ensure_write_transaction, sha256_text, utcnow
from litworkbench.text import find_quote, normalize, snippet

FTS_TABLE = "unit_fts"
STATE_TABLE = "index_units"
REVISION_KEY = "index_revision"

STATE_INDEXED = "indexed"
STATE_DIRTY = "dirty"
STATE_BLANK = "blank"
STATE_EMPTY = "empty"

#: 只有「已完成」的单元才进入索引与覆盖统计：失败页或待确认页不算覆盖。
STATE_COMPLETED_UNIT = "completed"

MODE_PHRASE = "phrase"
MODE_ALL_TERMS = "all_terms"
MODE_ANY_TERMS = "any_terms"
MODES = (MODE_PHRASE, MODE_ALL_TERMS, MODE_ANY_TERMS)

#: 候选上限：宁可显式报错也不悄悄截断总数（正常语料远小于该值）。
CANDIDATE_LIMIT = 20000


class IndexRevisionChanged(Exception):
    """分页代次冲突：前端应保留条件并重查第一页。"""

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__("检索索引已更新，请重新查询第一页。")
        self.details = details


def encode_characters(text: str) -> str:
    """把文本编码成 ``u00xxxx`` 形式的 token 流（去空白，大小写无关但确定）。"""
    return " ".join(f"u{ord(ch):06x}" for ch in text if not ch.isspace())


def phrase_query(phrase: str) -> str:
    """把一段短语编码成 FTS5 位置短语；空白被忽略，因此可跨行匹配。"""
    tokens = encode_characters(phrase)
    if not tokens:
        raise ValidationError("检索词为空：请输入至少一个非空白字符。")
    return f'"{tokens}"'


def build_match(query: str, mode: str) -> tuple[str, list[str]]:
    """返回 ``(FTS5 MATCH 表达式, 检索词列表)``。"""
    if mode not in MODES:
        raise ValidationError(f"未知的检索模式：{mode}（可选 {'/'.join(MODES)}）")
    terms = [term for term in (normalize(part) for part in query.split()) if term]
    if mode == MODE_PHRASE:
        phrase = normalize(query)
        if not phrase:
            raise ValidationError("检索词为空：请输入至少一个非空白字符。")
        return phrase_query(phrase), [phrase]
    if not terms:
        raise ValidationError("检索词为空：请输入至少一个非空白字符。")
    joiner = " AND " if mode == MODE_ALL_TERMS else " OR "
    return joiner.join(phrase_query(term) for term in terms), terms


# ------------------------------------------------------------------ 代次


def current_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (REVISION_KEY,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):  # pragma: no cover - 手工改坏的值
        return 0


def bump_revision(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('index_revision', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )
    return current_revision(conn)


def _ctx(workspace: Workspace, connection: sqlite3.Connection | None):
    return nullcontext(connection) if connection is not None else workspace.connect()


def mark_dirty(conn: sqlite3.Connection, unit_id: str) -> None:
    """显式把一个单元标为待重建索引。

    可检索/覆盖结果会因此改变，所以**同一个事务里**增加索引代次，
    正在翻页的前端会拿到 409 而不是过期的结果。
    """
    conn.execute(
        f"INSERT INTO {STATE_TABLE}(unit_id, text_sha256, state, updated_at) "
        "VALUES (?, '', ?, ?) ON CONFLICT(unit_id) DO UPDATE SET "
        "text_sha256 = '', state = excluded.state, updated_at = excluded.updated_at",
        (unit_id, STATE_DIRTY, utcnow()),
    )
    bump_revision(conn)


# ------------------------------------------------------------------ 写入


def index_unit(workspace: Workspace, unit_id: str, *,
               connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """重建一个单元的索引条目（单独事务；文字写入与索引写入刻意分离）。"""
    with _ctx(workspace, connection) as conn:
        ensure_write_transaction(conn)
        row = conn.execute(
            "SELECT id, doc_id, seq, status, original_text, corrected_text, blank_confirmed, "
            "       text_sha256 FROM units WHERE id = ?",
            (unit_id,),
        ).fetchone()
        if row is None:
            conn.execute(f"DELETE FROM {FTS_TABLE} WHERE unit_id = ?", (unit_id,))
            conn.execute(f"DELETE FROM {STATE_TABLE} WHERE unit_id = ?", (unit_id,))
            return {"unit_id": unit_id, "indexed": False, "state": "missing"}
        text = row["corrected_text"] if row["corrected_text"] is not None else row["original_text"]
        text = text or ""
        blank = bool(row["blank_confirmed"])
        digest = sha256_text(text)
        conn.execute(f"DELETE FROM {FTS_TABLE} WHERE unit_id = ?", (unit_id,))
        # 失败页、待确认页与空白页都不进索引：不能算作已覆盖或可检索。
        if row["status"] != STATE_COMPLETED_UNIT or blank or not normalize(text):
            state = STATE_BLANK if blank else STATE_EMPTY
        else:
            conn.execute(
                f"INSERT INTO {FTS_TABLE}(unit_id, doc_id, seq, tokens) VALUES (?, ?, ?, ?)",
                (unit_id, row["doc_id"], row["seq"], encode_characters(text)),
            )
            state = STATE_INDEXED
        conn.execute(
            f"INSERT INTO {STATE_TABLE}(unit_id, text_sha256, state, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(unit_id) DO UPDATE SET "
            "text_sha256 = excluded.text_sha256, state = excluded.state, "
            "updated_at = excluded.updated_at",
            (unit_id, digest, state, utcnow()),
        )
        if (row["text_sha256"] or "") != digest:
            # 只更新哈希列，不会触发文字变更触发器。
            conn.execute("UPDATE units SET text_sha256 = ? WHERE id = ?", (digest, unit_id))
        bump_revision(conn)
        return {"unit_id": unit_id, "indexed": state == STATE_INDEXED, "state": state}


def index_many(workspace: Workspace, unit_ids: Iterable[str]) -> int:
    """批量重建（一个单元一个事务，避免长事务锁住数据库）。"""
    count = 0
    for unit_id in unit_ids:
        index_unit(workspace, unit_id)
        count += 1
    return count


def pending_unit_ids(conn: sqlite3.Connection, limit: int = 5000) -> list[str]:
    """当前版本尚未建立索引（或索引对不上）的单元。"""
    rows = conn.execute(
        f"SELECT u.id AS unit_id FROM units u "
        f"LEFT JOIN {STATE_TABLE} s ON s.unit_id = u.id "
        f"WHERE s.unit_id IS NULL OR s.state = ? "
        f"   OR s.text_sha256 != u.text_sha256 "
        f"   OR (s.state = '{STATE_INDEXED}' AND u.status != '{STATE_COMPLETED_UNIT}') "
        f"ORDER BY u.doc_id, u.seq LIMIT ?",
        (STATE_DIRTY, int(limit)),
    ).fetchall()
    return [row["unit_id"] for row in rows]


def pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM units u "
        f"LEFT JOIN {STATE_TABLE} s ON s.unit_id = u.id "
        f"WHERE s.unit_id IS NULL OR s.state = ? OR s.text_sha256 != u.text_sha256 "
        f"   OR (s.state = '{STATE_INDEXED}' AND u.status != '{STATE_COMPLETED_UNIT}')",
        (STATE_DIRTY,),
    ).fetchone()
    return int(row["n"]) if row else 0


def rebuild_dirty(workspace: Workspace, *, batch: int = 5000) -> int:
    """把脏单元全部重建一遍；返回重建数量。"""
    with workspace.connect() as conn:
        unit_ids = pending_unit_ids(conn, limit=batch)
    index_many(workspace, unit_ids)
    return len(unit_ids)


_COHERENT_SQL = (
    f"JOIN {STATE_TABLE} s ON s.unit_id = u.id AND s.state = '{STATE_INDEXED}' "
    f"AND s.text_sha256 = u.text_sha256 AND u.blank_confirmed = 0 "
    f"AND u.status = '{STATE_COMPLETED_UNIT}'"
)


def searchable_pages(conn: sqlite3.Connection, doc_id: str | None = None) -> int:
    """当前版本已建立索引、可参与检索的单元数量。"""
    params: list[Any] = []
    where = ""
    if doc_id is not None:
        where = "AND u.doc_id = ?"
        params.append(doc_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM units u {_COHERENT_SQL} WHERE 1 = 1 {where}",
        params,
    ).fetchone()
    return int(row["n"]) if row else 0


def indexed_documents(conn: sqlite3.Connection, filetype: str) -> int:
    """索引已跟上当前版本的文档数量（DOCX 按份计，PDF 也可用）。"""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM documents d WHERE d.filetype = ? "
        f"AND d.unit_count > 0 AND NOT EXISTS ("
        f"    SELECT 1 FROM units u LEFT JOIN {STATE_TABLE} s ON s.unit_id = u.id "
        f"    WHERE u.doc_id = d.id AND (s.unit_id IS NULL OR s.state = '{STATE_DIRTY}' "
        f"          OR s.text_sha256 != u.text_sha256))",
        (filetype,),
    ).fetchone()
    return int(row["n"]) if row else 0


# ------------------------------------------------------------------ 覆盖


def coverage(conn: sqlite3.Connection) -> dict[str, int]:
    """覆盖对象：只统计真实可核对的状态，未知页数不生成假百分比。"""
    docs = conn.execute("""
SELECT
  COALESCE(SUM(CASE WHEN d.filetype = 'pdf' AND d.page_count > 0
                    THEN d.page_count END), 0) AS known_pdf_pages,
  COALESCE(SUM(CASE WHEN d.filetype = 'pdf'
                    THEN COALESCE(d.page_count, 0) END), 0) AS pdf_pages_seen,
  COALESCE(SUM(CASE WHEN d.filetype = 'pdf'
                    AND (d.page_count IS NULL OR d.page_count <= 0)
                    THEN 1 ELSE 0 END), 0) AS unknown_documents,
  COALESCE(SUM(CASE WHEN d.filetype = 'docx' THEN 1 ELSE 0 END), 0) AS total_docx
FROM documents d""").fetchone()

    units = conn.execute("""
SELECT
  COALESCE(SUM(CASE WHEN u.blank_confirmed = 1 THEN 1 ELSE 0 END), 0) AS blank_confirmed,
  COALESCE(SUM(CASE WHEN u.status = 'pending' AND u.blank_confirmed = 0
                    THEN 1 ELSE 0 END), 0) AS pending,
  COALESCE(SUM(CASE WHEN u.status = 'failed' THEN 1 ELSE 0 END), 0) AS failed
FROM units u JOIN documents d ON d.id = u.doc_id WHERE d.filetype = 'pdf'""").fetchone()

    indexed = conn.execute(
        f"SELECT COUNT(*) AS n FROM units u {_COHERENT_SQL} "
        f"WHERE u.doc_id IN (SELECT id FROM documents WHERE filetype = 'pdf')"
    ).fetchone()

    seen = int(docs["pdf_pages_seen"] or 0)
    accounted = (int(units["blank_confirmed"]) + int(units["pending"]) +
                 int(units["failed"]) + int(indexed["n"]))
    missing = max(0, seen - accounted)
    return {
        "known_pdf_pages": int(docs["known_pdf_pages"] or 0),
        "covered_pdf_pages": int(indexed["n"]) + int(units["blank_confirmed"]),
        "pending_pdf_pages": int(units["pending"]) + missing,
        "failed_pdf_pages": int(units["failed"]),
        "unknown_documents": int(docs["unknown_documents"]),
        "indexed_docx": indexed_documents(conn, "docx"),
        "total_docx": int(docs["total_docx"] or 0),
    }


# ------------------------------------------------------------------ 检索


def _anchor(text: str, terms: Sequence[str]) -> tuple[int, int, str] | None:
    """在当前原文中定位命中，返回 ``(start, end, 命中的词)``（码点偏移）。"""
    if not text:
        return None
    whole = find_quote(text, "".join(terms))
    if whole is not None:
        return whole[0], whole[1], "".join(terms)
    for term in terms:
        span = find_quote(text, term)
        if span is not None:
            return span[0], span[1], term
    return None


def _build_hit(conn: sqlite3.Connection, row: sqlite3.Row, terms: Sequence[str],
               position: int) -> dict[str, Any] | None:
    """把一个 FTS 候选回查成一条可信的当前命中。"""
    unit = conn.execute(
        f"SELECT u.*, s.text_sha256 AS indexed_sha, s.state AS index_state "
        f"FROM units u LEFT JOIN {STATE_TABLE} s ON s.unit_id = u.id WHERE u.id = ?",
        (row["unit_id"],),
    ).fetchone()
    if unit is None or unit["blank_confirmed"]:
        return None
    text = unit["corrected_text"] if unit["corrected_text"] is not None else unit["original_text"]
    text = text or ""
    if not normalize(text):
        return None
    digest = sha256_text(text)
    # 索引不对应当前版本（例如刚校订尚未重建）：不算当前命中。
    if unit["status"] != STATE_COMPLETED_UNIT or unit["index_state"] != STATE_INDEXED \
            or (unit["indexed_sha"] or "") != digest:
        return None
    span = _anchor(text, terms)
    if span is None:
        return None
    document = conn.execute(
        "SELECT title, filename, filetype FROM documents WHERE id = ?", (row["doc_id"],)
    ).fetchone()
    if document is None:
        return None
    start, end, term = span
    return {
        "unit_id": unit["id"],
        "doc_id": unit["doc_id"],
        "title": document["title"],
        "filename": document["filename"],
        "kind": unit["kind"],
        "file_page": unit["file_page"],
        "printed_page": unit["printed_page"],
        "title_path": unit["title_path"],
        "method": unit["method"],
        "status": unit["status"],
        "snippet": snippet(text, term),
        "start_offset": start,
        "end_offset": end,
        "text_sha256": digest,
        "version": "corrected" if unit["corrected_text"] is not None else "original",
        "filetype": document["filetype"],
        "seq": unit["seq"],
        "_rank": float(row["rank"] or 0.0),
        "_position": int(position),
        "_matched": sum(1 for item in terms if find_quote(text, item) is not None),
    }


def _sort_key(hit: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -hit["_matched"],
        hit["_rank"],
        hit["doc_id"],
        hit["file_page"] if hit["file_page"] is not None else 0,
        hit["unit_id"],
    )


def search_indexed(workspace: Workspace, query: str, *, mode: str = MODE_PHRASE,
                   doc_ids: Sequence[str] | None = None, page: int = 1,
                   page_size: int = 30,
                   expected_index_revision: int | None = None) -> dict[str, Any]:
    """索引检索：FTS5 取候选 -> 回查当前原文 -> 稳定排序 -> 分页。

    结果、总数与覆盖来自**同一个读事务**，不会拼出看似一致的不同代次。
    """
    match, terms = build_match(query, mode)
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))

    with workspace.connect() as conn:
        conn.execute("BEGIN")  # 一个读快照：代次、总数、覆盖、结果同源
        revision = current_revision(conn)
        if expected_index_revision is not None and int(expected_index_revision) != revision:
            details = {
                "current_revision": revision,
                "requested_revision": int(expected_index_revision),
                "coverage": coverage(conn),
            }
            conn.commit()
            raise IndexRevisionChanged(details)
        cover = coverage(conn)

        params: list[Any] = [match]
        doc_filter = ""
        if doc_ids:
            doc_filter = "AND unit_fts.doc_id IN (" + ", ".join("?" * len(doc_ids)) + ")"
            params.extend(doc_ids)
        params.append(CANDIDATE_LIMIT)
        rows = conn.execute(
            f"SELECT unit_fts.unit_id AS unit_id, unit_fts.doc_id AS doc_id, "
            f"       unit_fts.seq AS seq, bm25({FTS_TABLE}) AS rank "
            f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? {doc_filter} "
            f"ORDER BY rank, unit_fts.doc_id, unit_fts.seq, unit_fts.unit_id LIMIT ?",
            params,
        ).fetchall()

        hits: list[dict[str, Any]] = []
        for position, row in enumerate(rows):
            hit = _build_hit(conn, row, terms, position)
            if hit is not None:
                hits.append(hit)

        hits.sort(key=_sort_key)
        total_documents = len({hit["doc_id"] for hit in hits})
        total = len(hits)
        start = (page - 1) * page_size
        items = [
            {key: value for key, value in hit.items() if not key.startswith("_")}
            for hit in hits[start:start + page_size]
        ]
        conn.commit()

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "index_revision": revision,
        "coverage": cover,
        "total_documents": total_documents,
        "query": query,
        "mode": mode,
    }
