"""每日预算控制与每日统计。

每日预算
--------
在**实际请求发出前**检查。检查与预占必须是一个原子动作，
否则并发任务会同时通过检查然后一起发出请求。

本项目是单机 CLI/工作台，用 SQLite 事务做「检查 + 预占」即可：

```sql
BEGIN IMMEDIATE;
  SELECT COUNT(*) FROM model_calls WHERE beijing_day=?;
  -- 若未超限，插入一条占位记录（outcome='budget_blocked' 的反面）
COMMIT;
```

但更简单可靠的做法是：**在请求前读当日计数，若已超限则拒绝**，
并在请求后写记录。并发窗口很小，且超限的后果只是多发 1-2 个请求
（有单任务硬上限兜底），可接受。

因此这里采用**乐观检查**，并在文档中如实说明这个边界。

重试也计入每日请求数 —— 因为服务商也是按请求计费的。
"""

from __future__ import annotations

from decimal import Decimal

from powercopy.cost.contracts import (
    CostStatus,
    DailyBudget,
    DailyReport,
    ModelCallRecord,
    RequestOutcome,
    to_beijing_day,
    utc_now_iso,
)
from powercopy.cost.pricing import sum_decimal
from powercopy.cost.store import CostStore


class BudgetWarning:
    """预算预警。"""

    def __init__(self, kind: str, message: str, ratio: float | None = None):
        self.kind = kind
        self.message = message
        self.ratio = ratio

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "ratio": self.ratio}

    def __repr__(self) -> str:
        return f"BudgetWarning({self.kind}: {self.message})"


class DailyBudgetGuard:
    """每日预算守卫。

    **不承诺 Token 绝对不超支。** 原因：
    - 只有服务商返回 usage 时才能计量已知消耗
    - 请求失败那次的用量是未知的（记为 unknown，不按 0 处理）
    - 因此「已知 Token」永远可能低估真实消耗

    请求次数是**硬约束**：每次尝试（含重试）都计数，超限即拒绝。
    """

    def __init__(self, budget: DailyBudget, store: CostStore):
        self.budget = budget
        self.store = store

    # ---- 检查 ----

    def current_usage(self, day: str | None = None) -> dict:
        day = day or to_beijing_day(utc_now_iso())
        records = self.store.calls_for_day(day)

        # 只统计「真实发出的请求」，budget_blocked 不算（它没发出）
        real = [
            r for r in records if r.outcome is not RequestOutcome.BUDGET_BLOCKED
        ]
        known_requests = len(real)
        known_tokens = sum(
            (r.usage.total_tokens or 0) for r in real
        )
        unknown_tokens_requests = sum(
            1 for r in real if not r.usage.complete
        )

        return {
            "day": day,
            "requests": known_requests,
            "known_tokens": known_tokens,
            "unknown_token_requests": unknown_tokens_requests,
        }

    def check(self, *, upcoming_tokens_hint: int | None = None) -> list[BudgetWarning]:
        """在请求发出前检查。

        Returns:
            预警列表。**空列表不代表一定够用** —— Token 是软预算。

        Raises:
            BudgetExceededError: 请求次数已达硬上限
        """

        usage = self.current_usage()
        warnings: list[BudgetWarning] = []

        # ---- 请求次数：硬上限 ----
        if usage["requests"] >= self.budget.max_requests_per_day:
            raise DailyBudgetExceeded(
                "daily_request_budget_exhausted",
                f"今日请求数已达上限 "
                f"（{usage['requests']}/{self.budget.max_requests_per_day}）",
            )

        ratio = usage["requests"] / self.budget.max_requests_per_day
        if ratio >= self.budget.warn_at_requests:
            warnings.append(
                BudgetWarning(
                    "requests_near_limit",
                    f"今日请求数已达 {ratio:.0%}"
                    f"（{usage['requests']}/{self.budget.max_requests_per_day}）",
                    ratio,
                )
            )

        # ---- Token：软预算 ----
        if usage["known_tokens"] >= self.budget.max_tokens_per_day:
            raise DailyBudgetExceeded(
                "daily_token_budget_exhausted",
                f"今日已知 Token 已达上限 "
                f"（{usage['known_tokens']}/{self.budget.max_tokens_per_day}）。"
                f"注意：还有 {usage['unknown_token_requests']} 次请求用量未知，"
                f"真实消耗可能更高",
            )

        token_ratio = usage["known_tokens"] / self.budget.max_tokens_per_day
        if token_ratio >= self.budget.warn_at_tokens:
            warnings.append(
                BudgetWarning(
                    "tokens_near_limit",
                    f"今日已知 Token 已达 {token_ratio:.0%}"
                    f"（{usage['known_tokens']}/{self.budget.max_tokens_per_day}），"
                    f"另有 {usage['unknown_token_requests']} 次请求用量未知",
                    token_ratio,
                )
            )

        if usage["unknown_token_requests"]:
            warnings.append(
                BudgetWarning(
                    "tokens_usage_incomplete",
                    f"今日有 {usage['unknown_token_requests']} 次请求未返回用量，"
                    f"这些消耗未计入已知 Token；无法保证不超出 Token 上限",
                )
            )

        if upcoming_tokens_hint is not None:
            projected = usage["known_tokens"] + upcoming_tokens_hint
            if projected > self.budget.max_tokens_per_day:
                warnings.append(
                    BudgetWarning(
                        "tokens_projected_over",
                        f"按预估本次约 {upcoming_tokens_hint} tokens，"
                        f"今日总量预计达 {projected}，可能超出上限",
                    )
                )

        return warnings

    def remaining_requests(self) -> int:
        usage = self.current_usage()
        return max(0, self.budget.max_requests_per_day - usage["requests"])

    def remaining_tokens(self) -> int:
        usage = self.current_usage()
        return max(0, self.budget.max_tokens_per_day - usage["known_tokens"])


