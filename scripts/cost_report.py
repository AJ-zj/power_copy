"""成本台账 CLI：日报、明细、对账。

用法
----
    # 今日日报（Markdown）
    python scripts/cost_report.py

    # 指定日期，导出 CSV
    python scripts/cost_report.py --day 2026-11-27 --format csv

    # 导出调用明细
    python scripts/cost_report.py --day 2026-11-27 --detail

    # 导入账单对账
    python scripts/cost_report.py --import-bill bill.csv

    # 手工录入对账
    python scripts/cost_report.py --manual-bill 12.34 --note "服务商后台核对"

    # 对账总览
    python scripts/cost_report.py --reconciliation

安全说明
--------
- 只读本地 SQLite，不联网
- 导出内容不含提示词原文与任何密钥
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.cost import (  # noqa: E402
    CostAccounting,
    DailyBudget,
    load_price_table,
)
from powercopy.cost.contracts import to_beijing_day, utc_now_iso  # noqa: E402

DEFAULT_DB = ROOT / "storage" / "cost" / "cost.db"
DEFAULT_PRICING = ROOT / "config" / "pricing.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Token 与费用日报")
    parser.add_argument("--day", default=None, help="YYYY-MM-DD（北京时间），默认今天")
    parser.add_argument(
        "--format", default="markdown", choices=["markdown", "csv"],
        help="日报格式",
    )
    parser.add_argument("--detail", action="store_true", help="输出调用明细 CSV")
    parser.add_argument("--out", type=Path, help="写入文件而不是打印")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    parser.add_argument("--import-bill", type=Path, help="导入账单 CSV")
    parser.add_argument("--manual-bill", type=str, help="手工录入金额")
    parser.add_argument("--note", default="", help="对账备注")
    parser.add_argument("--reconciliation", action="store_true", help="对账总览")
    parser.add_argument("--list-days", action="store_true", help="列出有记录的日期")
    args = parser.parse_args()

    price_table = None
    if args.pricing.is_file():
        try:
            price_table = load_price_table(args.pricing)
        except Exception as exc:
            print(f"单价配置加载失败：{exc}", file=sys.stderr)
            return 2
    else:
        print(
            f"提示：未找到单价配置 {args.pricing}，"
            f"费用将全部记为 unknown（不填零、不套用其他平台价格）",
            file=sys.stderr,
        )

    accounting = CostAccounting(
        args.db,
        price_table=price_table,
        daily_budget=DailyBudget(),
        mode="live",
    )

    # ---- 对账操作 ----
    if args.import_bill is not None:
        try:
            result = accounting.import_bill(args.import_bill)
        except Exception as exc:
            print(f"账单导入失败：{exc}", file=sys.stderr)
            return 2
        print("账单导入完成：")
        for key, value in result.items():
            print(f"  {key:14} {value}")
        return 0

    if args.manual_bill is not None:
        try:
            total = Decimal(args.manual_bill)
        except InvalidOperation:
            print(f"金额格式错误：{args.manual_bill}", file=sys.stderr)
            return 2
        result = accounting.manual_bill(total, note=args.note)
        print("手工对账已录入：")
        for key, value in result.items():
            print(f"  {key:14} {value}")
        return 0

    if args.reconciliation:
        summary = accounting.reconciliation_summary()
        print("对账总览：")
        for key, value in summary.items():
            print(f"  {key:24} {value}")
        print()
        print("说明：variance = 账单总额 - 内部估算总额。")
        print("      为负表示账单低于估算，为正表示估算偏低。")
        return 0

    if args.list_days:
        days = accounting.store.available_days()
        if not days:
            print("暂无记录")
        else:
            for day in days:
                count = len(accounting.store.calls_for_day(day))
                print(f"  {day}  {count} 条请求")
        return 0

    # ---- 日报 ----
    day = args.day or to_beijing_day(utc_now_iso())

    if args.detail:
        content = accounting.export_calls_csv(day)
        filename = f"cost-detail-{day}.csv"
    elif args.format == "csv":
        content = accounting.export_csv(day)
        filename = f"cost-report-{day}.csv"
    else:
        content = accounting.export_markdown(day)
        filename = f"cost-report-{day}.md"

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(content, encoding="utf-8")
        print(f"已写入：{args.out}")
    else:
        print(content)
        if args.format == "markdown" and not args.detail:
            print()
            print(f"（如需导出：--out reports/{filename}）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
