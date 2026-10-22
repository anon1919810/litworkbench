"""通用受控子进程监督器（Task B0）。

本模块提供一个与数据库、配置、模型无关的**通用**原语 :func:`supervise_process`：
它只负责在父进程里监督一个**自己启动**的短命子进程——GO 握手、初始化/单页阶段
看门狗、进程树内存采样与终止、以及结构化的结果信封。

设计边界（与 B0 冻结接口一致）：

* ``command`` 必须由调用方内部构造，绝不接受来自网页请求的任意命令；
* 子进程读 stdin 一行，必须恰为 ``GO`` 才开始工作；EOF/非 ``GO`` 直接退出、不做 OCR；
  stdout 逐行 UTF-8 JSON：``{"phase": "page"}`` 表示初始化完成并开始单页时限，
  末行 ``{"result": {...}}`` 是成功载荷；
* 只有在 ``result`` 为对象**且**退出码为 0**且**进程树确认回收时才算成功；
* 资源监控依赖 ``psutil``：缺失、身份不可记录、或**根进程存活期间连续无法采样**时，
  都必须明确失败（``missing_monitor``），绝不静默放行、也不伪造 0 内存；只有
  进程已退出的正常竞态不算监控缺失；
* 只使用 ``pid`` + 创建时间做身份校验：绝不采样、枚举或终止被回收复用的 PID 的进程树；
  自有根进程用 Popen 句柄安全终止，不使用任意 PID 的 taskkill；
* 只终止自己启动且身份吻合的进程树，先子后父，并在每次退出（含根正常退出）时清理
  **观测到的**自有子孙；无法确认回收时返回失败（``cleanup_error``）且
  ``process.reaped=False``，绝不假装已回收；
* stdout/stderr 诊断**有界**，单行超长也会被截断，不会无上限占内存。

B1 会在同一模块内追加真正的单页入口 ``run_page``；本模块自身不导入评测脚本。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

__all__ = ["supervise_process"]

#: stdout/stderr 诊断尾部保留的最大字节数（有界，避免无上限占用内存）。
DIAGNOSTICS_TAIL_BYTES = 64 * 1024
#: 单个协议行的最大字节数；超过则截断并继续丢弃该行剩余部分。
MAX_LINE_BYTES = 1024 * 1024
#: 终止进程树后等待回收的超时。
REAP_TIMEOUT_SECONDS = 15.0
#: 根进程存活期间，允许的连续采样失败次数上限；超过即判定监控不可用。
MAX_SAMPLER_FAILURES = 3
#: 根进程存活期间，允许的连续身份核实失败次数上限；超过即判定监控不可用。
MAX_IDENTITY_FAILURES = 3


# --------------------------------------------------------------- 内部工具


class _StreamReader:
    """后台线程读取子进程管道，保留**有界**尾部并可选逐行回调。"""

    def __init__(
        self,
        stream,
        on_line: Callable[[bytes], None] | None = None,
        *,
        tail_limit: int = DIAGNOSTICS_TAIL_BYTES,
        max_line: int = MAX_LINE_BYTES,
    ) -> None:
        self._stream = stream
        self._on_line = on_line
        self._tail = bytearray()
        self._tail_limit = tail_limit
        self._max_line = max_line
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")

    def _append_tail(self, data: bytes) -> None:
        if not data:
            return
        self._tail.extend(data)
        overflow = len(self._tail) - self._tail_limit
        if overflow > 0:
            del self._tail[:overflow]

    def _run(self) -> None:
        stream = self._stream
        try:
            while True:
                line = stream.readline(self._max_line)
                if not line:
                    break
                self._append_tail(line)
                if self._on_line is None:
                    continue
                if line.endswith(b"\n"):
                    self._on_line(line.rstrip(b"\r\n"))
                    continue
                # 单行超长：丢弃剩余部分，只报告一个空的截断行。
                while line and not line.endswith(b"\n"):
                    line = stream.readline(self._max_line)
                    self._append_tail(line)
                self._on_line(b"")
        except Exception:
            return


class _ProtocolState:
    """父进程从 stdout 逐行解析出的协议状态（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase_seen = False
        self._phase_time: float | None = None
        self._result: Any = None
        self._result_time: float | None = None
        self._malformed = False

    def on_stdout_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        with self._lock:
            if not text.startswith("{"):
                self._malformed = True
                return
            try:
                payload = json.loads(text)
            except Exception:
                self._malformed = True
                return
            if not isinstance(payload, dict):
                self._malformed = True
                return
            if payload.get("phase") == "page":
                if not self._phase_seen:
                    self._phase_seen = True
                    self._phase_time = time.monotonic()
            elif "result" in payload:
                self._result = payload["result"]
                self._result_time = time.monotonic()

    def snapshot(self) -> tuple[bool, float | None]:
        with self._lock:
            return self._phase_seen, self._phase_time

    @property
    def result(self) -> Any:
        with self._lock:
            return self._result

    @property
    def result_time(self) -> float | None:
        with self._lock:
            return self._result_time

    @property
    def malformed(self) -> bool:
        with self._lock:
            return self._malformed


