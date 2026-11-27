"""成本台账数据契约。

核心原则
--------
1. **未知不得填零。** `Usage.total_tokens is None` 表示「不知道消耗了多少」，
   与「消耗了 0」是两件事。前者 `usage_complete=False`、
   `cost_status=UNKNOWN`，日报里单独统计。

2. **货币用 Decimal。** 所有金额字段是 `Decimal`，序列化时转字符串，
   避免 JSON 浮点误差。

3. **三种费用严格区分。**
   - `estimated`：按内部单价表估算
   - `billed`：服务商账单已导入并匹配
   - `unreconciled`：有估算但没账单

4. **时间一律 UTC 存储，北京时间展示。** 日报按 `Asia/Shanghai` 切日。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

BEIJING_TZ = "Asia/Shanghai"

# 北京时间固定 UTC+8，中国自 1991 年起不实行夏令时。
#
# 这里刻意用固定偏移而不是 zoneinfo：Windows 上默认没有 IANA 时区数据库
# （除非额外安装 tzdata），`ZoneInfo("Asia/Shanghai")` 会抛
# ZoneInfoNotFoundError。用固定偏移既无依赖又正确。
BEIJING_OFFSET = timezone(timedelta(hours=8))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


class RequestOutcome(StrEnum):
    """单次 API 请求尝试的结果。"""

    SUCCESS = "success"
    """请求成功并返回内容"""

    API_ERROR = "api_error"
    """网络/协议层失败（连接、超时、5xx）"""

    RATE_LIMITED = "rate_limited"
    """被限流（429）"""

    QUOTA_EXCEEDED = "quota_exceeded"
    """额度不足/欠费（非重试类）"""

    OUTPUT_INVALID = "output_invalid"
    """请求成功但结构化输出不合规"""

    BUDGET_BLOCKED = "budget_blocked"
    """预算不足，请求未发出"""


class CostStatus(StrEnum):
    """费用状态。

    `UNKNOWN` 不是错误，是「没有可靠单价或没有可靠用量」的如实表达。
    """

    ESTIMATED = "estimated"
    """按内部单价表估算"""

    BILLED = "billed"
    """已用服务商账单对账"""

    UNRECONCILED = "unreconciled"
    """有估算但尚未对账"""

    UNKNOWN = "unknown"
    """无法计算（缺单价或缺用量）"""


class Usage(BaseModel):
    """一次请求尝试的用量。

    **`None` 与 `0` 语义不同**：None 表示服务商没给或请求失败不知道，
    0 表示明确知道消耗为 0。
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    complete: bool = Field(
        default=False,
        description="服务商是否提供了完整用量。False 时不得按 0 计入统计",
    )

    @model_validator(mode="after")
    def _check(self):
        # total 未给但 input/output 都有 → 可推导
        if (
            self.total_tokens is None
            and self.input_tokens is not None
            and self.output_tokens is not None
        ):
            object.__setattr__(
                self, "total_tokens", self.input_tokens + self.output_tokens
            )
        # 有 total 但没有分项：仍算「用量不完整」（无法按输入输出分别计价）
        if self.total_tokens is not None and (
            self.input_tokens is None or self.output_tokens is None
        ):
            object.__setattr__(self, "complete", False)
        elif self.total_tokens is not None:
            object.__setattr__(self, "complete", True)
        return self

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "complete": self.complete,
        }


