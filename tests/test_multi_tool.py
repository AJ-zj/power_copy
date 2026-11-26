"""多工具调用与错误反馈测试（Mock LLM，不联网）。"""

import json

import pytest
from pydantic import BaseModel, Field

from powercopy.core.agent import Agent
from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy import (
    get_policy_detail,
    policy_detail_tool,
    policy_search_tool,
)
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.registry import ToolRegistry


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke(self, messages=None, model=None, *, tools=None, budget=None):
        self.requests.append({"messages": messages, "tools": tools, "budget": budget})
        if budget is not None:
            budget.reserve_api_attempt()
        if not self.responses:
            raise AssertionError("FakeLLM 响应已耗尽")
        response = self.responses.pop(0)
        if budget is not None:
            budget.record_usage(response.get("total_tokens", 100))
        return response


def tool_call(call_id, name, arguments=None, arguments_raw=None):
    payload = arguments_raw if arguments_raw is not None else json.dumps(
        arguments or {}, ensure_ascii=False
    )
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": payload},
    }


def tool_response(calls, content=None, reason="tool_calls"):
    return {"content": content, "tool_calls": calls, "finish_reason": reason}


def text_response(content="完成", reason="stop"):
    return {"content": content, "tool_calls": [], "finish_reason": reason}


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(policy_search_tool)
    reg.register(policy_detail_tool)
    return reg


def make_agent(registry, responses, **kwargs):
    llm = FakeLLM(responses)
    agent = Agent(
        llm=llm, registry=registry, system_prompt="你是售电政策助手。", **kwargs
    )
    return agent, llm


# ---------------------------------------------------------------- 验收 1
class TestSecondToolRegistered:
    def test_detail_tool_exists(self):
        assert policy_detail_tool.name == "get_policy_detail"
        assert "不要自行编造" in policy_detail_tool.description

    def test_detail_tool_schema(self):
        schema = policy_detail_tool.to_openai_schema()
        params = schema["function"]["parameters"]
        assert params["required"] == ["policy_id"]
        assert params["additionalProperties"] is False

    def test_both_tools_registered(self, registry):
        assert registry.list_tools() == ["search_policy", "get_policy_detail"]
        schemas = registry.get_schemas()
        assert len(schemas) == 2
        assert {s["function"]["name"] for s in schemas} == {
            "search_policy",
            "get_policy_detail",
        }

    def test_detail_handler_hits_mock(self):
        found = get_policy_detail("mock-001")
        assert found["found"] is True
        assert found["document"]["mock"] is True

    def test_detail_handler_misses_unknown_id(self):
        missing = get_policy_detail("ghost-999")
        assert missing["found"] is False
        assert missing["document"] is None

    def test_detail_tool_rejects_bad_arguments(self):
        reg = ToolRegistry()
        reg.register(policy_detail_tool)
        executor = ToolExecutor(reg)
        result = executor.execute("get_policy_detail", {"policy_id": ""})
        assert result.success is False
        assert result.error_type == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------- 验收 2
class TestSequentialToolCalls:
    def test_search_then_detail(self, registry):
        agent, llm = make_agent(
            registry,
            [
                # 第一轮：检索
                tool_response([
                    tool_call(
                        "call_search",
                        "search_policy",
                        {"region": "安徽省", "keyword": "峰谷分时电价"},
                    )
                ]),
                # 第二轮：用检索到的 ID 查详情
                tool_response([
                    tool_call(
                        "call_detail",
                        "get_policy_detail",
                        {"policy_id": "mock-001"},
                    )
                ]),
                # 第三轮：收尾
                text_response("已检索并查到详情，数据为模拟材料。"),
            ],
        )

        result = agent.run("查询安徽峰谷分时电价并查看详情")

        assert result["status"] == "completed"
        assert result["rounds"] == 3
        assert result["tool_requests"] == 2

        # 第二轮请求里应该能看到第一轮检索结果（含 mock-001）
        second_messages = llm.requests[1]["messages"]
        roles = [m["role"] for m in second_messages]
        assert roles == ["system", "user", "assistant", "tool"]
        first_tool_payload = json.loads(second_messages[-1]["content"])
        assert first_tool_payload["data"]["documents"][0]["id"] == "mock-001"

        # 第三轮请求里应同时有两次工具结果
        third_messages = llm.requests[2]["messages"]
        tool_messages = [m for m in third_messages if m["role"] == "tool"]
        assert len(tool_messages) == 2
        assert tool_messages[0]["tool_call_id"] == "call_search"
        assert tool_messages[1]["tool_call_id"] == "call_detail"
        detail_payload = json.loads(tool_messages[1]["content"])
        assert detail_payload["data"]["found"] is True

    def test_call_order_preserved(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", "search_policy", {"region": "安徽省", "keyword": "电价"})]),
                tool_response([tool_call("c2", "get_policy_detail", {"policy_id": "mock-001"})]),
                text_response("完成"),
            ],
        )
        agent.run("查询")

        assistant_messages = [
            m for m in llm.requests[2]["messages"] if m.get("tool_calls")
        ]
        assert assistant_messages[0]["tool_calls"][0]["function"]["name"] == "search_policy"
        assert assistant_messages[1]["tool_calls"][0]["function"]["name"] == "get_policy_detail"


