"""Execution Trace 验收测试（全 Mock，不联网）。"""

import json
import os
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from openai import APIStatusError
from pydantic import BaseModel, Field

from powercopy.core import trace as trace_module
from powercopy.core.agent import Agent
from powercopy.core.budget import TaskBudget
from powercopy.core.llm import LLMClient
from powercopy.core.trace import (
    ExecutionTrace,
    get_current_trace,
    trace_task,
    trace_tool,
)
from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy import policy_search_tool
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.registry import ToolRegistry

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")


def status_error(code):
    return APIStatusError(
        message="mock", response=httpx.Response(code, request=REQUEST), body=None
    )


def text_response(content="完成", reason="stop"):
    return {"content": content, "tool_calls": [], "finish_reason": reason}


def tool_response(calls, reason="tool_calls"):
    return {"content": None, "tool_calls": calls, "finish_reason": reason}


def tool_call(call_id="c1", name="search_policy", arguments=None):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments
            if isinstance(arguments, str)
            else json.dumps(
                arguments or {"region": "安徽省", "keyword": "电价"},
                ensure_ascii=False,
            ),
        },
    }


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def invoke(self, messages=None, model=None, *, tools=None, budget=None):
        self.calls += 1
        if budget is not None:
            budget.reserve_api_attempt()
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if budget is not None:
            budget.record_usage(response.get("total_tokens", 100))
        return response


