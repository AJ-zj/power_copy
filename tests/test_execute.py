"""统一执行器 _execute() 的验收测试。

全部使用 Mock，不向真实 API 发送失败请求。
"""

import json
import logging

import httpx2 as httpx
import pytest
from openai import APIStatusError

from powercopy.core.llm import LLMClient
from powercopy.core.schemas import PolicyAnalysis

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")

VALID_JSON = json.dumps(
    {
        "policy_title": "安徽电价通知",
        "summary": "摘要",
        "key_points": ["要点一", "要点二"],
        "relevant": True,
    },
    ensure_ascii=False,
)


def status_error(code: int, body=None) -> APIStatusError:
    return APIStatusError(
        message="mock",
        response=httpx.Response(code, request=REQUEST),
        body=body,
    )


def ok_result(content: str = "ok", tokens: int = 15, model: str = "glm-5.3-flash"):
    return {
        "content": content,
        "model": model,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": tokens,
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("powercopy.core.llm.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    return LLMClient()


@pytest.fixture
def clock(monkeypatch):
    delays = []
    monkeypatch.setattr(
        "powercopy.core.llm.time.sleep", lambda seconds: delays.append(seconds)
    )
    return delays


def scripts(client, monkeypatch, behaviors):
    """按顺序消费 behaviors：dict 表示返回结果，Exception 表示抛出。"""
    calls = {"count": 0, "messages": []}

    def fake_once(messages, model=None):
        index = calls["count"]
        calls["count"] += 1
        # 快照：实现在重试时会往同一个列表追加修复指令，
        # 这里保存副本才能对比每一轮的差异
        calls["messages"].append([dict(item) for item in messages])
        behavior = behaviors[min(index, len(behaviors) - 1)]
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    monkeypatch.setattr(client, "_invoke_once", fake_once)
    return calls


# ---------------------------------------------------------------- 验收 1
class TestNormalTextCall:
    def test_single_request_success(self, client, clock, monkeypatch):
        calls = scripts(client, monkeypatch, [ok_result("纯文本回复")])

        result = client.invoke([{"role": "user", "content": "hi"}])

        assert calls["count"] == 1
        assert result["content"] == "纯文本回复"
        assert result["attempts"] == 1
        assert result["retry_count"] == 0
        assert result["format_retries"] == 0
        assert result["total_tokens"] == 15
        assert "data" not in result
        assert clock == []

    def test_keeps_original_fields(self, client, clock, monkeypatch):
        scripts(client, monkeypatch, [ok_result()])
        result = client.invoke([{"role": "user", "content": "hi"}])
        for key in ("content", "model", "input_tokens", "output_tokens", "total_tokens"):
            assert key in result


# ---------------------------------------------------------------- 验收 2
class TestNormalStructuredCall:
    def test_returns_pydantic_instance(self, client, clock, monkeypatch):
        calls = scripts(client, monkeypatch, [ok_result(VALID_JSON)])

        result = client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )

        assert calls["count"] == 1
        assert isinstance(result["data"], PolicyAnalysis)
        assert result["data"].policy_title == "安徽电价通知"
        assert result["data"].relevant is True
        assert result["format_retries"] == 0


# ---------------------------------------------------------------- 验收 3
class TestJsonErrorThenSuccess:
    def test_bad_json_then_valid(self, client, clock, monkeypatch):
        # 第一次返回非法 JSON，第二次合法
        calls = scripts(
            client, monkeypatch, [ok_result("这不是JSON"), ok_result(VALID_JSON)]
        )

        result = client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )

        assert calls["count"] == 2
        assert result["format_retries"] == 1
        assert result["attempts"] == 2
        assert isinstance(result["data"], PolicyAnalysis)


# ---------------------------------------------------------------- 验收 4
class TestSchemaFailThenSuccess:
    def test_validation_error_then_valid(self, client, clock, monkeypatch):
        # 第一次缺少字段（可解析但校验失败），第二次合法
        bad = json.dumps({"policy_title": "只有标题"}, ensure_ascii=False)
        calls = scripts(client, monkeypatch, [ok_result(bad), ok_result(VALID_JSON)])

        result = client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )

        assert calls["count"] == 2
        assert result["format_retries"] == 1
        assert isinstance(result["data"], PolicyAnalysis)
        assert result["data"].policy_title == "安徽电价通知"


# ---------------------------------------------------------------- 验收 5
class TestMixedErrors:
    def test_503_then_bad_json_then_valid(self, client, clock, monkeypatch):
        calls = scripts(
            client,
            monkeypatch,
            [status_error(503), ok_result("坏JSON"), ok_result(VALID_JSON)],
        )

        result = client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )

        assert calls["count"] == 3
        assert result["attempts"] == 3
        assert result["format_retries"] == 1
        assert isinstance(result["data"], PolicyAnalysis)


# ---------------------------------------------------------------- 验收 6
class TestTotalBudget:
    def test_no_fourth_request(self, client, clock, monkeypatch):
        calls = scripts(
            client, monkeypatch, [status_error(503)] * 10
        )

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        assert calls["count"] == 3, "最多 3 次 API 请求"


