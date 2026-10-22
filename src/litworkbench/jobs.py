"""持久化任务队列（SQLite 租约 + 逐页检查点）。

网页请求只负责**入队和读状态**，识别本身在 :mod:`litworkbench.worker` 的独立
进程里执行。核心规则：

* **单执行者**：``claim_next`` 用 ``BEGIN IMMEDIATE`` + 条件更新做租约，
  同一个任务不可能被两个执行者同时拿走；
* **逐页提交**：每个单元一行 ``job_units`` 检查点，页面提交成功才记 ``done``；
  重试只把 ``failed`` 放回队列，成功页不会重做；
* **可恢复**：租约过期后 ``recover_stale`` 把任务放回队列，检查点原样保留；
* **计数诚实**：完成/失败/待处理/可检索都来自数据库真实状态，
  识别为空或失败的页绝不会计入成功。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Sequence

from litworkbench import search_index
from litworkbench.errors import LitError, NotFoundError, ValidationError, EditConflictError
from litworkbench.store import Workspace, utcnow


class ClaimLost(ValidationError):
    """执行权已丢失：过期代次、他人执行者、任务已终结或租约已过期。

    执行者拿到任务后，所有权威写入都必须携带 ``owner`` + ``generation``；
    一旦当前领取已经改变，写入必须整体拒绝（``ClaimLost``）而不是覆盖新结果。
    """

KIND_IMPORT = "import"
KIND_REPROCESS = "reprocess"
KIND_INDEX = "index"
KIND_OCR_CANDIDATE = "ocr_candidate"

STATE_QUEUED = "queued"
STATE_PREPARING = "preparing"
STATE_RUNNING = "running"
STATE_PAUSING = "pausing"
STATE_PAUSED = "paused"
STATE_PARTIAL = "partial"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"

STATES = (STATE_QUEUED, STATE_PREPARING, STATE_RUNNING, STATE_PAUSING, STATE_PAUSED,
          STATE_PARTIAL, STATE_COMPLETED, STATE_FAILED)
TERMINAL_STATES = (STATE_PARTIAL, STATE_COMPLETED, STATE_FAILED)
#: 执行者可能停在这几个状态里：崩溃、进程被杀或服务重启后要能收拢回队列。
RUNNING_STATES = (STATE_PREPARING, STATE_RUNNING, STATE_PAUSING)
INTERRUPTED_STATES = RUNNING_STATES

UNIT_WORK = "work"
UNIT_DONE = "done"
UNIT_PENDING = "pending"
UNIT_FAILED = "failed"
#: 只有 ``done`` 是「成功且不用再做」；其余都可被重试重新排队。
CHECKPOINT_STATES = (UNIT_WORK, UNIT_DONE, UNIT_PENDING, UNIT_FAILED)

DEFAULT_LEASE_SECONDS = 300

_STAGE_LABELS = {
    STATE_QUEUED: "排队中",
    STATE_PREPARING: "准备文件",
    STATE_RUNNING: "识别中",
    STATE_PAUSING: "正在暂停",
    STATE_PAUSED: "已暂停",
    STATE_PARTIAL: "部分完成",
    STATE_COMPLETED: "已完成",
    STATE_FAILED: "失败",
}


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def _in_seconds(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=float(seconds))
    return moment.replace(microsecond=0).isoformat()


def stage_label(state: str, detail: str = "") -> str:
    label = _STAGE_LABELS.get(state, state)
    return f"{label}·{detail}" if detail else label


class JobStore:
    """工作区任务队列。所有状态都落在 SQLite，进程退出不丢。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    # ------------------------------------------------------------ 事务

    @contextmanager
    def _write(self, connection=None) -> Iterator[sqlite3.Connection]:
        """写事务：立即取写锁，保证「读当前状态 -> 判断 -> 写入」原子。"""
        with (nullcontext(connection) if connection is not None else self.workspace.connect()) as conn:
            if connection is None:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _require_claim(self, row: Any, job_id: str, owner: str | None,
                       generation: int | None) -> None:
        """校验「任务存在 + 活动状态 + owner 匹配 + 代次匹配 + 租约未过期」。

        任一不符抛 :class:`ClaimLost`；调用方必须在**同一个写事务**里随后写入，
        不能「先检查再另开事务」。
        """
        if row is None:
            raise ClaimLost(f"任务不存在或已被收回：{job_id}")
        if row["state"] not in RUNNING_STATES:
            raise ClaimLost(f"任务当前不可执行（{row['state']}）：{job_id}")
        if (row["owner"] or "") != (owner or ""):
            raise ClaimLost(f"执行权已转移：{job_id}")
        if int(row["claim_generation"] or 0) != int(generation or 0):
            raise ClaimLost(f"执行代次已过期：{job_id}")
        lease = row["lease_expires_at"]
        if not lease or lease <= utcnow():
            raise ClaimLost(f"租约已过期：{job_id}")

    def _guard(self, conn: sqlite3.Connection, job_id: str, owner: str | None,
               generation: int | None) -> None:
        """执行写栅栏：提供 owner/generation 时按当前租约校验，否则保留旧语义。"""
        if owner is None and generation is None:
            return
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self._require_claim(row, job_id, owner, generation)

    @contextmanager
    def execution(self, job_id: str, owner: str | None,
                  generation: int | None) -> Iterator[sqlite3.Connection]:
        """执行事务：``BEGIN IMMEDIATE`` 校验领取后 ``yield`` 同一个连接。

        校验「任务存在 + 活动状态 + owner 匹配 + 代次匹配 + 租约未过期」，
        失败抛 :class:`ClaimLost`。所有权威执行写入都应走这个连接，提交/回滚随
        上下文退出自动完成（正文抛异常时整体回滚）。
        """
        with self.workspace.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            self._require_claim(row, job_id, owner, generation)
            yield conn

    # ------------------------------------------------------------ 入队

    def enqueue_import(self, staging_id: str, filename: str, ocr_mode: str = "auto",
                       *, doc_id: str | None = None) -> dict[str, Any]:
        job_id = new_job_id()
        now = utcnow()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO jobs (id, kind, state, stage, filename, doc_id, staging_id, "
                "ocr_mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, KIND_IMPORT, STATE_QUEUED, stage_label(STATE_QUEUED),
                 filename, doc_id, staging_id, ocr_mode, now, now),
            )
        return self.get(job_id)

    def enqueue_reprocess(self, doc_id: str, unit_ids: Sequence[str], *,
                          mode: str = "force", keep_corrections: bool = True,
                          filename: str = "") -> dict[str, Any]:
        if mode != "force":
            raise ValidationError("重新识别只支持 mode='force'。")
        if not unit_ids:
            raise ValidationError("请选择需要重新识别的页。")
        job_id = new_job_id()
        now = utcnow()
        with self._write() as conn:
            # 先写父行：job_units.job_id 有外键约束（PRAGMA foreign_keys = ON），
            # 顺序反了会直接 IntegrityError，整个入队失败。
            conn.execute(
                "INSERT INTO jobs (id, kind, state, stage, filename, doc_id, ocr_mode, "
                "total_pages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, KIND_REPROCESS, STATE_QUEUED, stage_label(STATE_QUEUED),
                 filename, doc_id, "force", len(set(unit_ids)), now, now),
            )
            rows = []
            for position, unit_id in enumerate(dict.fromkeys(unit_ids)):
                unit = conn.execute(
                    "SELECT id FROM units WHERE id = ? AND doc_id = ?", (unit_id, doc_id)
                ).fetchone()
                if unit is None:
                    raise NotFoundError(f"单元 {unit_id} 不属于文献 {doc_id}。")
                rows.append((job_id, unit_id, position, UNIT_WORK, 0, "", now))
            if not rows:
                raise ValidationError("请选择需要重新识别的页。")
            conn.executemany(
                "INSERT INTO job_units (job_id, unit_id, position, state, attempts, error, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows,
            )
            # total_pages 写的是去重后的真实页数
            conn.execute(
                "UPDATE jobs SET total_pages = ? WHERE id = ?", (len(rows), job_id)
            )
        return self.get(job_id)

    def enqueue_index(self, *, force: bool = False) -> dict[str, Any] | None:
        """为脏单元排一个索引任务；没有脏单元时返回 ``None``。"""
        with self.workspace.connect() as conn:
            pending = search_index.pending_count(conn)
            running = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE kind = ? AND state IN "
                f"({', '.join('?' * len(RUNNING_STATES))}, '{STATE_QUEUED}')",
                (KIND_INDEX, *RUNNING_STATES),
            ).fetchone()["n"]
        if pending == 0 and not force:
            return None
        if running:
            return None
        job_id = new_job_id()
        now = utcnow()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO jobs (id, kind, state, stage, total_pages, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, KIND_INDEX, STATE_QUEUED, stage_label(STATE_QUEUED), pending, now, now),
            )
        return self.get(job_id)

    # ------------------------------------------------------------ 租约

    def claim_next(self, owner_id: str, *,
                   lease_seconds: float = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        """取一个可执行的任务并加租约；同一任务不可能被两个执行者同时拿到。"""
        now = utcnow()
        expires = _in_seconds(lease_seconds)
        with self._write() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE state = ? AND pause_requested = 0 "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY created_at, rowid LIMIT 1",
                (STATE_QUEUED, now),
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                "UPDATE jobs SET state = ?, stage = ?, owner = ?, lease_expires_at = ?, "
                "started_at = COALESCE(started_at, ?), claim_generation = claim_generation + 1, "
                "updated_at = ? "
                "WHERE id = ? AND state = ? AND pause_requested = 0",
                (STATE_RUNNING, stage_label(STATE_RUNNING), owner_id, expires, now, now,
                 row["id"], STATE_QUEUED),
            ).rowcount
            if changed != 1:  # pragma: no cover - BEGIN IMMEDIATE 下不会发生
                return None
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return dict(job)

    def renew_lease(self, job_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS,
                    owner: str | None = None, generation: int | None = None,
                    connection=None) -> None:
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            conn.execute(
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (_in_seconds(lease_seconds), utcnow(), job_id),
            )

    def recover_stale(self) -> int:
        """把中断的执行中任务放回队列（租约过期、或服务重启后没有租约）。

        逐页检查点原样保留，因此**已经成功的页不会重做**。``pausing`` 也要收拢，
        否则暂停请求会让任务永远卡在中间状态，既不能继续也不能重试。

        候选任务的进行中尝试（``queued``/``running``）在同一事务里终结为
        ``interrupted``：旧执行者不能提交结果，新的领取可以重新开始尝试。
        """
        now = utcnow()
        placeholders = ", ".join("?" * len(INTERRUPTED_STATES))
        with self._write() as conn:
            rows = conn.execute(
                f"SELECT id FROM jobs WHERE state IN ({placeholders}) "
                f"AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (*INTERRUPTED_STATES, now),
            ).fetchall()
            job_ids = [row["id"] for row in rows]
            changed = conn.execute(
                f"UPDATE jobs SET state = ?, stage = ?, pause_requested = 0, owner = NULL, lease_expires_at = NULL, "
                f"updated_at = ? WHERE state IN ({placeholders}) "
                f"AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (STATE_QUEUED, stage_label(STATE_QUEUED), now, *INTERRUPTED_STATES, now),
            ).rowcount
            if job_ids:
                marks = ", ".join("?" * len(job_ids))
                conn.execute(
                    "UPDATE ocr_candidates SET state = 'failed', error_kind = 'interrupted', "
                    "error_message = ?, updated_at = ? "
                    f"WHERE state IN ('queued', 'running') AND job_id IN ({marks})",
                    ("执行者中断，尝试已作废。", now, *job_ids),
                )
        return int(changed)

    # ------------------------------------------------------------ 控制

    def get(self, job_id: str) -> dict[str, Any]:
        with self.workspace.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"找不到任务：{job_id}")
            return self.view(dict(row), review_pending_pages=self._review_pending(conn, row))

    def exists(self, job_id: str) -> bool:
        with self.workspace.connect() as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None

    def _review_pending(self, conn: sqlite3.Connection, row: Any) -> int:
        """待人工核对的 ready 候选数；只对候选任务有意义，其余恒为 0。"""
        if row["kind"] != KIND_OCR_CANDIDATE:
            return 0
        found = conn.execute(
            "SELECT COUNT(*) AS n FROM ocr_candidates WHERE job_id = ? AND state = 'ready'",
            (row["id"],),
        ).fetchone()
        return int(found["n"]) if found else 0

    def view(self, row: dict[str, Any], *, review_pending_pages: int = 0) -> dict[str, Any]:
        """契约里的任务条目字段。"""
        return {
            "id": row["id"],
            "filename": row["filename"],
            "doc_id": row["doc_id"],
            "kind": row["kind"],
            "state": row["state"],
            "stage": row["stage"],
            "total_pages": row["total_pages"],
            "processed_pages": row["processed_pages"],
            "completed_pages": row["completed_pages"],
            "failed_pages": row["failed_pages"],
            "pending_pages": row["pending_pages"],
            "searchable_pages": row["searchable_pages"],
            "review_pending_pages": int(review_pending_pages),
            "duplicate": bool(row["duplicate"]),
            "error": row["error"],
            "pause_requested": bool(row["pause_requested"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list(self, *, page: int = 1, page_size: int = 30,
             doc_id: str | None = None) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        where, params = "", []
        if doc_id:
            where = "WHERE doc_id = ?"
            params.append(doc_id)
        with self.workspace.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM jobs {where}", params).fetchone()["n"])
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC, rowid DESC "
                f"LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            items = [
                self.view(dict(row), review_pending_pages=self._review_pending(conn, row))
                for row in rows
            ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def request_pause(self, job_id: str) -> dict[str, Any]:
        """请求暂停：在当前页提交后的安全点生效；幂等。"""
        with self._write() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"找不到任务：{job_id}")
            if row["state"] in (STATE_RUNNING, STATE_PREPARING):
                conn.execute(
                    "UPDATE jobs SET state = ?, stage = ?, pause_requested = 1, updated_at = ? "
                    "WHERE id = ?",
                    (STATE_PAUSING, stage_label(STATE_PAUSING), utcnow(), job_id),
                )
            elif row["state"] in (STATE_QUEUED, STATE_PAUSED):
                conn.execute(
                    "UPDATE jobs SET state = ?, stage = ?, pause_requested = 1, updated_at = ? "
                    "WHERE id = ?",
                    (STATE_PAUSED, stage_label(STATE_PAUSED), utcnow(), job_id),
                )
            # 终态任务保持原样：暂停一个已完成的任务没有意义，也不算错误。
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self.view(dict(job))

    def resume(self, job_id: str) -> dict[str, Any]:
        """继续任务：回到队列，未处理的检查点继续做，成功页不重做。幂等。"""
        with self._write() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"找不到任务：{job_id}")
            if row["state"] == STATE_PAUSING:
                conn.execute("UPDATE jobs SET state=?, stage=?, pause_requested=0, updated_at=? WHERE id=?",
                             (STATE_RUNNING, stage_label(STATE_RUNNING), utcnow(), job_id))
            elif row["state"] in (STATE_PARTIAL, STATE_FAILED, STATE_PAUSED, STATE_QUEUED):
                conn.execute("UPDATE job_units SET state=?, error='' WHERE job_id=? AND state!=?",
                             (UNIT_WORK, job_id, UNIT_DONE))
                conn.execute(
                    "UPDATE jobs SET state = ?, stage = ?, pause_requested = 0, error = '', "
                    "owner = NULL, lease_expires_at = NULL, finished_at = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (STATE_QUEUED, stage_label(STATE_QUEUED), utcnow(), job_id),
                )
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self.view(dict(job))

    def retry_failed(self, job_id: str) -> dict[str, Any]:
        """只把失败/未完成的检查点放回队列；已完成页不重做。幂等。"""
        now = utcnow()
        with self._write() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"找不到任务：{job_id}")
            if row["state"] in RUNNING_STATES:
                raise EditConflictError("任务仍在执行，请先暂停并等待当前页结束，再重试失败页。")
            if row["kind"] == KIND_INDEX:
                conn.execute(
                    "UPDATE jobs SET state = ?, stage = ?, error = '', owner = NULL, "
                    "lease_expires_at = NULL, finished_at = NULL, updated_at = ? WHERE id = ?",
                    (STATE_QUEUED, stage_label(STATE_QUEUED), now, job_id),
                )
            else:
                conn.execute(
                    "UPDATE job_units SET state = ?, error = '', updated_at = ? "
                    "WHERE job_id = ? AND state != ?",
                    (UNIT_WORK, now, job_id, UNIT_DONE),
                )
                conn.execute(
                    "UPDATE jobs SET state = ?, stage = ?, error = '', pause_requested = 0, "
                    "owner = NULL, lease_expires_at = NULL, finished_at = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (STATE_QUEUED, stage_label(STATE_QUEUED), now, job_id),
                )
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self.view(dict(job))

    # ------------------------------------------------------------ 执行期

    def set_stage(self, job_id: str, detail: str = "", *, state: str | None = None,
                  owner: str | None = None, generation: int | None = None,
                  connection=None) -> None:
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
            target = state or (row["state"] if row else STATE_QUEUED)
            conn.execute(
                "UPDATE jobs SET stage = ?, state = COALESCE(?, state), updated_at = ? WHERE id = ?",
                (stage_label(target, detail), state, utcnow(), job_id),
            )

    def _state_of(self, job_id: str) -> str:
        with self.workspace.connect() as conn:
            row = conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["state"] if row else STATE_QUEUED

    def plan_units(self, job_id: str, unit_ids: Sequence[str], *,
                   owner: str | None = None, generation: int | None = None,
                   connection=None) -> int:
        """登记本任务的检查点；已有行保持原状态（崩溃后重排不会重做成功页）。"""
        now = utcnow()
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM job_units WHERE job_id = ?", (job_id,)
            ).fetchone()["n"]
            conn.executemany(
                "INSERT OR IGNORE INTO job_units (job_id, unit_id, position, state, attempts, "
                "error, updated_at) VALUES (?, ?, ?, ?, 0, '', ?)",
                [(job_id, unit_id, position, UNIT_WORK, now)
                 for position, unit_id in enumerate(unit_ids)],
            )
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM job_units WHERE job_id = ?", (job_id,)
            ).fetchone()["n"]
        return int(after) - int(before)

    def todo_units(self, job_id: str) -> list[dict[str, Any]]:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_units WHERE job_id = ? AND state = ? ORDER BY position",
                (job_id, UNIT_WORK),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_unit(self, job_id: str, unit_id: str, state: str, error: str = "", *,
                    owner: str | None = None, generation: int | None = None,
                    connection=None) -> None:
        """记录一个单元检查点的真实结局。

        ``done`` 表示这一页处理完且拿到了文字；``pending`` 表示处理了但没有文字
        （识别为空/引擎不可用），可以重试；``failed`` 表示出错，可以重试。
        只有 ``done`` 不会被 ``retry-failed`` 重做。
        """
        if state not in CHECKPOINT_STATES:
            raise ValidationError(f"未知的检查点状态：{state}")
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            conn.execute(
                "UPDATE job_units SET state = ?, error = ?, attempts = attempts + 1, "
                "updated_at = ? WHERE job_id = ? AND unit_id = ?",
                (state, error[:2000], utcnow(), job_id, unit_id),
            )

    def pause_requested(self, job_id: str) -> bool:
        with self.workspace.connect() as conn:
            row = conn.execute(
                "SELECT pause_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row["pause_requested"]) if row else False

    def finish(self, job_id: str, state: str, *, error: str = "",
               owner: str | None = None, generation: int | None = None,
               connection=None) -> dict[str, Any]:
        if state not in STATES:
            raise ValidationError(f"未知的任务状态：{state}")
        now = utcnow()
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return {}
            # 计数先于清空 owner 落库：同一个事务里完成，计数器永不对应过期领取。
            counts = self._counts(conn, dict(row))
            conn.execute(
                "UPDATE jobs SET total_pages = ?, processed_pages = ?, completed_pages = ?, "
                "failed_pages = ?, pending_pages = ?, searchable_pages = ?, state = ?, stage = ?, "
                "error = ?, owner = NULL, lease_expires_at = NULL, finished_at = ?, "
                "pause_requested = 0, updated_at = ? WHERE id = ?",
                (counts["total_pages"], counts["processed_pages"], counts["completed_pages"],
                 counts["failed_pages"], counts["pending_pages"], counts["searchable_pages"],
                 state, stage_label(state), error[:2000], now, now, job_id),
            )
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            pending = self._review_pending(conn, job)
        return self.view(dict(job), review_pending_pages=pending) if job else {}

    def update_fields(self, job_id: str, *, owner=None, generation=None,
                      connection=None, **fields: Any) -> None:
        allowed = {"doc_id", "total_pages", "filename", "duplicate", "ocr_mode", "stage"}
        unknown = set(fields) - allowed
        if unknown or not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            conn.execute(
                f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ?",
                [*fields.values(), utcnow(), job_id],
            )

    # ------------------------------------------------------------ 计数

    def refresh_counters(self, job_id: str, *, owner: str | None = None,
                         generation: int | None = None, connection=None) -> dict[str, int]:
        """按数据库真实状态重算计数（不信任任何缓存值）。"""
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise NotFoundError(f"找不到任务：{job_id}")
            counts = self._counts(conn, dict(job))
            conn.execute(
                "UPDATE jobs SET total_pages = ?, processed_pages = ?, completed_pages = ?, "
                "failed_pages = ?, pending_pages = ?, searchable_pages = ?, updated_at = ? "
                "WHERE id = ?",
                (counts["total_pages"], counts["processed_pages"], counts["completed_pages"],
                 counts["failed_pages"], counts["pending_pages"], counts["searchable_pages"],
                 utcnow(), job_id),
            )
        return counts

    def _counts(self, conn: sqlite3.Connection, job: dict[str, Any]) -> dict[str, Any]:
        kind = job["kind"]
        if kind in (KIND_REPROCESS, KIND_INDEX, KIND_OCR_CANDIDATE):
            # 逐个单元的检查点：成功/失败/未完成都来自 job_units，不猜测。
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "       COALESCE(SUM(state = 'done'), 0) AS done, "
                "       COALESCE(SUM(state = 'failed'), 0) AS failed, "
                "       COALESCE(SUM(state IN ('work', 'pending')), 0) AS work "
                "FROM job_units WHERE job_id = ?", (job["id"],),
            ).fetchone()
            return {
                "total_pages": int(row["total"]),
                "processed_pages": int(row["done"]) + int(row["failed"]),
                "completed_pages": int(row["done"]),
                "failed_pages": int(row["failed"]),
                "pending_pages": int(row["work"]),
                "searchable_pages": search_index.searchable_pages(conn, job["doc_id"]),
            }
        # import：以文献真实页数与单元状态为准
        row = conn.execute(
            "SELECT COUNT(*) AS units, "
            "       COALESCE(SUM(status = 'completed'), 0) AS completed, "
            "       COALESCE(SUM(status = 'failed'), 0) AS failed, "
            "       COALESCE(SUM(status = 'pending'), 0) AS pending "
            "FROM units WHERE doc_id = ?", (job["doc_id"],),
        ).fetchone()
        total_pages = job["total_pages"]
        if total_pages is None and job["doc_id"]:
            document = conn.execute(
                "SELECT page_count FROM documents WHERE id = ?", (job["doc_id"],)
            ).fetchone()
            total_pages = document["page_count"] if document else None
        known_pages = int(total_pages or 0)
        missing = max(0, known_pages - int(row["units"] or 0))
        return {
            "total_pages": total_pages,
            "processed_pages": int(row["completed"]) + int(row["failed"]),
            "completed_pages": int(row["completed"]),
            "failed_pages": int(row["failed"]),
            "pending_pages": int(row["pending"]) + missing,
            "searchable_pages": search_index.searchable_pages(conn, job["doc_id"]),
        }

    # ------------------------------------------------------------ 暂存

    def add_staging(self, staging_id: str, filename: str, stored_path: str,
                    digest: str, byte_size: int) -> None:
        with self.workspace.connect() as conn:
            conn.execute(
                "INSERT INTO import_staging (id, filename, stored_path, sha256, byte_size, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (staging_id, filename, stored_path, digest, int(byte_size), utcnow()),
            )

    def staging(self, staging_id: str) -> dict[str, Any]:
        with self.workspace.connect() as conn:
            row = conn.execute(
                "SELECT * FROM import_staging WHERE id = ?", (staging_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"找不到上传暂存记录：{staging_id}")
        return dict(row)

    def drop_staging(self, staging_id: str, *, status: str = "consumed",
                     error: str = "", job_id=None, owner=None, generation=None,
                     connection=None) -> None:
        with self._write(connection) as conn:
            self._guard(conn, job_id, owner, generation)
            conn.execute(
                "UPDATE import_staging SET status = ?, error = ? WHERE id = ?",
                (status, error[:2000], staging_id),
            )
