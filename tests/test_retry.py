import httpx2 as httpx
import pytest
from openai import APIConnectionError, APITimeoutError, APIStatusError

from powercopy.core.retry import RetryPolicy

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")


def status_error(code: int, body=None) -> APIStatusError:
    return APIStatusError(
        message="error",
        response=httpx.Response(code, request=REQUEST),
        body=body,
    )


class TestRetryPolicyValidation:
    """构造参数校验。"""

    @pytest.mark.parametrize("max_attempts", [0, -1, -100])
    def test_max_attempts_must_be_positive(self, max_attempts):
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=max_attempts)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"base_delay": 0},
            {"base_delay": -1},
            {"base_delay": 20, "max_delay": 10},
            {"base_delay": 5, "max_delay": 1},
        ],
    )
    def test_delay_config_must_be_sane(self, kwargs):
        with pytest.raises(ValueError, match="退避时间配置不合法"):
            RetryPolicy(**kwargs)

    def test_defaults(self):
        policy = RetryPolicy()
        assert policy.max_attempts == 3
        assert policy.base_delay == 1.0
        assert policy.max_delay == 10.0

    def test_single_attempt_allowed(self):
        assert RetryPolicy(max_attempts=1).max_attempts == 1

    def test_equal_base_and_max_allowed(self):
        policy = RetryPolicy(base_delay=5, max_delay=5)
        assert policy.base_delay == policy.max_delay == 5


class TestShouldRetry:
    """哪些异常值得重试。"""

    def setup_method(self):
        self.policy = RetryPolicy()

    def test_connection_error_is_retryable(self):
        assert self.policy.should_retry(APIConnectionError(request=REQUEST)) is True

    def test_timeout_error_is_retryable(self):
        assert self.policy.should_retry(APITimeoutError(request=REQUEST)) is True

    @pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
    def test_retryable_status_codes(self, code):
        assert self.policy.should_retry(status_error(code)) is True

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
    def test_non_retryable_status_codes(self, code):
        assert self.policy.should_retry(status_error(code)) is False

    @pytest.mark.parametrize(
        "billing_code", ["insufficient_quota", "billing_hard_limit_reached"]
    )
    def test_billing_errors_never_retried(self, billing_code):
        # 即使状态码是 429，额度类错误重试也没用，必须立即失败
        error = status_error(429, {"code": billing_code})
        assert self.policy.should_retry(error) is False

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("普通异常"),
            RuntimeError("运行时错误"),
            KeyError("key"),
            TypeError(),
        ],
    )
    def test_non_api_errors_not_retried(self, error):
        assert self.policy.should_retry(error) is False


class TestGetDelay:
    """指数退避与抖动。"""

    def test_exponential_growth(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0)
        # 每次尝试延迟落在 [base*2^(n-1), base*2^(n-1)*1.1] 区间
        for attempt in range(1, 5):
            expected = 1.0 * (2 ** (attempt - 1))
            delay = policy.get_delay(attempt)
            assert expected <= delay <= expected * 1.1 + 1e-9

    def test_never_exceeds_max_delay(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
        for attempt in range(1, 20):
            assert policy.get_delay(attempt) <= policy.max_delay

    def test_capped_at_max_delay(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=3.0)
        assert policy.get_delay(10) <= 3.0

    def test_jitter_is_random(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
        samples = {policy.get_delay(3) for _ in range(10)}
        assert len(samples) > 1

    def test_jitter_within_ten_percent(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0)
        for _ in range(50):
            delay = policy.get_delay(3)
            assert 4.0 <= delay <= 4.4 + 1e-9

    def test_first_attempt_uses_base_delay(self):
        policy = RetryPolicy(base_delay=2.0, max_delay=10.0)
        delay = policy.get_delay(1)
        assert 2.0 <= delay <= 2.2 + 1e-9
