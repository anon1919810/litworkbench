"""OCR 质量评测工具的测试夹具（全部为**合成数据**，不是真实县志）。

把项目根加入 ``sys.path``，使 ``scripts.ocr_quality.*``（命名空间包）可导入；
根目录本身没有 ``__init__.py``，这里不新增。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def digest_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest_tree(root: Path) -> dict[str, str]:
    """目录内容指纹（文件哈希 / 目录标记），用于证明只读边界没有被破坏。"""
    root = Path(root)
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = "dir" if path.is_dir() else digest_file(path)
    return out


@pytest.fixture
def project_root() -> Path:
    return ROOT


@pytest.fixture
def synthetic_library(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """一个**合成**文献库：1 份三页文字 PDF + 1 份无文字层扫描 PDF。

    导入时 ``ocr_mode="off"``，避免单元测试真的调用 OCR 引擎。
    """
    from litworkbench import demo
    from litworkbench.ingest import import_document
    from litworkbench.store import Workspace

    root = tmp_path / "合成库"
    ws = Workspace(root)
    ws.initialize()
    fixtures = demo.make_fixtures(tmp_path / "合成 夹具")
    text_doc = import_document(ws, fixtures["text_pdf"])
    scan_doc = import_document(ws, fixtures["scan_pdf"], ocr_mode="off")
    return root, {
        "fixtures": fixtures,
        "text_doc": text_doc,
        "scan_doc": scan_doc,
        "db": ws.db_path,
        "text_pdf": fixtures["text_pdf"],
        "scan_pdf": fixtures["scan_pdf"],
    }


def write_reference(path: Path, *, sample_id: str, text: str,
                    review_level: str = "human_checked", reviewer: str = "合成测试",
                    uncertain_spans: list | None = None,
                    reading_order: dict | None = None) -> Path:
    """写一份**合成**参考稿；review_level 与 reviewer 由测试显式指定。"""
    payload = {
        "sample_id": sample_id,
        "text": text,
        "review_level": review_level,
        "reviewer": reviewer,
        "uncertain_spans": list(uncertain_spans or []),
        "reading_order": reading_order or {"status": "unreviewed", "reviewer": "", "note": ""},
        "synthetic": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_sample(source_pdf: Path, *, sample_id: str, doc_id: str, file_page: int,
                **overrides: Any) -> dict[str, Any]:
    """构造一条符合清单契约的样本记录（未派生页图/参考稿）。"""
    from litworkbench.store import sha256_file

    sample = {
        "sample_id": sample_id,
        "source_sha256": sha256_file(source_pdf),
        "source_filename": source_pdf.name,
        "doc_id": doc_id,
        "file_page": file_page,
        "printed_page": "",
        "printed_page_status": "none",
        "tags": ["synthetic"],
        "page_pdf": "",
        "image_paths": [],
        "reference_path": "",
    }
    sample.update(overrides)
    return sample


def write_manifest(path: Path, samples: list[dict[str, Any]], *,
                   query_cases: list[dict[str, Any]] | None = None,
                   library_root: Path | str = "",
                   **extra: Any) -> Path:
    from scripts.ocr_quality import common

    payload = {
        "schema_version": common.SCHEMA_VERSION,
        "synthetic": True,
        "dataset_label": "合成测试数据（非真实县志）",
        "source_manifest": {"library_root_relative": "", "config_sha256": "",
                            "database_sha256": ""},
        "local_config": {"library_root": str(library_root), "output_root": str(path.parent)},
        "samples": samples,
        "query_cases": list(query_cases or []),
    }
    payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path