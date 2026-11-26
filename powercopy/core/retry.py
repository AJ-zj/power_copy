import random

from openai import (
    APIConnectionError,
    APITimeoutError,
    APIStatusError,
)


class RetryPolicy:

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 10.0
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")

        if not 0 < base_delay <= max_delay:
            raise ValueError("退避时间配置不合法")

        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def should_retry(self, error: Exception) -> bool:
        """判断 API 异常是否值得重试。"""

        # 网络连接异常与请求超时
        if isinstance(
            error,
            (APIConnectionError, APITimeoutError)
        ):
            return True

        # HTTP 状态码异常
        if isinstance(error, APIStatusError):

            if getattr(error, "code", None) in {
                "insufficient_quota",
                "billing_hard_limit_reached"
            }:
                return False

            return error.status_code in {
                408, 429, 500, 502, 503, 504
            }

        return False

    def get_delay(self, attempt: int) -> float:
        """计算指数退避时间，并加入随机抖动。"""

        delay = min(
            self.base_delay * (2 ** (attempt - 1)),
            self.max_delay
        )

        jitter = random.uniform(0, delay * 0.1)

        return min(delay + jitter, self.max_delay)
