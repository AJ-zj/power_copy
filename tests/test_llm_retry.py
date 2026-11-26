"""Retry 接入 LLM Client 的验收测试。

全部使用 mock，不产生真实 API 费用。
"""

import logging

import httpx2 as httpx
import pytest
from openai import APIStatusError

from powercopy.core.llm import LLMClient


REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")


def status_error(code: int, body=None) -> APIStatusError:
    return APIStatusError(
        message="error",
        response=httpx.Response(code, request=REQUEST),
        body=body,
    )


def fake_success(content="ok"):
    return {
        "content": content,
        "model": "glm-5.3-flash",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


@pytest.fixture
def client(monkeypatch):
    """构造 LLMClient，但跳过真实 .env 读取和网络。"""
    monkeypatch.setattr(
        "powercopy.core.llm.load_dotenv", lambda *a, **k: None
    )
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    return LLMClient()


@pytest.fixture
def no_sleep(monkeypatch):
    """记录 sleep 调用，但立即返回，让测试快速跑完。"""
    calls = []
    monkeypatch.setattr(
        "powercopy.core.llm.time.sleep", lambda seconds: calls.append(seconds)
    )
    return calls


class TestNormalRequest:
    """验收 1：正常请求只调用一次 API。"""

    def test_single_call_on_success(self, client, no_sleep, monkeypatch):
        calls = []

        def fake_once(messages, model=None):
            calls.append(model)
            return fake_success()

        monkeypatch.setattr(client, "_invoke_once", fake_once)

        result = client.invoke([{"role": "user", "content": "hi"}])

        assert len(calls) == 1
        assert result["content"] == "ok"
        assert result["attempts"] == 1
        assert result["retry_count"] == 0
        assert no_sleep == []
        assert isinstance(result["latency_ms"], float)

    def test_model_override_is_passed(self, client, no_sleep, monkeypatch):
        seen = {}

        def fake_once(messages, model=None):
            seen["model"] = model
            return fake_success()

        monkeypatch.setattr(client, "_invoke_once", fake_once)
        client.invoke([{"role": "user", "content": "hi"}], model="custom-model")

        assert seen["model"] == "custom-model"


class TestRetryThenSuccess:
    """验收 2：前两次失败、第三次成功，正确返回结果。"""

    def test_two_failures_then_success(self, client, no_sleep, monkeypatch):
        attempts = []

        def flaky(messages, model=None):
            attempts.append(1)
            if len(attempts) < 3:
                raise status_error(503)
            return fake_success("第三次成功")

        monkeypatch.setattr(client, "_invoke_once", flaky)

        result = client.invoke([{"role": "user", "content": "hi"}])

        assert len(attempts) == 3
        assert result["content"] == "第三次成功"
        assert result["attempts"] == 3
        assert result["retry_count"] == 2
        # 两次失败后各等待一次
        assert len(no_sleep) == 2


class TestNoRetryForAuthErrors:
    """验收 3：HTTP 401 直接抛出异常，不重试。"""

    def test_401_raises_immediately(self, client, no_sleep, monkeypatch):
        calls = []

        def always_401(messages, model=None):
            calls.append(1)
            raise status_error(401)

        monkeypatch.setattr(client, "_invoke_once", always_401)

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        assert len(calls) == 1
        assert no_sleep == []

    @pytest.mark.parametrize("code", [400, 403, 404, 422])
    def test_other_client_errors_not_retried(
        self, client, no_sleep, monkeypatch, code
    ):
        calls = []

        def fail(messages, model=None):
            calls.append(1)
            raise status_error(code)

        monkeypatch.setattr(client, "_invoke_once", fail)

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        assert len(calls) == 1

    def test_insufficient_quota_not_retried(self, client, no_sleep, monkeypatch):
        calls = []

        def fail(messages, model=None):
            calls.append(1)
            raise status_error(429, {"code": "insufficient_quota"})

        monkeypatch.setattr(client, "_invoke_once", fail)

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        # 虽然是 429，但额度问题不重试
        assert len(calls) == 1


class TestRetryableStatusCodes:
    """验收 4：HTTP 429 或 503 能触发有限次重试。"""

    @pytest.mark.parametrize("code", [429, 503, 500, 502, 504, 408])
    def test_retryable_code_triggers_retry(
        self, client, no_sleep, monkeypatch, code
    ):
        calls = []

        def once_fail_then_ok(messages, model=None):
            calls.append(1)
            if len(calls) == 1:
                raise status_error(code)
            return fake_success()

        monkeypatch.setattr(client, "_invoke_once", once_fail_then_ok)

        result = client.invoke([{"role": "user", "content": "hi"}])

        assert len(calls) == 2
        assert result["attempts"] == 2
        assert result["retry_count"] == 1


class TestMaxAttempts:
    """验收 5：连续三次失败后停止，不再发起第四次请求。"""

    def test_stops_after_three_failures(self, client, no_sleep, monkeypatch):
        calls = []

        def always_503(messages, model=None):
            calls.append(1)
            raise status_error(503)

        monkeypatch.setattr(client, "_invoke_once", always_503)

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        assert len(calls) == 3, "最多 3 次，不能有第 4 次"
        assert len(no_sleep) == 2, "只有前两次失败后等待"

    def test_custom_max_attempts(self, client, no_sleep, monkeypatch):
        from powercopy.core.retry import RetryPolicy

        client.retry_policy = RetryPolicy(max_attempts=5)
        calls = []

        def always_503(messages, model=None):
            calls.append(1)
            raise status_error(503)

        monkeypatch.setattr(client, "_invoke_once", always_503)

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        assert len(calls) == 5
        assert len(no_sleep) == 4


class TestBackoff:
    """验收 6：指数退避与随机抖动符合配置范围。"""

    def test_delays_follow_exponential_backoff(
        self, client, no_sleep, monkeypatch
    ):
        def always_503(messages, model=None):
            raise status_error(503)

        monkeypatch.setattr(client, "_invoke_once", always_503)

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        # 两次退避：第 1 次约 1.0~1.1s，第 2 次约 2.0~2.2s
        assert len(no_sleep) == 2
        assert 1.0 <= no_sleep[0] <= 1.1 + 1e-6
        assert 2.0 <= no_sleep[1] <= 2.2 + 1e-6

    def test_delay_respects_custom_policy(self, client, no_sleep, monkeypatch):
        from powercopy.core.retry import RetryPolicy

        client.retry_policy = RetryPolicy(
            max_attempts=3, base_delay=0.1, max_delay=0.5
        )

        def always_503(messages, model=None):
            raise status_error(503)

        monkeypatch.setattr(client, "_invoke_once", always_503)

        with pytest.raises(APIStatusError):
            client.invoke([{"role": "user", "content": "hi"}])

        # 0.1~0.11 和 0.2~0.22，均不超过 max_delay
        assert all(delay <= 0.5 for delay in no_sleep)


class TestLogging:
    """验收 7：正确记录调用次数、Token、耗时与异常类型。"""

    def test_logs_success_with_metadata(self, client, no_sleep, monkeypatch, caplog):
        # 缩短退避，只验证日志内容
        def flaky(messages, model=None):
            if not hasattr(flaky, "called"):
                flaky.called = True
                raise status_error(503)
            return fake_success()

        monkeypatch.setattr(client, "_invoke_once", flaky)

        with caplog.at_level(logging.INFO, logger="powercopy.core.llm"):
            result = client.invoke([{"role": "user", "content": "hi"}])

        text = caplog.text
        assert "LLM failed" in text
        assert "error_type=APIStatusError" in text
        assert "LLM success" in text
        assert "attempt=2" in text
        assert "total_tokens=15" in text
        assert "latency_ms=" in text
        assert result["total_tokens"] == 15

    def test_logs_retry_waiting(self, client, no_sleep, monkeypatch, caplog):
        calls = []

        def flaky(messages, model=None):
            calls.append(1)
            if len(calls) < 2:
                raise status_error(503)
            return fake_success()

        monkeypatch.setattr(client, "_invoke_once", flaky)

        with caplog.at_level(logging.INFO, logger="powercopy.core.llm"):
            client.invoke([{"role": "user", "content": "hi"}])

        assert "LLM retry waiting" in caplog.text


class TestStructuredOutputUsesRetry:
    """invoke_structured 复用 invoke，因此自动获得重试能力。"""

    def test_structured_retries_via_invoke(self, client, no_sleep, monkeypatch):
        calls = []

        def flaky_once(messages, model=None):
            calls.append(1)
            if len(calls) < 2:
                raise status_error(503)
            return {
                "content": '{"policy_title":"标题","summary":"摘要","key_points":["要点"],"relevant":true}',
                "model": "glm-5.3-flash",
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
            }

        monkeypatch.setattr(client, "_invoke_once", flaky_once)

        from powercopy.core.schemas import PolicyAnalysis

        result = client.invoke_structured(
            [{"role": "user", "content": "分析政策"}], PolicyAnalysis
        )

        assert len(calls) == 2
        assert result["attempts"] == 2
        assert result["data"].policy_title == "标题"
