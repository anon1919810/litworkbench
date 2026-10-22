"""任务 2：导出报告与证据清单。"""

from __future__ import annotations

import csv
import json

import pytest

from litworkbench.evidence import REVIEW_ACCEPTED, REVIEW_REJECTED, save_evidence
from litworkbench.export import export_evidence, export_report
from litworkbench.ingest import import_document
from litworkbench.tasks import create_task, save_task_report

REPORT = {
    "title": "青川白塔考（合成样例）",
    "findings": [
        {"text": "白塔见于卷二古迹门，属县级文物保护单位。", "evidence_ids": [], "section": "源流与沿革"},
    ],
    "open_questions": ["白塔的确切重修年代仍需核对碑文。"],
    "status": "completed",
}


@pytest.fixture
def prepared(workspace, fixtures):
    """一个导入完成的文献 + 一条摘录 + 一个写好报告的任务。"""
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    task = create_task(
        workspace,
        task_id="task_test1",
        question="青川白塔见于何处？",
        profile_name="heritage",
        profile_snapshot={"name": "heritage", "sections": ["源流与沿革"]},
        config_snapshot={"provider": {"base_url": "http://127.0.0.1:1/v1"}},
        max_steps=8,
    )
    evidence = save_evidence(workspace, unit["id"], "白塔位于县城北侧", category="文物",
                             task_id=task["id"])
    REPORT["findings"][0]["evidence_ids"] = [evidence["id"]]
    save_task_report(workspace, task["id"], REPORT, status="completed")
    return {"doc": doc, "unit": unit, "evidence": evidence, "task": task}


def test_markdown_report_contains_locator_quote_and_state(workspace, prepared, tmp_path):
    out = export_report(workspace, prepared["task"]["id"], output=tmp_path / "报告.md")
    text = out.read_text(encoding="utf-8")
    assert "青川白塔考（合成样例）" in text
    assert "青川白塔见于何处？" in text
    assert "白塔见于卷二古迹门" in text
    assert "白塔位于县城北侧" in text          # 逐字引文
    assert "文物" in text                     # 分类
    assert "pending" in text                  # 核验状态
    assert prepared["evidence"]["id"] in text
    assert prepared["doc"]["filename"] in text
    assert "PDF 第 1 页" in text
    assert "结论仍需人工" in text or "研究结论" in text and "人工确认" in text


def test_markdown_escapes_markdown_control_chars(workspace, fixtures, tmp_path):
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    quote = "白塔位于县城北侧"
    create_task(
        workspace, task_id="task_esc", question="转义测试", profile_name="general",
        profile_snapshot={"name": "general"}, config_snapshot={}, max_steps=4,
    )
    evidence = save_evidence(workspace, unit["id"], quote, note="含有 | 竖线 与 *星号*",
                             task_id="task_esc")
    save_task_report(workspace, "task_esc", {
        "title": "转义 *标题*",
        "findings": [{"text": "包含 | 管道符 的结论", "evidence_ids": [evidence["id"]],
                      "section": "主要发现"}],
        "open_questions": [],
        "status": "completed",
    }, status="completed")

    text = export_report(workspace, "task_esc", output=tmp_path / "esc.md").read_text(encoding="utf-8")
    assert "\\| 管道符" in text
    assert "\\*标题\\*" in text
    assert quote in text


