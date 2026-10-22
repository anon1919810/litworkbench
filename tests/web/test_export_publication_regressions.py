"""独立验收：标准 CSV、真实 DOCX 重复生成和并发文件发布。"""

import csv
import hashlib
import io
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from litworkbench import evidence_exports as exports
from litworkbench.evidence import review_evidence, save_evidence
from litworkbench.store import sha256_text


@pytest.fixture
def material(web_workspace):
    ws = web_workspace
    ws.save_document({
        "id": "doc_material", "sha256": sha256_text("source"),
        "original_path": "private-source.pdf", "stored_path": "sources/source.pdf",
        "filename": "县志.pdf", "filetype": "pdf", "title": "合成县志",
        "author": "", "year": "", "edition": "", "volume": "",
        "status": "completed", "ocr_mode": "auto", "page_count": 1,
        "unit_count": 1, "pending_count": 0,
    })
    ws.save_unit({
        "id": "unit_material", "doc_id": "doc_material", "seq": 1,
        "kind": "page", "file_page": 1, "printed_page": "775",
        "title_path": "", "method": "ocr",
        "status": "completed", "ocr_decision": "ocr", "original_text": "文庙材料原文",
        "corrected_text": None, "notes": "", "ocr_json": None,
    })
    with ws.connect() as conn:
        conn.execute("UPDATE units SET printed_page_status='confirmed' WHERE id='unit_material'")
    item = save_evidence(ws, "unit_material", "文庙材料")
    review_evidence(ws, item["id"], "accepted")
    return ws, item["id"]


def test_csv_opens_with_standard_header_and_one_evidence_row(material):
    ws, evidence_id = material
    snapshot = exports.build_snapshot(ws, "csv", exports.normalize_scope())
    rows = list(csv.DictReader(io.StringIO(exports.render(snapshot).decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["evidence_id"] == evidence_id
    assert rows[0]["quote"] == "文庙材料"
    assert rows[0]["printed_page_status"] == "confirmed"
    assert rows[0]["snapshot_token"] == snapshot.token


def test_docx_visibly_escapes_xml_controls_without_changing_saved_evidence(material):
    import docx

    ws, evidence_id = material
    note = "pasted\x01note\x0b\ufffe"
    with ws.connect() as conn:
        conn.execute("UPDATE evidence SET note=? WHERE id=?", (note, evidence_id))
        conn.execute("UPDATE documents SET title=? WHERE id='doc_material'", ("县\x02志",))
    snapshot = exports.build_snapshot(ws, "docx", exports.normalize_scope())
    output = exports.render(snapshot)
    paragraphs = "\n".join(p.text for p in docx.Document(io.BytesIO(output)).paragraphs)
    assert r"pasted\u0001note\u000b\ufffe" in paragraphs
    assert r"县\u0002志" in paragraphs
    assert "文庙材料" in paragraphs
    with ws.connect() as conn:
        assert conn.execute("SELECT note FROM evidence WHERE id=?", (evidence_id,)).fetchone()[0] == note


def test_repeated_real_docx_render_reuses_first_immutable_file(material):
    ws, _ = material
    snapshot = exports.build_snapshot(ws, "docx", exports.normalize_scope())
    first_bytes = exports.render(snapshot)
    first = exports.store_export(ws, snapshot, first_bytes)
    time.sleep(2.1)  # ZIP 日期只精确到两秒：跨过真正的封装时间差。
    later_bytes = exports.render(snapshot)
    later = exports.store_export(ws, snapshot, later_bytes)
    assert later["id"] == first["id"]
    assert later["sha256"] == first["sha256"]
    assert exports.export_path(ws, later).read_bytes() == first_bytes


def _retime_docx(data):
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(result, "w") as target:
        for info in source.infolist():
            info.date_time = (2000, 1, 1, 0, 0, 0)
            target.writestr(info, source.read(info.filename))
    return result.getvalue()


def test_concurrent_first_exports_keep_registered_bytes(material, monkeypatch):
    ws, _ = material
    snapshot = exports.build_snapshot(ws, "docx", exports.normalize_scope())
    first_data = exports.render(snapshot)
    later_data = _retime_docx(first_data)
    assert first_data != later_data
    target = exports.exports_dir(ws) / snapshot.filename
    checked = threading.Event()
    release = threading.Event()
    first_done = threading.Event()
    original_exists = Path.exists
    records = {}
    errors = []

    def exists(path):
        result = original_exists(path)
        if path == target and threading.current_thread().name == "publication-later" and not result:
            checked.set()
            assert release.wait(5), "test release timeout"
        return result

    monkeypatch.setattr(Path, "exists", exists)

    def publish(name, data):
        try:
            records[name] = exports.store_export(ws, snapshot, data)
        except Exception as exc:
            errors.append(exc)
        finally:
            if name == "first":
                first_done.set()

    later = threading.Thread(target=publish, args=("later", later_data), name="publication-later")
    first = threading.Thread(target=publish, args=("first", first_data), name="publication-first")
    later.start()
    try:
        assert checked.wait(3)
        first.start()
        # 旧实现允许 first 此时写入并登记；持写锁的实现会等待 later 完成。
        first_done.wait(2)
    finally:
        release.set()
        later.join(8)
        if first.ident is not None:
            first.join(8)
    assert not later.is_alive() and not first.is_alive()
    assert not errors
    assert records["first"]["id"] == records["later"]["id"]
    stored_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert records["first"]["sha256"] == records["later"]["sha256"] == stored_hash


def test_commit_lock_failure_leaves_no_file_and_allows_docx_retry(material, monkeypatch):
    ws, _ = material
    snapshot = exports.build_snapshot(ws, "docx", exports.normalize_scope())
    data = exports.render(snapshot)
    connect = ws.connect

    @contextmanager
    def short_wait():
        with connect() as conn:
            conn.execute("PRAGMA busy_timeout=25")
            yield conn

    monkeypatch.setattr(ws, "connect", short_wait)
    with connect() as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM exports").fetchall()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            exports.store_export(ws, snapshot, data)
        reader.rollback()
    target = exports.exports_dir(ws) / snapshot.filename
    assert not target.exists()
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM exports").fetchone()[0] == 0
    retried = exports.store_export(ws, snapshot, _retime_docx(data))
    assert hashlib.sha256(exports.export_path(ws, retried).read_bytes()).hexdigest() == retried["sha256"]
