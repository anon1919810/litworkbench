"""Independent regressions for read provenance and durable task checkpoints."""
import json

import pytest

from test_agent import action, configure, library, server_with, units
from litworkbench import agent
from litworkbench.evidence import correct_unit, list_evidence
from litworkbench.tasks import task_actions


def target(workspace):
    return next(u for u in units(workspace) if "白塔位于县城北侧" in u["text"])


def test_malformed_tool_type_gets_feedback(library, server_with):
    fake = server_with(['{"tool": [], "args": {}}', action("finish", findings=[])])
    configure(library, fake)
    result = agent.run_research(library, "合成资料测试", max_steps=2)
    assert result["status"] == "completed"
    assert task_actions(library, result["task_id"])[0]["status"] == "error"


def test_correction_requires_new_read(library, server_with):
    unit = target(library)
    fake = server_with([action("read", unit_id=unit["id"])])
    configure(library, fake)
    first = agent.run_research(library, "合成资料测试", max_steps=1)
    correct_unit(library, unit["id"], "校订新版独有文字。", "合成验收")
    fake.script = [action("excerpt", unit_id=unit["id"], quote="新版独有文字")]
    agent.run_research(library, "", resume_id=first["task_id"], max_steps=1)
    assert list_evidence(library, task_id=first["task_id"]) == []


def test_long_unit_requires_reading_the_quoted_range(library, server_with):
    unit = target(library)
    correct_unit(library, unit["id"], "合成文字" * 1100 + "末尾关键记载。", "合成验收")
    fake = server_with([
        action("read", unit_id=unit["id"]),
        action("excerpt", unit_id=unit["id"], quote="末尾关键记载。"),
        action("read", unit_id=unit["id"], offset=4000),
        action("excerpt", unit_id=unit["id"], quote="末尾关键记载。"),
    ])
    configure(library, fake)
    result = agent.run_research(library, "合成资料测试", max_steps=4)
    actions = task_actions(library, result["task_id"])
    assert [a["status"] for a in actions] == ["ok", "error", "ok", "ok"]
    assert actions[0]["result"]["data"]["next_offset"] == 4000
    assert len(list_evidence(library, task_id=result["task_id"])) == 1


def test_normalized_quote_retry_is_idempotent(library, server_with):
    unit = target(library)
    correct_unit(library, unit["id"], "白塔\n建于合成年间。", "合成验收")
    excerpt = action("excerpt", unit_id=unit["id"], quote="白塔建于合成年间。")
    fake = server_with([action("read", unit_id=unit["id"]), excerpt, excerpt])
    configure(library, fake)
    result = agent.run_research(library, "合成资料测试", max_steps=3)
    assert len(list_evidence(library, task_id=result["task_id"])) == 1


def test_repeated_sentence_uses_the_occurrence_actually_read(library, server_with):
    unit = target(library)
    correct_unit(library, unit["id"], "重复引文" + "甲" * 4100 + "重复引文", "合成验收")
    fake = server_with([action("read", unit_id=unit["id"], offset=4104),
                        action("excerpt", unit_id=unit["id"], quote="重复引文")])
    configure(library, fake)
    result = agent.run_research(library, "合成资料测试", max_steps=2)
    evidence = list_evidence(library, task_id=result["task_id"])
    assert len(evidence) == 1
    assert evidence[0]["start_offset"] == 4104 and evidence[0]["end_offset"] == 4108


def test_resume_recovers_tool_result_and_step_after_interrupt(library, server_with, monkeypatch):
    unit = target(library)
    fake = server_with([action("read", unit_id=unit["id"])])
    configure(library, fake)
    append = agent.append_message

    def interrupted(workspace, task_id, role, content):
        if role == "tool":
            raise KeyboardInterrupt()
        return append(workspace, task_id, role, content)

    monkeypatch.setattr(agent, "append_message", interrupted)
    with pytest.raises(KeyboardInterrupt):
        agent.run_research(library, "合成资料测试", max_steps=1)
    task_id = agent.list_tasks(library)[0]["id"]
    monkeypatch.setattr(agent, "append_message", append)
    fake.script = [action("excerpt", unit_id=unit["id"], quote="白塔位于县城北侧")]
    agent.run_research(library, "", resume_id=task_id, max_steps=1)
    messages = fake.requests[-1]["messages"]
    assert any("白塔位于县城北侧" in m["content"] for m in messages)
    assert [a["step"] for a in task_actions(library, task_id)] == [1, 2]


def test_completed_finish_recovers_without_provider(library, server_with, monkeypatch):
    fake = server_with([action("finish", title="合成验收", findings=[], open_questions=["无证据"])])
    configure(library, fake)
    save = agent.save_task_report

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(agent, "save_task_report", interrupted)
    with pytest.raises(KeyboardInterrupt):
        agent.run_research(library, "合成资料测试", max_steps=1)
    task_id = agent.list_tasks(library)[0]["id"]
    monkeypatch.setattr(agent, "save_task_report", save)
    monkeypatch.delenv("LITWORKBENCH_TEST_KEY")
    result = agent.run_research(library, "", resume_id=task_id, max_steps=1)
    assert result["status"] == "completed" and fake.hits == 1
