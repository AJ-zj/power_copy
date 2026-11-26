import pytest
from pydantic import BaseModel, Field

from powercopy.tools.base import Tool


class PolicyQueryArgs(BaseModel):
    """示例参数模型：模拟一个政策查询工具。"""

    keyword: str = Field(description="政策关键词")
    region: str = Field(default="", description="省份，例如 江西")
    limit: int = Field(default=5, description="返回条数上限")


def dummy_handler(**kwargs):
    return kwargs


class TestToolDefinition:
    def test_fields_are_stored(self):
        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        assert tool.name == "query_policy"
        assert tool.description == "按关键词查询政策库"
        assert tool.args_schema is PolicyQueryArgs
        assert tool.handler is dummy_handler

    def test_to_openai_schema_shape(self):
        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        schema = tool.to_openai_schema()

        assert schema["type"] == "function"
        assert set(schema["function"]) == {"name", "description", "parameters"}
        assert schema["function"]["name"] == "query_policy"
        assert schema["function"]["description"] == "按关键词查询政策库"

    def test_parameters_come_from_pydantic_schema(self):
        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        params = tool.to_openai_schema()["function"]["parameters"]

        assert params["type"] == "object"
        assert set(params["properties"]) == {"keyword", "region", "limit"}
        assert params["required"] == ["keyword"]

    def test_parameters_include_field_descriptions(self):
        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        params = tool.to_openai_schema()["function"]["parameters"]
        assert params["properties"]["keyword"]["description"] == "政策关键词"
        assert params["properties"]["region"]["description"] == "省份，例如 江西"

    def test_json_serializable(self):
        import json

        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        text = json.dumps(tool.to_openai_schema(), ensure_ascii=False)
        assert "query_policy" in text

    def test_each_call_returns_new_dict(self):
        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        first = tool.to_openai_schema()
        second = tool.to_openai_schema()
        assert first == second
        assert first is not second

    def test_handler_is_callable_and_runs(self):
        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        assert callable(tool.handler)
        assert tool.handler(keyword="电价") == {"keyword": "电价"}

    def test_dataclass_equality(self):
        kwargs = dict(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        assert Tool(**kwargs) == Tool(**kwargs)


class TestToolWithEmptyArgs:
    def test_no_parameter_tool(self):
        class EmptyArgs(BaseModel):
            pass

        tool = Tool(
            name="health_check",
            description="检查服务状态",
            args_schema=EmptyArgs,
            handler=lambda **kw: "ok",
        )
        params = tool.to_openai_schema()["function"]["parameters"]
        assert params["type"] == "object"
        assert params.get("properties", {}) == {}


class TestToolSchemaCompatibility:
    """确认生成的 schema 能直接塞进 OpenAI tools 参数。"""

    def test_fits_tools_list(self):
        tool = Tool(
            name="query_policy",
            description="按关键词查询政策库",
            args_schema=PolicyQueryArgs,
            handler=dummy_handler,
        )
        tools = [tool.to_openai_schema()]
        assert isinstance(tools, list)
        assert tools[0]["function"]["name"] == "query_policy"
