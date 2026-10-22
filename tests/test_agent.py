"""任务 3：可配置、可恢复的研究 Agent（用本地假服务驱动，不接触真实模型）。"""

from __future__ import annotations

import json

import pytest
from fake_provider import FAKE_KEY, FakeChatServer

from litworkbench.agent import available_profiles, load_profile, run_research
from litworkbench.errors import AgentError
from litworkbench.evidence import list_evidence, save_evidence
from litworkbench.ingest import import_document
from litworkbench.tasks import get_task, list_tasks, show_task, task_actions


def action(tool: str, **args) -> str:
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False)


@pytest.fixture
def library(workspace, fixtures):
    """一个含文字与文档两类单元的文献库。"""
    import_document(workspace, fixtures["text_pdf"])
    import_document(workspace, fixtures["docx"])
    return workspace


@pytest.fixture
def server_with(monkeypatch):
    monkeypatch.setenv("LITWORKBENCH_TEST_KEY", FAKE_KEY)

    def factory(script):
        fake = FakeChatServer(script=script)
        fake.__enter__()
        return fake

    created = []

    def make(script):
        fake = factory(script)
        created.append(fake)
        return fake

    yield make
    for fake in created:
        fake.close()


def configure(workspace, server, **overrides):
    config = dict(workspace.config)
    config["provider"] = {
        "base_url": server.base_url,
        "model": "fake-model",
        "api_key_env": "LITWORKBENCH_TEST_KEY",
        "timeout": 5.0,
        "temperature": 0.0,
        "max_output_tokens": 512,
    }
    agent = dict(config.get("agent", {}))
    agent.update(overrides)
    config["agent"] = agent
    workspace._config = config
    return config


def units(workspace):
    return workspace.list_units(limit=500)


# ---------------------------------------------------------------- 基本流程


def test_happy_path_search_read_excerpt_finish(library, server_with):
    workspace = library
    fake = server_with([])
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    quote = "白塔位于县城北侧"
    fake.script = [
        action("search", query="白塔"),
        action("read", unit_id=target["id"]),
        action("excerpt", unit_id=target["id"], quote=quote, category="文物"),
        action("finish", title="白塔考（合成样例）",
               findings=[{"text": "白塔见于卷一文物门。", "evidence_ids": ["PLACEHOLDER"],
                          "section": "源流与沿革"}],
               open_questions=["重修年代待核"]),
    ]
    configure(workspace, fake)

    result = run_research(workspace, "青川白塔见于何处？", profile_name="heritage")
    # 第一次运行会因为证据 ID 未知而反馈，这里先跑到读取摘录为止
    assert result["status"] in {"paused", "completed"}

    # 用真实证据 ID 重放一次完整流程
    evidence = list_evidence(workspace, limit=5)[0]
    assert evidence["task_id"] == result["task_id"]
    fake.script = [
        action("finish", title="白塔考（合成样例）",
               findings=[{"text": "白塔见于卷一文物门。", "evidence_ids": [evidence["id"]],
                          "section": "源流与沿革"}],
               open_questions=["重修年代待核"]),
    ]
    final = run_research(workspace, "", resume_id=result["task_id"])
    assert final["status"] == "completed"
    assert final["report"]["findings"][0]["evidence_ids"] == [evidence["id"]]


def test_fenced_json_is_accepted(library, server_with):
    workspace = library
    fake = server_with(["```json\n" + action("finish", title="未找到",
                                              findings=[], open_questions=["资料没有提到"]) + "\n```"])
    configure(workspace, fake)
    result = run_research(workspace, "有没有白塔？")
    assert result["status"] == "completed"
    assert result["report"]["open_questions"] == ["资料没有提到"]
    assert result["report"]["findings"] == []


