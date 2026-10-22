"""任务 4：本地可读对照报告（report.html + summary.json + metrics.csv）。

规则：

* 报告**只渲染清单与真实运行结果**，所有文字都经 HTML 转义，无外链脚本、
  无网络请求；页图从派生目录**复制**到报告的 ``assets/``，因此报告写到别处也可用；
* 每个样本都必须有结果或缺失原因：失败、超时、缺依赖、参考稿未核稿都写清楚，
  **不因为缺项就缩小分母**；
* 输入/参考/源哈希对不上时把该运行标成 ``过期``，不拿旧分数做比较；
* 没有已核稿参考就**不给准确率结论**；模型置信度不是准确率，报告不混用。
"""

from __future__ import annotations

import argparse
import csv
import difflib
import html
import io
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from scripts.ocr_quality import common, metrics
from scripts.ocr_quality.common import DERIVED_DIRNAME, SafetyError, ValidationFailure

EXIT_OK = 0
EXIT_PROBLEMS = 1

ASSET_SCALE_LABEL = "s2"
NO_METRIC_TEXT = "未提供指标"
STATUS_LABELS = {"ok": "成功", "timeout": "超时", "error": "失败",
                 "unavailable": "不可用（缺依赖或被禁用）"}


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _read_run(run_dir: Path) -> dict[str, Any]:
    directory = common.resolve(run_dir)
    meta = common.load_json(directory / "run.json") if (directory / "run.json").is_file() else {}
    records: list[dict[str, Any]] = []
    results = directory / "results.jsonl"
    if results.is_file():
        for line in results.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    queries: dict[str, Any] = {}
    if (directory / "queries.json").is_file():
        queries = common.load_json(directory / "queries.json")
    _attach_persisted_queries(records, queries)
    return {
        "dir": directory,
        "meta": meta,
        "records": records,
        "queries": queries,
        "has_results_file": results.is_file(),
    }


def _attach_persisted_queries(records: list[dict[str, Any]], queries: dict[str, Any]) -> None:
    """记录里没有逐页用例时，从 ``queries.json`` 按样本补齐（命中与漏检都补）。"""
    if not queries:
        return
    cases: dict[str, list[dict[str, Any]]] = {}
    for kind in ("positives", "negatives"):
        for case in queries.get(kind, {}).get("cases", []) or []:
            cases.setdefault(str(case.get("sample_id")), []).append(case)
    for record in records:
        if record.get("query_cases"):
            continue
        record["query_cases"] = cases.get(str(record.get("sample_id")), [])


def _expand_run_paths(run_paths: Sequence[Path | str]) -> list[Path]:
    """``--runs`` 可以给运行目录，也可以给包含多个运行的父目录。"""
    found: list[Path] = []
    for item in run_paths:
        path = common.resolve(item)
        if (path / "run.json").is_file() or (path / "results.jsonl").is_file():
            found.append(path)
            continue
        if path.is_dir():
            children = [child for child in sorted(path.iterdir())
                        if child.is_dir() and (child / "run.json").is_file()]
            # 空的运行目录也要显示「没有结果文件」，不能静默跳过。
            found.extend(children or [path])
    return found


def _stale_reason(record: dict[str, Any], sample: dict[str, Any],
                  out_root: Path) -> str | None:
    """结果与当前清单/参考/源哈希对不上就标过期，不参与比较。"""
    page_pdf = str(sample.get("page_pdf") or "")
    for key in ("source_sha256", "doc_id", "file_page"):
        if record.get(key) != sample.get(key):
            return f"来源定位已变化：{key}"
    if record.get("input_sha256") and not page_pdf:
        return "输入页引用已清空"
    if record.get("reference_sha256") and not sample.get("reference_path"):
        return "参考稿引用已清空"
    if page_pdf and record.get("input_sha256"):
        if not (out_root / page_pdf).is_file():
            return "输入页已缺失"
        current = common.sha256_file(out_root / page_pdf)
        if current != record["input_sha256"]:
            return f"输入页已变化：运行记录 {record['input_sha256'][:12]}，当前 {current[:12]}"
    reference = str(sample.get("reference_path") or "")
    if reference and record.get("reference_sha256"):
        target = out_root / reference
        if not target.is_file():
            return "参考稿已缺失"
        if target.is_file():
            current = common.sha256_file(target)
            if current != record["reference_sha256"]:
                return (f"参考稿已变更：运行记录 {record['reference_sha256'][:12]}，"
                        f"当前 {current[:12]}")
    if sample.get("source_sha256") and record.get("source_sha256") \
            and sample["source_sha256"] != record["source_sha256"]:
        return "原件哈希与清单不一致"
    return None


