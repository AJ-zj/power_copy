import json

import pytest
from pydantic import ValidationError

from powercopy.core.llm import parse_json_content
from powercopy.core.schemas import PolicyAnalysis

VALID_DATA = {
    "policy_title": "安徽省工商业峰谷分时电价通知",
    "summary": "明确 100 千伏安及以上工商业用户执行峰谷分时电价。",
    "key_points": ["低谷下浮 61.8%", "夏冬季高峰上浮 84.3%"],
    "relevant": True,
}
VALID_JSON = json.dumps(VALID_DATA, ensure_ascii=False)


class TestStructuredOutputAcceptance:
    """对应 6 项验收标准。"""

    def test_plain_json_parses(self):
        """验收 1：标准 JSON 正常解析。"""
        data = parse_json_content(VALID_JSON)
        assert isinstance(data, dict)
        assert data["policy_title"] == VALID_DATA["policy_title"]

        model = PolicyAnalysis.model_validate(data)
        assert isinstance(model, PolicyAnalysis)
        assert model.relevant is True
        assert model.key_points == VALID_DATA["key_points"]

    def test_markdown_json_parses(self):
        """验收 2：Markdown JSON 正常解析。"""
        fenced = f"```json\n{VALID_JSON}\n```"
        data = parse_json_content(fenced)
        assert data["policy_title"] == VALID_DATA["policy_title"]

        model = PolicyAnalysis.model_validate(data)
        assert model.relevant is True
        assert len(model.key_points) == 2

    @pytest.mark.parametrize(
        "content",
        [
            "这不是 JSON",
            "好的，以下是分析结果：{}\n希望有帮助",
            '{"policy_title": "安徽…", "summary":',
            "{'policy_title': '安徽', 'relevant': true}",
        ],
    )
    def test_invalid_json_raises(self, content):
        """验收 3：非法 JSON 正确抛出异常。"""
        with pytest.raises(json.JSONDecodeError):
            parse_json_content(content)

    @pytest.mark.parametrize("content", ["", "   \n\t  ", None, 123, [], {}])
    def test_empty_content_raises(self, content):
        """验收 4：空内容正确抛出异常。"""
        with pytest.raises(ValueError):
            parse_json_content(content)

    @pytest.mark.parametrize(
        "payload",
        [
            {k: v for k, v in VALID_DATA.items() if k != "relevant"},
            {k: v for k, v in VALID_DATA.items() if k != "key_points"},
            {k: v for k, v in VALID_DATA.items() if k != "policy_title"},
            {k: v for k, v in VALID_DATA.items() if k != "summary"},
            {},
        ],
    )
    def test_missing_field_rejected(self, payload):
        """验收 5：缺失字段被 Pydantic 拒绝。"""
        with pytest.raises(ValidationError):
            PolicyAnalysis.model_validate(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            {**VALID_DATA, "relevant": "是"},
            {**VALID_DATA, "relevant": "true"},
            {**VALID_DATA, "key_points": "低谷下浮61.8%"},
            {**VALID_DATA, "key_points": [618]},
            {**VALID_DATA, "policy_title": 2026},
            {**VALID_DATA, "summary": ["列表不是字符串"]},
        ],
    )
    def test_wrong_type_rejected(self, payload):
        """验收 6：错误字段类型被 Pydantic 拒绝。"""
        with pytest.raises(ValidationError):
            PolicyAnalysis.model_validate(payload)


class TestStructuredOutputExtras:
    """补充边界，确保清洗规则不越界。"""

    def test_extra_field_rejected(self):
        payload = {**VALID_DATA, "unexpected": "多余字段"}
        with pytest.raises(ValidationError):
            PolicyAnalysis.model_validate(payload)

    def test_fence_without_language_tag(self):
        fenced = f"```\n{VALID_JSON}\n```"
        assert parse_json_content(fenced)["relevant"] is True

    def test_text_before_json_still_fails(self):
        # 清洗只处理「完整围栏」和「纯 JSON」，不负责从句子中抠 JSON
        with pytest.raises(json.JSONDecodeError):
            parse_json_content(f'分析如下：{VALID_JSON}')

    def test_unclosed_fence_fails(self):
        with pytest.raises(json.JSONDecodeError):
            parse_json_content(f"```json\n{VALID_JSON}")