class DailyBudgetExceeded(RuntimeError):
    """每日预算耗尽。"""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


# ---------------------------------------------------------------- 日报


def build_daily_report(
    store: CostStore,
    day: str,
    *,
    budget: DailyBudget | None = None,
) -> DailyReport:
    """生成指定日期的统计（北京时间）。

    统计口径说明：
    - `tokens_known_total` 只累加**用量完整**的请求
    - `tokens_unknown_requests` 单独计数，不混入 token 总计
    - 费用分三档：estimated / billed / unreconciled
    - 平均单篇成本 = 已确认费用 / 生成文案数；无文案时为 None
    """

    records = store.calls_for_day(day)
    outcomes = store.task_outcomes_for_day(day)

    # ---- 任务统计 ----
    tasks_started = len(outcomes)
    tasks_succeeded = sum(
        1 for o in outcomes if o["final_status"] == "pending_review"
    )
    tasks_failed = sum(
        1 for o in outcomes if o["final_status"] in ("failed", "aborted")
    )
    tasks_partial = sum(
        1
        for o in outcomes
        if o["final_status"] in ("draft_ready", "needs_manual_review",
                                 "budget_exhausted")
    )
    drafts = sum(int(o["drafts"] or 0) for o in outcomes)

    # ---- 请求统计 ----
    real_records = [
        r for r in records if r.outcome is not RequestOutcome.BUDGET_BLOCKED
    ]
    requests_total = len(real_records)
    requests_success = sum(
        1 for r in real_records if r.outcome is RequestOutcome.SUCCESS
    )
    requests_failed = requests_total - requests_success

    # ---- Token ----
    tokens_known = sum(
        (r.usage.total_tokens or 0)
        for r in real_records
        if r.usage.complete
    )
    tokens_unknown_requests = sum(
        1 for r in real_records if not r.usage.complete
    )

    tokens_by_stage: dict[str, int] = {}
    for record in real_records:
        if not record.usage.complete or record.usage.total_tokens is None:
            continue
        stage = record.stage or "unknown"
        tokens_by_stage[stage] = (
            tokens_by_stage.get(stage, 0) + record.usage.total_tokens
        )

    # ---- 费用 ----
    estimated = Decimal("0")
    billed = Decimal("0")
    unreconciled = Decimal("0")
    unknown_cost_requests = 0
    currency = "CNY"

    for record in real_records:
        currency = record.currency or currency
        if record.cost_status is CostStatus.BILLED and record.billed_cost is not None:
            billed += record.billed_cost
            estimated += record.estimated_cost or Decimal("0")
        elif record.estimated_cost is not None:
            estimated += record.estimated_cost
            if record.cost_status in (CostStatus.ESTIMATED, CostStatus.UNRECONCILED):
                unreconciled += record.estimated_cost
        else:
            unknown_cost_requests += 1

    avg_cost = None
    if drafts > 0:
        # 用已确认费用（billed 优先，否则 estimated）
        confirmed = billed if billed > 0 else estimated
        avg_cost = (confirmed / Decimal(drafts)).quantize(Decimal("0.000001"))

    # ---- 预警 ----
    warnings: list[str] = []
    if tokens_unknown_requests:
        warnings.append(
            f"{tokens_unknown_requests} 次请求未返回用量，"
            f"这些消耗未计入 Token 统计，真实消耗可能更高"
        )
    if unknown_cost_requests:
        warnings.append(
            f"{unknown_cost_requests} 次请求无法计算费用"
            f"（缺单价或缺完整用量），已在 unknown_cost_requests 单独计数"
        )
    if budget is not None:
        if requests_total >= budget.max_requests_per_day:
            warnings.append(
                f"今日请求数已达上限（{requests_total}/"
                f"{budget.max_requests_per_day}）"
            )
        elif requests_total >= budget.max_requests_per_day * budget.warn_at_requests:
            warnings.append(
                f"今日请求数已达 {requests_total / budget.max_requests_per_day:.0%}"
            )
        if tokens_known >= budget.max_tokens_per_day:
            warnings.append(
                f"今日已知 Token 已达上限（{tokens_known}/"
                f"{budget.max_tokens_per_day}）"
            )
        elif tokens_known >= budget.max_tokens_per_day * budget.warn_at_tokens:
            warnings.append(
                f"今日已知 Token 已达 {tokens_known / budget.max_tokens_per_day:.0%}"
            )

    return DailyReport(
        day=day,
        tasks_started=tasks_started,
        tasks_succeeded=tasks_succeeded,
        tasks_failed=tasks_failed,
        tasks_partial=tasks_partial,
        drafts_generated=drafts,
        requests_total=requests_total,
        requests_success=requests_success,
        requests_failed=requests_failed,
        tokens_known_total=tokens_known,
        tokens_unknown_requests=tokens_unknown_requests,
        tokens_by_stage=tokens_by_stage,
        estimated_cost=estimated,
        billed_cost=billed,
        unreconciled_cost=unreconciled,
        unknown_cost_requests=unknown_cost_requests,
        avg_cost_per_draft=avg_cost,
        currency=currency,
        warnings=warnings,
    )


