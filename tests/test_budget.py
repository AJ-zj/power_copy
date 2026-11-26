"""TaskBudget 验收测试（全 Mock，不联网）。"""

import json

import httpx2 as httpx
import pytest
from openai import APIStatusError
from pydantic import BaseModel, Field

from powercopy.core.agent import Agent
from powercopy.core.budget import BudgetExceeded, TaskBudget
from powercopy.core.llm import LLMClient
from powercopy.core.schemas import PolicyAnalysis
from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy import policy_search_tool
from powercopy.tools.registry import ToolRegistry

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")
VALID = json.dumps(
    {
        "policy_title": "安徽电价通知",
        "summary": "摘要",
        "key_points": ["要点"],
        "relevant": True,
    },
    ensure_ascii=False,
)


def status_error(code, body=None):
    return APIStatusError(
        message="mock", response=httpx.Response(code, request=REQUEST), body=body
    )


def ok_result(content="ok", tokens=100):
    if tokens is None:
        return {
            "content": content,
            "model": "glm-5.3-flash",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    return {
        "content": content,
        "model": "glm-5.3-flash",
        "input_tokens": tokens // 2,
        "output_tokens": tokens // 2,
        "total_tokens": tokens,
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("powercopy.core.llm.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    return LLMClient()


@pytest.fixture
def no_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr(
        "powercopy.core.llm.time.sleep", lambda s: delays.append(s)
    )
    return delays


def script(client, monkeypatch, behaviors):
    calls = {"count": 0}

    def fake_once(messages, model=None, **kwargs):
        i = calls["count"]
        calls["count"] += 1
        behavior = behaviors[min(i, len(behaviors) - 1)]
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    monkeypatch.setattr(client, "_invoke_once", fake_once)
    return calls


# ============================================================ TaskBudget 单测
class TestTaskBudgetUnit:
    def test_defaults(self):
        b = TaskBudget()
        assert b.max_api_attempts == 8
        assert b.max_known_tokens == 20000
        assert b.api_attempts == 0
        assert b.known_total_tokens == 0
        assert b.usage_incomplete is False

    @pytest.mark.parametrize(
        "kwargs", [{"max_api_attempts": 0}, {"max_known_tokens": 0},
                   {"max_api_attempts": -1}, {"max_known_tokens": -5}]
    )
    def test_rejects_non_positive(self, kwargs):
        with pytest.raises(ValueError, match="预算必须大于零"):
            TaskBudget(**kwargs)

    def test_reserve_increments(self):
        b = TaskBudget(max_api_attempts=3)
        b.reserve_api_attempt()
        assert b.api_attempts == 1
        assert b.stop_reason() is None

    def test_api_exhausted_reason(self):
        b = TaskBudget(max_api_attempts=2)
        b.reserve_api_attempt()
        b.reserve_api_attempt()
        assert b.stop_reason() == "api_budget_exhausted"

    def test_reserve_raises_when_exhausted(self):
        b = TaskBudget(max_api_attempts=1)
        b.reserve_api_attempt()
        with pytest.raises(BudgetExceeded) as exc:
            b.reserve_api_attempt()
        assert exc.value.reason == "api_budget_exhausted"

    def test_token_exhausted_reason(self):
        b = TaskBudget(max_known_tokens=100)
        b.record_usage(100)
        assert b.stop_reason() == "token_budget_exhausted"

    def test_token_accumulates(self):
        b = TaskBudget(max_known_tokens=1000)
        b.record_usage(100)
        b.record_usage(200)
        assert b.known_total_tokens == 300

    def test_record_usage_none_marks_incomplete(self):
        b = TaskBudget()
        b.record_usage(None)
        assert b.usage_incomplete is True
        assert b.known_total_tokens == 0

    @pytest.mark.parametrize("bad", [-1, "100", 1.5, True])
    def test_record_usage_rejects_bad_values(self, bad):
        b = TaskBudget()
        b.record_usage(bad)
        assert b.usage_incomplete is True

    def test_check_raises(self):
        b = TaskBudget(max_api_attempts=1, max_known_tokens=100)
        b.api_attempts = 1
        with pytest.raises(BudgetExceeded):
            b.check()

    def test_snapshot(self):
        b = TaskBudget(max_api_attempts=4, max_known_tokens=500)
        b.reserve_api_attempt()
        b.record_usage(120)
        snap = b.snapshot()
        assert snap == {
            "api_attempts": 1,
            "max_api_attempts": 4,
            "remaining_api_attempts": 3,
            "known_total_tokens": 120,
            "max_known_tokens": 500,
            "usage_incomplete": False,
        }


# ============================================================ 验收 1：API 硬限制
class TestApiHardLimit:
    def test_never_exceeds_global_limit(self, client, no_sleep, monkeypatch):
        calls = script(client, monkeypatch, [ok_result()] * 20)
        budget = TaskBudget(max_api_attempts=3, max_known_tokens=10**9)

        for _ in range(3):
            client.invoke([{"role": "user", "content": "hi"}], budget=budget)

        assert calls["count"] == 3
        assert budget.api_attempts == 3

        # 第 4 次必须直接拒绝，不能发出请求
        with pytest.raises(BudgetExceeded) as exc:
            client.invoke([{"role": "user", "content": "hi"}], budget=budget)
        assert exc.value.reason == "api_budget_exhausted"
        assert calls["count"] == 3, "预算耗尽后绝不能发出新请求"


# ============================================================ 验收 2：局部与全局
class TestLocalVsGlobal:
    def test_local_allows_three_global_only_two(self, client, no_sleep, monkeypatch):
        # 局部 RetryPolicy 允许 3 次；全局只剩 2 次
        calls = script(client, monkeypatch, [status_error(503)] * 10)
        budget = TaskBudget(max_api_attempts=2, max_known_tokens=10**9)

        with pytest.raises(BudgetExceeded) as exc:
            client.invoke([{"role": "user", "content": "hi"}], budget=budget)

        assert exc.value.reason == "api_budget_exhausted"
        assert calls["count"] == 2, "实际只能请求 2 次，不是局部的 3 次"
        assert budget.api_attempts == 2


# ============================================================ 验收 3：跨轮共享
class TestAgentSharesBudget:
    def test_agent_rounds_share_one_budget(self, monkeypatch):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        seen = []

        class FakeLLM:
            def __init__(self):
                self.round = 0

            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                self.round += 1
                seen.append(budget)
                assert budget is not None
                budget.reserve_api_attempt()
                budget.record_usage(100)
                if self.round == 1:
                    return {
                        "content": None,
                        "tool_calls": [{
                            "id": "c1", "type": "function",
                            "function": {"name": "search_policy",
                                         "arguments": '{"region":"安徽省","keyword":"电价"}'},
                        }],
                        "finish_reason": "tool_calls",
                    }
                return {"content": "完成", "tool_calls": [], "finish_reason": "stop"}

        agent = Agent(
            llm=FakeLLM(), registry=registry, system_prompt="提示",
            max_api_attempts=5,
        )
        result = agent.run("查询")

        assert result["status"] == "completed"
        assert len(seen) == 2
        assert seen[0] is seen[1], "多轮必须共享同一个 TaskBudget 实例"
        assert result["budget"]["api_attempts"] == 2


# ============================================================ 验收 4：累计 Token
class TestTokenAccumulation:
    def test_multi_round_tokens_accumulate(self, client, no_sleep, monkeypatch):
        script(client, monkeypatch, [ok_result(tokens=100), ok_result(tokens=200)])
        budget = TaskBudget(max_api_attempts=10, max_known_tokens=10**9)

        client.invoke([{"role": "user", "content": "a"}], budget=budget)
        client.invoke([{"role": "user", "content": "b"}], budget=budget)

        assert budget.known_total_tokens == 300

    def test_format_retry_tokens_accumulate(self, client, no_sleep, monkeypatch):
        # 第一次坏 JSON（成功返回、有 usage），第二次合法
        script(
            client,
            monkeypatch,
            [ok_result(content="坏JSON", tokens=50), ok_result(VALID, tokens=70)],
        )
        budget = TaskBudget(max_api_attempts=10, max_known_tokens=10**9)

        client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis, budget=budget
        )

        assert budget.known_total_tokens == 120, "格式重试的两次用量都要累计"
        assert budget.api_attempts == 2


# ============================================================ 验收 5：Token 阈值
class TestTokenThreshold:
    def test_stops_when_token_threshold_reached(self, client, no_sleep, monkeypatch):
        calls = script(client, monkeypatch, [ok_result(tokens=600)])
        budget = TaskBudget(max_api_attempts=10, max_known_tokens=500)

        # 第一次：请求前 0 < 500，允许；返回后 600 >= 500
        client.invoke([{"role": "user", "content": "a"}], budget=budget)
        assert budget.known_total_tokens == 600

        with pytest.raises(BudgetExceeded) as exc:
            client.invoke([{"role": "user", "content": "b"}], budget=budget)
        assert exc.value.reason == "token_budget_exhausted"
        assert calls["count"] == 1


# ============================================================ 验收 6：未知用量
class TestUnknownUsage:
    def test_network_failure_marks_incomplete(self, client, no_sleep, monkeypatch):
        script(client, monkeypatch, [status_error(503), ok_result(tokens=100)])
        budget = TaskBudget(max_api_attempts=5, max_known_tokens=10**9)

        client.invoke([{"role": "user", "content": "hi"}], budget=budget)

        assert budget.usage_incomplete is True
        assert budget.known_total_tokens == 100
        assert budget.api_attempts == 2

    def test_missing_usage_marks_incomplete(self, client, no_sleep, monkeypatch):
        script(client, monkeypatch, [ok_result(tokens=None)])
        budget = TaskBudget()

        client.invoke([{"role": "user", "content": "hi"}], budget=budget)

        assert budget.usage_incomplete is True
        assert budget.known_total_tokens == 0

    def test_failed_request_token_unknown_but_counted(self, client, no_sleep, monkeypatch):
        # 全部失败：api_attempts 仍要计数，usage 标记不完整
        script(client, monkeypatch, [status_error(500)] * 5)
        budget = TaskBudget(max_api_attempts=2, max_known_tokens=10**9)

        with pytest.raises(BudgetExceeded):
            client.invoke([{"role": "user", "content": "hi"}], budget=budget)

        assert budget.api_attempts == 2
        assert budget.usage_incomplete is True


# ============================================================ 验收 7：安全退出
class TestSafeExit:
    def test_agent_stops_with_reason_when_budget_exhausted(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        executed = []

        class FakeLLM:
            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                budget.reserve_api_attempt()
                budget.record_usage(100)
                return {
                    "content": None,
                    "tool_calls": [{
                        "id": f"c{budget.api_attempts}", "type": "function",
                        "function": {"name": "search_policy",
                                     "arguments": '{"region":"安徽省","keyword":"电价"}'},
                    }],
                    "finish_reason": "tool_calls",
                }

        agent = Agent(
            llm=FakeLLM(), registry=registry, system_prompt="提示",
            max_rounds=10, max_tool_calls=10, max_api_attempts=1,
        )

        # 打补丁记录工具是否被执行
        original_execute = agent.executor.execute

        def spy_execute(*args, **kwargs):
            executed.append(args)
            return original_execute(*args, **kwargs)

        agent.executor.execute = spy_execute

        result = agent.run("查询")

        assert result["status"] == "api_budget_exhausted"
        assert result["rounds"] == 1
        assert executed == [], "没有后续预算时不能执行无法反馈的工具"
        assert result["budget"]["api_attempts"] == 1

    def test_final_answer_still_allowed_at_budget_edge(self):
        """预算刚好用完但模型已给出最终回答，应正常 completed。"""
        registry = ToolRegistry()
        registry.register(policy_search_tool)

        class FakeLLM:
            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                budget.reserve_api_attempt()
                budget.record_usage(100)
                return {"content": "最终回答", "tool_calls": [], "finish_reason": "stop"}

        agent = Agent(
            llm=FakeLLM(), registry=registry, system_prompt="提示",
            max_api_attempts=1,
        )
        result = agent.run("查询")

        assert result["status"] == "completed"
        assert result["content"] == "最终回答"


# ============================================================ 验收 8：任务隔离
class TestTaskIsolation:
    def test_two_runs_have_independent_budgets(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        budgets = []

        class FakeLLM:
            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                budgets.append(budget)
                budget.reserve_api_attempt()
                budget.record_usage(100)
                return {"content": "完成", "tool_calls": [], "finish_reason": "stop"}

        agent = Agent(
            llm=FakeLLM(), registry=registry, system_prompt="提示",
            max_api_attempts=1,
        )

        first = agent.run("任务一")
        second = agent.run("任务二")

        assert len(budgets) == 2
        assert budgets[0] is not budgets[1], "每次 run 必须新建预算"
        assert first["budget"]["api_attempts"] == 1
        assert second["budget"]["api_attempts"] == 1
        # 两个任务都成功，说明第二次没有被第一次的消耗拖累
        assert first["status"] == "completed"
        assert second["status"] == "completed"

    def test_second_task_would_fail_if_budget_shared(self):
        """反证：预算共享的话第二个任务会立刻耗尽。"""
        registry = ToolRegistry()
        registry.register(policy_search_tool)

        class FakeLLM:
            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                budget.reserve_api_attempt()
                return {"content": "完成", "tool_calls": [], "finish_reason": "stop"}

        agent = Agent(
            llm=FakeLLM(), registry=registry, system_prompt="提示",
            max_api_attempts=1,
        )

        assert agent.run("一")["status"] == "completed"
        assert agent.run("二")["status"] == "completed"


# ============================================================ 附加：snapshot 出现在结果里
class TestSnapshotInResult:
    def test_result_contains_budget_snapshot(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)

        class FakeLLM:
            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                budget.reserve_api_attempt()
                budget.record_usage(150)
                return {"content": "完成", "tool_calls": [], "finish_reason": "stop"}

        agent = Agent(
            llm=FakeLLM(), registry=registry, system_prompt="提示",
            max_api_attempts=4, max_known_tokens=1000,
        )
        result = agent.run("查询")

        snap = result["budget"]
        assert snap["api_attempts"] == 1
        assert snap["max_api_attempts"] == 4
        assert snap["remaining_api_attempts"] == 3
        assert snap["known_total_tokens"] == 150
        assert snap["max_known_tokens"] == 1000
        assert snap["usage_incomplete"] is False
