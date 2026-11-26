import json

import pytest
from pydantic import BaseModel, Field

from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy import policy_search_tool
from powercopy.tools.registry import ToolRegistry


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


class TestRegister:
    """验收 1：能够注册 Tool 对象。"""

    def test_register_single_tool(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        assert registry.list_tools() == ["search_policy"]

    def test_register_returns_none(self):
        registry = ToolRegistry()
        assert registry.register(policy_search_tool) is None

    def test_register_rejects_non_tool(self):
        registry = ToolRegistry()
        for bad in ("search_policy", None, 123, {"name": "x"}, object()):
            with pytest.raises(TypeError, match="只能注册 Tool 对象"):
                registry.register(bad)

    def test_register_rejects_subclass_instance_of_wrong_type(self):
        """非 Tool 的普通对象必须被拒绝。"""
        class NotATool:
            name = "not_a_tool"

        with pytest.raises(TypeError):
            ToolRegistry().register(NotATool())


class TestGet:
    """验收 2：能够根据名称获取指定工具。"""

    def test_get_returns_same_object(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        assert registry.get("search_policy") is policy_search_tool

    def test_get_multiple_tools(self):
        registry = ToolRegistry()
        echo = make_echo_tool()
        registry.register(policy_search_tool)
        registry.register(echo)
        assert registry.get("echo") is echo
        assert registry.get("search_policy") is policy_search_tool


class TestDuplicate:
    """验收 3：重复注册时拒绝覆盖原工具。"""

    def test_duplicate_name_raises(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        with pytest.raises(ValueError, match="工具已存在"):
            registry.register(make_echo_tool(name="search_policy"))

    def test_original_tool_kept_after_duplicate_attempt(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        original = registry.get("search_policy")
        try:
            registry.register(make_echo_tool(name="search_policy"))
        except ValueError:
            pass
        # 原工具未被覆盖
        assert registry.get("search_policy") is original
        assert registry.get("search_policy").args_schema.__name__ == "PolicySearchInput"


class TestNotFound:
    """验收 4：不存在的工具正确抛出异常。"""

    def test_get_missing_raises_key_error(self):
        with pytest.raises(KeyError, match="工具不存在"):
            ToolRegistry().get("missing_tool")

    def test_get_missing_after_registering_others(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        with pytest.raises(KeyError):
            registry.get("another_missing")

    def test_error_message_contains_name(self):
        with pytest.raises(KeyError) as exc:
            ToolRegistry().get("ghost")
        assert "ghost" in str(exc.value)


class TestMultipleAndSchemas:
    """验收 5：管理多个工具并批量生成 Schema。"""

    def test_manage_multiple_tools(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        registry.register(make_echo_tool())
        assert registry.list_tools() == ["search_policy", "echo"]
        assert len(registry) == 2
        assert "echo" in registry
        assert "missing" not in registry

    def test_batch_schemas(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        registry.register(make_echo_tool())

        schemas = registry.to_openai_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) == 2
        assert [item["function"]["name"] for item in schemas] == [
            "search_policy",
            "echo",
        ]
        # 每个都能直接给模型用
        for schema in schemas:
            assert schema["type"] == "function"
            assert "parameters" in schema["function"]
            json.dumps(schema)

    def test_empty_registry_schemas(self):
        assert ToolRegistry().to_openai_schemas() == []

    def test_schema_matches_single_tool_schema(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        assert registry.to_openai_schemas()[0] == policy_search_tool.to_openai_schema()

    def test_list_tools_returns_copy(self):
        registry = ToolRegistry()
        registry.register(policy_search_tool)
        names = registry.list_tools()
        names.append("injected")
        assert registry.list_tools() == ["search_policy"]

    def test_registration_order_preserved(self):
        registry = ToolRegistry()
        for name in ("a", "b", "c"):
            registry.register(make_echo_tool(name=name))
        assert registry.list_tools() == ["a", "b", "c"]
