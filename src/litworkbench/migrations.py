"""工作区结构迁移：增量、幂等、先备份、失败不损坏已有数据。

设计要点（与网页里程碑契约一致）：

* **只增不改**：新表、新列、索引、触发器都用 ``ADD``/``CREATE ... IF NOT EXISTS``；
  已有的 ``documents``/``units``/``evidence``/``tasks`` 结构与内容一律不动，
  因此迁移前后摘录、校订历史与任务仍然可读。
* **先备份**：应用任何一条语句之前，先把数据库文件复制到 ``backups/``。
* **一个事务**：所有语句要么全部成功，要么全部回滚；SQLite 的 DDL 是事务性的。
* **幂等**：``meta.schema_version`` 记录当前版本，重复启动不会重复执行，
  旧版本工作区只升级结构，不重写任何引用内容。
* **失败可诊断**：失败时恢复备份文件并抛出 :class:`WorkspaceError`，
  调用方不会看到一个「半迁移」的工作区。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from litworkbench.errors import WorkspaceError
from litworkbench.store import Workspace, utcnow

#: 网页里程碑引入的结构版本（1 = 核心 CLI 结构）
SCHEMA_VERSION = 8


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _add_unit_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(units)")}
    if "blank_confirmed" not in existing:
        conn.execute(
            "ALTER TABLE units ADD COLUMN blank_confirmed INTEGER NOT NULL DEFAULT 0"
        )
    if "printed_page_status" not in existing:
        conn.execute(
            "ALTER TABLE units ADD COLUMN printed_page_status TEXT NOT NULL DEFAULT 'none'"
        )
    if "text_sha256" not in existing:
        # 当前有效文字的哈希：让索引是否对应当前版本可以在 SQL 里判断。
        conn.execute("ALTER TABLE units ADD COLUMN text_sha256 TEXT NOT NULL DEFAULT ''")


def _create_web_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
CREATE TABLE IF NOT EXISTS import_staging (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    byte_size INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)""")
    conn.execute("""
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    doc_id TEXT,
    staging_id TEXT,
    ocr_mode TEXT NOT NULL DEFAULT 'auto',
    total_pages INTEGER,
    processed_pages INTEGER NOT NULL DEFAULT 0,
    completed_pages INTEGER NOT NULL DEFAULT 0,
    failed_pages INTEGER NOT NULL DEFAULT 0,
    pending_pages INTEGER NOT NULL DEFAULT 0,
    searchable_pages INTEGER NOT NULL DEFAULT 0,
    duplicate INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    pause_requested INTEGER NOT NULL DEFAULT 0,
    owner TEXT,
    lease_expires_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""")
    conn.execute("""
CREATE TABLE IF NOT EXISTS job_units (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    unit_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'work',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, unit_id)
)""")
    conn.execute("""
CREATE TABLE IF NOT EXISTS index_units (
    unit_id TEXT PRIMARY KEY REFERENCES units(id) ON DELETE CASCADE,
    text_sha256 TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'dirty',
    updated_at TEXT NOT NULL DEFAULT ''
)""")
    conn.execute("""
CREATE VIRTUAL TABLE IF NOT EXISTS unit_fts USING fts5(
    unit_id UNINDEXED,
    doc_id UNINDEXED,
    seq UNINDEXED,
    tokens,
    tokenize = 'unicode61'
)""")
    now = utcnow()
    _create_index_triggers(conn)
    conn.execute("""
CREATE TRIGGER IF NOT EXISTS trg_documents_index_insert AFTER INSERT ON documents BEGIN
    UPDATE meta SET value = CAST(CAST(COALESCE(value, '0') AS INTEGER) + 1 AS TEXT)
        WHERE key = 'index_revision';
END""")
    conn.execute("""
CREATE TRIGGER IF NOT EXISTS trg_documents_index_update
AFTER UPDATE OF title, author, year, edition, volume ON documents BEGIN
    UPDATE meta SET value = CAST(CAST(COALESCE(value, '0') AS INTEGER) + 1 AS TEXT)
        WHERE key = 'index_revision';
END""")
    conn.execute("""
CREATE TRIGGER IF NOT EXISTS trg_documents_index_delete AFTER DELETE ON documents BEGIN
    UPDATE meta SET value = CAST(CAST(COALESCE(value, '0') AS INTEGER) + 1 AS TEXT)
        WHERE key = 'index_revision';
END""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_doc ON jobs(doc_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_units_state ON job_units(job_id, state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_index_units_state ON index_units(state)")
    # 已有单元在第一次建索引任务里补登记；这里只登记，保证「有没有被索引过」可查。
    conn.execute(
        "INSERT INTO index_units(unit_id, text_sha256, state, updated_at) "
        "SELECT id, '', 'dirty', ? FROM units "
        "WHERE id NOT IN (SELECT unit_id FROM index_units)",
        (now,),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('index_revision', '0') "
        "ON CONFLICT(key) DO NOTHING"
    )


def _create_index_triggers(conn: sqlite3.Connection) -> None:
    """文字变更 -> 该单元索引失效 + 索引代次 +1（同一个写事务内）。

    ``UPDATE`` 触发器带 ``WHEN``：只监听**内容真的变了**的更新。
    ``save_unit`` 会把所有列一起写回（列清单里带着 original_text），
    没有 ``WHEN`` 时，改个印刷页码或写回索引哈希都会把刚建好的索引重新标脏。
    """
    conn.execute("DROP TRIGGER IF EXISTS trg_units_index_insert")
    conn.execute("""
CREATE TRIGGER trg_units_index_insert AFTER INSERT ON units BEGIN
    INSERT INTO index_units(unit_id, text_sha256, state, updated_at)
    VALUES (NEW.id, '', 'dirty', NEW.updated_at)
    ON CONFLICT(unit_id) DO UPDATE SET
        text_sha256 = '', state = 'dirty', updated_at = excluded.updated_at;
    UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
        WHERE key = 'index_revision';
END""")
    conn.execute("DROP TRIGGER IF EXISTS trg_units_index_update")
    conn.execute("""
CREATE TRIGGER trg_units_index_update
AFTER UPDATE OF original_text, corrected_text, status, blank_confirmed ON units
WHEN NEW.original_text IS NOT OLD.original_text
     OR NEW.corrected_text IS NOT OLD.corrected_text
     OR NEW.status IS NOT OLD.status
     OR NEW.blank_confirmed IS NOT OLD.blank_confirmed
BEGIN
    INSERT INTO index_units(unit_id, text_sha256, state, updated_at)
    VALUES (NEW.id, '', 'dirty', NEW.updated_at)
    ON CONFLICT(unit_id) DO UPDATE SET
        text_sha256 = '', state = 'dirty', updated_at = excluded.updated_at;
    UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
        WHERE key = 'index_revision';
END""")
    conn.execute("""
CREATE TRIGGER IF NOT EXISTS trg_units_index_delete AFTER DELETE ON units BEGIN
    DELETE FROM unit_fts WHERE unit_id = OLD.id;
    DELETE FROM index_units WHERE unit_id = OLD.id;
    UPDATE meta SET value = CAST(CAST(COALESCE(value, '0') AS INTEGER) + 1 AS TEXT)
        WHERE key = 'index_revision';
END""")
    # 印刷页码/状态会出现在检索结果里：改变结果就要换代次，但**不把文字索引标脏**。
    conn.execute("DROP TRIGGER IF EXISTS trg_units_page_label")
    conn.execute("""
CREATE TRIGGER trg_units_page_label
AFTER UPDATE OF printed_page, printed_page_status ON units
WHEN NEW.printed_page IS NOT OLD.printed_page
     OR NEW.printed_page_status IS NOT OLD.printed_page_status
BEGIN
    UPDATE meta SET value = CAST(CAST(COALESCE(value, '0') AS INTEGER) + 1 AS TEXT)
        WHERE key = 'index_revision';
END""")


def _upgrade_index_consistency(conn: sqlite3.Connection) -> None:
    _create_index_triggers(conn)
    # v3 首次索引的竞态可能留下被错误标为 clean 的旧哈希；升级后重新核对。
    changed = conn.execute("UPDATE index_units SET state='dirty' WHERE state!='dirty'").rowcount
    if changed:
        conn.execute("UPDATE meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='index_revision'")


def _add_extraction_status(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)")}
    if "extraction_status" not in columns:
        conn.execute("ALTER TABLE units ADD COLUMN extraction_status TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE units SET extraction_status=status WHERE extraction_status=''")


def _create_collection_tables(conn: sqlite3.Connection) -> None:
    """摘录集合（专题）与下载记录：只新增表与索引，不改动任何既有行。

    * ``collections`` / ``collection_evidence``：摘录可以属于多个集合，
      集合**永不删除**（没有 DELETE 接口），成员关系可以移除，摘录本身不删；
    * ``exports``：只登记服务端生成的资料文件，文件名与相对路径由服务端决定，
      下载按主键解析，不接受任意路径参数。
    """
    conn.execute("""
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""")
    conn.execute("""
CREATE TABLE IF NOT EXISTS collection_evidence (
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (collection_id, evidence_id)
)""")
    conn.execute("""
CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    format TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    review_counts TEXT NOT NULL DEFAULT '{}',
    sha256 TEXT NOT NULL DEFAULT '',
    byte_size INTEGER NOT NULL DEFAULT 0,
    snapshot_token TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collection_evidence_evidence "
        "ON collection_evidence(evidence_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exports_filename ON exports(filename)"
    )


