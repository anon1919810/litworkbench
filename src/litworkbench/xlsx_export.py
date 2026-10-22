"""Excel（``.xlsx``）导出：一个五张表的工作簿 + 列契约 JSON。

契约要点：

* **五张表，顺序固定**：说明、文献、摘录、证据、待核；
* **只读**：整个导出只有一条读事务，不写库、不改结构、不联网；
* **复用既有装配**：证据行与出处一律走 ``evidence_exports.select_rows`` 与
  ``export.evidence_view``，本模块不另写一套取数 SQL；只有确实缺字段的两处
  （证据行的 ``updated_at``、待核候选与单元的关联）才补查询，并在注释里写明原因；
* **字段即契约**：每列的列名／类型／是否必填／枚举取值／只读标记既写进「说明」表，
  也由 ``lit export xlsx --schemas`` 以 JSON 输出，供 Agent 照单填列；
* **缺必需字段的行不导出**：证据／待核行缺 ``unit_id``、字符区间或原件哈希时跳过，
  跳过条数写进「说明」表，绝不静默补默认值；
* **openpyxl 是可选依赖**：没装时给明确中文提示并提示装 extra，
  不抛栈，也不影响 csv/json/markdown/docx 这些既有导出格式。

工作簿是**资料集**，不是研究报告：不生成结论、不评价文献、不做归纳。
"""

from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from litworkbench import __version__, search_index
from litworkbench.errors import LitError, ValidationError
from litworkbench.evidence import REVIEW_PENDING, REVIEW_STALE, REVIEW_STATUSES
from litworkbench.evidence_exports import SCOPE_ALL, normalize_scope, select_rows
from litworkbench.export import _resolve_output, evidence_repository, evidence_view
from litworkbench.store import Workspace, utcnow

#: 表顺序固定：说明在前，其余按「文献 → 摘录 → 证据 → 待核」递进。
SHEETS = ("说明", "文献", "摘录", "证据", "待核")
#: 说明表的表名（其余四张是数据表）
HELP_SHEET = "说明"
DATA_SHEETS = SHEETS[1:]
#: 列契约本身的版本：改列名/列序/枚举时必须递增
CONTRACT_VERSION = 1
#: Excel 单元格上限是 32767 字符；超长引文截断（见说明表「已知限制」）
CELL_TEXT_LIMIT = 32767
#: 日期一律写成真日期，显示到分钟
DATE_FORMAT = "yyyy-mm-dd hh:mm"
#: 列宽自适应上限（字符数，全角按 2 计）
MAX_COLUMN_WIDTH = 60.0
MIN_COLUMN_WIDTH = 8.0

#: 「待核」表收录的摘录复核状态：已拒绝的不再需要复核
REVIEW_STATUSES_PENDING = (REVIEW_PENDING, REVIEW_STALE)

MISSING_DEPENDENCY = (
    "缺少可选依赖 openpyxl，无法导出 Excel（.xlsx）："
    "请先安装 pip install \"litworkbench[xlsx]\"（或直接 pip install openpyxl）后重试。"
    "csv / json / markdown / docx 导出不受影响。"
)

#: 说明表 B 列里「每张表行数」的标签（测试与说明表共用同一份文字）
ROW_COUNT_LABELS = {name: f"行数：{name}" for name in SHEETS if name != HELP_SHEET}
#: 说明表 B 列里「跳过行数」的四个标签
SKIP_LABELS = {
    "摘录": "跳过行数：摘录（证据行无法定位出处）",
    "证据": "跳过行数：证据（缺 unit_id／字符区间／原件哈希）",
    "待核": "跳过行数：待核（缺 unit_id／字符区间／原件哈希）",
}

# ------------------------------------------------------------------ 列契约


@dataclass(frozen=True)
class Column:
    """一列的契约：列名、类型、是否必填、枚举取值、只读标记。"""

    name: str
    type: str
    required: bool
    readonly: bool
    enum: tuple[str, ...] = ()
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "readonly": self.readonly,
            "enum": list(self.enum),
            "note": self.note,
        }

    def contract_line(self, sheet: str) -> str:
        """说明表 A 列的一行：列名、类型、是否必填、枚举取值、只读标记。"""
        return "｜".join((
            sheet,
            self.name,
            f"类型：{self.type}",
            "必填" if self.required else "可空",
            "只读" if self.readonly else "可写",
            "枚举：" + ("/".join(self.enum) if self.enum else "无"),
        ))