class _DescendantRegistry:
    """记录**已观测到的自有子孙**（pid + 创建时间），供退出时安全回收。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, float] = {}

    def observe(self, psutil_module: Any, root_pid: int, root_create_time: float | None) -> None:
        if psutil_module is None or root_create_time is None:
            return
        try:
            root = psutil_module.Process(root_pid)
            if float(root.create_time()) != float(root_create_time):
                return
            children = root.children(recursive=True)
        except Exception:
            return
        with self._lock:
            for child in children:
                try:
                    self._entries[int(child.pid)] = float(child.create_time())
                except Exception:
                    continue

    def snapshot(self) -> dict[int, float]:
        with self._lock:
            return dict(self._entries)


def _envelope(
    *,
    ok: bool,
    result: Any,
    error: dict[str, str] | None,
    init_seconds: float | None,
    page_seconds: float | None,
    total_seconds: float,
    memory: dict[str, Any],
    pid: int | None,
    create_time: float | None,
    reaped: bool,
    stdout_tail: str,
    stderr_tail: str,
    command: Sequence[str],
    returncode: int | None,
    killed_reason: str | None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "result": result,
        "error": error,
        "timing": {
            "init_seconds": init_seconds,
            "page_seconds": page_seconds,
            "total_seconds": round(total_seconds, 6),
        },
        "memory": memory,
        "process": {"pid": pid, "create_time": create_time, "reaped": reaped},
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "command": list(command),
        "returncode": returncode,
        "killed_reason": killed_reason,
    }


def _available_memory(*, limit_bytes: int | None) -> dict[str, Any]:
    return {
        "available": True,
        "peak_rss_bytes": None,
        "peak_private_bytes": None,
        "samples": 0,
        "reason": None,
        "limit_bytes": limit_bytes,
        "limit_enforced": limit_bytes is not None,
        "shared_pages_may_double_count": True,
    }


def _unavailable_memory(reason: str, *, limit_bytes: int | None) -> dict[str, Any]:
    return {
        "available": False,
        "peak_rss_bytes": None,
        "peak_private_bytes": None,
        "samples": 0,
        "reason": reason,
        "limit_bytes": limit_bytes,
        "limit_enforced": False,
        "shared_pages_may_double_count": False,
    }


def _load_psutil():
    """依赖边界：返回 ``psutil`` 模块，缺失时返回 ``None``。

    测试用 monkeypatch 注入 ``None`` 以验证「监控缺失时必须在启动前失败」。
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    return psutil


