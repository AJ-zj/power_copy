import json

import pytest
from pydantic import ValidationError

from powercopy.tools.builtin.policy import (
    PolicySearchInput,
    policy_search_tool,
    search_policy,
)


class TestPolicySearchInput:
    """入参模型：严格模式 + 禁止多余字段。"""

    def test_accepts_valid_arguments(self):
        args = PolicySearchInput(region="江西省", keyword="峰谷分时电价")
        assert args.region == "江西省"
        assert args.keyword == "峰谷分时电价"

    def test_region_must_not_be_empty(self):
        with pytest.raises(ValidationError):
            PolicySearchInput(region="", keyword="电价")

    @pytest.mark.parametrize("region", ["   ", "\t", "\n", " \t\n ", "   　"])
    def test_whitespace_only_region_rejected(self, region):
        """纯空白（空格、制表、换行、全角空格）必须被拒绝。"""
        with pytest.raises(ValidationError):
            PolicySearchInput(region=region, keyword="电价")

    @pytest.mark.parametrize("keyword", ["   ", "\t\n", "　　"])
    def test_whitespace_only_keyword_rejected(self, keyword):
        with pytest.raises(ValidationError):
            PolicySearchInput(region="江西省", keyword=keyword)

    def test_surrounding_whitespace_is_stripped(self):
        args = PolicySearchInput(region="  江西省  ", keyword="  峰谷电价  ")
        assert args.region == "江西省"
        assert args.keyword == "峰谷电价"

    def test_keyword_must_not_be_blank(self):
        with pytest.raises(ValidationError):
            PolicySearchInput(region="江西省", keyword="")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            PolicySearchInput(region="江西省", keyword="电价", extra="x")

    def test_strict_mode_rejects_wrong_type(self):
        with pytest.raises(ValidationError):
            PolicySearchInput(region=123, keyword="电价")

    def test_missing_field_rejected(self):
        with pytest.raises(ValidationError):
            PolicySearchInput(region="江西省")

    def test_schema_exposes_constraints(self):
        schema = PolicySearchInput.model_json_schema()
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["region", "keyword"]
        assert schema["properties"]["region"]["minLength"] == 1

class TestSearchPolicyHandler:
    """业务函数：当前返回模拟结果。"""

    def test_returns_mock_flag(self):
        result = search_policy(region="安徽省", keyword="分时电价")
        assert result["mock"] is True

    def test_echoes_input(self):
        result = search_policy(region="安徽省", keyword="分时电价")
        assert result["region"] == "安徽省"
        assert result["keyword"] == "分时电价"

    def test_returns_documents_list(self):
        result = search_policy(region="安徽省", keyword="分时电价")
        assert isinstance(result["documents"], list)
        assert len(result["documents"]) == 1
        assert result["documents"][0]["id"] == "mock-001"

    def test_result_is_json_serializable(self):
        result = search_policy(region="安徽省", keyword="分时电价")
        assert "mock-001" in json.dumps(result, ensure_ascii=False)


class TestPolicyToolDefinition:
    """工具封装：能被模型识别，且能通过 handler 执行。"""

    def test_name_and_description(self):
        assert policy_search_tool.name == "search_policy"
        assert "模拟工具" in policy_search_tool.description
        assert "不返回真实政策依据" in policy_search_tool.description

    def test_schema_is_openai_compatible(self):
        schema = policy_search_tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "search_policy"
        params = schema["function"]["parameters"]
        assert params["required"] == ["region", "keyword"]
        assert params["additionalProperties"] is False

    def test_handler_runs_through_tool(self):
        result = policy_search_tool.handler(region="广东省", keyword="绿电交易")
        assert result["mock"] is True
        assert result["region"] == "广东省"

    def test_tool_can_validate_arguments_before_calling(self):
        """调用方可用 args_schema 先校验，再交给 handler。"""
        args = policy_search_tool.args_schema(
            region="山西省", keyword="现货市场"
        )
        result = policy_search_tool.handler(**args.model_dump())
        assert result["region"] == "山西省"
        assert result["keyword"] == "现货市场"

    def test_invalid_arguments_blocked_before_handler(self):
        with pytest.raises(ValidationError):
            policy_search_tool.args_schema(region="", keyword="")
