"""Excel（.xlsx）导出：五张表、列契约、类型、降级路径。

全部用 ``litworkbench.demo`` 生成的合成数据；不联网、不写库，导出只读。
"""

from __future__ import annotations

import builtins
import json
import sqlite3
import sys
from datetime import datetime

import pytest

from litworkbench import cli
from litworkbench.errors import LitError
from litworkbench.evidence import review_evidence, save_evidence
from litworkbench.ingest import import_document
from litworkbench.xlsx_export import (
    CONTRACTS,
    DATE_FORMAT,
    MAX_COLUMN_WIDTH,
    ROW_COUNT_LABELS,
    SHEETS,
    SKIP_LABELS,
    export_xlsx,
    schema_contract,
)

QUOTE = "白塔位于县城北侧"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def prepared(workspace, fixtures):
    """一篇已导入的文献 + 一条已接受的摘录（含印刷页码）。"""
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    evidence = save_evidence(workspace, unit["id"], QUOTE, category="文物")
    review_evidence(workspace, evidence["id"], status="accepted", note="已对照原页")
    workspace.set_printed_page(unit["id"], "12")
    return {"doc": doc, "unit": unit, "evidence": evidence}


@pytest.fixture
def exported(workspace, prepared, tmp_path):
    path = tmp_path / "资料 集.xlsx"
    result = export_xlsx(workspace, output=path)
    return {"path": path, "result": result, "workspace": workspace,
            "unit": prepared["unit"]}


def load(path):
    openpyxl = pytest.importorskip("openpyxl")
    return openpyxl.load_workbook(path)


def header(sheet):
    return [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]


def help_value(help_sheet, label):
    for row in help_sheet.iter_rows(values_only=True):
        if row[0] == label:
            return row[1]
    raise AssertionError(f"说明表里没有 {label}")


def column(sheet, name):
    """按列名取整列（含表头）的值。"""
    index = header(sheet).index(name) + 1
    return [row[0] for row in sheet.iter_rows(min_row=1, min_col=index, max_col=index,
                                              values_only=True)]


# ------------------------------------------------------------------ 五张表


def test_workbook_has_five_sheets_in_fixed_order(exported):
    workbook = load(exported["path"])
    assert workbook.sheetnames == list(SHEETS)
    assert SHEETS == ("说明", "文献", "摘录", "证据", "待核")


def test_column_names_and_order_match_contract(exported):
    workbook = load(exported["path"])
    for name in ("文献", "摘录", "证据", "待核"):
        assert header(workbook[name]) == [spec.name for spec in CONTRACTS[name]]
    # 待核 = 证据的列 + 候选建议 一列
    assert header(workbook["待核"]) == header(workbook["证据"]) + ["候选建议"]


def test_schema_contract_is_valid_json_and_stable():
    first = schema_contract()
    second = schema_contract()
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True)
    reloaded = json.loads(json.dumps(first, ensure_ascii=False))
    assert reloaded["format"] == "xlsx"
    assert reloaded["sheet_order"] == list(SHEETS)
    assert [sheet["name"] for sheet in reloaded["sheets"]] == ["文献", "摘录", "证据", "待核"]
    for sheet in reloaded["sheets"]:
        for entry in sheet["columns"]:
            assert set(entry) == {"name", "type", "required", "readonly", "enum", "note"}
    evidence_sheet = next(s for s in reloaded["sheets"] if s["name"] == "证据")
    for required in ("unit_id", "字符区间", "原件哈希"):
        assert required in evidence_sheet["required_per_row"]
    # 契约里不含生成时间：同一份代码永远得到同样的文档
    assert "生成时间" not in json.dumps(first, ensure_ascii=False)


def test_columns_are_declared_in_the_help_sheet(exported):
    workbook = load(exported["path"])
    help_sheet = workbook["说明"]
    lines = [row[0] for row in help_sheet.iter_rows(min_row=2, values_only=True)]
    for name in ("文献", "摘录", "证据", "待核"):
        for spec in CONTRACTS[name]:
            expected = spec.contract_line(name)
            assert expected in lines, expected
    assert help_value(help_sheet, "工作区") == str(exported["workspace"].root)
    assert isinstance(help_value(help_sheet, "生成时间"), datetime)


# ------------------------------------------------------------------ 类型


