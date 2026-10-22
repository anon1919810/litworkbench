"""OCR 候选：存储、内容修订号与采用事务（Task A）。

契约见 ``docs/web-ocr-candidate-contract.md`` 与
``docs/superpowers/plans/2026-10-16-ocr-candidate-review.md``。

核心保证（都在**一个写事务**里完成）：

* **生成为只读旁路**：建任务、开尝试、完成尝试都不改 ``units``/``evidence``/
  ``index``；只有人工**采用**才会更新机器底稿。
* **尝试即快照**：``begin_attempt`` 在拿写锁后记录当时的
  ``units.content_revision`` 与来源指纹；推理期间页面被改动，
  迟到的 ``complete_attempt`` 会标 ``stale`` 而不是覆盖现用正文。
* **采用是 CAS**：要求 ``expected_content_revision == 候选基础修订号 ==
  当前修订号``，并重新核对**实际存储的 PDF 字节**的 SHA，任一处不符即 409。
* **采用可回滚**：快照、正文更新、历史、摘录复核、索引失效在同一事务，
  任何一步失败都整体回滚，候选与正文都不留半成品。
* **幂等**：同一候选重复采用返回原决定；重复丢弃保留首次``decided_at``。
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from litworkbench import jobs as jobs_mod
from litworkbench.errors import EditConflictError, NotFoundError, ValidationError
from litworkbench.evidence import _mark_stale_evidence
from litworkbench.jobs import JobStore
from litworkbench.store import Workspace, sha256_file, sha256_text, utcnow

#: 只允许这两个互相兼容的配置；真正的能力探测由 Task B 的受控推理实现。
PROFILE_COMPATIBLE = "compatible-v1"
PROFILE_SMALL_LOWMEM = "small-lowmem-v1"
PROFILES = (PROFILE_COMPATIBLE, PROFILE_SMALL_LOWMEM)
DEFAULT_PROFILE = PROFILE_SMALL_LOWMEM

#: 版面选择：本阶段只支持原版。
LAYOUT_ORIGINAL = "original"
LAYOUTS = (LAYOUT_ORIGINAL,)

#: 单次任务最多多少页（去重后）。防止一次点选整个大型 PDF。
MAX_UNITS = 1000

CANDIDATE_QUEUED = "queued"
CANDIDATE_RUNNING = "running"
CANDIDATE_READY = "ready"
CANDIDATE_FAILED = "failed"
CANDIDATE_ADOPTED = "adopted"
CANDIDATE_REJECTED = "rejected"
CANDIDATE_STALE = "stale"

#: 仍在进行、占用「每页一个活动尝试」名额的状态。
CANDIDATE_ACTIVE_STATES = (CANDIDATE_QUEUED, CANDIDATE_RUNNING)

#: 候选详情字段（冻结契约）。
CANDIDATE_DETAIL_FIELDS = (
    "id", "job_id", "attempt_id", "unit_id", "doc_id", "file_page", "state",
    "base_content_revision", "profile_id", "layout", "result", "error",
    "created_at", "decided_at",
)


def profile_descriptors() -> list[dict[str, Any]]:
    """可供选择的识别配置。

    Task A 阶段能力探测由 Task B 接入，这里**静态**返回 ``available=False``
    并给出明确的分期说明；不含任何本机路径。
    """
    from litworkbench.ocr_profiles import get_profiles
    return get_profiles()


def _normalize_error(error: Any) -> tuple[str, str]:
    """把 ``fail_attempt`` 的 ``error`` 归一成 ``(kind, message)``。"""
    if isinstance(error, dict):
        kind = str(error.get("kind") or "error")[:100]
        message = str(error.get("message") or error.get("detail") or "")
    else:
        kind = "error"
        message = str(error or "")
    return kind, message[:2000]


def _mean_confidence(lines: Any) -> float | None:
    confidences = [
        line.get("confidence")
        for line in (lines or [])
        if isinstance(line, dict) and isinstance(line.get("confidence"), (int, float))
    ]
    return sum(confidences) / len(confidences) if confidences else None


def _ocr_record(result: dict[str, Any]) -> dict[str, Any]:
    """现用 ``units.ocr_json`` 的完整形状。

    保留候选结果的**全部**来源信息（引擎、模型指纹、配置、计时、内存、坐标系），
    而不是只留 unit_detail 需要的那几项；``save_unit`` 会把它序列化后落库。
    只补算一个 ``mean_confidence`` 方便列表展示。
    """
    record = dict(result)
    record.setdefault("mean_confidence", _mean_confidence(result.get("lines")))
    return record


class CandidateStore:
    """工作区里的 OCR 候选与采用决定。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    # ------------------------------------------------------------ 事务

    @contextmanager
    def _write(self) -> Iterator[Any]:
        """写事务：立即拿写锁，保证「读当前状态 -> 判断 -> 写入」原子。"""
        with self.workspace.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    # ------------------------------------------------------------ 入队

    def create_job(self, doc_id: str, unit_ids: Sequence[str], *,
                   profile_id: str = DEFAULT_PROFILE,
                   layout: str = LAYOUT_ORIGINAL) -> dict[str, Any]:
        """建一个 ``ocr_candidate`` 任务；只登记，不做任何识别或正文改动。"""
        if profile_id not in PROFILES:
            raise ValidationError(
                f"未知的识别配置：{profile_id}（可选 {'/'.join(PROFILES)}）"
            )
        if layout not in LAYOUTS:
            raise ValidationError(
                f"未知的版面选择：{layout}（可选 {'/'.join(LAYOUTS)}）"
            )
        ordered = list(dict.fromkeys(unit_ids or []))
        if not ordered:
            raise ValidationError("请选择至少一页来生成识别候选。")
        if len(ordered) > MAX_UNITS:
            raise ValidationError(f"一次最多 {MAX_UNITS} 页（去重后）。")

        document = self.workspace.get_document(doc_id)
        if document["filetype"] != "pdf":
            raise ValidationError("识别候选目前只支持 PDF 页；Word 请使用校订。")

        now = utcnow()
        job_id = jobs_mod.new_job_id()
        with self._write() as conn:
            rows: list[tuple[Any, ...]] = []
            for position, unit_id in enumerate(ordered):
                unit = conn.execute(
                    "SELECT id, doc_id, kind FROM units WHERE id = ?", (unit_id,)
                ).fetchone()
                if unit is None or unit["doc_id"] != doc_id:
                    raise NotFoundError(f"单元 {unit_id} 不属于文献 {doc_id}。")
                if unit["kind"] != "page":
                    raise ValidationError("只有 PDF 页可以生成识别候选；Word 请使用校订。")
                rows.append((job_id, unit_id, position, jobs_mod.UNIT_WORK, 0, "", now))
            conn.execute(
                "INSERT INTO jobs (id, kind, state, stage, filename, doc_id, ocr_mode, "
                "total_pages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, jobs_mod.KIND_OCR_CANDIDATE, jobs_mod.STATE_QUEUED,
                 jobs_mod.stage_label(jobs_mod.STATE_QUEUED), document["filename"], doc_id,
                 "candidate", len(rows), now, now),
            )
            conn.execute(
                "INSERT INTO ocr_candidate_jobs (job_id, profile_id, layout, created_at) "
                "VALUES (?, ?, ?, ?)",
                (job_id, profile_id, layout, now),
            )
            conn.executemany(
                "INSERT INTO job_units (job_id, unit_id, position, state, attempts, error, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return JobStore(self.workspace).get(job_id)

    # ------------------------------------------------------------ 尝试

    def _require_active_owner(self, job: Any, owner: str | None, generation: int) -> None:
        JobStore(self.workspace)._require_claim(job, job["id"], owner, generation)
        if job["state"] not in jobs_mod.RUNNING_STATES:
            raise ValidationError("任务当前不可执行（可能已被暂停或终结）。")
        if (job["owner"] or "") != (owner or ""):
            raise ValidationError("执行权已转移：当前执行者不是本写入者。")
        if int(job["claim_generation"] or 0) != int(generation):
            raise ValidationError("执行代次已过期：拒绝写入。")

    def _require_own_attempt(self, candidate: Any, owner: str | None,
                             generation: int) -> None:
        if candidate["state"] not in CANDIDATE_ACTIVE_STATES:
            raise ValidationError("这次尝试已经结束，不能重复提交。")
        if (candidate["owner"] or "") != (owner or ""):
            raise ValidationError("候选尝试的执行者不匹配。")
        if int(candidate["generation"] or 0) != int(generation):
            raise ValidationError("候选尝试的执行代次不匹配。")

    def begin_attempt(self, job_id: str, unit_id: str, *, owner: str,
                      generation: int) -> dict[str, Any]:
        """开启一次尝试：原子记录基础修订号与来源指纹。

        同一页同一时刻只允许一个活动尝试（数据库部分唯一索引兜底）。
        """
        now = utcnow()
        with self._write() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise NotFoundError(f"找不到任务：{job_id}")
            self._require_active_owner(job, owner, generation)
            if job["kind"] != jobs_mod.KIND_OCR_CANDIDATE:
                raise ValidationError("该任务不是识别候选任务。")
            link = conn.execute(
                "SELECT profile_id, layout FROM ocr_candidate_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if link is None:
                raise ValidationError("候选任务缺少配置记录。")
            membership = conn.execute(
                "SELECT 1 FROM job_units WHERE job_id = ? AND unit_id = ?", (job_id, unit_id)
            ).fetchone()
            if membership is None:
                raise ValidationError(f"单元 {unit_id} 不属于任务 {job_id}。")
            unit = conn.execute(
                "SELECT id, doc_id, file_page, original_text, corrected_text, content_revision "
                "FROM units WHERE id = ?", (unit_id,)
            ).fetchone()
            if unit is None:
                raise NotFoundError(f"找不到原文单元：{unit_id}")
            document = self.workspace.get_document(unit["doc_id"], connection=conn)
            corrected = unit["corrected_text"]
            candidate_id = f"candidate_{uuid.uuid4().hex[:12]}"
            attempt_id = f"attempt_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO ocr_candidates (id, attempt_id, job_id, unit_id, doc_id, file_page, "
                "source_sha256, state, base_content_revision, base_original_text_sha256, "
                "base_corrected_text_sha256, profile_id, layout, owner, generation, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate_id, attempt_id, job_id, unit_id, unit["doc_id"], unit["file_page"],
                 document["sha256"], CANDIDATE_RUNNING, int(unit["content_revision"] or 0),
                 sha256_text(unit["original_text"] or ""),
                 sha256_text(corrected) if corrected is not None else None,
                 link["profile_id"], link["layout"], owner, int(generation), now, now),
            )
            row = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._detail(row)

    def complete_attempt(self, candidate_id: str, result: dict[str, Any], *, owner: str,
                         generation: int) -> dict[str, Any]:
        """提交一次尝试的识别结果。

        空结果/缺 ``text`` 直接拒绝（不能混进待核候选）。页面在推理期间被改动时，
        候选标 ``stale``，绝不覆盖现用正文；候选与 ``job_units`` 检查点同一事务提交。
        """
        if not isinstance(result, dict) or not str(result.get("text") or "").strip():
            raise ValidationError("识别结果为空或缺少 text，不能记为待核候选。")
        now = utcnow()
        with self._write() as conn:
            candidate = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise NotFoundError(f"找不到候选：{candidate_id}")
            job = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (candidate["job_id"],)
            ).fetchone()
            if job is None:
                raise NotFoundError(f"找不到任务：{candidate['job_id']}")
            self._require_active_owner(job, owner, generation)
            self._require_own_attempt(candidate, owner, generation)
            unit = conn.execute(
                "SELECT content_revision FROM units WHERE id = ?", (candidate["unit_id"],)
            ).fetchone()
            if unit is None:
                raise NotFoundError(f"找不到原文单元：{candidate['unit_id']}")
            changed = (
                int(unit["content_revision"] or 0)
                != int(candidate["base_content_revision"] or 0)
            )
            state = CANDIDATE_STALE if changed else CANDIDATE_READY
            conn.execute(
                "UPDATE ocr_candidates SET state = ?, result_json = ?, updated_at = ? "
                "WHERE id = ?",
                (state, json.dumps(result, ensure_ascii=False), now, candidate_id),
            )
            conn.execute(
                "UPDATE job_units SET state = ?, error = '', attempts = attempts + 1, "
                "updated_at = ? WHERE job_id = ? AND unit_id = ?",
                (jobs_mod.UNIT_DONE, now, candidate["job_id"], candidate["unit_id"]),
            )
            row = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._detail(row)

    def fail_attempt(self, candidate_id: str, error: dict[str, Any] | str, *, owner: str,
                     generation: int) -> dict[str, Any]:
        """记录一次失败尝试；失败审计保留，重试会新建候选而不是覆盖它。"""
        kind, message = _normalize_error(error)
        now = utcnow()
        with self._write() as conn:
            candidate = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise NotFoundError(f"找不到候选：{candidate_id}")
            job = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (candidate["job_id"],)
            ).fetchone()
            if job is None:
                raise NotFoundError(f"找不到任务：{candidate['job_id']}")
            self._require_active_owner(job, owner, generation)
            self._require_own_attempt(candidate, owner, generation)
            conn.execute(
                "UPDATE ocr_candidates SET state = ?, error_kind = ?, error_message = ?, "
                "updated_at = ? WHERE id = ?",
                (CANDIDATE_FAILED, kind, message, now, candidate_id),
            )
            conn.execute(
                "UPDATE job_units SET state = ?, error = ?, attempts = attempts + 1, "
                "updated_at = ? WHERE job_id = ? AND unit_id = ?",
                (jobs_mod.UNIT_FAILED, message[:2000], now, candidate["job_id"],
                 candidate["unit_id"]),
            )
            row = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._detail(row)

    # ------------------------------------------------------------ 读取

    def get(self, candidate_id: str) -> dict[str, Any]:
        with self.workspace.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"找不到候选：{candidate_id}")
        return self._detail(row)

    def list_for_unit(self, unit_id: str, *, page: int = 1, page_size: int = 30,
                      job_id: str | None = None) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        clauses = ["unit_id = ?"]
        params: list[Any] = [unit_id]
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        where = " AND ".join(clauses)
        with self.workspace.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM ocr_candidates WHERE {where}", params
            ).fetchone()["n"])
            rows = conn.execute(
                f"SELECT * FROM ocr_candidates WHERE {where} "
                f"ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [self._summary(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ------------------------------------------------------------ 决定

    def reject(self, candidate_id: str, *, reason: str = "") -> dict[str, Any]:
        """丢弃候选：只改候选状态，绝不动正文；幂等保留首次决定时间。"""
        now = utcnow()
        with self._write() as conn:
            candidate = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise NotFoundError(f"找不到候选：{candidate_id}")
            if candidate["state"] == CANDIDATE_REJECTED:
                return self._detail(candidate)
            if candidate["state"] in CANDIDATE_ACTIVE_STATES:
                raise EditConflictError("候选尝试尚未结束（queued/running），不能丢弃。")
            if candidate["state"] == CANDIDATE_ADOPTED:
                raise EditConflictError("候选已经被采用，不能再丢弃。")
            conn.execute(
                "UPDATE ocr_candidates SET state = ?, decision = 'rejected', "
                "reject_reason = ?, decided_at = COALESCE(decided_at, ?), updated_at = ? "
                "WHERE id = ?",
                (CANDIDATE_REJECTED, reason or candidate["reject_reason"] or "", now, now,
                 candidate_id),
            )
            row = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return self._detail(row)

    def adopt(self, candidate_id: str, *, expected_content_revision: int) -> dict[str, Any]:
        """人工采用候选：CAS + 来源指纹校验 + 快照 + 原子更新。"""
        now = utcnow()
        with self._write() as conn:
            candidate = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if candidate is None:
                raise NotFoundError(f"找不到候选：{candidate_id}")

            # 1) 幂等：已经采用过就直接返回原决定，不再校验版本、不再改写正文。
            if candidate["state"] == CANDIDATE_ADOPTED:
                return {
                    "candidate": self._detail(candidate),
                    "unit": self.workspace.get_unit(candidate["unit_id"], connection=conn),
                }

            unit = self.workspace.get_unit(candidate["unit_id"], connection=conn)
            document = self.workspace.get_document(unit["doc_id"], connection=conn)

            # 2) 身份 / 来源 / 页序：不能只信 documents.sha256，要重算实际字节。
            if unit["doc_id"] != candidate["doc_id"] or unit["file_page"] != candidate["file_page"]:
                raise EditConflictError("页面身份已经变化，候选对照已失效。")
            if document["sha256"] != candidate["source_sha256"]:
                raise EditConflictError("来源文献指纹已经变化，候选对照已失效。")
            stored = self.workspace.path_of(document)
            if not stored.exists() or sha256_file(stored) != candidate["source_sha256"]:
                raise EditConflictError("来源文件内容已经变化，候选对照已失效。")

            # 3) 状态与修订号 CAS。
            if candidate["state"] != CANDIDATE_READY:
                raise EditConflictError("候选尚未就绪或已结束，不能采用。")
            if unit["blank_confirmed"]:
                raise EditConflictError("该页已被确认为空白，不能采用候选。")
            current = int(unit["content_revision"] or 0)
            base = int(candidate["base_content_revision"] or 0)
            if int(expected_content_revision) != current or base != current:
                raise EditConflictError("页面内容修订号已经变化，请重新生成候选后再采用。")

            result = json.loads(candidate["result_json"]) if candidate["result_json"] else {}
            new_text = str(result.get("text") or "")
            if not new_text.strip():
                raise EditConflictError("候选结果为空，不能采用。")

            # 4) 采用前完整快照（不可变，独立于现用正文）。
            self._save_snapshot(conn, candidate, unit, now)

            # 5) 原子更新机器 OCR 层。走 Workspace.save_unit：检索字段、文字哈希、
            #    ocr_decision 与文档计数一起按现行有效文字维护，避免整行裸写遗漏。
            #    人工校订与印刷页码原样保留；有效文字为空时状态仍是 pending。
            prior_original = unit["original_text"]
            effective = unit.get("corrected_text")
            unit["original_text"] = new_text
            unit["ocr_json"] = _ocr_record(result)
            unit["method"] = "ocr"
            unit["ocr_decision"] = "ocr"
            unit["status"] = "completed" if str(effective if effective is not None else new_text).strip() else "pending"
            unit["extraction_status"] = "completed"
            saved = self.workspace.save_unit(unit, connection=conn)
            self.workspace.add_revision(
                unit["id"], "ocr-adopt", prior_original or "", new_text,
                f"采用 OCR 候选 {candidate_id}", connection=conn,
            )
            _mark_stale_evidence(self.workspace, unit["id"], connection=conn)

            # 6) 同页其他 ready 候选标 stale；本候选记为 adopted。
            conn.execute(
                "UPDATE ocr_candidates SET state = ?, updated_at = ? "
                "WHERE unit_id = ? AND id != ? AND state = ?",
                (CANDIDATE_STALE, now, unit["id"], candidate_id, CANDIDATE_READY),
            )
            conn.execute(
                "UPDATE ocr_candidates SET state = ?, decision = 'adopted', decided_at = ?, "
                "updated_at = ? WHERE id = ?",
                (CANDIDATE_ADOPTED, now, now, candidate_id),
            )
            adopted = conn.execute(
                "SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            return {
                "candidate": self._detail(adopted),
                "unit": saved,
            }

    # ------------------------------------------------------------ 序列化 / 快照

    def _save_snapshot(self, conn: Any, candidate: Any, unit: dict[str, Any],
                       now: str) -> None:
        """保留采用前的完整旧 OCR 底稿与来源元数据。

        直接读原始列，不经过 ``get_unit`` 的 JSON 解析：旧 ``ocr_json`` 若是无法解析
        的坏数据也要**原样**留下。旧来源的引擎/模型/配置/版面取自旧 ``ocr_json``，
        缺失或坏掉时标 ``unknown``，绝不误标成新候选自己的配置。
        """
        raw = conn.execute(
            "SELECT original_text, corrected_text, ocr_json FROM units WHERE id = ?",
            (unit["id"],),
        ).fetchone()
        old_original = (raw["original_text"] if raw is not None else unit.get("original_text")) or ""
        old_corrected = raw["corrected_text"] if raw is not None else unit.get("corrected_text")
        raw_prior = raw["ocr_json"] if raw is not None else None

        prior: dict[str, Any] | None = None
        if isinstance(raw_prior, str) and raw_prior.strip():
            old_ocr_json = raw_prior  # 原样保留（即使不是合法 JSON）
            try:
                parsed = json.loads(raw_prior)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                prior = parsed
        else:
            old_ocr_json = "unknown"

        engine: Any = None
        fingerprints: Any = None
        profile: Any = None
        layout: Any = None
        if prior is not None:
            engine = prior.get("engine")
            fingerprints = prior.get("model_fingerprints")
            layout = prior.get("layout")
            config = prior.get("config")
            if isinstance(config, dict):
                profile = config.get("profile_id")
            profile = prior.get("profile_id") or profile

        conn.execute(
            "INSERT INTO ocr_candidate_snapshots (id, candidate_id, unit_id, doc_id, "
            "source_sha256, file_page, old_original_text, old_ocr_json, "
            "old_corrected_text_sha256, engine, model_fingerprints, profile_id, layout, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"snap_{uuid.uuid4().hex[:12]}", candidate["id"], unit["id"], unit["doc_id"],
                candidate["source_sha256"], unit["file_page"], old_original, old_ocr_json,
                sha256_text(old_corrected) if old_corrected is not None else None,
                engine if isinstance(engine, str) and engine else "unknown",
                json.dumps(fingerprints, ensure_ascii=False) if fingerprints is not None else "[]",
                profile if isinstance(profile, str) and profile else "unknown",
                layout if isinstance(layout, str) and layout else "unknown",
                now,
            ),
        )

    @staticmethod
    def _detail(row: Any) -> dict[str, Any]:
        error = None
        if row["error_kind"]:
            error = {"kind": row["error_kind"], "message": row["error_message"] or ""}
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "unit_id": row["unit_id"],
            "doc_id": row["doc_id"],
            "file_page": row["file_page"],
            "state": row["state"],
            "base_content_revision": int(row["base_content_revision"] or 0),
            "profile_id": row["profile_id"],
            "layout": row["layout"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": error,
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
        }

    @staticmethod
    def _summary(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "attempt_id": row["attempt_id"],
            "unit_id": row["unit_id"],
            "doc_id": row["doc_id"],
            "file_page": row["file_page"],
            "state": row["state"],
            "base_content_revision": int(row["base_content_revision"] or 0),
            "profile_id": row["profile_id"],
            "layout": row["layout"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
        }