def _char_diff(reference: str, hypothesis: str) -> list[tuple[str, str]]:
    """逐字差异（按去空白后的码点序列），用于人工核对错字位置。"""
    left = common.strip_spaces(reference or "")
    right = common.strip_spaces(hypothesis or "")
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    pieces: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pieces.append(("same", left[i1:i2]))
        elif tag == "delete":
            pieces.append(("missing", left[i1:i2]))
        elif tag == "insert":
            pieces.append(("extra", right[j1:j2]))
        else:
            pieces.append(("missing", left[i1:i2]))
            pieces.append(("extra", right[j1:j2]))
    return [(kind, text) for kind, text in pieces if text]


def _reference_changed(sample: dict[str, Any], out_root: Path) -> bool:
    reference = str(sample.get("reference_path") or "")
    recorded = sample.get("reference_sha256")
    if not reference or not recorded:
        return False
    target = out_root / reference
    return target.is_file() and common.sha256_file(target) != str(recorded)


def _load_reference(sample: dict[str, Any], out_root: Path) -> dict[str, Any] | None:
    path = str(sample.get("reference_path") or "")
    target = out_root / path if path else None
    if target is None or not target.is_file():
        return None
    try:
        return common.load_json(target)
    except Exception:  # pragma: no cover - 损坏参考稿按缺失处理
        return None


