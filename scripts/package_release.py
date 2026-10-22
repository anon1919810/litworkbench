"""把「解压即用」的 Windows 包和源码包打成一个 zip。

内容：源码（src/）+ 已构建前端（web-client/dist）+ 启动脚本 + pyproject/uv.lock
     + 文档与示例 + 首次安装脚本。刻意排除 .git/.venv/node_modules/__pycache__/测试缓存。
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INCLUDE_FILES = [
    "README.md", "LICENSE", "pyproject.toml", "uv.lock", "lit.cmd", "启动文献工作台.cmd", ".gitignore",
]
INCLUDE_DIRS = ["src", "docs", "examples", "scripts", "tests", "web-client/dist"]
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".venv", "node_modules", ".git"}

INSTALL_CMD = """@echo off
chcp 65001 >nul
setlocal
rem ============================================================================
rem  litworkbench 首次安装（Windows）
rem  需要一个 Python 3.11+ 和 uv：https://docs.astral.sh/uv/getting-started/installation/
rem  装完双击 启动文献工作台.cmd 即可打开网页版；命令行用 lit.cmd。
rem ============================================================================

set "UV=uv"
where uv >nul 2>nul || (
  echo [ERROR] 没有找到 uv。请先安装：https://docs.astral.sh/uv/getting-started/installation/
  echo         安装后重开这个窗口再双击本文件。
  pause
  exit /b 1
)

echo [1/2] 正在安装依赖（OCR / 网页 / Excel 三组，首次需要网络，几百 MB）……
%UV% sync --python 3.12 --extra ocr --extra web --extra xlsx
if errorlevel 1 (
  echo [ERROR] 依赖安装失败，请把上面的报错贴给维护者。
  pause
  exit /b 1
)

echo [2/2] 自检……
%UV% run --extra ocr --extra web --extra xlsx lit --help >nul
if errorlevel 1 (
  echo [ERROR] 命令入口不可用，请把上面的报错贴给维护者。
  pause
  exit /b 1
)

echo.
echo 装好了。双击「启动文献工作台.cmd」打开网页版；命令行用「lit.cmd」。
pause
"""


def _iter_files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
        else:
            print(f"warn: 缺少 {name}", file=sys.stderr)
    for name in INCLUDE_DIRS:
        base = ROOT / name
        if not base.is_dir():
            print(f"warn: 缺少目录 {name}", file=sys.stderr)
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if EXCLUDE_PARTS & set(path.relative_to(ROOT).parts):
                continue
            files.append(path)
    return files


def build(out_dir: Path, version: str) -> Path:
    dist_index = ROOT / "web-client" / "dist" / "index.html"
    if not dist_index.is_file():
        raise SystemExit("缺少 web-client/dist/index.html：先在 web-client 里跑 npm run build，或让 CI 构建。")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"litworkbench-{version}-win64.zip"
    files = _iter_files()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            zf.write(path, path.relative_to(ROOT).as_posix())
        zf.writestr("安装.cmd", INSTALL_CMD.replace("\n", "\r\n"))
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 litworkbench 的解压即用包")
    ap.add_argument("--out", default="dist-release", help="输出目录")
    ap.add_argument("--version", default="dev", help="版本号，写进 zip 文件名")
    args = ap.parse_args()
    target = build(Path(args.out), args.version)
    size = target.stat().st_size
    print(f"{target}  {size} bytes  {size / 1048576:.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())