def real_client_with(monkeypatch, scripted):
    """构造真实 LLMClient，但把底层 create 换成脚本化响应。

    这样 _invoke_once 上的 @trace_model_attempt 装饰器会真实生效。
    """
    monkeypatch.setattr("powercopy.core.llm.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("LLM_TIMEOUT", "60")

    client = LLMClient()
    state = {"i": 0}

    def fake_create(**kwargs):
        index = state["i"]
        state["i"] += 1
        behavior = scripted[min(index, len(scripted) - 1)]
        if isinstance(behavior, Exception):
            raise behavior

        content = behavior.get("content")
        calls = behavior.get("tool_calls")
        reason = behavior.get("finish_reason", "stop")
        tokens = behavior.get("total_tokens", 15)

        tool_calls = None
        if calls:
            tool_calls = [
                SimpleNamespace(
                    id=c["id"],
                    type=c["type"],
                    function=SimpleNamespace(
                        name=c["function"]["name"],
                        arguments=c["function"]["arguments"],
                    ),
                )
                for c in calls
            ]

        message = SimpleNamespace(content=content, tool_calls=tool_calls)
        choice = SimpleNamespace(message=message, finish_reason=reason)
        usage = SimpleNamespace(
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - tokens // 2,
            total_tokens=tokens,
        )
        return SimpleNamespace(
            choices=[choice], usage=usage, model="glm-5.3-flash"
        )

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    return client, state


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(policy_search_tool)
    return reg


def make_agent(registry, responses, **kwargs):
    llm = FakeLLM(responses)
    return Agent(llm=llm, registry=registry, system_prompt="提示", **kwargs), llm


def event_types(trace):
    return [e["event_type"] for e in trace["events"]]


# ---------------------------------------------------------------- 1
class TestTaskId:
    def test_unique_task_id(self, registry):
        agent, _ = make_agent(registry, [text_response()])
        first = agent.run("任务")

        agent2, _ = make_agent(registry, [text_response()])
        second = agent2.run("任务")

        assert first["task_id"]
        assert len(first["task_id"]) == 32
        assert first["task_id"] != second["task_id"]

    def test_trace_contains_task_id(self, registry):
        agent, _ = make_agent(registry, [text_response()])
        result = agent.run("任务")
        assert result["trace"]["task_id"] == result["task_id"]


# ---------------------------------------------------------------- 2
class TestModelAttemptEvents:
    def test_model_attempt_recorded(self, registry, monkeypatch):
        client, _ = real_client_with(
            monkeypatch, [{"content": "完成", "finish_reason": "stop"}]
        )
        agent = Agent(llm=client, registry=registry, system_prompt="提示")
        result = agent.run("任务")

        events = [e for e in result["trace"]["events"]
                  if e["event_type"] == "model_attempt"]
        assert len(events) == 1
        assert events[0]["status"] == "success"
        assert events[0]["duration_ms"] >= 0

    def test_multiple_rounds_recorded(self, registry, monkeypatch):
        client, _ = real_client_with(
            monkeypatch,
            [
                {"content": None, "tool_calls": [tool_call()], "finish_reason": "tool_calls"},
                {"content": "完成", "finish_reason": "stop"},
            ],
        )
        agent = Agent(llm=client, registry=registry, system_prompt="提示")
        result = agent.run("任务")

        events = [e for e in result["trace"]["events"]
                  if e["event_type"] == "model_attempt"]
        assert len(events) == 2
        assert all(e["status"] == "success" for e in events)


# ---------------------------------------------------------------- 3
class TestToolExecutionEvents:
    def test_tool_success_recorded(self, registry):
        agent, _ = make_agent(
            registry,
            [tool_response([tool_call()]), text_response()],
        )
        result = agent.run("任务")

        events = [e for e in result["trace"]["events"]
                  if e["event_type"] == "tool_execution"]
        assert len(events) == 1
        assert events[0]["tool_name"] == "search_policy"
        assert events[0]["status"] == "success"
        assert events[0]["error_type"] is None


# ---------------------------------------------------------------- 4
class TestStructuredFailure:
    def test_invalid_arguments_marks_failed(self, registry):
        """工具返回 success=False（不抛异常）也必须记为 failed。"""
        agent, _ = make_agent(
            registry,
            [
                tool_response([tool_call(arguments={"region": "", "keyword": ""})]),
                text_response(),
            ],
        )
        result = agent.run("任务")

        events = [e for e in result["trace"]["events"]
                  if e["event_type"] == "tool_execution"]
        assert len(events) == 1
        assert events[0]["status"] == "failed"
        assert events[0]["error_type"] == "INVALID_ARGUMENTS"

    def test_unknown_tool_marks_failed(self, registry):
        agent, _ = make_agent(
            registry,
            [tool_response([tool_call(name="ghost")]), text_response()],
        )
        result = agent.run("任务")

        events = [e for e in result["trace"]["events"]
                  if e["event_type"] == "tool_execution"]
        assert events[0]["status"] == "failed"
        assert events[0]["error_type"] == "TOOL_NOT_FOUND"

    def test_executor_without_trace_still_works(self, registry):
        """无 Trace 上下文时，装饰器必须直接透传。"""
        executor = ToolExecutor(registry)
        result = executor.execute("search_policy", {"region": "安徽省", "keyword": "电价"})
        assert result.success is True


# ---------------------------------------------------------------- 5
class TestModelFailureEvents:
    def test_model_exception_recorded(self, registry):
        class BoomLLM:
            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                raise RuntimeError("模型炸了")

        agent = Agent(llm=BoomLLM(), registry=registry, system_prompt="提示")

        with pytest.raises(RuntimeError):
            agent.run("任务")

    def test_retry_events_via_real_client(self, monkeypatch):
        """真实 LLMClient：第一次 503、第二次成功，两次请求都应被记录。"""
        client, _ = real_client_with(
            monkeypatch,
            [
                status_error(503),
                {"content": "ok", "finish_reason": "stop", "total_tokens": 10},
            ],
        )
        monkeypatch.setattr("powercopy.core.llm.time.sleep", lambda s: None)

        trace = ExecutionTrace()
        token = trace_module._current_trace.set(trace)
        try:
            result = client.invoke([{"role": "user", "content": "hi"}])
        finally:
            trace_module._current_trace.reset(token)

        assert result["content"] == "ok"

        attempts = [e for e in trace.events if e["event_type"] == "model_attempt"]
        assert len(attempts) == 2, "失败一次 + 成功一次都要记录"
        assert attempts[0]["status"] == "failed"
        assert attempts[0]["error_type"] == "APIStatusError"
        assert attempts[1]["status"] == "success"


# ---------------------------------------------------------------- 6
class TestStatusAndBudget:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("completed", "success"),
            ("max_rounds", "stopped"),
            ("tool_budget_exhausted", "stopped"),
            ("api_budget_exhausted", "stopped"),
            ("token_budget_exhausted", "stopped"),
            ("invalid_response", "failed"),
            ("incomplete_response", "failed"),
        ],
    )
    def test_status_mapping(self, status, expected):
        @trace_task
        def fake_run():
            return {"status": status, "content": None, "rounds": 1,
                    "tool_requests": 0, "budget": {}}

        result = fake_run()
        assert result["trace"]["status"] == expected
        assert result["trace"]["stop_reason"] == status

    def test_budget_snapshot_recorded(self, registry):
        agent, _ = make_agent(
            registry, [text_response()], max_api_attempts=5, max_known_tokens=1000
        )
        result = agent.run("任务")

        budget = result["trace"]["budget"]
        assert budget["max_api_attempts"] == 5
        assert budget["max_known_tokens"] == 1000
        assert budget["api_attempts"] == 1


# ---------------------------------------------------------------- 7
class TestDurations:
    def test_duration_recorded(self, registry):
        agent, _ = make_agent(registry, [text_response()])
        result = agent.run("任务")

        assert isinstance(result["trace"]["duration_ms"], float)
        assert result["trace"]["duration_ms"] >= 0

    def test_event_offsets_monotonic(self, registry):
        agent, _ = make_agent(
            registry, [tool_response([tool_call()]), text_response()]
        )
        result = agent.run("任务")

        offsets = [e["offset_ms"] for e in result["trace"]["events"]]
        assert offsets == sorted(offsets), "事件时间偏移必须单调递增"

    def test_per_attempt_duration(self, registry, monkeypatch):
        client, _ = real_client_with(
            monkeypatch, [{"content": "完成", "finish_reason": "stop"}]
        )
        agent = Agent(llm=client, registry=registry, system_prompt="提示")
        result = agent.run("任务")

        attempt = next(
            e for e in result["trace"]["events"]
            if e["event_type"] == "model_attempt"
        )
        assert isinstance(attempt["duration_ms"], float)


