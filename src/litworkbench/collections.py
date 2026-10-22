"""摘录集合（专题）与批量整理。

规则：

* 集合是**多对多**的标签式归类：一个摘录可以同时属于多个集合，
  同一对关系重复加入是幂等的；
* 集合只有「新建 / 改名 / 改说明」，**没有任何永久删除接口**；
  移出集合只删成员关系，摘录本身、它的校订历史与核验状态一律不动；
* 批量分类与批量成员变更在**一个写事务**里完成：先校验全部显式 ID，
  任何未知 ID 都会让整批回滚，绝不出现「一半成功」；
* 批量操作**不提供**批量接受核验：核验是逐条人工对照原页的判断。
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from typing import Any

from litworkbench.errors import NotFoundError, ValidationError
from litworkbench.store import Workspace, ensure_write_transaction, utcnow

#: 一次请求允许操作的最大显式摘录 ID 数（列表筛选的全量导出不受此限制）
MAX_BATCH_IDS = 500

NAME_MAX = 80
DESCRIPTION_MAX = 1000
CATEGORY_MAX = 100


# ------------------------------------------------------------------ 名称校验


def clean_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise ValidationError("集合名称不能为空。")
    if len(name) > NAME_MAX:
        raise ValidationError(f"集合名称最多 {NAME_MAX} 个字符（当前 {len(name)}）。")
    if any(ord(ch) < 32 for ch in name):
        raise ValidationError("集合名称不能包含控制字符。")
    return name


def clean_description(raw: str | None) -> str:
    text = raw or ""
    if len(text) > DESCRIPTION_MAX:
        raise ValidationError(f"集合说明最多 {DESCRIPTION_MAX} 个字符（当前 {len(text)}）。")
    return text


def clean_category(raw: str | None) -> str:
    """分类允许空串（清除分类），但不允许超长或控制字符。"""
    text = (raw or "").strip()
    if len(text) > CATEGORY_MAX:
        raise ValidationError(f"分类最多 {CATEGORY_MAX} 个字符（当前 {len(text)}）。")
    return text


def normalize_ids(raw: list[str] | None) -> list[str]:
    """规范化显式 ID：按**首次出现**去重，保留顺序。

    三条硬规则：

    * 显式传了空数组 → 拒绝。绝不把 ``[]`` 当成「未指定」而回退到全部结果；
    * 上限按**原始条目数**判断：501 个条目即使去重后只剩 1 条也照样拒绝，
      避免用重复 ID 绕过「一次最多 500 条」的约定；
    * 非字符串、空串同样拒绝。
    """
    if raw is None:
        raise ValidationError("请至少选择一条摘录。")
    if not isinstance(raw, list):
        raise ValidationError("evidence_ids 必须是数组。")
    if len(raw) > MAX_BATCH_IDS:
        raise ValidationError(
            f"一次最多处理 {MAX_BATCH_IDS} 条摘录（当前提交 {len(raw)} 条）；请分批操作。"
        )
    if not raw:
        raise ValidationError("已选 0 条：请显式选择要处理的摘录。")
    seen: set[str] = set()
    ids: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("evidence_ids 只能包含非空的摘录 ID。")
        key = value.strip()
        if key in seen:
            continue
        seen.add(key)
        ids.append(key)
    return ids


# ------------------------------------------------------------------ 集合读写


def _row_to_collection(conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data.pop("name_key", None)      # 内部唯一性口径，不出现在接口里
    count = int(conn.execute(
        "SELECT COUNT(*) AS n FROM collection_evidence WHERE collection_id = ?",
        (data["id"],),
    ).fetchone()["n"])
    data["evidence_count"] = count
    return data


def list_collections(workspace: Workspace) -> list[dict[str, Any]]:
    with workspace.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM collections ORDER BY created_at DESC, id"
        ).fetchall()
        return [_row_to_collection(conn, row) for row in rows]


def get_collection(workspace: Workspace, collection_id: str,
                   *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    with (nullcontext(connection) if connection is not None else workspace.connect()) as conn:
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"找不到集合：{collection_id}")
        return _row_to_collection(conn, row)


def create_collection(workspace: Workspace, name: str, description: str = "") -> dict[str, Any]:
    clean = clean_name(name)
    desc = clean_description(description)
    now = utcnow()
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        _require_free_name(conn, clean, None)
        collection_id = _new_id()
        conn.execute(
            "INSERT INTO collections (id, name, name_key, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (collection_id, clean, clean.casefold(), desc, now, now),
        )
        return _row_to_collection(conn, conn.execute(
            "SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone())


def update_collection(workspace: Workspace, collection_id: str, *, name: str | None = None,
                      description: str | None = None) -> dict[str, Any]:
    """改名 / 改说明。没有删除分支：集合一旦建立就长期保留。"""
    changes: dict[str, str] = {}
    if name is not None:
        changes["name"] = clean_name(name)
    if description is not None:
        changes["description"] = clean_description(description)
    if not changes:
        raise ValidationError("没有可修改的字段（name / description）。")
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        get_collection(workspace, collection_id, connection=conn)
        if "name" in changes:
            _require_free_name(conn, changes["name"], collection_id)
            changes["name_key"] = changes["name"].casefold()
        changes["updated_at"] = utcnow()
        conn.execute(
            "UPDATE collections SET " + ", ".join(f"{key} = ?" for key in changes)
            + " WHERE id = ?",
            [*changes.values(), collection_id],
        )
        return _row_to_collection(conn, conn.execute(
            "SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone())


def _require_free_name(conn: sqlite3.Connection, name: str, keep_id: str | None) -> None:
    row = conn.execute(
        "SELECT id FROM collections WHERE name_key = ?", (name.casefold(),)
    ).fetchone()
    if row is not None and row["id"] != keep_id:
        raise ValidationError(f"已存在同名集合「{name}」，请换一个名称。")


def _new_id() -> str:
    import uuid

    return f"col_{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ 成员关系


def collection_ids_for(evidence_id: str, *, connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT collection_id FROM collection_evidence WHERE evidence_id = ? ORDER BY collection_id",
        (evidence_id,),
    ).fetchall()
    return [row["collection_id"] for row in rows]


def add_evidence(workspace: Workspace, collection_id: str, evidence_ids: list[str]) -> int:
    """批量加入集合。重复加入幂等，返回真正新增的关系条数。"""
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        get_collection(workspace, collection_id, connection=conn)
        return _insert_members(conn, collection_id, evidence_ids)


def remove_evidence(workspace: Workspace, collection_id: str, evidence_ids: list[str]) -> int:
    """移出集合只删除成员关系；摘录本身与其校订、核验状态不受影响。"""
    with workspace.connect() as conn:
        ensure_write_transaction(conn)
        get_collection(workspace, collection_id, connection=conn)
        removed = 0
        for evidence_id in evidence_ids:
            removed += conn.execute(
                "DELETE FROM collection_evidence WHERE collection_id = ? AND evidence_id = ?",
                (collection_id, evidence_id),
            ).rowcount
        return removed


def _insert_members(conn: sqlite3.Connection, collection_id: str,
                    evidence_ids: list[str]) -> int:
    now = utcnow()
    added = 0
    for evidence_id in evidence_ids:
        added += conn.execute(
            "INSERT INTO collection_evidence (collection_id, evidence_id, added_at) "
            "VALUES (?, ?, ?) ON CONFLICT(collection_id, evidence_id) DO NOTHING",
            (collection_id, evidence_id, now),
        ).rowcount
    return added


# ------------------------------------------------------------------ 批量整理


#: 批量请求的规范化结果
class BatchPlan(dict[str, Any]):
    """``plan_batch`` 的返回值：字段名与请求体一致，便于接口直接回显。"""


def plan_batch(*, evidence_ids: list[str] | None, category: str | None = None,
               add_to_collection_id: str | None = None,
               remove_from_collection_id: str | None = None) -> BatchPlan:
    """校验请求形状；任何一项不合法都在写库之前就拒绝。"""
    ids = normalize_ids(evidence_ids)
    if category is None and not add_to_collection_id and not remove_from_collection_id:
        raise ValidationError(
            "请至少选择一种操作：设置分类、加入集合或移出集合。"
        )
    if add_to_collection_id and remove_from_collection_id and \
            add_to_collection_id == remove_from_collection_id:
        raise ValidationError("加入与移出不能是同一个集合。")
    clean_cat = clean_category(category) if category is not None else None
    return BatchPlan({
        "evidence_ids": ids,
        "category": clean_cat,
        "add_to_collection_id": add_to_collection_id or None,
        "remove_from_collection_id": remove_from_collection_id or None,
    })


def apply_batch(workspace: Workspace, plan: BatchPlan,
                *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    """在**一个**写事务里完成批量分类与成员变更。

    校验顺序刻意固定：先确认所有 ID 存在，再动一行数据。
    任何未知 ID 都在写之前抛出，因此不会出现「前 10 条已改、第 11 条失败」。
    """
    owned = connection is None
    del owned  # 仅文档用途：自己开连接时由 workspace.connect() 负责提交
    ids = plan["evidence_ids"]
    category = plan["category"]
    add_id = plan["add_to_collection_id"]
    remove_id = plan["remove_from_collection_id"]

    def _run(conn: sqlite3.Connection) -> dict[str, Any]:
        ensure_write_transaction(conn)
        # 集合必须先存在（未知集合同样整批拒绝）。
        if add_id:
            get_collection(workspace, add_id, connection=conn)
        if remove_id:
            get_collection(workspace, remove_id, connection=conn)
        placeholders = ", ".join("?" * len(ids))
        known = {
            row["id"] for row in conn.execute(
                f"SELECT id FROM evidence WHERE id IN ({placeholders})", ids
            ).fetchall()
        }
        unknown = [value for value in ids if value not in known]
        if unknown:
            raise NotFoundError(
                "找不到这些摘录：" + "、".join(unknown[:10])
                + ("…" if len(unknown) > 10 else "")
                + "；本次操作没有任何改动。"
            )
        now = utcnow()
        updated = 0
        if category is not None:
            conn.execute(
                f"UPDATE evidence SET category = ?, updated_at = ? WHERE id IN ({placeholders})",
                [category, now, *ids],
            )
            updated = len(ids)
        added = _insert_members(conn, add_id, ids) if add_id else 0
        removed = 0
        if remove_id:
            for evidence_id in ids:
                removed += conn.execute(
                    "DELETE FROM collection_evidence "
                    "WHERE collection_id = ? AND evidence_id = ?",
                    (remove_id, evidence_id),
                ).rowcount
        return {
            "evidence_ids": list(ids),
            "updated": updated,
            "collection_id": add_id or remove_id,
            "added": added,
            "removed": removed,
        }

    if connection is not None:
        return _run(connection)
    with workspace.connect() as conn:
        return _run(conn)


# ------------------------------------------------------------------ 筛选条件


def collection_filter_clause(collection_id: str) -> tuple[str, list[str]]:
    """集合筛选子句；与其它条件取交集。"""
    return (
        "id IN (SELECT evidence_id FROM collection_evidence WHERE collection_id = ?)",
        [collection_id],
    )


def assert_collection_exists(conn: sqlite3.Connection, collection_id: str) -> None:
    row = conn.execute(
        "SELECT id FROM collections WHERE id = ?", (collection_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"找不到集合：{collection_id}")


__all__ = [
    "MAX_BATCH_IDS",
    "NAME_MAX",
    "DESCRIPTION_MAX",
    "BatchPlan",
    "add_evidence",
    "apply_batch",
    "assert_collection_exists",
    "clean_category",
    "clean_description",
    "clean_name",
    "collection_filter_clause",
    "collection_ids_for",
    "create_collection",
    "get_collection",
    "list_collections",
    "normalize_ids",
    "plan_batch",
    "remove_evidence",
    "update_collection",
]