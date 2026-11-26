class BudgetExceeded(RuntimeError):
    """任务预算耗尽异常。"""

    def __init__(self, reason: str):

        self.reason = reason

        super().__init__(reason)


class TaskBudget:
    """管理整个 Agent 任务的模型调用预算。"""

    def __init__(
        self,
        max_api_attempts: int = 8,
        max_known_tokens: int = 20000
    ):

        if max_api_attempts < 1 or max_known_tokens < 1:
            raise ValueError("预算必须大于零")

        # 预算上限
        self.max_api_attempts = max_api_attempts
        self.max_known_tokens = max_known_tokens

        # 实际消耗
        self.api_attempts = 0
        self.known_total_tokens = 0
        self.usage_incomplete = False

    def stop_reason(self) -> str | None:
        """检查是否达到预算限制。"""

        if self.api_attempts >= self.max_api_attempts:
            return "api_budget_exhausted"

        if self.known_total_tokens >= self.max_known_tokens:
            return "token_budget_exhausted"

        return None

    def check(self):
        """预算不足时拒绝继续执行模型请求。"""

        reason = self.stop_reason()

        if reason is not None:
            raise BudgetExceeded(reason)

    def reserve_api_attempt(self):
        """在实际发送 API 请求前消耗一次预算。"""

        self.check()

        self.api_attempts += 1

    def record_usage(self, tokens: int | None):
        """累计模型返回的已知 Token 消耗。"""

        if type(tokens) is int and tokens >= 0:

            self.known_total_tokens += tokens

        else:

            self.usage_incomplete = True

    def snapshot(self) -> dict:
        """返回当前预算消耗情况。"""

        return {
            "api_attempts": self.api_attempts,
            "max_api_attempts": self.max_api_attempts,
            "remaining_api_attempts": (
                self.max_api_attempts - self.api_attempts
            ),
            "known_total_tokens": self.known_total_tokens,
            "max_known_tokens": self.max_known_tokens,
            "usage_incomplete": self.usage_incomplete
        }