def test_numeric_and_date_cells_are_real_numbers_and_dates(exported):
    workbook = load(exported["path"])
    documents = workbook["文献"]
    assert isinstance(column(documents, "页数")[1], int)
    assert isinstance(column(documents, "已索引页数")[1], int)
    assert isinstance(column(documents, "更新时间")[1], datetime)
    updated_index = header(documents).index("更新时间") + 1
    assert documents.cell(row=2, column=updated_index).number_format == DATE_FORMAT

    evidence_sheet = workbook["证据"]
    assert column(evidence_sheet, "文件页")[1] == 1
    assert isinstance(column(evidence_sheet, "文件页")[1], int)
    assert column(evidence_sheet, "印刷页")[1] == 12
    assert isinstance(column(evidence_sheet, "印刷页")[1], int)
    assert isinstance(column(evidence_sheet, "更新时间")[1], datetime)
    start, end = column(evidence_sheet, "字符区间")[1].split("-")
    assert int(end) - int(start) == len(QUOTE)      # 区间长度与引文一致
    assert int(start) == (exported["unit"]["text"] or "").index(QUOTE)
    assert len(column(evidence_sheet, "原件哈希")[1]) == 64

    excerpts = workbook["摘录"]
    assert isinstance(column(excerpts, "创建时间")[1], datetime)
    assert column(excerpts, "版本")[1] == "原文"
    assert column(excerpts, "核验状态")[1] == "accepted"
    assert column(excerpts, "摘录原文")[1] == QUOTE


def test_numeric_printed_page_falls_back_to_text(workspace, fixtures, tmp_path):
    """印刷页是区间/字母时按文本原样写入，绝不丢成空或猜成数字。"""
    doc = import_document(workspace, fixtures["text_pdf"])
    unit = workspace.units_for_doc(doc["id"])[0]
    workspace.set_printed_page(unit["id"], "12-13")
    evidence = save_evidence(workspace, unit["id"], QUOTE)
    path = tmp_path / "印刷页.xlsx"
    export_xlsx(workspace, output=path)
    workbook = load(path)
    assert column(workbook["摘录"], "印刷页")[1] == "12-13"
    assert column(workbook["证据"], "evidence_id")[1] == evidence["id"]


def test_sheets_are_frozen_filtered_and_bold(exported):
    workbook = load(exported["path"])
    for name in SHEETS:
        sheet = workbook[name]
        assert sheet.freeze_panes == "A2", name
        assert sheet.auto_filter.ref == sheet.dimensions, name
        assert sheet.cell(row=1, column=1).font.bold is True, name
        for letter, dimension in sheet.column_dimensions.items():
            assert dimension.width is None or dimension.width <= MAX_COLUMN_WIDTH, (name, letter)


# ------------------------------------------------------------------ 降级


