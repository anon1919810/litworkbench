"""页图渲染子进程。

PDFium **不允许在多个线程里同时调用**，而网页服务是多线程的（请求并发），
OCR 又在另一个进程里跑。因此页图渲染统一走这个短命子进程：

* 网页请求把 ``{source, page_index, scale}`` 通过 stdin 交给子进程；
* 子进程渲染完把 PNG 字节写到 stdout，错误信息写 stderr；
* 进程内再用一把锁串行化，保证同一时刻只有一个 PDFium 调用在飞。

用法：``python -m litworkbench.render_process``（一般由网页服务自动调用）。
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from litworkbench.errors import LitError, ValidationError

#: 允许的缩放档位（契约固定四档，避免任意倍率把内存吃满）。
ALLOWED_SCALES = (1.0, 1.5, 2.0, 3.0)

_RENDER_LOCK = threading.Lock()
_TIMEOUT_SECONDS = 120


def validate_scale(scale: Any) -> float:
    try:
        value = float(scale)
    except (TypeError, ValueError):
        raise ValidationError(
            f"缩放倍率必须是数字，可选：{'、'.join(f'{item:g}' for item in ALLOWED_SCALES)}。"
        )
    for allowed in ALLOWED_SCALES:
        if abs(value - allowed) < 1e-9:
            return allowed
    raise ValidationError(
        f"只支持 {len(ALLOWED_SCALES)} 档缩放："
        f"{'、'.join(f'{item:g}' for item in ALLOWED_SCALES)}（收到 {value:g}）。"
    )


def render_png_bytes(source: Path | str, page_index: int, scale: float) -> bytes:
    """渲染一页为 PNG 字节（**在独立进程中执行**，见模块说明）。"""
    import pypdfium2 as pdfium

    scale = validate_scale(scale)
    index = int(page_index)
    if index < 0:
        raise ValidationError("页序必须是从 0 开始的整数。")
    with _RENDER_LOCK:
        pdf = pdfium.PdfDocument(str(Path(source)))
        try:
            if index >= len(pdf):
                raise LitError(f"页序超出范围：共 {len(pdf)} 页，请求第 {index + 1} 页。")
            page = pdf[index]
            try:
                image = page.render(scale=scale).to_pil().convert("RGB")
                try:
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    return buffer.getvalue()
                finally:
                    image.close()
            finally:
                page.close()
        finally:
            pdf.close()


def request_render(source: Path | str, page_index: int, scale: float, *,
                   timeout: float = _TIMEOUT_SECONDS) -> bytes:
    """父进程侧调用：起一个渲染子进程并取回 PNG。"""
    payload = json.dumps({
        "source": str(source),
        "page_index": int(page_index),
        "scale": float(scale),
    }).encode("utf-8")
    command = [sys.executable, "-m", "litworkbench.render_process"]
    try:
        completed = subprocess.run(
            command, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - 依赖外部渲染耗时
        raise LitError("页图渲染超时，请稍后重试或调低缩放倍率。") from exc
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise LitError(detail or "页图渲染失败。")
    return completed.stdout


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 子进程入口
    parser = argparse.ArgumentParser(
        prog="litworkbench-render",
        description="把 PDF 页渲染成 PNG（由网页服务在独立进程中调用）",
    )
    parser.add_argument("--info", action="store_true", help="只打印允许的缩放档位")
    args = parser.parse_args(argv)
    if args.info:
        sys.stdout.write(json.dumps({"scales": list(ALLOWED_SCALES)}) + "\n")
        return 0
    raw = sys.stdin.buffer.read()
    try:
        request = json.loads(raw.decode("utf-8"))
        data = render_png_bytes(request["source"], request["page_index"], request["scale"])
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())