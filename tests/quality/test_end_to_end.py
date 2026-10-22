"""合成端到端：清点 → 选页 → 派生 → 运行（替身） → 报告。

全部数据为 :mod:`litworkbench.demo` 生成的合成 PDF，**不是真实县志**；
识别结果来自协议替身，不是真实 OCR，也不构成任何准确率结论。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.ocr_quality import common, prepare, report, run
from tests.quality.conftest import make_sample, write_manifest, write_reference

STUB_WORKER = '''
import argparse, json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--scale", default="2.0")
parser.add_argument("--repeats", default="1")
parser.add_argument("--params")
parser.add_argument("--init-timeout", type=float, default=600.0)
args = parser.parse_args()

payload = {
    "schema_version": 1, "status": "ok", "engine": "stub-worker", "engine_version": "0",
    "text": "白塔位于县城北侧",
    "lines": [{"box": [[1, 1], [2, 1], [2, 2], [1, 2]], "text": "白塔位于县城北侧",
               "confidence": 0.97}],
    "regions": [{"bbox": [1, 1, 2, 2], "page": 1, "source": "stub"}],
    "coordinate_system": "pixel_top_left", "granularity": "line",
    "mean_confidence": 0.97, "confidence_available": True,
    "config": {"scale": float(args.scale), "repeats": int(args.repeats)},
    "timing": {"init_seconds": 0.0, "render_seconds": [0.01], "recognize_seconds": [0.05]},
    "memory_self_reported": None, "model_fingerprints": [],
    "page": {"width_points": 200.0, "height_points": 300.0},
    "raw_output": {"stub": True},
}
Path(args.output).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
'''


def test_synthetic_end_to_end_pipeline(tmp_path, synthetic_library, capsys):
    library, data = synthetic_library
    worker = tmp_path / "stub_worker.py"
    worker.write_text(STUB_WORKER, encoding="utf-8")
    out = tmp_path / "eval"

    assert prepare.main(["--library", str(library), "--out", str(out)]) == 0
    assert (out / "inventory.json").exists()

    # 第一步只清点；样本由测试显式选定（与真实流程一样由人挑页）。
    draft = json.loads((out / "samples.draft.json").read_text(encoding="utf-8"))
    assert draft["samples"] == []

    samples = [
        make_sample(data["scan_pdf"], sample_id="syn-01", doc_id=data["scan_doc"]["id"],
                    file_page=1),
        make_sample(data["text_pdf"], sample_id="syn-02", doc_id=data["text_doc"]["id"],
                    file_page=2),
    ]
    for sample in samples:
        ref = write_reference(out / "refs" / f"{sample['sample_id']}.json",
                              sample_id=sample["sample_id"], text="白塔位于县城北侧")
        sample["reference_path"] = ref.relative_to(out).as_posix()
        sample["reference_sha256"] = common.sha256_file(ref)
    manifest = write_manifest(out / "samples.json", samples, library_root=library,
                              query_cases=[
        {"query_id": "q01", "kind": "positive", "sample_id": "syn-01", "query": "白塔"},
        {"query_id": "q02", "kind": "positive", "sample_id": "syn-02", "query": "白塔"},
        {"query_id": "q03", "kind": "negative", "sample_id": "syn-01", "query": "紫阳书院"},
    ])
    assert prepare.main(["--materialize", str(manifest)]) == 0
    report_json = prepare.main(["--validate", str(manifest)])
    assert report_json == 1, "样本与检索案例不足计划数量时应返回非零"

    run_dir = run.run_benchmark(manifest, tmp_path / "runs" / "baseline", "rapidocr-current",
                               worker_script=worker, repeats=1, mode="force")
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok"
    assert meta["record_count"] == 2
    assert meta["synthetic_worker"] is True

    html = report.build_report(manifest, [run_dir], tmp_path / "报告 输出")
    summary = json.loads((html.parent / "summary.json").read_text(encoding="utf-8"))
    assert summary["dataset_label"].startswith("合成")
    assert summary["runs"][0]["full_cohort"]["scored_pages"] == 2
    assert summary["accuracy_claim"]["scorable_pages"] == 2
    assert summary["runs"][0]["queries"]["positives"]["hit"] == 2
    text = html.read_text(encoding="utf-8")
    assert "syn-01" in text and "白塔位于县城北侧" in text
    assert (html.parent / "assets" / "syn-01_s2.png").exists()
    assert "http" not in text
    captured = capsys.readouterr()
    assert "合成" in captured.out