# ---------------------------------------------------------------- 8
class TestTraceIsolation:
    def test_two_runs_have_different_task_ids(self, registry):
        agent, _ = make_agent(registry, [text_response()])
        first = agent.run("任务一")

        agent2, _ = make_agent(registry, [text_response()])
        second = agent2.run("任务二")

        assert first["trace"]["task_id"] != second["trace"]["task_id"]
        assert first["trace"]["events"] != second["trace"]["events"]

    def test_context_cleared_after_run(self, registry):
        agent, _ = make_agent(registry, [text_response()])
        agent.run("任务")
        assert get_current_trace() is None, "任务结束后必须清理上下文"

    def test_nested_trace_isolation(self):
        """内层任务结束后，外层 Trace 应恢复。"""
        outer_holder = {}

        @trace_task
        def inner():
            return {"status": "completed", "content": "in", "rounds": 1,
                    "tool_requests": 0, "budget": {}}

        @trace_task
        def outer():
            before = get_current_trace()
            inner_result = inner()
            after = get_current_trace()
            outer_holder["before"] = before
            outer_holder["after"] = after
            outer_holder["inner_result"] = inner_result
            return {"status": "completed", "content": "out", "rounds": 1,
                    "tool_requests": 0, "budget": {}}

        result = outer()

        assert outer_holder["before"] is not None
        assert outer_holder["after"] is outer_holder["before"], "外层上下文必须恢复"
        assert outer_holder["inner_result"]["task_id"] != result["task_id"]


# ---------------------------------------------------------------- 9
class TestExceptionCleanup:
    def test_exception_keeps_trace_and_clears_context(self, registry):
        class BoomLLM:
            def invoke(self, messages=None, model=None, *, tools=None, budget=None):
                raise RuntimeError("boom")

        agent = Agent(llm=BoomLLM(), registry=registry, system_prompt="提示")

        with pytest.raises(RuntimeError):
            agent.run("任务")

        assert get_current_trace() is None, "异常后也必须清理上下文"

    def test_trace_recorded_on_exception(self, caplog):
        """异常路径应打日志，且状态为 failed。"""
        import logging

        captured = {}

        @trace_task
        def broken():
            trace = get_current_trace()
            captured["trace"] = trace
            raise ValueError("炸了")

        with caplog.at_level(logging.ERROR, logger="powercopy.core.trace"):
            with pytest.raises(ValueError):
                broken()

        assert "agent_task_failed" in caplog.text
        assert captured["trace"].status == "failed"
        assert captured["trace"].stop_reason == "ValueError"
        assert any(
            e["event_type"] == "task_error" for e in captured["trace"].events
        )


# ---------------------------------------------------------------- 10
class TestRuntimeWiring:
    def test_full_mock_runtime(self, registry):
        """真实 Agent + 真实 LLMClient + Mock 底层请求，全链路接线验证。"""
        import powercopy.core.llm as llm_module

        llm_module.load_dotenv = lambda *a, **k: None

        os.environ["LLM_API_KEY"] = "sk-test"
        os.environ["LLM_BASE_URL"] = "https://example.com/v1"
        os.environ["LLM_MODEL"] = "glm-5.3-flash"
        os.environ["LLM_TIMEOUT"] = "60"

        client = LLMClient()
        state = {"round": 0}

        def fake_create(**kwargs):
            state["round"] += 1

            if state["round"] == 1:
                message = SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(
                                name="search_policy",
                                arguments='{"region":"安徽省","keyword":"电价"}',
                            ),
                        )
                    ],
                )
                choice = SimpleNamespace(message=message, finish_reason="tool_calls")
            else:
                message = SimpleNamespace(content="最终回答", tool_calls=None)
                choice = SimpleNamespace(message=message, finish_reason="stop")

            return SimpleNamespace(
                choices=[choice],
                usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5, total_tokens=15
                ),
                model="glm-5.3-flash",
            )

        client.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
        )

        agent = Agent(
            llm=client, registry=registry, system_prompt="提示",
            max_api_attempts=4,
        )
        result = agent.run("查询安徽电价")

        assert result["status"] == "completed"
        assert result["content"] == "最终回答"
        assert result["task_id"]

        trace = result["trace"]
        assert trace["status"] == "success"
        event_types = [e["event_type"] for e in trace["events"]]

        assert "task_started" in event_types
        assert "round_started" in event_types
        assert event_types.count("model_attempt") == 2
        assert event_types.count("tool_execution") == 1
        assert "task_completed" in event_types
        assert "task_finished" in event_types

        # 工具事件字段
        tool_event = next(e for e in trace["events"]
                          if e["event_type"] == "tool_execution")
        assert tool_event["tool_name"] == "search_policy"
        assert tool_event["status"] == "success"

        # 预算快照
        assert trace["budget"]["api_attempts"] == 2
        assert trace["budget"]["known_total_tokens"] == 30

    def test_trace_json_serializable(self, registry):
        agent, _ = make_agent(
            registry, [tool_response([tool_call()]), text_response()]
        )
        result = agent.run("任务")
        text = json.dumps(result["trace"], ensure_ascii=False)
        assert "task_id" in text
