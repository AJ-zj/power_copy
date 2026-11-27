"""Token 计费与成本控制（MVP-05-B 补充）。

模块
----
contracts    数据契约（ModelCallRecord / PriceRule / DailyBudget / DailyReport）
pricing      单价表与费用计算（Decimal）
store        SQLite 台账（模型调用明细 + 对账记录）
daily        每日预算与每日统计
recorder     把 LLMClient 的实际请求写入台账
reconcile    账单 CSV 导入与人工录入
ledger       门面（CostAccounting）

设计要点
--------
1. **未知即未知，不得填零。** 模型不返回 usage 时，`total_tokens` 记为
   `None`，`usage_complete=False`，`cost_status="unknown"`。
   绝不把未知用量当成 0 消耗。

2. **金额一律用 Decimal。** 浮点数在货币累加时会出现
   `0.1 + 0.2 != 0.3` 这类误差，日报金额必须精确。

3. **区分三种费用状态：**
   `estimated`（按单价表估算）/ `billed`（服务商账单已导入）/
   `unreconciled`（有估算但尚无账单）。三者不混用。

4. **未知价格不得套用其他平台。** 单价的 key 是
   `(provider, model, price_version)`，查不到就返回 None，
   进而让费用状态为 `unknown`。

5. **不记录密钥与完整提示词。** 台账只存摘要性字段，
   提示词仅存长度与哈希。
"""

from powercopy.cost.contracts import (
    CostStatus,
    DailyBudget,
    DailyReport,
    ModelCallRecord,
    PriceRule,
    RequestOutcome,
    Usage,
)
from powercopy.cost.daily import (
    DailyBudgetGuard,
    build_daily_report,
    calls_to_csv,
    report_to_csv,
    report_to_markdown,
)
from powercopy.cost.ledger import (
    CostAccounting,
    load_price_table,
)
from powercopy.cost.pricing import PriceTable, compute_cost
from powercopy.cost.recorder import CallRecorder
from powercopy.cost.reconcile import Reconciler
from powercopy.cost.store import CostStore

__all__ = [
    "CallRecorder",
    "CostAccounting",
    "CostStatus",
    "CostStore",
    "DailyBudget",
    "DailyBudgetGuard",
    "DailyReport",
    "ModelCallRecord",
    "PriceRule",
    "PriceTable",
    "Reconciler",
    "RequestOutcome",
    "Usage",
    "build_daily_report",
    "calls_to_csv",
    "compute_cost",
    "load_price_table",
    "report_to_csv",
    "report_to_markdown",
]