def test_json_report_is_structured(workspace, prepared, tmp_path):
    out = export_report(workspace, prepared["task"]["id"], output=tmp_path / "报告.json",
                        format="json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["question"] == "青川白塔见于何处？"
    assert payload["evidence"][0]["quote"] == "白塔位于县城北侧"
    assert payload["evidence"][0]["locator"]
    assert payload["evidence"][0]["file_page"] == 1
    assert payload["findings"][0]["evidence"][0]["id"] == prepared["evidence"]["id"]
    assert payload["findings"][0]["supported"] is True
    assert payload["evidence_verified"] is False
    assert payload["conclusions_verified"] is False


def test_docx_report_is_readable(workspace, prepared, tmp_path):
    import docx

    out = export_report(workspace, prepared["task"]["id"], output=tmp_path / "报告.docx",
                        format="docx")
    document = docx.Document(str(out))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "青川白塔考（合成样例）" in text
    assert "白塔位于县城北侧" in text
    assert prepared["doc"]["filename"] in text


def test_report_uses_stored_metadata_not_model_text(workspace, prepared, tmp_path):
    """模型自编的「出处」不进入报告；定位一律来自数据库。"""
    task = create_task(
        workspace, task_id="task_fake", question="模型编造出处", profile_name="general",
        profile_snapshot={"name": "general"}, config_snapshot={}, max_steps=4,
    )
    evidence = save_evidence(workspace, prepared["unit"]["id"], "白塔位于县城北侧", task_id=task["id"])
    save_task_report(workspace, "task_fake", {
        "title": "含编造引用",
        "findings": [{"text": "结论一", "evidence_ids": [evidence["id"]],
                      "section": "主要发现", "citation": "《不存在的地方志》卷九十九"}],
        "open_questions": [],
        "status": "completed",
    }, status="completed")
    out = export_report(workspace, "task_fake", output=tmp_path / "fake.md")
    text = out.read_text(encoding="utf-8")
    assert "不存在的地方志" not in text
    assert prepared["doc"]["filename"] in text


def test_rejected_evidence_is_flagged_but_still_shown(workspace, prepared, tmp_path):
    from litworkbench.evidence import review_evidence

    review_evidence(workspace, prepared["evidence"]["id"], REVIEW_REJECTED, "人工否定")
    out = export_report(workspace, prepared["task"]["id"], output=tmp_path / "rej.md")
    text = out.read_text(encoding="utf-8")
    assert "rejected" in text
    payload = json.loads(
        export_report(workspace, prepared["task"]["id"], output=tmp_path / "rej.json",
                      format="json").read_text(encoding="utf-8")
    )
    assert payload["evidence"][0]["review_status"] == REVIEW_REJECTED


def test_evidence_csv_columns_and_formula_guard(workspace, prepared, tmp_path):
    out = export_evidence(workspace, output=tmp_path / "证据.csv", format="csv",
                          task_id=prepared["task"]["id"])
    rows = list(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert rows
    for column in ("evidence_id", "filename", "file_page", "printed_page", "quote",
                   "category", "version", "review_status", "locator"):
        assert column in rows[0]
    assert rows[0]["quote"] == "白塔位于县城北侧"
    assert rows[0]["evidence_id"] == prepared["evidence"]["id"]


def test_evidence_csv_escapes_formula_like_values(workspace, prepared, tmp_path):
    ev = prepared["evidence"]
    with workspace.connect() as conn:
        conn.execute("UPDATE evidence SET note = ? WHERE id = ?", ("=SUM(A1:A9)", ev["id"]))
    out = export_evidence(workspace, output=tmp_path / "公式.csv", format="csv")
    rows = list(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))
    note = rows[0]["note"]
    assert note == "'=SUM(A1:A9)"  # 前缀单引号，避免被当成公式执行
    assert not note.startswith("=")


def test_evidence_json_export(workspace, prepared, tmp_path):
    out = export_evidence(workspace, output=tmp_path / "证据.json", format="json",
                          task_id=prepared["task"]["id"])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["evidence"][0]["quote"] == "白塔位于县城北侧"
    assert payload["evidence"][0]["filename"] == prepared["doc"]["filename"]


def test_export_refuses_task_without_report(workspace, tmp_path):
    from litworkbench.errors import LitError

    create_task(
        workspace, task_id="task_none", question="没有报告", profile_name="general",
        profile_snapshot={}, config_snapshot={}, max_steps=4,
    )
    with pytest.raises(LitError):
        export_report(workspace, "task_none", output=tmp_path / "x.md")


def test_export_defaults_into_workspace(workspace, prepared):
    out = export_report(workspace, prepared["task"]["id"])
    assert out.parent == workspace.artifacts_dir / "reports"
    assert out.exists()
    out.unlink()