class PriceRule(BaseModel):
    """单价规则。

    **单位：每百万 Token 的金额（币种由 currency 指定）。**
    国内厂商普遍按「元/千 Token」或「元/百万 Token」报价，
    这里统一用「元/百万 Token」，避免小数点后位数过多。

    `price_version` 用于价格调整后的历史追溯：旧记录保留旧版本号，
    重算时才用新版本。
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=120)

    input_per_million: Decimal = Field(ge=0)
    output_per_million: Decimal = Field(ge=0)
    cached_input_per_million: Decimal | None = Field(default=None, ge=0)

    currency: str = Field(default="CNY", max_length=8)
    price_version: str = Field(default="v1", max_length=40)

    effective_from: str = Field(default_factory=utc_now_iso)
    source: str = Field(
        default="",
        max_length=300,
        description="价格来源（官网链接或公告标题），便于追溯与复核",
    )
    note: str = Field(default="", max_length=300)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.price_version)

    def covers(self, provider: str, model: str) -> bool:
        return self.provider == provider and self.model == model


class ModelCallRecord(BaseModel):
    """一次**实际 API 请求尝试**的台账记录。

    注意粒度：一次 `invoke()` 内部若重试 3 次，会产生 3 条记录。
    这样才能准确反映「请求次数预算」与实际计费次数。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=64)
    task_id: str = Field(default="", max_length=64)
    call_index: int = Field(default=1, ge=1, description="同一 task 内的第几次请求")

    occurred_at: str = Field(default_factory=utc_now_iso)
    """ISO 8601 UTC 时间"""

    stage: str = Field(default="", max_length=40)
    """业务阶段：research / copywriting / review / revision / planner / extractor"""

    provider: str = Field(default="", max_length=60)
    model: str = Field(default="", max_length=120)

    usage: Usage = Field(default_factory=Usage)

    outcome: RequestOutcome = RequestOutcome.SUCCESS
    retry_index: int = Field(default=0, ge=0, description="该次 invoke 内的第几次重试（0 起）")
    latency_ms: float = Field(default=0.0, ge=0)

    price_version: str = Field(default="", max_length=40)
    input_price_per_million: Decimal | None = Field(default=None, ge=0)
    output_price_per_million: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="CNY", max_length=8)

    estimated_cost: Decimal | None = Field(default=None, ge=0)
    billed_cost: Decimal | None = Field(default=None, ge=0)
    cost_status: CostStatus = CostStatus.UNKNOWN

    mode: str = Field(default="mock", max_length=10, description="mock / live")
    billed_ref: str = Field(default="", max_length=80, description="对账批次号")
    note: str = Field(default="", max_length=300)

    # 提示词只保留摘要，绝不存原文
    prompt_chars: int = Field(default=0, ge=0)
    prompt_sha256: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _check_mock(self):
        # Mock 不产生费用，强制为 0 且标记 complete
        if self.mode == "mock":
            object.__setattr__(self, "estimated_cost", Decimal("0"))
            object.__setattr__(self, "cost_status", CostStatus.ESTIMATED)
        return self

    def day_beijing(self) -> str:
        """按北京时间取日期 YYYY-MM-DD。"""
        return to_beijing_day(self.occurred_at)

    def cost_or_zero(self) -> Decimal:
        """统计用：未知费用按 0 参与「已确认花费」累加。

        但调用方必须同时看 `cost_status` —— 未知费用会被单独统计，
        不能只看这个数就认为总成本已知。
        """
        return self.estimated_cost or Decimal("0")


class DailyBudget(BaseModel):
    """每日预算（北京时间自然日）。

    三层预算同时生效：
    1. 单任务 API 请求硬上限（TaskBudget，既有权能）
    2. 单任务 Token 软预算（TaskBudget，既有权能）
    3. **每日**请求硬上限 + 每日 Token 软预算（本契约新增）

    **不承诺 Token 绝对不超支**：只有服务商返回 usage 时才能计量，
    请求失败那次的用量是未知的，无法参与「是否超支」的判定。
    """

    model_config = ConfigDict(extra="forbid")

    max_requests_per_day: int = Field(default=200, ge=1, le=100_000)
    max_tokens_per_day: int = Field(default=2_000_000, ge=1, le=10_000_000_000)

    warn_at_requests: float = Field(
        default=0.8, ge=0.0, le=1.0, description="请求数预警阈值（比例）"
    )
    warn_at_tokens: float = Field(default=0.8, ge=0.0, le=1.0)

    daily_cost_ceiling: Decimal | None = Field(
        default=None,
        ge=0,
        description="每日费用上限（元）。None 表示不设上限（因单价可能未知）",
    )