def _sample_tree_memory(pid: int, psutil_module: Any) -> dict[str, Any]:
    """一次采样存活进程树的 RSS/私有提交之和（保守上界）。

    返回 ``{"rss_bytes": int|None, "private_bytes": int|None, "processes": int}``。
    测试可在真实分配不可控时用脚本化采样替换本函数。
    """
    process = psutil_module.Process(pid)
    processes = [process] + list(process.children(recursive=True))
    rss_total = 0
    private_total = 0
    has_private = False
    counted = 0
    for item in processes:
        try:
            info = item.memory_info()
        except Exception:  # 采样间隙进程退出
            continue
        rss_total += int(getattr(info, "rss", 0) or 0)
        private = getattr(info, "private", None)
        if private is not None:
            private_total += int(private)
            has_private = True
        counted += 1
    return {
        "rss_bytes": rss_total if counted else None,
        "private_bytes": private_total if has_private else None,
        "processes": counted,
    }


def _update_memory(memory: dict[str, Any], sample: dict[str, Any]) -> None:
    rss = sample.get("rss_bytes")
    if rss is not None:
        rss = int(rss)
        peak = memory["peak_rss_bytes"]
        if peak is None or rss > peak:
            memory["peak_rss_bytes"] = rss
    private = sample.get("private_bytes")
    if private is not None:
        private = int(private)
        peak = memory["peak_private_bytes"]
        if peak is None or private > peak:
            memory["peak_private_bytes"] = private
    memory["samples"] = int(memory.get("samples") or 0) + 1


def _get_create_time(psutil_module: Any, pid: int) -> float | None:
    try:
        return float(psutil_module.Process(pid).create_time())
    except Exception:
        return None


def _is_absent_exception(psutil_module: Any, exc: BaseException) -> bool:
    """只有「确定不存在」的异常才证明进程已消失；其余（AccessDenied 等）都是未知。"""
    for name in ("NoSuchProcess", "ProcessLookupError", "ZombieProcess"):
        cls = getattr(psutil_module, name, None)
        if isinstance(cls, type) and isinstance(exc, cls):
            return True
    return isinstance(exc, ProcessLookupError)


def _probe_process(psutil_module: Any, pid: int, create_time: float | None) -> str:
    """巡检单个进程，返回 ``"alive"`` / ``"absent"`` / ``"unknown"``。

    * ``absent``：仅由 NoSuchProcess/ProcessLookupError、确认 not-running/zombie，
      或**成功读取到**且与登记值不符的 create_time 证明。
    * ``unknown``：任何无法完成的巡检（AccessDenied、身份读取异常等）都保守按未知处理，
      既不当作已消失、也不动手。
    """
    if psutil_module is None or create_time is None:
        return "unknown"
    try:
        process = psutil_module.Process(pid)
    except Exception as exc:
        return "absent" if _is_absent_exception(psutil_module, exc) else "unknown"
    try:
        actual = float(process.create_time())
    except Exception as exc:
        return "absent" if _is_absent_exception(psutil_module, exc) else "unknown"
    if actual != float(create_time):
        return "absent"  # 确认身份不符：PID 已被回收，不是我们的进程
    try:
        if not process.is_running():
            return "absent"
    except Exception as exc:
        return "absent" if _is_absent_exception(psutil_module, exc) else "unknown"
    try:
        zombie = getattr(psutil_module, "STATUS_ZOMBIE", None)
        if zombie is not None and process.status() == zombie:
            return "absent"
    except Exception as exc:
        return "absent" if _is_absent_exception(psutil_module, exc) else "unknown"
    return "alive"


def _identity_state(psutil_module: Any, pid: int, expected: float | None) -> str:
    """返回 ``"match"`` / ``"mismatch"`` / ``"absent"`` / ``"unknown"``。

    只有 ``match`` 才允许采样；``mismatch``/``absent`` 表示已不是我们的进程；
    ``unknown`` 表示核实失败，必须计入连续失败。
    """
    if expected is None:
        return "unknown"
    try:
        process = psutil_module.Process(pid)
    except Exception as exc:
        return "absent" if _is_absent_exception(psutil_module, exc) else "unknown"
    try:
        actual = float(process.create_time())
    except Exception as exc:
        return "absent" if _is_absent_exception(psutil_module, exc) else "unknown"
    return "match" if actual == float(expected) else "mismatch"


