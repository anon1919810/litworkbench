"""工作区迁移：增量、幂等、先备份、失败不损坏已有证据。"""

from __future__ import annotations

import sqlite3

import pytest

from litworkbench import migrations
from litworkbench.errors import WorkspaceError
from litworkbench.store import SCHEMA_VERSION, Workspace, utcnow


@pytest.fixture
def legacy_workspace(tmp_path):
    """一个只有核心 v1 结构（没有网页里程碑附加表/列）的工作区。"""
    root = tmp_path / "旧 工作区"
    ws = Workspace(root)
    ws.initialize()
    with ws.connect() as conn:
        conn.executescript("""
        DROP TRIGGER IF EXISTS trg_units_index_insert;
        DROP TRIGGER IF EXISTS trg_units_index_update;
        DROP TRIGGER IF EXISTS trg_units_index_delete;
        DROP TRIGGER IF EXISTS trg_units_page_label;
        DROP TRIGGER IF EXISTS trg_units_content_revision_update;
        DROP TRIGGER IF EXISTS trg_documents_index_insert;
        DROP TRIGGER IF EXISTS trg_documents_index_update;
        DROP TRIGGER IF EXISTS trg_documents_index_delete;
        DROP TABLE IF EXISTS ocr_candidate_snapshots;
        DROP TABLE IF EXISTS ocr_candidates;
        DROP TABLE IF EXISTS ocr_candidate_jobs;
        DROP TABLE IF EXISTS job_units;
        DROP TABLE IF EXISTS jobs;
        DROP TABLE IF EXISTS import_staging;
        DROP TABLE IF EXISTS index_units;
        DROP TABLE IF EXISTS unit_fts;
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)")}
        for extra in ("text_sha256", "blank_confirmed", "printed_page_status"):
            if extra in columns:  # SQLite >= 3.35
                conn.execute(f"ALTER TABLE units DROP COLUMN {extra}")
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    return ws


def test_fresh_workspace_is_already_migrated(web_workspace):
    with web_workspace.connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert int(row["value"]) == migrations.SCHEMA_VERSION
    with web_workspace.connect() as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    assert {"jobs", "job_units", "import_staging", "index_units", "unit_fts"} <= tables


def test_upgrade_keeps_existing_evidence(legacy_workspace):
    ws = legacy_workspace
    with ws.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(units)")}
    assert "text_sha256" not in columns

    with ws.connect() as conn:
        conn.execute(
            "INSERT INTO documents (id, sha256, original_path, stored_path, filename, "
            "filetype, status, ocr_mode, imported_at, updated_at) "
            "VALUES ('doc_old', :sha, 'x', 'sources/x.pdf', '旧文献.pdf', 'pdf', "
            "'completed', 'off', :now, :now)",
            {"sha": "a" * 64, "now": utcnow()},
        )
        conn.execute(
            "INSERT INTO units (id, doc_id, seq, kind, file_page, method, status, "
            "ocr_decision, original_text, created_at, updated_at) "
            "VALUES ('doc_old_page_0001', 'doc_old', 1, 'page', 1, 'text', 'completed', "
            "'not_needed', '旧工作区里的白塔记载', :now, :now)", {"now": utcnow()},
        )
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, quote, start_offset, end_offset, "
            "version, version_snapshot, created_at, updated_at) "
            "VALUES ('ev_old', 'doc_old_page_0001', 'doc_old', '白塔', 0, 2, 'original', "
            "'{}', :now, :now)", {"now": utcnow()},
        )

    result = migrations.ensure_schema(ws)
    assert result["from"] == 1 and result["to"] == migrations.SCHEMA_VERSION
    assert "web-jobs-and-index" in result["applied"]
    assert result["backup"] and (ws.root / "backups").exists()

    with ws.connect() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id = 'ev_old'").fetchone()
        unit = conn.execute("SELECT * FROM units WHERE id = 'doc_old_page_0001'").fetchone()
    assert row is not None and row["quote"] == "白塔"      # 旧摘录原样保留
    assert unit is not None and unit["original_text"] == "旧工作区里的白塔记载"
    # 新结构可用
    ws.update_document("doc_old", title="旧文献（已改名）")
    assert ws.get_document("doc_old")["title"] == "旧文献（已改名）"


def test_migration_is_idempotent(legacy_workspace):
    migrations.ensure_schema(legacy_workspace)
    second = migrations.ensure_schema(legacy_workspace)
    assert second["applied"] == [] and second["from"] == second["to"]
    assert migrations.SCHEMA_VERSION >= 3


def test_upgrade_registers_existing_units_as_dirty(legacy_workspace):
    """旧单元在第一次索引任务里补建，迁移本身不重写引用内容。"""
    with legacy_workspace.connect() as conn:
        conn.execute(
            "INSERT INTO documents (id, sha256, original_path, stored_path, filename, "
            "filetype, status, ocr_mode, imported_at, updated_at) "
            "VALUES ('doc_d', :sha, 'x', 'sources/d.pdf', '待索引.pdf', 'pdf', "
            "'completed', 'off', :now, :now)", {"sha": "b" * 64, "now": utcnow()},
        )
        conn.execute(
            "INSERT INTO units (id, doc_id, seq, kind, file_page, method, status, "
            "ocr_decision, original_text, created_at, updated_at) "
            "VALUES ('doc_d_page_0001', 'doc_d', 1, 'page', 1, 'text', 'completed', "
            "'not_needed', '需要重建索引的文字', :now, :now)", {"now": utcnow()},
        )
    migrations.ensure_schema(legacy_workspace)
    from litworkbench import search_index

    with legacy_workspace.connect() as conn:
        assert search_index.pending_count(conn) == 1
        assert search_index.searchable_pages(conn) == 0
    assert search_index.rebuild_dirty(legacy_workspace) == 1
    with legacy_workspace.connect() as conn:
        assert search_index.pending_count(conn) == 0
        assert search_index.searchable_pages(conn) == 1


def test_failed_migration_rolls_back_and_keeps_evidence(legacy_workspace, monkeypatch):
    with legacy_workspace.connect() as conn:
        conn.execute(
            "INSERT INTO documents (id, sha256, original_path, stored_path, filename, "
            "filetype, status, ocr_mode, imported_at, updated_at) "
            "VALUES ('doc_keep', :sha, 'x', 'sources/k.pdf', '保留.pdf', 'pdf', "
            "'completed', 'off', :now, :now)", {"sha": "c" * 64, "now": utcnow()},
        )
        conn.execute(
            "INSERT INTO units (id, doc_id, seq, kind, file_page, method, status, "
            "ocr_decision, original_text, created_at, updated_at) "
            "VALUES ('doc_keep_page_0001', 'doc_keep', 1, 'page', 1, 'text', 'completed', "
            "'not_needed', '不能丢的原文', :now, :now)", {"now": utcnow()},
        )
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, quote, start_offset, end_offset, "
            "version, version_snapshot, created_at, updated_at) "
            "VALUES ('ev_keep', 'doc_keep_page_0001', 'doc_keep', '不能丢的摘录', 0, 5, "
            "'original', '{}', :now, :now)", {"now": utcnow()},
        )
    # 触发器是 NOT NULL 的，插入 null 会在事务中途失败
    original = migrations._create_web_tables

    def exploding(conn):
        original(conn)
        conn.execute("INSERT INTO index_units(unit_id, text_sha256, state, updated_at) "
                     "VALUES (NULL, NULL, NULL, NULL)")

    monkeypatch.setattr(migrations, "_create_web_tables", exploding)
    with pytest.raises(WorkspaceError) as excinfo:
        migrations.ensure_schema(legacy_workspace)
    assert "回滚" in str(excinfo.value)

    monkeypatch.undo()
    with legacy_workspace.connect() as conn:
        row = conn.execute("SELECT quote FROM evidence WHERE id = 'ev_keep'").fetchone()
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row is not None and row["quote"] == "不能丢的摘录"
    assert int(version["value"]) == 1        # 没有半迁移


def test_backup_uses_consistent_snapshot_and_unique_names(web_workspace):
    with web_workspace.connect() as conn:
        conn.execute(
            "INSERT INTO documents (id, sha256, original_path, stored_path, filename, "
            "filetype, status, ocr_mode, imported_at, updated_at) "
            "VALUES ('doc_wal', :sha, 'x', 'sources/w.pdf', '备份.pdf', 'pdf', 'completed', "
            "'off', :now, :now)", {"sha": "d" * 64, "now": utcnow()},
        )
    first = migrations.backup_database(web_workspace)
    second = migrations.backup_database(web_workspace)
    assert first != second and first.exists() and second.exists()
    for backup in (first, second):
        conn = sqlite3.connect(str(backup))
        try:
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        finally:
            conn.close()


def test_core_cli_open_upgrades_workspace(web_workspace):
    with web_workspace.connect() as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    reopened = Workspace.open(web_workspace.root)
    with reopened.connect() as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert int(version["value"]) == migrations.SCHEMA_VERSION


def test_schema_version_constant_is_consistent():
    assert migrations.SCHEMA_VERSION > SCHEMA_VERSION
    assert [m.version for m in migrations.MIGRATIONS] == sorted(
        m.version for m in migrations.MIGRATIONS)