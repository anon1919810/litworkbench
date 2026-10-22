"""任务 2：字符编辑距离、检索用例核对与合并口径。

三条硬规则：

* CER = **去空白后**的字符编辑距离 / 参考字数（按 Unicode 码点计），
  不做同音替换、繁简转换或大小写折叠；
* 参考稿没有整页核稿（含 draft、含 ``uncertain_spans``、缺参考）时，
  CER 记 ``None`` 并写明原因——不写 0，也不把页移出分母；
* 检索核对走与网页相同的 FTS5 路径（``search_index.index_many`` +
  ``search_index.search_indexed(mode="phrase")``），在**临时**检索库里跑，
  绝不复用旧的 LIKE 检索。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from scripts.ocr_quality import common

SEARCH_BACKEND = "litworkbench.search_index"


# -------------------------------------------------------------- 字符编辑距离


def levenshtein(left: str, right: str) -> int:
    """标准 Levenshtein 距离（按码点，两个滚动行，O(min) 内存）。"""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for index, char in enumerate(right, start=1):
        current = [index]
        for position, other in enumerate(left, start=1):
            insert = current[position - 1] + 1
            delete = previous[position] + 1
            replace = previous[position - 1] + (0 if char == other else 1)
            current.append(min(insert, delete, replace))
        previous = current
    return previous[-1]


def score_text(reference: str, hypothesis: str) -> dict[str, Any]:
    """整页 CER。参考为空 → ``unavailable``；识别为空 → CER=1（整页漏检）。"""
    reference_chars = common.strip_spaces(reference or "")
    hypothesis_chars = common.strip_spaces(hypothesis or "")
    if not reference_chars:
        return {
            "status": "unavailable",
            "distance": None,
            "reference_chars": 0,
            "hypothesis_chars": len(hypothesis_chars),
            "cer": None,
            "reason": "参考稿去空白后没有可用文字，无法计算 CER",
        }
    distance = levenshtein(reference_chars, hypothesis_chars)
    return {
        "status": "ok",
        "distance": distance,
        "reference_chars": len(reference_chars),
        "hypothesis_chars": len(hypothesis_chars),
        "cer": distance / len(reference_chars),
    }


def score_page(reference: dict[str, Any] | None, hypothesis: str | None,
               *, error: dict[str, Any] | None = None) -> dict[str, Any]:
    """结合参考稿核稿状态给出整页 CER；不可评分时写原因，绝不写 0。"""
    level = common.reference_review_level(reference)
    base: dict[str, Any] = {
        "review_level": level,
        "reviewer": (reference or {}).get("reviewer") or "",
    }
    if error:
        base.update({"status": "error", "distance": None, "reference_chars": 0,
                     "cer": None, "reason": f"识别失败：{error.get('message', '')}".strip(),
                     "error": error})
        return base
    scorable, reason = common.reference_is_scorable(reference)
    if not scorable:
        base.update({"status": "unavailable", "distance": None,
                     "reference_chars": len(common.strip_spaces((reference or {}).get("text") or "")),
                     "cer": None, "reason": reason})
        return base
    scored = score_text(reference["text"], hypothesis or "")
    base.update(scored)
    base["reason"] = ""
    return base


# -------------------------------------------------------------- 检索核对


def _sample_pages(dataset: dict[str, Any]) -> dict[tuple[str, int], str]:
    return {common.sample_page_key(sample): str(sample["sample_id"])
            for sample in dataset.get("samples") or []}


def _build_scratch_workspace(scratch_root: Path) -> Path:
    root = Path(scratch_root)
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="search-", dir=str(root)))


def evaluate_queries(dataset: dict[str, Any], results: Sequence[dict[str, Any]],
                     scratch_root: Path) -> dict[str, Any]:
    """在**临时**检索库里核对正/负检索用例。

    索引与检索都走 ``litworkbench.search_index``（FTS5），与网页检索同一条路径；
    识别失败或空输出的页照实计漏检，不会因此缩小分母。
    """
    from litworkbench import search_index
    from litworkbench.store import Workspace

    workspace_dir = _build_scratch_workspace(scratch_root)
    workspace = Workspace(workspace_dir)
    workspace.initialize()

    pages = _sample_pages(dataset)
    usable = []
    failed_samples: list[str] = []
    empty_samples: list[str] = []
    for record in results:
        sample_id = str(record.get("sample_id") or "")
        if record.get("status") != "ok":
            failed_samples.append(sample_id)
            continue
        text = record.get("text") or ""
        doc_id = str(record.get("doc_id") or f"doc_{sample_id}")
        unit_id = f"u_{sample_id}"
        workspace.save_document({
            "id": doc_id, "sha256": (sample_id + record.get("input_sha256", "")) * 8,
            "original_path": "", "stored_path": "", "filename": sample_id,
            "filetype": "pdf", "title": sample_id, "author": "", "year": "", "edition": "",
            "volume": "", "status": "completed", "ocr_mode": "off",
            "page_count": int(record.get("file_page") or 1), "unit_count": 1,
            "pending_count": 0,
        })
        workspace.save_unit({
            "id": unit_id, "doc_id": doc_id, "seq": int(record.get("file_page") or 1),
            "kind": "page", "file_page": int(record.get("file_page") or 1),
            "printed_page": record.get("printed_page") or "", "title_path": "",
            "method": record.get("method") or "ocr", "status": "completed",
            "ocr_decision": "not_needed", "original_text": text, "corrected_text": None,
            "notes": "", "ocr_json": None,
        })
        search_index.index_many(workspace, [unit_id])
        usable.append(record)
        if not common.strip_spaces(text):
            empty_samples.append(sample_id)

    positives = {"total": 0, "hit": 0, "missed": [], "cases": []}
    negatives = {"total": 0, "false_hit": 0, "cases": []}
    failed_cases: list[dict[str, Any]] = []

    for case in dataset.get("query_cases") or []:
        query = str(case.get("query") or "")
        sample = next((item for item in (dataset.get("samples") or [])
                       if str(item.get("sample_id")) == str(case.get("sample_id"))), None)
        expected = {"doc_id": (sample or {}).get("doc_id"),
                    "file_page": (sample or {}).get("file_page")}
        kind = str(case.get("kind") or "positive")
        bucket = positives if kind == "positive" else negatives
        bucket["total"] += 1
        entry: dict[str, Any] = {
            "query_id": case.get("query_id"), "kind": kind, "query": query,
            "sample_id": case.get("sample_id"),
        }
        if kind == "positive":
            entry["expected_page"] = expected
        try:
            found = search_index.search_indexed(workspace, query, mode=search_index.MODE_PHRASE,
                                               page_size=100)
        except Exception as exc:
            failed_cases.append({"query_id": case.get("query_id"), "kind": kind,
                                 "error": common.record_error(type(exc).__name__, str(exc))})
            entry.update({"hit": None, "matched": [], "error": str(exc),
                          "search_total": None, "reason": "case_error"})
            bucket["cases"].append(entry)
            # 检索用例自身出错也算漏检：分母不变，不给“通过”。
            if kind == "positive":
                positives["missed"].append(entry)
            continue

        matched = []
        for hit in found["items"]:
            key = (str(hit["doc_id"]), int(hit["file_page"] or 0))
            if key in pages:
                matched.append({"sample_id": pages[key], "doc_id": hit["doc_id"],
                                "file_page": hit["file_page"], "method": hit.get("method"),
                                "snippet": hit.get("snippet", "")})
        entry["search_total"] = found["total"]
        entry["matched"] = matched
        if kind == "positive":
            expected_key = (str(expected.get("doc_id") or ""),
                            int(expected.get("file_page") or 0))
            on_target = any((str(item["doc_id"]), int(item["file_page"] or 0)) == expected_key
                            for item in matched)
            entry["hit"] = on_target
            if on_target:
                positives["hit"] += 1
            else:
                entry["reason"] = ("engine_empty_text"
                                   if str(case.get("sample_id")) in empty_samples
                                   else ("sample_failed" if str(case.get("sample_id"))
                                         in failed_samples else "not_found"))
                positives["missed"].append(entry)
        else:
            entry["false_hit"] = bool(matched)
            if matched:
                negatives["false_hit"] += 1
        bucket["cases"].append(entry)

    return {
        "search_backend": SEARCH_BACKEND,
        "scratch_root": str(Path(scratch_root).resolve()),
        "scratch_workspace": str(workspace_dir),
        "positives": positives,
        "negatives": negatives,
        "failed_cases": failed_cases,
        "failed_samples": sorted(set(failed_samples)),
        "empty_text_samples": sorted(set(empty_samples)),
        "indexed_samples": sorted({str(record.get("sample_id")) for record in usable}),
    }


# ---------------------------------------------------------------- 合并口径


def aggregate_page_scores(scores: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """合并整页分数：按参考字数加权；失败与未评分页照实列出，不缩小分母。"""
    items = list(scores)
    scored = [item for item in items if item.get("cer") is not None]
    reference_chars = sum(int(item.get("reference_chars") or 0) for item in scored)
    weighted = (sum(float(item["cer"]) * int(item["reference_chars"]) for item in scored)
                / reference_chars) if reference_chars else None
    macro = (sum(float(item["cer"]) for item in scored) / len(scored)) if scored else None
    return {
        "total_pages": len(items),
        "scored_pages": len(scored),
        "unavailable_pages": len(items) - len(scored),
        "reference_chars": reference_chars,
        "weighted_cer": weighted,
        "macro_cer": macro,
        "failed_samples": sorted(str(item.get("sample_id")) for item in items
                                 if item.get("status") == "error"),
        "unavailable_samples": sorted(str(item.get("sample_id")) for item in items
                                      if item.get("status") == "unavailable"),
        "unavailable_reasons": sorted({str(item.get("reason") or "") for item in items
                                       if item.get("cer") is None and item.get("reason")}),
    }


def comparable_cohort(runs: Sequence[dict[str, Any]]) -> list[str]:
    """共同集合：所有对比运行都成功、且**输入与参考哈希一致**的可评分样本。"""
    if not runs:
        return []
    shared: set[str] | None = None
    for entry in runs:
        inputs = entry.get("inputs") or {}
        references = entry.get("references") or {}
        usable = {str(sample_id) for sample_id in (entry.get("scorable") or [])}
        ids = {sample_id for sample_id in usable
               if str(sample_id) not in (entry.get("failed") or [])
               and references.get(sample_id) and inputs.get(sample_id)}
        shared = ids if shared is None else (shared & ids)
    if shared is None:
        return []
    reference = runs[0].get("references") or {}
    baseline_inputs = runs[0].get("inputs") or {}
    consistent = {sample_id for sample_id in shared
                  if all(str((run.get("references") or {}).get(sample_id)) == str(reference.get(sample_id))
                         and str((run.get("inputs") or {}).get(sample_id))
                         == str(baseline_inputs.get(sample_id))
                         for run in runs)}
    return sorted(consistent)