_REVIEW_ENUM = tuple(REVIEW_STATUSES)
_VERSION_ENUM = ("原文", "校订")

_DOCUMENT_COLUMNS = (
    Column("doc_id", "文本", True, True),
    Column("书名", "文本", False, True, note="书目题名，缺省为空"),
    Column("原始文件名", "文本", True, True),
    Column("类型", "枚举", True, True, enum=("pdf", "docx")),
    Column("页数", "整数", False, True, note="PDF 页数；未知时留空，绝不填 0"),
    Column("已索引页数", "整数", True, True, note="当前版本已建立检索索引的单元数"),
    Column("状态", "文本", True, True),
    Column("原件哈希", "文本", True, True, note="原件 SHA-256，不是正文哈希"),
    Column("更新时间", "日期", True, True),
)

_EXCERPT_COLUMNS = (
    Column("evidence_id", "文本", True, True),
    Column("doc_id", "文本", True, True),
    Column("书名", "文本", False, True),
    Column("文件页", "整数", False, True, note="PDF 文件页序；DOCX 段无文件页时留空"),
    Column("印刷页", "整数", False, True,
           note="原始记录是区间或文字时按文本原样写入"),
    Column("摘录原文", "文本", True, False),
    Column("字符区间", "文本", True, True, note="start-end，指向记录时的文字版本"),
    Column("版本", "枚举", True, False, enum=_VERSION_ENUM),
    Column("核验状态", "枚举", True, False, enum=_REVIEW_ENUM),
    Column("创建时间", "日期", True, True),
)

_EVIDENCE_COLUMNS = (
    Column("evidence_id", "文本", True, True),
    Column("unit_id", "文本", True, True),
    Column("书名", "文本", False, True),
    Column("文件页", "整数", False, True),
    Column("印刷页", "整数", False, True),
    Column("原文切片", "文本", True, True),
    Column("字符区间", "文本", True, True),
    Column("核验状态", "枚举", True, False, enum=_REVIEW_ENUM),
    Column("原件哈希", "文本", True, True),
    Column("更新时间", "日期", True, True),
)

_PENDING_COLUMNS = _EVIDENCE_COLUMNS + (
    Column("候选建议", "文本", False, False,
           note="仅校对候选行有值；摘录待复核行留空"),
)

#: 每张表的列契约（顺序即列序）
CONTRACTS: dict[str, tuple[Column, ...]] = {
    "文献": _DOCUMENT_COLUMNS,
    "摘录": _EXCERPT_COLUMNS,
    "证据": _EVIDENCE_COLUMNS,
    "待核": _PENDING_COLUMNS,
}

#: 每张表每行必须有值的列 = 契约里标了「必填」的列（由同一份契约派生，不会与说明表跑偏）。
#: 证据/待核因此必然包含 unit_id + 字符区间 + 原件哈希；缺任何一项该行不导出。
REQUIRED_RULES: dict[str, tuple[str, ...]] = {
    name: tuple(column.name for column in columns if column.required)
    for name, columns in CONTRACTS.items()
}


def schema_contract() -> dict[str, Any]:
    """列契约 JSON：**不含生成时间**，同样的代码永远得到同样的文档。"""
    return {
        "format": "xlsx",
        "contract_version": CONTRACT_VERSION,
        "tool_version": __version__,
        "sheet_order": list(SHEETS),
        "help_sheet": HELP_SHEET,
        "data_sheets": list(DATA_SHEETS),
        "sheets": [
            {
                "name": name,
                "columns": [column.as_dict() for column in CONTRACTS[name]],
                "required_per_row": list(REQUIRED_RULES[name]),
            }
            for name in DATA_SHEETS
        ],
        "skip_policy": (
            "证据/待核行缺 unit_id、字符区间或原件哈希时不导出该行，"
            "跳过条数写进说明表；绝不补默认值。"
        ),
        "date_format": DATE_FORMAT.replace("mm", "MM"),
        "pending_source": (
            "校对候选中 status=pending 的条目，加上核验状态为 "
            + "/".join(REVIEW_STATUSES_PENDING)
            + " 的摘录待复核项"
        ),
    }


# ------------------------------------------------------------------ 数据装配


@dataclass(frozen=True)
class SheetData:
    """一次导出要写进工作簿的全部内容（已脱离数据库）。"""

    workspace_root: str
    generated_at: datetime
    rows: dict[str, tuple[dict[str, Any], ...]]
    skipped: dict[str, int]


