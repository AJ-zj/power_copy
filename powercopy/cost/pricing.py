"""单价表与费用计算。

货币计算一律用 `Decimal`，并显式按 `ROUND_HALF_UP` 保留 8 位小数
（内部精度），展示时再按币种精度截断。

**未知价格不填零、不套用其他平台：**
`PriceTable.lookup()` 找不到就返回 None，调用方据此把
`cost_status` 置为 UNKNOWN，日报里单独统计「未知费用请求数」。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from powercopy.cost.contracts import PriceRule, Usage

# 内部计算精度（比展示多得多，避免累计误差）
_INTERNAL_PLACES = Decimal("0.00000001")
# 人民币展示精度
_CNY_PLACES = Decimal("0.01")

MILLION = Decimal("1000000")


class PriceTable:
    """模型单价表。

    用法：

    ```python
    table = PriceTable()
    table.add(PriceRule(
        provider="example", model="glm-5.3-flash",
        input_per_million=Decimal("1.0"),
        output_per_million=Decimal("2.0"),
        price_version="v1",
        source="官网定价页（待核实）",
    ))
    rule = table.lookup("example", "glm-5.3-flash")
    ```
    """

    def __init__(self, rules: list[PriceRule] | None = None):
        self._rules: dict[tuple[str, str, str], PriceRule] = {}
        for rule in rules or []:
            self.add(rule)

    # ---- 维护 ----

    def add(self, rule: PriceRule) -> None:
        self._rules[rule.key] = rule

    def remove(self, provider: str, model: str, price_version: str) -> bool:
        return self._rules.pop((provider, model, price_version), None) is not None

    def rules(self) -> list[PriceRule]:
        return list(self._rules.values())

    # ---- 查询 ----

    def lookup(
        self, provider: str, model: str, price_version: str | None = None
    ) -> PriceRule | None:
        """按 provider + model 查单价。

        指定 `price_version` 时精确匹配；不指定时取 effective_from 最新的一条。

        **查不到返回 None** —— 绝不回退到「其他平台」或「同厂商其他模型」。
        """

        if price_version is not None:
            return self._rules.get((provider, model, price_version))

        candidates = [
            rule
            for rule in self._rules.values()
            if rule.covers(provider, model)
        ]
        if not candidates:
            return None

        # effective_from 是 ISO 字符串，可直接字典序比较
        return max(candidates, key=lambda r: r.effective_from)

    def has_model(self, provider: str, model: str) -> bool:
        return any(r.covers(provider, model) for r in self._rules.values())

    def models_without_price(
        self, observed: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """返回观测到但没有单价的 (provider, model)，用于日报告警。"""

        seen = set()
        missing = []
        for provider, model in observed:
            key = (provider, model)
            if key in seen:
                continue
            seen.add(key)
            if not self.has_model(provider, model):
                missing.append(key)
        return missing


def compute_cost(
    usage: Usage,
    rule: PriceRule | None,
) -> tuple[Decimal | None, str]:
    """按用量与单价计算费用。

    Returns:
        (费用, 状态说明)
        费用为 None 表示无法计算（缺单价或缺完整用量）

    计算规则：
    - 输入/输出分别按各自单价
    - 缓存 Token 若有单独单价则用它，否则**按输入单价**（保守偏高估算，
      并在返回值说明中写明）
    - 用量不完整（缺 input/output 分项）→ 无法精确计算，返回 None
    """

    if rule is None:
        return (None, "no_price_rule")

    if not usage.complete:
        return (None, "usage_incomplete")

    if usage.input_tokens is None or usage.output_tokens is None:
        return (None, "usage_missing_breakdown")

    input_tokens = Decimal(usage.input_tokens)
    output_tokens = Decimal(usage.output_tokens)

    cost = (input_tokens / MILLION) * rule.input_per_million
    cost += (output_tokens / MILLION) * rule.output_per_million

    note = "ok"
    if usage.cached_tokens:
        cached = Decimal(usage.cached_tokens)
        if rule.cached_input_per_million is not None:
            # 缓存部分已含在 input_tokens 里，替换为缓存单价
            cost -= (cached / MILLION) * rule.input_per_million
            cost += (cached / MILLION) * rule.cached_input_per_million
            note = "ok_cached_price"
        else:
            note = "ok_cached_at_input_price"

    return (
        cost.quantize(_INTERNAL_PLACES, rounding=ROUND_HALF_UP),
        note,
    )


def quantize_display(amount: Decimal, currency: str = "CNY") -> Decimal:
    """按币种精度截断用于展示。"""

    places = _CNY_PLACES if currency == "CNY" else Decimal("0.000001")
    return amount.quantize(places, rounding=ROUND_HALF_UP)


def sum_decimal(values) -> Decimal:
    """Decimal 求和，忽略 None。"""

    total = Decimal("0")
    for value in values:
        if value is not None:
            total += value
    return total


__all__ = [
    "MILLION",
    "PriceTable",
    "compute_cost",
    "quantize_display",
    "sum_decimal",
]