def _close_stdin(proc: "subprocess.Popen[bytes]") -> None:
    try:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
    except Exception:
        pass


def _terminate_tree(
    proc: "subprocess.Popen[bytes]",
    psutil_module: Any,
    *,
    expected_create_time: float | None = None,
    descendants: dict[int, float] | None = None,
) -> bool:
    """只终止**本监督器启动且身份吻合**的进程树：先经校验的子孙，再自有根句柄。

    返回**是否确认回收**（``reaped``）。未知巡检（如 AccessDenied）一律保守视为
    未确认且不触碰该 PID；绝不枚举/终止被回收 PID 的树；自有根进程使用 Popen 句柄
    安全终止，不按任意 PID 调用 taskkill。
    """
    reaped = True
    entries: dict[int, float] = {}
    for raw_pid, raw_time in (descendants or {}).items():
        if raw_time is None:
            reaped = False  # 身份未知 → 无法核实，保守未回收
            continue
        entries[int(raw_pid)] = float(raw_time)

    # 仅当根身份可确认存活时才补充枚举；无法核实时保守标未确认，绝不枚举未知子树。
    if psutil_module is not None and expected_create_time is not None:
        root_state = _probe_process(psutil_module, proc.pid, expected_create_time)
        if root_state == "alive":
            children: list[Any] = []
            try:
                children = psutil_module.Process(proc.pid).children(recursive=True)
            except Exception as exc:
                if not _is_absent_exception(psutil_module, exc):
                    reaped = False
            for child in children:
                try:
                    entries[int(child.pid)] = float(child.create_time())
                except Exception as exc:
                    if not _is_absent_exception(psutil_module, exc):
                        reaped = False
        elif root_state == "unknown":
            reaped = False
        # absent / mismatch：不是我们启动的存活进程，无需枚举

    to_confirm: list[tuple[int, float]] = []
    for pid, ctime in entries.items():
        state = _probe_process(psutil_module, pid, ctime)
        if state == "absent":
            continue
        if state == "unknown":
            # 无法核实：保守视为仍未确认且绝不动未知 PID。
            reaped = False
            continue
        try:
            psutil_module.Process(pid).kill()
        except Exception:
            pass
        to_confirm.append((pid, ctime))

    # 自有根句柄：句柄安全，不依赖 PID 复用防护。
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass

    deadline = time.monotonic() + REAP_TIMEOUT_SECONDS
    while to_confirm:
        still: list[tuple[int, float]] = []
        for pid, ctime in to_confirm:
            state = _probe_process(psutil_module, pid, ctime)
            if state == "alive":
                still.append((pid, ctime))
            elif state == "unknown":
                reaped = False
        if not still:
            break
        if time.monotonic() >= deadline:
            reaped = False
            break
        to_confirm = still
        time.sleep(0.05)

    try:
        proc.wait(timeout=REAP_TIMEOUT_SECONDS)
    except Exception:
        pass
    if proc.poll() is None:
        reaped = False
    return reaped


