"""Consistent SQLite + referenced immutable files, restored only into new directories."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

from litworkbench import config, migrations
from litworkbench.errors import WorkspaceError
from litworkbench.store import Workspace, sha256_file, utcnow

AUDIT_TABLES = ('documents', 'units', 'evidence', 'revisions', 'tasks', 'task_actions',
    'jobs', 'job_units', 'ocr_candidates', 'ocr_candidate_snapshots', 'proofread_candidates',
    'pages', 'page_revisions', 'collections', 'collection_evidence')
FILE_TABLES = ('documents', 'import_staging', 'note_assets', 'exports', 'page_exports')
AUTH_TABLES = ('cloud_sessions', 'oauth_flows', 'agent_tokens')


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _target(path, source: Path) -> Path:
    target = Path(path).expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if target.exists() or _inside(target, source) or _inside(target, repo):
        raise WorkspaceError('目标必须是源码和源资料库之外的全新目录。')
    return target


def _relative(value: str) -> str:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or PureWindowsPath(value).drive or '\\' in value or '..' in p.parts or p.as_posix() != value:
        raise WorkspaceError('备份包含不安全的相对路径。')
    return value


def _file(root: Path, relative: str) -> Path:
    relative = _relative(relative)
    path = root / relative
    if not _inside(path.resolve(), root.resolve()) or path.is_symlink() or any(p.is_symlink() for p in path.parents if _inside(p, root)):
        raise WorkspaceError('备份文件不得通过链接离开资料目录。')
    if not path.is_file():
        raise WorkspaceError('备份依赖文件缺失：' + relative)
    return path


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _audit(conn):
    tables = _tables(conn); result = {}
    for table in AUDIT_TABLES:
        if table not in tables: continue
        columns = conn.execute(f'PRAGMA table_info({table})').fetchall()
        keys = [row[1] for row in sorted(columns, key=lambda r: r[5]) if row[5]]
        if not keys: keys = ['rowid']
        rows = conn.execute(f'SELECT {",".join(keys)} FROM {table}').fetchall()
        result[table] = {'count': len(rows), 'ids': sorted([list(row) for row in rows], key=lambda row: json.dumps(row))}
    return result


def _strip_auth(conn):
    tables = _tables(conn)
    for table in AUTH_TABLES:
        if table in tables: conn.execute(f'DELETE FROM {table}')


def backup_workspace(workspace: Workspace, destination) -> dict:
    if not workspace.exists(): raise WorkspaceError('资料库尚未初始化。')
    target = _target(destination, workspace.root)
    db_relative = _relative(workspace.db_path.relative_to(workspace.root).as_posix())
    target.mkdir(parents=True)
    manifest = {'format_version': 1, 'created_at': utcnow(), 'database': db_relative,
                'schema_version': None, 'files': [], 'tables': {}}
    try:
        # A separate reader performs SQLite backup while this connection excludes writers.
        with workspace.connect() as lock:
            lock.execute('BEGIN IMMEDIATE')
            copy_path = target / db_relative; copy_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(workspace.db_path)) as source, sqlite3.connect(str(copy_path)) as copy:
                source.backup(copy)
                manifest['schema_version'] = int(copy.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
                manifest['tables'] = _audit(copy)
                tables = _tables(copy); paths = set()
                for table in FILE_TABLES:
                    if table not in tables: continue
                    where = " WHERE status != 'consumed'" if table == 'import_staging' else ''
                    paths.update(row[0] for row in copy.execute(f'SELECT stored_path FROM {table}{where}') if row[0])
                for relative in sorted(paths):
                    relative = _relative(relative); original = _file(workspace.root, relative)
                    dest = target / relative
                    if dest.exists(): raise WorkspaceError('备份路径冲突。')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    before = sha256_file(original); shutil.copyfile(original, dest)
                    if before != sha256_file(dest) or before != sha256_file(original):
                        raise WorkspaceError('备份期间依赖文件发生变化，请停写后重试。')
                _strip_auth(copy); copy.commit()
                copy.execute('VACUUM')  # Do not retain deleted OAuth/session material in free pages.
            safe = config.redact_config(workspace.config)
            # Only the documented application sections enter a portable backup.
            safe = {key: safe[key] for key in config.DEFAULT_CONFIG}
            if 'staging_dir' in workspace.config.get('workspace', {}):
                safe['workspace']['staging_dir'] = workspace.config['workspace']['staging_dir']
            (target / config.CONFIG_FILENAME).write_text(config.render_config_toml(safe), encoding='utf-8')
            for path in sorted(p for p in target.rglob('*') if p.is_file()):
                manifest['files'].append({'path': path.relative_to(target).as_posix(), 'sha256': sha256_file(path), 'bytes': path.stat().st_size})
        (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        return manifest
    except Exception:
        # Keep partial output for diagnosis; it lacks manifest.json and cannot be restored.
        raise


def restore_workspace(snapshot, destination) -> dict:
    source = Path(snapshot).resolve(); target = _target(destination, source)
    try:
        manifest = json.loads((source / 'manifest.json').read_text('utf-8'))
        if manifest['format_version'] != 1 or manifest['schema_version'] > migrations.SCHEMA_VERSION:
            raise WorkspaceError('备份版本需要匹配或更新的程序镜像。')
        entries = manifest['files']; paths = [entry['path'] for entry in entries]
        if len(paths) != len(set(paths)) or manifest['database'] not in paths or config.CONFIG_FILENAME not in paths:
            raise WorkspaceError('备份清单不完整或有重复路径。')
        for entry in entries:
            file = _file(source, entry['path'])
            if file.stat().st_size != entry['bytes'] or sha256_file(file) != entry['sha256']:
                raise WorkspaceError('备份文件校验失败：' + entry['path'])
        with sqlite3.connect(str(_file(source, manifest['database']))) as conn:
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchall():
                raise WorkspaceError('数据库完整性校验失败。')
            if _audit(conn) != manifest['tables']:
                raise WorkspaceError('数据库数量或 ID 与清单不一致。')
        # Reject malicious config paths before any target directory is created.
        source_ws = Workspace(source)
        for path in (source_ws.db_path, source_ws.sources_dir, source_ws.artifacts_dir, source_ws.staging_dir):
            if not _inside(path.resolve(), source): raise WorkspaceError('配置路径离开备份目录。')
        if source_ws.db_path.relative_to(source).as_posix() != manifest['database']:
            raise WorkspaceError('备份数据库路径与配置不一致。')
    except WorkspaceError:
        raise
    except Exception:
        raise WorkspaceError('备份清单无效或依赖文件不完整。') from None
    target.mkdir(parents=True)
    for entry in entries:
        dest = target / entry['path']; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_file(source, entry['path']), dest)
    workspace = Workspace(target)
    migrations.ensure_schema(workspace, backup=False)
    report = {'restored_at': utcnow(), 'paused_jobs': [], 'paused_tasks': [], 'counts_and_ids_verified': True,
              'credentials_invalidated': True, 'requires_manual_preflight': True}
    with workspace.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _strip_auth(conn)
        for row in conn.execute("SELECT id,state FROM jobs WHERE state NOT IN ('completed','failed','partial')").fetchall():
            report['paused_jobs'].append(dict(row))
            conn.execute("UPDATE jobs SET state='paused',stage='迁移后暂停，等待预检',pause_requested=1,owner=NULL,lease_expires_at=NULL,claim_generation=claim_generation+1 WHERE id=?", (row['id'],))
        for row in conn.execute("SELECT id,status FROM tasks WHERE status NOT IN ('completed','failed')").fetchall():
            report['paused_tasks'].append(dict(row))
            conn.execute("UPDATE tasks SET status='paused' WHERE id=?", (row['id'],))
        conn.execute('DELETE FROM ocr_gate')
        conn.execute("UPDATE ocr_candidates SET state='failed',owner=NULL,error_kind='migration_interrupted',error_message='迁移前尝试已中断，请预检后重试。' WHERE state IN ('queued','running')")
    (target / 'restore-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