def _copy_assets(samples: list[dict[str, Any]], out_root: Path,
                 report_dir: Path) -> dict[str, str]:
    """把页图复制到报告目录，保持相对引用（报告写到别处也可用）。"""
    assets = report_dir / "assets"
    mapping: dict[str, str] = {}
    for sample in samples:
        common.validate_sample_id(sample.get("sample_id"))
        for relative in sample.get("image_paths") or []:
            source = out_root / relative
            if not source.is_file():
                continue
            name = f"{sample.get('sample_id')}_{Path(relative).stem.split('_')[-1]}.png"
            target = common.safe_target(report_dir, assets / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            mapping[f"{sample.get('sample_id')}::{relative}"] = f"assets/{name}"
    return mapping


def _run_summary(run: dict[str, Any], samples: list[dict[str, Any]],
                 out_root: Path) -> dict[str, Any]:
    """单个运行的分数汇总：全体样本 + 共同可比集合，过期运行不参与比较。"""
    records = run["records"]
    expired_reasons: dict[str, str] = {}
    live: list[dict[str, Any]] = []
    for record in records:
        sample = next((item for item in samples
                       if str(item.get("sample_id")) == str(record.get("sample_id"))), None)
        reason = _stale_reason(record, sample or {}, out_root) if sample else "清单里没有该样本"
        if record.get("run_id") != run["meta"].get("run_id"):
            reason = "结果不属于当前运行"
        if reason:
            expired_reasons[str(record.get("sample_id"))] = reason
        else:
            live.append(record)

    scores = [dict(record.get("metrics") or {}, sample_id=record.get("sample_id"))
              for record in live]
    full = metrics.aggregate_page_scores(scores)
    covered = {str(record.get("sample_id")) for record in live}
    full["missing_pages"] = sorted({str(item.get("sample_id")) for item in samples} - covered)
    # 全体口径按清单里的**全部样本**计算：失败、超时、缺依赖都不缩小分母。
    full["total_pages"] = len(samples)
    full["unavailable_pages"] = len(samples) - full["scored_pages"]
    full["expired_samples"] = sorted(expired_reasons)
    comparable_input = {
        "inputs": {str(record.get("sample_id")): record.get("input_sha256") for record in live},
        "references": {str(record.get("sample_id")): record.get("reference_sha256")
                       for record in live},
        "scorable": [str(record.get("sample_id")) for record in live
                     if (record.get("metrics") or {}).get("cer") is not None],
        "failed": [str(record.get("sample_id")) for record in live if record.get("status") != "ok"],
    }
    comparable_ids = metrics.comparable_cohort([comparable_input])
    comparable_records = [record for record in live
                          if str(record.get("sample_id")) in comparable_ids]
    queries = {key: run["queries"].get(key) for key in ("positives", "negatives", "failed_cases")
               if key in run["queries"]}
    if expired_reasons and queries:
        queries = {
            "status": "expired", "reason": "输入、参考或运行身份已变化；检索指标不可沿用",
            "positives": {"total": (queries.get("positives") or {}).get("total"), "hit": None},
            "negatives": {"total": (queries.get("negatives") or {}).get("total"), "false_hit": None},
        }
    return {
        "profile": run["meta"].get("profile") or "unknown",
        "run_id": run["meta"].get("run_id"),
        "run_dir": str(run["dir"]),
        "status": run["meta"].get("status"),
        "record_count": len(records),
        "expired": bool(expired_reasons),
        "expired_reason": "；".join(f"{key}: {value}" for key, value in
                                   sorted(expired_reasons.items())) or None,
        "full_cohort": full,
        "comparable_cohort": {
            "sample_ids": comparable_ids,
            "weighted_cer": metrics.aggregate_page_scores(
                [dict(record.get("metrics") or {}, sample_id=record.get("sample_id"))
                 for record in comparable_records])["weighted_cer"],
        },
        "cohort_input": comparable_input,
        "config": run["meta"].get("config"),
        "environment": run["meta"].get("environment"),
        "max_rss_bytes": run["meta"].get("max_rss_bytes"),
        "queries": queries,
        "synthetic_worker": run["meta"].get("synthetic_worker"),
    }


def _sample_block(sample: dict[str, Any], runs: list[dict[str, Any]],
                  references: dict[str, Any], out_root: Path,
                  assets: dict[str, str]) -> str:
    sample_id = str(sample.get("sample_id"))
    reference = references.get(sample_id)
    rows: list[str] = []
    reading_order = sample.get("reading_order") or {}
    order = str(reading_order.get("status") or "unreviewed")
    order_label = {"unreviewed": "未复核", "ok": "正确", "wrong": "有错",
                   "unavailable": "无法判断"}.get(order, order)
    rows.append(
        f"<p class=\"meta\">源文件：{_escape(sample.get('source_filename'))} · "
        f"文件页 {_escape(sample.get('file_page'))} · 印刷页 "
        f"{_escape(sample.get('printed_page') or '（未标）')}（"
        f"{_escape(sample.get('printed_page_status') or 'none')}） · "
        f"参考稿核稿：{_escape(common.reference_review_level(reference))} · "
        f"阅读顺序核对：{_escape(order_label)}（核对人："
        f"{_escape(reading_order.get('reviewer') or '—')}）</p>")
    if _reference_changed(sample, out_root):
        rows.append("<p class=\"warn\">参考稿已变更：旧评分标记为过期，需重新计算。</p>")

    primary = None
    for relative in sample.get("image_paths") or []:
        if f"{sample_id}::{relative}" in assets:
            primary = assets[f"{sample_id}::{relative}"]
            break
    if primary:
        rows.append(f"<p><img src=\"{_escape(primary)}\" alt=\"原图\"></p>")
    else:
        rows.append(f"<p class=\"warn\">没有派生页图：{_escape(sample_id)}</p>")

    if reference is not None:
        rows.append(f"<h4>参考稿</h4><pre class=\"ref\">{_escape(reference.get('text'))}</pre>")
        scorable, reason = common.reference_is_scorable(reference)
        if not scorable:
            rows.append(f"<p class=\"warn\">{NO_METRIC_TEXT}：{_escape(reason)}"
                        "；本页只报告运行结果与逐字差异，不给整页 CER。</p>")
    else:
        rows.append(f"<p class=\"warn\">{NO_METRIC_TEXT}：没有参考稿，无法核对。</p>")

    for run in runs:
        record = next((item for item in run["records"]
                       if str(item.get("sample_id")) == sample_id), None)
        rows.append(_run_block(run, record, sample, reference, out_root))
    return f"<section class=\"sample\"><h3>{_escape(sample_id)}</h3>{''.join(rows)}</section>"


def _run_block(run: dict[str, Any], record: dict[str, Any] | None,
               sample: dict[str, Any], reference: dict[str, Any] | None,
               out_root: Path) -> str:
    profile = run["meta"].get("profile") or "unknown"
    directory = run.get("dir")
    label = f"{Path(directory).name} · {profile}" if directory else profile
    if record is None:
        return (f"<div class=\"run\"><h4>{_escape(label)}</h4>"
                f"<p class=\"warn\">没有结果文件或缺少该页记录。</p></div>")
    stale = _stale_reason(record, sample, out_root)
    if record.get("run_id") != run["meta"].get("run_id"):
        stale = "结果不属于当前运行"
    scored = record.get("metrics") or {}
    parts = [f"<div class=\"run\"><h4>{_escape(label)}</h4>"]
    if stale:
        parts.append(f"<p class=\"warn\">过期（不参与比较）：{_escape(stale)}</p>")
    if record.get("status") != "ok":
        error = record.get("error") or {}
        status_label = STATUS_LABELS.get(str(record.get("status")), str(record.get("status")))
        parts.append(f"<p class=\"bad\">状态 {_escape(status_label)}（{_escape(record.get('status'))}） · "
                     f"{_escape(error.get('kind'))}：{_escape(error.get('message'))}</p>")
    metric_line = (f"CER {_esc_cer(scored.get('cer'))} · 编辑距离 "
                   f"{_esc_number(scored.get('distance'))} / 参考字数 "
                   f"{_esc_number(scored.get('reference_chars'))}")
    parts.append(f"<p class=\"metric\">{metric_line}"
                 + (f"（{_escape(scored.get('reason'))}）" if scored.get("reason") else "")
                 + "</p>")
    timing = record.get("timing") or {}
    parts.append(
        "<p class=\"meta\">耗时：初始化 "
        f"{_esc_number(timing.get('init_seconds'))} 秒 · 渲染中位数 "
        f"{_esc_number(timing.get('median_render_seconds'))} 秒 · 识别中位数 "
        f"{_esc_number(timing.get('median_recognize_seconds'))} 秒 · 连续识别 "
        f"{_esc_number(timing.get('repeats'))} 次（首轮可能含冷成本）</p>")
    memory = record.get("memory") or {}
    parts.append(
        "<p class=\"meta\">内存采样峰值："
        + (f"{_esc_number(memory.get('peak_rss_bytes'))} 字节"
           if memory.get("peak_rss_bytes") is not None else "null（未采样）")
        + (f" · 原因：{_escape(memory.get('reason'))}" if memory.get("reason") else "")
        + f" · 闸门 {_esc_number(memory.get('limit_bytes'))} 字节</p>")
    parts.append(
        "<p class=\"meta\">坐标：" + _escape(record.get("coordinate_system"))
        + " · 粒度：" + _escape(record.get("granularity"))
        + " · 置信度：" + (f"{_esc_number(record.get('mean_confidence'))}"
                           if record.get("mean_confidence") is not None else "null")
        + (f"（{_escape(record.get('confidence_reason'))}）"
           if record.get("confidence_reason") else "")
        + " · 引擎：" + _escape(record.get("engine"))
        + f" {'' if record.get('synthetic_worker') is not True else '（替身，非真实识别）'}</p>")
    fingerprints = record.get("model_fingerprints") or []
    if fingerprints:
        rows = "".join(
            f"<li>{_escape(item.get('filename') or item.get('path') or '未知')}"
            f" · {_escape(item.get('sha256') or 'null')} · {_escape(item.get('status'))}</li>"
            for item in fingerprints)
        parts.append(f"<p class=\"meta\">模型指纹（实际加载文件）：</p><ul>{rows}</ul>")
    parts.append(f"<pre class=\"ocr\">{_escape(record.get('text'))}</pre>")
    if reference is not None and record.get("text") is not None:
        diff = _char_diff(reference.get("text") or "", record.get("text") or "")
        marks = "".join(
            f"<span class=\"d-{kind}\">{_escape(text)}</span>" for kind, text in diff)
        parts.append(f"<p class=\"meta\">逐字差异（<span class=\"d-missing\">缺/错</span>、"
                     f"<span class=\"d-extra\">多</span>）：</p><p class=\"diff\">{marks}</p>")
    cases = record.get("query_cases") or []
    if cases:
        rows = "".join(
            f"<li>{_escape(case.get('query_id'))}：{_escape(case.get('query'))}"
            f" · {_case_verdict(case)}"
            + (f"（{_escape(case.get('reason'))}）" if case.get("reason") else "")
            + (f" · 期望页 {_escape((case.get('expected_page') or {}).get('doc_id'))}"
               f" 第 {_escape((case.get('expected_page') or {}).get('file_page'))} 页"
               if case.get("expected_page") else "")
            + f"</li>" for case in cases)
        parts.append(f"<p class=\"meta\">检索用例：</p><ul>{rows}</ul>")
    parts.append("</div>")
    return "".join(parts)


def _case_verdict(case: dict[str, Any]) -> str:
    if case.get("hit") is True:
        return "命中"
    if case.get("false_hit"):
        return "阴性异常命中"
    if case.get("hit") is False:
        return "未命中"
    if case.get("error"):
        return f"用例出错（{_escape(case['error'])}）"
    return "未核对"


def _esc_number(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.4f}"
    return _escape(value)


def _esc_cer(value: Any) -> str:
    return f"{value * 100:.3f}%" if isinstance(value, (int, float)) else _esc_number(value)


def _query_table(data: dict[str, Any]) -> str:
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            _escape(case.get("query_id")), _escape(case.get("kind")),
            _escape(case.get("sample_id")), _escape(case.get("query")))
        for case in data.get("query_cases") or [])
    return ("<h3>检索用例清单</h3><table><tr><th>编号</th><th>类型</th><th>样本</th>"
            f"<th>检索词</th></tr>{rows}</table>")


