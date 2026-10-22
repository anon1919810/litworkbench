"""后台执行者：唯一负责长耗时 OCR 的受控子进程。

入口 ``python -m litworkbench.worker --workspace <path>``：

* 一次只拿一个任务（SQLite 租约），因此同一任务永远只有一个执行者；
* 逐页提交：每页的识别结果、校订保留、证据复核与索引更新都**先落盘再进下一页**，
  中途崩溃或被暂停时已完成的部分仍然可用；
* 暂停在「页面提交后的安全点」生效，不会留下半页结果；
* PDFium 只在本进程内单线程调用；网页进程的页图渲染在另一个隔离进程里，
  两个进程不会同时进入同一个 PDFium 库实例。

停止方式：父进程在 ``<workspace>/.litworkbench/worker.stop`` 写入停止标记，
本进程在轮询间隙优雅退出（释放 OCR 引擎），超时后才会被强制结束。
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from litworkbench import ocr as ocr_mod
from litworkbench import search_index
from litworkbench.errors import LitError, NotFoundError, UnsupportedFileError
from litworkbench.ingest import (
    _doc_id,
    _iter_docx_blocks,
    _save_extraction,
    _store_copy,
    _unit_id,
    failed_page_block,
    force_page_block,
    open_pdf_pages,
)
from litworkbench.jobs import (
    KIND_INDEX,
    KIND_IMPORT,
    KIND_REPROCESS,
    KIND_OCR_CANDIDATE,
    ClaimLost,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_PARTIAL,
    STATE_PAUSED,
    JobStore,
)
from litworkbench.store import Workspace, sha256_file, utcnow
from litworkbench.text import UnitStatus

POLL_INTERVAL = 0.25


def stop_marker_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / ".litworkbench" / "worker.stop"


class Worker:
    """单个执行者。``tick`` 处理一个可领取的任务并返回是否处理过。"""

    def __init__(self, workspace: Workspace, *, owner_id: str | None = None,
                 lease_seconds: float = 300.0, run_page=None, ocr_page=None) -> None:
        self.workspace = workspace
        self.store = JobStore(workspace)
        self.owner_id = owner_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        from litworkbench.ocr_process import run_page as controlled_page
        self.run_page = run_page or controlled_page
        self.ocr_page = ocr_page
        self._generation = None
        self._active_job = None

    def _execution(self, job_id):
        return self.store.execution(job_id, self.owner_id, self._generation)

    def _job_call(self, method, job_id, *args, **kwargs):
        return getattr(self.store, method)(job_id, *args, owner=self.owner_id,
                                           generation=self._generation, **kwargs)

    def _heartbeat(self, job_id):
        self._job_call("renew_lease", job_id, lease_seconds=self.lease_seconds)

    def _with_ocr_gate(self, job_id, action):
        from litworkbench.ocr_gate import OcrGate
        gate = OcrGate(self.workspace)
        self._heartbeat(job_id)
        ticket = gate.acquire(job_id=job_id, owner=self.owner_id, generation=self._generation,
                              lease_seconds=self.lease_seconds,
                              renew_own_lease=lambda: self._heartbeat(job_id))
        reaped = False
        started = False

        def checkpoint(identity=None):
            nonlocal started
            if identity is not None:
                started = True
                gate.register_child(ticket["token"], **identity)
            gate.renew(ticket["token"], lease_seconds=self.lease_seconds)

        try:
            value = action(checkpoint)
            reaped = bool(value.get("process", {}).get("reaped")) if "ok" in value else not started
            self._heartbeat(job_id)
            return value
        finally:
            # Exceptions before spawn have nothing to reap. A registered child requires proof.
            try:
                gate.release(ticket["token"], reaped=reaped or not started)
            except ClaimLost:
                pass  # A recovery owner already fenced and reaped this token.

    def _background_ocr(self, workspace, path, index, text, scale):
        job_id = self._active_job
        if self.ocr_page:
            return self._with_ocr_gate(job_id, lambda checkpoint:
                                       self.ocr_page(workspace, path, index, text, scale))
        doc_id = self.store.get(job_id)["doc_id"]
        unit = {"doc_id": doc_id, "file_page": index + 1, "_render_scale": scale}
        try:
            envelope = self._with_ocr_gate(job_id, lambda checkpoint:
                        self.run_page(workspace, unit, profile_id="compatible-v1", checkpoint=checkpoint))
            result = self._result(envelope)
        except ClaimLost:
            raise
        except ocr_mod.OcrUnavailable as exc:
            return {"method": "text", "status": UnitStatus.PENDING, "notes": str(exc),
                    "original_text": text}
        except Exception as exc:
            return {"method": "text", "status": UnitStatus.FAILED,
                    "notes": f"OCR 失败：{type(exc).__name__}: {exc}", "original_text": text}
        if not result.get("text", "").strip():
            return {"method": "ocr", "status": UnitStatus.PENDING, "notes": "OCR 未返回文字",
                    "original_text": text, "ocr_json": result}
        return {"method": "ocr", "status": UnitStatus.COMPLETED, "notes": "",
                "original_text": result["text"], "ocr_json": result}

    @staticmethod
    def _result(value):
        if "ok" in value:
            if not value["ok"]:
                error = value.get("error") or {}
                raise LitError(f"{error.get('kind', 'ocr_error')}：{error.get('message', '')}")
            return value["result"]
        return value

    # ------------------------------------------------------------ 调度

    def tick(self) -> dict[str, Any] | None:
        job = self.store.claim_next(self.owner_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        self._generation = job["claim_generation"]
        self._active_job = job["id"]
        try:
            self.process(job)
        except ClaimLost:
            pass
        except LitError as exc:
            try:
                self._job_call("finish", job["id"], STATE_FAILED, error=str(exc))
            except ClaimLost:
                pass
        except Exception as exc:  # pragma: no cover - 保护性分支：真实错误会留在任务里
            try:
                self._job_call("finish", job["id"], STATE_FAILED,
                               error=f"{type(exc).__name__}: {exc}")
            except ClaimLost:
                pass
        return self.store.get(job["id"])

    def process(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        self._generation = job["claim_generation"]
        self._active_job = job_id
        with self._execution(job_id):
            pass
        handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            KIND_IMPORT: self.run_import,
            KIND_REPROCESS: self.run_reprocess,
            KIND_INDEX: self.run_index,
            KIND_OCR_CANDIDATE: self.run_candidates,
        }
        handler = handlers.get(job["kind"])
        if handler is None:
            self._job_call("finish", job_id, STATE_FAILED, error=f"未知任务类型：{job['kind']}")
            return
        state = handler(job)
        self._job_call("finish", job_id, state)

    def _checkpoint(self, job_id: str, detail: str = "") -> bool:
        """页面提交后的安全点：续租、刷计数、检查暂停请求。"""
        self._job_call("renew_lease", job_id, lease_seconds=self.lease_seconds)
        self._job_call("refresh_counters", job_id)
        if self.store.pause_requested(job_id):
            return True
        if detail:
            self._job_call("set_stage", job_id, detail)
        return False

    # ------------------------------------------------------------ 导入

    def run_import(self, job: dict[str, Any]) -> str:
        job_id = job["id"]
        staging_id = job["staging_id"]
        staging = self.store.staging(staging_id)
        staged = self.workspace.root / staging["stored_path"]
        if not staged.exists():
            raise LitError(f"上传文件已丢失：{staging['filename']}")

        self._job_call("set_stage", job_id, "校验文件", state="preparing")
        digest = sha256_file(staged)
        if staging["sha256"] and staging["sha256"] != digest:
            raise LitError("上传文件在暂存期间发生变化，请重新上传。")
        suffix = _check_suffix(staging["filename"])

        self._job_call("set_stage", job_id, "保存原件")
        document = self._prepare_document(job, staging, staged, digest, suffix)
        path = self.workspace.path_of(document)
        self._job_call("set_stage", job_id, "处理页面", state="running")

        if document["filetype"] == "docx":
            state = self._run_docx(job_id, path, document["id"], job["ocr_mode"])
        else:
            state = self._run_pdf(job_id, path, document["id"], job["ocr_mode"])
        if state == STATE_COMPLETED:
            # 只有真正跑完才清理上传暂存；暂停/部分完成/失败必须保留，
            # 否则「继续」或「重试失败页」会因为文件已被删掉而无法恢复。
            with self._execution(job_id) as conn:
                self.store.drop_staging(staging_id, connection=conn)
                try:
                    staged.unlink()
                except OSError:
                    pass
        else:
            with self._execution(job_id) as conn:
                self.store.drop_staging(staging_id, status="retained", connection=conn)
        return state

    def _prepare_document(self, job: dict[str, Any], staging: dict[str, Any],
                          staged: Path, digest: str, suffix: str) -> dict[str, Any]:
        """保存不可变原件并确定稳定文献 ID；重复内容关联已有文献。"""
        existing = self.workspace.document_by_hash(digest)
        if existing:
            _store_copy(self.workspace, staged, digest, suffix)
            self._job_call("update_fields", job["id"], doc_id=existing["id"], duplicate=True)
            return existing
        stored = _store_copy(self.workspace, staged, digest, suffix)
        doc_id = _doc_id(digest)
        now = utcnow()
        document = {
            "id": doc_id,
            "sha256": digest,
            # 用户看到的文件名始终是上传时的原始文件名，不是暂存 ID
            "original_path": str(staged),
            "stored_path": stored.relative_to(self.workspace.root).as_posix(),
            "filename": staging["filename"],
            "filetype": suffix.lstrip("."),
            "title": "",
            "author": "",
            "year": "",
            "edition": "",
            "volume": "",
            "status": "importing",
            "ocr_mode": job["ocr_mode"],
            "page_count": None,
            "unit_count": 0,
            "pending_count": 0,
        }
        with self._execution(job["id"]) as conn:
            # A concurrent import may have created this stable hash while we copied.
            found = conn.execute("SELECT id FROM documents WHERE sha256=?", (digest,)).fetchone()
            if found is None:
                self.workspace.save_document(document, connection=conn)
            self._job_call("update_fields", job["id"], doc_id=doc_id, connection=conn)
        return self.workspace.get_document(doc_id)

    def _run_pdf(self, job_id: str, path: Path, doc_id: str, ocr_mode: str) -> str:
        with open_pdf_pages(self.workspace, path, doc_id, ocr_mode, ocr_page=self._background_ocr) as pages:
            if pages.page_count == 0:
                raise LitError(f"PDF 无法解析：{path.name}（文件可能已损坏或被加密）")
            # 页数与元数据立刻落盘：中断/暂停后覆盖统计仍然知道真实页数。
            updates: dict[str, Any] = {"page_count": pages.page_count, "status": "importing"}
            current = self.workspace.get_document(doc_id)
            if pages.metadata.get("title") and not current["title"]:
                updates["title"] = pages.metadata["title"]
            if pages.metadata.get("author") and not current["author"]:
                updates["author"] = pages.metadata["author"]
            with self._execution(job_id) as conn:
                self.workspace.update_document(doc_id, connection=conn, **updates)
            self._job_call("update_fields", job_id, total_pages=pages.page_count)
            self._job_call("plan_units", 
                job_id, [_unit_id(doc_id, "page", index + 1) for index in range(pages.page_count)]
            )
            self._reuse_completed(job_id, doc_id)
            # 只处理检查点里还没成功的页：暂停、崩溃恢复、失败重试都不会重做成功页。
            todo = self._todo_set(job_id)
            for index in range(pages.page_count):
                if _unit_id(doc_id, "page", index + 1) not in todo:
                    continue
                self._process_page(job_id, pages, index, doc_id, ocr_mode)
                if self._checkpoint(job_id, f"第 {index + 1}/{pages.page_count} 页"):
                    return STATE_PAUSED
        self._finalize_document(doc_id, job_id)
        return self._state_from_units(doc_id)

    def _todo_set(self, job_id: str) -> set[str]:
        return {row["unit_id"] for row in self.store.todo_units(job_id)}

    def _reuse_completed(self, job_id: str, doc_id: str) -> None:
        """重复导入复用已提交的可用页；强制重识别不走这个入口。"""
        with self._execution(job_id) as conn:
            conn.execute(
                "UPDATE job_units SET state='done', error='' WHERE job_id=? AND state!='done' "
                "AND unit_id IN (SELECT id FROM units WHERE doc_id=? AND status='completed' "
                "AND (blank_confirmed=1 OR CASE WHEN corrected_text IS NOT NULL "
                "THEN search_text_corrected ELSE search_text END != ''))",
                (job_id, doc_id),
            )

    def _process_page(self, job_id: str, pages: Any, index: int, doc_id: str,
                      ocr_mode: str) -> None:
        """处理一页：落盘 -> 索引 -> 按**真实结局**记录检查点。

        * 拿到文字      -> ``done``（成功，不再重做）
        * 处理了但没字   -> ``pending``（可重试，绝不算成功）
        * 出错          -> ``failed``（可重试）
        """
        unit_id = _unit_id(doc_id, "page", index + 1)
        try:
            block = pages.block_for(index, ocr_mode=ocr_mode)
        except ClaimLost:
            raise
        except Exception as exc:
            # 抛异常时这一页可能连单元行都还没有：落一个真实的失败占位，
            # 否则它会从计数里消失，任务会被误报成「完成」。
            self._mark_page_failed(job_id, doc_id, index + 1,
                                   f"{type(exc).__name__}: {exc}")
            return
        status = block.get("status")
        if status == UnitStatus.FAILED:
            # _ocr_page 返回 failed：这是真的失败，不是成功。
            with self._execution(job_id) as conn:
                _save_extraction(self.workspace, block, connection=conn)
                self._job_call("finish_unit", job_id, unit_id, "failed", block.get("notes", ""), connection=conn)
            return
        with self._execution(job_id) as conn:
            _save_extraction(self.workspace, block, connection=conn)
            self._record_block_result(job_id, unit_id, block, connection=conn)
        # 索引与文字提交分离：索引失败可以重试，文字结果不会因此丢失
        #（触发器已把该单元标为 dirty，下次索引任务会补上）。
        try:
            with self._execution(job_id) as conn:
                search_index.index_unit(self.workspace, unit_id, connection=conn)
        except ClaimLost:
            raise
        except Exception as exc:  # pragma: no cover - 索引异常不该丢掉文字
            self._job_call("set_stage", job_id, f"索引待补：{type(exc).__name__}")

    def _record_block_result(self, job_id: str, unit_id: str, block: dict[str, Any], *, connection=None) -> None:
        """按单元状态记录检查点：只有真的有文字才算 ``done``。"""
        status = block.get("status")
        checkpoint = "done" if status == UnitStatus.COMPLETED else "pending"
        self._job_call("finish_unit", job_id, unit_id, checkpoint,
                               "" if checkpoint == "done" else (block.get("notes") or ""), connection=connection)

    def _mark_page_failed(self, job_id: str, doc_id: str, file_page: int,
                          reason: str) -> None:
        unit_id = _unit_id(doc_id, "page", int(file_page))
        try:
            existing = self.workspace.get_unit(unit_id)
        except NotFoundError:
            existing = None
        with self._execution(job_id) as conn:
            if existing is None:
                # 补一条真实的失败单元（原件不变，只是这一页没有可用文字）
                _save_extraction(
                    self.workspace,
                    failed_page_block(doc_id, file_page, f"识别失败：{reason}"), connection=conn,
                )
            else:
                # 已有结果：只标失败，**不覆盖**之前的文字或人工校订
                self.workspace.mark_unit_failed(unit_id, f"识别失败：{reason}", connection=conn)
            self._job_call("finish_unit", job_id, unit_id, "failed", reason, connection=conn)

    def _run_docx(self, job_id: str, path: Path, doc_id: str, ocr_mode: str) -> str:
        processed = 0
        for block, metadata in _iter_docx_blocks(self.workspace, path, doc_id, ocr_mode):
            if metadata:
                current = self.workspace.get_document(doc_id)
                changes = {key: value for key, value in metadata.items()
                           if value and not current.get(key)}
                if changes:
                    with self._execution(job_id) as conn:
                        self.workspace.update_document(doc_id, connection=conn, **changes)
            if not block.get("id"):
                continue
            self._job_call("plan_units", job_id, [block["id"]])
            self._reuse_completed(job_id, doc_id)
            if block["id"] not in self._todo_set(job_id):
                continue
            processed += 1
            with self._execution(job_id) as conn:
                _save_extraction(self.workspace, block, connection=conn)
                self._record_block_result(job_id, block["id"], block, connection=conn)
                search_index.index_unit(self.workspace, block["id"], connection=conn)
            if self._checkpoint(job_id, f"第 {processed} 个文本单元"):
                return STATE_PAUSED
        self._finalize_document(doc_id, job_id)
        return self._state_from_units(doc_id)

    def _finalize_document(self, doc_id: str, job_id: str) -> None:
        from litworkbench.ingest import _final_status

        with self._execution(job_id) as conn:
            updates = _final_status(self.workspace, doc_id, "", connection=conn)
            document = self.workspace.get_document(doc_id, connection=conn)
            if not document["title"]:
                updates["title"] = Path(document["filename"]).stem
            self.workspace.update_document(doc_id, connection=conn, **updates)

    def _state_from_units(self, doc_id: str) -> str:
        """任务的最终状态：只看数据库里的真实结局。

        失败页、待处理页、**以及根本没有单元行的缺失页**都不算完成；
        全部页都失败时任务本身就是失败，不会因为「别的页成功过」而被粉饰。
        """
        document = self.workspace.get_document(doc_id)
        units = self.workspace.units_for_doc(doc_id)
        if not units:
            return STATE_FAILED
        failed = [unit for unit in units if unit["status"] == UnitStatus.FAILED]
        pending = [unit for unit in units if unit["status"] == UnitStatus.PENDING]
        completed = [unit for unit in units if unit["status"] == UnitStatus.COMPLETED]
        page_count = int(document.get("page_count") or 0) if document["filetype"] == "pdf" else 0
        missing = max(0, page_count - len(units))
        if not completed and (failed or pending or missing):
            return STATE_FAILED
        if failed or pending or missing:
            return STATE_PARTIAL
        return STATE_COMPLETED

    # ------------------------------------------------------------ 重新识别

    def run_reprocess(self, job: dict[str, Any]) -> str:
        job_id = job["id"]
        doc_id = job["doc_id"]
        document = self.workspace.get_document(doc_id)
        path = self.workspace.path_of(document)
        if document["filetype"] != "pdf":
            raise LitError("重新识别目前只支持 PDF 页；Word 请使用校订。")
        total = self._job_call("refresh_counters", job_id)["total_pages"] or 0
        position = 0
        for entry in self.store.todo_units(job_id):
            position += 1
            unit = self.workspace.get_unit(entry["unit_id"])
            file_page = int(unit["file_page"] or 0)
            try:
                block = force_page_block(self.workspace, path, doc_id, file_page, "force", ocr_page=self._background_ocr)
            except ClaimLost:
                raise
            except Exception as exc:
                # 失败绝不能毁掉旧的可用结果：只记失败，不动单元。
                self._job_call("finish_unit", job_id, unit["id"], "failed",
                                       f"{type(exc).__name__}: {exc}")
                if self._checkpoint(job_id, f"第 {position}/{total} 页失败"):
                    return STATE_PAUSED
                continue
            if block.get("status") != UnitStatus.COMPLETED:
                # 识别为空或引擎不可用同样算失败：保留旧结果，交由重试或人工处理。
                self._job_call("finish_unit", job_id, unit["id"], "failed",
                                       block.get("notes") or "强制识别没有产出文字")
                if self._checkpoint(job_id, f"第 {position}/{total} 页失败"):
                    return STATE_PAUSED
                continue
            # 提交新版本：人工校订、历史与证据关系由 _save_extraction 保留，
            # 旧版本证据按哈希变化进入「需复核」。
            fresh = dict(unit)
            fresh.update({key: block[key] for key in
                          ("method", "status", "original_text", "ocr_json", "notes",
                           "ocr_decision") if key in block})
            fresh["corrected_text"] = unit.get("corrected_text")
            fresh["printed_page"] = unit.get("printed_page")
            with self._execution(job_id) as conn:
                _save_extraction(self.workspace, fresh, connection=conn)
                search_index.index_unit(self.workspace, unit["id"], connection=conn)
                self._job_call("finish_unit", job_id, unit["id"], "done", connection=conn)
            if self._checkpoint(job_id, f"第 {position}/{total} 页"):
                return STATE_PAUSED
        counts = self._job_call("refresh_counters", job_id)
        return STATE_PARTIAL if counts["failed_pages"] else STATE_COMPLETED

    # ------------------------------------------------------------ 索引

    def run_candidates(self, job):
        from litworkbench.ocr_candidates import CandidateStore
        candidates = CandidateStore(self.workspace)
        job_id = job["id"]
        for entry in self.store.todo_units(job_id):
            self._heartbeat(job_id)
            attempt = None

            def infer(checkpoint):
                nonlocal attempt
                # Snapshot after waiting for the gate, immediately before this actual attempt.
                attempt = candidates.begin_attempt(job_id, entry["unit_id"], owner=self.owner_id,
                                                    generation=self._generation)
                unit = self.workspace.get_unit(entry["unit_id"])
                return self.run_page(self.workspace, unit, profile_id=attempt["profile_id"], checkpoint=checkpoint)

            try:
                value = self._with_ocr_gate(job_id, infer)
                if "ok" in value and not value["ok"]:
                    candidates.fail_attempt(attempt["id"], value.get("error") or "识别失败",
                                             owner=self.owner_id, generation=self._generation)
                else:
                    candidates.complete_attempt(attempt["id"], self._result(value),
                                                 owner=self.owner_id, generation=self._generation)
            except ClaimLost:
                raise
            except Exception as exc:
                if attempt is not None:
                    candidates.fail_attempt(attempt["id"], {"kind": type(exc).__name__, "message": str(exc)},
                                             owner=self.owner_id, generation=self._generation)
                else:
                    self._job_call("finish_unit", job_id, entry["unit_id"], "failed", str(exc))
            if self._checkpoint(job_id, f"文件第 {self.workspace.get_unit(entry['unit_id'])['file_page']} 页"):
                return STATE_PAUSED
        counts = self._job_call("refresh_counters", job_id)
        if counts["failed_pages"]:
            return STATE_PARTIAL if counts["completed_pages"] else STATE_FAILED
        return STATE_COMPLETED

    def run_index(self, job: dict[str, Any]) -> str:
        job_id = job["id"]
        with self.workspace.connect() as conn:
            pending = search_index.pending_unit_ids(conn, limit=100000)
        self._job_call("plan_units", job_id, pending)
        todo = self._todo_set(job_id)
        total = self._job_call("refresh_counters", job_id)["total_pages"] or 0
        position = 0
        for unit_id in list(todo):
            position += 1
            try:
                with self._execution(job_id) as conn:
                    search_index.index_unit(self.workspace, unit_id, connection=conn)
                    self._job_call("finish_unit", job_id, unit_id, "done", connection=conn)
            except ClaimLost:
                raise
            except Exception as exc:  # pragma: no cover - 索引失败可重试
                self._job_call("finish_unit", job_id, unit_id, "failed",
                                       f"{type(exc).__name__}: {exc}")
            if self._checkpoint(job_id, f"第 {position}/{total} 个单元"):
                return STATE_PAUSED
        counts = self._job_call("refresh_counters", job_id)
        return STATE_PARTIAL if counts["failed_pages"] else STATE_COMPLETED


def _check_suffix(filename: str) -> str:
    from litworkbench.ingest import SUPPORTED_EXTENSIONS

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileError(
            f"不支持的文件类型：{suffix or '（无扩展名）'}；当前仅支持 "
            f"{'、'.join(SUPPORTED_EXTENSIONS)}。"
        )
    return suffix


def run_worker(workspace_root: Path | str, *, stop_event: Any = None,
               max_jobs: int | None = None, poll_interval: float = POLL_INTERVAL,
               owner_id: str | None = None, recovery_interval: float = 10.0) -> dict[str, Any]:
    """主循环：领取 -> 执行 -> 直到停止标记/停止事件/达到 max_jobs。"""
    workspace = Workspace.open(workspace_root)
    worker = Worker(workspace, owner_id=owner_id)
    marker = stop_marker_path(workspace.root)
    processed: list[dict[str, Any]] = []
    worker.store.recover_stale()
    last_recovery = time.monotonic()
    try:
        while True:
            if marker.exists():
                break
            if stop_event is not None and stop_event.is_set():
                break
            job = worker.tick()
            if job is None:
                if max_jobs is not None and len(processed) >= max_jobs:
                    break
                # 空闲时定期收拢中断的任务：执行者崩溃不需要重启服务。
                now = time.monotonic()
                if now - last_recovery >= recovery_interval:
                    worker.store.recover_stale()
                    last_recovery = now
                time.sleep(poll_interval)
                continue
            processed.append(job)
            last_recovery = time.monotonic()
            if max_jobs is not None and len(processed) >= max_jobs:
                break
    finally:
        ocr_mod.shutdown()
    return {"processed": processed, "count": len(processed)}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 子进程入口
    parser = argparse.ArgumentParser(
        prog="litworkbench-worker",
        description="文献工作台后台识别执行者（由网页服务启动，一般不需要手动运行）",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL)
    parser.add_argument("--max-jobs", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        run_worker(args.workspace, poll_interval=args.poll_interval, max_jobs=args.max_jobs)
    except LitError as exc:
        sys.stderr.write(f"后台执行者启动失败：{exc}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
