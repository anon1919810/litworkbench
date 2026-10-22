"""任务 1：只读清点、派生单页 PDF/PNG、清单与参考稿校验。

三个子命令各自独立，都拒绝把输出写进源目录或真实库：

* ``--library/--out``：只读 SQLite（``mode=ro``）清点，输出 ``inventory.json`` 与
  ``samples.draft.json``；**不自动挑页、不 OCR**；
* ``--materialize``：在清单**已经显式选定样本**后，派生一页 PDF 与 2/3/4 倍率页图，
  保留 ``source_sha256`` 与 ``file_page``（候选看到的永远是派生 PDF 的第 1 页）；
* ``--validate``：核对原件哈希、页序、派生文件、真实库与输出边界、参考稿核稿状态。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from scripts.ocr_quality import common
from scripts.ocr_quality.common import (
    DRAFT_MANIFEST_FILENAME,
    INVENTORY_FILENAME,
    DERIVED_DIRNAME,
    FULLPAGE_REVIEWED,
    MANIFEST_FILENAME,
    PLAN_NEGATIVE_QUERIES,
    PLAN_POSITIVE_QUERIES,
    PLAN_SAMPLE_COUNT,
    SafetyError,
    ValidationFailure,
    default_scales,
)

EXIT_OK = 0
EXIT_PROBLEMS = 1


# ------------------------------------------------------------------ 清点


def _library_config(library_root: Path) -> tuple[dict[str, Any], Path, Path]:
    """返回 ``(配置, 数据库路径, 配置文件路径)``；库根只读打开。"""
    from litworkbench import config as config_mod

    config_path = Path(library_root) / config_mod.CONFIG_FILENAME
    if not config_path.exists():
        raise ValidationFailure(f"找不到工作区配置：{config_path}")
    config = config_mod.load_config(config_path)
    db_name = str(config.get("workspace", {}).get("db_file", "litworkbench.sqlite3"))
    return config, Path(library_root) / db_name, config_path


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """只读连接：URI ``mode=ro`` + ``query_only``，从连接层面禁止写入。"""
    if not db_path.exists():
        raise ValidationFailure(f"找不到文献库数据库：{db_path}")
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _page_text_layer(page: Any) -> str:
    from litworkbench.text import collapse_pdf_text

    textpage = page.get_textpage()
    try:
        return collapse_pdf_text(textpage.get_text_range() or "")
    finally:
        textpage.close()


def build_inventory(library_root: Path | str) -> dict[str, Any]:
    """逐文献逐页清点真实库（只读）。不选页、不 OCR、不把既有 OCR 当真值。"""
    from litworkbench.text import first_meaningful_chars

    library_root = common.resolve(library_root)
    config, db_path, config_path = _library_config(library_root)
    min_chars = int(config.get("ocr", {}).get("min_text_chars", 20))
    conn = _open_readonly(db_path)
    try:
        documents = [dict(row) for row in conn.execute(
            "SELECT id, filename, sha256, stored_path, filetype, page_count, status, ocr_mode "
            "FROM documents ORDER BY filename")]
        units: dict[tuple[str, int], dict[str, Any]] = {}
        for row in conn.execute(
                "SELECT doc_id, file_page, method, status, extraction_status, printed_page, "
                "       printed_page_status, text_sha256, corrected_text FROM units"):
            units[(row["doc_id"], int(row["file_page"] or 0))] = dict(row)
        corrections = conn.execute(
            "SELECT COUNT(*) AS n FROM units WHERE corrected_text IS NOT NULL").fetchone()["n"]
        evidence = conn.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"]
    finally:
        conn.close()

    pages: list[dict[str, Any]] = []
    for document in documents:
        stored = library_root / str(document["stored_path"])
        entry = {
            "doc_id": document["id"],
            "filename": document["filename"],
            "source_sha256": document["sha256"],
            "source_relative_path": str(document["stored_path"]),
            "filetype": document["filetype"],
            "library_status": document["status"],
            "library_ocr_mode": document["ocr_mode"],
            "library_page_count": document["page_count"],
        }
        if document["filetype"] != "pdf" or not stored.exists():
            pages.append({**entry, "skipped": "原件不在库内或不是 PDF"})
            continue

        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(stored))
        try:
            entry["page_count"] = len(pdf)
            entry["library_page_count_matches"] = (len(pdf) == int(
                document["page_count"] or len(pdf)))
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    text = _page_text_layer(page)
                finally:
                    page.close()
                file_page = index + 1
                unit = units.get((document["id"], file_page), {})
                chars = first_meaningful_chars(text)
                pages.append({
                    **entry,
                    "file_page": file_page,
                    "printed_page": unit.get("printed_page", ""),
                    "printed_page_status": unit.get("printed_page_status") or "none",
                    "library_method": unit.get("method"),
                    "library_status_page": unit.get("status"),
                    "library_extraction_status": unit.get("extraction_status"),
                    "text_layer_meaningful_chars": chars,
                    "text_layer_char_count": len(common.strip_spaces(text)),
                    "text_layer_sufficient": chars >= min_chars,
                    # 只存哈希：既有 OCR 文本不能当参考真值。
                    "text_layer_sha256": common.sha256_text(text),
                    "library_text_sha256": unit.get("text_sha256") or None,
                    "has_correction": unit.get("corrected_text") is not None,
                    "text_layer_needs_review": chars >= min_chars,
                })
        finally:
            pdf.close()

    return {
        "schema_version": common.SCHEMA_VERSION,
        "generated_at": common.now_iso(),
        "readonly": True,
        "database_mode": "sqlite mode=ro + PRAGMA query_only",
        "library": {
            "root_relative": common.relative_reference(library_root),
            "config_sha256": common.sha256_file(config_path),
            "database_sha256": common.sha256_file(db_path),
            "database_relative_path": db_path.name,
            "ocr_config": config.get("ocr", {}),
        },
        "totals": {
            "documents": len(documents),
            "pages": len([p for p in pages if "file_page" in p]),
            "units_with_correction": int(corrections or 0),
            "evidence_rows": int(evidence or 0),
            "text_layer_sufficient_pages": len([p for p in pages if p.get("text_layer_sufficient")]),
        },
        "documents": documents,
        "pages": pages,
    }


def prepare_dataset(library_root: Path | str, output_root: Path | str) -> Path:
    """只读清点并写出 ``inventory.json`` 与 ``samples.draft.json``。

    :returns: ``samples.draft.json`` 的路径（人工选页后就地改名为 ``samples.json``）
    """
    library_root = common.resolve(library_root)
    out_root = common.ensure_safe_output(output_root, library_root=library_root)
    out_root.mkdir(parents=True, exist_ok=True)
    inventory = build_inventory(library_root)

    inventory["library"]["root_relative"] = common.relative_reference(library_root)
    common.dump_json(out_root / INVENTORY_FILENAME, inventory)

    draft = {
        "schema_version": common.SCHEMA_VERSION,
        "generated_at": common.now_iso(),
        "selection": {
            "note": "本工具不自动挑页：由人看原图后填 samples（每份文献 2 页，共 12 页）。",
            "plan_sample_count": PLAN_SAMPLE_COUNT,
            "plan_positive_query_cases": PLAN_POSITIVE_QUERIES,
            "plan_negative_query_cases": PLAN_NEGATIVE_QUERIES,
            "required_fields": ["sample_id", "source_sha256", "source_filename", "doc_id",
                                "file_page", "printed_page", "printed_page_status", "tags"],
        },
        # 源路径只存在于本机配置；清单与报告一律用相对引用。
        "local_config": {"library_root": str(library_root), "output_root": str(out_root)},
        "source_manifest": {
            "inventory_file": INVENTORY_FILENAME,
            "inventory_sha256": common.sha256_file(out_root / INVENTORY_FILENAME),
            "library_root_relative": common.relative_reference(library_root),
            "config_sha256": inventory["library"]["config_sha256"],
            "database_sha256": inventory["library"]["database_sha256"],
        },
        "samples": [],
        "query_cases": [],
    }
    return common.dump_json(out_root / DRAFT_MANIFEST_FILENAME, draft)


# ------------------------------------------------------------ 派生单页


def _source_path(sample: dict[str, Any], library_root: Path | None) -> Path | None:
    """按清单里的来源线索定位库内原件（相对引用优先，其次按 doc_id 查库）。"""
    digest = str(sample.get("source_sha256") or "")
    relative = str(sample.get("source_relative_path") or "")
    if library_root is not None and relative:
        candidate = library_root / relative
        if candidate.exists():
            return candidate
    if library_root is not None and not relative:
        try:
            config_db = _library_config(library_root)[1]
        except ValidationFailure:
            config_db = None  # 该目录不是完整工作区：退回到 sources/ 扫描
        if config_db is not None and config_db.exists():
            conn = _open_readonly(config_db)
            try:
                row = conn.execute(
                    "SELECT stored_path FROM documents WHERE id = ? OR sha256 = ?",
                    (sample.get("doc_id") or "", digest)).fetchone()
            finally:
                conn.close()
            if row is not None:
                return library_root / str(row["stored_path"])
        if digest:
            for candidate in library_root.glob(f"sources/*/{digest}.*"):
                return candidate
            # 兜底：按内容哈希在库根附近找原件（只读，不猜路径）
            for candidate in sorted(library_root.glob("*")) + sorted(
                    library_root.glob("*/*")):
                if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
                    continue
                if common.sha256_file(candidate) == digest:
                    return candidate
    return None


def _pdf_page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def _write_single_page_pdf(source: Path, file_page: int, target: Path) -> None:
    """导出一页 PDF。候选看到的永远是第 1 页，原页码另存 ``file_page``。"""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(source))
    try:
        single = pdfium.PdfDocument.new()
        try:
            single.import_pages(pdf, pages=[int(file_page) - 1])
            single.save(str(target))
        finally:
            single.close()
    finally:
        pdf.close()


def _render_scales(page_pdf: Path, scales: tuple[float, ...], directory: Path,
                   out_root: Path) -> list[str]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(page_pdf))
    relative: list[str] = []
    try:
        page = pdf[0]
        try:
            for scale in scales:
                image = page.render(scale=float(scale)).to_pil().convert("RGB")
                try:
                    target = common.safe_target(out_root, directory / f"page_s{float(scale):g}.png")
                    image.save(target)
                    relative.append(target.relative_to(out_root).as_posix())
                finally:
                    image.close()
        finally:
            page.close()
    finally:
        pdf.close()
    return relative


def materialize_dataset(manifest: Path | str,
                        scales: tuple[float, ...] = default_scales()) -> Path:
    """按**已选定**的样本派生单页 PDF 与各倍率页图，并回写清单。"""
    manifest_path = common.resolve(manifest)
    data = common.load_json(manifest_path)
    out_root = manifest_path.parent
    library_root = data.get("local_config", {}).get("library_root") or ""
    library = common.resolve(library_root) if library_root else None
    common.ensure_safe_output(out_root, library_root=library,
                              protected=[("派生文件目录", out_root / DERIVED_DIRNAME)])

    samples = data.get("samples") or []
    if not samples:
        raise ValidationFailure(
            "清单里还没有样本：请先在 samples.json 中显式选定样本（每份文献 2 页，共 12 页），"
            "再运行 --materialize。"
        )
    problems = _sample_problems(samples, library)
    fatal = [item for item in problems if item["severity"] == "error"]
    if fatal:
        raise ValidationFailure("样本选择有硬错误：\n" + "\n".join(
            f"- {item['code']}: {item['message']}" for item in fatal))

    derived_root = common.safe_target(out_root, out_root / DERIVED_DIRNAME)
    for sample in samples:
        sample_id = common.validate_sample_id(sample["sample_id"])
        directory = common.safe_target(out_root, derived_root / sample_id)
        directory.mkdir(parents=True, exist_ok=True)
        source = _source_path(sample, library)
        assert source is not None  # _sample_problems 已拦截
        page_pdf = common.safe_target(out_root, directory / "page.pdf")
        _write_single_page_pdf(source, int(sample["file_page"]), page_pdf)
        images = _render_scales(page_pdf, tuple(scales), directory, out_root)

        sample["source_relative_path"] = source.relative_to(library).as_posix() if library \
            and common.path_is_within(source, library) else sample.get("source_relative_path", "")
        sample["page_pdf"] = page_pdf.relative_to(out_root).as_posix()
        sample["page_pdf_sha256"] = common.sha256_file(page_pdf)
        sample["image_paths"] = images
        sample["image_sha256"] = {relative: common.sha256_file(out_root / relative)
                                  for relative in images}
        sample["scales"] = [float(scale) for scale in scales]
        sample["derived"] = {
            "source_sha256": sample["source_sha256"],
            "file_page": int(sample["file_page"]),
            "candidate_pdf_page": 1,
            "note": "候选 PDF 永远只有第 1 页；原文件页码见 file_page。",
        }
    common.dump_json(manifest_path, data)
    return manifest_path


# ---------------------------------------------------------------- 校验


def _problem(code: str, message: str, *, severity: str = "error", sample_id: str = "") -> dict:
    return {"code": code, "message": message, "severity": severity, "sample_id": sample_id}


def _sample_problems(samples: list[dict[str, Any]],
                     library: Path | None) -> list[dict[str, Any]]:
    """样本自身的硬错误（重复、哈希不符、页码越界、原件找不到）。"""
    problems: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, int]] = set()
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        try:
            common.validate_sample_id(sample_id)
        except ValidationFailure as exc:
            problems.append(_problem("unsafe_sample_id", str(exc), sample_id=sample_id))
        if not sample_id:
            problems.append(_problem("sample_id_missing", "样本缺少 sample_id"))
        elif sample_id in seen_ids:
            problems.append(_problem("duplicate_sample_id", f"重复的 sample_id：{sample_id}",
                                     sample_id=sample_id))
        seen_ids.add(sample_id)

        file_page = int(sample.get("file_page") or 0)
        key = (str(sample.get("source_sha256") or ""), file_page)
        if key in seen_pages:
            problems.append(_problem("duplicate_sample_page",
                                     f"同一原件同一页被选了两次：{key}", sample_id=sample_id))
        seen_pages.add(key)

        source = _source_path(sample, library)
        if source is None:
            problems.append(_problem("source_not_found",
                                     "找不到库内原件（需要 local_config.library_root 与 source_relative_path）",
                                     sample_id=sample_id))
            continue
        actual = common.sha256_file(source)
        if actual != str(sample.get("source_sha256") or ""):
            problems.append(_problem(
                "source_hash_mismatch",
                f"原件哈希不符：期望 {sample.get('source_sha256')}，实际 {actual}",
                sample_id=sample_id))
        try:
            pages = _pdf_page_count(source)
        except Exception as exc:  # pragma: no cover - 损坏原件
            problems.append(_problem("source_unreadable", f"无法打开原件：{exc}",
                                     sample_id=sample_id))
            continue
        if not 1 <= file_page <= pages:
            problems.append(_problem("page_out_of_range",
                                     f"文件页 {file_page} 超出原件页数 1..{pages}",
                                     sample_id=sample_id))
    return problems


def _asset_problems(out_root: Path, sample: dict[str, Any]) -> list[dict[str, Any]]:
    sample_id = str(sample.get("sample_id") or "")
    relatives = [sample.get("page_pdf") or ""]
    relatives.extend(sample.get("image_paths") or [])
    problems: list[dict[str, Any]] = []
    derived = sample.get("derived") or {}
    for key in ("source_sha256", "file_page"):
        if derived and derived.get(key) != sample.get(key):
            problems.append(_problem("derived_identity_mismatch", f"派生页来源定位不符：{key}", sample_id=sample_id))
    for relative in [item for item in relatives if item]:
        target = out_root / relative
        if target.is_symlink():
            problems.append(_problem("derived_asset_is_symlink",
                                     f"派生文件是符号链接：{relative}", sample_id=sample_id))
            continue
        if not common.path_is_within(target, out_root / DERIVED_DIRNAME):
            problems.append(_problem("derived_asset_escapes_output_root",
                                     f"派生文件不在派生目录内：{relative}", sample_id=sample_id))
            continue
        if not target.exists():
            problems.append(_problem("derived_asset_missing", f"派生文件缺失：{relative}",
                                     sample_id=sample_id))
            continue
        recorded = (sample.get("page_pdf_sha256") if relative == sample.get("page_pdf")
                    else (sample.get("image_sha256") or {}).get(relative))
        if recorded and common.sha256_file(target) != recorded:
            problems.append(_problem("derived_asset_hash_mismatch",
                                     f"派生文件哈希不符：{relative}", sample_id=sample_id))
    return problems


def validate_dataset(manifest: Path | str) -> dict[str, Any]:
    """校验清单：原件哈希、页序、派生文件、真实库/输出边界、参考稿核稿状态。"""
    manifest_path = common.resolve(manifest)
    data = common.load_json(manifest_path)
    out_root = manifest_path.parent
    library_root = str(data.get("local_config", {}).get("library_root") or "")
    library = common.resolve(library_root) if library_root else None
    problems: list[dict[str, Any]] = []

    try:
        common.ensure_safe_output(out_root, library_root=library,
                                  protected=[("派生文件目录", out_root / DERIVED_DIRNAME)])
    except SafetyError as exc:
        problems.append(_problem("unsafe_output_root", str(exc)))

    samples = data.get("samples") or []
    problems.extend(_sample_problems(samples, library))

    levels: dict[str, int] = {}
    scorable = 0
    for sample in samples:
        problems.extend(_asset_problems(out_root, sample))
        reference_path = str(sample.get("reference_path") or "")
        reference: dict[str, Any] | None = None
        if not reference_path:
            problems.append(_problem("reference_missing", "样本没有参考稿",
                                     sample_id=str(sample.get("sample_id"))))
        elif not (out_root / reference_path).exists():
            problems.append(_problem("reference_file_missing", f"参考稿文件不存在：{reference_path}",
                                     sample_id=str(sample.get("sample_id"))))
        else:
            target = out_root / reference_path
            try:
                reference = common.load_json(target)
            except Exception as exc:
                problems.append(_problem("reference_unreadable", f"参考稿无法解析：{exc}",
                                         sample_id=str(sample.get("sample_id"))))
            if reference is not None:
                level = common.reference_review_level(reference)
                levels[level] = levels.get(level, 0) + 1
                ok, reason = common.reference_is_scorable(reference)
                scorable += int(ok)
                if level not in FULLPAGE_REVIEWED:
                    problems.append(_problem("reference_not_reviewed", reason,
                                             severity="warning",
                                             sample_id=str(sample.get("sample_id"))))
                elif reference.get("uncertain_spans"):
                    problems.append(_problem("uncertain_spans_disable_cer", reason,
                                             severity="warning",
                                             sample_id=str(sample.get("sample_id"))))
                if level != "draft" and not str(reference.get("reviewer") or "").strip():
                    problems.append(_problem("reference_reviewer_missing",
                                             "已核稿参考稿必须记录 reviewer",
                                             sample_id=str(sample.get("sample_id"))))
                recorded = sample.get("reference_sha256")
                if recorded and common.sha256_file(target) != recorded:
                    problems.append(_problem("reference_hash_mismatch",
                                             f"参考稿哈希不符：{reference_path}",
                                             sample_id=str(sample.get("sample_id"))))

    if len(samples) < PLAN_SAMPLE_COUNT:
        problems.append(_problem(
            "sample_count_below_plan",
            f"样本只有 {len(samples)} 页，计划为 {PLAN_SAMPLE_COUNT} 页；"
            "只能报告局部判断，不能声称质量通过"))
    if not common.path_is_within(out_root / DERIVED_DIRNAME, out_root):
        problems.append(_problem("derived_dir_outside_output", "派生目录不在输出目录内"))

    sample_ids = {str(item.get("sample_id")) for item in samples}
    positives = negatives = 0
    for case in data.get("query_cases") or []:
        query_id = str(case.get("query_id") or "")
        kind = str(case.get("kind") or "")
        if kind not in ("positive", "negative"):
            problems.append(_problem("query_case_kind_invalid",
                                     f"{query_id}: kind 必须是 positive 或 negative"))
            continue
        if not str(case.get("query") or "").strip():
            problems.append(_problem("query_case_empty", f"{query_id}: 检索词为空"))
            continue
        if str(case.get("sample_id")) not in sample_ids:
            problems.append(_problem("query_case_sample_unknown",
                                     f"{query_id}: 指向不存在的样本 {case.get('sample_id')}"))
            continue
        positives += int(kind == "positive")
        negatives += int(kind == "negative")
    if positives < PLAN_POSITIVE_QUERIES or negatives < PLAN_NEGATIVE_QUERIES:
        problems.append(_problem(
            "query_cases_below_plan",
            f"检索案例 {positives} 正 / {negatives} 负，"
            f"计划为 {PLAN_POSITIVE_QUERIES} / {PLAN_NEGATIVE_QUERIES}"))

    errors = [item for item in problems if item["severity"] == "error"]
    warnings = [item for item in problems if item["severity"] != "error"]
    return {
        "manifest": str(manifest_path),
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "sample_count": len(samples),
            "scorable_samples": scorable,
            "reference_review_levels": levels,
            "positive_query_cases": positives,
            "negative_query_cases": negatives,
            "samples_by_document": _count_by_document(samples),
            "library_root_relative": data.get("source_manifest", {}).get(
                "library_root_relative", ""),
        },
    }


def _count_by_document(samples: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sample in samples:
        key = str(sample.get("source_filename") or sample.get("doc_id") or "?")
        counts[key] = counts.get(key, 0) + 1
    return counts


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ocr_quality.prepare",
        description="县志 OCR 质量评测：只读清点、派生单页、清单校验（只读边界由工具强制）。")
    parser.add_argument("--library", type=Path, help="真实文献库目录（只读打开 SQLite）")
    parser.add_argument("--out", type=Path, help="评测输出目录（不得位于源目录或真实库内）")
    parser.add_argument("--materialize", type=Path, metavar="MANIFEST",
                        help="按清单中已选定的样本派生单页 PDF 与 2/3/4 倍率页图")
    parser.add_argument("--validate", type=Path, metavar="MANIFEST", help="校验清单与参考稿")
    parser.add_argument("--scales", type=float, nargs="+", default=list(default_scales()),
                        help="页图倍率（默认 2 3 4）")
    args = parser.parse_args(argv)

    try:
        if args.materialize is not None:
            path = materialize_dataset(args.materialize, tuple(args.scales))
            print(json.dumps({"materialized": str(path), "scales": list(args.scales)},
                             ensure_ascii=False))
            return EXIT_OK
        if args.validate is not None:
            result = validate_dataset(args.validate)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return EXIT_OK if result["ok"] else EXIT_PROBLEMS
        if args.library is not None and args.out is not None:
            draft = prepare_dataset(args.library, args.out)
            print(json.dumps({"inventory": str(Path(args.out) / INVENTORY_FILENAME),
                              "draft_manifest": str(draft),
                              "note": "未选页、未 OCR；请人工选定 12 页后改名为 "
                                      + MANIFEST_FILENAME},
                             ensure_ascii=False, indent=2))
            return EXIT_OK
    except (ValidationFailure, SafetyError) as exc:
        print(json.dumps({"error": {"kind": type(exc).__name__, "message": str(exc)}},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return EXIT_PROBLEMS
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