def _summary_table(runs: list[dict[str, Any]], comparable: list[str]) -> str:
    rows = ""
    for item in runs:
        full = item["full_cohort"]
        rows += (
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td></tr>".format(
                _escape(Path(item["run_dir"]).name), _escape(item["status"]),
                full["scored_pages"], full["total_pages"],
                _esc_cer(full["weighted_cer"]),
                _esc_cer(item["comparable_cohort"]["weighted_cer"]),
                "过期" if item["expired"] else "有效"))
    return ("<h3>汇总</h3><p class=\"meta\">共同可比集合："
            f"{_escape(', '.join(comparable) or '（空）')}；"
            "加权 CER 按参考字数加权，缺失分母如实列出。</p>"
            "<table><tr><th>方案</th><th>状态</th><th>可评分页</th><th>全体页</th>"
            f"<th>已评分页加权 CER</th><th>共同集合加权 CER</th><th>哈希</th></tr>{rows}</table>")


def build_report(manifest: Path | str, run_paths: Sequence[Path | str],
                 output_root: Path | str, *, copy_assets: bool = True) -> Path:
    """产出 ``report.html``、``summary.json`` 与 ``metrics.csv``。"""
    manifest_path = common.resolve(manifest)
    data = common.load_json(manifest_path)
    source_root = manifest_path.parent
    library_root = str(data.get("local_config", {}).get("library_root") or "")
    report_dir = common.ensure_safe_output(
        output_root, library_root=library_root or None,
        protected=[("派生文件目录", source_root / DERIVED_DIRNAME)], label="报告目录")
    report_dir.mkdir(parents=True, exist_ok=True)

    runs = [_read_run(path) for path in _expand_run_paths(run_paths)]
    samples = list(data.get("samples") or [])
    summaries = [_run_summary(run, samples, source_root) for run in runs]
    comparable = metrics.comparable_cohort([item["cohort_input"] for item in summaries])
    for item, source_run in zip(summaries, runs):
        scores = [dict(record.get("metrics") or {}, sample_id=record.get("sample_id"))
                  for record in source_run["records"]
                  if str(record.get("sample_id")) in comparable]
        item["comparable_cohort"] = {
            "sample_ids": list(comparable),
            "weighted_cer": metrics.aggregate_page_scores(scores)["weighted_cer"],
        }
        item.pop("cohort_input", None)

    references = {str(item.get("sample_id")): _load_reference(item, source_root)
                  for item in samples}
    assets = _copy_assets(samples, source_root, report_dir) if copy_assets else {}
    reference_changed = [str(item.get("sample_id")) for item in samples
                         if _reference_changed(item, source_root)]
    accuracy_claim = None
    if comparable:
        accuracy_claim = {
            "scorable_pages": len(comparable),
            "comparable_sample_ids": comparable,
            "reference_review_levels": sorted({
                common.reference_review_level(references[str(sample_id)])
                for sample_id in comparable if str(sample_id) in references}),
            "statement": "仅在共同可比集合的已核稿参考上计算；不代表整库或长书准确率。",
        }

    summary_payload = {
        "generated_at": common.now_iso(),
        "dataset_label": data.get("dataset_label") or "",
        "synthetic": bool(data.get("synthetic")),
        "manifest": str(manifest_path),
        "manifest_sha256": common.sha256_file(manifest_path),
        "library_root_relative": data.get("source_manifest", {}).get(
            "library_root_relative", ""),
        "sample_count": len(samples),
        "comparable_sample_ids": comparable,
        "manifest_reference_changed": reference_changed,
        "accuracy_claim": accuracy_claim,
        "runs": summaries,
        "notes": [
            "缺依赖、超时与未核稿参考都按缺失计入分母，不做剔除。",
            "模型置信度不是准确率；阅读顺序必须逐页人工核对。",
        ],
    }
    common.dump_json(report_dir / "summary.json", summary_payload)
    _write_metrics_csv(report_dir / "metrics.csv", runs, samples, source_root)

    body = "".join(_sample_block(sample, runs, references, source_root, assets)
                   for sample in samples)
    report_html = _render_html(data, summaries, comparable, reference_changed, body)
    target = common.safe_target(report_dir, report_dir / "report.html")
    target.write_text(report_html, encoding="utf-8")
    return target