def _finalize(
    *,
    proc: "subprocess.Popen[bytes]",
    psutil_module: Any,
    out_reader: _StreamReader,
    err_reader: _StreamReader,
    state: _ProtocolState,
    started: float,
    memory: dict[str, Any],
    pid: int | None,
    create_time: float | None,
    command: Sequence[str],
    killed_kind: str | None,
    killed_message: str | None,
    descendants: dict[int, float] | None,
) -> dict[str, Any]:
    # 每次退出（含根正常退出）都清理观测到的自有子孙，并确认回收。
    reaped = _terminate_tree(
        proc,
        psutil_module,
        expected_create_time=create_time,
        descendants=descendants,
    )
    try:
        returncode = proc.wait(timeout=REAP_TIMEOUT_SECONDS)
    except Exception:
        returncode = proc.poll()
    out_reader.join(REAP_TIMEOUT_SECONDS)
    err_reader.join(REAP_TIMEOUT_SECONDS)

    phase_seen, phase_time = state.snapshot()
    result = state.result
    result_time = state.result_time

    if killed_kind is None:
        if returncode == 0 and isinstance(result, dict):
            if not reaped:
                killed_kind = "cleanup_error"
                killed_message = "无法确认自有子进程树已回收，拒绝当成安全通过"
        elif returncode != 0:
            tail = err_reader.text().strip().splitlines()
            detail = tail[-1] if tail else ""
            killed_kind = "child_error"
            killed_message = f"子进程以退出码 {returncode} 结束" + (f"：{detail}" if detail else "")
        else:
            killed_kind = "protocol_error"
            killed_message = "子进程正常退出但没有有效的 result 载荷"

    ok = killed_kind is None
    init_seconds = (phase_time - started) if phase_seen else None
    page_seconds = None
    if phase_seen and result_time is not None:
        page_seconds = result_time - phase_time
    return _envelope(
        ok=ok,
        result=result if ok else None,
        error=None if ok else {"kind": killed_kind, "message": killed_message or ""},
        init_seconds=init_seconds,
        page_seconds=page_seconds,
        total_seconds=time.monotonic() - started,
        memory=memory,
        pid=pid,
        create_time=create_time,
        reaped=reaped,
        stdout_tail=out_reader.text(),
        stderr_tail=err_reader.text(),
        command=command,
        returncode=returncode,
        killed_reason=killed_kind,
    )


# --------------------------------------------------------------- 公开接口

def run_page(workspace, unit, profile_id, *, checkpoint=None):
    """Validate local capability, supervise one PDF page and retain full raw audit."""
    import sys
    import uuid
    from pathlib import Path
    from litworkbench.ocr_profiles import profile_runtime, _local_capability, model_paths
    from litworkbench.errors import ValidationError
    from litworkbench.store import sha256_file

    runtime = profile_runtime(profile_id)
    capability = _local_capability()
    if not capability.get("available"):
        from litworkbench.ocr import OcrUnavailable
        raise OcrUnavailable(capability.get("reason") or "本机识别能力不可用")
    document = workspace.get_document(unit["doc_id"])
    source = workspace.path_of(document).resolve()
    if not source.is_relative_to(workspace.sources_dir.resolve()) or document["filetype"] != "pdf":
        raise ValidationError("识别来源必须是库内 PDF 原件")
    if sha256_file(source) != document["sha256"]:
        raise ValidationError("PDF 原件指纹不匹配")
    # Legacy background import/reprocess keeps the user's render scale.
    if unit.get("_render_scale") is not None:
        runtime["render_scale"] = float(unit["_render_scale"])
    request = {"source": str(source), "source_sha256": document["sha256"],
               "file_page": int(unit["file_page"]), "profile_id": profile_id,
               "runtime": runtime, "models": model_paths()}
    if request["file_page"] < 1:
        raise ValidationError("文件页码必须大于零")
    result = supervise_process(
        [sys.executable, "-B", "-m", "litworkbench.ocr_page_worker", "--request",
         json.dumps(request, ensure_ascii=True)],
        on_started=(lambda identity: checkpoint(identity)) if checkpoint else None,
        on_heartbeat=(lambda: checkpoint()) if checkpoint else None,
        init_timeout=runtime["init_timeout"], page_timeout=runtime["page_timeout"],
        max_memory_bytes=runtime["max_memory_bytes"])
    if result.get("ok"):
        result["result"]["timing"] = result["timing"]
        result["result"]["memory"] = result["memory"]
    folder = workspace.artifacts_dir / "ocr-raw"
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or not folder.resolve().is_relative_to(workspace.artifacts_dir.resolve()):
        raise ValidationError("识别审计目录不可为外部链接")
    target = folder / (uuid.uuid4().hex + ".json")
    temporary = target.with_suffix(".part")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    if result.get("ok"):
        result["result"]["raw_artifact"] = target.relative_to(workspace.root).as_posix()
    return result


