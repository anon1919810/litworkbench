"""``lit`` / ``python -m litworkbench`` 命令行入口。

约定：

* 数据（JSON）走 stdout，进度与错误走 stderr；
* Windows 下强制 stdout/stderr 使用 UTF-8，保证中文路径与引文不乱码；
* 退出码：0 成功、1 业务错误、2 参数错误（argparse 默认）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from litworkbench import __version__
from litworkbench.errors import LitError

PROG = "lit"


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，这里显式改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover - 极少数流不支持
                pass


def emit(payload: Any) -> None:
    """把结构化数据写到 stdout（UTF-8；Path 等类型转成字符串）。"""
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    sys.stdout.flush()


def log(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()


def _workspace(args: argparse.Namespace, create: bool = False):
    from litworkbench.store import Workspace

    workspace = Workspace(Path(args.workspace))
    if create:
        return workspace
    return Workspace.open(workspace.root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="本地文献工作台：导入/检索/摘录/研究/导出（本地优先，默认不需要 API）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  lit --workspace ./库 init\n"
            "  lit --workspace ./库 import ./资料.pdf\n"
            "  lit --workspace ./库 search 白塔\n"
            "  lit --workspace ./库 read u_xxx_page_0001\n"
            "  lit --workspace ./库 research 青川白塔始建于何时 --profile heritage\n"
            "  lit --workspace ./库 export report task_xxx --format markdown --out 报告.md\n"
        ),
    )
    parser.add_argument("--workspace", required=True, metavar="PATH",
                        help="工作区目录（init 会创建，其他命令需已存在）")
    parser.add_argument("--version", action="version", version=f"litworkbench {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="命令", required=True)

    # ------------------------------------------------------------- 基础
    init_parser = subparsers.add_parser("init", help="初始化工作区（已存在时需 --force）")
    init_parser.add_argument("--force", action="store_true", help="允许覆盖已有工作区")

    import_parser = subparsers.add_parser("import", help="导入 PDF/DOCX 文件或目录")
    import_parser.add_argument("path", nargs="+", help="文件或目录")
    import_parser.add_argument("--ocr", choices=("auto", "off", "force"),
                               help="覆盖配置中的 OCR 模式")
    import_parser.add_argument("--no-recursive", action="store_true", help="目录导入不递归子目录")

    list_parser = subparsers.add_parser("list", help="列出文献或待处理单元")
    list_parser.add_argument("--unit", action="store_true", help="列出原文单元")
    list_parser.add_argument("--doc", help="只看某个文献（ID 或文件名）")
    list_parser.add_argument("--status", help="按状态过滤（completed/pending/failed）")
    list_parser.add_argument("--limit", type=int, default=50)

    search_parser = subparsers.add_parser("search", help="本地中文检索")
    search_parser.add_argument("query", help="检索词或短语（忽略文字间空白）")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--include-pending", action="store_true",
                               help="同时返回尚未识别的单元（可能没有文字）")

    read_parser = subparsers.add_parser("read", help="读取某个原文单元")
    read_parser.add_argument("unit_id")

    render_parser = subparsers.add_parser("render", help="把 PDF 页渲染成图片便于核验")
    render_parser.add_argument("unit_id")
    render_parser.add_argument("--scale", type=float, default=2.0)
    render_parser.add_argument("--out", help="输出图片路径（默认写入 artifacts/pages）")

    metadata_parser = subparsers.add_parser("metadata", help="查看/修改书目信息")
    metadata_parser.add_argument("doc")
    metadata_parser.add_argument("--set", nargs="*", default=[], metavar="字段=值",
                                 help="可修改：title/author/year/edition/volume")

    label_parser = subparsers.add_parser("label", help="维护印刷页码（不影响文件页序）")
    label_parser.add_argument("unit_id")
    label_parser.add_argument("printed_page")

    correct_parser = subparsers.add_parser("correct", help="人工校订识别文字（保留原文）")
    correct_parser.add_argument("unit_id")
    correct_parser.add_argument("--text", help="校订后的整段文字")
    correct_parser.add_argument("--text-file", help="从 UTF-8 文件读取校订文字")
    correct_parser.add_argument("--reason", required=True, help="校订原因（必填）")
    correct_parser.add_argument("--revert", action="store_true", help="撤销校订，回到原始识别结果")

    doctor_parser = subparsers.add_parser("doctor", help="检查环境（OCR/配置/数据库）")
    doctor_parser.set_defaults(_doctor=True)

    demo_parser = subparsers.add_parser("make-demo", help="生成合成演示资料（虚构数据）")
    demo_parser.add_argument("dest", help="输出目录")
    demo_parser.add_argument("--font", help="中文字体路径（.ttf/.ttc）")

    # ------------------------------------------------------------- 摘录
    excerpt_parser = subparsers.add_parser("excerpt", help="摘录与核验")
    excerpt_sub = excerpt_parser.add_subparsers(dest="excerpt_command", required=True)
    excerpt_save = excerpt_sub.add_parser("save", help="保存摘录（引文必须逐字来自原文）")
    excerpt_save.add_argument("unit_id")
    excerpt_save.add_argument("--quote", required=True)
    excerpt_save.add_argument("--category", default="")
    excerpt_save.add_argument("--note", default="")
    excerpt_save.add_argument("--task", help="关联的研究任务 ID")
    excerpt_save.add_argument("--version", choices=("original", "corrected"), help="取自哪个版本")
    excerpt_list = excerpt_sub.add_parser("list", help="列出摘录")
    excerpt_list.add_argument("--task")
    excerpt_list.add_argument("--doc")
    excerpt_list.add_argument("--unit")
    excerpt_list.add_argument("--status", help="pending/accepted/rejected/stale")
    excerpt_list.add_argument("--limit", type=int, default=50)
    excerpt_review = excerpt_sub.add_parser("review", help="核验摘录")
    excerpt_review.add_argument("evidence_id")
    excerpt_review.add_argument("--status", required=True,
                                choices=("accepted", "rejected", "stale", "pending"))
    excerpt_review.add_argument("--note", default="")

    # ------------------------------------------------------------- 研究
    research_parser = subparsers.add_parser("research", help="启动研究任务（需要配置模型）")
    research_parser.add_argument("question")
    research_parser.add_argument("--profile", help="内置研究方案名或自定义 TOML 文件路径（默认取配置）")
    research_parser.add_argument("--max-steps", type=int, help="本次运行的步骤上限")

    resume_parser = subparsers.add_parser("resume", help="恢复暂停/失败的任务")
    resume_parser.add_argument("task_id")
    resume_parser.add_argument("--max-steps", type=int, help="本次恢复的步骤上限")

    tasks_parser = subparsers.add_parser("tasks", help="查看任务列表或单个任务详情")
    tasks_parser.add_argument("task_id", nargs="?")
    tasks_parser.add_argument("--status")
    tasks_parser.add_argument("--limit", type=int, default=20)

    # ------------------------------------------------------------- 导出
    export_parser = subparsers.add_parser("export", help="导出报告或证据")
    export_sub = export_parser.add_subparsers(dest="export_command", required=True)
    export_report = export_sub.add_parser("report", help="导出研究报告")
    export_report.add_argument("task_id")
    export_report.add_argument("--format", default="markdown", choices=("markdown", "json", "docx"))
    export_report.add_argument("--out", help="输出文件（默认写入 artifacts/reports）")
    export_evidence = export_sub.add_parser("evidence", help="导出证据清单")
    export_evidence.add_argument("--format", default="csv", choices=("csv", "json"))
    export_evidence.add_argument("--task")
    export_evidence.add_argument("--doc")
    export_evidence.add_argument("--out")

    return parser


# --------------------------------------------------------------- 子命令


def cmd_init(args: argparse.Namespace) -> int:
    from litworkbench import ocr as ocr_mod

    workspace = _workspace(args, create=True)
    info = workspace.initialize(force=args.force)
    info["ocr"] = ocr_mod.availability()
    emit(info)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from litworkbench.ingest import import_path

    workspace = _workspace(args)
    results = []
    for path in args.path:
        results.append(import_path(workspace, path, ocr_mode=args.ocr,
                                   recursive=not args.no_recursive))
    emit({
        "workspace": str(workspace.root),
        "paths": results,
        "stats": workspace.stats(),
    })
    errors = [error for result in results for error in result["errors"]]
    for result in results:
        for document in result["documents"]:
            errors.extend(document.get("errors", []))
            if document["status"] in {"failed", "empty"}:
                errors.append({"error": f"文献未完成导入：{document['filename']}"})
    for error in errors:
        log(f"导入失败：{error.get('error', '未知原因')}")
    return 1 if errors else 0


def cmd_list(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    if args.unit:
        doc_id = workspace.find_document(args.doc)["id"] if args.doc else None
        units = workspace.list_units(doc_id=doc_id, status=args.status, limit=args.limit)
        emit({"units": units})
        return 0
    if args.doc:
        documents = [workspace.find_document(args.doc)]
    else:
        documents = workspace.list_documents(limit=-1 if args.status else args.limit)
    if args.status:
        documents = [doc for doc in documents if doc["status"] == args.status][:args.limit]
    emit({"documents": documents})
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from litworkbench.ingest import search

    workspace = _workspace(args)
    hits = search(workspace, args.query, limit=args.limit,
                  include_pending=args.include_pending)
    emit({"query": args.query, "count": len(hits), "hits": hits})
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    from litworkbench.ingest import read_unit

    workspace = _workspace(args)
    emit(read_unit(workspace, args.unit_id))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    from litworkbench.ingest import render_page

    workspace = _workspace(args)
    emit(render_page(workspace, args.unit_id, scale=args.scale, output=args.out))
    return 0


def cmd_metadata(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    document = workspace.find_document(args.doc)
    updates: dict[str, str] = {}
    for item in args.set:
        if "=" not in item:
            raise LitError(f"参数格式应为 字段=值，收到：{item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if key not in {"title", "author", "year", "edition", "volume"}:
            raise LitError(f"可修改的字段：title/author/year/edition/volume，收到：{key}")
        updates[key] = value.strip()
    if updates:
        document = workspace.update_document(document["id"], **updates)
    emit(document)
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    emit(workspace.set_printed_page(args.unit_id, args.printed_page))
    return 0


def cmd_correct(args: argparse.Namespace) -> int:
    from litworkbench.evidence import clear_correction, correct_unit

    workspace = _workspace(args)
    if args.revert:
        unit = clear_correction(workspace, args.unit_id, args.reason)
    else:
        text = args.text
        if args.text_file:
            text = Path(args.text_file).expanduser().read_text(encoding="utf-8")
        if text is None:
            raise LitError("请提供 --text 或 --text-file")
        unit = correct_unit(workspace, args.unit_id, text, args.reason)
    emit(unit)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from litworkbench import ocr as ocr_mod
    from litworkbench.config import provider_settings

    workspace = _workspace(args)
    provider = provider_settings(workspace.config)
    key_env = provider.get("api_key_env", "")
    key_present = bool(key_env and key_env in __import__("os").environ)
    emit({
        "workspace": str(workspace.root),
        "python": sys.version.split()[0],
        "sqlite_ok": workspace.db_path.exists(),
        "ocr": ocr_mod.availability(),
        "provider": {
            "base_url": provider.get("base_url"),
            "model": provider.get("model"),
            "api_key_env": key_env,
            "api_key_present": key_present,
            "timeout": provider.get("timeout"),
        },
        "ocr_extras_installed": ocr_mod.is_available(),
        "stats": workspace.stats(),
    })
    return 0


def cmd_make_demo(args: argparse.Namespace) -> int:
    from litworkbench.demo import find_cjk_font, make_fixtures

    font = Path(args.font) if args.font else find_cjk_font()
    result = make_fixtures(args.dest, font=font)
    emit({
        "note": "以下均为合成虚构数据，不是真实县志或丛书",
        "files": {key: str(value) for key, value in result.items()},
    })
    return 0


def cmd_excerpt(args: argparse.Namespace) -> int:
    from litworkbench.evidence import list_evidence, review_evidence, save_evidence

    workspace = _workspace(args)
    if args.excerpt_command == "save":
        emit(save_evidence(workspace, args.unit_id, args.quote, category=args.category,
                           note=args.note, task_id=args.task, version=args.version))
    elif args.excerpt_command == "list":
        doc_id = workspace.find_document(args.doc)["id"] if args.doc else None
        items = list_evidence(workspace, task_id=args.task, doc_id=doc_id, unit_id=args.unit,
                              review_status=args.status, limit=args.limit)
        emit({"count": len(items), "evidence": items})
    else:
        emit(review_evidence(workspace, args.evidence_id, args.status, note=args.note))
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    from litworkbench.agent import run_research

    workspace = _workspace(args)
    result = run_research(workspace, args.question, profile_name=args.profile,
                          max_steps=args.max_steps)
    emit(result)
    return 0 if result["status"] == "completed" else 1


def cmd_resume(args: argparse.Namespace) -> int:
    from litworkbench.agent import run_research

    workspace = _workspace(args)
    result = run_research(workspace, "", profile_name=None, resume_id=args.task_id,
                          max_steps=args.max_steps)
    emit(result)
    return 0 if result["status"] == "completed" else 1


def cmd_tasks(args: argparse.Namespace) -> int:
    from litworkbench.agent import show_task, list_tasks

    workspace = _workspace(args)
    if args.task_id:
        emit(show_task(workspace, args.task_id))
        return 0
    emit({"tasks": list_tasks(workspace, status=args.status, limit=args.limit)})
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from litworkbench.export import export_evidence, export_report

    workspace = _workspace(args)
    if args.export_command == "report":
        path = export_report(workspace, args.task_id, output=args.out, format=args.format)
    else:
        path = export_evidence(workspace, output=args.out, format=args.format,
                               task_id=args.task, doc=args.doc)
    emit({"output": str(path), "exists": path.exists(), "bytes": path.stat().st_size})
    return 0


COMMANDS = {
    "init": cmd_init,
    "import": cmd_import,
    "list": cmd_list,
    "search": cmd_search,
    "read": cmd_read,
    "render": cmd_render,
    "metadata": cmd_metadata,
    "label": cmd_label,
    "correct": cmd_correct,
    "doctor": cmd_doctor,
    "make-demo": cmd_make_demo,
    "excerpt": cmd_excerpt,
    "research": cmd_research,
    "resume": cmd_resume,
    "tasks": cmd_tasks,
    "export": cmd_export,
}


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]
    try:
        return handler(args)
    except LitError as exc:
        log(f"错误：{exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        log("已取消。")
        return 130
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