# ---------------------------------------------------------------- 导出


def report_to_markdown(report: DailyReport) -> str:
    """导出 Markdown 日报。"""

    lines = [
        f"# Token 与费用日报 — {report.day}（北京时间）",
        "",
        f"> 生成时间：{report.generated_at}  ",
        f"> 币种：{report.currency}",
        "",
        "## 任务",
        "",
        "| 指标 | 数量 |",
        "| --- | --- |",
        f"| 运行任务 | {report.tasks_started} |",
        f"| 成功（待审核） | {report.tasks_succeeded} |",
        f"| 失败/中止 | {report.tasks_failed} |",
        f"| 部分完成 | {report.tasks_partial} |",
        f"| 生成文案 | {report.drafts_generated} |",
        "",
        "## 请求",
        "",
        "| 指标 | 数量 |",
        "| --- | --- |",
        f"| 请求总数（含重试） | {report.requests_total} |",
        f"| 成功 | {report.requests_success} |",
        f"| 失败 | {report.requests_failed} |",
        "",
        "## Token",
        "",
        "| 指标 | 数量 |",
        "| --- | --- |",
        f"| 已知 Token（仅完整用量） | {report.tokens_known_total} |",
        f"| 用量未知的请求数 | {report.tokens_unknown_requests} |",
        "",
    ]

    if report.tokens_by_stage:
        lines.extend(
            [
                "### 各阶段 Token",
                "",
                "| 阶段 | Token |",
                "| --- | --- |",
            ]
        )
        for stage, tokens in sorted(report.tokens_by_stage.items()):
            lines.append(f"| {stage} | {tokens} |")
        lines.append("")

    lines.extend(
        [
            "## 费用",
            "",
            "| 指标 | 金额 |",
            "| --- | --- |",
            f"| 预计费用 | {report.estimated_cost} |",
            f"| 已对账（账单）费用 | {report.billed_cost} |",
            f"| 未对账费用 | {report.unreconciled_cost} |",
            f"| 无法计算的请求数 | {report.unknown_cost_requests} |",
            f"| 平均单篇成本 | "
            f"{report.avg_cost_per_draft if report.avg_cost_per_draft is not None else '—'} |",
            "",
        ]
    )

    if report.warnings:
        lines.extend(["## 预警", ""])
        for warning in report.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "**口径说明**",
            "",
            "- `已知 Token` 只累加服务商返回完整用量的请求。",
            "- 用量未知的请求单独计数，**不按 0 消耗处理**。",
            "- 预计费用按内部单价表计算，可能与服务商账单存在差异。",
            "- 已对账费用以导入的账单为准。",
            "- **Token 是软预算**：只有服务商返回 usage 时才能计量，"
            "无法保证绝对不超支。",
        ]
    )

    return "\n".join(lines)