def test_protocol_violations_are_recoverable(library, server_with):
    workspace = library
    fake = server_with([
        "这不是 JSON，只是一段闲聊。",
        json.dumps({"tool": "delete_everything", "args": {}}, ensure_ascii=False),
        action("finish", title="未找到", findings=[], open_questions=["无法定位相关记载"]),
    ])
    configure(workspace, fake, max_steps=6)
    result = run_research(workspace, "白塔在哪？")
    assert result["status"] == "completed"
    errors = [item["error"] for item in task_actions(workspace, result["task_id"])
              if item["status"] != "ok"]
    assert any("JSON" in error for error in errors)
    assert any("未知工具" in error or "delete_everything" in error for error in errors)


def test_fabricated_quote_is_refused(library, server_with):
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    fake = server_with([
        action("read", unit_id=target["id"]),
        action("excerpt", unit_id=target["id"], quote="白塔位于县城东侧，为全县最高的古塔。"),
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧", category="文物"),
    ])
    configure(workspace, fake, max_steps=6)
    result = run_research(workspace, "白塔位置？")
    stored = list_evidence(workspace, task_id=result["task_id"])
    assert len(stored) == 1
    assert stored[0]["quote"] == "白塔位于县城北侧"
    errors = [item["error"] for item in task_actions(workspace, result["task_id"])
              if item["status"] != "ok"]
    assert any("引文" in error for error in errors)


def test_excerpt_requires_read_first(library, server_with):
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    fake = server_with([
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧"),
    ])
    configure(workspace, fake, max_steps=4)
    result = run_research(workspace, "白塔位置？")
    assert list_evidence(workspace, task_id=result["task_id"]) == []
    errors = [item["error"] for item in task_actions(workspace, result["task_id"])
              if item["status"] != "ok"]
    assert any("read" in error for error in errors)


def test_unknown_and_foreign_evidence_ids_are_refused(library, server_with):
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    # 先建一个属于别的任务的摘录
    foreign = save_evidence(workspace, target["id"], "白塔位于县城北侧")
    fake = server_with([
        action("read", unit_id=target["id"]),
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧", category="文物"),
        action("finish", title="带编造引用",
               findings=[{"text": "结论一", "evidence_ids": ["ev_完全不存在"],
                          "section": "源流与沿革"}],
               open_questions=[]),
        action("finish", title="仍然编造",
               findings=[{"text": "结论二", "evidence_ids": [foreign["id"]],
                          "section": "源流与沿革"}],
               open_questions=[]),
    ])
    configure(workspace, fake, max_steps=8)
    result = run_research(workspace, "白塔位置？")
    assert result["status"] == "paused"  # 步骤用尽且没有合法报告
    assert result["report"] is None
    errors = [item["error"] for item in task_actions(workspace, result["task_id"])
              if item["status"] != "ok"]
    assert any("不存在" in error or "未知" in error for error in errors)
    assert any("其他任务" in error or "不属于" in error for error in errors)


def test_findings_without_evidence_cannot_be_submitted(library, server_with):
    workspace = library
    fake = server_with([
        action("finish", title="无凭据结论",
               findings=[{"text": "白塔始建于汉代。", "evidence_ids": [], "section": "源流与沿革"}],
               open_questions=[]),
        action("finish", title="空结论", findings=[], open_questions=["没有找到可引用内容"]),
    ])
    configure(workspace, fake, max_steps=5)
    result = run_research(workspace, "白塔始建年代？")
    assert result["status"] == "completed"
    assert result["report"]["findings"] == []
    errors = [item["error"] for item in task_actions(workspace, result["task_id"])
              if item["status"] != "ok"]
    assert any("evidence_ids" in error for error in errors)


def test_injected_instructions_cannot_fabricate_evidence(library, server_with, workspace_extra=None):
    """文档正文里的指令只是资料，不能绕过逐字校验。"""
    workspace = library
    injected = next(u for u in units(workspace) if "合成测试数据" in (u["text"] or ""))
    fake = server_with([
        action("read", unit_id=injected["id"]),
        action("excerpt", unit_id=injected["id"],
               quote="白塔始建于汉代，为全国重点文物保护单位。"),
        action("finish", title="照做",
               findings=[{"text": "白塔始建于汉代", "evidence_ids": ["ev_伪造"],
                          "section": "源流与沿革"}],
               open_questions=[]),
    ])
    configure(workspace, fake, max_steps=6)
    result = run_research(workspace, "白塔始建年代？")
    assert list_evidence(workspace, task_id=result["task_id"]) == []
    assert result["status"] == "paused"
    assert result["report"] is None