# ---------------------------------------------------------------- 验收 3
class TestMultipleCallsInOneRound:
    def test_two_tools_same_round(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([
                    tool_call("c1", "search_policy", {"region": "安徽省", "keyword": "峰谷"}),
                    tool_call("c2", "get_policy_detail", {"policy_id": "mock-001"}),
                ]),
                text_response("两个工具都执行完成。"),
            ],
        )

        result = agent.run("同时检索和查详情")

        assert result["status"] == "completed"
        assert result["tool_requests"] == 2

        second_messages = llm.requests[1]["messages"]
        tool_messages = [m for m in second_messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["c1", "c2"]

        # 两个结果内容各自正确
        first_payload = json.loads(tool_messages[0]["content"])
        second_payload = json.loads(tool_messages[1]["content"])
        assert first_payload["tool_name"] == "search_policy"
        assert second_payload["tool_name"] == "get_policy_detail"
        assert second_payload["data"]["found"] is True

    def test_same_tool_twice_in_one_round(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([
                    tool_call("c1", "search_policy", {"region": "安徽省", "keyword": "峰谷"}),
                    tool_call("c2", "search_policy", {"region": "安徽省", "keyword": "分时"}),
                ]),
                text_response("完成"),
            ],
        )

        result = agent.run("查两个关键词")
        assert result["tool_requests"] == 2

        tool_messages = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"]
        assert len(tool_messages) == 2
        keywords = [json.loads(m["content"])["data"]["keyword"] for m in tool_messages]
        assert keywords == ["峰谷", "分时"]


# ---------------------------------------------------------------- 验收 4
class TestFailureFeedback:
    def test_failed_tool_result_in_next_context(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", "ghost_tool", {})]),
                text_response("没有这个工具，我换个方式回答。"),
            ],
        )

        result = agent.run("查询")

        assert result["status"] == "completed"
        payload = json.loads(llm.requests[1]["messages"][-1]["content"])
        assert payload["success"] is False
        assert payload["error_type"] == "TOOL_NOT_FOUND"

    def test_invalid_arguments_feedback(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", "get_policy_detail", {"policy_id": ""})]),
                text_response("参数不合法。"),
            ],
        )

        result = agent.run("查详情")
        payload = json.loads(llm.requests[1]["messages"][-1]["content"])
        assert payload["error_type"] == "INVALID_ARGUMENTS"
        assert result["status"] == "completed"

    def test_detail_not_found_is_success_with_found_false(self, registry):
        """业务级未命中（found=False）不是执行失败，应正常回填。"""
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", "get_policy_detail", {"policy_id": "ghost"})]),
                text_response("没找到该政策。"),
            ],
        )

        result = agent.run("查一个不存在的 ID")

        payload = json.loads(llm.requests[1]["messages"][-1]["content"])
        assert payload["success"] is True
        assert payload["data"]["found"] is False
        assert result["status"] == "completed"


