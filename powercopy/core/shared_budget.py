"""跨阶段共享的模型调用预算。

背景
----
MVP-03 的实现在两个地方有缺陷（ChatGPT 源码审查发现）：

1. **重复计数**：`research/agent.py` 与 `copywriting/agent.py` 在外部手动
   `reserve_api_attempt()`，但随后调用 `self.llm.invoke(messages)` 时
   **没有把 budget 传入**。而 `LLMClient._execute` 内部本身就会
   `reserve_api_attempt()` 与 `record_usage()`。结果是：
   - 真实 LLM 模式下，内部每次 API 请求（含重试）都不受预算约束
   - 外部那一次预占只是"看起来管住了"，实际账目对不上

2. **预算被拆分**：`scripts/generate_copy.py` 为研究阶段和创作阶段各建一个
   `TaskBudget`，研究拿满额、创作额外拿一半。整个业务任务的真实上限
   是两者之和，与用户设定的单次任务预算不符。

本模块的解决办法
----------------
提供**一个** `BudgetContext`，在整个业务流程内传递同一个 `TaskBudget`：

```python
with BudgetContext(max_api_attempts=8, max_known_tokens=20000) as budget:
    research = run_research(..., budget=budget)
    draft = run_copywriting(..., budget=budget)
    review = run_review(..., budget=budget)
```

调用方**不再手动预占**，一切由 `LLMClient` 内部负责。
Mock 客户端也走同一套接口，保证行为一致。

Token 上限的性质
----------------
**是硬约束，但只在"已知消耗"上生效**。具体含义：

- `max_api_attempts` —— **硬约束**。每次真实 API 请求（含重试）都会
  预占一个名额，超出即抛 `BudgetExceeded`。
- `max_known_tokens` —— **软预算**。只有在模型返回 `usage` 时才能累计；
  若模型不返回 usage，只能标记 `usage_incomplete=True` 而**无法阻止**
  实际消耗继续增长。

因此不能宣称"Token 绝对不会超"。`BudgetContext.exhausted_by` 会如实
报告是哪个维度触发，以及 usage 是否完整。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

from powercopy.core.budget import BudgetExceeded, TaskBudget

# 预算触发维度
STOP_API = "api_budget_exhausted"
STOP_TOKEN = "token_budget_exhausted"


@dataclass
class BudgetSnapshot:
    """预算快照。用于写入业务输出与报告。"""

    max_api_attempts: int
    used_api_attempts: int
    max_known_tokens: int
    known_total_tokens: int
    usage_incomplete: bool
    exhausted_by: str | None = None

    def to_dict(self) -> dict:
        return {
            "max_api_attempts": self.max_api_attempts,
            "used_api_attempts": self.used_api_attempts,
            "remaining_api_attempts": max(
                0, self.max_api_attempts - self.used_api_attempts
            ),
            "max_known_tokens": self.max_known_tokens,
            "known_total_tokens": self.known_total_tokens,
            "remaining_known_tokens": max(
                0, self.max_known_tokens - self.known_total_tokens
            ),
            "usage_incomplete": self.usage_incomplete,
            "exhausted_by": self.exhausted_by,
            "token_limit_is_soft": True,
            "note": (
                "api_attempts 是硬约束；known_tokens 只在模型返回 usage 时"
                "可计量，usage_incomplete=True 表示实际消耗可能更高"
            ),
        }


class SharedBudget:
    """整个业务任务共享的预算包装器。

    与裸 `TaskBudget` 的区别：
    - 记录是否已被耗尽（`exhausted_by`）
    - 提供 `snapshot()` 供报告使用
    - Mock 客户端也可以接收它，行为一致

    用法：把它作为 `budget=` 参数传给 `LLMClient.invoke` / `_execute`。
    """

    def __init__(
        self,
        *,
        max_api_attempts: int = 8,
        max_known_tokens: int = 20_000,
    ):
        self._budget = TaskBudget(
            max_api_attempts=max_api_attempts,
            max_known_tokens=max_known_tokens,
        )
        self._exhausted_by: str | None = None

    # ---- 委托给底层 TaskBudget ----

    @property
    def max_api_attempts(self) -> int:
        return self._budget.max_api_attempts

    @property
    def max_known_tokens(self) -> int:
        return self._budget.max_known_tokens

    @property
    def api_attempts(self) -> int:
        return self._budget.api_attempts

    @property
    def known_total_tokens(self) -> int:
        return self._budget.known_total_tokens

    @property
    def usage_incomplete(self) -> bool:
        return self._budget.usage_incomplete

    @property
    def exhausted_by(self) -> str | None:
        return self._exhausted_by

    def stop_reason(self) -> str | None:
        reason = self._budget.stop_reason()
        if reason is not None and self._exhausted_by is None:
            self._exhausted_by = reason
        return reason

    def check(self) -> None:
        try:
            self._budget.check()
        except BudgetExceeded as exc:
            if self._exhausted_by is None:
                self._exhausted_by = getattr(exc, "reason", "unknown")
            raise

    def reserve_api_attempt(self) -> None:
        try:
            self._budget.reserve_api_attempt()
        except BudgetExceeded as exc:
            if self._exhausted_by is None:
                self._exhausted_by = getattr(exc, "reason", STOP_API)
            raise

    def record_usage(self, tokens: int | None) -> None:
        self._budget.record_usage(tokens)

    # ---- 报告 ----

    @property
    def exhausted(self) -> bool:
        return self.stop_reason() is not None

    def remaining_api_attempts(self) -> int:
        return max(0, self.max_api_attempts - self.api_attempts)

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            max_api_attempts=self.max_api_attempts,
            used_api_attempts=self.api_attempts,
            max_known_tokens=self.max_known_tokens,
            known_total_tokens=self.known_total_tokens,
            usage_incomplete=self.usage_incomplete,
            exhausted_by=self._exhausted_by,
        )

    def to_dict(self) -> dict:
        return self.snapshot().to_dict()


@contextmanager
def budget_scope(
    *,
    max_api_attempts: int = 8,
    max_known_tokens: int = 20_000,
):
    """上下文管理器：一次业务任务一个预算。

    ```python
    with budget_scope(max_api_attempts=8) as budget:
        ...
    ```
    """

    yield SharedBudget(
        max_api_attempts=max_api_attempts,
        max_known_tokens=max_known_tokens,
    )


__all__ = [
    "BudgetSnapshot",
    "SharedBudget",
    "STOP_API",
    "STOP_TOKEN",
    "budget_scope",
]
