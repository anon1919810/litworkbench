"""``python -m litworkbench.web`` 入口：本地网页服务。

只监听 127.0.0.1；默认打开浏览器，加 ``--no-browser`` 不打开。
日常使用不需要 Node，前端编译产物随源码目录或安装包提供。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from litworkbench.errors import LitError
from litworkbench.launcher import DEFAULT_HOST, DEFAULT_PORT, find_free_port, launch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m litworkbench.web",
        description="启动本地文献工作台网页（仅监听 127.0.0.1）",
    )
    parser.add_argument("--workspace", required=True, metavar="PATH",
                        help="工作区目录（不存在会自动创建）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"端口，默认 {DEFAULT_PORT}；被占用时自动顺延")
    parser.add_argument("--host", default=DEFAULT_HOST, help="只允许 127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别")
    return parser


def banner(workspace: Path, host: str, port: int) -> str:
    """启动提示（中文）。由 Python 输出，避免 .cmd 的编码问题。"""
    return "\n".join([
        "",
        "  个人图书馆 · 本地文献工作台",
        f"  工作区：{workspace}",
        f"  地址：http://{host}:{port}/",
        "  浏览器会自动打开；关闭本窗口或按 Ctrl+C 会停止后台识别任务。",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    from litworkbench.runtime import RuntimeSettings
    try:
        settings = RuntimeSettings.from_env()
        if settings.mode == 'cloud':
            from litworkbench.web.cloud import main as cloud_main
            cloud_main(argv)
            return 0
    except ValueError as exc:
        sys.stderr.write(f'配置错误：{exc}\n')
        return 2
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        sys.stderr.write("出于本地优先的考虑，服务只监听 127.0.0.1。\n")
        return 2
    workspace = Path(args.workspace).expanduser()
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
    from litworkbench.launcher import find_free_port

    port = args.port or find_free_port(args.host)
    sys.stdout.write(banner(workspace, args.host, port) + "\n")
    sys.stdout.flush()
    try:
        return launch(
            workspace,
            port=port,
            open_browser_flag=not args.no_browser,
            host=args.host,
            log_level=args.log_level,
        )
    except LitError as exc:
        sys.stderr.write(f"启动失败：{exc}\n")
        return 1
    except KeyboardInterrupt:  # pragma: no cover - 用户中断
        sys.stderr.write("服务已停止。\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
