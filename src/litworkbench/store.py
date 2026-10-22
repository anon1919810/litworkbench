"""SQLite 存储与工作区对象。

设计要点：

* 原件按内容哈希保存为不可变副本（``sources/xx/<sha256>.<ext>``）；
* PDF 一页 / DOCX 一个段落或表格是一个「原文单元」，带稳定 ID；
* 原始识别文字不可变，人工校订另存，旧摘录自动标记为待复核；
* 任务、消息、动作逐步落盘，支持中断后恢复。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from litworkbench import config as config_mod
from litworkbench.errors import NotFoundError, WorkspaceError
from litworkbench.text import normalize

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    original_path TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    filetype TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    edition TEXT NOT NULL DEFAULT '',
    volume TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    ocr_mode TEXT NOT NULL,
    page_count INTEGER,
    unit_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    file_page INTEGER,
    printed_page TEXT NOT NULL DEFAULT '',
    title_path TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    ocr_decision TEXT NOT NULL,
    original_text TEXT NOT NULL DEFAULT '',
    corrected_text TEXT,
    notes TEXT NOT NULL DEFAULT '',
    ocr_json TEXT,
    search_text TEXT NOT NULL DEFAULT '',
    search_text_corrected TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    old_text TEXT NOT NULL DEFAULT '',
    new_text TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    task_id TEXT,
    quote TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    version TEXT NOT NULL,
    version_snapshot TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'pending',
    review_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    profile_snapshot TEXT NOT NULL,
    config_snapshot TEXT NOT NULL,
    status TEXT NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL,
    report TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    step INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_units_doc ON units(doc_id, seq);
CREATE INDEX IF NOT EXISTS idx_evidence_unit ON evidence(unit_id);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON evidence(task_id);
CREATE INDEX IF NOT EXISTS idx_task_actions_task ON task_actions(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_task_messages_task ON task_messages(task_id, seq);
"""

