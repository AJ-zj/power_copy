"""Agent Loop 测试（Mock LLM，不联网）。"""

import json

import pytest

from powercopy.core.agent import Agent
from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy import policy_search_tool
from powercopy.tools.registry import ToolRegistry

from pydantic import BaseModel, Field


class NoteInput(BaseModel):
    text: str = Field(min_length=1, description="要保存的文本")


class FakeLLM:
    """按顺序返回预设响应，并记录每次请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke(self, messages=None, model=None, *, tools=None, budget=None):
        self.requests.append({"messages": messages, "tools": tools, "budget": budget})
        if budget is not None:
            # 模拟真实客户端：请求前占用一次预算，返回后累计 token
            budget.reserve_api_attempt()
        if not self.responses:
            raise AssertionError("FakeLLM 响应已耗尽，可能发生了多余的模型调用")
        response = self.responses.pop(0)
        # 支持可调用，便于断言请求内容后再构造响应
        if callable(response):
            response = response(messages)
        if budget is not None:
            budget.record_usage(response.get("total_tokens", 100))
        return response


def tool_call(call_id, name="search_policy", arguments=None, call_type="function"):
    return {
        "id": call_id,
        "type": call_type,
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments if arguments is not None else {"region": "安徽省", "keyword": "峰谷电价"},
                ensure_ascii=False,
            ),
        },
    }


def text_response(content="好的", reason="stop"):
    return {"content": content, "tool_calls": [], "finish_reason": reason}


def tool_response(calls, content=None, reason="tool_calls"):
    return {"content": content, "tool_calls": calls, "finish_reason": reason}


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(policy_search_tool)
    return reg


def make_agent(registry, responses, **kwargs):
    llm = FakeLLM(responses)
    agent = Agent(
        llm=llm,
        registry=registry,
        system_prompt="你是售电政策助手。",
        **kwargs,
    )
    return agent, llm


# ---------------------------------------------------------------- 验收 1
class TestAgentCreation:
    def test_injects_llm_and_registry(self, registry):
        llm = FakeLLM([])
        agent = Agent(llm=llm, registry=registry, system_prompt="提示")

        assert agent.llm is llm
        assert agent.registry is registry
        assert agent.system_prompt == "提示"
        assert agent.max_rounds == 5
        assert agent.max_tool_calls == 8

    def test_creates_executor_with_same_registry(self, registry):
        agent = Agent(llm=FakeLLM([]), registry=registry, system_prompt="提示")
        assert agent.executor.registry is registry

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_rounds": 0},
            {"max_rounds": -1},
            {"max_tool_calls": 0},
            {"max_tool_calls": -5},
        ],
    )
    def test_rejects_non_positive_budget(self, registry, kwargs):
        with pytest.raises(ValueError, match="运行预算必须大于零"):
            Agent(llm=FakeLLM([]), registry=registry, system_prompt="提示", **kwargs)

    def test_rejects_empty_registry(self):
        agent = Agent(llm=FakeLLM([]), registry=ToolRegistry(), system_prompt="提示")
        with pytest.raises(ValueError, match="没有注册工具"):
            agent.run("任务")


# ---------------------------------------------------------------- 验收 2
class TestDirectAnswer:
    def test_answers_without_tools(self, registry):
        agent, llm = make_agent(registry, [text_response("安徽执行峰谷分时电价。")])

        result = agent.run("安徽峰谷分时电价是什么政策？")

        assert result["status"] == "completed"
        assert result["content"] == "安徽执行峰谷分时电价。"
        assert result["rounds"] == 1
        assert result["tool_requests"] == 0
        assert len(llm.requests) == 1

    def test_tools_passed_every_round(self, registry):
        agent, llm = make_agent(registry, [text_response("直接回答")])
        agent.run("任务")

        sent = llm.requests[0]
        assert sent["tools"]
        assert sent["tools"][0]["function"]["name"] == "search_policy"


# ---------------------------------------------------------------- 验收 3
class TestMultiRoundToolCalls:
    def test_two_rounds_of_tool_calls(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("call_1", arguments={"region": "安徽省", "keyword": "峰谷电价"})]),
                tool_response([tool_call("call_2", arguments={"region": "安徽省", "keyword": "分时电价"})]),
                text_response("综合两次检索，安徽执行峰谷分时电价。"),
            ],
        )

        result = agent.run("查询安徽峰谷分时电价")

        assert result["status"] == "completed"
        assert result["rounds"] == 3
        assert result["tool_requests"] == 2
        assert len(llm.requests) == 3

    def test_multiple_calls_in_one_round(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([
                    tool_call("call_a", arguments={"region": "安徽省", "keyword": "峰谷"}),
                    tool_call("call_b", arguments={"region": "安徽省", "keyword": "分时"}),
                ]),
                text_response("完成"),
            ],
        )

        result = agent.run("查询")

        assert result["status"] == "completed"
        assert result["tool_requests"] == 2
        assert len(llm.requests) == 2


# ---------------------------------------------------------------- 验收 4
class TestHistoryFeedback:
    def test_tool_result_in_next_request(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("call_1", arguments={"region": "安徽省", "keyword": "峰谷电价"})]),
                text_response("完成"),
            ],
        )

        agent.run("查询")

        second_messages = llm.requests[1]["messages"]
        roles = [m["role"] for m in second_messages]
        assert roles == ["system", "user", "assistant", "tool"]

        tool_message = second_messages[-1]
        assert tool_message["tool_call_id"] == "call_1"
        payload = json.loads(tool_message["content"])
        assert payload["success"] is True
        assert payload["data"]["mock"] is True

    def test_id_matches_between_request_and_result(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([
                    tool_call("id_one", arguments={"region": "安徽省", "keyword": "a"}),
                    tool_call("id_two", arguments={"region": "安徽省", "keyword": "b"}),
                ]),
                text_response("完成"),
            ],
        )

        agent.run("查询")

        messages = llm.requests[1]["messages"]
        assistant = next(m for m in messages if m.get("tool_calls"))
        tools = [m for m in messages if m["role"] == "tool"]

        assert [c["id"] for c in assistant["tool_calls"]] == ["id_one", "id_two"]
        assert [m["tool_call_id"] for m in tools] == ["id_one", "id_two"]

    def test_history_accumulates_across_rounds(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", arguments={"region": "安徽省", "keyword": "a"})]),
                tool_response([tool_call("c2", arguments={"region": "安徽省", "keyword": "b"})]),
                text_response("完成"),
            ],
        )

        agent.run("查询")

        # 第 3 次请求应该包含：system, user, assistant, tool, assistant, tool
        roles = [m["role"] for m in llm.requests[2]["messages"]]
        assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]


# ---------------------------------------------------------------- 验收 5
class TestMaxRounds:
    def test_stops_at_last_round_without_executing(self, registry):
        # 每轮都要求调用工具；max_rounds=2，第 2 轮不应执行工具
        responses = [
            tool_response([tool_call(f"c{i}", arguments={"region": "安徽省", "keyword": str(i)})])
            for i in range(1, 4)
        ]
        agent, llm = make_agent(registry, responses, max_rounds=2)

        result = agent.run("查询")

        assert result["status"] == "max_rounds"
        assert result["rounds"] == 2
        # 只有第 1 轮真正执行了工具
        assert result["tool_requests"] == 1
        assert len(llm.requests) == 2

    def test_max_rounds_one_never_executes_tools(self, registry):
        agent, llm = make_agent(
            registry,
            [tool_response([tool_call("c1")])],
            max_rounds=1,
        )

        result = agent.run("查询")

        assert result["status"] == "max_rounds"
        assert result["rounds"] == 1
        assert result["tool_requests"] == 0
        assert len(llm.requests) == 1


# ---------------------------------------------------------------- 验收 6
class TestToolBudget:
    def test_exhausted_when_batch_exceeds_budget(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([
                    tool_call("c1", arguments={"region": "安徽省", "keyword": "a"}),
                    tool_call("c2", arguments={"region": "安徽省", "keyword": "b"}),
                ]),
            ],
            max_tool_calls=1,
        )

        result = agent.run("查询")

        assert result["status"] == "tool_budget_exhausted"
        assert result["tool_requests"] == 0

    def test_exhausted_across_rounds(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", arguments={"region": "安徽省", "keyword": "a"})]),
                tool_response([
                    tool_call("c2", arguments={"region": "安徽省", "keyword": "b"}),
                    tool_call("c3", arguments={"region": "安徽省", "keyword": "c"}),
                ]),
            ],
            max_tool_calls=2,
        )

        result = agent.run("查询")

        assert result["status"] == "tool_budget_exhausted"
        assert result["tool_requests"] == 1

    def test_exact_budget_allowed(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([
                    tool_call("c1", arguments={"region": "安徽省", "keyword": "a"}),
                    tool_call("c2", arguments={"region": "安徽省", "keyword": "b"}),
                ]),
                text_response("完成"),
            ],
            max_tool_calls=2,
        )

        result = agent.run("查询")

        assert result["status"] == "completed"
        assert result["tool_requests"] == 2


# ---------------------------------------------------------------- 协议与异常
class TestProtocolValidation:
    def test_invalid_when_reason_not_tool_calls(self, registry):
        agent, _ = make_agent(
            registry,
            [{"content": None, "tool_calls": [tool_call("c1")], "finish_reason": "stop"}],
        )
        assert agent.run("查询")["status"] == "invalid_response"

    def test_invalid_when_call_type_wrong(self, registry):
        agent, _ = make_agent(
            registry,
            [tool_response([tool_call("c1", call_type="other")])],
        )
        assert agent.run("查询")["status"] == "invalid_response"

    def test_invalid_when_id_missing(self, registry):
        agent, _ = make_agent(registry, [tool_response([tool_call("")])])
        assert agent.run("查询")["status"] == "invalid_response"

    def test_invalid_when_duplicate_ids(self, registry):
        agent, _ = make_agent(
            registry,
            [tool_response([tool_call("dup"), tool_call("dup")])],
        )
        assert agent.run("查询")["status"] == "invalid_response"

    def test_invalid_when_arguments_not_string(self, registry):
        bad = {
            "id": "c1",
            "type": "function",
            "function": {"name": "search_policy", "arguments": {"region": "安徽省"}},
        }
        agent, _ = make_agent(registry, [tool_response([bad])])
        assert agent.run("查询")["status"] == "invalid_response"

    def test_incomplete_when_length(self, registry):
        agent, _ = make_agent(registry, [text_response("半截", reason="length")])
        assert agent.run("查询")["status"] == "incomplete_response"

    def test_incomplete_when_content_filter(self, registry):
        agent, _ = make_agent(registry, [text_response("x", reason="content_filter")])
        assert agent.run("查询")["status"] == "incomplete_response"

    def test_invalid_when_empty_content(self, registry):
        agent, _ = make_agent(registry, [text_response("   ", reason="stop")])
        assert agent.run("查询")["status"] == "invalid_response"

    def test_invalid_when_content_not_string(self, registry):
        agent, _ = make_agent(registry, [{"content": 123, "tool_calls": [], "finish_reason": "stop"}])
        assert agent.run("查询")["status"] == "invalid_response"


class TestToolFailureFeedback:
    def test_failed_tool_result_still_fed_back(self, registry):
        # 关键词为空 -> INVALID_ARGUMENTS，但结果仍要回填给模型
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", arguments={"region": "安徽省", "keyword": ""})]),
                text_response("工具调用失败，请补充关键词。"),
            ],
        )

        result = agent.run("查询")

        assert result["status"] == "completed"
        tool_message = llm.requests[1]["messages"][-1]
        payload = json.loads(tool_message["content"])
        assert payload["success"] is False
        assert payload["error_type"] == "INVALID_ARGUMENTS"

    def test_unknown_tool_result_fed_back(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", name="ghost_tool")]),
                text_response("没有该工具。"),
            ],
        )

        result = agent.run("查询")

        assert result["status"] == "completed"
        payload = json.loads(llm.requests[1]["messages"][-1]["content"])
        assert payload["error_type"] == "TOOL_NOT_FOUND"

    def test_unserializable_result_marked(self, registry):
        class BadArgs(BaseModel):
            text: str = Field(min_length=1)

        def bad_handler(text: str):
            return {"value": float("nan")}  # allow_nan=False 会拒绝

        reg = ToolRegistry()
        reg.register(
            Tool(name="bad", description="返回 NaN", args_schema=BadArgs, handler=bad_handler)
        )

        agent, llm = make_agent(
            reg,
            [
                tool_response([
                    {"id": "c1", "type": "function",
                     "function": {"name": "bad", "arguments": '{"text":"x"}'}}
                ]),
                text_response("工具结果异常。"),
            ],
        )

        result = agent.run("查询")

        payload = json.loads(llm.requests[1]["messages"][-1]["content"])
        assert payload["error_type"] == "SERIALIZATION_ERROR"
        assert result["status"] == "completed"