def _as_datetime(value: Any) -> datetime | None:
    """UTC ISO 字符串 → 不带时区的 datetime（写出的是真日期，不是文本）。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _now() -> datetime:
    return _as_datetime(utcnow()) or datetime.now(timezone.utc).replace(
        tzinfo=None, microsecond=0)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _page_number(value: Any) -> int | str | None:
    """印刷页：能解析成数字就写真数字，区间/字母原样保留为文本。"""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _span(start: Any, end: Any) -> str:
    """字符区间：``start-end``；两端缺一就返回空串（该行随即不导出）。"""
    if start is None or end is None:
        return ""
    try:
        return f"{int(start)}-{int(end)}"
    except (TypeError, ValueError):
        return ""


def _version_label(version: Any) -> str:
    return "校订" if version == "corrected" else "原文"


def _missing(row: dict[str, Any], sheet: str) -> bool:
    for key in REQUIRED_RULES[sheet]:
        value = row.get(key)
        if value is None or value == "":
            return True
    return False


def _candidates(conn: Any) -> list[dict[str, Any]]:
    """待核的校对候选。

    这里补一条查询而不是复用 ``proofread.list_candidates``：导出需要**全部**
    待核候选（那个函数带 limit，并且另开连接会跳出本次只读事务），
    还需要候选所属单元的书页与 doc_id。用 LEFT JOIN 是为了让「单元已不存在」
    的候选能被数出来，而不是被 JOIN 悄悄丢掉。
    """
    rows = conn.execute(
        "SELECT c.id AS candidate_id, c.unit_id AS unit_id, c.start_offset, c.end_offset, "
        "c.original, c.suggestion, c.reason, c.status, c.created_at, c.updated_at, "
        "u.doc_id AS doc_id, u.file_page, u.printed_page "
        "FROM proofread_candidates c LEFT JOIN units u ON u.id = c.unit_id "
        "WHERE c.status = ? ORDER BY c.created_at, c.rowid",
        (REVIEW_PENDING,),
    ).fetchall()
    return [dict(row) for row in rows]


def collect(workspace: Workspace) -> SheetData:
    """在一个只读事务里把五张表的内容装配好。

    证据行与出处的装配全部复用 ``evidence_exports.select_rows`` /
    ``export.evidence_view``：同一条连接、同一份快照，
    不会出现「行来自旧版本、书页来自新版本」的新旧混排。
    """
    skipped = {name: 0 for name in DATA_SHEETS}
    scope = normalize_scope(review_status=SCOPE_ALL)
    with workspace.connect() as conn:
        conn.execute("BEGIN")
        try:
            # ``list_documents`` 另开连接且带 limit，会跳出本次只读事务并截断；
            # 这里与证据行同一条连接、同一个快照地一次取全。
            documents = [dict(row) for row in conn.execute(
                "SELECT * FROM documents ORDER BY imported_at, id")]
            indexed = {
                row["id"]: search_index.searchable_pages(conn, row["id"])
                for row in documents
            }
            by_id = {row["id"]: row for row in documents}
            repo = evidence_repository(workspace, conn)
            views: list[dict[str, Any]] = []
            for row in select_rows(workspace, scope, conn):
                try:
                    view = evidence_view(repo, row)
                except LitError:
                    # 出处已不存在（单元/文献被删）：该行不导出，计入跳过数。
                    skipped["摘录"] += 1
                    skipped["证据"] += 1
                    continue
                views.append({**view, "updated_at": row["updated_at"]})
            candidates = _candidates(conn)
        finally:
            # 只读事务：立刻结束，写文件阶段不再持有任何锁。
            conn.rollback()

    document_rows: list[dict[str, Any]] = [{
        "doc_id": document["id"],
        "书名": _text(document["title"]),
        "原始文件名": document["filename"],
        "类型": document["filetype"],
        "页数": _optional_int(document["page_count"]),
        "已索引页数": indexed.get(document["id"], 0),
        "状态": document["status"],
        "原件哈希": document["sha256"],
        "更新时间": _as_datetime(document["updated_at"]),
    } for document in documents]

    excerpt_rows: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    for view in views:
        common = {
            "书名": _text(view["title"]),
            "文件页": _optional_int(view["file_page"]),
            "印刷页": _page_number(view["printed_page"]),
            "字符区间": _span(view["start_offset"], view["end_offset"]),
        }
        excerpt = {
            "evidence_id": view["id"],
            "doc_id": view["doc_id"],
            **common,
            "摘录原文": view["quote"],
            "版本": _version_label(view["version"]),
            "核验状态": view["review_status"],
            "创建时间": _as_datetime(view["created_at"]),
        }
        if _missing(excerpt, "摘录"):
            skipped["摘录"] += 1
        else:
            excerpt_rows.append(excerpt)

        evidence = {
            "evidence_id": view["id"],
            "unit_id": view["unit_id"],
            **common,
            "原文切片": view["quote"],
            "核验状态": view["review_status"],
            "原件哈希": view["source_sha256"],
            "更新时间": _as_datetime(view["updated_at"]),
        }
        if _missing(evidence, "证据"):
            skipped["证据"] += 1
        else:
            evidence_rows.append(evidence)
            if view["review_status"] in REVIEW_STATUSES_PENDING:
                review_rows.append({**evidence, "候选建议": ""})

    for candidate in candidates:
        document = by_id.get(candidate["doc_id"] or "")
        row = {
            # 候选行的 ID 列填候选 ID（pf_ 前缀，与证据的 ev_ 前缀可区分）
            "evidence_id": candidate["candidate_id"],
            "unit_id": candidate["unit_id"],
            "书名": _text(document["title"]) if document else "",
            "文件页": _optional_int(candidate["file_page"]),
            "印刷页": _page_number(candidate["printed_page"]),
            "原文切片": candidate["original"],
            "字符区间": _span(candidate["start_offset"], candidate["end_offset"]),
            "核验状态": REVIEW_PENDING,
            "原件哈希": document["sha256"] if document else "",
            "更新时间": _as_datetime(candidate["updated_at"]),
            "候选建议": candidate["suggestion"],
        }
        if _missing(row, "待核"):
            skipped["待核"] += 1
            continue
        review_rows.append(row)

    return SheetData(
        workspace_root=str(workspace.root),
        generated_at=_now(),
        rows={
            "文献": tuple(document_rows),
            "摘录": tuple(excerpt_rows),
            "证据": tuple(evidence_rows),
            "待核": tuple(review_rows),
        },
        skipped=skipped,
    )


# ------------------------------------------------------------------ 渲染


def _openpyxl() -> Any:
    """导入 openpyxl；没装时给中文提示（不抛栈、不影响其它格式）。"""
    try:
        import openpyxl
    except ImportError:
        raise LitError(MISSING_DEPENDENCY) from None
    return openpyxl


def _display_width(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        return 16.0
    widest = 0.0
    for line in str(value).splitlines() or [""]:
        width = 0.0
        for character in line:
            width += 2.0 if unicodedata.east_asian_width(character) in "WF" else 1.0
        widest = max(widest, width)
    return widest


def _cell_text(value: Any) -> str:
    """超过 Excel 单元格上限的文本截断（已知限制，写进说明表与交付报告）。"""
    text = _text(value)
    if len(text) <= CELL_TEXT_LIMIT:
        return text
    return text[:CELL_TEXT_LIMIT - 1] + "…"


def _style_header(sheet: Any, columns: int) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    fill = PatternFill("solid", fgColor="DCE6F1")
    font = Font(bold=True)
    for index in range(1, columns + 1):
        cell = sheet.cell(row=1, column=index)
        cell.font = font
        cell.fill = fill
        cell.alignment = Alignment(vertical="center")


def _widths(sheet: Any, columns: int, rows: int) -> dict[int, float]:
    widths: dict[int, float] = {}
    for index in range(1, columns + 1):
        widest = 0.0
        for row in range(1, rows + 1):
            widest = max(widest, _display_width(sheet.cell(row=row, column=index).value))
        widths[index] = widest
    return widths


def _finish_sheet(sheet: Any, widths: dict[int, float]) -> None:
    """冻结首行 + 自动筛选 + 列宽自适应（上限 60 字符）。"""
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, width in widths.items():
        letter = sheet.cell(row=1, column=index).column_letter
        sheet.column_dimensions[letter].width = min(MAX_COLUMN_WIDTH,
                                                   max(MIN_COLUMN_WIDTH, width))


def _write_data_sheet(workbook: Any, name: str, rows: tuple[dict[str, Any], ...]) -> None:
    columns = CONTRACTS[name]
    sheet = workbook.create_sheet(name)
    sheet.append([column.name for column in columns])
    for row in rows:
        sheet.append([
            _cell_text(row.get(column.name)) if column.type in {"文本", "枚举"}
            else row.get(column.name)
            for column in columns
        ])
    for index, column in enumerate(columns, start=1):
        if column.type != "日期":
            continue
        for row in range(2, len(rows) + 2):
            sheet.cell(row=row, column=index).number_format = DATE_FORMAT
    _style_header(sheet, len(columns))
    _finish_sheet(sheet, _widths(sheet, len(columns), len(rows) + 1))


def _help_lines(data: SheetData) -> list[tuple[str, Any]]:
    """说明表：A 列写列契约，B 列写生成时间/工作区/行数/版本。"""
    lines: list[tuple[str, Any]] = [
        ("本表说明", "A 列：每张表的列契约（列名、类型、是否必填、枚举取值、只读标记）"),
        ("本表说明", "B 列：生成时间、工作区、行数、版本等生成信息"),
        ("本表说明", "只读列由程序生成；按契约填列时不要改动，可写列供人工/Agent 填写"),
        ("本表说明", "「待核」的 evidence_id 列在候选行填候选 ID（pf_ 前缀，与 ev_ 前缀可区分）"),
        ("本表说明", "「待核」先列校对候选，再列摘录待复核项"),
    ]
    for name in DATA_SHEETS:
        for column in CONTRACTS[name]:
            lines.append((column.contract_line(name), None))
    lines.extend([
        ("生成时间", data.generated_at),
        ("工作区", data.workspace_root),
        ("版本", f"litworkbench {__version__}｜列契约 v{CONTRACT_VERSION}"),
        *[(label, len(data.rows[name])) for name, label in ROW_COUNT_LABELS.items()],
        *[(label, data.skipped[name]) for name, label in SKIP_LABELS.items()],
        ("核对提示", "「已接受」只表示摘录已被人工对照原页核验，不代表研究结论已被确认"),
        ("核对提示", "标注为需复核（stale）或待核（pending）的条目，复核前不应直接引用"),
        ("已知限制", f"超过 Excel 单元格上限 {CELL_TEXT_LIMIT} 字符的原文按上限截断"),
    ])
    return lines


def _write_help_sheet(workbook: Any, data: SheetData) -> None:
    sheet = workbook.create_sheet(HELP_SHEET)
    sheet.append(["列契约", "生成信息"])
    for label, value in _help_lines(data):
        sheet.append([label, value])
        if isinstance(value, datetime):
            sheet.cell(row=sheet.max_row, column=2).number_format = DATE_FORMAT
    _style_header(sheet, 2)
    _finish_sheet(sheet, _widths(sheet, 2, sheet.max_row))


def render(data: SheetData) -> bytes:
    """把装配好的内容渲染成 .xlsx 字节。"""
    openpyxl = _openpyxl()
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    _write_help_sheet(workbook, data)
    for name in DATA_SHEETS:
        _write_data_sheet(workbook, name, data.rows[name])
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


# ------------------------------------------------------------------ 落盘


def default_output(workspace: Workspace) -> Path:
    return workspace.artifacts_dir / "xlsx" / "文献工作台导出.xlsx"


def export_xlsx(workspace: Workspace, output: Path | str | None = None) -> dict[str, Any]:
    """导出五张表的工作簿，返回写出的路径、字节数与每表行数。"""
    _openpyxl()          # 先确认依赖：缺 openpyxl 时立刻给中文错误，不做任何写盘
    data = collect(workspace)
    payload = render(data)
    if output is None:
        target = default_output(workspace)
    else:
        target = _resolve_output(output, "文献工作台导出", ".xlsx", {".xlsx"})
        if target is None:  # pragma: no cover - output 非空时必有路径
            raise ValidationError("输出路径不能为空。")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {
        "output": str(target),
        "exists": target.exists(),
        "bytes": len(payload),
        "sheets": {name: len(data.rows[name]) for name in DATA_SHEETS},
        "skipped": dict(data.skipped),
        "generated_at": _text(data.generated_at),
    }


__all__ = [
    "CELL_TEXT_LIMIT",
    "CONTRACTS",
    "CONTRACT_VERSION",
    "Column",
    "DATA_SHEETS",
    "DATE_FORMAT",
    "MISSING_DEPENDENCY",
    "REQUIRED_RULES",
    "ROW_COUNT_LABELS",
    "SHEETS",
    "SKIP_LABELS",
    "SheetData",
    "collect",
    "default_output",
    "export_xlsx",
    "render",
    "schema_contract",
]