def _write_metrics_csv(path: Path, runs: list[dict[str, Any]],
                       samples: list[dict[str, Any]], source_root: Path) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["profile", "sample_id", "case", "status", "cer", "distance",
                     "reference_chars", "review_level", "input_sha256", "reference_sha256",
                     "init_seconds", "median_render_seconds", "median_recognize_seconds",
                     "peak_rss_bytes", "error"])
    for run in runs:
        profile = run["meta"].get("profile") or "unknown"
        for sample in samples:
            record = next((item for item in run["records"]
                           if str(item.get("sample_id")) == str(sample.get("sample_id"))), None)
            if record is None:
                writer.writerow([profile, sample.get("sample_id"), "", "missing", "", "", "",
                                 common.reference_review_level(None), "", "", "", "", "", "", ""])
                continue
            scored = record.get("metrics") or {}
            stale = _stale_reason(record, sample, source_root) or (
                "结果不属于当前运行" if record.get("run_id") != run["meta"].get("run_id") else None)
            if stale:
                scored = {}
            timing = record.get("timing") or {}
            memory = record.get("memory") or {}
            error = record.get("error") or {}
            writer.writerow([
                profile, sample.get("sample_id"), record.get("case"), "expired" if stale else record.get("status"),
                scored.get("cer"), scored.get("distance"), scored.get("reference_chars"),
                record.get("reference_review_level"), record.get("input_sha256"),
                record.get("reference_sha256"), timing.get("init_seconds"),
                timing.get("median_render_seconds"), timing.get("median_recognize_seconds"),
                memory.get("peak_rss_bytes"), error.get("message"),
            ])
    common.safe_target(path.parent, path).write_text(buffer.getvalue(), encoding="utf-8")