def supervise_process(
    command: Sequence[str],
    *,
    on_started: Callable[[dict[str, Any]], None] | None = None,
    on_heartbeat: Callable[[], None] | None = None,
    init_timeout: float = 120.0,
    page_timeout: float = 60.0,
    max_memory_bytes: int | None = 2684354560,
    sample_interval: float = 0.1,
) -> dict[str, Any]:
    """监督一个自己启动的短命子进程，返回结构化结果信封。

    见模块文档字符串中关于协议、握手、看门狗与资源保护的全部约定。
    """
    command = list(command)
    started = time.monotonic()

    psutil_module = _load_psutil()
    if psutil_module is None:
        return _envelope(
            ok=False,
            result=None,
            error={
                "kind": "missing_monitor",
                "message": "psutil 不可用：无法监控子进程资源，拒绝在不受保护的情况下启动 OCR 子进程。",
            },
            init_seconds=None,
            page_seconds=None,
            total_seconds=time.monotonic() - started,
            memory=_unavailable_memory("psutil 不可用，无法采样内存", limit_bytes=max_memory_bytes),
            pid=None,
            create_time=None,
            reaped=True,
            stdout_tail="",
            stderr_tail="",
            command=command,
            returncode=None,
            killed_reason="missing_monitor",
        )

    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pid = proc.pid
    create_time = _get_create_time(psutil_module, pid)

    state = _ProtocolState()
    out_reader = _StreamReader(proc.stdout, state.on_stdout_line)
    err_reader = _StreamReader(proc.stderr, None)
    out_reader.start()
    err_reader.start()

    # 身份不可记录：绝不能发放 GO 后静默跳过每一次采样。
    if create_time is None:
        _close_stdin(proc)
        reaped = _terminate_tree(proc, psutil_module, expected_create_time=None)
        out_reader.join(REAP_TIMEOUT_SECONDS)
        err_reader.join(REAP_TIMEOUT_SECONDS)
        reason = "无法记录子进程身份（create_time）：拒绝在无法防护 PID 复用的情况下启动 OCR 子进程。"
        return _envelope(
            ok=False,
            result=None,
            error={"kind": "missing_monitor", "message": reason},
            init_seconds=None,
            page_seconds=None,
            total_seconds=time.monotonic() - started,
            memory=_unavailable_memory(reason, limit_bytes=max_memory_bytes),
            pid=pid,
            create_time=None,
            reaped=reaped,
            stdout_tail=out_reader.text(),
            stderr_tail=err_reader.text(),
            command=command,
            returncode=proc.poll(),
            killed_reason="missing_monitor",
        )

    # on_started 必须在发送 GO 之前成功；失败则不发放行、也不做 OCR。
    if on_started is not None:
        try:
            on_started({"pid": pid, "create_time": create_time})
        except Exception as exc:
            _close_stdin(proc)
            reaped = _terminate_tree(
                proc, psutil_module, expected_create_time=create_time
            )
            out_reader.join(REAP_TIMEOUT_SECONDS)
            err_reader.join(REAP_TIMEOUT_SECONDS)
            return _envelope(
                ok=False,
                result=None,
                error={
                    "kind": "callback_error",
                    "message": f"on_started 回调失败：{type(exc).__name__}: {exc}",
                },
                init_seconds=None,
                page_seconds=None,
                total_seconds=time.monotonic() - started,
                memory=_available_memory(limit_bytes=max_memory_bytes),
                pid=pid,
                create_time=create_time,
                reaped=reaped,
                stdout_tail=out_reader.text(),
                stderr_tail=err_reader.text(),
                command=command,
                returncode=proc.poll(),
                killed_reason="callback_error",
            )

    memory = _available_memory(limit_bytes=max_memory_bytes)
    registry = _DescendantRegistry()
    killed_kind: str | None = None
    killed_message: str | None = None

    go_error: Exception | None = None
    try:
        assert proc.stdin is not None
        proc.stdin.write(b"GO\n")
        proc.stdin.flush()
    except Exception as exc:  # 子进程可能在握手前就退出
        go_error = exc
    finally:
        _close_stdin(proc)

    if go_error is not None:
        killed_kind = "child_error"
        killed_message = f"发送 GO 握手失败：{type(go_error).__name__}: {go_error}"
    else:
        consecutive_sampler_failures = 0
        consecutive_identity_failures = 0
        while True:
            if proc.poll() is not None:
                break

            identity = _identity_state(psutil_module, pid, create_time)
            if identity == "match":
                consecutive_identity_failures = 0
                sample = None
                try:
                    sample = _sample_tree_memory(pid, psutil_module)
                except Exception:
                    sample = None
                usable = (
                    isinstance(sample, dict)
                    and sample.get("rss_bytes") is not None
                    and sample.get("private_bytes") is not None
                )
                if usable:
                    consecutive_sampler_failures = 0
                    _update_memory(memory, sample)
                    registry.observe(psutil_module, pid, create_time)
                    if max_memory_bytes is not None:
                        rss = memory["peak_rss_bytes"]
                        if rss is not None and rss > max_memory_bytes:
                            killed_kind = "memory_limit"
                            killed_message = f"进程树 RSS 峰值 {rss} 字节超过上限 {max_memory_bytes}"
                            break
                        private = memory["peak_private_bytes"]
                        if private is not None and private > max_memory_bytes:
                            killed_kind = "private_limit"
                            killed_message = (
                                f"进程树私有提交峰值 {private} 字节超过上限 {max_memory_bytes}"
                            )
                            break
                else:
                    # 采样失败：进程已退出属正常竞态；仍存活则视为监控不可用。
                    if proc.poll() is not None or _identity_state(
                        psutil_module, pid, create_time
                    ) != "match":
                        break
                    consecutive_sampler_failures += 1
                    if consecutive_sampler_failures >= MAX_SAMPLER_FAILURES:
                        killed_kind = "missing_monitor"
                        killed_message = (
                            "资源监控不可用：根进程仍存活却连续无法采样内存，"
                            "拒绝在不受保护的情况下继续。"
                        )
                        memory = _unavailable_memory(
                            killed_message, limit_bytes=max_memory_bytes
                        )
                        break
            elif identity == "unknown":
                # 核实身份本身失败：绝不静默跳过监控，连续失败即判定监控不可用。
                consecutive_identity_failures += 1
                if consecutive_identity_failures >= MAX_IDENTITY_FAILURES:
                    killed_kind = "missing_monitor"
                    killed_message = (
                        "资源监控不可用：根进程仍存活却连续无法核实其身份，"
                        "拒绝在不受保护的情况下继续。"
                    )
                    memory = _unavailable_memory(
                        killed_message, limit_bytes=max_memory_bytes
                    )
                    break
            else:
                # absent / mismatch：已不是我们启动的存活进程，跳过采样、绝不误伤他人。
                consecutive_identity_failures = 0

            now = time.monotonic()
            phase_seen, phase_time = state.snapshot()
            if not phase_seen:
                if init_timeout is not None and now - started > init_timeout:
                    killed_kind = "init_timeout"
                    killed_message = f"初始化阶段超过 {init_timeout} 秒"
                    break
            elif page_timeout is not None and phase_time is not None:
                if now - phase_time > page_timeout:
                    killed_kind = "page_timeout"
                    killed_message = f"单页阶段超过 {page_timeout} 秒"
                    break

            if on_heartbeat is not None:
                try:
                    on_heartbeat()
                except Exception as exc:
                    killed_kind = "callback_error"
                    killed_message = f"on_heartbeat 回调失败：{type(exc).__name__}: {exc}"
                    break

            time.sleep(sample_interval)

    return _finalize(
        proc=proc,
        psutil_module=psutil_module,
        out_reader=out_reader,
        err_reader=err_reader,
        state=state,
        started=started,
        memory=memory,
        pid=pid,
        create_time=create_time,
        command=command,
        killed_kind=killed_kind,
        killed_message=killed_message,
        descendants=registry.snapshot(),
    )
