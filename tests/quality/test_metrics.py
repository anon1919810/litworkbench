"""任务 2：字符错误率、检索核对与合并口径。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ocr_quality import metrics


# ----------------------------------------------------------------- 字符距离


@pytest.mark.parametrize("reference, hypothesis, distance", [
    ("白塔位于县城北侧", "白塔位于县邑北侧", 1),   # 替换
    ("白塔位于县城北侧", "白塔位于县城北", 1),     # 删除
    ("白塔位于县城北侧", "白塔位于县城北侧。", 1),  # 插入
    ("白塔位于县城北侧", "白塔位于县城南侧", 1),
    ("白塔", "白塔", 0),
])
def test_score_text_counts_single_edits(reference, hypothesis, distance):
    result = metrics.score_text(reference, hypothesis)
    assert result["status"] == "ok"
    assert result["distance"] == distance
    assert result["reference_chars"] == len(reference)
    assert result["cer"] == pytest.approx(distance / len(reference))


def test_score_text_ignores_whitespace_and_counts_codepoints():
    """跨行词按码点比较：空白不算错误，生僻字与 emoji 各算一个码点。"""
    a = "白塔\n位于\n县城北侧"
    b = "白 塔位于县城北侧"
    assert metrics.score_text(a, b)["distance"] == 0

    rare = metrics.score_text("鬱州鬱鬱", "鬱州郁郁")
    assert rare["reference_chars"] == 4 and rare["distance"] == 2

    astral = metrics.score_text("𠀀塔", "𠀁塔")
    assert astral["reference_chars"] == 2 and astral["distance"] == 1


def test_score_text_empty_reference_is_unavailable_not_zero():
    result = metrics.score_text("", "白塔")
    assert result["status"] == "unavailable"
    assert result["cer"] is None
    assert result["distance"] is None
    assert "参考" in result["reason"]


def test_score_text_empty_hypothesis_is_full_miss():
    """识别空串必须计漏检，不能当成「没有错误」。"""
    result = metrics.score_text("白塔位于县城北侧", "")
    assert result["status"] == "ok"
    assert result["distance"] == 8
    assert result["cer"] == 1.0


# ------------------------------------------------------------- 参考稿可用性


def test_score_page_draft_reference_has_no_cer():
    page = metrics.score_page({"review_level": "draft", "text": "白塔"}, "白塔")
    assert page["cer"] is None
    assert page["status"] == "unavailable"
    assert "draft" in page["reason"]


def test_score_page_uncertain_spans_disable_whole_page_cer():
    reference = {
        "review_level": "agent_checked", "text": "白塔残碑",
        "uncertain_spans": [{"line": 1, "text": "残碑"}],
    }
    page = metrics.score_page(reference, "白塔残碑")
    assert page["cer"] is None
    assert "uncertain_spans" in page["reason"]


def test_score_page_checked_reference_scores_and_records_level():
    page = metrics.score_page(
        {"review_level": "agent_checked", "text": "白塔位于县城北侧"}, "白塔位于城南"
    )
    assert page["status"] == "ok"
    assert page["review_level"] == "agent_checked"
    assert page["cer"] == pytest.approx(3 / 8)


def test_score_page_error_input_yields_null_metrics():
    page = metrics.score_page({"review_level": "human_checked", "text": "白塔"}, None,
                              error={"kind": "timeout", "message": "超过 180 秒"})
    assert page["cer"] is None
    assert page["distance"] is None
    assert page["status"] == "error"
    assert page["error"]["kind"] == "timeout"


# ----------------------------------------------------------------- 检索核对


def _dataset() -> dict:
    return {
        "samples": [
            {"sample_id": "syn-01", "doc_id": "doc_a", "file_page": 1},
            {"sample_id": "syn-02", "doc_id": "doc_a", "file_page": 2},
        ],
        "query_cases": [
            {"query_id": "q01", "kind": "positive", "sample_id": "syn-01", "query": "白塔"},
            {"query_id": "q02", "kind": "positive", "sample_id": "syn-02", "query": "残碑"},
            {"query_id": "q03", "kind": "positive", "sample_id": "syn-02", "query": "铁葫芦"},
            {"query_id": "q04", "kind": "negative", "sample_id": "syn-01", "query": "紫阳书院"},
        ],
    }


def _results(text1: str, text2: str) -> list[dict]:
    return [
        {"sample_id": "syn-01", "doc_id": "doc_a", "file_page": 1, "text": text1,
         "status": "ok", "case": "auto", "scale": 2.0, "engine": "rapidocr"},
        {"sample_id": "syn-02", "doc_id": "doc_a", "file_page": 2, "text": text2,
         "status": "ok", "case": "auto", "scale": 2.0, "engine": "rapidocr"},
    ]


def test_evaluate_queries_uses_fts_phrase_search(tmp_path):
    report = metrics.evaluate_queries(_dataset(), _results(
        "白塔位于县城北侧，为县级文物保护单位。", "塔基用条石砌成，塔身砖雕莲瓣纹。碑文另载铁葫芦。",
    ), tmp_path)

    assert report["positives"]["total"] == 3
    assert report["positives"]["hit"] == 2
    missed = {case["query_id"]: case for case in report["positives"]["missed"]}
    assert set(missed) == {"q02"}
    assert missed["q02"]["expected_page"] == {"doc_id": "doc_a", "file_page": 2}
    assert report["negatives"]["total"] == 1
    assert report["negatives"]["false_hit"] == 0
    hit = next(c for c in report["positives"]["cases"] if c["query_id"] == "q01")
    assert [item["sample_id"] for item in hit["matched"]] == ["syn-01"]
    assert hit["matched"][0]["file_page"] == 1
    assert hit["search_total"] == 1
    assert report["search_backend"] == "litworkbench.search_index"


def test_empty_ocr_text_counts_as_missed_query(tmp_path):
    """某一页识别为空：阳性用例必须计漏检，且不能因此把整页移出分母。"""
    report = metrics.evaluate_queries(_dataset(), _results("", "塔基用条石砌成。碑文另载铁葫芦。"),
                                     tmp_path)
    missed = {case["query_id"] for case in report["positives"]["missed"]}
    assert missed == {"q01", "q02"}
    empty = [case for case in report["positives"]["cases"] if case["query_id"] == "q01"][0]
    assert empty["reason"] == "engine_empty_text"
    assert report["empty_text_samples"] == ["syn-01"]


def test_negative_case_flags_false_hit_on_other_selected_page(tmp_path):
    report = metrics.evaluate_queries(
        _dataset(),
        _results("白塔", "塔基用条石砌成。紫阳书院旧址。"),
        tmp_path,
    )
    assert report["negatives"]["false_hit"] == 1
    case = report["negatives"]["cases"][0]
    assert [item["sample_id"] for item in case["matched"]] == ["syn-02"]
    assert case["false_hit"] is True


def test_evaluate_queries_survives_case_error_and_keeps_denominator(tmp_path):
    dataset = _dataset()
    dataset["query_cases"] = dataset["query_cases"] + [
        {"query_id": "q05", "kind": "positive", "sample_id": "syn-01", "query": "   "},
    ]
    report = metrics.evaluate_queries(dataset, _results("白塔", "塔基用条石砌成。"), tmp_path)
    assert report["positives"]["total"] == 4
    assert [case["query_id"] for case in report["failed_cases"]] == ["q05"]
    assert report["failed_cases"][0]["error"]
    assert report["positives"]["hit"] + len(report["positives"]["missed"]) == 4


def test_evaluate_queries_ignores_failed_records_but_counts_them(tmp_path):
    results = _results("白塔", "塔基用条石砌成。")
    results[1].update({"status": "timeout", "text": None,
                       "error": {"kind": "timeout", "message": "超时"}})
    report = metrics.evaluate_queries(_dataset(), results, tmp_path)
    assert report["failed_samples"] == ["syn-02"]
    missed = {case["query_id"] for case in report["positives"]["missed"]}
    assert missed == {"q02", "q03"}, "失败页的阳性用例计漏检，而不是静默消失"


def test_evaluate_queries_keeps_scratch_workspace_inside_root(tmp_path):
    scratch = tmp_path / "scratch"
    report = metrics.evaluate_queries(_dataset(), _results("白塔", "残碑"), scratch)
    assert scratch.exists()
    assert str(scratch.resolve()).startswith(str(tmp_path.resolve()))
    assert report["scratch_root"] == str(scratch.resolve())


# ------------------------------------------------------------------- 合并


def test_aggregate_reports_full_and_comparable_cohorts():
    scores = [
        {"sample_id": "a", "cer": 0.1, "reference_chars": 100, "status": "ok"},
        {"sample_id": "b", "cer": 0.5, "reference_chars": 300, "status": "ok"},
        {"sample_id": "c", "cer": None, "reference_chars": 0, "status": "unavailable",
         "reason": "参考稿为 draft"},
        {"sample_id": "d", "cer": None, "reference_chars": 0, "status": "error",
         "error": {"kind": "timeout", "message": "超时"}},
    ]
    summary = metrics.aggregate_page_scores(scores)
    assert summary["total_pages"] == 4
    assert summary["scored_pages"] == 2
    assert summary["unavailable_pages"] == 2
    assert summary["weighted_cer"] == pytest.approx((0.1 * 100 + 0.5 * 300) / 400)
    assert summary["failed_samples"] == ["d"]
    assert summary["unavailable_samples"] == ["c"]


def test_comparable_cohort_uses_shared_hashes_and_skips_failures():
    runs = [
        {"profile": "baseline", "inputs": {"a": "i1", "b": "i2"},
         "references": {"a": "r1", "b": "r2"}, "scorable": ["a", "b"], "failed": ["b"]},
        {"profile": "candidate", "inputs": {"a": "i1", "b": "i2-old"},
         "references": {"a": "r1", "b": "r2"}, "scorable": ["a", "b"], "failed": []},
    ]
    assert metrics.comparable_cohort(runs) == ["a"]


def test_comparable_cohort_empty_when_reference_hash_differs():
    runs = [
        {"profile": "baseline", "inputs": {"a": "i1"}, "references": {"a": "r1"},
         "scorable": ["a"], "failed": []},
        {"profile": "candidate", "inputs": {"a": "i1"}, "references": {"a": "r1-new"},
         "scorable": ["a"], "failed": []},
    ]
    assert metrics.comparable_cohort(runs) == []
    assert metrics.comparable_cohort([runs[0]]) == ["a"], "只有一个运行时即共同集合等于其可评分页"