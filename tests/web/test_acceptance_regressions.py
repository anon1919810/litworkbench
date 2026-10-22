"""Independent reproductions for the final data-integrity review."""
from contextlib import contextmanager
import threading

import pytest
from fastapi.testclient import TestClient

from litworkbench import migrations, ocr, search_index
from litworkbench.evidence import clear_correction, correct_unit, get_evidence, review_evidence, save_evidence
from litworkbench.jobs import JobStore
from litworkbench.store import sha256_text
from litworkbench.web.app import create_app


def seed(ws, name="one", text="旧文庙", status="completed"):
    ws.save_document(dict(id=name, sha256=sha256_text(name), original_path="x",
        stored_path=f"sources/{name}.pdf", filename=f"{name}.pdf", filetype="pdf",
        title=name, author="", year="", edition="", volume="", status="completed",
        ocr_mode="off", page_count=1, unit_count=0, pending_count=0))
    unit_id = name + "_p1"
    ws.save_unit(dict(id=unit_id, doc_id=name, seq=1, kind="page", file_page=1,
        printed_page="", title_path="", method="text", status=status,
        ocr_decision="not_needed", original_text=text, corrected_text=None,
        notes="", ocr_json=None))
    search_index.index_unit(ws, unit_id)
    return unit_id


def test_bootstrap_rejects_untrusted_host(web_workspace):
    app = create_app(web_workspace.root, start_worker=False)
    with TestClient(app, base_url="http://attacker.example:8765") as c:
        r = c.get("/api/v1/workspace")
        assert r.status_code in (400, 403)
        assert "set-cookie" not in r.headers


def test_normal_correction_advances_pagination_revision(web_workspace):
    uid = seed(web_workspace)
    old = search_index.search_indexed(web_workspace, "文庙")
    correct_unit(web_workspace, uid, "新古塔", "对照原页")
    with pytest.raises(search_index.IndexRevisionChanged):
        search_index.search_indexed(web_workspace, "文庙", expected_index_revision=old["index_revision"])


def test_matching_document_count_is_before_pagination(web_workspace):
    seed(web_workspace)
    seed(web_workspace, "two", "没有检索词")
    result = search_index.search_indexed(web_workspace, "文庙", page_size=1)
    assert result["total_documents"] == 1


def test_recovered_pause_can_really_be_claimed(web_workspace):
    store = JobStore(web_workspace)
    job = store.enqueue_import("unused", "x.pdf")
    with web_workspace.connect() as c:
        c.execute("UPDATE jobs SET state='pausing', pause_requested=1, lease_expires_at='2000' WHERE id=?", (job["id"],))
    assert store.recover_stale() == 1
    store.resume(job["id"])
    assert store.claim_next("new-worker")["id"] == job["id"]


def test_confirmed_blank_correction_becomes_searchable(web_workspace):
    uid = seed(web_workspace, text="", status="pending")
    with web_workspace.connect() as c:
        c.execute("UPDATE units SET blank_confirmed=1, status='completed' WHERE id=?", (uid,))
    search_index.index_unit(web_workspace, uid)
    correct_unit(web_workspace, uid, "实际有文庙", "人工转录原页")
    search_index.index_unit(web_workspace, uid)
    assert not web_workspace.get_unit(uid)["blank_confirmed"]
    assert search_index.search_indexed(web_workspace, "文庙")["total"] == 1


def test_duplicate_scan_reuses_good_text_and_manual_catalog(session, run_worker,
        web_workspace, web_fixtures, monkeypatch):
    def recognized(image):
        return dict(text="可靠文庙原文", lines=[], mean_confidence=.99,
                    render_width=image.width, render_height=image.height, engine="test")
    monkeypatch.setattr(ocr, "recognize", recognized)
    upload = [("files", ("扫描.pdf", web_fixtures["scan_pdf"].read_bytes(), "application/pdf"))]
    assert session.upload(upload).status_code == 202
    run_worker()
    doc = web_workspace.list_documents()[0]
    unit = web_workspace.units_for_doc(doc["id"])[0]
    evidence = save_evidence(web_workspace, unit["id"], "可靠文庙原文")
    review_evidence(web_workspace, evidence["id"], "accepted")
    web_workspace.update_document(doc["id"], title="人工书名", author="人工责任者")
    calls = []
    def unavailable(image):
        calls.append(True)
        raise RuntimeError("临时 OCR 故障")
    monkeypatch.setattr(ocr, "recognize", unavailable)
    assert session.upload(upload).status_code == 202
    run_worker()
    assert calls == []
    assert web_workspace.get_unit(unit["id"])["original_text"] == "可靠文庙原文"
    assert get_evidence(web_workspace, evidence["id"])["review_status"] == "accepted"
    fresh = web_workspace.get_document(doc["id"])
    assert (fresh["title"], fresh["author"]) == ("人工书名", "人工责任者")