def _render_html(data: dict[str, Any], summaries: list[dict[str, Any]], comparable: list[str],
                 reference_changed: list[str], body: str) -> str:
    label = data.get("dataset_label") or f"本地样本（{len(data.get('samples') or [])} 页）"
    synthetic = "（合成/测试数据，不代表真实县志）" if data.get("synthetic") else ""
    changed = ("<p class=\"warn\">参考稿已变更："
               f"{_escape(', '.join(reference_changed))}；相关旧分数已标过期。</p>"
               if reference_changed else "")
    claim = summaries[0] if summaries else {}
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>OCR 质量对照报告{synthetic}</title>
<style>
body {{ font-family: "Microsoft YaHei", sans-serif; margin: 1.5rem; color: #222; }}
table {{ border-collapse: collapse; margin: 0.5rem 0 1rem; }}
td, th {{ border: 1px solid #ccc; padding: 4px 8px; font-size: 14px; text-align: left; }}
section.sample {{ border: 1px solid #ddd; border-radius: 6px; padding: 0.8rem; margin-bottom: 1rem; }}
div.run {{ border-left: 3px solid #999; padding-left: 0.8rem; margin: 0.8rem 0; }}
pre {{ white-space: pre-wrap; word-break: break-all; background: #f7f7f7; padding: 0.6rem; }}
p.warn {{ color: #a15c00; }} p.bad {{ color: #b00020; }} p.metric {{ font-weight: 600; }}
.d-missing {{ background: #ffd7d7; text-decoration: line-through; }}
.d-extra {{ background: #d7f0ff; }} img {{ max-width: 420px; border: 1px solid #bbb; }}
</style></head><body>
<h1>OCR 质量对照报告</h1>
<p class="warn">代理核对参考稿仍是暂定参考，不等于人工金标准。含未决字的整页不计 CER。这里的 CER 是错误率，越低越好。</p>
<p class="meta">数据集：{_escape(label)}{_escape(synthetic)} · 生成时间：{_escape(common.now_iso())}</p>
<p class="meta">本页只用本地文件与相对引用，不加载任何外部脚本或资源。</p>
{changed}
{_summary_table(summaries, comparable)}
{_query_table(data)}
{body}
</body></html>
"""


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ocr_quality.report",
        description="生成 OCR 质量本地对照报告（HTML + JSON + CSV）。")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--runs", nargs="+", required=True, type=Path,
                        help="运行目录，或包含多个运行的父目录")
    parser.add_argument("--out", required=True, type=Path, help="报告输出目录")
    parser.add_argument("--no-assets", action="store_true",
                        help="不复制页图（默认复制到 assets/ 以支持报告异地输出）")
    args = parser.parse_args(argv)

    try:
        target = build_report(args.manifest, args.runs, args.out,
                              copy_assets=not args.no_assets)
    except (ValidationFailure, SafetyError) as exc:
        print(json.dumps({"error": {"kind": type(exc).__name__, "message": str(exc)}},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return EXIT_PROBLEMS
    print(json.dumps({"report": str(target), "summary": str(target.parent / "summary.json"),
                      "metrics_csv": str(target.parent / "metrics.csv")}, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