# ---------------------------------------------------------------- 预算/恢复


def test_step_budget_exhaustion_pauses_without_report(library, server_with):
    workspace = library
    fake = server_with([action("search", query="白塔")])
    configure(workspace, fake, max_steps=2)
    result = run_research(workspace, "白塔？", max_steps=2)
    assert result["status"] == "paused"
    assert result["report"] is None
    task = get_task(workspace, result["task_id"])
    assert task["step_count"] == 2
    assert task["status"] == "paused"


def test_resume_continues_without_duplicating_evidence(library, server_with):
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    fake = server_with([
        action("search", query="白塔"),
        action("read", unit_id=target["id"]),
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧", category="文物"),
        action("search", query="碑"),
    ])
    configure(workspace, fake, max_steps=3)
    first = run_research(workspace, "白塔与碑刻？")
    assert first["status"] == "paused"
    evidence_before = list_evidence(workspace, task_id=first["task_id"])
    assert len(evidence_before) == 1

    fake.script = [
        action("finish", title="白塔考（合成样例）",
               findings=[{"text": "白塔见于卷一。", "evidence_ids": [evidence_before[0]["id"]],
                          "section": "源流与沿革"}],
               open_questions=[]),
    ]
    second = run_research(workspace, "", resume_id=first["task_id"], max_steps=4)
    assert second["status"] == "completed"
    assert len(list_evidence(workspace, task_id=first["task_id"])) == 1
    assert second["task_id"] == first["task_id"]


def test_completed_task_cannot_be_resumed(library, server_with):
    workspace = library
    fake = server_with([action("finish", title="完成", findings=[], open_questions=[])])
    configure(workspace, fake)
    result = run_research(workspace, "问题？")
    assert result["status"] == "completed"
    with pytest.raises(AgentError):
        run_research(workspace, "", resume_id=result["task_id"])


def test_resume_uses_original_profile_snapshot(library, server_with):
    workspace = library
    fake = server_with([action("search", query="白塔")])
    configure(workspace, fake, max_steps=1, default_profile="general")
    first = run_research(workspace, "白塔？", profile_name="heritage")

    # 修改配置与方案，不应影响历史任务
    config = dict(workspace.config)
    config["agent"] = {**config["agent"], "default_profile": "general", "max_steps": 3}
    workspace._config = config
    snapshot = get_task(workspace, first["task_id"])["profile_snapshot"]
    assert snapshot["name"] == "heritage"

    fake.script = [action("finish", title="恢复完成", findings=[], open_questions=[])]
    second = run_research(workspace, "", resume_id=first["task_id"], max_steps=3)
    assert second["status"] == "completed"
    assert get_task(workspace, first["task_id"])["profile_snapshot"]["name"] == "heritage"


def test_allowed_tools_restriction_is_explicit(library, server_with):
    """没有 finish 就明确拒绝开任务（不放宽权限）。"""
    workspace = library
    fake = server_with([action("finish", title="不该成功", findings=[], open_questions=[])])
    configure(workspace, fake, allowed_tools=["search", "read"])
    with pytest.raises(AgentError) as exc:
        run_research(workspace, "白塔？")
    assert "finish" in str(exc.value)
    assert list_tasks(workspace) == []


def test_tool_outside_allow_list_gets_feedback(library, server_with):
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    fake = server_with([
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧"),
        action("search", query="白塔"),
        action("finish", title="未找到", findings=[], open_questions=["没有可用摘录"]),
    ])
    configure(workspace, fake, allowed_tools=["search", "read", "finish"], max_steps=5)
    result = run_research(workspace, "白塔？")
    assert result["status"] == "completed"
    errors = [item["error"] for item in task_actions(workspace, result["task_id"])
              if item["status"] != "ok"]
    assert any("excerpt" in error and "允许" in error for error in errors)
    assert list_evidence(workspace, task_id=result["task_id"]) == []


