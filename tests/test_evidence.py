"""任务 2：摘录、校订与审核。"""

from __future__ import annotations

import pytest

from litworkbench.errors import NotFoundError, QuotationError
from litworkbench.evidence import (
    REVIEW_ACCEPTED,
    REVIEW_REJECTED,
    REVIEW_STALE,
    correct_unit,
    get_evidence,
    list_evidence,
    review_evidence,
    save_evidence,
)
from litworkbench.ingest import import_document


@pytest.fixture
def doc_and_unit(workspace, fixtures):
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]  # 第 1 页含「白塔位于县城北侧」
    return doc, unit


def test_evidence_requires_verbatim_quote(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    with pytest.raises(QuotationError):
        save_evidence(workspace, unit["id"], "白塔位于城南，是本县最大的古塔")
    assert list_evidence(workspace, unit_id=unit["id"]) == []


def test_evidence_offsets_are_exact(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    quote = "白塔位于县城北侧"
    ev = save_evidence(workspace, unit["id"], quote, category="文物")
    assert unit["original_text"][ev["start_offset"]:ev["end_offset"]] == quote
    assert ev["quote"] == quote
    assert ev["version"] == "original"
    assert ev["review_status"] == "pending"
    assert ev["category"] == "文物"
    assert "PDF 第 1 页" in ev["locator"]


def test_evidence_prefers_corrected_version(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    correct_unit(workspace, unit["id"], unit["original_text"].replace("北侧", "北郊"), "校订地名")
    ev = save_evidence(workspace, unit["id"], "白塔位于县城北郊")
    assert ev["version"] == "corrected"
    corrected = workspace.get_unit(unit["id"])["corrected_text"]
    assert corrected[ev["start_offset"]:ev["end_offset"]] == ev["quote"]

    with pytest.raises(QuotationError):
        save_evidence(workspace, unit["id"], "白塔位于县城北侧")  # 旧文字已不在校订版中


def test_cross_line_quote_is_exact_source_slice(workspace, fixtures):
    """跨行匹配时保存的引文必须与原文切片完全一致（偏移自洽）。"""
    doc = import_document(workspace, fixtures["docx"])
    unit = workspace.units_for_doc(doc["id"])[-1]
    text = "合成测试：非物质文化\n遗产。"
    correct_unit(workspace, unit["id"], text, "测试跨行引用")
    ev = save_evidence(workspace, unit["id"], "非物质文化遗产", "活动")
    assert ev["quote"] == text[ev["start_offset"]:ev["end_offset"]]
    assert "\n" in ev["quote"]


def test_correction_keeps_original_and_marks_evidence_stale(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    original = unit["original_text"]
    ev = save_evidence(workspace, unit["id"], "白塔位于县城北侧")
    review_evidence(workspace, ev["id"], REVIEW_ACCEPTED, "人工看过")

    correct_unit(workspace, unit["id"], original.replace("北侧", "北郊"), "校订地名")

    updated = get_evidence(workspace, ev["id"])
    assert updated["review_status"] == REVIEW_STALE
    assert updated["quote"] == "白塔位于县城北侧"  # 引文不被改写
    unit_after = workspace.get_unit(unit["id"])
    assert unit_after["original_text"] == original  # 原始识别结果不被覆盖
    revisions = workspace.revisions_for_unit(unit["id"])
    assert revisions and revisions[-1]["reason"] == "校订地名"
    assert revisions[-1]["new_text"] == unit_after["corrected_text"]


def test_printed_page_label_does_not_touch_text(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    ev = save_evidence(workspace, unit["id"], "白塔位于县城北侧")
    workspace.set_printed_page(unit["id"], "317")
    refreshed = workspace.get_unit(unit["id"])
    assert refreshed["printed_page"] == "317"
    assert refreshed["file_page"] == 1  # 文件页序不变
    assert refreshed["original_text"] == unit["original_text"]
    assert get_evidence(workspace, ev["id"])["printed_page"] == "317"


def test_evidence_survives_reimport_and_label_changes(workspace, fixtures):
    """重复导入 / 改页码 / 校订都不应删掉已有摘录（upsert 不做替换删除）。"""
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    ev = save_evidence(workspace, unit["id"], "白塔位于县城北侧")

    workspace.set_printed_page(unit["id"], "12")
    import_document(workspace, fixtures["text_pdf"])  # 重复导入（相同哈希）
    corrected = workspace.get_unit(unit["id"])["original_text"].replace("北侧", "北郊")
    correct_unit(workspace, unit["id"], corrected, "重新确认校订")

    assert len(list_evidence(workspace, unit_id=unit["id"])) == 1
    assert workspace.revisions_for_unit(unit["id"])
    assert get_evidence(workspace, ev["id"])["review_status"] == REVIEW_STALE
    assert get_evidence(workspace, ev["id"])["quote"] == "白塔位于县城北侧"


def test_rejected_evidence_is_listed_but_flagged(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    ev = save_evidence(workspace, unit["id"], "白塔位于县城北侧")
    review_evidence(workspace, ev["id"], REVIEW_REJECTED, "OCR 误识，需重做")
    stored = get_evidence(workspace, ev["id"])
    assert stored["review_status"] == REVIEW_REJECTED
    assert stored["review_note"] == "OCR 误识，需重做"
    assert list_evidence(workspace, review_status=REVIEW_REJECTED)[0]["id"] == ev["id"]
    assert list_evidence(workspace, review_status=REVIEW_ACCEPTED) == []


def test_review_rejects_unknown_status(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    ev = save_evidence(workspace, unit["id"], "白塔位于县城北侧")
    with pytest.raises(QuotationError):
        review_evidence(workspace, ev["id"], "looks-good")
    with pytest.raises(NotFoundError):
        review_evidence(workspace, "ev_不存在", REVIEW_ACCEPTED)


def test_evidence_requires_existing_task(workspace, doc_and_unit):
    doc, unit = doc_and_unit
    with pytest.raises(NotFoundError):
        save_evidence(workspace, unit["id"], "白塔位于县城北侧", task_id="task_不存在")


def test_evidence_for_unknown_unit(workspace):
    with pytest.raises(NotFoundError):
        save_evidence(workspace, "u_不存在", "随便")