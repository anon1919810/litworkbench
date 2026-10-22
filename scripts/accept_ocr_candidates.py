#!/usr/bin/env python3
"""Task D 内部验收：在**独立克隆**上生成/采用真实 OCR 候选（准备脚本）。

* 默认 dry-run：只读校验 + 快照，绝不 ``Workspace.open``（不迁移、不写克隆、不入队）。
* ``--execute``：仅在**通过全部写前守卫**后打开克隆、迁移、生成/采用候选并写报告；
  任一守卫失败即拒绝执行（不迁移、不入队），绝不伪造 OCR/历史文本自圆其说。
* 只用同一套产品 API（CandidateStore / Worker / evidence / collections / search_index /
  ingest.search）与标准库；不 import 评测脚本，不启服务器，不装依赖、不下模型。

用法与判定见 ``docs/ocr-candidate-acceptance.md``。真实 12 页只在 B1 集成后由 root 用
``--execute`` 运行；当前仅应通过 ``--help`` / dry-run / 守卫单测验证。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

CLONE_PARENT = "ocr-candidates-2026-10-16"
CLONE_BASENAME = "acceptance-library"
WORK_BASENAME = "work"
EXPECTED_DOCS, EXPECTED_UNITS, EXPECTED_SAMPLES, EXPECTED_SOURCES = 6, 91, 12, 6
PROFILE_ID, LAYOUT_ID = "small-lowmem-v1", "original"
JSON_NAME, MD_NAME = "taskd-acceptance.json", "taskd-acceptance.md"
TERMINAL_JOB_STATES = ("completed", "partial", "failed", "paused")
MAX_TICKS = 200

FIXTURE_BOUNDARIES = {
    "test_fixtures": "tests/ 用 litworkbench.demo 合成 PDF 与注入/伪 OCR，只证明编排与契约，不证明真实 RapidOCR/模型。",
    "real_boundary": "本脚本在冻结克隆上跑真实 CandidateStore + Worker + 本机已装 RapidOCR；不注入 executor、不伪造结果。",
    "browser": "网页交互（生成/等待/对照/采用/保留/409/失败说明、截图）由 root 用真实 API + 浏览器另行验证，本脚本不覆盖。",
    "supplemental": "为覆盖校订/空白分支额外生成的候选属补充场景，计数与冻结 12 页分开报告。",
}

class GuardError(RuntimeError):
    """安全边界不满足：一律失败封闭，不做任何写入。"""

# ------------------------------------------------------------------ 只读工具

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents

def load_samples(path: Path) -> dict:
    path = Path(path)
    if not path.is_file() or path.name != "samples.json":
        raise GuardError(f"samples 必须是存在的 samples.json：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise GuardError(f"samples 无法解析：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise GuardError("samples 结构异常：缺少 samples 列表")
    return data

def load_config(clone: Path) -> dict:
    path = clone / "config.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except Exception as exc:  # noqa: BLE001
        raise GuardError(f"克隆 config.toml 解析失败：{exc}") from exc

def db_path_for(clone: Path) -> Path:
    """config.toml 的 db_file 必须解析到克隆目录之内（否则 fail-closed）。"""
    name = str(load_config(clone).get("workspace", {}).get("db_file", "litworkbench.sqlite3"))
    if Path(name).is_absolute():
        raise GuardError("config.toml 的 db_file 不能是绝对路径")
    resolved = (clone / name).resolve()
    if clone not in resolved.parents:
        raise GuardError(f"config.toml 的 db_file 逃逸出克隆：{resolved}")
    return resolved

# ------------------------------------------------------------------ 安全守卫（可单测）

def default_clone_root() -> Path:
    """仓库旁的 ``<work>/ocr-candidates-2026-10-16/acceptance-library``。"""
    repo = Path(__file__).resolve().parents[1]
    return repo.parent.parent / WORK_BASENAME / CLONE_PARENT / CLONE_BASENAME

def resolve_clone(raw, expected_root=None) -> Path:
    """解析并要求路径**精确等于**唯一批准的克隆；否则 fail-closed。"""
    if raw is None:
        raise GuardError("必须显式提供 --workspace（克隆路径）。")
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise GuardError(f"--workspace 不是目录：{path}")
    root = path.resolve()
    expected = Path(expected_root).expanduser().resolve() if expected_root else default_clone_root()
    if root != expected:
        raise GuardError(f"--workspace 必须是唯一批准克隆 {expected}，实际 {root}")
    if root.name.casefold() != CLONE_BASENAME or root.parent.name.casefold() != CLONE_PARENT:
        raise GuardError(f"克隆目录形状不符：{root}")
    if not (root / "config.toml").is_file():
        raise GuardError(f"克隆缺少 config.toml：{root}")
    if not db_path_for(root).is_file():
        raise GuardError(f"克隆缺少 SQLite：{db_path_for(root)}")
    return root

def assert_not_real_library(clone: Path, library_root=None) -> None:
    """拒绝真实库 outputs/library 及其内部路径，也拒绝任何原始书籍目录。"""
    if "/outputs/library" in clone.as_posix().casefold():
        raise GuardError(f"拒绝在真实库上运行：{clone}")
    if library_root:
        real = Path(library_root).expanduser().resolve()
        if _within(clone, real):
            raise GuardError(f"--workspace 位于真实库 {real} 之内，拒绝运行。")

def expected_source_hashes(samples: dict) -> dict:
    """冻结样本里唯一来源路径 -> 期望 SHA-256；必须恰好 6 个、字符集合法。"""
    pairs: dict[str, str] = {}
    for sample in samples["samples"]:
        rel, digest = sample.get("source_relative_path"), sample.get("source_sha256")
        if not rel or not isinstance(digest, str) or len(digest) != 64:
            raise GuardError(f"样本缺少合法来源路径/哈希：{sample.get('sample_id')}")
        pairs[rel] = digest
    if len(pairs) != EXPECTED_SOURCES or len(set(pairs.values())) != EXPECTED_SOURCES:
        raise GuardError(f"期望 {EXPECTED_SOURCES} 个唯一来源，实际 {len(pairs)}（哈希需唯一）")
    return pairs

def validate_source_paths(clone: Path, samples: dict) -> list:
    """每个 source_relative_path 必须落在克隆 sources/ 内且不是符号链接。"""
    sources = (clone / "sources").resolve()
    problems = []
    for sample in samples["samples"]:
        target = clone / (sample.get("source_relative_path") or "")
        if target.is_symlink() or sources not in target.resolve().parents:
            problems.append(str(sample.get("sample_id")))
    return problems

def verify_clone_sources(clone: Path, expected: dict, sha256=sha256_file) -> tuple:
    results, ok = {}, True
    for rel, want in sorted(expected.items()):
        path = clone / rel
        got = sha256(path) if path.is_file() else None
        results[rel] = {"expected": want, "actual": got, "ok": got == want}
        ok = ok and got == want
    return ok, results

def resolve_output(raw, clone: Path) -> Path:
    """输出目录必须位于批准目录（克隆父目录）之下，且不落在克隆/sources 内、无符号链接。"""
    approved = clone.parent
    target = Path(raw).expanduser().resolve() if raw else approved
    if target != approved and approved not in target.parents:
        raise GuardError(f"输出目录必须在批准目录 {approved} 之下，实际 {target}")
    if _within(target, clone):
        raise GuardError(f"输出目录不能位于克隆（含 sources）之内：{target}")
    node = target
    while True:
        if node.is_symlink():
            raise GuardError(f"输出目录含符号链接：{node}")
        if node == approved:
            break
        node = node.parent
    return target

# ------------------------------------------------------------------ 只读快照

def snapshot_state(conn: sqlite3.Connection) -> dict:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    snap = {
        "meta": {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")},
        "units": {}, "evidence": {}, "documents": {}, "collections": [],
        "collection_evidence": [], "jobs": {},
    }
    for row in conn.execute("SELECT id, sha256, filetype FROM documents"):
        snap["documents"][row[0]] = {"sha256": row[1], "filetype": row[2]}
    cols = {row[1] for row in conn.execute("PRAGMA table_info(units)")}
    rev = "content_revision" if "content_revision" in cols else "0 AS content_revision"
    for row in conn.execute(
        f"SELECT id, doc_id, file_page, status, original_text, corrected_text, {rev} FROM units"
    ):
        snap["units"][row[0]] = {
            "doc_id": row[1], "file_page": row[2], "status": row[3],
            "original_sha256": sha256_text(row[4] or ""),
            "corrected_sha256": None if row[5] is None else sha256_text(row[5]),
            "content_revision": int(row[6] or 0),
        }
    if "evidence" in tables:
        for row in conn.execute(
            "SELECT id, unit_id, review_status, quote, version_snapshot FROM evidence"
        ):
            snap["evidence"][row[0]] = {"unit_id": row[1], "review_status": row[2],
                                        "quote_sha256": sha256_text(row[3] or ""),
                                        "snapshot_sha256": sha256_text(row[4] or "")}
    if "collections" in tables:
        snap["collections"] = sorted(row[0] for row in conn.execute("SELECT id FROM collections"))
        snap["collection_evidence"] = sorted(
            [row[0], row[1]] for row in conn.execute("SELECT collection_id, evidence_id FROM collection_evidence")
        )
    if "jobs" in tables:
        for row in conn.execute("SELECT id, kind, state FROM jobs"):
            snap["jobs"][row[0]] = {"kind": row[1], "state": row[2]}
    return snap

def open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def read_snapshot(ws) -> dict:
    with ws.connect() as conn:
        return snapshot_state(conn)

def _snapshot_diff(before: dict, after: dict, keys: tuple) -> dict:
    diff = {}
    for key in keys:
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            diff[key] = {"count": len(changed), "ids": changed[:50]}
        else:
            diff[key] = {"changed": True}
    return diff

# ------------------------------------------------------------------ 报告收集

class Acceptance:
    def __init__(self, mode: str) -> None:
        self.mode, self.stages, self.failures, self.warnings, self.scenarios = mode, {}, [], [], []

    def warn(self, stage: str, message: str) -> None:
        self.warnings.append({"stage": stage, "message": message})

    def fail(self, stage: str, ident: str, reason: str) -> None:
        self.failures.append({"stage": stage, "id": ident, "reason": reason})

    def check(self, ok: bool, stage: str, ident: str, detail: str = "") -> bool:
        if not ok:
            self.fail(stage, ident, detail or ident)
        return bool(ok)

    def scenario(self, name: str, kind: str, stage=None, detail: str = "", status=None) -> None:
        """status 缺省时由该阶段的失败推导（避免失败仍记 pass）。"""
        if status is None:
            status = "fail" if any(f["stage"] == stage for f in self.failures) else "pass"
        self.scenarios.append({"name": name, "kind": kind, "status": status, "detail": detail})

    def guard_stage(self, stage: str, fn, *args):
        """运行一个阶段并把意外异常记入结构化报告（不中断其余阶段）。"""
        try:
            return fn(self, *args)
        except Exception as exc:  # noqa: BLE001
            self.fail(stage, "stage_error", f"{type(exc).__name__}: {exc}")
            return None

    @property
    def passed(self) -> bool:
        return not self.failures

# ------------------------------------------------------------------ dry-run 结构校验（写前守卫）

def structural_checks(acc, clone, samples, snap, src_ok, src_detail, expected) -> None:
    stage = "stage1_clone"
    docs, units, items = snap["documents"], snap["units"], samples["samples"]
    acc.check(src_ok, stage, "source_hashes", "克隆来源 SHA-256 与冻结样本不一致")
    acc.check(len(docs) == EXPECTED_DOCS, stage, "document_count", f"期望 {EXPECTED_DOCS} 篇，实际 {len(docs)}")
    acc.check(len(units) == EXPECTED_UNITS, stage, "unit_count", f"期望 {EXPECTED_UNITS} 页，实际 {len(units)}")
    acc.check(len(items) == EXPECTED_SAMPLES, stage, "sample_count", f"期望 {EXPECTED_SAMPLES} 个样本")
    acc.check({d["sha256"] for d in docs.values()} == set(expected.values()), stage, "document_hashes",
              "克隆文档哈希与样本来源哈希不一致")
    index = {(u["doc_id"], u["file_page"]): uid for uid, u in units.items()}
    missing = [s["sample_id"] for s in items if (s["doc_id"], s["file_page"]) not in index]
    acc.check(not missing, stage, "sample_units_resolvable", "样本页在克隆中找不到单元：" + "、".join(missing[:12]))
    acc.scenario("clone_shape", "frozen", stage=stage, detail=f"{len(docs)} 篇/{len(units)} 页/{len(items)} 样本")
    acc.stages[stage] = {
        "workspace": str(clone), "samples": len(items), "documents": len(docs), "units": len(units),
        "schema_version": snap["meta"].get("schema_version"), "index_revision": snap["meta"].get("index_revision"),
        "source_hash_ok": src_ok, "source_hash_detail": src_detail,
    }

def run_dry(acc, clone, samples, snap, src_ok, src_detail, expected) -> None:
    structural_checks(acc, clone, samples, snap, src_ok, src_detail, expected)
    acc.stages["stage1_clone"]["note"] = "dry-run：只读，未 Workspace.open/迁移/写克隆/生成候选。"

# ------------------------------------------------------------------ execute：真实阶段

def _tick_until_terminal(acc, ws, job_ids, stage) -> dict:
    from litworkbench.jobs import JobStore
    from litworkbench.worker import Worker

    store, worker, expected = JobStore(ws), Worker(ws, owner_id="taskd-acceptance"), set(job_ids)
    for _ in range(MAX_TICKS):
        states = {jid: store.get(jid)["state"] for jid in job_ids}
        if all(state in TERMINAL_JOB_STATES for state in states.values()):
            return states
        finished = worker.tick()
        if finished is None:
            acc.warn(stage, "没有可领取任务但候选任务未终结；停止以避免空转")
            break
        if finished["id"] not in expected:
            acc.warn(stage, f"领取到非预期任务 {finished['id']}；停止")
            break
    return {jid: store.get(jid)["state"] for jid in job_ids}

def _candidate_full(ws, candidate_id: str) -> dict:
    with ws.connect() as conn:
        row = conn.execute("SELECT * FROM ocr_candidates WHERE id = ?", (candidate_id,)).fetchone()
    return dict(row) if row else {}

def _snapshot_rows(ws, candidate_id: str) -> list:
    with ws.connect() as conn:
        rows = conn.execute(
            "SELECT old_original_text FROM ocr_candidate_snapshots WHERE candidate_id = ?", (candidate_id,)
        ).fetchall()
    return [row[0] for row in rows]

def _slice_quote(text: str, size: int = 8):
    if not text:
        return None
    start = len(text) // 2
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text):
        start = 0
    quote = text[start:start + size]
    return (quote, start) if quote.strip() else None

def _distinct_window(text: str, other: str, size: int = 6):
    # Search deliberately ignores whitespace; a newline-only change is not a removed term.
    from litworkbench.text import normalize
    text, other = normalize(text), normalize(other)
    for i in range(0, max(0, len(text) - size + 1)):
        window = text[i:i + size]
        if len(window) == size and window.strip() and window not in other:
            return window
    return None

def verify_model_fingerprints(candidates: list) -> tuple:
    """独立重算候选声明的模型文件 SHA；缺路径或对不上都算问题（fail-closed）。"""
    entries, problems = [], []
    for cand in candidates:
        fingerprints = cand.get("model_fingerprints") or []
        if not fingerprints:
            problems.append((cand["candidate_id"], "no_model_fingerprints"))
            continue
        for fingerprint in fingerprints:
            if not isinstance(fingerprint, dict):
                continue
            path, recorded = fingerprint.get("path"), fingerprint.get("sha256")
            if not recorded:
                problems.append((cand["candidate_id"], "model_fingerprint_without_sha256"))
            elif not path:
                entries.append({"candidate_id": cand["candidate_id"], "path": None, "recorded": recorded, "ok": False})
                problems.append((cand["candidate_id"], "model_sha_unverifiable_no_path"))
            else:
                actual = sha256_file(Path(path)) if Path(path).is_file() else None
                entries.append({"candidate_id": cand["candidate_id"], "path": str(path),
                                "recorded": recorded, "actual": actual, "ok": actual == recorded})
                if actual != recorded:
                    problems.append((cand["candidate_id"], f"model_sha_mismatch:{Path(path).name}"))
    return entries, problems

def stage_generation(acc, ws, samples, baseline) -> dict:
    from litworkbench.ocr_candidates import CandidateStore, LAYOUT_ORIGINAL, PROFILE_SMALL_LOWMEM

    stage = "stage2_generation"
    store = CandidateStore(ws)
    index = {(u["doc_id"], u["file_page"]): uid for uid, u in baseline["units"].items()}
    per_doc: dict = {}
    for s in samples["samples"]:
        per_doc.setdefault(s["doc_id"], []).append(s)
    jobs, sample_unit = {}, {}
    for doc_id, items in per_doc.items():
        unit_ids = []
        for s in items:
            sample_unit[s["sample_id"]] = index.get((s["doc_id"], s["file_page"]))
            if sample_unit[s["sample_id"]]:
                unit_ids.append(sample_unit[s["sample_id"]])
        jobs[doc_id] = store.create_job(
            doc_id, unit_ids, profile_id=PROFILE_SMALL_LOWMEM, layout=LAYOUT_ORIGINAL)["id"]
    acc.check(len(jobs) == EXPECTED_DOCS and all(sample_unit.values()), stage, "jobs_created",
              "并非所有样本页都能入队（可能缺页或缺文献）")
    states = _tick_until_terminal(acc, ws, list(jobs.values()), stage)

    records, ready, failed = [], [], []
    for s in samples["samples"]:
        uid, job_id = sample_unit.get(s["sample_id"]), jobs.get(s["doc_id"])
        acc.check(bool(uid and job_id), stage, f"enqueued:{s['sample_id']}", "样本页未入队")
        if not uid:
            continue
        items = store.list_for_unit(uid, job_id=job_id, page_size=5)["items"] if job_id else []
        if not items:
            acc.fail(stage, f"missing_candidate:{s['sample_id']}", "该页没有候选记录（不算成功）")
            continue
        detail = store.get(items[0]["id"])
        raw = _candidate_full(ws, detail["id"])
        unit = ws.get_unit(uid)
        document = ws.get_document(s["doc_id"])
        result = detail.get("result") or {}
        record = {
            "sample_id": s["sample_id"], "doc_id": s["doc_id"], "file_page": s["file_page"],
            "unit_id": uid, "job_id": job_id, "candidate_id": detail["id"], "state": detail["state"],
            "base_revision": detail["base_content_revision"], "current_revision": int(unit["content_revision"]),
            "text_changed": bool(result.get("text")) and result["text"] != unit["original_text"],
            "has_correction": unit.get("corrected_text") is not None, "text": result.get("text"),
            "engine": result.get("engine"), "text_len": len(result.get("text") or ""),
            "model_fingerprints": result.get("model_fingerprints") or [], "error": detail.get("error"),
        }
        acc.check(detail["file_page"] == s["file_page"], stage, f"file_page:{s['sample_id']}", "候选文件页与样本不一致")
        acc.check(raw.get("source_sha256") == document["sha256"] == s["source_sha256"], stage,
                  f"source_sha256:{s['sample_id']}", "候选来源指纹与文献/样本不一致")
        acc.check(detail["base_content_revision"] == int(unit["content_revision"]), stage,
                  f"base_revision:{s['sample_id']}", "生成期间页面修订号已变化")
        if detail["state"] == "ready":
            acc.check(bool((result.get("text") or "").strip()), stage, f"empty_text:{s['sample_id']}", "ready 候选却是空文字")
            ready.append(record)
        elif detail["state"] == "failed":
            acc.check(bool((detail.get("error") or {}).get("kind")), stage, f"vague_failure:{s['sample_id']}",
                      "失败候选缺少真实错误类型")
            failed.append(record)
        else:
            acc.fail(stage, f"unexpected_state:{s['sample_id']}",
                     f"候选状态为 {detail['state']}，既非 ready 也非 failed")
        records.append(record)

    acc.check(len(records) == EXPECTED_SAMPLES, stage, "recorded_all", f"只记录了 {len(records)}/{EXPECTED_SAMPLES} 页")
    after = read_snapshot(ws)
    diff = _snapshot_diff(baseline, after, ("units", "evidence", "documents", "collections", "collection_evidence"))
    if baseline["meta"].get("index_revision") != after["meta"].get("index_revision"):
        diff["index_revision"] = {"changed": True}
    acc.check(not diff, stage, "generation_readonly",
              f"生成过程改动了现用数据：{json.dumps(diff, ensure_ascii=False)[:300]}")
    entries, problems = verify_model_fingerprints(ready)
    acc.check(not problems, stage, "model_sha",
              "模型指纹缺失或无法独立核对：" + "; ".join(f"{c}:{w}" for c, w in problems[:6]))
    acc.check(bool(entries), stage, "model_sha_present", "没有任何可独立核对的模型文件 SHA")
    acc.scenario("frozen_12_generation", "frozen", stage=stage,
                 detail=f"ready={len(ready)} failed={len(failed)} total={len(records)}")
    acc.stages[stage] = {
        "jobs": {doc: {"job_id": jid, "state": states.get(jid)} for doc, jid in jobs.items()},
        "records": records, "ready_count": len(ready), "failed_count": len(failed),
        "model_fingerprints": entries, "model_problems": problems,
    }
    return {"store": store, "jobs": jobs, "states": states, "records": records,
            "ready": ready, "failed": failed, "sample_unit": sample_unit}

def stage_adopt(acc, ws, gen, used) -> dict:
    from litworkbench import collections as col, evidence as ev, search_index
    from litworkbench.ingest import search as legacy_search

    stage = "stage3_adopt"
    target = next((r for r in gen["ready"]
                   if r["text_changed"] and not r["has_correction"] and r["unit_id"] not in used), None)
    if target is None:
        acc.scenario("frozen_changed_adopt", "frozen", status="unavailable",
                     detail="冻结候选里没有“文字确实变化且无校订”的页；不伪造候选/历史文本")
        acc.fail(stage, "scenario_unavailable", "无法演示采用路径（无匹配候选）")
        return {}
    used.add(target["unit_id"])
    uid, cid = target["unit_id"], target["candidate_id"]
    unit0 = ws.get_unit(uid)
    old_text, rev0 = unit0["original_text"], int(unit0["content_revision"])
    quote = _slice_quote(old_text)
    acc.check(quote is not None, stage, "quote_slice", "无法从原文取精确切片")
    if quote is None:
        return {}
    evidence_row = ev.save_evidence(ws, uid, quote[0], category="task-d-acceptance",
                                    note="采用前精确切片（验收夹具）", version="original",
                                    start_offset=quote[1], strict=True)
    ev.review_evidence(ws, evidence_row["id"], "accepted")
    col_id = col.create_collection(ws, name=f"taskD-{cid}")["id"]
    col.add_evidence(ws, col_id, [evidence_row["id"]])
    snapshot_before = ev.get_evidence(ws, evidence_row["id"])["version_snapshot"]
    revisions_before = len(ws.revisions_for_unit(uid))

    adopted = gen["store"].adopt(cid, expected_content_revision=rev0)
    unit1 = ws.get_unit(uid)
    new_text = target["text"] or ""
    acc.check(adopted["candidate"]["state"] == "adopted", stage, "candidate_adopted", "候选未进入 adopted")
    acc.check(unit1["original_text"] == new_text, stage, "original_updated", "机器底稿未更新为候选文字")
    acc.check(sha256_text(new_text) != sha256_text(old_text), stage, "hash_changed",
              "采用前后原文哈希一致，无法证明变更")
    acc.check(int(unit1["content_revision"]) > rev0, stage, "revision_advanced", "内容修订号未推进")
    revisions = ws.revisions_for_unit(uid)
    acc.check(len(revisions) == revisions_before + 1 and revisions[-1]["kind"] == "ocr-adopt"
              and revisions[-1]["old_text"] == old_text, stage, "history_prior_ocr", "未留下采用前完整机器底稿历史")
    snapshots = _snapshot_rows(ws, cid)
    acc.check(len(snapshots) == 1 and snapshots[0] == old_text, stage, "snapshot_preserved",
              "采用前旧 OCR 快照缺失或不一致")

    evidence_after = ev.get_evidence(ws, evidence_row["id"])
    acc.check(evidence_after["version_snapshot"] == snapshot_before, stage, "excerpt_snapshot_immutable",
              "摘录版本快照被改写")
    acc.check(evidence_after["review_status"] == "stale", stage, "excerpt_needs_review",
              "所用版本原文变了，摘录应变为待复核")

    search_index.rebuild_dirty(ws)
    with ws.connect() as conn:
        index_state = conn.execute("SELECT state FROM index_units WHERE unit_id = ?", (uid,)).fetchone()
    acc.check(index_state and index_state["state"] == "indexed", stage, "index_rebuilt", "索引未重建")
    new_query, old_query = _distinct_window(new_text, old_text), _distinct_window(old_text, new_text)
    if new_query:
        fts = search_index.search_indexed(ws, new_query, page_size=100)
        acc.check(any(h["unit_id"] == uid for h in fts["items"]), stage, "fts_new_hit", "FTS 未命中新文字")
        acc.check(any(h["unit_id"] == uid for h in legacy_search(ws, new_query, limit=500)), stage,
                  "legacy_new_hit", "旧检索未命中新文字")
    if old_query:
        fts_old = search_index.search_indexed(ws, old_query, page_size=100)
        acc.check(not any(h["unit_id"] == uid for h in fts_old["items"]), stage, "fts_old_miss",
                  "FTS 仍命中被替换的旧文字")
        acc.check(not any(h["unit_id"] == uid for h in legacy_search(ws, old_query, limit=500)), stage,
                  "legacy_old_miss", "旧检索仍命中被替换的旧文字")

    duplicate = gen["store"].adopt(cid, expected_content_revision=int(unit1["content_revision"]))
    acc.check(duplicate["candidate"]["state"] == "adopted" and len(ws.revisions_for_unit(uid)) == len(revisions)
              and len(_snapshot_rows(ws, cid)) == 1, stage, "duplicate_idempotent", "重复采用产生了额外历史或快照")
    acc.scenario("frozen_changed_adopt", "frozen", stage=stage,
                 detail=f"unit={uid} candidate={cid} quote={evidence_row['id']}")
    acc.stages[stage] = {"unit_id": uid, "candidate_id": cid, "collection_id": col_id,
                         "evidence_id": evidence_row["id"], "old_revision": rev0,
                         "new_revision": int(unit1["content_revision"])}
    return {"unit_id": uid, "candidate_id": cid}

def _supplemental_candidate(acc, ws, gen, doc_id, unit_id, stage):
    job = gen["store"].create_job(doc_id, [unit_id], profile_id=PROFILE_ID, layout=LAYOUT_ID)
    states = _tick_until_terminal(acc, ws, [job["id"]], stage)
    items = gen["store"].list_for_unit(unit_id, job_id=job["id"], page_size=5)["items"]
    if not items:
        acc.fail(stage, "supplemental_missing", "补充候选未生成")
        return None
    detail = gen["store"].get(items[0]["id"])
    acc.check(detail["state"] == "ready", stage, "supplemental_ready",
              f"补充候选状态为 {detail['state']}（job={states.get(job['id'])}）")
    return detail

def stage_correction(acc, ws, gen, used) -> dict:
    """校订保留 + 空白校订待定；两者都需在设置校订后再生成补充候选。"""
    from litworkbench import collections as col, evidence as ev

    results = {}
    candidates = [r for r in gen["ready"] if not r["has_correction"]]
    for name, empty in (("corrected_preserved", False), ("empty_correction_pending", True)):
        stage = f"stage4_{name}"
        target = next((r for r in candidates if r["unit_id"] not in used), None)
        if target is None:
            acc.scenario(name, "supplemental", status="unavailable", detail="没有可用于补充校订场景的候选页")
            acc.fail(stage, "scenario_unavailable", "补充校订场景无候选页可用")
            continue
        used.add(target["unit_id"])
        uid, doc_id = target["unit_id"], target["doc_id"]
        old_text = ws.get_unit(uid)["original_text"]
        correction = "" if empty else old_text
        ev.correct_unit(ws, uid, correction, f"Task D 验收夹具：{'空校订（待定）' if empty else '校订保持与旧机器底稿一致'}")
        collection_id = col.create_collection(ws, name=f"taskD-{name}-{uid[-4:]}")["id"]
        evidence_id = None
        if not empty:
            quote = _slice_quote(correction)
            if quote:
                row = ev.save_evidence(ws, uid, quote[0], version="corrected", start_offset=quote[1], strict=True,
                                       category="task-d-acceptance", note="Task D 验收夹具：校订版切片")
                ev.review_evidence(ws, row["id"], "accepted")
                col.add_evidence(ws, collection_id, [row["id"]])
                evidence_id = row["id"]
        detail = _supplemental_candidate(acc, ws, gen, doc_id, uid, stage)
        if detail is None:
            acc.scenario(name, "supplemental", stage=stage, detail="补充候选不可用")
            continue
        rev = int(ws.get_unit(uid)["content_revision"])
        adopted = gen["store"].adopt(detail["id"], expected_content_revision=rev)
        unit = ws.get_unit(uid)
        acc.check(adopted["candidate"]["state"] == "adopted", stage, "adopted", "补充候选未采用")
        acc.check(unit["original_text"] == ((detail.get("result") or {}).get("text") or ""), stage,
                  "original_updated", "机器底稿未更新")
        if empty:
            acc.check(unit["corrected_text"] == "", stage, "empty_correction_kept", "空校订被清除或改写")
            acc.check(unit["text"] == "", stage, "empty_effective_text", "空校订页的有效文字不为空")
            acc.check(unit["status"] == "pending", stage, "empty_still_pending",
                      f"空校订页状态变成 {unit['status']}（应保持待定）")
        else:
            acc.check(unit["corrected_text"] == old_text, stage, "corrected_preserved", "采用改动了人工校订文字")
            acc.check(unit["text"] == old_text, stage, "effective_still_corrected", "有效文字不再是校订版")
            if evidence_id:
                row = ev.get_evidence(ws, evidence_id)
                acc.check(row["version"] == "corrected" and row["review_status"] == "accepted", stage,
                          "corrected_excerpt_valid", "校订版摘录在校订未变时被误标复核")
        acc.scenario(name, "supplemental", stage=stage, detail=f"unit={uid} status={unit['status']}")
        results[name] = {"unit_id": uid, "candidate_id": detail["id"], "evidence_id": evidence_id,
                         "status": unit["status"]}
    acc.stages["stage4_correction"] = results
    return results

def stage_cas_reject(acc, ws, gen, used) -> dict:
    from litworkbench.errors import EditConflictError
    from litworkbench import evidence as ev

    stage = "stage5_cas_reject"
    outcomes = {}
    cas = next((r for r in gen["ready"] if not r["has_correction"] and r["unit_id"] not in used), None)
    if cas is None:
        acc.fail(stage, "cas_unavailable", "没有可用于 CAS 冲突的候选页")
    else:
        used.add(cas["unit_id"])
        uid, cid = cas["unit_id"], cas["candidate_id"]
        rev0 = int(ws.get_unit(uid)["content_revision"])
        ev.correct_unit(ws, uid, ws.get_unit(uid)["original_text"] + "（验收改动）", "Task D CAS 夹具")
        conflict = None
        try:
            gen["store"].adopt(cid, expected_content_revision=rev0)
        except EditConflictError as exc:
            conflict = exc
        except Exception as exc:  # noqa: BLE001
            acc.fail(stage, "cas_wrong_error", f"期望 EditConflictError，得到 {type(exc).__name__}")
        acc.check(conflict is not None, stage, "cas_409", "并发校订后采用没有 409")
        acc.check(gen["store"].get(cid)["state"] == "ready", stage, "cas_candidate_untouched", "冲突后候选状态被改动")
        acc.check(not _snapshot_rows(ws, cid), stage, "cas_no_snapshot", "冲突写入留下了快照")
        acc.check(not any(r["kind"] == "ocr-adopt" for r in ws.revisions_for_unit(uid)), stage,
                  "cas_no_adopt_history", "冲突写入留下了采用历史")
        outcomes["cas"] = {"unit_id": uid, "candidate_id": cid, "conflict": conflict is not None}
    reject = next((r for r in gen["ready"] if r["unit_id"] not in used), None)
    if reject is None:
        acc.fail(stage, "reject_unavailable", "没有可用于丢弃的候选页")
    else:
        used.add(reject["unit_id"])
        uid, cid = reject["unit_id"], reject["candidate_id"]
        before = ws.get_unit(uid)
        rejected = gen["store"].reject(cid, reason="Task D 验收：保留现用")
        after = ws.get_unit(uid)
        acc.check(rejected["state"] == "rejected", stage, "rejected_state", "候选未进入 rejected")
        acc.check(before["original_text"] == after["original_text"]
                  and int(before["content_revision"]) == int(after["content_revision"]), stage,
                  "reject_no_write", "丢弃候选改动了正文或修订号")
        outcomes["reject"] = {"unit_id": uid, "candidate_id": cid}
    acc.stages[stage] = outcomes
    return outcomes

def stage_failures(acc, ws, gen, baseline) -> None:
    """失败页必须保留真实错误类型，且现用 status/底稿/校订与基线逐项一致（旧页可处于任意状态）。"""
    stage = "stage5_failure_reporting"
    failed = gen["failed"]
    if not failed:
        acc.scenario("generation_failures", "observational", status="unavailable",
                     detail="本次没有生成失败页可核对（不视为通过/失败）")
        acc.stages[stage] = {"failed": 0}
        return
    for record in failed:
        uid = record["unit_id"]
        base = baseline["units"].get(uid, {})
        unit = ws.get_unit(uid)
        acc.check(bool((record.get("error") or {}).get("kind")), stage, f"kind:{uid}", "失败页缺少真实错误类型")
        acc.check(unit["status"] == base.get("status"), stage, f"status:{uid}", "失败页的现用状态被改动")
        acc.check(sha256_text(unit["original_text"]) == base.get("original_sha256"), stage,
                  f"text:{uid}", "失败页被动用了现用正文")
        cur_corr = None if unit["corrected_text"] is None else sha256_text(unit["corrected_text"])
        acc.check(cur_corr == base.get("corrected_sha256"), stage, f"correction:{uid}", "失败页的校订被改动")
    acc.scenario("generation_failures", "observational", stage=stage, detail=f"核对了 {len(failed)} 个失败页")
    acc.stages[stage] = {"failed": len(failed)}

def stage_integrity(acc, ws, baseline) -> None:
    stage = "stage6_integrity"
    after = read_snapshot(ws)
    existing = list(baseline["jobs"])
    intact = all(after["jobs"].get(jid) == baseline["jobs"][jid] for jid in existing)
    acc.check(intact, stage, "existing_jobs_intact", "既有终态任务被改动或丢失")
    acc.check(baseline["documents"] == after["documents"], stage, "documents_intact", "生成/采用改动了文献行")
    acc.check(set(baseline["collections"]).issubset(after["collections"]), stage, "collections_preserved",
              "既有集合被改动或丢失（新集合应追加）")
    acc.check({tuple(p) for p in baseline["collection_evidence"]}.issubset(
        {tuple(p) for p in after["collection_evidence"]}), stage, "collection_evidence_preserved",
        "既有集合成员关系被改动或丢失")
    from litworkbench import ocr as ocr_mod
    engine_retained = getattr(ocr_mod, "_ENGINE", None) is not None
    acc.check(not engine_retained, stage, "no_retained_engine", "root 进程仍保留 RapidOCR 引擎单例")
    acc.scenario("existing_terminal_jobs", "frozen", stage=stage, detail=f"{len(existing)} 个既有终态任务保持原样")
    acc.stages[stage] = {"existing_jobs": len(existing), "engine_retained": engine_retained}

def run_execute(acc, clone, samples, snap, src_ok, src_detail, expected) -> None:
    # 写前守卫：任何结构/来源失败都在 Workspace.open（迁移/写入）之前终止。
    structural_checks(acc, clone, samples, snap, src_ok, src_detail, expected)
    if not acc.passed:
        acc.warn("stage1_clone", "写前结构/来源校验未通过：已拒绝 Workspace.open、迁移与入队（fail-closed）")
        return
    from litworkbench.store import Workspace
    from litworkbench.ocr_candidates import CandidateStore

    try:
        ws = Workspace.open(clone)
    except Exception as exc:  # noqa: BLE001
        acc.fail("stage1_clone", "workspace_open", f"{type(exc).__name__}: {exc}")
        return
    baseline = read_snapshot(ws)
    store_probe = acc.guard_stage("stage2_generation", lambda a, w: CandidateStore(w), ws)
    if store_probe is None:
        return
    gen = acc.guard_stage("stage2_generation", stage_generation, ws, samples, baseline)
    if gen is None:
        return
    used: set = set()
    acc.guard_stage("stage3_adopt", stage_adopt, ws, gen, used)
    acc.guard_stage("stage4_correction", stage_correction, ws, gen, used)
    acc.guard_stage("stage5_cas_reject", stage_cas_reject, ws, gen, used)
    acc.guard_stage("stage5_failure_reporting", stage_failures, ws, gen, baseline)
    acc.guard_stage("stage6_integrity", stage_integrity, ws, baseline)

# ------------------------------------------------------------------ 报告与入口

def build_report(acc, clone, samples_path, output_dir, src_detail, expected) -> dict:
    frozen = [s for s in acc.scenarios if s["kind"] == "frozen"]
    supplemental = [s for s in acc.scenarios if s["kind"] == "supplemental"]
    return {
        "schema_version": 1, "generated_at": now_iso(), "task": "D", "mode": acc.mode,
        "plan": "docs/superpowers/plans/2026-10-16-ocr-candidate-review.md",
        "workspace": str(clone), "samples": str(samples_path), "output": str(output_dir),
        "expected_source_hashes": expected, "source_hash_checks": src_detail,
        "fixture_boundaries": FIXTURE_BOUNDARIES,
        "counts": {"frozen_scenarios": len(frozen), "supplemental_scenarios": len(supplemental),
                   "failures": len(acc.failures), "warnings": len(acc.warnings)},
        "scenarios": acc.scenarios, "stages": acc.stages, "failures": acc.failures,
        "warnings": acc.warnings,
        "required_acceptance_passed": acc.mode == "execute" and acc.passed,
    }

def render_markdown(report: dict) -> str:
    c = report["counts"]
    outcome = "未执行（仅只读预检）" if report["mode"] == "dry-run" else (
        "通过" if report["required_acceptance_passed"] else "**未通过**"
    )
    lines = [
        "# OCR 候选采用 · Task D 内部验收报告", "",
        f"- 生成时间：{report['generated_at']}", f"- 模式：**{report['mode']}**",
        f"- 克隆：`{report['workspace']}`", f"- 样本：`{report['samples']}`",
        f"- 必需验收：{outcome}",
        f"- 失败 {c['failures']} · 警告 {c['warnings']}", "", "## 场景", "",
        "| 场景 | 类型 | 状态 | 说明 |", "| --- | --- | --- | --- |",
    ]
    lines += [f"| {s['name']} | {s['kind']} | {s['status']} | {s['detail']} |" for s in report["scenarios"]]
    lines += ["", "## 阶段", ""]
    for name, data in report["stages"].items():
        if name == "stage2_generation":
            lines.append(f"- **{name}**：ready={data.get('ready_count')} failed={data.get('failed_count')}")
        else:
            lines.append(f"- **{name}**：{json.dumps(data, ensure_ascii=False)[:400]}")
    lines += ["", "## 失败", ""]
    lines += [f"- `{f['stage']}/{f['id']}`：{f['reason']}" for f in report["failures"]] or ["- 无"]
    if report["warnings"]:
        lines += ["", "## 警告", ""] + [f"- `{w['stage']}`：{w['message']}" for w in report["warnings"]]
    lines += ["", "## 边界", ""] + [f"- {k}：{v}" for k, v in FIXTURE_BOUNDARIES.items()] + [""]
    return "\n".join(lines)

def write_outputs(output_dir: Path, report: dict) -> tuple:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = output_dir / JSON_NAME, output_dir / MD_NAME
    for target in (json_path, md_path):
        if target.is_symlink():
            raise GuardError(f"输出目标是符号链接，拒绝覆盖：{target}")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accept_ocr_candidates.py",
        description="Task D 内部验收：在独立克隆上生成/采用真实 OCR 候选（默认 dry-run）。")
    parser.add_argument("--workspace", required=True,
                        help=f"唯一批准克隆（默认 {default_clone_root()}）")
    parser.add_argument("--samples", required=True, help="冻结 samples.json 路径")
    parser.add_argument("--output", default=None, help="输出目录（须在批准目录之下）；默认克隆父目录")
    parser.add_argument("--execute", action="store_true", help="真正运行：通过写前守卫后打开克隆、生成/采用并写报告")
    return parser

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    acc = Acceptance("execute" if args.execute else "dry-run")
    try:
        clone = resolve_clone(args.workspace)
        samples_path = Path(args.samples).expanduser()
        samples = load_samples(samples_path)
        assert_not_real_library(clone, samples.get("local_config", {}).get("library_root"))
        expected = expected_source_hashes(samples)
        escapes = validate_source_paths(clone, samples)
        if escapes:
            raise GuardError("样本来源路径逃逸 sources/ 或为符号链接：" + "; ".join(escapes[:6]))
        output_dir = resolve_output(args.output, clone)  # 写前校验输出目录
        src_ok, src_detail = verify_clone_sources(clone, expected)
        conn = open_ro(db_path_for(clone))
        try:
            snap = snapshot_state(conn)
        finally:
            conn.close()
    except GuardError as exc:
        sys.stderr.write(f"[guard] 拒绝运行：{exc}\n")
        return 2

    if args.execute:
        run_execute(acc, clone, samples, snap, src_ok, src_detail, expected)
    else:
        run_dry(acc, clone, samples, snap, src_ok, src_detail, expected)

    report = build_report(acc, clone, samples_path, output_dir, src_detail, expected)
    if args.execute:
        json_path, md_path = write_outputs(output_dir, report)
        print(json.dumps({"exit": 0 if acc.passed else 1, "json": str(json_path),
                          "markdown": str(md_path), "failures": len(acc.failures)}, ensure_ascii=False))
    else:
        print(render_markdown(report))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if acc.passed else 1

if __name__ == "__main__":
    sys.exit(main())