def _break_openpyxl(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl" or name.startswith("openpyxl."):
            raise ImportError("No module named 'openpyxl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "openpyxl", raising=False)


def test_missing_openpyxl_gives_chinese_error_without_traceback(
        workspace, prepared, tmp_path, monkeypatch, capsys):
    _break_openpyxl(monkeypatch)
    target = tmp_path / "缺依赖.xlsx"
    with pytest.raises(LitError) as excinfo:
        export_xlsx(workspace, output=target)
    message = str(excinfo.value)
    assert "openpyxl" in message
    assert "litworkbench[xlsx]" in message
    assert "pip install" in message
    assert not target.exists()          # 依赖缺失时不留下半个文件

    code = cli.main(["--workspace", str(workspace.root), "export", "xlsx",
                     "--out", str(target)])
    captured = capsys.readouterr()
    assert code == 1
    assert "错误：" in captured.err
    assert "openpyxl" in captured.err
    assert "Traceback" not in captured.err + captured.out

    # 列契约是静态文档：缺依赖也能读出来
    assert schema_contract()["format"] == "xlsx"


# ------------------------------------------------------------------ 边界


def test_empty_workspace_exports_headers_only(workspace, tmp_path):
    path = tmp_path / "空库.xlsx"
    result = export_xlsx(workspace, output=path)
    assert result["sheets"] == {"文献": 0, "摘录": 0, "证据": 0, "待核": 0}
    workbook = load(path)
    assert workbook.sheetnames == list(SHEETS)
    for name in ("文献", "摘录", "证据", "待核"):
        assert workbook[name].max_row == 1
        assert header(workbook[name]) == [spec.name for spec in CONTRACTS[name]]
    help_sheet = workbook["说明"]
    assert help_value(help_sheet, ROW_COUNT_LABELS["证据"]) == 0
    assert help_value(help_sheet, SKIP_LABELS["证据"]) == 0


def _insert_orphan_evidence(workspace, doc) -> str:
    """写入一条出处已不存在的证据行（绕过外键，模拟库被手工改坏）。"""
    orphan_id = "ev_orphan_test"
    conn = sqlite3.connect(workspace.db_path)   # 默认不启用外键：故意造坏数据
    try:
        conn.execute(
            "INSERT INTO evidence (id, unit_id, doc_id, task_id, quote, start_offset, "
            "end_offset, version, version_snapshot, category, note, review_status, "
            "review_note, created_at, updated_at) "
            "VALUES (?, 'u_missing', ?, NULL, ?, 0, 4, 'original', '{}', '', '', "
            "'pending', '', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (orphan_id, doc["id"], QUOTE[:4]),
        )
        conn.commit()
    finally:
        conn.close()
    return orphan_id


def test_rows_without_unit_id_are_skipped_and_counted(workspace, prepared, tmp_path):
    orphan_id = _insert_orphan_evidence(workspace, prepared["doc"])
    path = tmp_path / "跳过.xlsx"
    result = export_xlsx(workspace, output=path)
    assert result["skipped"]["证据"] == 1
    assert result["skipped"]["摘录"] == 1
    assert result["skipped"]["待核"] == 0

    workbook = load(path)
    assert orphan_id not in column(workbook["证据"], "evidence_id")
    assert orphan_id not in column(workbook["摘录"], "evidence_id")
    help_sheet = workbook["说明"]
    assert help_value(help_sheet, SKIP_LABELS["证据"]) == 1
    assert help_value(help_sheet, SKIP_LABELS["摘录"]) == 1
    # 正常的行照常导出，没有被「跳过」连坐
    assert prepared["evidence"]["id"] in column(workbook["证据"], "evidence_id")
    assert help_value(help_sheet, ROW_COUNT_LABELS["证据"]) == 1


def test_pending_proofread_candidate_lands_in_review_sheet(workspace, prepared, tmp_path):
    unit = prepared["unit"]
    text = unit["text"] or ""
    start = text.index(QUOTE)
    with workspace.connect() as conn:
        conn.execute(
            "INSERT INTO proofread_candidates (id, unit_id, start_offset, end_offset, "
            "original, suggestion, reason, model, status, decision_note, base_text_sha256, "
            "decided_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '模型判断', "
            "'fake-model', 'pending', '', '', NULL, ?, ?)",
            ("pf_test_pending", unit["id"], start, start + len(QUOTE), QUOTE,
             "白塔位于县城北郊", "2026-01-01T00:00:10+00:00", "2026-01-01T00:00:10+00:00"),
        )
        conn.commit()
        digest = conn.execute("SELECT sha256 FROM documents WHERE id = ?",
                             (prepared["doc"]["id"],)).fetchone()["sha256"]

    path = tmp_path / "待核.xlsx"
    result = export_xlsx(workspace, output=path)
    assert result["sheets"]["待核"] == 1
    workbook = load(path)
    sheet = workbook["待核"]
    assert header(sheet) == header(workbook["证据"]) + ["候选建议"]
    assert column(sheet, "evidence_id")[1] == "pf_test_pending"
    assert column(sheet, "unit_id")[1] == unit["id"]
    assert column(sheet, "字符区间")[1] == f"{start}-{start + len(QUOTE)}"
    assert column(sheet, "候选建议")[1] == "白塔位于县城北郊"
    assert column(sheet, "核验状态")[1] == "pending"
    assert column(sheet, "原件哈希")[1] == digest
    assert isinstance(column(sheet, "更新时间")[1], datetime)


def test_cli_schemas_output_is_json_and_stable(workspace, capsys):
    assert cli.main(["--workspace", str(workspace.root), "export", "xlsx",
                     "--schemas"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["--workspace", str(workspace.root), "export", "xlsx",
                     "--schemas"]) == 0
    second = capsys.readouterr().out
    assert first == second
    payload = json.loads(first)
    assert payload["sheets"][0]["name"] == "文献"
    assert payload["data_sheets"] == ["文献", "摘录", "证据", "待核"]


def test_cli_writes_the_workbook(workspace, prepared, tmp_path, capsys):
    target = tmp_path / "命令行.xlsx"
    assert cli.main(["--workspace", str(workspace.root), "export", "xlsx",
                     "--out", str(target)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is True
    assert payload["sheets"]["文献"] == 1
    assert payload["sheets"]["证据"] == 1
    assert target.exists() and target.stat().st_size == payload["bytes"]
    assert load(target).sheetnames == list(SHEETS)
