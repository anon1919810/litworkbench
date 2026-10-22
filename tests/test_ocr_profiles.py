"""Task B1 固定识别配置与单页入口的 RED 测试。

本模块只覆盖两个**新增**生产能力，尚未实现时在导入阶段即失败（刻意的 RED）：

* ``litworkbench.ocr_profiles``：两种固定配置、真实能力检查、模型/参数指纹。
* ``litworkbench.ocr_process.run_page``：受控单页入口的外观（成年后由 B0 的
  :func:`litworkbench.ocr_process.supervise_process` 驱动真正的短子进程）。

冻结接口（本轮由测试固定，供 GREEN 实现）：

``ocr_profiles.get_profiles() -> list[dict]``
    仅返回 ``compatible-v1``、``small-lowmem-v1`` 两项；每项形如
    ``{id, label, description, available: bool, reason: str|None}``。
    绝不回显本机绝对模型路径，也绝不访问网络或下载模型。

``ocr_profiles.profile_runtime(profile_id) -> dict``
    未知 profile 抛 :class:`ValidationError`。键至少包含：
    ``render_scale, det_batch, rec_batch, cls_batch, onnx_intra_threads,
    onnx_inter_threads, init_timeout, page_timeout, max_memory_bytes``。
    ``small-lowmem-v1`` 固定：render_scale=2；det/rec/cls 批次=1；ONNX
    intra/inter=2；初始化 120 秒、单页 60 秒；RSS/private 上限 2.5 GiB。
    ``compatible-v1`` 保留当前产品默认行为（不被强制成省资源批次）。

``ocr_profiles._local_capability() -> dict``  （窄注入接缝，供测试替换）
    返回 ``{"available": bool, "reason": str|None}``；产品端探测**本机**
    依赖/模型/资源监控的真实状态。返回 ``None`` 或缺失表示不可用，
    必须是显式失败原因，不能静默、不能假装可用。

``ocr_process.run_page(workspace, unit, profile_id, *, checkpoint=None) -> dict``
    ``unit`` 是单元视图字典；返回受控进程信封
    ``{"ok": bool, "result": dict|None, "error": {"kind","message"}|None,
    "timing": {...}, "memory": {...}}``。未知 profile 在**启动任何子进程之前**
    抛 :class:`ValidationError`。
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import socket

import pytest

from litworkbench import ocr_process
from litworkbench.errors import ValidationError
from litworkbench.ingest import import_document


def _profiles():
    """延迟导入缺失的新模块 ``litworkbench.ocr_profiles``：本轮刻意保留 RED。"""
    return importlib.import_module("litworkbench.ocr_profiles")


FIXED_PROFILE_IDS = {"compatible-v1", "small-lowmem-v1"}
COMPATIBLE = "compatible-v1"
LOWMEM = "small-lowmem-v1"
TWO_POINT_FIVE_GIB = 2684354560  # 2.5 * 1024**3

RUNTIME_KEYS = {
    "render_scale", "det_batch", "rec_batch", "cls_batch",
    "onnx_intra_threads", "onnx_inter_threads",
    "init_timeout", "page_timeout", "max_memory_bytes",
}


# ------------------------------------------------------------------ 固定配置清单


def test_get_profiles_lists_exactly_the_two_fixed_options():
    profiles = _profiles().get_profiles()
    assert isinstance(profiles, list)
    assert {item["id"] for item in profiles} == FIXED_PROFILE_IDS
    for item in profiles:
        assert {"id", "label", "description", "available", "reason"} <= set(item)
        assert isinstance(item["label"], str) and item["label"]
        assert isinstance(item["description"], str) and item["description"]
        assert isinstance(item["available"], bool)
        assert item["reason"] is None or isinstance(item["reason"], str)


def test_profiles_never_expose_absolute_model_paths():
    text = json.dumps(_profiles().get_profiles(), ensure_ascii=False)
    assert not re.search(r"[A-Za-z]:[\\/]", text), text
    assert "\\\\" not in text, text


def test_get_profiles_never_downloads_models_or_touches_network(monkeypatch):
    def _no_network(*args, **kwargs):  # noqa: ARG001 - 只用来断网
        raise AssertionError("列举配置不得访问网络或下载模型")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket, "getaddrinfo", _no_network)
    profiles = _profiles().get_profiles()
    assert {item["id"] for item in profiles} == FIXED_PROFILE_IDS


@pytest.mark.parametrize(
    "available,reason",
    [(True, None), (False, "本机缺少推理依赖或模型权重")],
)
def test_profile_availability_truthfully_reflects_local_state(monkeypatch, available, reason):
    monkeypatch.setattr(
        _profiles(), "_local_capability",
        lambda: {"available": available, "reason": reason},
    )
    profiles = _profiles().get_profiles()
    for item in profiles:
        assert item["available"] is available
        if available:
            assert item["reason"] in (None, "")
        else:
            assert item["reason"]


# ------------------------------------------------------------------ 固定参数


def test_small_lowmem_runtime_parameters_are_fixed():
    runtime = _profiles().profile_runtime(LOWMEM)
    assert RUNTIME_KEYS <= set(runtime)
    assert runtime["render_scale"] == 2
    assert runtime["det_batch"] == 1
    assert runtime["rec_batch"] == 1
    assert runtime["cls_batch"] == 1
    assert runtime["onnx_intra_threads"] == 2
    assert runtime["onnx_inter_threads"] == 2
    assert runtime["init_timeout"] == 120
    assert runtime["page_timeout"] == 60
    assert runtime["max_memory_bytes"] == TWO_POINT_FIVE_GIB


def test_compatible_profile_keeps_product_defaults():
    runtime = _profiles().profile_runtime(COMPATIBLE)
    assert RUNTIME_KEYS <= set(runtime)
    assert float(runtime["render_scale"]) > 0
    assert int(runtime["init_timeout"]) > 0
    assert int(runtime["page_timeout"]) > 0
    assert int(runtime["max_memory_bytes"]) > 0


def test_unknown_profile_is_rejected():
    with pytest.raises(ValidationError):
        _profiles().profile_runtime("medium-lowmem-v1")


# ------------------------------------------------------------------ run_page 外观


def test_run_page_facade_signature():
    signature = inspect.signature(ocr_process.run_page)
    names = list(signature.parameters)
    assert names[:3] == ["workspace", "unit", "profile_id"]
    checkpoint = signature.parameters["checkpoint"]
    assert checkpoint.kind is inspect.Parameter.KEYWORD_ONLY
    assert checkpoint.default is None


def test_run_page_rejects_unknown_profile_before_spawning(workspace, fixtures):
    document = import_document(workspace, fixtures["text_pdf"], ocr_mode="off")
    unit = workspace.units_for_doc(document["id"])[0]
    with pytest.raises(ValidationError):
        ocr_process.run_page(workspace, unit, "unknown-v9")
