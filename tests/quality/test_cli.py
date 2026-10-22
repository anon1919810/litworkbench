"""命令行入口：``python -m scripts.ocr_quality.X`` 必须真的执行。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ocr_quality import common

MODULES = ("prepare", "run", "report")


def _run_module(module: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", f"scripts.ocr_quality.{module}", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600, cwd=str(common.project_root()),
        env={**_env(), "PYTHONIOENCODING": "utf-8"},
    )


def _env() -> dict:
    import os

    return dict(os.environ)


@pytest.mark.parametrize("module", MODULES)
def test_module_help_documents_cli(module):
    completed = _run_module(module, "--help")
    assert completed.returncode == 0, completed.stderr[-1500:]
    assert "usage:" in completed.stdout
    assert "--help" in completed.stdout


def test_prepare_cli_creates_inventory_and_draft(synthetic_library, tmp_path):
    library, _ = synthetic_library
    out = tmp_path / "cli-eval"

    completed = _run_module("prepare", "--library", str(library), "--out", str(out))

    assert completed.returncode == 0, completed.stderr[-2000:]
    payload = json.loads(completed.stdout)
    assert (out / "inventory.json").is_file()
    assert Path(payload["draft_manifest"]).is_file()
    assert json.loads(Path(payload["draft_manifest"]).read_text("utf-8"))["samples"] == []


def test_prepare_cli_validate_returns_nonzero_for_incomplete_manifest(tmp_path, synthetic_library):
    from scripts.ocr_quality import prepare
    from tests.quality.conftest import make_sample, write_manifest

    library, data = synthetic_library
    out = tmp_path / "cli-eval"
    manifest = write_manifest(out / "samples.json", [
        make_sample(data["scan_pdf"], sample_id="syn-01", doc_id="doc_x", file_page=1)
    ], library_root=library)
    prepare.materialize_dataset(manifest)

    completed = _run_module("prepare", "--validate", str(manifest))

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["ok"] is False
    assert "sample_count_below_plan" in {item["code"] for item in report["errors"]}


def test_run_cli_writes_results_and_honours_sample_filter(tmp_path, synthetic_library, fake_worker):
    from scripts.ocr_quality import prepare
    from tests.quality.conftest import make_sample, write_manifest, write_reference

    library, data = synthetic_library
    out = tmp_path / "cli-eval"
    samples = [make_sample(data["scan_pdf"], sample_id="syn-a", doc_id="doc_a", file_page=1),
               make_sample(data["text_pdf"], sample_id="syn-b", doc_id="doc_b", file_page=2)]
    for sample in samples:
        ref = write_reference(out / "refs" / f"{sample['sample_id']}.json",
                              sample_id=sample["sample_id"], text="白塔位于县城北侧")
        sample["reference_path"] = ref.relative_to(out).as_posix()
        sample["reference_sha256"] = common.sha256_file(ref)
    manifest = write_manifest(out / "samples.json", samples, library_root=library)
    prepare.materialize_dataset(manifest)

    completed = _run_module("run", "--manifest", str(manifest), "--profile", "rapidocr-current",
                            "--worker-script", str(fake_worker), "--repeats", "1",
                            "--sample-id", "syn-a", "--out", str(tmp_path / "cli-run"))

    assert completed.returncode == 0, completed.stderr[-2000:]
    records = [json.loads(line) for line in
               (tmp_path / "cli-run" / "results.jsonl").read_text("utf-8").splitlines() if line]
    assert [item["sample_id"] for item in records] == ["syn-a"]
    assert json.loads((tmp_path / "cli-run" / "run.json").read_text("utf-8"))["status"] == "ok"


@pytest.fixture
def fake_worker(tmp_path) -> Path:
    from tests.quality.test_runner import FAKE_WORKER

    path = tmp_path / "cli_fake_worker.py"
    path.write_text(FAKE_WORKER, encoding="utf-8")
    return path