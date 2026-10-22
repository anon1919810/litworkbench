"""模型自校对：让用户配置的模型提出**校订候选**，人工确认后才写入。

这是「候选 -> 采用/保留」流程的第二次落地（第一次是 OCR 重识别候选）。

规则（与 ``evidence`` / ``ocr_candidates`` 一致）：

* 候选**只入库**，绝不直接改 ``units``；只有 :func:``accept_candidate`` 才改文字，
  而且必须走既有 :func:``litworkbench.evidence.correct_unit``（原始识别结果与
  历史一律保留）；
* **防幻觉优先**：模型给的 ``original`` 必须与当前正文 ``[start:end]`` **逐字相等**；
  越界、负偏移、空建议、原文等于建议、区间重叠、缺字段、非 JSON ——
  一律丢弃并计数，绝不猜测修正；
* **密钥安全**：只从 ``config.toml`` 的 ``[provider]`` 段读 ``base_url``/``model``/
  ``api_key_env``，密钥值只来自环境变量；错误信息、数据库字段与命令行输出里
  不出现密钥原文。

偏移语义：``start``/``end`` 是**当前有效文字**（有校订时为校订版）上的字符下标，
左闭右开、按 Unicode 字符计 —— 与 :func:``litworkbench.store._unit_view`` 的
``text`` 字段一致。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from litworkbench.errors import LitError, NotFoundError, ValidationError
from litworkbench.store import Workspace, sha256_text, utcnow

# --------------------------------------------------------------- 常量

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED)

#: 一次生成的最大单元数（避免误操作把整库都发给模型）
MAX_UNITS = 200
#: 单个单元允许发给模型的最大字符数（过长时整体跳过并给出错误码）
MAX_TEXT_CHARS = 20000
#: 单条候选理由/建议的长度上限（防止模型灌入超长文本）
MAX_FIELD_CHARS = 2000

# 错误码：既出现在返回值里，也作为丢弃计数的键
ERROR_EMPTY_RESPONSE = "empty_response"
ERROR_NOT_JSON = "not_json"
ERROR_NOT_ARRAY = "not_array"
ERROR_ITEM_NOT_OBJECT = "item_not_object"
ERROR_MISSING_FIELD = "missing_field"
ERROR_ORIGINAL_MISMATCH = "original_mismatch"
ERROR_OFFSET_OUT_OF_RANGE = "offset_out_of_range"
ERROR_EMPTY_SUGGESTION = "empty_suggestion"
ERROR_NO_CHANGE = "no_change"
ERROR_OVERLAP = "overlap"
ERROR_DUPLICATE = "duplicate"
ERROR_EMPTY_TEXT = "empty_text"
ERROR_TEXT_TOO_LONG = "text_too_long"
ERROR_PROVIDER = "provider_error"

SYSTEM_PROMPT = (
    "你是中文古籍与地方志的校对助手，只做一件事：找出原文中明确的错字、异体字、"
    "明显的识别误字或标点错误，并给出**最小**改写建议。\n"
    "只输出一个 JSON 数组，不要输出任何解释、标题或代码围栏：\n"
    '[{"original": "原文片段", "suggestion": "建议片段", "start": 0, "end": 4, '
    '"reason": "理由"}]\n'
    "硬性要求：\n"
    "1. original 必须是原文中的逐字片段（含标点），一个字都不能改写；\n"
    "2. start/end 是该片段在原文中的字符下标：左闭右开、从 0 开始、按字符计；\n"
    "3. suggestion 只用于替换 original 这一段，不要带上下文；\n"
    "4. 没有把握就不要提候选；没有问题就返回 []；\n"
    "5. 不要提与原文字相同的建议，也不要提互相重叠的片段。"
)

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


# --------------------------------------------------------------- 工具


def new_candidate_id() -> str:
    return f"pf_{uuid.uuid4().hex[:12]}"


@contextmanager
def _write(workspace: Workspace) -> Iterator[sqlite3.Connection]:
    """写事务：先拿写锁，保证「读当前正文 -> 校验 -> 写入」原子。"""
    with workspace.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def _text_of_row(row: sqlite3.Row | None) -> str:
    if row is None:
        return ""
    corrected = row["corrected_text"]
    if corrected is not None:
        return corrected
    return row["original_text"] or ""


def _scrub(workspace: Workspace, message: str) -> str:
    """防御性擦除：万一上游把密钥值带进了文本，也不落库、不上屏。"""
    try:
        from litworkbench.config import provider_settings

        env_name = str(provider_settings(workspace.config).get("api_key_env") or "")
    except Exception:  # pragma: no cover - 配置异常时不影响主流程
        return message
    value = os.environ.get(env_name, "") if env_name else ""
    if value and value in message:
        message = message.replace(value, "[已隐藏]")
    return message


def _clip(value: Any) -> str:
    return str(value or "")[:MAX_FIELD_CHARS]


# --------------------------------------------------------------- 提示词


def build_prompt(text: str, *, unit_id: str = "", locator: str = "") -> list[dict[str, str]]:
    """构造一次校对请求的消息；``text`` 原样给出，不做任何归一化（偏移必须自洽）。"""
    header = ["请校对下面这一段原文，并按约定的 JSON 数组返回候选。"]
    if unit_id:
        header.append(f"单元：{unit_id}")
    if locator:
        header.append(f"出处：{locator}")
    header.append(f"原文共 {len(text)} 个字符。")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(header) + "\n\n--- 原文开始 ---\n" + text
                                     + "\n--- 原文结束 ---"},
    ]


# --------------------------------------------------------------- 解析


def extract_items(raw: str) -> tuple[list[Any] | None, str]:
    """把模型回复解析成列表；返回 ``(列表, 错误码)``，绝不抛异常。

    容忍：markdown 代码围栏、前后多余解释文字、``{"candidates": [...]}`` 包装。
    """
    text = (raw or "").strip()
    if not text:
        return None, ERROR_EMPTY_RESPONSE
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    data: Any
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return None, ERROR_NOT_JSON
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None, ERROR_NOT_JSON
    if isinstance(data, dict):
        inner = data.get("candidates")
        if not isinstance(inner, list):
            return None, ERROR_NOT_ARRAY
        data = inner
    if not isinstance(data, list):
        return None, ERROR_NOT_ARRAY
    return data, ""


def validate_candidates(items: Iterable[Any],
                        text: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """逐条校验；返回 ``(可用候选, 丢弃计数)``。

    重叠策略：**按模型返回顺序先到先得** —— 先出现的候选占住区间，
    之后与之重叠的候选丢弃并计入 ``overlap``。这样同一份回复的解析结果稳定可复现。
    """
    kept: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}

    def drop(code: str) -> None:
        dropped[code] = dropped.get(code, 0) + 1

    for item in items:
        if not isinstance(item, dict):
            drop(ERROR_ITEM_NOT_OBJECT)
            continue
        original = item.get("original")
        suggestion = item.get("suggestion")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(original, str) or not isinstance(suggestion, str):
            drop(ERROR_MISSING_FIELD)
            continue
        # bool 是 int 的子类，必须单独排除
        if isinstance(start, bool) or isinstance(end, bool) \
                or not isinstance(start, int) or not isinstance(end, int):
            drop(ERROR_MISSING_FIELD)
            continue
        # 先判范围：负偏移在切片里会「绕回」到尾部，必须先拦住
        if start < 0 or end > len(text) or start >= end:
            drop(ERROR_OFFSET_OUT_OF_RANGE)
            continue
        if text[start:end] != original:
            drop(ERROR_ORIGINAL_MISMATCH)
            continue
        if not suggestion.strip():
            drop(ERROR_EMPTY_SUGGESTION)
            continue
        if suggestion == original:
            drop(ERROR_NO_CHANGE)
            continue
        if any(start < other["end"] and other["start"] < end for other in kept):
            drop(ERROR_OVERLAP)
            continue
        kept.append({
            "start": start,
            "end": end,
            "original": original,
            "suggestion": suggestion,
            "reason": _clip(item.get("reason")),
        })
    return kept, dropped


# --------------------------------------------------------------- 生成


def _provider(workspace: Workspace):
    """按工作区配置构造 :class:``ChatProvider``（密钥仍只来自环境变量）。"""
    from litworkbench.config import provider_settings
    from litworkbench.provider import ChatProvider

    return ChatProvider(provider_settings(workspace.config))


def resolve_unit_ids(workspace: Workspace, *, unit_id: str | None = None,
                     doc_id: str | None = None, all_units: bool = False) -> list[str]:
    """把命令行给的「单元 / 文献 / 全部」解析成待生成候选的单元列表。"""
    if unit_id:
        workspace.get_unit(unit_id)  # 不存在时抛 NotFoundError
        return [unit_id]
    if doc_id:
        units = workspace.units_for_doc(workspace.find_document(doc_id)["id"])
        return [unit["id"] for unit in units]
    if all_units:
        return [unit["id"] for unit in workspace.list_units(limit=MAX_UNITS)]
    raise ValidationError("请指定一个单元 ID、--doc 文献或 --all；或用 --list/--accept/--reject。")


def store_candidates(workspace: Workspace, unit_id: str, items: Iterable[Any], *,
                     model: str = "") -> dict[str, Any]:
    """把模型给的条目落库：在写事务里重新读正文、重新校验，再逐条插入。

    * 校验用的是**写入那一刻**的正文，读-校验-写之间没有窗口；
    * 同一单元、同一区间、同一建议的 ``pending`` 候选由部分唯一索引兜底，
      重复生成幂等（计入 ``duplicate``，不产生重复待核行）。
    """
    created = 0
    dropped: dict[str, int] = {}
    now = utcnow()
    with _write(workspace) as conn:
        row = conn.execute(
            "SELECT id, original_text, corrected_text FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"找不到原文单元：{unit_id}")
        text = _text_of_row(row)
        kept, dropped = validate_candidates(items, text)
        base_hash = sha256_text(text)
        for item in kept:
            try:
                conn.execute(
                    "INSERT INTO proofread_candidates (id, unit_id, start_offset, end_offset, "
                    "original, suggestion, reason, model, status, base_text_sha256, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_candidate_id(), unit_id, item["start"], item["end"],
                     item["original"], item["suggestion"], item["reason"], _clip(model),
                     STATUS_PENDING, base_hash, now, now),
                )
            except sqlite3.IntegrityError:
                dropped[ERROR_DUPLICATE] = dropped.get(ERROR_DUPLICATE, 0) + 1
                continue
            created += 1
    return {"unit_id": unit_id, "created": created, "dropped": dropped}


def generate_candidates(workspace: Workspace, unit_ids: Sequence[str], *,
                        provider: Any = None, model: str | None = None) -> dict[str, Any]:
    """对若干单元生成候选；每个单元返回一条结果，失败只记错误码，不抛栈。"""
    ordered = list(dict.fromkeys(unit_ids or []))
    if not ordered:
        raise ValidationError("请至少选择一个单元来生成校对候选。")
    if len(ordered) > MAX_UNITS:
        raise ValidationError(f"一次最多 {MAX_UNITS} 个单元（去重后）。")
    provider = provider if provider is not None else _provider(workspace)
    model_name = (model if model is not None else getattr(provider, "model", "")) or ""
    results: list[dict[str, Any]] = []
    for unit_id in ordered:
        unit = workspace.get_unit(unit_id)
        text = unit.get("text") or ""
        if not text.strip():
            results.append({"unit_id": unit_id, "status": "skipped",
                            "created": 0, "dropped": {}, "error": ERROR_EMPTY_TEXT})
            continue
        if len(text) > MAX_TEXT_CHARS:
            results.append({"unit_id": unit_id, "status": "skipped", "created": 0,
                            "dropped": {}, "error": ERROR_TEXT_TOO_LONG})
            continue
        locator = _locator(workspace, unit)
        try:
            raw = provider.complete(build_prompt(text, unit_id=unit_id, locator=locator))
        except LitError as exc:
            results.append({"unit_id": unit_id, "status": "error", "created": 0,
                            "dropped": {}, "error": ERROR_PROVIDER,
                            "error_message": _scrub(workspace, str(exc))})
            continue
        items, error = extract_items(raw)
        if items is None:
            results.append({"unit_id": unit_id, "status": "error", "created": 0,
                            "dropped": {}, "error": error})
            continue
        stored = store_candidates(workspace, unit_id, items, model=model_name)
        results.append({
            "unit_id": unit_id,
            "status": "ok",
            "created": stored["created"],
            "dropped": stored["dropped"],
            "error": "",
        })
    return {
        "provider": {"model": model_name},
        "units": results,
        "created": sum(item["created"] for item in results),
        "failed": sum(1 for item in results if item["status"] == "error"),
    }


def _locator(workspace: Workspace, unit: dict[str, Any]) -> str:
    try:
        document = workspace.get_document(unit["doc_id"])
    except LitError:  # pragma: no cover - 文献被删时不影响生成
        return unit["id"]
    page = (f"PDF 第 {unit['file_page']} 页" if unit.get("kind") == "page"
            else f"单元 {unit['seq']}")
    return f"{document['filename']} · {page}"


# --------------------------------------------------------------- 读取


def _decorate(workspace: Workspace, row: dict[str, Any]) -> dict[str, Any]:
    record = {
        "id": row["id"],
        "unit_id": row["unit_id"],
        "start_offset": row["start_offset"],
        "end_offset": row["end_offset"],
        "original": row["original"],
        "suggestion": row["suggestion"],
        "reason": row["reason"],
        "model": row["model"],
        "status": row["status"],
        "decision_note": row["decision_note"],
        "base_text_sha256": row["base_text_sha256"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "decided_at": row["decided_at"],
    }
    try:
        unit = workspace.get_unit(row["unit_id"])
    except LitError:  # pragma: no cover - 单元被删时仍能列出候选
        return record
    record["locator"] = _locator(workspace, unit)
    current = unit.get("text") or ""
    start, end = row["start_offset"], row["end_offset"]
    # 偏移是否仍然对应当前正文：错位时就地标出，避免用户误用陈旧候选
    record["offsets_match_text"] = (
        0 <= start < end <= len(current) and current[start:end] == row["original"]
    )
    return record


def get_candidate(workspace: Workspace, candidate_id: str) -> dict[str, Any]:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT * FROM proofread_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
    if row is None:
        raise NotFoundError(f"找不到校对候选：{candidate_id}")
    return _decorate(workspace, dict(row))


def list_candidates(workspace: Workspace, *, status: str | None = None,
                    unit_id: str | None = None, doc_id: str | None = None,
                    limit: int = 200) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        if status not in STATUSES:
            raise ValidationError(
                f"未知的候选状态：{status}（可选 {'/'.join(STATUSES)}）"
            )
        clauses.append("c.status = ?")
        params.append(status)
    if unit_id:
        clauses.append("c.unit_id = ?")
        params.append(unit_id)
    if doc_id:
        resolved = workspace.find_document(doc_id)["id"]
        clauses.append("c.unit_id IN (SELECT id FROM units WHERE doc_id = ?)")
        params.append(resolved)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with workspace.connect() as conn:
        rows = conn.execute(
            f"SELECT c.* FROM proofread_candidates c {where} "
            f"ORDER BY c.created_at DESC, c.rowid DESC LIMIT ?",
            params,
        ).fetchall()
    return [_decorate(workspace, dict(row)) for row in rows]


# --------------------------------------------------------------- 决定


def accept_candidate(workspace: Workspace, candidate_id: str, *,
                     reason: str = "") -> dict[str, Any]:
    """采用一条候选：调用既有 ``correct_unit`` 改写校订版，原文与历史保留。

    采用前**重新核对**一次正文切片：生成候选之后正文可能已被校订或重新识别，
    错位的候选一律拒绝（不猜测要改哪里）。
    """
    from litworkbench.evidence import correct_unit

    with _write(workspace) as conn:
        candidate = conn.execute(
            "SELECT * FROM proofread_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            raise NotFoundError(f"找不到校对候选：{candidate_id}")
        if candidate["status"] != STATUS_PENDING:
            raise ValidationError(
                f"候选 {candidate_id} 已处理（{candidate['status']}），不能重复采用。"
            )
        unit_row = conn.execute(
            "SELECT id, original_text, corrected_text FROM units WHERE id = ?",
            (candidate["unit_id"],),
        ).fetchone()
        if unit_row is None:
            raise NotFoundError(f"找不到原文单元：{candidate['unit_id']}")
        text = _text_of_row(unit_row)
        start, end = int(candidate["start_offset"]), int(candidate["end_offset"])
        if not 0 <= start < end <= len(text) or text[start:end] != candidate["original"]:
            raise ValidationError(
                f"候选 {candidate_id} 与当前正文不一致（正文可能已被校订或重新识别），"
                "请重新生成候选。"
            )
        new_text = text[:start] + candidate["suggestion"] + text[end:]
        note = reason.strip() or (
            f"采用校对候选 {candidate_id}：{candidate['reason'] or '模型建议'}"
        )
        unit = correct_unit(workspace, candidate["unit_id"], new_text, note, connection=conn)
        now = utcnow()
        conn.execute(
            "UPDATE proofread_candidates SET status = ?, decision_note = ?, decided_at = ?, "
            "updated_at = ? WHERE id = ?",
            (STATUS_ACCEPTED, _clip(note), now, now, candidate_id),
        )
    return {"candidate": get_candidate(workspace, candidate_id), "unit": unit}


def reject_candidate(workspace: Workspace, candidate_id: str, *,
                     reason: str = "") -> dict[str, Any]:
    """拒绝一条候选：只改候选状态，**不碰正文**。"""
    with _write(workspace) as conn:
        candidate = conn.execute(
            "SELECT status FROM proofread_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            raise NotFoundError(f"找不到校对候选：{candidate_id}")
        # rejected 是终态：已采用（accepted）的候选也不能再标成拒绝，
        # 否则「候选说不用、正文却已经改了」会自相矛盾。
        if candidate["status"] != STATUS_PENDING:
            raise ValidationError(
                f"候选 {candidate_id} 已处理（{candidate['status']}），不能重复拒绝。"
            )
        now = utcnow()
        conn.execute(
            "UPDATE proofread_candidates SET status = ?, decision_note = ?, decided_at = ?, "
            "updated_at = ? WHERE id = ?",
            (STATUS_REJECTED, _clip(reason), now, now, candidate_id),
        )
    return {"candidate": get_candidate(workspace, candidate_id)}


def pending_count(workspace: Workspace, *, unit_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM proofread_candidates WHERE status = ?"
    params: list[Any] = [STATUS_PENDING]
    if unit_id:
        sql += " AND unit_id = ?"
        params.append(unit_id)
    with workspace.connect() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])