class DailyReport(BaseModel):
    """每日统计（北京时间）。"""

    model_config = ConfigDict(extra="forbid")

    day: str = Field(min_length=10, max_length=10, description="YYYY-MM-DD（北京时间）")

    tasks_started: int = Field(default=0, ge=0)
    tasks_succeeded: int = Field(default=0, ge=0)
    tasks_failed: int = Field(default=0, ge=0)
    tasks_partial: int = Field(default=0, ge=0)

    drafts_generated: int = Field(default=0, ge=0)

    requests_total: int = Field(default=0, ge=0)
    requests_success: int = Field(default=0, ge=0)
    requests_failed: int = Field(default=0, ge=0)

    tokens_known_total: int = Field(default=0, ge=0)
    tokens_unknown_requests: int = Field(
        default=0, ge=0, description="用量未知的请求数（不得按 0 计入 tokens_known）"
    )

    tokens_by_stage: dict[str, int] = Field(default_factory=dict)

    estimated_cost: Decimal = Field(default=Decimal("0"), ge=0)
    billed_cost: Decimal = Field(default=Decimal("0"), ge=0)
    unreconciled_cost: Decimal = Field(default=Decimal("0"), ge=0)
    unknown_cost_requests: int = Field(default=0, ge=0)

    avg_cost_per_draft: Decimal | None = Field(default=None, ge=0)

    currency: str = Field(default="CNY", max_length=8)
    generated_at: str = Field(default_factory=utc_now_iso)

    warnings: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "tasks_started": self.tasks_started,
            "tasks_succeeded": self.tasks_succeeded,
            "tasks_failed": self.tasks_failed,
            "tasks_partial": self.tasks_partial,
            "drafts_generated": self.drafts_generated,
            "requests_total": self.requests_total,
            "requests_success": self.requests_success,
            "requests_failed": self.requests_failed,
            "tokens_known_total": self.tokens_known_total,
            "tokens_unknown_requests": self.tokens_unknown_requests,
            "tokens_by_stage": dict(self.tokens_by_stage),
            "estimated_cost": str(self.estimated_cost),
            "billed_cost": str(self.billed_cost),
            "unreconciled_cost": str(self.unreconciled_cost),
            "unknown_cost_requests": self.unknown_cost_requests,
            "avg_cost_per_draft": (
                str(self.avg_cost_per_draft)
                if self.avg_cost_per_draft is not None
                else None
            ),
            "currency": self.currency,
            "generated_at": self.generated_at,
            "warnings": list(self.warnings),
        }


def to_beijing_day(iso_timestamp: str) -> str:
    """把 ISO 8601 时间戳转换为北京时间日期（YYYY-MM-DD）。"""

    dt = datetime.fromisoformat(iso_timestamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_OFFSET).strftime("%Y-%m-%d")


def beijing_now_iso() -> str:
    """当前北京时间（带 +08:00 偏移）。"""

    return datetime.now(BEIJING_OFFSET).isoformat()


def prompt_fingerprint(text: str) -> tuple[int, str]:
    """返回提示词的（字符数，sha256 前 16 位）。

    **不存原文**：台账一旦入库就长期留存，提示词可能含业务信息。
    """

    if not text:
        return (0, "")
    return (len(text), hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])


__all__ = [
    "BEIJING_OFFSET",
    "BEIJING_TZ",
    "CostStatus",
    "DailyBudget",
    "DailyReport",
    "ModelCallRecord",
    "PriceRule",
    "RequestOutcome",
    "Usage",
    "beijing_now_iso",
    "prompt_fingerprint",
    "to_beijing_day",
    "utc_now",
    "utc_now_iso",
]
