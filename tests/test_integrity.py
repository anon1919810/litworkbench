"""Document durable storage and evidence integrity regressions from independent review."""
from datetime import datetime
from pathlib import Path
import sqlite3
import tomllib

from docx import Document
import pytest

from litworkbench import ingest, ocr
from litworkbench.cli import main
from litworkbench.config import render_config_toml
from litworkbench.errors import LitError
from litworkbench.evidence import (
    clear_correction, correct_unit, get_evidence, review_evidence, save_evidence,
)
from litworkbench.store import sha256_file


def test_interrupted_copy_does_not_publish_partial_original(workspace, fixtures, monkeypatch):
    source = fixtures["text_pdf"]
    copy = ingest.shutil.copy2

    def interrupted(src, dst):
        Path(dst).write_bytes(Path(src).read_bytes()[:20])
        raise KeyboardInterrupt()

    monkeypatch.setattr(ingest.shutil, "copy2", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ingest.import_document(workspace, source, ocr_mode="off")
    monkeypatch.setattr(ingest.shutil, "copy2", copy)
    doc = ingest.import_document(workspace, source, ocr_mode="off")
    assert doc["status"] == "completed"
    assert sha256_file(workspace.path_of(doc)) == sha256_file(source)
    assert not list(workspace.sources_dir.rglob("*.part"))


def test_reimport_repairs_corrupt_cached_original(workspace, fixtures):
    source = fixtures["text_pdf"]
    doc = ingest.import_document(workspace, source, ocr_mode="off")
    workspace.path_of(doc).write_bytes(b"damaged cache")
    ingest.import_document(workspace, source, ocr_mode="off")
    assert sha256_file(workspace.path_of(doc)) == doc["sha256"]


def test_recovery_failure_does_not_complete_missing_pages(workspace, fixtures, monkeypatch):
    def interrupted(_image):
        raise KeyboardInterrupt()

    monkeypatch.setattr(ocr, "recognize", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ingest.import_document(workspace, fixtures["mixed_pdf"])
    doc = workspace.list_documents()[0]
    assert len(workspace.units_for_doc(doc["id"])) == 1

    def failed(*args, **kwargs):
        raise LitError("Second page unavailable")

    monkeypatch.setattr(ingest, "_extract_into", failed)
    result = ingest.retry_pending(workspace, doc["id"], ocr_mode="off")
    after = workspace.get_document(doc["id"])
    assert result["errors"]
    assert after["status"] != "completed"
    assert after["pending_count"] == 1


def test_correction_and_revert_roll_back_if_history_write_fails(workspace, fixtures):
    doc = ingest.import_document(workspace, fixtures["docx"])
    unit = workspace.units_for_doc(doc["id"])[-1]
    quote = unit["original_text"]
    ev = save_evidence(workspace, unit["id"], quote)
    review_evidence(workspace, ev["id"], "accepted")
    with workspace.connect() as conn:
        conn.executescript("CREATE TRIGGER fail_revision BEFORE INSERT ON unit_revisions BEGIN SELECT RAISE(ABORT, 'injected failure'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        correct_unit(workspace, unit["id"], "修改文字", "检查事务")
    assert workspace.get_unit(unit["id"])["corrected_text"] is None
    assert get_evidence(workspace, ev["id"])["review_status"] == "accepted"
    with workspace.connect() as conn:
        conn.execute("DROP TRIGGER fail_revision")
    correct_unit(workspace, unit["id"], "修改文字", "检查撤销")
    with workspace.connect() as conn:
        conn.executescript("CREATE TRIGGER fail_revision BEFORE INSERT ON unit_revisions BEGIN SELECT RAISE(ABORT, 'injected failure'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        clear_correction(workspace, unit["id"], "撤销")
    assert workspace.get_unit(unit["id"])["corrected_text"] == "修改文字"


def test_ocr_retry_keeps_old_extraction_and_invalidates_old_evidence(workspace, fixtures, monkeypatch):
    doc = ingest.import_document(workspace, fixtures["header_scan_pdf"], ocr_mode="off")
    unit = workspace.units_for_doc(doc["id"])[0]
    ev = save_evidence(workspace, unit["id"], unit["original_text"])
    review_evidence(workspace, ev["id"], "accepted")

    def recognized(image):
        return {"text": "合成扫描文字已重新识别。", "lines": [], "mean_confidence": 0.99,
                "render_width": image.width, "render_height": image.height}

    monkeypatch.setattr(ocr, "recognize", recognized)
    ingest.import_document(workspace, fixtures["header_scan_pdf"], ocr_mode="auto")
    assert get_evidence(workspace, ev["id"])["review_status"] == "stale"
    revisions = workspace.revisions_for_unit(unit["id"])
    assert revisions[0]["old_text"] == unit["original_text"]
    assert revisions[0]["kind"] == "extraction"


def test_docx_created_timestamp_is_not_publication_year(workspace, tmp_path):
    path = tmp_path / "1994年县志_合成.docx"
    document = Document()
    document.core_properties.created = datetime(2026, 10, 11)
    document.add_paragraph("合成验收资料，不是历史文献。")
    document.save(path)
    assert ingest.import_document(workspace, path)["year"] == ""


def test_empty_correction_hides_noise_until_reverted(workspace, fixtures):
    doc = ingest.import_document(workspace, fixtures["docx"])
    unit = workspace.units_for_doc(doc["id"])[-1]
    correct_unit(workspace, unit["id"], "", "删除错误识别文字")
    read = ingest.read_unit(workspace, unit["id"])
    assert read["text"] == "" and read["version"] == "corrected"
    assert unit["id"] not in {h["unit_id"] for h in ingest.search(workspace, "白塔")}
    clear_correction(workspace, unit["id"], "恢复原文")
    assert ingest.read_unit(workspace, unit["id"])["text"] == unit["original_text"]


def test_search_returns_the_version_that_actually_matches(workspace, fixtures):
    doc = ingest.import_document(workspace, fixtures["docx"])
    unit = workspace.units_for_doc(doc["id"])[-1]
    correct_unit(workspace, unit["id"], "合成黑塔说明。", "验收")
    assert unit["id"] not in {h["unit_id"] for h in ingest.search(workspace, "白塔")}
    hit = next(h for h in ingest.search(workspace, "黑塔") if h["unit_id"] == unit["id"])
    assert hit["version"] == "corrected" and "黑塔" in hit["snippet"]


def test_multiline_config_round_trips():
    text = "第一行\n第二行\t引号\"和斜杠\\"
    rendered = render_config_toml({"custom": {"prompt": text}})
    assert tomllib.loads(rendered)["custom"]["prompt"] == text


@pytest.mark.parametrize("suffix", [".pdf", ".doc"])
def test_failed_cli_import_returns_nonzero_and_reason(workspace, tmp_path, capsys, suffix):
    source = tmp_path / ("损坏或不支持" + suffix)
    source.write_bytes(b"not a supported document")
    assert main(["--workspace", str(workspace.root), "import", str(source)]) == 1
    captured = capsys.readouterr()
    assert "导入失败" in captured.err
    if suffix == ".doc":
        assert ".docx" in captured.err