def _create_candidate_schema(conn: sqlite3.Connection) -> None:
    """OCR 候选与内容修订号（网页里程碑 Task A）。

    只增不改：

    * ``units.content_revision``：单调整数，由触发器在所有「内容真的变了」的
      更新后自增；印刷页码/书目等元数据更新**不**推进它；
    * ``jobs.claim_generation``：执行代次（默认 0），执行者在拿到任务后自增，
      过期代次的写入被拒绝；
    * ``ocr_candidate_jobs``：任务级识别配置快照；
    * ``ocr_candidates``：候选 + 尝试审计（每次尝试一行，失败记录不覆盖）；
    * ``ocr_candidate_snapshots``：采用前的完整旧 OCR 底稿，不可变；
    * 部分唯一索引保证**每页同一时刻最多一个活动尝试**。
    """
    unit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)")}
    if "content_revision" not in unit_columns:
        conn.execute(
            "ALTER TABLE units ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 0"
        )
    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "claim_generation" not in job_columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0"
        )

    conn.execute("DROP TRIGGER IF EXISTS trg_units_content_revision_update")
    conn.execute("""
CREATE TRIGGER trg_units_content_revision_update
AFTER UPDATE OF original_text, corrected_text, status, extraction_status, blank_confirmed, ocr_json, method, ocr_decision ON units
WHEN NEW.original_text IS NOT OLD.original_text
     OR NEW.corrected_text IS NOT OLD.corrected_text
     OR NEW.status IS NOT OLD.status
     OR NEW.extraction_status IS NOT OLD.extraction_status
     OR NEW.blank_confirmed IS NOT OLD.blank_confirmed
     OR NEW.ocr_json IS NOT OLD.ocr_json
     OR NEW.method IS NOT OLD.method
     OR NEW.ocr_decision IS NOT OLD.ocr_decision
BEGIN
    UPDATE units SET content_revision = COALESCE(OLD.content_revision, 0) + 1
        WHERE id = NEW.id;
END""")

    conn.execute("""
CREATE TABLE IF NOT EXISTS ocr_candidate_jobs (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    layout TEXT NOT NULL DEFAULT 'original',
    created_at TEXT NOT NULL
)""")
    conn.execute("""
CREATE TABLE IF NOT EXISTS ocr_candidates (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    unit_id TEXT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL,
    file_page INTEGER,
    source_sha256 TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'running',
    base_content_revision INTEGER NOT NULL DEFAULT 0,
    base_original_text_sha256 TEXT NOT NULL DEFAULT '',
    base_corrected_text_sha256 TEXT,
    profile_id TEXT NOT NULL DEFAULT '',
    layout TEXT NOT NULL DEFAULT 'original',
    result_json TEXT,
    error_kind TEXT,
    error_message TEXT,
    owner TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    decision TEXT,
    reject_reason TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_candidates_active "
        "ON ocr_candidates(unit_id) WHERE state IN ('queued', 'running')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ocr_candidates_unit_page "
        "ON ocr_candidates(unit_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ocr_candidates_job_state "
        "ON ocr_candidates(job_id, state)"
    )
    conn.execute("""
CREATE TABLE IF NOT EXISTS ocr_candidate_snapshots (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES ocr_candidates(id) ON DELETE CASCADE,
    unit_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL DEFAULT '',
    file_page INTEGER,
    old_original_text TEXT NOT NULL DEFAULT '',
    old_ocr_json TEXT,
    old_corrected_text_sha256 TEXT,
    engine TEXT,
    model_fingerprints TEXT NOT NULL DEFAULT '[]',
    profile_id TEXT NOT NULL DEFAULT '',
    layout TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)""")
def _create_ocr_gate(conn: sqlite3.Connection) -> None:
    # 工作区级 OCR 闸门：**单行**持久化单例（id 恒为 1）。同一工作区任何时刻
    # 最多一个受控识别子进程；跨进程、跨崩溃都成立（读的是数据库，不是内存）。
    conn.execute("""
CREATE TABLE IF NOT EXISTS ocr_gate (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'free',
    job_id TEXT,
    owner TEXT,
    generation INTEGER,
    token TEXT,
    lease_expires_at TEXT,
    child_pid INTEGER,
    child_create_time REAL,
    child_tree_json TEXT NOT NULL DEFAULT '[]',
    recovery_token TEXT,
    recovery_state TEXT,
    updated_at TEXT NOT NULL
)""")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(2, "web-jobs-and-index", lambda conn: (_add_unit_columns(conn),
                                                     _create_web_tables(conn))),
    # 修复：UPDATE 触发器加 WHEN，元数据更新（印刷页码、哈希回写）不再把索引标脏。
    Migration(3, "index-trigger-when", _create_index_triggers),
    Migration(4, "atomic-index-visibility-revision", _upgrade_index_consistency),
    Migration(5, "preserve-extraction-status-for-undo", _add_extraction_status),
    Migration(6, "collections-and-exports", _create_collection_tables),
    Migration(7, "ocr-candidates-and-content-revision", _create_candidate_schema),
    Migration(8, "supervised-ocr-gate", _create_ocr_gate),
)


def _current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:  # pragma: no cover - 完全没有 meta 表
        return 0
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):  # pragma: no cover - 手工改坏的值
        raise WorkspaceError("工作区的 schema_version 不是整数，请先备份并检查数据库。")


def backup_database(workspace: Workspace) -> Path | None:
    """用 SQLite 自己的备份 API 复制一份数据库。

    ``shutil.copy2`` 只复制主文件，会漏掉 WAL 里已经提交但尚未合并的内容；
    ``Connection.backup()`` 给出的是一致快照。文件名带随机后缀，
    同一秒内多次迁移也不会互相覆盖。
    """
    db_path = workspace.db_path
    if not db_path.exists():
        return None
    workspace.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().replace(":", "").replace("-", "")
    target = workspace.backup_dir / f"{db_path.name}.{stamp}.{uuid.uuid4().hex[:8]}.bak"
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


def ensure_schema(workspace: Workspace, *, backup: bool = True) -> dict[str, Any]:
    """把工作区升级到 :data:`SCHEMA_VERSION`。已经是最新版本时不做任何写入。"""
    workspace.db_path.parent.mkdir(parents=True, exist_ok=True)
    with workspace.connect() as conn:
        current = _current_version(conn)
        if current >= SCHEMA_VERSION:
            return {"from": current, "to": current, "applied": [], "backup": None}

    pending = [m for m in MIGRATIONS if m.version > current]
    if not pending:
        return {"from": current, "to": current, "applied": [], "backup": None}

    backup_path = backup_database(workspace) if backup else None
    applied: list[str] = []
    with workspace.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for migration in pending:
                migration.apply(conn)
                applied.append(migration.name)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(migration.version),),
                )
        except Exception as exc:
            raise WorkspaceError(
                f"工作区结构升级失败（已回滚，原有数据未改动）：{type(exc).__name__}: {exc}"
            ) from exc
    return {
        "from": current,
        "to": SCHEMA_VERSION,
        "applied": applied,
        "backup": str(backup_path) if backup_path else None,
    }