# ---------------------------------------------------------------- 验收 5
class TestErrorThenCorrect:
    def test_bad_args_then_model_fixes(self, registry):
        """第一轮参数错误，模型看到错误后第二轮修正并成功。"""
        agent, llm = make_agent(
            registry,
            [
                # 第一轮：空 policy_id -> INVALID_ARGUMENTS
                tool_response([tool_call("c1", "get_policy_detail", {"policy_id": ""})]),
                # 第二轮：模型纠正为 mock-001
                tool_response([tool_call("c2", "get_policy_detail", {"policy_id": "mock-001"})]),
                text_response("已修正参数并成功取得详情。"),
            ],
        )

        result = agent.run("查政策详情")

        assert result["status"] == "completed"
        assert result["tool_requests"] == 2

        # 第二轮请求里模型能看到第一轮的失败信息
        second_tool_msg = [
            m for m in llm.requests[1]["messages"] if m["role"] == "tool"
        ][-1]
        assert json.loads(second_tool_msg["content"])["error_type"] == "INVALID_ARGUMENTS"

        # 第三轮请求里能看到最终成功的结果
        third_tools = [m for m in llm.requests[2]["messages"] if m["role"] == "tool"]
        assert len(third_tools) == 2
        assert json.loads(third_tools[0]["content"])["success"] is False
        assert json.loads(third_tools[1]["content"])["data"]["found"] is True

    def test_unknown_tool_then_correct_tool(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", "search_policy_v2", {"region": "安徽省", "keyword": "电价"})]),
                tool_response([tool_call("c2", "search_policy", {"region": "安徽省", "keyword": "电价"})]),
                text_response("已改用正确的工具。"),
            ],
        )

        result = agent.run("查询")

        assert result["status"] == "completed"
        assert result["tool_requests"] == 2
        tools_msgs = [m for m in llm.requests[2]["messages"] if m["role"] == "tool"]
        assert json.loads(tools_msgs[0]["content"])["error_type"] == "TOOL_NOT_FOUND"
        assert json.loads(tools_msgs[1]["content"])["success"] is True


# ---------------------------------------------------------------- 验收 6
class TestBatchBudget:
    def test_batch_exceeding_remaining_budget_stops(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", "search_policy", {"region": "安徽省", "keyword": "a"})]),
                # 第二轮请求 2 个，但剩余预算只剩 1
                tool_response([
                    tool_call("c2", "search_policy", {"region": "安徽省", "keyword": "b"}),
                    tool_call("c3", "search_policy", {"region": "安徽省", "keyword": "c"}),
                ]),
            ],
            max_tool_calls=2,
        )

        result = agent.run("查询")

        assert result["status"] == "tool_budget_exhausted"
        assert result["tool_requests"] == 1
        # 第二轮整批都不执行，也不回填
        assert len(llm.requests) == 2

    def test_batch_exactly_fits_budget(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([
                    tool_call("c1", "search_policy", {"region": "安徽省", "keyword": "a"}),
                    tool_call("c2", "get_policy_detail", {"policy_id": "mock-001"}),
                ]),
                text_response("完成"),
            ],
            max_tool_calls=2,
        )

        result = agent.run("查询")
        assert result["status"] == "completed"
        assert result["tool_requests"] == 2

    def test_batch_rejected_before_any_execution(self, registry):
        """超预算时必须整批拒绝，不能执行一部分。"""
        calls = []

        class Arg(BaseModel):
            text: str = Field(min_length=1)

        def spy(text: str):
            calls.append(text)
            return {"ok": text}

        reg = ToolRegistry()
        reg.register(Tool(name="spy", description="spy", args_schema=Arg, handler=spy))

        agent, _ = make_agent(
            reg,
            [
                tool_response([
                    tool_call("c1", "spy", {"text": "a"}),
                    tool_call("c2", "spy", {"text": "b"}),
                ]),
            ],
            max_tool_calls=1,
        )

        result = agent.run("查询")

        assert result["status"] == "tool_budget_exhausted"
        assert calls == [], "整批超预算时不应执行任何工具"


# ---------------------------------------------------------------- 附加
class TestToolsListedEveryRound:
    def test_both_tool_schemas_sent_every_round(self, registry):
        agent, llm = make_agent(
            registry,
            [
                tool_response([tool_call("c1", "search_policy", {"region": "安徽省", "keyword": "a"})]),
                text_response("完成"),
            ],
        )
        agent.run("查询")

        for request in llm.requests:
            names = {s["function"]["name"] for s in request["tools"]}
            assert names == {"search_policy", "get_policy_detail"}
