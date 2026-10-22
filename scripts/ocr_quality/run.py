"""任务 2/3：受控子进程调度 —— RapidOCR 基线与候选适配器。

要点：

* 每页/每个用例都是**独立子进程**：超时（默认 180 秒/页）、内存闸门、
  强制终止都只作用于本工具启动的进程树；
* 基线子进程默认走生产路径（``litworkbench.ocr.recognize``）；
  ``--params-json`` 只把参数注入**该子进程**，用于比较 PP-OCRv6 medium、
  检测阈值等，不改产品代码、不改主环境；
* ``--mode auto`` 逐页看文字层：文字足够就直接取文字层（``method=text``），
  否则才调引擎；``--mode force`` 全部走引擎。两条流程分开记录；
* 失败、超时、缺依赖都**照实落一条结果**并继续下一页；指标缺失写 ``None``。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Sequence

from scripts.ocr_quality import common, metrics, workers
from scripts.ocr_quality.common import DERIVED_DIRNAME, SafetyError, ValidationFailure

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_UNAVAILABLE = 2

DEFAULT_REPEATS = 3
DEFAULT_SCALE = 2.0
MODES = ("auto", "force")
PROFILES = ("rapidocr-current", "docling-rapidocr")
BASELINE_PROFILE = "rapidocr-current"
CANDIDATE_PROFILE = "docling-rapidocr"
BASELINE_WORKER = "rapidocr_worker.py"
CANDIDATE_ADAPTER = "docling_adapter.py"


def exceeds_rss(peak_bytes: int | None, limit_bytes: int | None) -> bool:
    return workers.exceeds_rss(peak_bytes, limit_bytes)


# ------------------------------------------------------------------ 工具


def _median(values: Sequence[float] | None) -> float | None:
    items = [float(item) for item in (values or []) if item is not None]
    return round(statistics.median(items), 4) if items else None


def _max(values: Sequence[float] | None) -> float | None:
    items = [float(item) for item in (values or []) if item is not None]
    return round(max(items), 4) if items else None


def summarize_timing(timing: dict[str, Any] | None) -> dict[str, Any]:
    timing = dict(timing or {})
    recognize = timing.get("recognize_seconds") or []
    render = timing.get("render_seconds") or []
    timing["repeats"] = len(recognize) or None
    timing["median_render_seconds"] = _median(render)
    timing["median_recognize_seconds"] = _median(recognize)
    timing["max_recognize_seconds"] = _max(recognize)
    return timing


def page_text_layer(page_pdf: Path) -> str:
    """派生页的文字层（与产品同一套清理规则），只读。"""
    import pypdfium2 as pdfium

    from litworkbench.text import collapse_pdf_text

    pdf = pdfium.PdfDocument(str(page_pdf))
    try:
        page = pdf[0]
        try:
            textpage = page.get_textpage()
            try:
                return collapse_pdf_text(textpage.get_text_range() or "")
            finally:
                textpage.close()
        finally:
            page.close()
    finally:
        pdf.close()


def _min_text_chars(data: dict[str, Any], override: int | None) -> int:
    if override is not None:
        return int(override)
    library_root = str(data.get("local_config", {}).get("library_root") or "")
    if library_root:
        try:
            from litworkbench import config as config_mod

            config = config_mod.load_config(Path(library_root) / config_mod.CONFIG_FILENAME)
            return int(config.get("ocr", {}).get("min_text_chars", 20))
        except Exception:  # pragma: no cover - 读不到就退回默认值
            return 20
    return 20


def _resolve_worker(profile: str, *, candidate_python: Path | str | None,
                    candidate_script: Path | str | None,
                    worker_script: Path | str | None) -> dict[str, Any]:
    """解析要启动的解释器与脚本；不可用时给出明确原因（不是成功）。"""
    if profile == BASELINE_PROFILE:
        script = Path(worker_script) if worker_script else common.script_path(BASELINE_WORKER)
        if not script.is_file():
            return {"unavailable_reason": f"基线子进程脚本不存在：{script}"}
        return {
            "python": sys.executable,
            "script": script,
            "bundled": script.resolve() == common.script_path(BASELINE_WORKER).resolve(),
        }
    if not candidate_python:
        return {"unavailable_reason": "候选方案需要 --candidate-python <候选环境的绝对 Python 路径>"}
    python_path = Path(candidate_python)
    if not python_path.is_absolute():
        return {"unavailable_reason": f"--candidate-python 必须是绝对路径，收到 {candidate_python}"}
    if not python_path.is_file():
        return {"unavailable_reason": f"候选解释器不存在：{python_path}"}
    script = Path(candidate_script) if candidate_script else common.script_path(CANDIDATE_ADAPTER)
    if not script.is_file():
        return {"unavailable_reason": f"候选适配器脚本不存在：{script}"}
    return {"python": str(python_path), "script": script,
            "bundled": script.resolve() == common.script_path(CANDIDATE_ADAPTER).resolve()}


def _worker_args(profile: str, *, scale: float, repeats: int, init_timeout: float,
                  params_json: Path | str | None) -> list[str]:
    """两侧子进程各自只接收自己认识的参数（避免把基线参数塞给候选）。"""
    if profile == BASELINE_PROFILE:
        args = ["--scale", str(scale), "--repeats", str(repeats),
                "--init-timeout", str(init_timeout)]
        if params_json:
            args += ["--params", str(common.resolve(params_json))]
        return args
    args = ["--force-ocr", "--init-timeout", str(init_timeout),
            "--scale", str(scale), "--repeats", str(repeats)]
    if params_json:
        settings = common.load_json(params_json)
        for key, flag in (("Det.model_path", "--det-model"),
                          ("Rec.model_path", "--rec-model"),
                          ("Cls.model_path", "--cls-model"),
                          ("expect_version", "--expect-version"),
                          ("layout_manifest", "--layout-manifest")):
            if key in settings:
                args.extend([flag, str(settings[key])])
    return args


# ------------------------------------------------------------------ 记录


def _base_record(sample: dict[str, Any], out_root: Path, case: str, profile: str,
                 run_id: str, scale: float, repeats: int) -> dict[str, Any]:
    page_pdf = str(sample.get("page_pdf") or "")
    return {
        "record_version": common.RECORD_VERSION,
        "run_id": run_id,
        "profile": profile,
        "sample_id": sample.get("sample_id"),
        "doc_id": sample.get("doc_id"),
        "file_page": sample.get("file_page"),
        "source_filename": sample.get("source_filename"),
        "source_sha256": sample.get("source_sha256"),
        "printed_page": sample.get("printed_page", ""),
        "printed_page_status": sample.get("printed_page_status", "none"),
        "tags": sample.get("tags", []),
        "page_pdf": page_pdf,
        "page_pdf_absolute": str(out_root / page_pdf) if page_pdf else "",
        "input_sha256": common.sha256_file(out_root / page_pdf) if page_pdf else None,
        "reference_sha256": sample.get("reference_sha256"),
        "reference_review_level": common.reference_review_level(None),
        "reference_reviewer": "",
        "case": case,
        "method": None,
        "scale": scale,
        "repeats": repeats,
        "engine": None,
        "engine_version": None,
        "runtime_versions": {},
        "model_fingerprints": [],
        "config": {},
        "text": None,
        "regions": [],
        "coordinate_system": "none",
        "granularity": "none",
        "mean_confidence": None,
        "confidence_available": False,
        "confidence_reason": None,
        "raw_output": None,
        "error": None,
        "status": "error",
        "text_empty": False,
        "timed_out": False,
        "timing": summarize_timing(None),
        "memory": workers.unavailable_memory("尚未启动子进程，未采样",
                                             limit_bytes=common.MAX_CANDIDATE_RSS_BYTES),
        "metrics": None,
        "query_cases": [],
    }


def _load_reference(sample: dict[str, Any], out_root: Path) -> dict[str, Any] | None:
    """读参考稿；读不到就返回 None（CER 会写 unavailable + 原因）。"""
    path = str(sample.get("reference_path") or "")
    if not path:
        return None
    target = out_root / path
    if not target.is_file():
        return None
    try:
        return common.load_json(target)
    except Exception:  # pragma: no cover - 损坏参考稿按缺失处理
        return None


def _map_worker_result(record: dict[str, Any], payload: dict[str, Any],
                       worker: dict[str, Any]) -> dict[str, Any]:
    record["engine"] = payload.get("engine") or record.get("engine")
    record["engine_version"] = payload.get("engine_version")
    record["runtime_versions"] = payload.get("runtime_versions") or {}
    record["model_fingerprints"] = payload.get("model_fingerprints") or []
    record["config"] = payload.get("config") or {}
    record["text"] = payload.get("text")
    record["regions"] = payload.get("regions") or []
    record["coordinate_system"] = payload.get("coordinate_system") or "none"
    record["granularity"] = payload.get("granularity") or "none"
    record["mean_confidence"] = payload.get("mean_confidence")
    record["confidence_available"] = bool(payload.get("confidence_available"))
    record["confidence_reason"] = payload.get("confidence_reason")
    record["page"] = payload.get("page")
    record["timing"] = summarize_timing(payload.get("timing"))
    record["memory"] = worker.get("memory") or record["memory"]
    record["production_path"] = payload.get("production_path")
    record["notes"] = payload.get("notes") or []
    raw = payload.get("raw_output")
    if isinstance(raw, dict) and raw.get("path"):
        # 原始输出与结果 JSON 同目录；这里记录相对运行目录的引用。
        record["raw_output"] = {"path": f"raw/{raw['path']}", "sha256": raw.get("sha256")}
    record["text_empty"] = not common.strip_spaces(record.get("text") or "")
    record["status"] = "ok"
    record["error"] = None
    return record


def _failed_record(record: dict[str, Any], worker: dict[str, Any],
                   fallback: str) -> dict[str, Any]:
    if worker.get("timed_out"):
        record["status"] = "timeout"
        record["timed_out"] = True
        record["error"] = common.record_error(
            "timeout", f"超过单页超时上限被终止：{fallback}")
    else:
        record["status"] = ("unavailable"
                            if worker.get("exit_kind") == "missing_dependency" else "error")
        record["error"] = workers.error_from_exit(worker, fallback_message=fallback)
    record["memory"] = worker.get("memory") or record["memory"]
    record["timing"] = summarize_timing({"init_seconds": None, "render_seconds": [],
                                         "recognize_seconds": []})
    record["stderr_tail"] = (worker.get("stderr_tail") or "")[-1200:]
    record["stdout_tail"] = (worker.get("stdout_tail") or "")[-1200:]
    return record


# ------------------------------------------------------------------ 主流程


def run_benchmark(manifest: Path | str, output_root: Path | str, profile: str, *,
                  scale: float | None = None, repeats: int = DEFAULT_REPEATS,
                  mode: str = "auto", sample_ids: Sequence[str] | None = None,
                  params_json: Path | str | None = None,
                  candidate_python: Path | str | None = None,
                  candidate_script: Path | str | None = None,
                  worker_script: Path | str | None = None,
                  page_timeout: float = common.PAGE_TIMEOUT_SECONDS,
                  init_timeout: float = common.INIT_TIMEOUT_SECONDS,
                  max_rss_bytes: int = common.MAX_CANDIDATE_RSS_BYTES,
                  min_text_chars: int | None = None) -> Path:
    """跑一轮评测，写 ``results.jsonl`` / ``run.json`` / ``queries.json``。"""
    if profile not in PROFILES:
        raise ValidationFailure(f"未知方案：{profile}（可选 {'/'.join(PROFILES)}）")
    if mode not in MODES:
        raise ValidationFailure(f"未知模式：{mode}（可选 {'/'.join(MODES)}）")

    manifest_path = common.resolve(manifest)
    data = common.load_json(manifest_path)
    library_root = str(data.get("local_config", {}).get("library_root") or "")
    library = common.resolve(library_root) if library_root else None
    out_root = common.ensure_safe_output(output_root, library_root=library,
                                         protected=[("派生文件目录",
                                                    manifest_path.parent / DERIVED_DIRNAME)])
    if out_root.exists() and any(out_root.iterdir()):
        raise ValidationFailure(f"运行目录已有内容，请选择新的空目录，避免混用旧结果：{out_root}")
    out_root.mkdir(parents=True, exist_ok=True)
    raw_dir = common.safe_target(out_root, out_root / "raw")
    log_dir = common.safe_target(out_root, out_root / "logs")
    raw_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    all_samples = list(data.get("samples") or [])
    for sample in all_samples:
        common.validate_sample_id(sample.get("sample_id"))
    dataset_ids = [str(item.get("sample_id")) for item in all_samples]
    known = set(dataset_ids)
    wanted = [str(item) for item in (sample_ids or [])]
    unknown = [item for item in wanted if item not in known]
    if unknown:
        raise ValidationFailure(f"清单里没有这些 sample_id：{unknown}")
    selected = [item for item in all_samples if not wanted or str(item.get("sample_id")) in set(wanted)]

    scale = float(scale if scale is not None else DEFAULT_SCALE)
    repeats = max(1, int(repeats))
    run_id = f"{profile}-{common.now_iso().replace(':', '').replace('-', '')}"
    config = {
        "profile": profile, "mode": mode, "scale": scale, "repeats": repeats,
        "params": common.load_json(params_json) if params_json else None,
        "params_sha256": common.sha256_file(params_json) if params_json else None,
        "params_path": str(params_json) if params_json else None,
        "page_timeout_seconds": float(page_timeout),
        "init_timeout_seconds": float(init_timeout),
        "max_rss_bytes": int(max_rss_bytes),
        "min_text_chars": None,  # 下面填真实读到的阈值
    }
    worker = _resolve_worker(profile, candidate_python=candidate_python,
                             candidate_script=candidate_script, worker_script=worker_script)
    run_meta: dict[str, Any] = {
        "record_version": common.RECORD_VERSION,
        "run_id": run_id,
        "profile": profile,
        "status": "ok",
        "unavailable_reason": None,
        "started_at": common.now_iso(),
        "manifest": str(manifest_path),
        "manifest_sha256": common.sha256_file(manifest_path),
        "dataset_label": data.get("dataset_label") or "",
        "synthetic": bool(data.get("synthetic")),
        "sample_filter": wanted,
        "dataset_sample_ids": dataset_ids,
        "config": config,
        "environment": common.environment_versions("rapidocr", "onnxruntime", "pypdfium2",
                                                   "litworkbench", "docling"),
        "worker": {"python": worker.get("python"), "script": str(worker.get("script") or ""),
                   "script_sha256": common.sha256_file(worker["script"]) if worker.get("script") else None,
                   "bundled": worker.get("bundled")},
        "inputs": {}, "references": {}, "scorable": [], "failed": [], "counts": {},
        "max_rss_bytes": int(max_rss_bytes),
    }

    if "unavailable_reason" in worker:
        run_meta["status"] = "unavailable"
        run_meta["unavailable_reason"] = worker["unavailable_reason"]
        run_meta["finished_at"] = common.now_iso()
        common.dump_json(out_root / "run.json", run_meta)
        return out_root

    config["min_text_chars"] = _min_text_chars(data, min_text_chars)
    threshold = int(config["min_text_chars"])
    total_timeout = float(init_timeout) + float(page_timeout) * repeats
    records: list[dict[str, Any]] = []

    for sample in selected:
        sample_id = str(sample.get("sample_id"))
        reference = _load_reference(sample, manifest_path.parent)
        record = _base_record(sample, manifest_path.parent, mode, profile, run_id, scale, repeats)
        record["synthetic_worker"] = not worker.get("bundled", False)
        record["reference_review_level"] = common.reference_review_level(reference)
        record["reference_reviewer"] = (reference or {}).get("reviewer") or ""
        record["reference_text_sha256"] = (
            common.sha256_text(reference.get("text") or "") if reference else None)
        run_meta["inputs"][sample_id] = record["input_sha256"]
        run_meta["references"][sample_id] = record["reference_sha256"]

        page_pdf = manifest_path.parent / str(sample.get("page_pdf") or "")
        text_layer = ""
        try:
            text_layer = page_text_layer(page_pdf)
        except Exception as exc:
            record["error"] = common.record_error("render_error", f"无法读取派生页：{exc}")
            record["status"] = "error"
            record["metrics"] = metrics.score_page(reference, None, error=record["error"])
            records.append(record)
            continue

        from litworkbench.text import first_meaningful_chars

        needs_engine = mode == "force" or first_meaningful_chars(text_layer) < threshold
        if not needs_engine:
            record["method"] = "text"
            record["engine"] = "pdf-text-layer"
            record["text"] = text_layer
            record["text_empty"] = not common.strip_spaces(text_layer)
            record["status"] = "ok"
            record["config"] = {"min_text_chars": threshold, "source": "pdf-text-layer",
                                "text_layer_meaningful_chars": first_meaningful_chars(text_layer)}
            record["timing"] = summarize_timing({
                "init_seconds": None, "render_seconds": None, "recognize_seconds": None,
                "recognize_reason": "该页使用 PDF 文字层，未调用识别引擎"})
            record["memory"] = workers.unavailable_memory(
                "该页未启动子进程，未采样", limit_bytes=int(max_rss_bytes))
            record["metrics"] = metrics.score_page(reference, text_layer)
            records.append(record)
            continue

        record["method"] = "ocr"
        extra = _worker_args(profile, scale=scale, repeats=repeats,
                             init_timeout=init_timeout, params_json=params_json)
        output_json = common.safe_target(out_root, raw_dir / f"{sample_id}__{mode}.json")
        command = workers.build_command(worker["python"], worker["script"],
                                        input_pdf=page_pdf, output_json=output_json,
                                        extra=extra)
        outcome = workers.run_worker(command, timeout=total_timeout,
                                     memory_limit_bytes=int(max_rss_bytes),
                                     log_dir=log_dir, name=f"{sample_id}__{mode}",
                                     init_timeout=float(init_timeout), page_timeout=float(page_timeout))
        record["command"] = command
        record["raw_output_dir"] = "raw"
        payload = workers.load_worker_result(output_json)
        if outcome["killed_reason"] in ("rss_limit", "private_limit"):
            _failed_record(record, outcome, "候选进程树 RSS 超过 8 GiB 闸门")
            record["error"] = common.record_error(
                outcome["killed_reason"],
                f"进程树 RSS 峰值 {outcome['memory'].get('peak_rss_bytes')} / "
                f"Windows private commit 峰值 {outcome['memory'].get('peak_private_bytes')} 字节，"
                f"超过上限 {max_rss_bytes} 字节")
        elif outcome["returncode"] != workers.EXIT_OK or payload is None:
            _failed_record(record, outcome, "子进程没有产出结果文件")
        else:
            _map_worker_result(record, payload, outcome)
        if record["status"] != "ok":
            record["metrics"] = metrics.score_page(reference, None, error=record["error"])
        else:
            record["metrics"] = metrics.score_page(reference, record["text"])
        records.append(record)

    # 先做检索核对再落盘：results.jsonl 里必须带逐页命中与漏检。
    queries = _score_queries(data, records, common.safe_target(out_root, out_root / "scratch"))
    common.dump_json(out_root / "queries.json", queries)
    _write_results(out_root / "results.jsonl", records)

    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    run_meta["counts"] = counts
    run_meta["record_count"] = len(records)
    run_meta["scorable"] = [str(item["sample_id"]) for item in records
                            if (item.get("metrics") or {}).get("cer") is not None]
    run_meta["failed"] = [str(item["sample_id"]) for item in records
                          if item["status"] != "ok"]
    run_meta["synthetic_worker"] = not worker.get("bundled", False)
    run_meta["queries"] = {key: queries[key] for key in ("positives", "negatives")
                           if key in queries}
    if counts.get("unavailable") == len(records) and records:
        run_meta["status"] = "unavailable"
    elif counts.get("ok") != len(records):
        run_meta["status"] = "partial"
    run_meta["finished_at"] = common.now_iso()
    common.dump_json(out_root / "run.json", run_meta)
    return out_root


def _write_results(path: Path, records: list[dict[str, Any]]) -> None:
    with common.safe_target(path.parent, path).open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _score_queries(data: dict[str, Any], records: list[dict[str, Any]],
                   scratch: Path) -> dict[str, Any]:
    """检索核对并**把命中与漏检都写回每页记录**（报告要逐页看到漏词）。"""
    if not records:
        return {}
    report = metrics.evaluate_queries(data, records, scratch)
    cases_by_sample: dict[str, list[dict[str, Any]]] = {}
    for kind in ("positives", "negatives"):
        for case in report[kind].get("cases", []):
            cases_by_sample.setdefault(str(case.get("sample_id")), []).append(case)
    for record in records:
        record["query_cases"] = cases_by_sample.get(str(record.get("sample_id")), [])
    return report


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ocr_quality.run",
        description="跑一轮 OCR 质量评测：受控子进程 + 真实指标（缺失即 null）。")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--profile", default=BASELINE_PROFILE, choices=PROFILES)
    parser.add_argument("--out", required=True, type=Path, help="本次运行输出目录")
    parser.add_argument("--mode", default="auto", choices=MODES)
    parser.add_argument("--scale", type=float, default=DEFAULT_SCALE)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--params-json", type=Path,
                        help="RapidOCR 参数 JSON（只注入基线子进程，不改主环境）")
    parser.add_argument("--sample-id", action="append", dest="sample_ids",
                        help="只跑指定样本，可重复；仍保留完整数据集标识")
    parser.add_argument("--candidate-python", type=Path, help="候选环境的绝对 Python 路径")
    parser.add_argument("--candidate-script", type=Path, help="候选适配器脚本（默认内置）")
    parser.add_argument("--worker-script", type=Path, help="基线子进程脚本（默认内置）")
    parser.add_argument("--page-timeout", type=float, default=common.PAGE_TIMEOUT_SECONDS)
    parser.add_argument("--init-timeout", type=float, default=common.INIT_TIMEOUT_SECONDS)
    parser.add_argument("--max-rss-gib", type=float, default=8.0)
    parser.add_argument("--min-text-chars", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        out_root = run_benchmark(
            args.manifest, args.out, args.profile, scale=args.scale, repeats=args.repeats,
            mode=args.mode, sample_ids=args.sample_ids, params_json=args.params_json,
            candidate_python=args.candidate_python, candidate_script=args.candidate_script,
            worker_script=args.worker_script, page_timeout=args.page_timeout,
            init_timeout=args.init_timeout,
            max_rss_bytes=int(float(args.max_rss_gib) * 1024 ** 3),
            min_text_chars=args.min_text_chars)
    except (ValidationFailure, SafetyError) as exc:
        print(json.dumps({"error": {"kind": type(exc).__name__, "message": str(exc)}},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return EXIT_PROBLEMS

    meta = common.load_json(out_root / "run.json")
    print(json.dumps({"run": str(out_root), "status": meta["status"],
                      "counts": meta.get("counts", {}),
                      "unavailable_reason": meta.get("unavailable_reason")},
                     ensure_ascii=False, indent=2))
    if meta["status"] == "unavailable":
        return EXIT_UNAVAILABLE
    return EXIT_OK if meta["status"] == "ok" else EXIT_PROBLEMS


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
