"""受控子进程协议：RapidOCR 基线子进程与候选适配器共用同一套约定。

约定（两侧都必须遵守）：

* ``--input <单页 PDF> --output <结果 JSON>``；附加参数由调度方显式给出；
* 退出码：0 成功、2 用法错误、3 缺依赖、4 运行期（含版本不匹配）错误、5 其他错误；
* 缺依赖**不是成功**：适配器必须把原因写进 stdout 的 JSON 并以 3 退出；
* 父进程只用本工具自己启动的进程树做超时/RSS 处理，不碰别的进程。

内存采样依赖可选：有 ``psutil`` 时按进程树采样 RSS 峰值，没有就写
``available=False`` + 原因，绝不写 0。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from scripts.ocr_quality import common

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_RUNTIME = 4
EXIT_ERROR = 5

EXIT_KINDS = {
    EXIT_USAGE: "usage_error",
    EXIT_MISSING_DEPENDENCY: "missing_dependency",
    EXIT_RUNTIME: "runtime_error",
    EXIT_ERROR: "error",
}

MEMORY_SAMPLE_SECONDS = 0.25
STDOUT_TAIL_CHARS = 4000


def build_command(python_exe: Path | str, script: Path | str, *,
                  input_pdf: Path | str, output_json: Path | str,
                  extra: Sequence[str] = ()) -> list[str]:
    return [str(python_exe), str(script), "--input", str(input_pdf),
            "--output", str(output_json), *[str(item) for item in extra]]


def exceeds_rss(peak_bytes: int | None, limit_bytes: int | None) -> bool:
    """只有真正采样到峰值才能判定超限；没采样到一律返回 False。"""
    if peak_bytes is None or limit_bytes is None:
        return False
    return int(peak_bytes) > int(limit_bytes)


def unavailable_memory(reason: str, *, limit_bytes: int | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "peak_rss_bytes": None,
        "peak_private_bytes": None,
        "samples": 0,
        "reason": reason,
        "limit_bytes": limit_bytes,
        "limit_enforced": False,
        "shared_pages_may_double_count": False,
        "note": "非精确系统峰值：只采样本工具启动的进程树 RSS 采样点之和",
    }


def tree_rss_sum(pid: int, psutil_module: Any) -> tuple[int | None, int, int | None]:
    """一次采样里返回**存活进程树的 RSS 之和**与存活进程数。"""
    process = psutil_module.Process(pid)
    processes = [process] + list(process.children(recursive=True))
    total = 0
    private = 0
    has_private = False
    alive = 0
    for item in processes:
        try:
            info = item.memory_info()
            total += int(info.rss)
            committed = getattr(info, "private", None)
            if committed is not None:
                private += int(committed)
                has_private = True
            alive += 1
        except Exception:  # pragma: no cover - 进程在采样间隙退出
            continue
    return (total or None), alive, private if has_private else None


def sample_process_tree_memory(pid: int, limit_bytes: int | None = None, *,
                               psutil_module: Any = None,
                               force_missing: bool = False) -> dict[str, Any]:
    """采样进程树 RSS 峰值（每次采样取整棵存活进程树之和，再取历次最大值）。

    ``psutil`` 缺失时返回 unavailable + 原因。共享页会让 RSS 求和**重复计数**，
    因此峰值是保守上界，不当作精确系统占用。
    """
    if psutil_module is None and not force_missing:
        try:
            import psutil  # type: ignore

            psutil_module = psutil
        except Exception as exc:
            return unavailable_memory(f"psutil 不可用，无法采样内存：{exc}",
                                     limit_bytes=limit_bytes)
    if psutil_module is None:
        return unavailable_memory("psutil 不可用，无法采样内存", limit_bytes=limit_bytes)

    peak = 0
    samples = 0
    try:
        total, alive, private = tree_rss_sum(pid, psutil_module)
    except Exception as exc:
        return unavailable_memory(f"采样进程树失败：{type(exc).__name__}: {exc}",
                                  limit_bytes=limit_bytes)
    if total is not None:
        peak = total
        samples = alive
    return {
        "available": True,
        "peak_rss_bytes": peak,
        "peak_private_bytes": private,
        "samples": samples,
        "reason": None,
        "limit_bytes": limit_bytes,
        "limit_enforced": limit_bytes is not None,
        "shared_pages_may_double_count": True,
        "note": "非精确系统峰值：每次采样取存活进程树 RSS 之和，共享页可能重复计数",
    }


def terminate_tree(process: "subprocess.Popen[str]") -> None:
    """只终止**本工具启动**的进程树。"""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=60, check=False)
        else:  # pragma: no cover - 当前只在 Windows 运行
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:  # pragma: no cover - 兜底
        process.kill()
    try:
        process.wait(timeout=30)
    except Exception:  # pragma: no cover - 兜底
        pass


def _last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def run_worker(command: Sequence[str], *, timeout: float,
               memory_limit_bytes: int | None = None,
               log_dir: Path | str | None = None,
               name: str = "worker", init_timeout: float | None = None,
               page_timeout: float | None = None) -> dict[str, Any]:
    """启动一个受控子进程并等待结果；超时或超内存只终止它自己。"""
    log_directory = Path(log_dir) if log_dir is not None else Path(".").resolve()
    log_directory.mkdir(parents=True, exist_ok=True)
    stdout_path = common.safe_target(log_directory, log_directory / f"{name}.stdout.log")
    stderr_path = common.safe_target(log_directory, log_directory / f"{name}.stderr.log")
    started = time.monotonic()
    memory = unavailable_memory("子进程尚未启动，未采样", limit_bytes=memory_limit_bytes)

    with stdout_path.open("w", encoding="utf-8") as out, stderr_path.open("w", encoding="utf-8") as err:
        creation = {"start_new_session": True} if os.name != "nt" else {}
        process = subprocess.Popen(list(command), stdout=out, stderr=err, text=True,
                                   **creation)
        killed_reason: str | None = None
        timeout_phase: str | None = None
        phase, phase_key, phase_started = "init", None, started
        while True:
            code = process.poll()
            if code is not None:
                break
            observation = sample_process_tree_memory(process.pid, memory_limit_bytes)
            if observation.get("available"):
                previous_peak = memory.get("peak_rss_bytes") or 0
                previous_samples = memory.get("samples") or 0
                previous_private = memory.get("peak_private_bytes")
                memory = dict(observation)
                memory["peak_rss_bytes"] = max(previous_peak, observation.get("peak_rss_bytes") or 0)
                memory["samples"] = previous_samples + (observation.get("samples") or 0)
                private_values = [x for x in (previous_private, observation.get("peak_private_bytes"))
                                  if x is not None]
                memory["peak_private_bytes"] = max(private_values) if private_values else None
            elif not memory.get("available"):
                memory = observation
            if init_timeout is not None or page_timeout is not None:
                event = _last_json_line(stdout_path.read_text(encoding="utf-8", errors="replace"))
                if event and event.get("event") == "phase" and event.get("phase") in ("init", "page"):
                    key = (event["phase"], event.get("index"))
                    if key != phase_key:
                        phase, phase_key, phase_started = event["phase"], key, time.monotonic()
                budget = init_timeout if phase == "init" else page_timeout
                if budget is not None and time.monotonic() - phase_started > budget:
                    killed_reason, timeout_phase = "timeout", phase
                    terminate_tree(process)
                    break
            if code is None and exceeds_rss(memory.get("peak_rss_bytes"), memory_limit_bytes):
                killed_reason = "rss_limit"
                memory["limit_exceeded"] = True
                terminate_tree(process)
                break
            if exceeds_rss(memory.get("peak_private_bytes"), memory_limit_bytes):
                killed_reason = "private_limit"
                memory["limit_exceeded"] = True
                terminate_tree(process)
                break
            if time.monotonic() - started > float(timeout):
                killed_reason = "timeout"
                terminate_tree(process)
                break
            time.sleep(MEMORY_SAMPLE_SECONDS)
        returncode = process.poll()

    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    return {
        "command": list(command),
        "returncode": returncode,
        "exit_kind": EXIT_KINDS.get(returncode, "ok" if returncode == EXIT_OK else "error"),
        "timed_out": killed_reason == "timeout",
        "timeout_phase": timeout_phase,
        "killed_reason": killed_reason,
        "duration_seconds": round(time.monotonic() - started, 3),
        "memory": memory,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "stdout_tail": stdout_text[-STDOUT_TAIL_CHARS:],
        "stderr_tail": stderr_text[-STDOUT_TAIL_CHARS:],
        "stdout_json": _last_json_line(stdout_text),
    }


def load_worker_result(output_json: Path | str) -> dict[str, Any] | None:
    target = Path(output_json)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def error_from_exit(worker: dict[str, Any], *, fallback_message: str) -> dict[str, str]:
    """把子进程的非零退出翻译成统一错误结构（缺依赖不会被当成成功）。"""
    payload = worker.get("stdout_json") or {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        kind = str(error.get("kind") or worker.get("exit_kind") or "error")
        return common.record_error(kind, str(error["message"]))
    tail = (worker.get("stderr_tail") or worker.get("stdout_tail") or "").strip()
    return common.record_error(str(worker.get("exit_kind") or "error"),
                               tail.splitlines()[-1] if tail else fallback_message)
