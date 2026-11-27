"""成本台账的统一入口：把记录器、预算守卫、日报串起来。

`CostAccounting` 是给业务层（CLI / workbench）用的门面，
避免各处重复拼装 PriceTable / CostStore / Recorder。

**默认不启用**：只有显式构造并传入 `cost_hook` 才会记录，
这样测试与离线脚本不会被迫建库。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from powercopy.cost.contracts import (
    DailyBudget,
    ModelCallRecord,
    PriceRule,
    to_beijing_day,
    utc_now_iso,
)
from powercopy.cost.daily import (
    DailyBudgetGuard,
    build_daily_report,
    calls_to_csv,
    report_to_csv,
    report_to_markdown,
)
from powercopy.cost.pricing import PriceTable
from powercopy.cost.recorder import CallRecorder
from powercopy.cost.reconcile import Reconciler
from powercopy.cost.store import CostStore


def default_price_table() -> PriceTable:
    """内置单价表。

    **刻意留空。** 单价必须由使用者显式配置，原因：
    1. 未经核实的单价写死在代码里会被误当成权威数据
    2. 不同平台/账号/协议的折扣不同
    3. 价格会变，写死后过期无感知

    通过 `config/pricing.example.json` 提供样例格式，
    实际单价由运维从服务商官网核实后填入。
    """

    return PriceTable()


def load_price_table(path: str | Path) -> PriceTable:
    """从 JSON 文件加载单价表。

    格式：

    ```json
    {
      "currency": "CNY",
      "rules": [
        {
          "provider": "example-provider",
          "model": "example-model",
          "input_per_million": "1.00",
          "output_per_million": "2.00",
          "price_version": "v1",
          "source": "服务商官网定价页（填写核实日期）"
        }
      ]
    }
    ```
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    currency = payload.get("currency", "CNY")

    rules = []
    for item in payload.get("rules") or []:
        rules.append(
            PriceRule(
                provider=item["provider"],
                model=item["model"],
                input_per_million=Decimal(str(item["input_per_million"])),
                output_per_million=Decimal(str(item["output_per_million"])),
                cached_input_per_million=(
                    Decimal(str(item["cached_input_per_million"]))
                    if item.get("cached_input_per_million") is not None
                    else None
                ),
                currency=item.get("currency", currency),
                price_version=item.get("price_version", "v1"),
                effective_from=item.get("effective_from", utc_now_iso()),
                source=item.get("source", ""),
                note=item.get("note", ""),
            )
        )

    return PriceTable(rules)


class CostAccounting:
    """成本核算门面。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        price_table: PriceTable | None = None,
        daily_budget: DailyBudget | None = None,
        mode: str = "live",
        provider: str = "",
        model: str = "",
    ):
        self.store = CostStore(db_path)
        self.price_table = price_table or default_price_table()
        self.budget = daily_budget or DailyBudget()
        self.guard = DailyBudgetGuard(self.budget, self.store)
        self.reconciler = Reconciler(self.store)
        self.mode = mode
        self.provider = provider
        self.model = model

    # ---- 记录器 ----

    def recorder_for(self, task_id: str, stage: str) -> CallRecorder:
        return CallRecorder(
            self.store,
            self.price_table,
            task_id=task_id,
            stage=stage,
            mode=self.mode,
            provider=self.provider,
            model=self.model,
        )

    # ---- 日报 ----

    def daily_report(self, day: str | None = None):
        day = day or to_beijing_day(utc_now_iso())
        return build_daily_report(self.store, day, budget=self.budget)

    def export_markdown(self, day: str | None = None) -> str:
        return report_to_markdown(self.daily_report(day))

    def export_csv(self, day: str | None = None) -> str:
        return report_to_csv(self.daily_report(day))

    def export_calls_csv(self, day: str | None = None) -> str:
        day = day or to_beijing_day(utc_now_iso())
        return calls_to_csv(self.store.calls_for_day(day))

    def task_outcome(
        self,
        task_id: str,
        final_status: str,
        *,
        drafts: int = 0,
        day: str | None = None,
    ) -> None:
        self.store.record_task_outcome(
            task_id,
            day or to_beijing_day(utc_now_iso()),
            final_status,
            drafts=drafts,
            mode=self.mode,
        )

    # ---- 对账 ----

    def import_bill(self, path: str | Path, **kwargs) -> dict:
        return self.reconciler.import_csv(path, **kwargs)

    def manual_bill(self, total: Decimal, **kwargs) -> dict:
        return self.reconciler.import_manual(total=total, **kwargs)

    def reconciliation_summary(self) -> dict:
        return self.reconciler.summary()


__all__ = [
    "CostAccounting",
    "default_price_table",
    "load_price_table",
]
