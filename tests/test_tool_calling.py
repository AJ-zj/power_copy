"""Function Calling 全链路测试（Mock 模型响应，不联网）。

只验证「tools 传参 → tool_calls 解析 → 执行 → 回填 → 二次调用」这条链路，
真实模型调用由 scripts/check_tool_calling.py 覆盖。
"""

import json
from types import SimpleNamespace

import pytest

from powercopy.core.llm import LLMClient
from powercopy.core.message import MessageManager, Messages
from powercopy.tools.builtin.policy import policy_search_tool
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.registry import ToolRegistry


# --------------------------------------------------------------------------
# 构造假的 OpenAI 响应对象
# --------------------------------------------------------------------------
def make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )


def make_response(content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=20, total_tokens=120
    )
    return SimpleNamespace(
        choices=[choice], usage=usage, model="glm-5.3-flash"
    )


class FakeCompletions:
    """记录每次请求参数，按顺序返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def make_client(responses):
    client = LLMClient()
    completions = FakeCompletions(responses)
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return client, completions


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("powercopy.core.llm.time.sleep", lambda s: None)


# --------------------------------------------------------------------------
# 验收 1：tools 正确传入接口
# --------------------------------------------------------------------------
class TestToolsPassedToApi:
    def test_tools_included_when_provided(self, no_sleep):
        client, completions = make_client([make_response("好的")])
        registry = ToolRegistry()
        registry.register(policy_search_tool)

        client.invoke(
            [{"role": "user", "content": "hi"}],
            tools=registry.get_schemas(),
        )

        sent = completions.requests[0]
        assert "tools" in sent
        assert sent["tools"][0]["function"]["name"] == "search_policy"

    def test_tools_omitted_when_not_provided(self, no_sleep):
        client, completions = make_client([make_response("好的")])

        client.invoke([{"role": "user", "content": "hi"}])

        assert "tools" not in completions.requests[0]

    def test_tools_none_explicitly_omitted(self, no_sleep):
        client, completions = make_client([make_response("好的")])
        client.invoke([{"role": "user", "content": "hi"}], tools=None)
        assert "tools" not in completions.requests[0]

    def test_tools_not_added_to_result_when_absent(self, no_sleep):
        client, _ = make_client([make_response("好的")])
        result = client.invoke([{"role": "user", "content": "hi"}])
        # 不传 tools 时不返回 tool_calls / finish_reason 字段
        assert "tool_calls" not in result
        assert "finish_reason" not in result


# --------------------------------------------------------------------------
# 验收 2：提取 tool_calls，允许 content 为 None
# --------------------------------------------------------------------------
class TestToolCallsExtraction:
    def test_extracts_tool_calls(self, no_sleep):
        client, _ = make_client([
            make_response(
                content=None,
                tool_calls=[make_tool_call("call_1", "search_policy", {"region": "安徽", "keyword": "电价"})],
                finish_reason="tool_calls",
            )
        ])
        result = client.invoke(
            [{"role": "user", "content": "查政策"}],
            tools=[policy_search_tool.to_openai_schema()],
        )

        assert result["content"] is None
        assert len(result["tool_calls"]) == 1
        call = result["tool_calls"][0]
        assert call["id"] == "call_1"
        assert call["type"] == "function"
        assert call["function"]["name"] == "search_policy"
        assert json.loads(call["function"]["arguments"])["region"] == "安徽"

    def test_finish_reason_exposed(self, no_sleep):
        client, _ = make_client([
            make_response(content=None, tool_calls=[make_tool_call("c1", "x", {})], finish_reason="tool_calls")
        ])
        result = client.invoke([{"role": "user", "content": "hi"}], tools=[{}])
        assert result["finish_reason"] == "tool_calls"

    def test_empty_tool_calls_list(self, no_sleep):
        client, _ = make_client([make_response("直接回答")])
        result = client.invoke([{"role": "user", "content": "hi"}], tools=[{}])
        assert result["tool_calls"] == []
        assert result["content"] == "直接回答"


# --------------------------------------------------------------------------
# 验收 3：Registry / Executor 执行模型指定的工具
# --------------------------------------------------------------------------
class TestExecuteRequestedTool:
    def test_executor_runs_named_tool(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        executor = ToolExecutor(registry)

        call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "search_policy",
                "arguments": json.dumps({"region": "安徽省", "keyword": "峰谷电价"}, ensure_ascii=False),
            },
        }
        result = executor.execute(call["function"]["name"], call["function"]["arguments"])

        assert result.success is True
        assert result.data["region"] == "安徽省"
        assert result.data["mock"] is True


# --------------------------------------------------------------------------
# 验收 4：消息管理保存 assistant 工具调用消息
# --------------------------------------------------------------------------
class TestMessageManager:
    def test_add_assistant_tool_calls(self):
        manager = MessageManager("系统提示")
        manager.add_user("查政策")
        manager.add_assistant_tool_calls(
            content=None,
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_policy", "arguments": '{"region":"安徽"}'},
            }],
        )

        messages = manager.get_messages()
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["content"] == ""
        assert messages[-1]["tool_calls"][0]["id"] == "call_1"
        assert messages[-1]["tool_calls"][0]["function"]["name"] == "search_policy"

    def test_tool_calls_deep_copied(self):
        manager = MessageManager("系统提示")
        source = [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "search_policy", "arguments": '{"region":"安徽"}'},
        }]
        manager.add_assistant_tool_calls(content=None, tool_calls=source)

        source[0]["id"] = "被外部改掉"
        assert manager.get_messages()[-1]["tool_calls"][0]["id"] == "call_1"

    def test_add_tool_message(self):
        manager = MessageManager("系统提示")
        manager.add_tool(tool_call_id="call_1", content='{"ok": true}')

        message = manager.get_messages()[-1]
        assert message["role"] == "tool"
        assert message["tool_call_id"] == "call_1"
        assert message["content"] == '{"ok": true}'

    def test_message_manager_is_messages(self):
        assert MessageManager is Messages


# --------------------------------------------------------------------------
# 验收 5：tool_call_id 匹配
# --------------------------------------------------------------------------
class TestToolCallIdMatching:
    def test_ids_match_between_assistant_and_tool(self):
        manager = MessageManager("系统提示")
        manager.add_user("查政策")

        calls = [
            {"id": "call_a", "type": "function",
             "function": {"name": "search_policy", "arguments": '{"region":"安徽"}'}},
            {"id": "call_b", "type": "function",
             "function": {"name": "search_policy", "arguments": '{"region":"安徽"}'}},
        ]
        manager.add_assistant_tool_calls(content=None, tool_calls=calls)
        manager.add_tool(tool_call_id="call_a", content="结果A")
        manager.add_tool(tool_call_id="call_b", content="结果B")

        messages = manager.get_messages()
        assistant_ids = [c["id"] for c in messages[2]["tool_calls"]]
        tool_ids = [m["tool_call_id"] for m in messages[3:]]

        assert assistant_ids == ["call_a", "call_b"]
        assert tool_ids == assistant_ids

    def test_each_tool_result_matches_its_request(self):
        manager = MessageManager("系统提示")
        manager.add_assistant_tool_calls(
            content=None,
            tool_calls=[{"id": "call_x", "type": "function",
                         "function": {"name": "n", "arguments": "{}"}}],
        )
        manager.add_tool(tool_call_id="call_x", content="payload")

        messages = manager.get_messages()
        assert messages[-1]["tool_call_id"] == messages[-2]["tool_calls"][0]["id"]


# --------------------------------------------------------------------------
# 验收 6：非法参数产生失败 ToolResult，handler 不执行
# --------------------------------------------------------------------------
class TestInvalidArgumentsViaFunctionCalling:
    def test_bad_arguments_return_failed_result(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        executor = ToolExecutor(registry)

        result = executor.execute("search_policy", '{"region": "", "keyword": ""}')

        assert result.success is False
        assert result.error_type == "INVALID_ARGUMENTS"
        assert result.data is None

    def test_handler_not_called_on_bad_arguments(self):
        from pydantic import BaseModel, Field
        from powercopy.tools.base import Tool

        class Arg(BaseModel):
            text: str = Field(min_length=1)

        calls = []

        def spy(text: str):
            calls.append(text)
            return text

        registry = ToolRegistry()
        registry.register(Tool(name="spy", description="spy", args_schema=Arg, handler=spy))
        executor = ToolExecutor(registry)

        result = executor.execute("spy", '{"text": ""}')

        assert result.success is False
        assert calls == []


# --------------------------------------------------------------------------
# 完整链路（Mock）：两轮调用
# --------------------------------------------------------------------------
class TestFullChainMock:
    def test_two_round_chain(self, no_sleep):
        client, completions = make_client([
            # 第一次：模型要求调用工具
            make_response(
                content=None,
                tool_calls=[make_tool_call(
                    "call_1", "search_policy",
                    {"region": "安徽省", "keyword": "峰谷分时电价"},
                )],
                finish_reason="tool_calls",
            ),
            # 第二次：模型给出最终文本
            make_response(content="根据模拟数据，安徽省执行峰谷分时电价。", finish_reason="stop"),
        ])

        registry = ToolRegistry()
        registry.register(policy_search_tool)
        executor = ToolExecutor(registry)
        manager = MessageManager("你是售电政策助手，必须使用工具查询。")
        manager.add_user("帮我查询安徽省的峰谷分时电价政策。")
        tools = registry.get_schemas()

        first = client.invoke(messages=manager.get_messages(), tools=tools)
        assert first["tool_calls"]

        manager.add_assistant_tool_calls(
            content=first["content"], tool_calls=first["tool_calls"]
        )
        for call in first["tool_calls"]:
            tool_result = executor.execute(
                call["function"]["name"], call["function"]["arguments"]
            )
            manager.add_tool(
                tool_call_id=call["id"],
                content=json.dumps(tool_result.to_dict(), ensure_ascii=False, allow_nan=False),
            )

        final = client.invoke(messages=manager.get_messages(), tools=tools)
        assert final["tool_calls"] == []
        assert final["content"].startswith("根据模拟数据")

        manager.add_assistant(final["content"])

        # 第二次请求应该带上完整的 assistant tool_calls + tool 消息
        second_request = completions.requests[1]
        roles = [m["role"] for m in second_request["messages"]]
        assert roles == ["system", "user", "assistant", "tool"]
        assert second_request["messages"][2]["tool_calls"][0]["id"] == "call_1"
        assert second_request["messages"][3]["tool_call_id"] == "call_1"

        # 历史完整
        assert len(manager.get_messages()) == 5
