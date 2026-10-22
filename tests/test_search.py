"""任务 1：本地中文检索。"""

from __future__ import annotations

from litworkbench import demo
from litworkbench.ingest import import_document, read_unit, search
from litworkbench.text import UnitStatus


def _units(ws, fixtures, key="text_pdf"):
    doc = import_document(ws, fixtures[key])
    return doc, ws.units_for_doc(doc["id"])


def test_short_chinese_term_matches(workspace, fixtures):
    import_document(workspace, fixtures["text_pdf"])
    hits = search(workspace, "塔")
    assert hits
    assert all("unit_id" in h and "snippet" in h for h in hits)
    unit_ids = {h["unit_id"] for h in hits}
    doc, units = _units(workspace, fixtures)
    assert unit_ids.issubset({u["id"] for u in units})


def test_two_character_term_and_locator(workspace, fixtures):
    import_document(workspace, fixtures["text_pdf"])
    hits = search(workspace, "白塔")
    assert hits
    first = hits[0]
    assert first["filename"] == fixtures["text_pdf"].name
    assert first["file_page"] >= 1
    assert first["status"] == UnitStatus.COMPLETED
    assert "白塔" in first["snippet"]


def test_cross_line_chinese_match(workspace, fixtures):
    """词组被换行拆开时仍可检索（合成 PDF 第 3 页刻意换行）。"""
    import_document(workspace, fixtures["text_pdf"])
    hits = search(workspace, "白塔建于道光十四年")
    assert hits, "跨换行中文短语应命中"
    assert hits[0]["file_page"] == 3


def test_punctuation_is_matched_literally(workspace, fixtures):
    import_document(workspace, fixtures["text_pdf"])
    # 原文确实包含这个全角逗号
    assert search(workspace, "北侧，")
    assert search(workspace, "白塔")
    assert not search(workspace, "白塔；")


def test_sql_wildcards_are_literal(workspace, fixtures):
    import_document(workspace, fixtures["text_pdf"])
    assert search(workspace, "%") == []
    assert search(workspace, "_") == []
    assert search(workspace, "白塔%") == []


def test_multiple_terms_are_anded(workspace, fixtures):
    import_document(workspace, fixtures["text_pdf"])
    assert search(workspace, "白塔 道光")
    assert search(workspace, "白塔 不存在的词") == []


def test_search_excludes_pending_units_unless_asked(workspace, fixtures):
    import pytest

    from litworkbench import ocr as ocr_mod

    if ocr_mod.is_available():
        pytest.skip("已安装 OCR，扫描页可直接检索")

    _units(workspace, fixtures, "scan_pdf")
    assert search(workspace, "白塔") == []
    # 待识别单元没有可检索文字，include_pending 也不返回伪造命中
    assert search(workspace, "白塔", include_pending=True) == []


def test_search_docx_and_pdf_together(workspace, fixtures):
    import_document(workspace, fixtures["text_pdf"])
    import_document(workspace, fixtures["docx"])
    hits = search(workspace, "白塔", limit=50)
    assert len({h["doc_id"] for h in hits}) >= 2


def test_search_limit_and_context(workspace, fixtures):
    import_document(workspace, fixtures["text_pdf"])
    hits = search(workspace, "塔", limit=1)
    assert len(hits) == 1
    assert len(hits[0]["snippet"]) <= 200


def test_read_unit_reports_versions(workspace, fixtures):
    from litworkbench.evidence import correct_unit

    doc, units = _units(workspace, fixtures)
    unit = units[1]
    data = read_unit(workspace, unit["id"])
    assert data["version"] == "original"
    before = data["text"]

    correct_unit(workspace, unit["id"], before.replace("白塔", "白塔塔"), "校订测试")
    after = read_unit(workspace, unit["id"])
    assert after["version"] == "corrected"
    assert after["original_text"] == before
    assert "白塔塔" in after["text"]


def test_search_finds_corrected_text(workspace, fixtures):
    from litworkbench.evidence import correct_unit

    doc, units = _units(workspace, fixtures)
    unit = units[0]
    correct_unit(workspace, unit["id"], "合成校订文字 甲乙丙丁", "仅用于检索测试")
    hits = search(workspace, "甲乙丙")
    assert any(h["unit_id"] == unit["id"] for h in hits)


def test_empty_query_raises(workspace):
    import pytest

    from litworkbench.errors import LitError

    with pytest.raises(LitError):
        search(workspace, "   ")


def test_ocr_text_is_searchable(workspace, fixtures):
    from litworkbench import ocr as ocr_mod
    import pytest

    if not ocr_mod.is_available():
        pytest.skip("未安装 OCR")
    import_document(workspace, fixtures["scan_pdf"])
    hits = search(workspace, "白塔")
    assert hits
    assert hits[0]["method"] == "ocr"


def test_demo_marker_present(workspace, fixtures):
    """夹具必须自带合成标识，避免被误认为真实县志。"""
    import_document(workspace, fixtures["text_pdf"])
    hits = search(workspace, demo.SYNTHETIC_MARK)
    assert hits