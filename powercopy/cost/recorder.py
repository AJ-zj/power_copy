"""把 LLMClient 的实际请求写入成本台账。

接入方式
--------
`LLMClient.invoke(..., cost_hook=hook)`。`cost_hook` 是一个
`CallRecorder` 实例，负责：
- 序列号（同一 task 内第几次请求）
- 查单价、算费用
- 写 SQLite

为什么用回调而不是让 LLMClient 直接依赖 CostStore
-------------------------------------------------
`powercopy/core` 是被所有业务模块依赖的基础层。让它反向依赖
`powercopy/cost` 会造成循环依赖风险，也让「不用台账」的场景
（如测试、离线脚本）被迫建库。

用回调保持 core 层零依赖：不传 `cost_hook` 就完全不记录。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from powercopy.cost.contracts import (
    CostStatus,
    ModelCallRecord,
    PriceRule,
    RequestOutcome,
    Usage,
    prompt_fingerprint,
    utc_now_iso,
)
from powercopy.cost.pricing import PriceTable, compute_cost
from powercopy.cost.store import CostStore


class CallRecorder:
    """单次业务任务的调用记录器。

    一个 task 一个实例，内部维护 call_index 递增。

    用法：

    ```python
    recorder = CallRecorder(
        store=store,
        price_table=table,
        task_id="task-abc",
        stage="research",
        mode="live",
        provider="yituohub",
        model="glm-5.3-flash",
    )
    client.invoke(messages, budget=budget, cost_hook=recorder)
    ```
    """

    def __init__(
        self,
        store: CostStore,
        price_table: PriceTable,
        *,
        task_id: str = "",
        stage: str = "",
        mode: str = "mock",
        provider: str = "",
        model: str = "",
        currency: str = "CNY",
        price_version: str | None = None,
    ):
        self.store = store
        self.price_table = price_table
        self.task_id = task_id
        self.stage = stage
        self.mode = mode
        self.provider = provider
        self.model = model
        self.currency = currency
        self.price_version = price_version

        self._call_index = 0
        self.records: list[ModelCallRecord] = []

    # ---- 阶段切换 ----

    def for_stage(self, stage: str) -> "CallRecorder":
        """返回同一 task 下、指定阶段的新记录器（共享 call_index 计数）。

        研究/文案/审核各阶段共用同一个 task_id 与计费上下文，
        但 stage 不同，便于日报按阶段拆分 Token。
        """

        child = CallRecorder(
            store=self.store,
            price_table=self.price_table,
            task_id=self.task_id,
            stage=stage,
            mode=self.mode,
            provider=self.provider,
            model=self.model,
            currency=self.currency,
            price_version=self.price_version,
        )
        child._call_index = self._call_index
        child._parent = self
        return child

    # ---- 记录 ----

    def record_attempt(
        self,
        *,
        outcome: RequestOutcome,
        usage: Usage | None = None,
        model: str = "",
        provider: str = "",
        latency_ms: float = 0.0,
        retry_index: int = 0,
        prompt_text: str = "",
        note: str = "",
    ) -> ModelCallRecord:
        """记录一次实际请求尝试。

        **失败请求的 usage 传 None 或 complete=False**，
        最终 `cost_status=UNKNOWN`，不会被当作 0 消耗。
        """

        parent = getattr(self, "_parent", None)
        if parent is not None:
            self._call_index = parent._call_index
        self._call_index += 1
        if parent is not None:
            parent._call_index = self._call_index

        actual_model = model or self.model
        actual_provider = provider or self.provider

        usage = usage or Usage()

        # Mock 一律零成本
        if self.mode == "mock":
            record = ModelCallRecord(
                request_id=f"req-{uuid.uuid4().hex[:20]}",
                task_id=self.task_id,
                call_index=self._call_index,
                occurred_at=utc_now_iso(),
                stage=self.stage,
                provider=actual_provider,
                model=actual_model,
                usage=usage,
                outcome=outcome,
                retry_index=retry_index,
                latency_ms=latency_ms,
                price_version="",
                currency=self.currency,
                estimated_cost=Decimal("0"),
                cost_status=CostStatus.ESTIMATED,
                mode="mock",
                note=note,
                prompt_chars=prompt_fingerprint(prompt_text)[0],
                prompt_sha256=prompt_fingerprint(prompt_text)[1],
            )
            self._persist(record)
            return record

        rule = self.price_table.lookup(
            actual_provider, actual_model, self.price_version
        )
        estimated, cost_note = compute_cost(usage, rule)

        if estimated is None:
            if rule is None:
                cost_status = CostStatus.UNKNOWN
                extra_note = (
                    f"未配置单价：provider={actual_provider} model={actual_model}。"
                    f"不得套用其他平台价格"
                )
            else:
                cost_status = CostStatus.UNKNOWN
                extra_note = f"用量不完整，无法计算费用（{cost_note}）"
        else:
            cost_status = CostStatus.UNRECONCILED
            extra_note = cost_note

        record = ModelCallRecord(
            request_id=f"req-{uuid.uuid4().hex[:20]}",
            task_id=self.task_id,
            call_index=self._call_index,
            occurred_at=utc_now_iso(),
            stage=self.stage,
            provider=actual_provider,
            model=actual_model,
            usage=usage,
            outcome=outcome,
            retry_index=retry_index,
            latency_ms=latency_ms,
            price_version=rule.price_version if rule else "",
            input_price_per_million=rule.input_per_million if rule else None,
            output_price_per_million=rule.output_per_million if rule else None,
            currency=self.currency,
            estimated_cost=estimated,
            cost_status=cost_status,
            mode=self.mode,
            note=(note + "；" if note else "") + extra_note,
            prompt_chars=prompt_fingerprint(prompt_text)[0],
            prompt_sha256=prompt_fingerprint(prompt_text)[1],
        )
        self._persist(record)
        return record

    def _persist(self, record: ModelCallRecord) -> None:
        self.store.record_call(record)
        self.records.append(record)

    # ---- 汇总 ----

    def summary(self) -> dict:
        records = sorted(self.records, key=lambda r: r.call_index)
        known = sum(
            (r.usage.total_tokens or 0) for r in records if r.usage.complete
        )
        unknown = sum(1 for r in records if not r.usage.complete)
        cost = sum(
            (r.estimated_cost or Decimal("0")) for r in records
        )
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "mode": self.mode,
            "requests": len(records),
            "known_tokens": known,
            "unknown_usage_requests": unknown,
            "estimated_cost": str(cost),
            "currency": self.currency,
        }


def usage_from_response(response: dict) -> Usage:
    """从 LLMClient 的返回结构提取用量。

    关键：请求失败时返回的 `total_tokens` 可能是 None，
    此时 `complete=False`，**不得当作 0**。
    """

    total = response.get("total_tokens")
    input_tokens = response.get("input_tokens")
    output_tokens = response.get("output_tokens")
    cached = response.get("cached_tokens")

    if total is None:
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            total_tokens=None,
            complete=False,
        )

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached,
        total_tokens=total,
        complete=(
            input_tokens is not None and output_tokens is not None
        ),
    )


def outcome_from_error(error: BaseException) -> RequestOutcome:
    """把异常映射为请求结果枚举。"""

    name = type(error).__name__

    if name in ("APIConnectionError", "APITimeoutError", "TimeoutError",
                "ConnectionError", "ConnectionResetError"):
        return RequestOutcome.API_ERROR

    if name == "RateLimitError":
        return RequestOutcome.RATE_LIMITED

    if name in ("AuthenticationError", "PermissionDeniedError"):
        return RequestOutcome.QUOTA_EXCEEDED

    status = getattr(error, "status_code", None)
    if status == 429:
        return RequestOutcome.RATE_LIMITED
    if status in (401, 402, 403):
        return RequestOutcome.QUOTA_EXCEEDED
    if isinstance(status, int) and status >= 500:
        return RequestOutcome.API_ERROR

    if name in ("JSONDecodeError", "ValidationError", "ValueError"):
        return RequestOutcome.OUTPUT_INVALID

    return RequestOutcome.API_ERROR


__all__ = [
    "CallRecorder",
    "outcome_from_error",
    "usage_from_response",
]
