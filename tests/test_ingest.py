"""任务 1：工作区初始化与文档导入。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from litworkbench import ocr as ocr_mod
from litworkbench.errors import LitError
from litworkbench.ingest import import_document, import_path, retry_pending
from litworkbench.store import Workspace
from litworkbench.text import UnitStatus

HAS_OCR = ocr_mod.is_available()


def test_init_creates_layout_and_refuses_overwrite(tmp_path):
    root = tmp_path / "中文 空间" / "库"
    ws = Workspace(root)
    info = ws.initialize()
    assert info["created"] is True
    assert ws.config_path.exists()
    assert ws.db_path.exists()
    assert ws.sources_dir.is_dir()

    with pytest.raises(LitError):
        Workspace(root).initialize()  # 拒绝覆盖已有工作区
    forced = Workspace(root).initialize(force=True)
    assert forced["created"] is True

    with pytest.raises(LitError):
        Workspace.open(tmp_path / "尚未 初始化")


def test_init_writes_editable_config_with_profiles(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.initialize()
    cfg = ws.config
    assert cfg["provider"]["base_url"].startswith("http")
    assert cfg["provider"]["api_key_env"]
    assert cfg["ocr"]["mode"] in {"auto", "off", "force"}
    assert cfg["agent"]["max_steps"] > 0
    assert "search" in cfg["agent"]["allowed_tools"]


def test_import_text_pdf_creates_ordered_page_units(workspace, fixtures):
    doc = import_document(workspace, fixtures["text_pdf"])
    units = workspace.units_for_doc(doc["id"])
    assert doc["status"] == "completed"
    assert doc["filetype"] == "pdf"
    assert len(units) == 3
    assert [u["file_page"] for u in units] == [1, 2, 3]
    assert all(u["status"] == UnitStatus.COMPLETED for u in units)
    assert all(u["method"] == "text" for u in units)
    # 文字 PDF 不伪造行框
    assert all(u["ocr"] is None for u in units)
    assert "白塔" in units[1]["text"]
    # 原始副本不可变，内容哈希一致
    assert doc["sha256"] == hashlib.sha256(fixtures["text_pdf"].read_bytes()).hexdigest()
    assert workspace.path_of(doc).read_bytes() == fixtures["text_pdf"].read_bytes()


def test_import_docx_keeps_paragraph_and_table_order(workspace, fixtures):
    doc = import_document(workspace, fixtures["docx"])
    units = workspace.units_for_doc(doc["id"])
    kinds = [u["kind"] for u in units]
    assert kinds == ["paragraph", "paragraph", "table", "paragraph", "paragraph"]
    assert [u["seq"] for u in units] == [1, 2, 3, 4, 5]
    table = units[2]
    rows = table["text"].split("\n")
    assert rows[0].split("\t") == ["名称", "年代", "备注"]
    assert rows[1].split("\t") == ["白塔", "乾隆", "砖塔"]
    assert rows[2].split("\t") == ["白塔碑记", "乾隆", "残石"]
    assert units[3]["title_path"]
    assert all(u["file_page"] is None for u in units)  # DOCX 不编造页码


def test_image_only_pdf_marks_pending_without_ocr(workspace, fixtures):
    doc = import_document(workspace, fixtures["scan_pdf"], ocr_mode="auto")
    units = workspace.units_for_doc(doc["id"])
    assert len(units) == 1
    unit = units[0]
    assert unit["ocr_decision"] == "ocr"
    if HAS_OCR:
        assert unit["status"] == UnitStatus.COMPLETED
        assert unit["method"] == "ocr"
        assert "白塔" in unit["text"]
        assert unit["ocr"]["mean_confidence"] > 0
        assert unit["ocr"]["lines"][0]["box"]  # 真实像素坐标
        assert unit["ocr"]["render_width"] > 0
    else:
        assert unit["status"] == UnitStatus.PENDING
        assert "待识别" in unit["notes"]


def test_header_only_text_layer_still_enters_ocr(workspace, fixtures):
    """扫描件只带可选取的页码时不能被判为已完成文字页。"""
    doc = import_document(workspace, fixtures["header_scan_pdf"], ocr_mode="auto")
    unit = workspace.units_for_doc(doc["id"])[0]
    assert unit["ocr_decision"] == "ocr"
    assert unit["method"] != "text"
    if not HAS_OCR:
        assert unit["status"] == UnitStatus.PENDING


def test_mixed_pdf_decides_per_page(workspace, fixtures):
    doc = import_document(workspace, fixtures["mixed_pdf"], ocr_mode="auto")
    units = workspace.units_for_doc(doc["id"])
    assert [u["ocr_decision"] for u in units] == ["not_needed", "ocr"]
    if not HAS_OCR:
        assert [u["status"] for u in units] == [UnitStatus.COMPLETED, UnitStatus.PENDING]


def test_force_ocr_mode_ocrs_text_page(workspace, fixtures):
    doc = import_document(workspace, fixtures["text_pdf"], ocr_mode="force")
    units = workspace.units_for_doc(doc["id"])
    assert all(u["ocr_decision"] == "ocr" for u in units)
    assert all(u["method"] == "ocr" for u in units)


def test_duplicate_hash_does_not_duplicate_units(workspace, fixtures):
    first = import_document(workspace, fixtures["text_pdf"])
    copy = workspace.path_of(first).parent.parent / "副本 副本.pdf"
    copy.write_bytes(fixtures["text_pdf"].read_bytes())

    second = import_document(workspace, copy)
    assert second["id"] == first["id"]
    assert second["duplicate"] is True
    assert len(workspace.units_for_doc(first["id"])) == 3


def test_reimport_retries_pending_units(workspace, fixtures):
    doc = import_document(workspace, fixtures["scan_pdf"], ocr_mode="auto")
    unit = workspace.units_for_doc(doc["id"])[0]
    if HAS_OCR:
        pytest.skip("本环境已安装 OCR，单元首次导入即完成")
    assert unit["status"] == UnitStatus.PENDING

    result = retry_pending(workspace, doc_id=doc["id"], ocr_mode="auto")
    assert result["retried"] >= 1
    again = workspace.get_unit(unit["id"])
    assert again["status"] == UnitStatus.PENDING


def test_failed_unit_is_diagnosable_and_retryable(workspace, fixtures):
    doc = import_document(workspace, fixtures["scan_pdf"], ocr_mode="auto")
    unit = workspace.units_for_doc(doc["id"])[0]
    workspace.mark_unit_failed(unit["id"], "模拟故障：渲染器不可用")
    failed = workspace.get_unit(unit["id"])
    assert failed["status"] == UnitStatus.FAILED
    assert "模拟故障" in failed["notes"]

    import_document(workspace, fixtures["scan_pdf"], ocr_mode="auto")
    retried = workspace.get_unit(unit["id"])
    assert retried["status"] in {UnitStatus.COMPLETED, UnitStatus.PENDING}
    assert "模拟故障" not in retried["notes"]


def test_old_doc_is_rejected_with_guidance(workspace, tmp_path):
    legacy = tmp_path / "旧版 样例.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    with pytest.raises(LitError) as exc:
        import_document(workspace, legacy)
    assert ".docx" in str(exc.value)


def test_import_directory_uses_allowlist_and_skips_workspace(workspace, fixtures):
    target = Path(fixtures["dir"]).parent / "批量 目录"
    target.mkdir()
    (target / "说明.txt").write_text("忽略我", encoding="utf-8")
    (target / "子目录").mkdir()
    (target / "子目录" / "复制.pdf").write_bytes(fixtures["text_pdf"].read_bytes())

    result = import_path(workspace, target)
    assert result["imported"] == 1  # 只导入子目录里的那份 PDF
    assert result["skipped"] == 1  # 说明.txt 不在允许列表
    assert len(workspace.list_documents()) == 1


def test_read_unit_returns_locator_and_text(workspace, fixtures):
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    from litworkbench.ingest import read_unit

    data = read_unit(workspace, unit["id"])
    assert data["unit_id"] == unit["id"]
    assert data["file_page"] == 1
    assert data["filename"] == fixtures["text_pdf"].name
    assert "合成" in data["text"]


def test_render_page_writes_image(workspace, fixtures):
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    from litworkbench.ingest import render_page

    out = render_page(workspace, unit["id"], scale=1.5)
    assert out["path"].exists()
    assert out["width"] > 0 and out["height"] > 0


# --- 持久化与耐久性回归（来自独立验收） -------------------------------


def test_corrupt_pdf_never_becomes_completed(workspace, tmp_path):
    broken = tmp_path / "损坏 文件.pdf"
    broken.write_bytes(b"not a PDF at all")
    for _ in range(2):
        with pytest.raises(LitError):
            import_document(workspace, broken, ocr_mode="off")
        documents = workspace.list_documents()
        assert documents, "失败的导入也要留下可诊断的记录"
        assert documents[0]["status"] != "completed"
        assert documents[0]["unit_count"] == 0


def test_scan_imported_with_ocr_off_stays_retryable(workspace, fixtures):
    doc = import_document(workspace, fixtures["scan_pdf"], ocr_mode="off")
    unit = workspace.units_for_doc(doc["id"])[0]
    assert unit["status"] == UnitStatus.PENDING
    assert doc["status"] == "partial"

    result = retry_pending(workspace, doc_id=doc["id"], ocr_mode="auto")
    assert result["retried"] == 1
    after = workspace.get_unit(unit["id"])
    if HAS_OCR:
        assert after["status"] == UnitStatus.COMPLETED and after["method"] == "ocr"
        assert workspace.get_document(doc["id"])["status"] == "completed"
    else:
        assert after["status"] == UnitStatus.PENDING


def test_interrupted_import_keeps_finished_pages(workspace, fixtures, monkeypatch):
    def interrupted(_image):
        raise KeyboardInterrupt()

    monkeypatch.setattr(ocr_mod, "recognize", interrupted)
    with pytest.raises(KeyboardInterrupt):
        import_document(workspace, fixtures["mixed_pdf"])

    document = workspace.list_documents()[0]
    units = workspace.units_for_doc(document["id"])
    assert units and units[0]["file_page"] == 1
    assert units[0]["status"] == UnitStatus.COMPLETED
    assert document["status"] in {"partial", "interrupted", "failed"}

    # 中断后重新导入：已完成页保留，剩余页补做
    monkeypatch.undo()
    again = import_document(workspace, fixtures["mixed_pdf"])
    assert again["duplicate"] is True
    units = workspace.units_for_doc(document["id"])
    assert len(units) == 2
    assert all(unit["status"] == UnitStatus.COMPLETED for unit in units)


def test_unit_ids_never_collide_between_documents(workspace, fixtures):
    from litworkbench.ingest import _unit_id

    assert _unit_id("doc_1234561111111111", "page", 1) != _unit_id("doc_1234562222222222", "page", 1)

    first = import_document(workspace, fixtures["text_pdf"])
    second = import_document(workspace, fixtures["mixed_pdf"])
    assert first["id"] != second["id"]
    ids_first = {unit["id"] for unit in workspace.units_for_doc(first["id"])}
    ids_second = {unit["id"] for unit in workspace.units_for_doc(second["id"])}
    assert ids_first & ids_second == set()
    assert any(unit.endswith("_page_0001") for unit in ids_first)
    assert any(unit.endswith("_page_0001") for unit in ids_second)