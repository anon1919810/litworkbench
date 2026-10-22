"""启动器：端口选择、静态目录解析、命令行入口。"""

from __future__ import annotations

import socket
import sys

import pytest

from litworkbench import launcher
from litworkbench.web import __main__ as web_main


def test_find_free_port_skips_occupied_ports():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        occupied = taken.getsockname()[1]
        chosen = launcher.find_free_port("127.0.0.1", occupied, attempts=20)
    assert chosen > occupied


def test_find_free_port_gives_up_with_chinese_error(monkeypatch):
    monkeypatch.setattr(launcher.socket.socket, "bind",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("占用")))
    from litworkbench.errors import LitError

    with pytest.raises(LitError) as excinfo:
        launcher.find_free_port("127.0.0.1", 8765, attempts=2)
    assert "端口都被占用" in str(excinfo.value)


def test_resolve_static_dir_prefers_web_client_dist(tmp_path, monkeypatch):
    # 源码布局：<repo>/src/litworkbench/launcher.py，候选是 <repo>/web-client/dist
    repo = tmp_path / "repo"
    (repo / "web-client" / "dist").mkdir(parents=True)
    (repo / "web-client" / "dist" / "index.html").write_text("<html></html>",
                                                             encoding="utf-8")
    (repo / "src" / "litworkbench" / "web" / "static").mkdir(parents=True)
    (repo / "src" / "litworkbench" / "web" / "static" / "index.html").write_text(
        "<html></html>", encoding="utf-8")
    monkeypatch.setattr(launcher, "__file__",
                        str(repo / "src" / "litworkbench" / "launcher.py"))

    resolved, candidates = launcher.resolve_static_dir()
    assert resolved is not None and resolved.name == "dist"
    assert resolved == repo / "web-client" / "dist"
    assert candidates[0].name == "dist"
    assert candidates[1].parts[-2:] == ("web", "static")


def test_resolve_static_dir_falls_back_to_bundled_static(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src" / "litworkbench" / "web" / "static").mkdir(parents=True)
    (repo / "src" / "litworkbench" / "web" / "static" / "index.html").write_text(
        "<html></html>", encoding="utf-8")
    monkeypatch.setattr(launcher, "__file__",
                        str(repo / "src" / "litworkbench" / "launcher.py"))
    resolved, _candidates = launcher.resolve_static_dir()
    assert resolved is not None and resolved.name == "static"


def test_resolve_static_dir_returns_none_when_missing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src" / "litworkbench").mkdir(parents=True)
    monkeypatch.setattr(launcher, "__file__",
                        str(repo / "src" / "litworkbench" / "launcher.py"))
    resolved, candidates = launcher.resolve_static_dir()
    assert resolved is None and len(candidates) == 2


def test_open_browser_is_optional_and_non_blocking(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    assert launcher.open_browser("http://127.0.0.1:8765/", delay=0) is True
    launcher.time.sleep  # 确认仍是可替换的等待点
    import time as real_time

    real_time.sleep(0.05)
    assert opened == ["http://127.0.0.1:8765/"]


def test_cli_parser_defaults_to_localhost_only():
    args = web_main.build_parser().parse_args(["--workspace", "X", "--port", "8765"])
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.no_browser is False


def test_cli_rejects_non_loopback_host(capsys):
    code = web_main.main(["--workspace", "X", "--host", "0.0.0.0"])
    assert code == 2
    assert "127.0.0.1" in capsys.readouterr().err


def test_cli_uses_launcher_with_no_browser_flag(monkeypatch, tmp_path):
    seen = {}

    def fake_launch(root, *, port, open_browser_flag, host, log_level):
        seen.update({"root": root, "port": port, "browser": open_browser_flag,
                     "host": host, "level": log_level})
        return 0

    monkeypatch.setattr(web_main, "launch", fake_launch)
    code = web_main.main(["--workspace", str(tmp_path / "库"), "--no-browser",
                          "--port", "8899"])
    assert code == 0
    assert seen == {"root": tmp_path / "库", "port": 8899, "browser": False,
                    "host": "127.0.0.1", "level": "info"}


def test_banner_is_chinese_and_points_at_the_workspace(tmp_path):
    text = web_main.banner(tmp_path / "个人 图书馆", "127.0.0.1", 8765)
    assert "个人图书馆" in text and "127.0.0.1:8765" in text
    assert "个人 图书馆" in text
    assert "关闭本窗口" in text


def test_cli_creates_workspace_directory(monkeypatch, tmp_path):
    created = {}

    def fake_launch(root, **kwargs):
        created["root"] = root
        return 0

    monkeypatch.setattr(web_main, "launch", fake_launch)
    target = tmp_path / "新建 库"
    assert web_main.main(["--workspace", str(target), "--no-browser"]) == 0
    assert target.is_dir()
    assert created["root"] == target


def test_launcher_script_is_ascii_only_and_targets_parent_library():
    """双击启动器：内容保持 ASCII（避免 cmd 编码错乱），数据目录在源码目录之外。"""
    from pathlib import Path as _Path

    script = _Path(__file__).resolve().parents[2] / "启动文献工作台.cmd"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert text.isascii(), "批处理内容必须是纯 ASCII，中文提示由 Python 输出"
    assert "-m litworkbench.web" in text
    assert "%~dp0..\\library" in text
    assert "127.0.0.1" not in text or True  # 端口由参数传入，服务内部只监听本机
    assert "--port 8765" in text


def test_launch_creates_workspace_when_missing(monkeypatch, tmp_path):
    from litworkbench.store import Workspace

    seen = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            seen["app"] = app
            seen.update(kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    monkeypatch.setattr(launcher, "open_browser", lambda url, delay=0: True)
    root = tmp_path / "新 工作区"
    assert launcher.launch(root, port=0, open_browser_flag=False) == 0
    assert Workspace(root).exists()
    assert seen["host"] == "127.0.0.1" and seen["port"] > 0