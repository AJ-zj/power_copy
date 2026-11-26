import json

import pytest
from pydantic import BaseModel, Field

from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy import policy_search_tool
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.registry import ToolRegistry
from powercopy.tools.result import ToolResult


class EchoInput(BaseModel):
    text: str = Field(min_length=1, description="要返回的文本")


def echo_handler(text: str):
    return {"echo": text}


def make_echo_tool(name: str = "echo") -> Tool:
    return Tool(
        name=name,
        description="原样返回输入文本",
        args_schema=EchoInput,
        handler=echo_handler,
    )


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(policy_search_tool)
    reg.register(make_echo_tool())
    return reg


@pytest.fixture
def executor(registry):
    return ToolExecutor(registry)


# ---------------------------------------------------------------- 验收 1
class TestValidArguments:
    """验收 1：能够执行合法的字典及 JSON 字符串参数。"""

    def test_dict_arguments(self, executor):
        result = executor.execute(
            "search_policy", {"region": "江西省", "keyword": "峰谷电价"}
        )
        assert result.success is True
        assert result.data["region"] == "江西省"
        assert result.data["mock"] is True

    def test_json_string_arguments(self, executor):
        raw = json.dumps(
            {"region": "安徽省", "keyword": "分时电价"}, ensure_ascii=False
        )
        result = executor.execute("search_policy", raw)
        assert result.success is True
        assert result.data["region"] == "安徽省"

    def test_json_string_with_whitespace(self, executor):
        raw = '  {"region": "广东省", "keyword": "绿电"}  '
        result = executor.execute("search_policy", raw)
        assert result.success is True
        assert result.data["keyword"] == "绿电"

    def test_echo_tool_via_executor(self, executor):
        result = executor.execute("echo", {"text": "hello"})
        assert result.success is True
        assert result.data == {"echo": "hello"}


# ---------------------------------------------------------------- 验收 2
class TestToolNotFound:
    """验收 2：未知工具返回 TOOL_NOT_FOUND。"""

    def test_unknown_tool(self, executor):
        result = executor.execute("no_such_tool", {"a": 1})
        assert result.success is False
        assert result.error_type == "TOOL_NOT_FOUND"
        assert result.error_message == "指定工具未注册"
        assert result.data is None

    def test_unknown_tool_checked_before_arguments(self, executor):
        # 工具不存在时，即使参数非法也应先报 TOOL_NOT_FOUND
        result = executor.execute("no_such_tool", "这不是JSON")
        assert result.error_type == "TOOL_NOT_FOUND"


# ---------------------------------------------------------------- 验收 3
class TestInvalidJson:
    """验收 3：非法 JSON 返回 INVALID_JSON。"""

    @pytest.mark.parametrize(
        "raw",
        [
            "这不是 JSON",
            "{'single': 'quotes'}",
            '{"unclosed": ',
            "",
            "   ",
        ],
    )
    def test_bad_json_string(self, executor, raw):
        result = executor.execute("search_policy", raw)
        assert result.success is False
        assert result.error_type == "INVALID_JSON"
        assert result.error_message == "工具参数不是合法的 JSON 对象"

    @pytest.mark.parametrize("arguments", [[1, 2, 3], "123", None, 42, True])
    def test_non_object_arguments(self, executor, arguments):
        result = executor.execute("search_policy", arguments)
        assert result.success is False
        assert result.error_type == "INVALID_JSON"


