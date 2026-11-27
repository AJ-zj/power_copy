"""账单对账：CSV 导入与人工录入。

为什么要对账
------------
内部单价表算出的 `estimated_cost` 与服务商实际账单几乎一定有差异
（阶梯价、优惠、缓存计价、汇率、四舍五入方式不同）。
因此必须保留两套数字：

- `estimated_cost`：我们按单价表估的
- `billed_cost`：服务商账单说的
- `unreconciled_cost`：有估算但还没有账单的部分

**不依赖任何第三方账单 API**（未核实、可能变更、需要额外凭据）。
只支持两种输入：CSV 文件、命令行/代码里手工录入。

CSV 格式
--------
最小必需列：

```csv
request_id,amount
req-abc123,0.0123
```

可选列：`currency`、`note`。若没有 `request_id` 只有汇总金额，
则整批作为一条 reconciliation 记录（`line_count=0`），
不挂到具体请求上 —— 这种情况下无法分摊到单次调用，
但日汇总仍可用。
"""

from __future__ import annotations

import csv
import io
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from powercopy.cost.contracts import utc_now_iso
from powercopy.cost.store import CostStore


class ReconciliationError(ValueError):
    """账单导入失败。"""


def parse_bill_csv(text: str) -> tuple[list[dict], Decimal]:
    """解析账单 CSV。

    Returns:
        (明细行列表, 总金额)

    每行：`{"request_id": str, "amount": Decimal, "currency": str, "note": str}`
    """

    if not text.strip():
        raise ReconciliationError("CSV 内容为空")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ReconciliationError("CSV 缺少表头")

    amount_column = None
    for candidate in ("amount", "cost", "billed_cost", "金额", "费用"):
        if candidate in reader.fieldnames:
            amount_column = candidate
            break

    if amount_column is None:
        raise ReconciliationError(
            f"CSV 缺少金额列。可用列名：amount / cost / billed_cost / 金额 / 费用。"
            f"实际表头：{reader.fieldnames}"
        )

    request_column = None
    for candidate in ("request_id", "requestid", "id", "请求ID"):
        if candidate in reader.fieldnames:
            request_column = candidate
            break

    lines: list[dict] = []
    total = Decimal("0")

    for index, row in enumerate(reader, start=2):
        raw = (row.get(amount_column) or "").strip()
        if not raw:
            continue
        try:
            amount = Decimal(raw)
        except InvalidOperation as exc:
            raise ReconciliationError(
                f"第 {index} 行金额无法解析为数字：{raw!r}"
            ) from exc

        if amount < 0:
            raise ReconciliationError(
                f"第 {index} 行金额为负：{raw}。"
                f"退款请单独记录，不要混入正向对账"
            )

        lines.append(
            {
                "request_id": (
                    (row.get(request_column) or "").strip()
                    if request_column
                    else ""
                ),
                "amount": amount,
                "currency": (row.get("currency") or "CNY").strip() or "CNY",
                "note": (row.get("note") or "").strip(),
            }
        )
        total += amount

    if not lines:
        raise ReconciliationError("CSV 中没有可用的数据行")

    return lines, total


class Reconciler:
    """账单对账器。

    用法：

    ```python
    reconciler = Reconciler(store)

    # 从 CSV 文件导入
    ref = reconciler.import_csv("bill-2026-11.csv")

    # 或手工录入单笔
    ref = reconciler.import_manual(
        total=Decimal("12.34"),
        note="服务商后台手工核对",
    )
    ```
    """

    def __init__(self, store: CostStore):
        self.store = store

    def import_csv(
        self,
        path: str | Path,
        *,
        source: str = "",
        billed_ref: str | None = None,
    ) -> dict:
        """从 CSV 文件导入账单并标记对应请求。

        Returns:
            {"billed_ref": ..., "total": str, "matched": int, "unmatched": int}
        """

        csv_path = Path(path)
        if not csv_path.is_file():
            raise ReconciliationError(f"账单文件不存在：{csv_path}")

        text = csv_path.read_text(encoding="utf-8-sig")
        lines, total = parse_bill_csv(text)

        ref = billed_ref or f"bill-{uuid.uuid4().hex[:12]}"

        matched_ids = [
            line["request_id"] for line in lines if line["request_id"]
        ]
        amounts = {
            line["request_id"]: line["amount"]
            for line in lines
            if line["request_id"]
        }
        matched = 0
        if matched_ids:
            matched = self.store.mark_billed(matched_ids, ref, amounts)

        currency = lines[0]["currency"] if lines else "CNY"

        self.store.record_reconciliation(
            ref,
            source=source or csv_path.name,
            total_amount=total,
            line_count=len(lines),
            currency=currency,
            note=f"CSV 导入；匹配 {matched}/{len(matched_ids)} 条请求",
            detail={
                "lines": [
                    {
                        "request_id": line["request_id"],
                        "amount": str(line["amount"]),
                        "currency": line["currency"],
                    }
                    for line in lines[:200]
                ]
            },
        )

        return {
            "billed_ref": ref,
            "total": str(total),
            "currency": currency,
            "line_count": len(lines),
            "matched": matched,
            "unmatched": max(0, len(matched_ids) - matched),
        }

    def import_manual(
        self,
        *,
        total: Decimal,
        note: str = "",
        source: str = "manual",
        currency: str = "CNY",
        request_ids: list[str] | None = None,
        billed_ref: str | None = None,
    ) -> dict:
        """手工录入一笔对账。

        `request_ids` 为空时只记批次总额，不挂到具体请求。
        """

        if total < 0:
            raise ReconciliationError("金额不能为负")

        ref = billed_ref or f"bill-{uuid.uuid4().hex[:12]}"

        matched = 0
        if request_ids:
            matched = self.store.mark_billed(request_ids, ref)

        self.store.record_reconciliation(
            ref,
            source=source,
            total_amount=total,
            line_count=len(request_ids or []),
            currency=currency,
            note=note or "人工录入",
            detail={"request_ids": request_ids or []},
        )

        return {
            "billed_ref": ref,
            "total": str(total),
            "currency": currency,
            "line_count": len(request_ids or []),
            "matched": matched,
            "unmatched": 0,
        }

    def summary(self) -> dict:
        """对账总览：账单总额、未对账估算、缺口。"""

        reconciliations = self.store.reconciliations()
        billed_total = sum(
            (Decimal(row["total_amount"]) for row in reconciliations),
            Decimal("0"),
        )

        calls = self.store.all_calls(limit=100_000)
        estimated_total = sum(
            (c.estimated_cost for c in calls if c.estimated_cost is not None),
            Decimal("0"),
        )
        billed_on_calls = sum(
            (c.billed_cost for c in calls if c.billed_cost is not None),
            Decimal("0"),
        )
        unreconciled = sum(
            (
                c.estimated_cost
                for c in calls
                if c.billed_cost is None and c.estimated_cost is not None
            ),
            Decimal("0"),
        )

        return {
            "reconciliations": len(reconciliations),
            "billed_total": str(billed_total),
            "estimated_total": str(estimated_total),
            "billed_on_calls": str(billed_on_calls),
            "unreconciled_estimated": str(unreconciled),
            "variance": str(billed_total - estimated_total),
            "generated_at": utc_now_iso(),
        }


__all__ = [
    "Reconciler",
    "ReconciliationError",
    "parse_bill_csv",
]