def test_first_index_does_not_certify_racing_old_text(web_workspace, monkeypatch):
    uid = seed(web_workspace)
    with web_workspace.connect() as c:
        c.execute("UPDATE units SET text_sha256='' WHERE id=?", (uid,))
    connect = web_workspace.connect
    done = threading.Event()
    threads = []
    errors = []
    def edit():
        try:
            correct_unit(web_workspace, uid, "新古塔", "并发校订")
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, key): return getattr(self.conn, key)
        def execute(self, sql, *args):
            cursor = self.conn.execute(sql, *args)
            if sql.startswith("SELECT id, doc_id, seq, status"):
                row = cursor.fetchone()
                thread = threading.Thread(target=edit)
                threads.append(thread)
                thread.start()
                done.wait(.15)
                class Result:
                    def fetchone(self): return row
                return Result()
            return cursor
    @contextmanager
    def wrapped():
        with connect() as conn:
            yield Connection(conn)
    monkeypatch.setattr(web_workspace, "connect", wrapped)
    search_index.index_unit(web_workspace, uid)
    for thread in threads: thread.join(5)
    assert done.is_set() and not errors
    unit = web_workspace.get_unit(uid)
    assert unit["text_sha256"] == sha256_text(unit["corrected_text"])
    search_index.rebuild_dirty(web_workspace)
    assert search_index.search_indexed(web_workspace, "新古塔")["total"] == 1


def test_schema3_upgrade_replaces_old_dirty_trigger(web_workspace):
    uid = seed(web_workspace)
    with web_workspace.connect() as c:
        c.execute("DROP TRIGGER trg_units_index_update")
        c.execute("CREATE TRIGGER trg_units_index_update AFTER UPDATE OF corrected_text ON units BEGIN UPDATE index_units SET state='dirty' WHERE unit_id=NEW.id; END")
        c.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    migrations.ensure_schema(web_workspace)
    old = search_index.search_indexed(web_workspace, "文庙")
    correct_unit(web_workspace, uid, "新古塔", "校订")
    with pytest.raises(search_index.IndexRevisionChanged):
        search_index.search_indexed(web_workspace, "文庙", expected_index_revision=old["index_revision"])


def test_review_note_only_is_persisted(session, web_workspace):
    uid = seed(web_workspace)
    evidence = save_evidence(web_workspace, uid, "文庙")
    r = session.patch("/api/v1/evidence/" + evidence["id"], json={"review_note": "已核对"})
    assert r.status_code == 200
    assert r.json()["review_note"] == "已核对"


def test_manual_transcription_of_pending_page_is_searchable(session, web_workspace):
    uid = seed(web_workspace, text="", status="pending")
    r = session.patch("/api/v1/units/" + uid + "/correction", json={
        "text": "人工转录文庙", "reason": "原页有字", "expected_hash": sha256_text("")})
    assert r.status_code == 200
    assert search_index.search_indexed(web_workspace, "文庙")["total"] == 1


@pytest.mark.parametrize("status,original,correction", [
    ("completed", "原始文庙", ""),
    ("failed", "存疑文字", "人工文庙"),
    ("pending", "", "人工文庙"),
])
def test_undo_restores_extraction_status(web_workspace, status, original, correction):
    uid = seed(web_workspace, text=original, status=status)
    correct_unit(web_workspace, uid, correction, "校订")
    clear_correction(web_workspace, uid, "撤回校订")
    unit = web_workspace.get_unit(uid)
    assert unit["original_text"] == original
    assert unit["status"] == status
    search_index.index_unit(web_workspace, uid)
    assert search_index.search_indexed(web_workspace, "原始文庙")["total"] == (1 if status == "completed" else 0)
