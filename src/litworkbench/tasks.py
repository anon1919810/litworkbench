"""研究任务的持久化：任务、对话、动作、检查点。

每一步都落盘，因此超时、协议错误、步骤用尽或人工中断之后都能恢复，
而不会重复提交已经保存的摘录。任务里保存的是**启动时**的方案与配置快照，
之后修改配置或方案不会悄悄改变历史任务。
"""

from __future__ import annotations

import json
from typing import Any

from litworkbench.errors import NotFoundError
from litworkbench.store import Workspace, utcnow

STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_COMPLETED = "completed"

RESUMABLE = (STATUS_CREATED, STATUS_RUNNING, STATUS_PAUSED, STATUS_FAILED, STATUS_INTERRUPTED)
ALL_STATUSES = (STATUS_CREATED, STATUS_RUNNING, STATUS_PAUSED, STATUS_FAILED,
                STATUS_INTERRUPTED, STATUS_COMPLETED)

_TASK_FIELDS = (
    "id", "question", "profile_name", "profile_snapshot", "config_snapshot",
    "status", "step_count", "max_steps", "report", "error", "created_at", "updated_at",
)


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:  # pragma: no cover - 容错
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def create_task(workspace: Workspace, task_id: str, question: str, profile_name: str,
                profile_snapshot: dict[str, Any], config_snapshot: dict[str, Any],
                max_steps: int) -> dict[str, Any]:
    now = utcnow()
    with workspace.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, question, profile_name, profile_snapshot, "
            "config_snapshot, status, step_count, max_steps, report, error, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, '', ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (task_id, question, profile_name, _dumps(profile_snapshot),
             _dumps(config_snapshot), STATUS_CREATED, int(max_steps), now, now),
        )
    return get_task(workspace, task_id)


def get_task(workspace: Workspace, task_id: str) -> dict[str, Any]:
    with workspace.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"找不到研究任务：{task_id}")
    data = dict(row)
    data["profile_snapshot"] = _loads(data["profile_snapshot"], {})
    data["config_snapshot"] = _loads(data["config_snapshot"], {})
    data["report"] = _loads(data.get("report"), None)
    return data


def find_task(workspace: Workspace, needle: str) -> dict[str, Any]:
    """按 ID 或 ID 前缀查找任务。"""
    with workspace.connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (needle,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM tasks WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1",
                (f"{needle}%",),
            ).fetchone()
    if row is None:
        raise NotFoundError(f"找不到研究任务：{needle}")
    return get_task(workspace, row["id"])


def list_tasks(workspace: Workspace, status: str | None = None,
               limit: int = 20) -> list[dict[str, Any]]:
    if status and status not in ALL_STATUSES:
        raise NotFoundError(
            f"未知的任务状态：{status}（可选 {'/'.join(ALL_STATUSES)}）"
        )
    sql = (
        "SELECT id, question, profile_name, status, step_count, max_steps, error, "
        "created_at, updated_at, report FROM tasks"
    )
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id LIMIT ?"
    params.append(int(limit))
    with workspace.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    tasks = []
    for row in rows:
        data = dict(row)
        data["has_report"] = bool(data.pop("report", None))
        tasks.append(data)
    return tasks


def update_task(workspace: Workspace, task_id: str, **fields: Any) -> dict[str, Any]:
    allowed = {"status", "step_count", "max_steps", "report", "error", "question"}
    unknown = set(fields) - allowed
    if unknown:
        raise NotFoundError(f"不可修改的任务字段：{'、'.join(sorted(unknown))}")
    if not fields:
        return get_task(workspace, task_id)
    if "report" in fields and not isinstance(fields["report"], (str, type(None))):
        fields["report"] = _dumps(fields["report"])
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with workspace.connect() as conn:
        conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", [*fields.values(), task_id]
        )
    return get_task(workspace, task_id)


def save_task_report(workspace: Workspace, task_id: str, report: dict[str, Any],
                     status: str | None = None) -> dict[str, Any]:
    payload = dict(report)
    payload.setdefault("task_id", task_id)
    updates: dict[str, Any] = {"report": payload}
    if status:
        updates["status"] = status
    return update_task(workspace, task_id, **updates)


def append_message(workspace: Workspace, task_id: str, role: str, content: str) -> int:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM task_messages WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        seq = int(row["seq"]) + 1
        conn.execute(
            "INSERT INTO task_messages (task_id, seq, role, content, at) VALUES (?, ?, ?, ?, ?)",
            (task_id, seq, role, content, utcnow()),
        )
    return seq


def task_messages(workspace: Workspace, task_id: str) -> list[dict[str, Any]]:
    with workspace.connect() as conn:
        rows = conn.execute(
            "SELECT seq, role, content, at FROM task_messages WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def append_action(workspace: Workspace, task_id: str, step: int, tool: str,
                  args: dict[str, Any], result: Any = None, status: str = "ok",
                  error: str = "") -> int:
    with workspace.connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM task_actions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        seq = int(row["seq"]) + 1
        conn.execute(
            "INSERT INTO task_actions (task_id, seq, step, tool, args, result, status, "
            "error, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, seq, int(step), tool, _dumps(args),
             _dumps(result) if result is not None else None, status, error, utcnow()),
        )
    return seq


def task_actions(workspace: Workspace, task_id: str) -> list[dict[str, Any]]:
    with workspace.connect() as conn:
        rows = conn.execute(
            "SELECT seq, step, tool, args, result, status, error, at FROM task_actions "
            "WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()
    actions = []
    for row in rows:
        data = dict(row)
        data["args"] = _loads(data["args"], {})
        data["result"] = _loads(data["result"], None)
        actions.append(data)
    return actions


def show_task(workspace: Workspace, task_id: str) -> dict[str, Any]:
    """CLI ``lit tasks <id>`` 用的任务详情。"""
    from litworkbench.evidence import list_evidence

    task = find_task(workspace, task_id)
    evidence = list_evidence(workspace, task_id=task["id"], limit=1000)
    return {
        "id": task["id"],
        "question": task["question"],
        "status": task["status"],
        "profile": task["profile_name"],
        "steps": task["step_count"],
        "max_steps": task["max_steps"],
        "error": task["error"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "report": task["report"],
        "actions": task_actions(workspace, task["id"]),
        "messages": task_messages(workspace, task["id"]),
        "evidence": [
            {
                "id": item["id"],
                "quote": item["quote"],
                "locator": item["locator"],
                "category": item["category"],
                "review_status": item["review_status"],
            }
            for item in evidence
        ],
        "config_snapshot": task["config_snapshot"],
    }