import json

import pytest

from powercopy.core.llm import parse_json_content


class TestParseJsonContent:
    """parse_json_content 的清洗与解析行为。"""

    def test_plain_json(self):
        assert parse_json_content('{"a": 1}') == {"a": 1}

    def test_json_fence(self):
        assert parse_json_content('```json\n{"a": 1}\n```') == {"a": 1}

    def test_plain_fence_without_language(self):
        assert parse_json_content('```\n{"a": 1}\n```') == {"a": 1}

    def test_uppercase_fence_language(self):
        assert parse_json_content('```JSON\n{"a": 1}\n```') == {"a": 1}

    def test_multiline_fenced_json(self):
        text = '```json\n{\n  "a": 1,\n  "b": [1, 2]\n}\n```'
        assert parse_json_content(text) == {"a": 1, "b": [1, 2]}

    def test_surrounding_whitespace(self):
        assert parse_json_content('  \n {"a": 1} \n  ') == {"a": 1}

    def test_array_root(self):
        assert parse_json_content('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]

    def test_chinese_content(self):
        text = '```json\n{"policy_title": "安徽省电价通知", "relevant": true}\n```'
        result = parse_json_content(text)
        assert result["policy_title"] == "安徽省电价通知"
        assert result["relevant"] is True


class TestParseJsonContentRejects:
    """无效输入应报错，而不是静默返回空值。"""

    @pytest.mark.parametrize("value", ["", "   \n  ", None, 123, b"{}", ["{}"]])
    def test_empty_or_non_string(self, value):
        with pytest.raises(ValueError):
            parse_json_content(value)

    def test_unclosed_fence(self):
        with pytest.raises(json.JSONDecodeError):
            parse_json_content('```json\n{"a": 1}')

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            parse_json_content("not a json")

    def test_extra_text_around_json(self):
        # 围栏外壳之外的额外文字不在清洗范围内，应保持报错
        with pytest.raises(json.JSONDecodeError):
            parse_json_content('结果如下：{"a": 1}')