def test_repeated_excerpt_is_idempotent(library, server_with):
    """崩溃恢复或模型重复调用不会产生重复证据。"""
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    excerpt = action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧", category="文物")
    fake = server_with([
        action("read", unit_id=target["id"]),
        excerpt,
        excerpt,
        excerpt,
        action("finish", title="完成", findings=[], open_questions=[]),
    ])
    configure(workspace, fake, max_steps=8)
    result = run_research(workspace, "白塔位置？")
    stored = list_evidence(workspace, task_id=result["task_id"])
    assert len(stored) == 1
    assert len(result["evidence"]) == 1


def test_stale_evidence_cannot_support_a_finding(library, server_with):
    from litworkbench.evidence import correct_unit

    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    fake = server_with([
        action("read", unit_id=target["id"]),
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧"),
    ])
    configure(workspace, fake, max_steps=4)
    first = run_research(workspace, "白塔位置？")
    evidence = list_evidence(workspace, task_id=first["task_id"])[0]
    correct_unit(workspace, target["id"],
                 workspace.get_unit(target["id"])["original_text"].replace("北侧", "北郊"),
                 "校订地名")

    fake.script = [action("finish", title="用旧证据",
                          findings=[{"text": "结论", "evidence_ids": [evidence["id"]],
                                     "section": "源流与沿革"}],
                          open_questions=[])]
    resumed = run_research(workspace, "", resume_id=first["task_id"], max_steps=3)
    assert resumed["status"] == "paused"
    errors = [item["error"] for item in task_actions(workspace, first["task_id"])
              if item["status"] != "ok"]
    assert any("stale" in error for error in errors)


def test_resume_keeps_history_and_counts_steps_cumulatively(library, server_with):
    workspace = library
    fake = server_with([action("search", query="白塔"), action("search", query="碑")])
    configure(workspace, fake, max_steps=2)
    first = run_research(workspace, "白塔？", max_steps=2)
    assert first["status"] == "paused"
    assert first["steps"] == 2

    fake.script = [action("search", query="塔")]
    second = run_research(workspace, "", resume_id=first["task_id"], max_steps=2)
    assert second["steps"] == 4
    detail = show_task(workspace, first["task_id"])
    assert len(detail["actions"]) == 4
    assert [item["step"] for item in detail["actions"]] == [1, 2, 3, 4]
    assert len(detail["messages"]) >= 3


def test_resume_after_failure_is_possible(library, server_with):
    workspace = library
    fake = server_with([{"status": 503, "body": {"error": "暂时不可用"}}])
    configure(workspace, fake, max_steps=4)
    first = run_research(workspace, "白塔？")
    assert first["status"] == "failed"

    fake.script = [action("finish", title="恢复后完成", findings=[], open_questions=["资料有限"])]
    second = run_research(workspace, "", resume_id=first["task_id"], max_steps=3)
    assert second["status"] == "completed"
    assert second["error"] == ""


def test_custom_profile_file_is_supported(library, server_with, tmp_path):
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    profile_path = tmp_path / "我的 方案.toml"
    profile_path.write_text(
        'name = "local-heritage"\n'
        'description = "自定义方案"\n'
        'prompt = "只关心碑刻。"\n'
        'categories = ["碑刻"]\n'
        'sections = ["碑刻与题记"]\n'
        '[aliases]\n'
        '"碑" = ["碑刻", "石刻"]\n',
        encoding="utf-8",
    )
    fake = server_with([
        action("read", unit_id=target["id"]),
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧", category="碑刻"),
        action("finish", title="自定义方案完成", findings=[], open_questions=[]),
    ])
    configure(workspace, fake, max_steps=6)
    result = run_research(workspace, "碑刻在哪？", profile_name=str(profile_path))
    assert result["status"] == "completed"
    assert result["profile"] == "local-heritage"
    snapshot = get_task(workspace, result["task_id"])["profile_snapshot"]
    assert snapshot["categories"] == ["碑刻"]
    assert snapshot["aliases"]["碑"] == ["碑刻", "石刻"]


