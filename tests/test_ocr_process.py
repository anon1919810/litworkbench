"""受控 OCR 子进程监督器（Task B0）的 RED 测试。

本模块只测试 ``litworkbench.ocr_process.supervise_process`` 这一个**通用**监督原语：
它与数据库、配置、模型无关，只在父进程里管理一个**自己启动**的短命子进程，
负责 GO 握手、初始化/单页阶段看门狗、进程树内存采样与终止。

冻结的产品接口（B0）：

* ``supervise_process(command, *, on_started=None, on_heartbeat=None,
  init_timeout=120, page_timeout=60, max_memory_bytes=2684354560,
  sample_interval=0.1) -> dict``；``command`` 由内部构造，不来自网页请求。
* 子进程协议：读 stdin 一行，必须恰为 ``GO`` 才开始工作；EOF 直接退出、不做 OCR。
  stdout 逐行 JSON：``{"phase": "page"}`` 表示初始化完成并开始单页时限，
  末行 ``{"result": {...}}`` 是成功载荷。
* 返回信封：``{ok, result, error:{kind,message}, timing, memory, process,
  stdout_tail, stderr_tail}``；``diagnostics`` 必须**有界**，不能无上限堆进内存。
* 失败 kind：``init_timeout``、``page_timeout``、``memory_limit``、
  ``private_limit``、``missing_monitor``、``child_error``、``protocol_error``、
  ``callback_error``。

两个**窄注入接缝**（测试用 monkeypatch；产品不含测试专用开关）：

* ``ocr_process._load_psutil()`` —— 依赖边界；返回 ``None`` 表示监控不可用，
  必须在**启动任何子进程之前**以 ``missing_monitor`` 失败，不能有静默不受保护的
  路径、也不能伪造 0 内存。
* ``ocr_process._sample_tree_memory(pid, psutil_module)`` —— 内存采样器，返回
  ``{"rss_bytes": int|None, "private_bytes": int|None, "processes": int}``；
  真实分配不可控时由测试注入脚本化采样。

``litworkbench.ocr_process`` 尚未实现时本模块在导入阶段即失败——这是本轮刻意保留的
RED，不修改任何产品代码。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from litworkbench import ocr_process
from litworkbench.ocr_process import supervise_process

# ------------------------------------------------------------------ 子进程脚本

#: 子进程公共前导：UTF-8 管道 + GO 握手。EOF/非 ``GO`` 一律退出、不做任何工作。
_PREAMBLE = textwrap.dedent(
    """\
    import json
    import sys
    import time

    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    _line = sys.stdin.readline()
    if _line.strip() != "GO":
        sys.exit(9)
    """
)


def _command(body: str) -> list[str]:
    """用当前解释器拼出真正的短子进程命令。"""
    return [sys.executable, "-u", "-c", _PREAMBLE + "\n" + textwrap.dedent(body)]


def _emit_phase() -> str:
    return 'print(json.dumps({"phase": "page"}), flush=True)\n'


def _emit_result(payload: dict) -> str:
    # 协议规定末行必须是 {"result": {...}} 信封，而不是裸载荷。
    envelope = {"result": payload}
    return f"print(json.dumps({envelope!r}, ensure_ascii=False), flush=True)\n"


def _sleep(seconds: float) -> str:
    return f"time.sleep({seconds})\n"


def _write(path, text: str) -> str:
    return f"open({str(path)!r}, 'w', encoding='utf-8').write({text!r})\n"


# ------------------------------------------------------------------ 夹具/工具


@pytest.fixture
def unrelated_process():
    """一个与本监督器无关的进程，用来验证只终止自有进程树。"""
    proc = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=15)


def _wait_dead(pid: int, timeout: float = 15.0) -> bool:
    """等待 pid 真正退出（子进程已回收）。"""
    import psutil  # 产品依赖，GREEN 才安装

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = psutil.Process(pid)
            if proc.status() == psutil.STATUS_ZOMBIE or not proc.is_running():
                return True
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            return True
        time.sleep(0.05)
    return False


class _ScriptedSampler:
    """注入式内存采样器：按调用次序返回脚本化的采样值，并记录调用次数。"""

    def __init__(self, values: list[dict]):
        self._values = list(values)
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        index = min(len(self.calls) - 1, len(self._values) - 1)
        return dict(self._values[index])


class _FakeMemoryInfo:
    def __init__(self, rss: int, private: int | None = None):
        self.rss = rss
        self.private = private


class _FakeProcess:
    """伪造 psutil.Process：create_time 按脚本变化，模拟 PID 被回收。"""

    def __init__(self, create_times: list[float], memory: _FakeMemoryInfo):
        self._create_times = list(create_times)
        self._memory = memory

    def create_time(self) -> float:
        if len(self._create_times) > 1:
            return self._create_times.pop(0)
        return self._create_times[0]

    def children(self, recursive: bool = True):  # noqa: ARG002 - 与 psutil 对齐
        return []

    def memory_info(self) -> _FakeMemoryInfo:
        return self._memory

    def is_running(self) -> bool:
        return True


class _FakePsutil:
    def __init__(self, process: _FakeProcess):
        self._process = process

    def Process(self, pid: int) -> _FakeProcess:  # noqa: N802 - 与 psutil 对齐
        return self._process

    def pid_exists(self, pid: int) -> bool:  # noqa: N802 - 与 psutil 对齐
        return True


# ------------------------------------------------------------------ 握手与排序


def test_go_handshake_success_reports_result_timing_memory():
    started: list[dict] = []
    body = _emit_phase() + _sleep(0.05) + _emit_result(
        {"text": "中文结果：白塔", "value": 42, "lines": [1, 2]}
    )
    env = supervise_process(
        _command(body),
        on_started=started.append,
        init_timeout=5,
        page_timeout=5,
        max_memory_bytes=2 * 10**9,
        sample_interval=0.02,
    )
    assert env["ok"] is True, env
    assert env["error"] is None
    assert env["result"] == {"text": "中文结果：白塔", "value": 42, "lines": [1, 2]}
    assert isinstance(env["timing"]["init_seconds"], float)
    assert env["timing"]["init_seconds"] >= 0
    assert isinstance(env["timing"]["page_seconds"], float)
    assert env["timing"]["page_seconds"] >= 0
    assert env["memory"]["available"] is True
    assert len(started) == 1
    assert isinstance(started[0]["pid"], int) and started[0]["pid"] > 0
    assert "create_time" in started[0]
    assert env["process"]["pid"] == started[0]["pid"]
    assert env["process"]["create_time"] == started[0]["create_time"]
    assert isinstance(env["stdout_tail"], str)
    assert isinstance(env["stderr_tail"], str)


def test_on_started_runs_before_go_is_sent(tmp_path):
    sentinel = tmp_path / "child-ran.txt"
    seen: dict = {}

    def on_started(info):
        seen.update(info)
        seen["sentinel_present_in_callback"] = sentinel.exists()

    body = _write(sentinel, "ran") + _emit_phase() + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body),
        on_started=on_started,
        init_timeout=5,
        page_timeout=5,
        sample_interval=0.02,
    )
    assert env["ok"] is True, env
    assert seen["sentinel_present_in_callback"] is False
    assert sentinel.read_text(encoding="utf-8") == "ran"


def test_on_started_failure_never_sends_go_and_reaps_child(tmp_path):
    sentinel = tmp_path / "child-ran.txt"

    def on_started(info):  # noqa: ARG001 - 回调失败本身即用例
        raise RuntimeError("start callback boom")

    body = _write(sentinel, "ran") + _emit_phase() + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body),
        on_started=on_started,
        init_timeout=5,
        page_timeout=5,
        sample_interval=0.02,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "callback_error"
    assert not sentinel.exists()
    assert _wait_dead(env["process"]["pid"])


def test_child_exits_without_ocr_on_stdin_eof(tmp_path):
    """子进程契约：父进程没发 GO（EOF）时不得做任何 OCR 工作。"""
    marker = tmp_path / "child-ran.txt"
    body = _write(marker, "ran") + _emit_phase() + _emit_result({"text": "ok"})
    proc = subprocess.run(
        [sys.executable, "-u", "-c", _PREAMBLE + "\n" + textwrap.dedent(body)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        text=True,
    )
    assert proc.returncode != 0
    assert not marker.exists()


# ------------------------------------------------------------------ 阶段看门狗


def test_init_timeout_is_reported_independently():
    env = supervise_process(
        _command(_sleep(30)),
        init_timeout=0.3,
        page_timeout=30,
        sample_interval=0.05,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "init_timeout"
    assert env["timing"]["page_seconds"] is None
    assert _wait_dead(env["process"]["pid"])


def test_page_timeout_is_reported_independently():
    env = supervise_process(
        _command(_emit_phase() + _sleep(30)),
        init_timeout=30,
        page_timeout=0.3,
        sample_interval=0.05,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "page_timeout"
    assert env["timing"]["init_seconds"] is not None
    assert _wait_dead(env["process"]["pid"])


# ------------------------------------------------------------------ 协议异常


def test_nonzero_exit_is_child_error_with_stderr_diagnostic():
    body = 'sys.stderr.write("engine exploded\\n")\nsys.exit(2)\n'
    env = supervise_process(
        _command(body), init_timeout=5, page_timeout=5, sample_interval=0.02
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "child_error"
    assert "engine exploded" in env["stderr_tail"] + env["error"]["message"]


def test_exit_zero_without_result_is_protocol_error():
    env = supervise_process(
        _command(_emit_phase()), init_timeout=5, page_timeout=5, sample_interval=0.02
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "protocol_error"


def test_malformed_stdout_is_protocol_error():
    body = 'print("{not json", flush=True)\n'
    env = supervise_process(
        _command(body), init_timeout=5, page_timeout=5, sample_interval=0.02
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "protocol_error"


def test_noisy_stderr_is_bounded_and_does_not_break_success():
    body = (
        _emit_phase()
        + 'marker = "noise-marker-9271"\n'
        + "for _ in range(2000):\n"
        + '    sys.stderr.write(marker + "x" * 200 + "\\n")\n'
        + "sys.stderr.flush()\n"
        + _emit_result({"text": "ok"})
    )
    env = supervise_process(
        _command(body), init_timeout=15, page_timeout=15, sample_interval=0.05
    )
    assert env["ok"] is True, env
    assert env["result"] == {"text": "ok"}
    assert len(env["stderr_tail"]) <= 64 * 1024
    assert "noise-marker-9271" in env["stderr_tail"]


# ------------------------------------------------------------------ 回调与进程树


def test_heartbeat_exception_kills_only_own_tree(unrelated_process):
    def on_heartbeat(*args, **kwargs):
        raise RuntimeError("heartbeat boom")

    env = supervise_process(
        _command(_emit_phase() + _sleep(30)),
        on_heartbeat=on_heartbeat,
        init_timeout=30,
        page_timeout=30,
        sample_interval=0.05,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "callback_error"
    assert _wait_dead(env["process"]["pid"])
    assert unrelated_process.poll() is None


def test_child_descendants_are_cleaned_up(unrelated_process, tmp_path):
    grandchild_pid = tmp_path / "grandchild.pid"
    body = (
        _emit_phase()
        + "import subprocess\n"
        + "gp = subprocess.Popen([sys.executable, '-u', '-c', 'import time; time.sleep(60)'])\n"
        + f"open({str(grandchild_pid)!r}, 'w', encoding='utf-8').write(str(gp.pid))\n"
        + _sleep(60)
    )
    env = supervise_process(
        _command(body), init_timeout=15, page_timeout=2.0, sample_interval=0.05
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "page_timeout"
    assert _wait_dead(env["process"]["pid"])
    gpid = int(grandchild_pid.read_text(encoding="utf-8").strip())
    assert _wait_dead(gpid)
    assert unrelated_process.poll() is None


# ------------------------------------------------------------------ 内存与监控


def test_injected_sampler_reports_temporal_rss_maximum_and_private(monkeypatch):
    sampler = _ScriptedSampler(
        [
            {"rss_bytes": 100_000, "private_bytes": 50_000, "processes": 1},
            {"rss_bytes": 300_000, "private_bytes": 90_000, "processes": 2},
            {"rss_bytes": 200_000, "private_bytes": 60_000, "processes": 1},
        ]
    )
    monkeypatch.setattr(ocr_process, "_sample_tree_memory", sampler)
    body = _emit_phase() + _sleep(0.4) + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body),
        init_timeout=15,
        page_timeout=15,
        max_memory_bytes=10**9,
        sample_interval=0.03,
    )
    assert env["ok"] is True, env
    assert env["memory"]["available"] is True
    assert env["memory"]["peak_rss_bytes"] == 300_000
    assert env["memory"]["peak_private_bytes"] == 90_000
    assert env["memory"]["samples"] >= 3


def test_memory_limit_enforced_via_injected_sampler(monkeypatch):
    sampler = _ScriptedSampler(
        [{"rss_bytes": 500_000_000, "private_bytes": 1, "processes": 1}]
    )
    monkeypatch.setattr(ocr_process, "_sample_tree_memory", sampler)
    env = supervise_process(
        _command(_sleep(30)),
        init_timeout=30,
        page_timeout=30,
        max_memory_bytes=10**8,
        sample_interval=0.03,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "memory_limit"
    assert _wait_dead(env["process"]["pid"])


def test_private_limit_enforced_via_injected_sampler(monkeypatch):
    sampler = _ScriptedSampler(
        [{"rss_bytes": 1_000, "private_bytes": 500_000_000, "processes": 1}]
    )
    monkeypatch.setattr(ocr_process, "_sample_tree_memory", sampler)
    env = supervise_process(
        _command(_sleep(30)),
        init_timeout=30,
        page_timeout=30,
        max_memory_bytes=10**8,
        sample_interval=0.03,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "private_limit"
    assert _wait_dead(env["process"]["pid"])


def test_missing_monitor_fails_before_spawn(monkeypatch, tmp_path):
    marker = tmp_path / "child-ran.txt"
    monkeypatch.setattr(ocr_process, "_load_psutil", lambda: None)
    body = _write(marker, "ran") + _emit_phase() + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body), init_timeout=5, page_timeout=5, sample_interval=0.02
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "missing_monitor"
    assert env["memory"]["available"] is False
    assert env["memory"]["peak_rss_bytes"] is None
    assert not marker.exists()
    assert (env.get("process") or {}).get("pid") in (None, 0)


def test_pid_identity_guard_never_attributes_recycled_pid(monkeypatch):
    recycled = 10**12
    fake_process = _FakeProcess(
        create_times=[100.0, 200.0],
        memory=_FakeMemoryInfo(rss=recycled, private=recycled),
    )
    monkeypatch.setattr(ocr_process, "_load_psutil", lambda: _FakePsutil(fake_process))
    body = _emit_phase() + _sleep(0.5) + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body),
        init_timeout=15,
        page_timeout=15,
        max_memory_bytes=10**6,
        sample_interval=0.02,
    )
    peak = (env.get("memory") or {}).get("peak_rss_bytes")
    assert peak is None or peak < recycled
    error = env.get("error") or {}
    assert error.get("kind") != "memory_limit"


# ------------------------------------------------------------------ R1 回归：真实保护缺陷


def _psutil_without_create_time():
    """伪 psutil：无法返回 create_time（身份不可记录）。"""

    class _Process:
        def create_time(self):
            raise OSError("no create_time")

        def children(self, recursive=True):  # noqa: ARG002 - 与 psutil 对齐
            return []

        def memory_info(self):
            raise OSError("no memory")

    class _Module:
        def Process(self, pid):  # noqa: N802 - 与 psutil 对齐
            return _Process()

        def pid_exists(self, pid):  # noqa: N802 - 与 psutil 对齐
            return True

    return _Module()


def test_unrecordable_create_time_fails_missing_monitor_before_go(monkeypatch, tmp_path):
    """create_time 拿不到时必须**在 GO 之前**以 missing_monitor 失败，绝不静默跳过采样。"""
    marker = tmp_path / "child-ran.txt"
    monkeypatch.setattr(ocr_process, "_load_psutil", _psutil_without_create_time)
    body = _write(marker, "ran") + _emit_phase() + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body), init_timeout=5, page_timeout=5, sample_interval=0.02
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "missing_monitor"
    assert env["memory"]["available"] is False
    assert not marker.exists()
    assert env["process"]["reaped"] is True


def test_repeated_sampler_exception_while_root_alive_fails_missing_monitor(monkeypatch):
    """根仍存活却反复采样异常：不得以 available=True+limit_enforced=True 成功。"""

    def boom(*args, **kwargs):
        raise RuntimeError("sampler boom")

    monkeypatch.setattr(ocr_process, "_sample_tree_memory", boom)
    env = supervise_process(
        _command(_emit_phase() + _sleep(30)),
        init_timeout=0.5,
        page_timeout=0.5,
        sample_interval=0.02,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "missing_monitor"
    assert env["memory"]["available"] is False
    assert env["memory"]["limit_enforced"] is False
    assert env["process"]["reaped"] is True
    assert _wait_dead(env["process"]["pid"])


def test_sampler_without_rss_while_root_alive_fails_missing_monitor(monkeypatch):
    """根仍存活却拿不到 RSS/private：不得静默继续直到成功。"""

    def no_rss(*args, **kwargs):
        return {"rss_bytes": None, "private_bytes": None, "processes": 0}

    monkeypatch.setattr(ocr_process, "_sample_tree_memory", no_rss)
    env = supervise_process(
        _command(_emit_phase() + _sleep(30)),
        init_timeout=0.5,
        page_timeout=0.5,
        sample_interval=0.02,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "missing_monitor"
    assert env["memory"]["available"] is False
    assert env["process"]["reaped"] is True
    assert _wait_dead(env["process"]["pid"])


def test_sampler_failure_during_process_exit_race_is_not_missing_monitor(monkeypatch):
    """采样失败若恰好发生在进程退出的竞态里，不应判为 missing_monitor。"""

    def boom(*args, **kwargs):
        raise RuntimeError("sampler boom")

    monkeypatch.setattr(ocr_process, "_sample_tree_memory", boom)
    body = _emit_phase() + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body), init_timeout=5, page_timeout=5, sample_interval=0.5
    )
    error = env.get("error")
    assert error is None or error.get("kind") != "missing_monitor"


def test_terminate_tree_ignores_recycled_root_and_descendants():
    """根/子孙身份与登记值不符（PID 已回收）时，绝不能枚举或终止那棵树。"""
    root = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    try:
        killed: list[int] = []

        class _Child:
            pid = 999999

            def create_time(self):
                return 777.0  # 已回收，不等于缓存值 555.0

            def kill(self):
                killed.append(self.pid)

            def is_running(self):
                return True

        class _RootProcess:
            def create_time(self):
                return 999.0  # 不等于登记的 100.0 → 视为被回收

            def children(self, recursive=True):  # noqa: ARG002 - 与 psutil 对齐
                return [_Child()]

        class _Module:
            def Process(self, pid):  # noqa: N802 - 与 psutil 对齐
                return _RootProcess()

            def pid_exists(self, pid):  # noqa: N802 - 与 psutil 对齐
                return True

        reaped = ocr_process._terminate_tree(
            root, _Module(), expected_create_time=100.0, descendants={999999: 555.0}
        )
        assert killed == []
        assert reaped is True
        assert _wait_dead(root.pid)
    finally:
        if root.poll() is None:
            root.kill()
        root.wait(timeout=15)


def test_terminate_tree_reports_unreaped_when_descendant_survives(monkeypatch):
    """采样器无法确认子孙已回收时必须报 reaped=False，不能假装回收完成。"""
    monkeypatch.setattr(ocr_process, "REAP_TIMEOUT_SECONDS", 0.2)
    root = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    try:

        class _Child:
            pid = 424242

            def create_time(self):
                return 7.0

            def kill(self):
                pass  # 杀不死

            def is_running(self):
                return True

            def status(self):
                return "running"

        class _RootProcess:
            def create_time(self):
                return 7.0

            def children(self, recursive=True):  # noqa: ARG002 - 与 psutil 对齐
                return [_Child()]

        class _Module:
            def Process(self, pid):  # noqa: N802 - 与 psutil 对齐
                return _Child() if pid == 424242 else _RootProcess()

        reaped = ocr_process._terminate_tree(
            root, _Module(), expected_create_time=7.0, descendants={424242: 7.0}
        )
        assert reaped is False
    finally:
        if root.poll() is None:
            root.kill()
        root.wait(timeout=15)


def test_unreaped_cleanup_downgrades_success_to_gate_unsafe_failure(monkeypatch):
    """清理无法确认时，即使协议成功也必须 downgrade 为失败并标 reaped=False。"""
    monkeypatch.setattr(ocr_process, "_terminate_tree", lambda *a, **k: False)
    body = _emit_phase() + _emit_result({"text": "ok"})
    env = supervise_process(
        _command(body), init_timeout=5, page_timeout=5, sample_interval=0.02
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "cleanup_error"
    assert env["process"]["reaped"] is False


def test_normal_exit_reaps_leftover_descendants(tmp_path):
    """根正常退出但留下存活子孙时，仍须终止并确认回收（不假装干净）。"""
    grandchild_pid = tmp_path / "grandchild.pid"
    body = (
        _emit_phase()
        + "import subprocess\n"
        + "gp = subprocess.Popen([sys.executable, '-u', '-c', 'import time; time.sleep(60)'])\n"
        + f"open({str(grandchild_pid)!r}, 'w', encoding='utf-8').write(str(gp.pid))\n"
        + _sleep(0.5)
        + _emit_result({"text": "ok"})
    )
    env = supervise_process(
        _command(body), init_timeout=15, page_timeout=15, sample_interval=0.05
    )
    assert env["ok"] is True, env
    assert env["process"]["reaped"] is True
    gpid = int(grandchild_pid.read_text(encoding="utf-8").strip())
    assert _wait_dead(gpid)


# ------------------------------------------------------------------ R2 回归：未知巡检≠已消失


class _AccessDeniedError(Exception):
    """模拟 psutil.AccessDenied：能确定进程存在但无法巡检。"""


class _GoneError(Exception):
    """模拟 psutil.NoSuchProcess：确定进程不存在。"""


def test_terminate_tree_access_denied_descendant_is_unconfirmed_and_not_killed():
    """缓存子孙巡检被拒（未知）：绝不动该 PID，且必须报 reaped=False。"""
    root = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    try:
        killed: list[int] = []

        class _RootProcess:
            def create_time(self):
                return 5.0

            def is_running(self):
                return True

            def status(self):
                return "running"

            def children(self, recursive=True):  # noqa: ARG002 - 与 psutil 对齐
                return []

        class _Module:
            def Process(self, pid):  # noqa: N802 - 与 psutil 对齐
                if pid == 314159:
                    raise _AccessDeniedError("access denied")
                return _RootProcess()

        reaped = ocr_process._terminate_tree(
            root, _Module(), expected_create_time=5.0, descendants={314159: 6.0}
        )
        assert killed == []  # 未知 PID 绝不动
        assert reaped is False  # 无法核实 → 保守未回收
        assert _wait_dead(root.pid)  # 自有根句柄仍被安全终止
    finally:
        if root.poll() is None:
            root.kill()
        root.wait(timeout=15)


def test_terminate_tree_confirmed_absent_descendant_counts_as_reaped():
    """只有 NoSuchProcess（确定不存在）才算已消失，可确认回收。"""
    root = subprocess.Popen([sys.executable, "-u", "-c", "import time; time.sleep(60)"])
    try:

        class _RootProcess:
            def create_time(self):
                return 5.0

            def is_running(self):
                return True

            def status(self):
                return "running"

            def children(self, recursive=True):  # noqa: ARG002 - 与 psutil 对齐
                return []

        class _Module:
            NoSuchProcess = _GoneError  # noqa: N815 - 与 psutil 名称对齐

            def Process(self, pid):  # noqa: N802 - 与 psutil 对齐
                if pid == 271828:
                    raise _GoneError("gone")
                return _RootProcess()

        reaped = ocr_process._terminate_tree(
            root, _Module(), expected_create_time=5.0, descendants={271828: 9.0}
        )
        assert reaped is True
        assert _wait_dead(root.pid)
    finally:
        if root.poll() is None:
            root.kill()
        root.wait(timeout=15)


class _IdentityProbeFailureProcess:
    """首次能给出 create_time（登记身份），其后巡检一律被拒（未知）。"""

    def __init__(self):
        self._calls = 0

    def create_time(self):
        self._calls += 1
        if self._calls == 1:
            return 100.0
        raise _AccessDeniedError("identity probe denied")

    def children(self, recursive=True):  # noqa: ARG002 - 与 psutil 对齐
        return []

    def memory_info(self):
        return _FakeMemoryInfo(rss=123, private=456)

    def is_running(self):
        return True

    def status(self):
        return "running"


class _SingleProcessPsutil:
    def __init__(self, process: _IdentityProbeFailureProcess):
        self._process = process

    def Process(self, pid):  # noqa: N802 - 与 psutil 对齐
        return self._process

    def pid_exists(self, pid):  # noqa: N802 - 与 psutil 对齐
        return True


def test_identity_probe_failure_while_alive_fails_missing_monitor(monkeypatch, unrelated_process):
    """根仍存活却反复无法核实身份：不得静默跳过监控，须 missing_monitor，且不误伤他人。"""
    monkeypatch.setattr(
        ocr_process, "_load_psutil", lambda: _SingleProcessPsutil(_IdentityProbeFailureProcess())
    )
    env = supervise_process(
        _command(_emit_phase() + _sleep(30)),
        init_timeout=30,
        page_timeout=30,
        sample_interval=0.02,
    )
    assert env["ok"] is False
    assert env["error"]["kind"] == "missing_monitor"
    assert env["memory"]["available"] is False
    assert env["memory"]["peak_rss_bytes"] is None  # 绝不归因错误的身份/内存
    assert _wait_dead(env["process"]["pid"])
    assert unrelated_process.poll() is None  # 不误伤无关进程树
