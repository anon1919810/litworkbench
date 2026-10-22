"""报告与证据导出。

原则：

* 出处信息一律来自数据库里的权威记录（文献元数据 + 单元定位 + 原件哈希），
  **不使用模型自编的出处**；
* 只统计属于本任务的摘录；引用到其他任务/不存在的证据会被显式标红，
  并且不会被算作「已核验」；
* ``evidence_verified`` 只表示**摘录**是否被人工接受，
  研究结论本身永远待人工确认，报告里必须写清楚这一点；
* Markdown 用自己的转义，DOCX 交给 python-docx 转义 XML，CSV 防公式注入。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from litworkbench.errors import LitError
from litworkbench.evidence import REVIEW_ACCEPTED, REVIEW_REJECTED, REVIEW_STALE, locator
from litworkbench.store import Workspace, utcnow
from litworkbench.tasks import get_task

FORMATS = ("markdown", "json", "docx")
EXTENSIONS = {"markdown": ".md", "json": ".json", "docx": ".docx"}
PAGE_SIZE = 200

DISCLAIMER = (
    "本报告由本地文献工作台根据模型的结构化结论与原文摘录生成。"
    "所有引文都可以回到原文单元核验，但研究结论**始终需要人工确认**；"
    "标注为 stale 或 rejected 的摘录在复核之前不得引用。"
)
CONCLUSION_NOTE = (
    "「证据核验」只表示摘录本身是否被人工接受，并不表示研究结论正确；"
    "结论仍需人工对照原文确认。"
)
_UNVERIFIABLE = (REVIEW_REJECTED, REVIEW_STALE)


# ------------------------------------------------------------- 权威数据读取


class _Repository:
    """带缓存的只读访问：文献元数据、单元定位、证据分页。

    ``connection`` 让网页资料导出在**一个**读事务里展开全部出处，
    保证书目、页码与引文来自同一份快照。
    """

    def __init__(self, workspace: Workspace, connection: sqlite3.Connection | None = None) -> None:
        self.workspace = workspace
        self.connection = connection
        self._documents: dict[str, dict[str, Any]] = {}
        self._units: dict[str, dict[str, Any]] = {}

    def document(self, doc_id: str) -> dict[str, Any]:
        if doc_id not in self._documents:
            self._documents[doc_id] = self.workspace.get_document(
                doc_id, connection=self.connection)
        return self._documents[doc_id]

    def unit(self, unit_id: str) -> dict[str, Any]:
        if unit_id not in self._units:
            self._units[unit_id] = self.workspace.get_unit(
                unit_id, connection=self.connection)
        return self._units[unit_id]

    def evidence_page(self, offset: int, limit: int, *, task_id: str | None = None,
                      doc_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if task_id:
            clauses.append("e.task_id = ?")
            params.append(task_id)
        if doc_id:
            clauses.append("e.doc_id = ?")
            params.append(doc_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        with self.workspace.connect() as conn:
            rows = conn.execute(
                f"SELECT e.* FROM evidence e {where} "
                "ORDER BY e.created_at, e.id LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def all_evidence(self, *, task_id: str | None = None,
                     doc_id: str | None = None) -> list[dict[str, Any]]:
        """分页取全量证据，不做静默截断。"""
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = self.evidence_page(offset, PAGE_SIZE, task_id=task_id, doc_id=doc_id)
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return items


def position_label(unit: dict[str, Any]) -> str:
    """人类可读定位：PDF 说页，DOCX 说段/表，不伪造页码。"""
    kind = unit.get("kind")
    if kind == "page":
        base = f"PDF 第 {unit.get('file_page')} 页"
    elif kind == "table":
        base = f"DOCX 文本单元 {unit.get('seq')}（表格）"
    else:
        base = f"DOCX 文本单元 {unit.get('seq')}（段落）"
    if unit.get("printed_page"):
        base += f"（印刷页码 {unit['printed_page']}）"
    return base


def evidence_view(repo: _Repository, evidence: dict[str, Any],
                  task_id: str | None = None) -> dict[str, Any]:
    """把一条证据展开成「完整出处」。"""
    document = repo.document(evidence["doc_id"])
    unit = repo.unit(evidence["unit_id"])
    snapshot = _loads(evidence.get("version_snapshot"))
    try:
        location = locator(repo.workspace, unit["id"], connection=repo.connection)
    except LitError:  # pragma: no cover - 防御性
        location = unit["id"]
    belongs = task_id is None or evidence.get("task_id") == task_id
    return {
        "id": evidence["id"],
        "task_id": evidence.get("task_id"),
        "belongs_to_task": belongs,
        "quote": evidence["quote"],
        "start_offset": evidence["start_offset"],
        "end_offset": evidence["end_offset"],
        "version": evidence["version"],
        "version_snapshot": snapshot,
        "unit_id": unit["id"],
        "unit_seq": unit["seq"],
        "doc_id": document["id"],
        "filename": document["filename"],
        "title": document["title"],
        "author": document["author"],
        "year": document["year"],
        "edition": document["edition"],
        "volume": document["volume"],
        "source_sha256": document["sha256"],
        "source_path": document["original_path"],
        "kind": unit["kind"],
        "file_page": unit["file_page"],
        "printed_page": unit["printed_page"],
        "printed_page_status": unit.get("printed_page_status") or "none",
        "title_path": unit["title_path"],
        "position": position_label(unit),
        "locator": location,
        "method": unit["method"],
        "unit_status": unit["status"],
        "category": evidence["category"],
        "note": evidence["note"],
        "review_status": evidence["review_status"],
        "review_note": evidence.get("review_note", ""),
        "created_at": evidence["created_at"],
        "usable": belongs and evidence["review_status"] not in _UNVERIFIABLE,
    }


def evidence_repository(workspace: Workspace,
                        connection: sqlite3.Connection | None = None) -> _Repository:
    """公开的只读数据访问，供网页资料导出复用同一套出处展开逻辑。"""
    return _Repository(workspace, connection)


def evidence_views(workspace: Workspace, rows: list[dict[str, Any]], *,
                   task_id: str | None = None,
                   connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """批量展开证据出处（引用时逐条走同一份权威读取）。"""
    repo = _Repository(workspace, connection)
    return [evidence_view(repo, row, task_id) for row in rows]


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):  # pragma: no cover - 容错
        return {}


# ----------------------------------------------------------------- 报告组装


def build_report(workspace: Workspace, task: dict[str, Any]) -> dict[str, Any]:
    """合并任务报告与数据库证据；只采信属于本任务、未被拒绝的证据。"""
    report = task.get("report")
    if not report:
        raise LitError(
            f"任务 {task['id']} 还没有报告（状态：{task['status']}）；"
            "步骤用尽或中断的任务不会生成报告。"
        )
    repo = _Repository(workspace)
    findings: list[dict[str, Any]] = []
    used: dict[str, dict[str, Any]] = {}
    sections: list[str] = []

    for item in report.get("findings") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        section = str(item.get("section") or "主要发现")
        if section not in sections:
            sections.append(section)
        resolved: list[dict[str, Any]] = []
        for raw_id in item.get("evidence_ids") or []:
            evidence_id = str(raw_id)
            stored = _find_evidence(repo, evidence_id)
            if stored is None:
                resolved.append({"id": evidence_id, "missing": True, "usable": False})
                continue
            view = evidence_view(repo, stored, task["id"])
            if view["belongs_to_task"]:
                used[evidence_id] = view
            resolved.append(view if view["belongs_to_task"] else {
                **view, "missing": True, "foreign": True,
            })
        supported = bool(resolved) and all(
            not item.get("missing") and item.get("usable") for item in resolved
        )
        findings.append({
            "text": text,
            "section": section,
            "evidence": resolved,
            "supported": supported,
        })

    evidence_views = list(used.values())
    evidence_verified = bool(evidence_views) and all(f["supported"] for f in findings) and all(
        item["review_status"] == REVIEW_ACCEPTED for item in evidence_views
    )
    return {
        "task_id": task["id"],
        "title": str(report.get("title") or "研究结果"),
        "question": task["question"],
        "profile": task["profile_name"],
        "status": task["status"],
        "steps": task["step_count"],
        "max_steps": task["max_steps"],
        "created_at": task["created_at"],
        "generated_at": utcnow(),
        "sections": sections,
        "findings": findings,
        "evidence": evidence_views,
        "open_questions": [str(q) for q in (report.get("open_questions") or [])],
        # 摘录被人工接受 ≠ 结论已核实
        "evidence_verified": evidence_verified,
        "conclusions_verified": False,
        "conclusion_note": CONCLUSION_NOTE,
        "disclaimer": DISCLAIMER,
    }


def _find_evidence(repo: _Repository, evidence_id: str) -> dict[str, Any] | None:
    with repo.workspace.connect() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------- 导出


def _resolve_output(output: Path | str | None, default_name: str, extension: str,
                    allowed: set[str]) -> Path:
    if output is None:
        return None  # 由调用方决定默认目录
    target = Path(output).expanduser().resolve()
    if target.suffix and target.suffix.lower() not in allowed:
        raise LitError(
            f"输出扩展名 {target.suffix} 与所选格式不符；"
            f"请使用 {'/'.join(sorted(allowed))}，或去掉扩展名由程序补全。"
        )
    if not target.suffix:
        target = target.with_suffix(extension)
    return target


def export_report(workspace: Workspace, task_id: str, output: Path | str | None = None,
                  format: str = "markdown") -> Path:
    if format not in FORMATS:
        raise LitError(f"不支持的报告格式：{format}（可选 {'/'.join(FORMATS)}）")
    task = get_task(workspace, task_id)
    payload = build_report(workspace, task)
    target = _resolve_output(output, task_id, EXTENSIONS[format], {EXTENSIONS[format]})
    if target is None:
        target = workspace.artifacts_dir / "reports" / f"{task['id']}{EXTENSIONS[format]}"
    target.parent.mkdir(parents=True, exist_ok=True)

    if format == "markdown":
        target.write_text(_markdown(payload), encoding="utf-8")
    elif format == "json":
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        _write_docx(payload, target)
    return target


def _md(text: Any) -> str:
    """Markdown 转义。

    刻意**不转义下划线**：文件名、单元 ID 等定位信息在导出结果里保持原样，
    便于人工照着定位串回到原件。
    """
    value = str(text if text is not None else "")
    escaped = value.replace("\\", "\\\\")
    for char in ("`", "*", "[", "]", "|", "<", ">"):
        escaped = escaped.replace(char, "\\" + char)
    if escaped[:1] in {"#", ">", "-", "+", "="}:
        escaped = "\\" + escaped
    return escaped


def _bibliography(item: dict[str, Any]) -> list[str]:
    parts = [f"文件：{_md(item['filename'])}"]
    if item["title"]:
        parts.append(f"题名：{_md(item['title'])}")
    if item["author"]:
        parts.append(f"作者/编者：{_md(item['author'])}")
    if item["year"]:
        parts.append(f"年份：{_md(item['year'])}")
    if item["edition"]:
        parts.append(f"版本：{_md(item['edition'])}")
    if item["volume"]:
        parts.append(f"卷次：{_md(item['volume'])}")
    parts.append(f"原件 SHA-256：{_md(item['source_sha256'])}")
    return parts


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# {_md(payload['title'])}",
        "",
        f"- 问题：{_md(payload['question'])}",
        f"- 研究方案：{_md(payload['profile'])}",
        f"- 任务：{payload['task_id']}（状态：{payload['status']}，"
        f"步骤 {payload['steps']}/{payload['max_steps']}）",
        f"- 生成时间：{payload['generated_at']}",
        f"- 证据核验：{'摘录均已被人工接受' if payload['evidence_verified'] else '存在未核验/被拒绝/待复核的摘录'}",
        "- 研究结论核验：未核验（结论始终需要人工确认）",
        "",
        f"> {_md(payload['disclaimer'])}",
        "",
        f"> {_md(payload['conclusion_note'])}",
        "",
    ]
    if payload["findings"]:
        for section in payload["sections"] or ["主要发现"]:
            lines.append(f"## {_md(section)}")
            lines.append("")
            for finding in payload["findings"]:
                if finding["section"] != section:
                    continue
                mark = "" if finding["supported"] else "（⚠ 证据不足，见下）"
                lines.append(f"- {_md(finding['text'])}{mark}")
                if not finding["evidence"]:
                    lines.append("  - ⚠ 该结论没有可核验的摘录")
                for evidence in finding["evidence"]:
                    lines.extend(_markdown_evidence(evidence))
            lines.append("")
    else:
        lines.extend([
            "## 未找到可引用的结论",
            "",
            "本次检索没有找到可以逐字引用的原文，请在「待解决问题」中记录原因。",
            "",
        ])

    lines.append("## 待解决问题")
    lines.append("")
    lines.extend(f"- {_md(question)}" for question in (payload["open_questions"] or ["（无）"]))
    lines.extend(["", "## 证据清单", ""])
    if payload["evidence"]:
        for evidence in payload["evidence"]:
            lines.extend(_markdown_evidence(evidence))
    else:
        lines.append("- （无）")
    lines.append("")
    return "\n".join(lines)


def _markdown_evidence(evidence: dict[str, Any]) -> list[str]:
    if evidence.get("missing"):
        reason = "不属于本任务" if evidence.get("foreign") else "不存在"
        return [f"  - ⚠ 证据 `{evidence['id']}` {reason}，未用于本结论"]
    lines = [f"  - 证据 `{evidence['id']}`｜{_md(evidence['filename'])} · "
             f"{_md(evidence['position'])}"]
    lines.append(f"    - 引文：{_md(evidence['quote'])}")
    version_label = "校订" if evidence["version"] == "corrected" else "原始识别"
    status = evidence["review_status"]
    lines.append(
        f"    - 分类：{_md(evidence['category'] or '未分类')}｜版本：{version_label}"
        f"（{_md(evidence['method'])}）｜核验状态：{status}"
    )
    lines.append(f"    - 单元：{_md(evidence['unit_id'])}"
                 f"｜字符偏移：{evidence['start_offset']}-{evidence['end_offset']}")
    lines.append("    - 定位串：" + _md(evidence["locator"]))
    lines.extend(f"    - {_part}" for _part in _bibliography(evidence))
    if status in _UNVERIFIABLE:
        lines.append(f"    - ⚠ 该摘录当前状态为 {status}，复核前不应引用")
    return lines


def _write_docx(payload: dict[str, Any], target: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading(str(payload["title"]), level=0)
    document.add_paragraph(f"问题：{payload['question']}")
    document.add_paragraph(
        f"研究方案：{payload['profile']}｜任务：{payload['task_id']}"
        f"｜状态：{payload['status']}"
    )
    document.add_paragraph(f"生成时间：{payload['generated_at']}")
    document.add_paragraph(
        "证据核验："
        + ("摘录均已被人工接受" if payload["evidence_verified"]
           else "存在未核验/被拒绝/待复核的摘录")
    )
    document.add_paragraph("研究结论核验：未核验（结论始终需要人工确认）")
    document.add_paragraph(str(payload["disclaimer"]))
    document.add_paragraph(str(payload["conclusion_note"]))

    if payload["findings"]:
        for section in payload["sections"] or ["主要发现"]:
            document.add_heading(str(section), level=1)
            for finding in payload["findings"]:
                if finding["section"] != section:
                    continue
                document.add_paragraph(str(finding["text"]), style="List Bullet")
                if not finding["evidence"]:
                    document.add_paragraph("⚠ 该结论没有可核验的摘录", style="List Bullet 2")
                for evidence in finding["evidence"]:
                    if evidence.get("missing"):
                        reason = "不属于本任务" if evidence.get("foreign") else "不存在"
                        document.add_paragraph(
                            f"⚠ 证据 {evidence['id']} {reason}，未用于本结论",
                            style="List Bullet 2",
                        )
                        continue
                    document.add_paragraph(
                        f"证据 {evidence['id']}：{evidence['filename']} · {evidence['position']}",
                        style="List Bullet 2",
                    )
                    document.add_paragraph(f"引文：{evidence['quote']}", style="List Bullet 3")
                    document.add_paragraph(
                        f"分类：{evidence['category'] or '未分类'}｜版本："
                        f"{'校订' if evidence['version'] == 'corrected' else '原始识别'}"
                        f"｜核验状态：{evidence['review_status']}",
                        style="List Bullet 3",
                    )
                    document.add_paragraph(
                        f"单元：{evidence['unit_id']}｜印刷页码："
                        f"{evidence['printed_page'] or '（未维护）'}｜"
                        f"字符偏移：{evidence['start_offset']}-{evidence['end_offset']}",
                        style="List Bullet 3",
                    )
                    for part in _bibliography(evidence):
                        document.add_paragraph(part, style="List Bullet 3")
    else:
        document.add_heading("未找到可引用的结论", level=1)
        document.add_paragraph("本次检索没有找到可以逐字引用的原文。")

    document.add_heading("待解决问题", level=1)
    if payload["open_questions"]:
        for question in payload["open_questions"]:
            document.add_paragraph(str(question), style="List Bullet")
    else:
        document.add_paragraph("（无）")

    document.add_heading("证据清单", level=1)
    if payload["evidence"]:
        for evidence in payload["evidence"]:
            document.add_paragraph(
                f"{evidence['id']}｜{evidence['filename']} · {evidence['position']}"
                f"｜核验状态：{evidence['review_status']}"
            )
            document.add_paragraph(f"引文：{evidence['quote']}")
            document.add_paragraph(
                f"单元：{evidence['unit_id']}｜印刷页码："
                f"{evidence['printed_page'] or '（未维护）'}"
            )
    else:
        document.add_paragraph("（无）")
    document.save(str(target))


# ----------------------------------------------------------------- 证据导出


EVIDENCE_COLUMNS = [
    "evidence_id", "task_id", "unit_id", "unit_seq", "filename", "title", "author",
    "year", "edition", "volume", "source_sha256", "kind", "file_page", "printed_page",
    "position", "version", "method", "start_offset", "end_offset", "quote", "category",
    "note", "review_status", "review_note", "created_at", "locator",
]


def export_evidence(workspace: Workspace, output: Path | str | None = None,
                    format: str = "csv", task_id: str | None = None,
                    doc: str | None = None) -> Path:
    if format not in {"csv", "json"}:
        raise LitError(f"不支持的证据导出格式：{format}（可选 csv/json）")
    repo = _Repository(workspace)
    doc_id = workspace.find_document(doc)["id"] if doc else None
    items = [evidence_view(repo, row, task_id) for row in
             repo.all_evidence(task_id=task_id, doc_id=doc_id)]

    target = _resolve_output(output, "evidence", f".{format}", {f".{format}"})
    if target is None:
        suffix = task_id or (doc_id or "all")
        target = workspace.artifacts_dir / "evidence" / f"evidence_{suffix}.{format}"
    target.parent.mkdir(parents=True, exist_ok=True)

    if format == "json":
        payload = {
            "generated_at": utcnow(),
            "task_id": task_id,
            "doc_id": doc_id,
            "count": len(items),
            "evidence": items,
            "disclaimer": DISCLAIMER,
            "conclusion_note": CONCLUSION_NOTE,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        return target

    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(EVIDENCE_COLUMNS)
        for item in items:
            row = {
                "evidence_id": item["id"],
                "task_id": item["task_id"] or "",
                "unit_id": item["unit_id"],
                "unit_seq": item["unit_seq"],
                "filename": item["filename"],
                "title": item["title"],
                "author": item["author"],
                "year": item["year"],
                "edition": item["edition"],
                "volume": item["volume"],
                "source_sha256": item["source_sha256"],
                "kind": item["kind"],
                "file_page": "" if item["file_page"] is None else item["file_page"],
                "printed_page": item["printed_page"],
                "position": item["position"],
                "version": item["version"],
                "method": item["method"],
                "start_offset": item["start_offset"],
                "end_offset": item["end_offset"],
                "quote": item["quote"],
                "category": item["category"],
                "note": item["note"],
                "review_status": item["review_status"],
                "review_note": item["review_note"],
                "created_at": item["created_at"],
                "locator": item["locator"],
            }
            writer.writerow([_csv_safe(row[column]) for column in EVIDENCE_COLUMNS])
    return target


def _csv_safe(value: Any) -> Any:
    """防止 Excel/WPS 把以 = + - @ 开头的文本当公式执行。"""
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


#: 公开别名：网页资料导出复用同一套 CSV 防公式转义，不另写一份。
csv_safe = _csv_safe
#: 公开别名：Markdown 转义规则与报告导出完全一致（刻意不转义下划线）。
md_escape = _md