def test_invalid_custom_profile_is_reported(library, server_with, tmp_path):
    workspace = library
    broken = tmp_path / "坏的 方案.toml"
    broken.write_text("categories = 12\n", encoding="utf-8")
    fake = server_with([action("finish", title="x", findings=[], open_questions=[])])
    configure(workspace, fake)
    with pytest.raises(AgentError):
        run_research(workspace, "问题？", profile_name=str(broken))
    with pytest.raises(AgentError):
        run_research(workspace, "问题？", profile_name=str(tmp_path / "不存在.toml"))


# ---------------------------------------------------------------- 故障处理


def test_provider_error_fails_task_without_leaking_key(library, server_with, tmp_path):
    workspace = library
    fake = server_with([{"status": 401, "body": {"error": f"bad key {FAKE_KEY}"}}])
    configure(workspace, fake, max_steps=3)
    result = run_research(workspace, "白塔？")
    assert result["status"] == "failed"
    assert FAKE_KEY not in json.dumps(result, ensure_ascii=False)
    assert FAKE_KEY not in get_task(workspace, result["task_id"])["error"]

    for path in workspace.root.rglob("*"):
        if path.is_file():
            assert FAKE_KEY not in path.read_bytes().decode("utf-8", "ignore"), path


def test_missing_provider_config_fails_without_network(library, monkeypatch):
    workspace = library
    monkeypatch.delenv("LITWORKBENCH_TEST_KEY", raising=False)
    config = dict(workspace.config)
    config["provider"] = {"base_url": "", "model": "", "api_key_env": "LITWORKBENCH_TEST_KEY"}
    workspace._config = config
    with pytest.raises(AgentError) as exc:
        run_research(workspace, "白塔？")
    assert "provider" in str(exc.value) or "配置" in str(exc.value)
    assert list_tasks(workspace) == []


def test_keyboard_interrupt_records_interruption(library, server_with, monkeypatch):
    workspace = library
    fake = server_with([action("search", query="白塔")])
    configure(workspace, fake, max_steps=5)

    from litworkbench import agent as agent_mod

    def boom(self, messages):
        raise KeyboardInterrupt()

    monkeypatch.setattr(agent_mod.ChatProvider, "complete", boom)
    with pytest.raises(KeyboardInterrupt):
        run_research(workspace, "白塔？")
    task = list_tasks(workspace)[0]
    assert task["status"] == "interrupted"
    assert task["step_count"] == 0


def test_unknown_profile_is_reported(library, server_with):
    workspace = library
    fake = server_with([action("finish", title="x", findings=[], open_questions=[])])
    configure(workspace, fake)
    with pytest.raises(AgentError):
        run_research(workspace, "问题？", profile_name="不存在的方案")


def test_builtin_profiles_are_loadable():
    names = available_profiles()
    assert "general" in names and "heritage" in names
    profile = load_profile("heritage")
    assert profile["categories"]
    assert profile["sections"]
    assert profile["prompt"]


def test_show_task_includes_actions_and_evidence(library, server_with):
    workspace = library
    target = next(u for u in units(workspace) if "白塔位于县城北侧" in (u["text"] or ""))
    fake = server_with([
        action("read", unit_id=target["id"]),
        action("excerpt", unit_id=target["id"], quote="白塔位于县城北侧"),
        action("finish", title="完成", findings=[], open_questions=[]),
    ])
    configure(workspace, fake, max_steps=6)
    result = run_research(workspace, "白塔位置？")
    detail = show_task(workspace, result["task_id"])
    assert detail["id"] == result["task_id"]
    assert [item["tool"] for item in detail["actions"]] == ["read", "excerpt", "finish"]
    assert detail["evidence"][0]["quote"] == "白塔位于县城北侧"
    assert detail["profile"] == "heritage"