_UNIT_PRIVATE_COLUMNS = {"search_text", "search_text_corrected"}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_to_dict(row: sqlite3.Row | None, private: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if not private:
        for key in _UNIT_PRIVATE_COLUMNS:
            data.pop(key, None)
    if data.get("ocr_json"):
        try:
            data["ocr_json"] = json.loads(data["ocr_json"])
        except (json.JSONDecodeError, TypeError):  # pragma: no cover - 容错
            data["ocr_json"] = None
    return data


def ensure_write_transaction(conn: sqlite3.Connection) -> sqlite3.Connection:
    """确保连接处于**写事务**中；已经在事务里则原样返回。

    SQLite 的普通 SELECT 不会隐式开事务，所以「读当前值 -> 校验哈希 -> 写入」
    如果不在显式写事务里就是 TOCTOU：另一个连接可能在中间改掉文字。
    ``BEGIN IMMEDIATE`` 在读之前拿写锁，堵住这个窗口。
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    return conn


def upsert(conn: sqlite3.Connection, table: str, fields: list[str],
           values: list[Any], key: str = "id") -> None:
    """``INSERT ... ON CONFLICT DO UPDATE``。

    绝不使用 ``INSERT OR REPLACE``：它是「先删后插」，在 ``ON DELETE CASCADE``
    下会连带删除该文献/单元下的摘录、校订历史与任务动作。
    """
    placeholders = ", ".join("?" * len(fields))
    assignments = ", ".join(f"{field} = excluded.{field}" for field in fields if field != key)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders}) "
        f"ON CONFLICT({key}) DO UPDATE SET {assignments}",
        values,
    )


def _unit_view(data: dict[str, Any]) -> dict[str, Any]:
    """给单元行补上易用字段：``text`` / ``version`` / ``ocr``。"""
    if data is None:
        return data  # type: ignore[return-value]
    corrected = data.get("corrected_text")
    data["version"] = "corrected" if corrected is not None else "original"
    data["text"] = corrected if corrected is not None else (data.get("original_text") or "")
    data["ocr"] = data.get("ocr_json")
    return data


class Workspace:
    """一个文献库：配置 + SQLite + 原件副本 + 产出目录。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    # ---------------------------------------------------------------- 路径

    @property
    def config_path(self) -> Path:
        return self.root / config_mod.CONFIG_FILENAME

    @property
    def db_path(self) -> Path:
        return self.root / str(self.config.get("workspace", {}).get("db_file", "litworkbench.sqlite3"))

    @property
    def sources_dir(self) -> Path:
        return self.root / str(self.config.get("workspace", {}).get("sources_dir", "sources"))

    @property
    def artifacts_dir(self) -> Path:
        return self.root / str(self.config.get("workspace", {}).get("artifacts_dir", "artifacts"))

    @property
    def renders_dir(self) -> Path:
        return self.artifacts_dir / "pages"

    @property
    def staging_dir(self) -> Path:
        """网页上传的流式暂存目录；文件名是随机 ID，用户文件名只作为数据保存。"""
        return self.root / str(self.config.get("workspace", {}).get("staging_dir", "staging"))

    @property
    def backup_dir(self) -> Path:
        """迁移前的数据库备份目录。"""
        return self.root / "backups"

    # ------------------------------------------------------------ 生命周期

    def exists(self) -> bool:
        return self.config_path.exists() and self.db_path.exists()

    def initialize(self, force: bool = False) -> dict[str, Any]:
        """创建工作区；已存在且未指定 force 时报错。"""
        if self.exists() and not force:
            raise WorkspaceError(
                f"工作区已存在：{self.root}（如需重建请使用 --force，或先自行备份）"
            )
        for directory in (self.root, self.sources_dir, self.artifacts_dir, self.renders_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if force or not self.config_path.exists():
            self.config_path.write_text(
                config_mod.render_config_toml(self.config), encoding="utf-8"
            )
        self._config = None
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("schema_version", str(SCHEMA_VERSION)),
            )
        for directory in (self.staging_dir, self.backup_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # 核心库之上还有网页里程碑的增量结构（任务队列、全文索引、暂存）。
        from litworkbench import migrations

        migrations.ensure_schema(self)
        return {
            "created": True,
            "workspace": str(self.root),
            "config": str(self.config_path),
            "database": str(self.db_path),
            "sources": str(self.sources_dir),
        }

    @classmethod
    def open(cls, root: Path | str) -> "Workspace":
        ws = cls(root)
        if not ws.exists():
            raise WorkspaceError(f"工作区不存在或未初始化：{ws.root}，请先运行 lit init")
        from litworkbench import migrations

        migrations.ensure_schema(ws)
        return ws

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -------------------------------------------------------------- 配置

    @property
    def config(self) -> dict[str, Any]:
        cached = getattr(self, "_config", None)
        if cached is None:
            cached = config_mod.load_config(self.config_path)
            self._config = cached
        return cached

    def save_config(self, config: dict[str, Any]) -> None:
        self._config = config
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            config_mod.render_config_toml(config), encoding="utf-8"
        )

    # ------------------------------------------------------------ 文献

    def save_document(self, document: dict[str, Any], *, connection=None) -> dict[str, Any]:
        now = utcnow()
        document.setdefault("imported_at", now)
        document["updated_at"] = now
        fields = [
            "id", "sha256", "original_path", "stored_path", "filename", "filetype",
            "title", "author", "year", "edition", "volume", "status", "ocr_mode",
            "page_count", "unit_count", "pending_count", "imported_at", "updated_at",
        ]
        values = [document.get(field) for field in fields]
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            upsert(conn, "documents", fields, values)
        return self.get_document(document["id"], connection=connection)

    def get_document(self, doc_id: str, *,
                      connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        """读取文献。``connection`` 让导出在一个事务里拿到一致快照。"""
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"找不到文献：{doc_id}")
        return dict(row)

    def document_by_hash(self, digest: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (digest,)).fetchone()
        return dict(row) if row else None

    def find_document(self, needle: str) -> dict[str, Any]:
        """按 id 或文件名前缀/包含查找文献。"""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (needle,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM documents WHERE filename = ? ORDER BY imported_at DESC LIMIT 1",
                    (needle,),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM documents WHERE filename LIKE ? ORDER BY imported_at DESC LIMIT 1",
                    (f"%{needle}%",),
                ).fetchone()
        if row is None:
            raise NotFoundError(f"找不到文献：{needle}")
        return dict(row)

    def list_documents(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY imported_at DESC, id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_document(self, doc_id: str, *, connection=None, **fields: Any) -> dict[str, Any]:
        allowed = {
            "title", "author", "year", "edition", "volume", "status", "ocr_mode",
            "page_count", "unit_count", "pending_count", "original_path", "filename",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise WorkspaceError(f"不可修改的字段：{'、'.join(sorted(unknown))}")
        if not fields:
            return self.get_document(doc_id, connection=connection)
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            conn.execute(
                f"UPDATE documents SET {assignments} WHERE id = ?",
                [*fields.values(), doc_id],
            )
        return self.get_document(doc_id, connection=connection)

    def path_of(self, document: dict[str, Any] | str) -> Path:
        if isinstance(document, str):
            document = self.get_document(document)
        return self.root / document["stored_path"]

    # ------------------------------------------------------------ 原文单元

    def save_unit(self, unit: dict[str, Any], *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        now = utcnow()
        unit.setdefault("created_at", now)
        unit["updated_at"] = now
        unit.setdefault("extraction_status", unit.get("status") or "pending")
        unit["search_text"] = normalize(unit.get("original_text") or "")
        unit["search_text_corrected"] = normalize(unit.get("corrected_text") or "")
        # 当前有效文字的哈希：供网页端 expected_hash 并发校验与索引一致性判断使用。
        unit["text_sha256"] = sha256_text(
            unit["corrected_text"] if unit.get("corrected_text") is not None
            else (unit.get("original_text") or "")
        )
        if unit.get("ocr_json") is not None and not isinstance(unit["ocr_json"], str):
            unit["ocr_json"] = json.dumps(unit["ocr_json"], ensure_ascii=False)
        fields = [
            "id", "doc_id", "seq", "kind", "file_page", "printed_page", "title_path",
            "method", "status", "extraction_status", "ocr_decision", "original_text", "corrected_text",
            "notes", "ocr_json", "search_text", "search_text_corrected", "text_sha256",
            "created_at", "updated_at",
        ]
        values = [unit.get(field) for field in fields]
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            upsert(conn, "units", fields, values)
            effective = unit.get("corrected_text")
            if effective is None:
                effective = unit.get("original_text") or ""
            if effective.strip():
                conn.execute("UPDATE units SET blank_confirmed = 0 WHERE id = ? AND blank_confirmed != 0",
                             (unit["id"],))
            conn.execute(
                "UPDATE documents SET unit_count = "
                "(SELECT COUNT(*) FROM units WHERE doc_id = ?), updated_at = ? WHERE id = ?",
                (unit["doc_id"], now, unit["doc_id"]),
            )
            conn.execute(
                "UPDATE documents SET pending_count = "
                "(SELECT COUNT(*) FROM units WHERE doc_id = ? AND status != 'completed'), updated_at = ? "
                "WHERE id = ?",
                (unit["doc_id"], now, unit["doc_id"]),
            )
            row = conn.execute("SELECT * FROM units WHERE id = ?", (unit["id"],)).fetchone()
            return _unit_view(_row_to_dict(row))

    def get_unit(self, unit_id: str, *,
                 connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            row = conn.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()
        data = _row_to_dict(row)
        if data is None:
            raise NotFoundError(f"找不到原文单元：{unit_id}")
        return _unit_view(data)  # type: ignore[return-value]

    def units_for_doc(self, doc_id: str, *, connection=None) -> list[dict[str, Any]]:
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM units WHERE doc_id = ? ORDER BY seq", (doc_id,)
            ).fetchall()
        return [_unit_view(_row_to_dict(row)) for row in rows]  # type: ignore[misc]

    def list_units(self, doc_id: str | None = None, status: str | None = None,
                   limit: int = 200) -> list[dict[str, Any]]:
        clauses, params = [], []
        if doc_id:
            clauses.append("doc_id = ?")
            params.append(doc_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM units {where} ORDER BY doc_id, seq LIMIT ?", params
            ).fetchall()
        return [_unit_view(_row_to_dict(row)) for row in rows]  # type: ignore[misc]

    def mark_unit_failed(self, unit_id: str, reason: str, *, connection=None) -> dict[str, Any]:
        unit = self.get_unit(unit_id, connection=connection)
        notes = unit["notes"]
        if reason and reason not in notes:
            notes = f"{notes}\n{reason}".strip()
        unit["notes"] = notes
        unit["status"] = "failed"
        unit["extraction_status"] = "failed"
        return self.save_unit(unit, connection=connection)

    def add_revision(self, unit_id: str, kind: str, old_text: str, new_text: str,
                     reason: str, *, connection: sqlite3.Connection | None = None) -> None:
        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            conn.execute(
                "INSERT INTO unit_revisions (unit_id, at, kind, old_text, new_text, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (unit_id, utcnow(), kind, old_text, new_text, reason),
            )

    def revisions_for_unit(self, unit_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM unit_revisions WHERE unit_id = ? ORDER BY id", (unit_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_printed_page(self, unit_id: str, printed_page: str) -> dict[str, Any]:
        """只写印刷页码这一个字段。

        绝不整行回写：并发的采用/校订可能已经改了正文，用陈旧快照整行写回会把
        新正文覆盖掉。单字段 ``UPDATE`` 只碰 ``printed_page``，不推进内容修订号。
        """
        with self.connect() as conn:
            changed = conn.execute(
                "UPDATE units SET printed_page = ? WHERE id = ?",
                (printed_page, unit_id),
            ).rowcount
        if not changed:
            raise NotFoundError(f"找不到原文单元：{unit_id}")
        return self.get_unit(unit_id)

    # -------------------------------------------------------------- 统计

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            units = conn.execute("SELECT COUNT(*) AS n FROM units").fetchone()["n"]
            by_status = {
                row["status"]: row["n"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM units GROUP BY status"
                ).fetchall()
            }
            evidence = conn.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"]
            tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        return {
            "documents": docs,
            "units": units,
            "units_by_status": by_status,
            "evidence": evidence,
            "tasks": tasks,
        }


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