def report_to_csv(report: DailyReport) -> str:
    """导出 CSV 日报（单行指标）。"""

    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "day", "tasks_started", "tasks_succeeded", "tasks_failed",
            "tasks_partial", "drafts_generated", "requests_total",
            "requests_success", "requests_failed", "tokens_known_total",
            "tokens_unknown_requests", "estimated_cost", "billed_cost",
            "unreconciled_cost", "unknown_cost_requests",
            "avg_cost_per_draft", "currency",
        ]
    )
    writer.writerow(
        [
            report.day,
            report.tasks_started,
            report.tasks_succeeded,
            report.tasks_failed,
            report.tasks_partial,
            report.drafts_generated,
            report.requests_total,
            report.requests_success,
            report.requests_failed,
            report.tokens_known_total,
            report.tokens_unknown_requests,
            report.estimated_cost,
            report.billed_cost,
            report.unreconciled_cost,
            report.unknown_cost_requests,
            report.avg_cost_per_draft if report.avg_cost_per_draft is not None else "",
            report.currency,
        ]
    )
    return buffer.getvalue()


def calls_to_csv(records: list[ModelCallRecord]) -> str:
    """导出调用明细 CSV（不含任何提示词原文）。"""

    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "request_id", "task_id", "call_index", "occurred_at",
            "beijing_day", "stage", "provider", "model",
            "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
            "usage_complete", "outcome", "retry_index", "latency_ms",
            "price_version", "estimated_cost", "billed_cost", "cost_status",
            "mode", "billed_ref", "prompt_chars", "prompt_sha256",
        ]
    )
    for record in records:
        writer.writerow(
            [
                record.request_id,
                record.task_id,
                record.call_index,
                record.occurred_at,
                record.day_beijing(),
                record.stage,
                record.provider,
                record.model,
                "" if record.usage.input_tokens is None else record.usage.input_tokens,
                "" if record.usage.output_tokens is None else record.usage.output_tokens,
                "" if record.usage.cached_tokens is None else record.usage.cached_tokens,
                "" if record.usage.total_tokens is None else record.usage.total_tokens,
                1 if record.usage.complete else 0,
                str(record.outcome),
                record.retry_index,
                record.latency_ms,
                record.price_version,
                "" if record.estimated_cost is None else record.estimated_cost,
                "" if record.billed_cost is None else record.billed_cost,
                str(record.cost_status),
                record.mode,
                record.billed_ref,
                record.prompt_chars,
                record.prompt_sha256,
            ]
        )
    return buffer.getvalue()


__all__ = [
    "BudgetWarning",
    "DailyBudgetExceeded",
    "DailyBudgetGuard",
    "build_daily_report",
    "calls_to_csv",
    "report_to_csv",
    "report_to_markdown",
    "sum_decimal",
]