# ---------------------------------------------------------------- 验收 4
class TestInvalidArguments:
    """验收 4：非法参数返回 INVALID_ARGUMENTS。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {"region": "", "keyword": "电价"},
            {"region": "江西省", "keyword": ""},
            {"region": "   ", "keyword": "电价"},
            {"region": "江西省"},
            {"keyword": "电价"},
            {"region": "江西省", "keyword": "电价", "extra": 1},
            {"region": 123, "keyword": "电价"},
            {},
        ],
    )
    def test_schema_violation(self, executor, payload):
        result = executor.execute("search_policy", payload)
        assert result.success is False
        assert result.error_type == "INVALID_ARGUMENTS"
        assert result.error_message == "工具参数不符合 Schema"
        assert result.data is None


# ---------------------------------------------------------------- 验收 5
class TestHandlerNotCalled:
    """验收 5：参数校验失败时 handler 绝不执行。"""

    def test_handler_not_called_on_invalid_arguments(self):
        calls = []

        def spy_handler(text: str):
            calls.append(text)
            return {"echo": text}

        tool = Tool(
            name="spy",
            description="记录调用次数的工具",
            args_schema=EchoInput,
            handler=spy_handler,
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = ToolExecutor(registry)

        result = executor.execute("spy", {"text": ""})

        assert result.success is False
        assert result.error_type == "INVALID_ARGUMENTS"
        assert calls == [], "校验失败时 handler 不能被执行"

    def test_handler_not_called_on_invalid_json(self):
        calls = []

        def spy_handler(text: str):
            calls.append(text)
            return {"echo": text}

        tool = Tool(
            name="spy", description="记录调用", args_schema=EchoInput, handler=spy_handler
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = ToolExecutor(registry)

        executor.execute("spy", "坏JSON")
        assert calls == []

    def test_handler_called_once_on_success(self):
        calls = []

        def spy_handler(text: str):
            calls.append(text)
            return {"echo": text}

        tool = Tool(
            name="spy", description="记录调用", args_schema=EchoInput, handler=spy_handler
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = ToolExecutor(registry)

        executor.execute("spy", {"text": "ok"})
        assert calls == ["ok"]


# ---------------------------------------------------------------- 验收 6
class TestExecutionError:
    """验收 6：业务函数异常被捕获并返回 EXECUTION_ERROR。"""

    @pytest.mark.parametrize(
        "exception",
        [
            ValueError("业务错误"),
            RuntimeError("运行时错误"),
            ZeroDivisionError("除零"),
            KeyError("缺键"),
            TypeError("类型错误"),
        ],
    )
    def test_handler_exception_caught(self, exception):
        def boom(text: str):
            raise exception

        tool = Tool(
            name="boom", description="总是抛错", args_schema=EchoInput, handler=boom
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = ToolExecutor(registry)

        result = executor.execute("boom", {"text": "x"})

        assert result.success is False
        assert result.error_type == "EXECUTION_ERROR"
        assert result.error_message == "工具执行失败"
        assert result.data is None

    def test_execution_error_logged(self, caplog):
        import logging

        def boom(text: str):
            raise ValueError("炸了")

        tool = Tool(
            name="boom", description="总是抛错", args_schema=EchoInput, handler=boom
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = ToolExecutor(registry)

        with caplog.at_level(logging.WARNING, logger="powercopy.tools.executor"):
            executor.execute("boom", {"text": "x"})

        assert "Tool execution failed" in caplog.text
        assert "boom" in caplog.text
        assert "ValueError" in caplog.text


# ---------------------------------------------------------------- 验收 7
class TestToolResultShape:
    """验收 7：统一返回 ToolResult 并记录耗时。"""

    def test_returns_tool_result_instance(self, executor):
        result = executor.execute("echo", {"text": "hi"})
        assert isinstance(result, ToolResult)

    def test_success_fields(self, executor):
        result = executor.execute("echo", {"text": "hi"})
        assert result.tool_name == "echo"
        assert result.success is True
        assert result.error_type is None
        assert result.error_message is None
        assert result.data == {"echo": "hi"}

    def test_failure_fields(self, executor):
        result = executor.execute("no_such", {})
        assert result.tool_name == "no_such"
        assert result.success is False
        assert result.error_type is not None
        assert result.data is None

    def test_latency_recorded(self, executor):
        result = executor.execute("echo", {"text": "hi"})
        assert isinstance(result.latency_ms, float)
        assert result.latency_ms >= 0

    def test_to_dict(self, executor):
        result = executor.execute("echo", {"text": "hi"})
        payload = result.to_dict()
        assert set(payload) == {
            "tool_name",
            "success",
            "data",
            "error_type",
            "error_message",
            "latency_ms",
        }
        assert payload["tool_name"] == "echo"
        assert payload["success"] is True
        assert json.dumps(payload, ensure_ascii=False)

    def test_latency_includes_handler_time(self):
        import time as real_time

        def slow(text: str):
            real_time.sleep(0.05)
            return {"echo": text}

        tool = Tool(
            name="slow", description="慢工具", args_schema=EchoInput, handler=slow
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = ToolExecutor(registry)

        result = executor.execute("slow", {"text": "x"})
        assert result.success is True
        assert result.latency_ms >= 50


# ---------------------------------------------------------------- 附加
class TestErrorPriority:
    """错误分类的优先级与顺序。"""

    def test_tool_not_found_before_json(self, executor):
        assert executor.execute("ghost", "bad json").error_type == "TOOL_NOT_FOUND"

    def test_invalid_json_before_schema(self, executor):
        assert executor.execute("search_policy", "bad json").error_type == "INVALID_JSON"

    def test_all_tool_names_recorded(self, executor):
        for name in ("echo", "search_policy", "missing"):
            result = executor.execute(name, {})
            assert result.tool_name == name