# ---------------------------------------------------------------- 验收 7
class TestFormatBudget:
    def test_two_bad_json_raises(self, client, clock, monkeypatch):
        calls = scripts(
            client, monkeypatch, [ok_result("坏JSON"), ok_result("还是坏JSON")]
        )

        with pytest.raises(json.JSONDecodeError):
            client.invoke_structured(
                [{"role": "user", "content": "分析"}], PolicyAnalysis
            )

        # 第一次失败后重试一次，第二次失败即抛出：共 2 次请求
        assert calls["count"] == 2

    def test_two_validation_errors_raise(self, client, clock, monkeypatch):
        bad = json.dumps({"policy_title": "缺字段"}, ensure_ascii=False)
        calls = scripts(client, monkeypatch, [ok_result(bad), ok_result(bad)])

        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            client.invoke_structured(
                [{"role": "user", "content": "分析"}], PolicyAnalysis
            )

        assert calls["count"] == 2


# ---------------------------------------------------------------- 验收 8
class TestTokenAccumulation:
    def test_tokens_accumulated_across_attempts(self, client, clock, monkeypatch):
        scripts(
            client,
            monkeypatch,
            [status_error(503), ok_result(VALID_JSON, tokens=40)],
        )

        result = client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )

        # 失败那次没有 usage（异常），成功那次 40
        assert result["known_total_tokens"] == 40
        assert result["total_tokens"] == 40

    def test_usage_incomplete_marks_flag(self, client, clock, monkeypatch):
        scripts(client, monkeypatch, [ok_result("ok", tokens=None)])
        result = client.invoke([{"role": "user", "content": "hi"}])
        assert result["usage_incomplete"] is True
        assert result["total_tokens"] is None
        assert result["known_total_tokens"] == 0


# ---------------------------------------------------------------- 验收 9
class TestLatency:
    def test_total_latency_covers_retries_and_sleep(
        self, client, monkeypatch
    ):
        # 这个用例要真实等待，因此不使用 clock fixture（它会把 sleep 换成空实现）。
        import time as real_time

        original_sleep = real_time.sleep
        real_sleep_calls = []

        def slow_sleep(seconds):
            real_sleep_calls.append(seconds)
            original_sleep(0.05)

        monkeypatch.setattr("powercopy.core.llm.time.sleep", slow_sleep)
        scripts(
            client,
            monkeypatch,
            [status_error(503), status_error(503), ok_result(VALID_JSON)],
        )

        result = client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )

        assert len(real_sleep_calls) == 2
        # 两次退避各 0.05s，整体耗时必然 >= 100ms
        assert result["total_latency_ms"] >= 100
        assert result["total_latency_ms"] >= result["latency_ms"]


# ---------------------------------------------------------------- 验收 10
class TestMessageIsolation:
    def test_caller_messages_not_mutated(self, client, clock, monkeypatch):
        original = [{"role": "user", "content": "分析"}]
        snapshot = [dict(item) for item in original]

        scripts(
            client, monkeypatch, [ok_result("坏JSON"), ok_result(VALID_JSON)]
        )
        client.invoke_structured(original, PolicyAnalysis)

        assert original == snapshot, "调用者的消息列表不能被修改"
        assert len(original) == 1

    def test_repair_instruction_only_in_internal_copy(
        self, client, clock, monkeypatch
    ):
        original = [{"role": "user", "content": "分析"}]
        calls = scripts(
            client, monkeypatch, [ok_result("坏JSON"), ok_result(VALID_JSON)]
        )
        client.invoke_structured(original, PolicyAnalysis)

        # 第二次请求内部列表应比第一次多一条修复指令
        first_len = len(calls["messages"][0])
        second_len = len(calls["messages"][1])
        assert second_len == first_len + 1
        assert "合法 JSON" in calls["messages"][1][-1]["content"]

    def test_invoke_does_not_copy_mutate(self, client, clock, monkeypatch):
        original = [{"role": "user", "content": "hi"}]
        scripts(client, monkeypatch, [ok_result()])
        client.invoke(original)
        assert original == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------- 附加
class TestLogging:
    def test_logs_stage_and_format_retry(
        self, client, clock, monkeypatch, caplog
    ):
        scripts(
            client, monkeypatch, [ok_result("坏JSON"), ok_result(VALID_JSON)]
        )
        with caplog.at_level(logging.INFO, logger="powercopy.core.llm"):
            client.invoke_structured(
                [{"role": "user", "content": "分析"}], PolicyAnalysis
            )

        text = caplog.text
        assert "stage=output" in text
        assert "LLM format retry 1/1" in text
        assert "LLM success" in text


class TestNoNestedRetry:
    def test_invoke_structured_does_not_double_retry(
        self, client, clock, monkeypatch
    ):
        """确保没有嵌套重试：3 次 API 全失败时总请求数就是 3。"""
        calls = scripts(client, monkeypatch, [status_error(503)] * 6)

        with pytest.raises(APIStatusError):
            client.invoke_structured(
                [{"role": "user", "content": "分析"}], PolicyAnalysis
            )

        assert calls["count"] == 3
