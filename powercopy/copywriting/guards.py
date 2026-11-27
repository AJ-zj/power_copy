"""文案生成的安全约束。

两类检查
--------
**1. 引用校验（`validate_citations`）** —— 程序可可靠判定：

- 每个 `key_points[].evidence_ids` 必须存在于证据包
- 引用的 evidence 必须来自**未被拒绝**的 claim（`ClaimStatus.REJECTED`
  的 claim 所引用的 evidence 不得使用）
- 不得引用不存在的 evidence ID

**2. 禁用内容检测（`check_forbidden_content`）** —— 启发式，**有局限**：

检测高风险表述：企业服务承诺、报价、客户案例、节省比例、编造的政策
条款/有效期/适用地区。

实现方式是**模式匹配 + 证据回溯**：命中风险模式后，检查该片段是否
能在证据包原文中找到依据；找不到就标记为「无依据」。

**必须诚实说明的局限**：

- 这是**字面**检查，无法识别语义等价的改写
- 无法发现「引用了真实条款但断章取义」
- 无法发现暗示性承诺（如「效果显著」这类不含数字的表述）
- 因此 `GuardReport.ok == True` **不等于**文案合规，仍需人工审核
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from powercopy.research.contracts import (
    ClaimStatus,
    EvidencePackage,
)


class CitationViolation(Exception):
    """引用校验失败。属于**硬失败**：程序可确定引用是错的。"""


class ForbiddenContentError(Exception):
    """检测到明确禁止的内容类型（如承诺节省比例）。"""


@dataclass
class GuardFinding:
    """一条安全检查发现。"""

    code: str
    category: str
    snippet: str
    detail: str
    severity: str = "warn"
    """warn = 需人工确认；error = 必须修正"""


@dataclass
class GuardReport:
    """安全检查结果。"""

    findings: list[GuardFinding] = field(default_factory=list)
    citations_checked: int = 0
    evidence_available: int = 0

    @property
    def ok(self) -> bool:
        """没有 error 级发现即为 ok。

        **注意**：ok 不代表文案合规，只代表没命中已知的硬性问题。
        """

        return not any(f.severity == "error" for f in self.findings)

    @property
    def errors(self) -> list[GuardFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[GuardFinding]:
        return [f for f in self.findings if f.severity == "warn"]

    def add(self, finding: GuardFinding) -> None:
        self.findings.append(finding)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "citations_checked": self.citations_checked,
            "evidence_available": self.evidence_available,
            "errors": [
                {"code": f.code, "snippet": f.snippet, "detail": f.detail}
                for f in self.errors
            ],
            "warnings": [
                {"code": f.code, "snippet": f.snippet, "detail": f.detail}
                for f in self.warnings
            ],
            "limitation": (
                "字面检查，无法识别语义等价改写与暗示性承诺；"
                "ok=True 不代表文案合规"
            ),
        }


# ---------------------------------------------------------------- 引用校验


def _usable_evidence_ids(package: EvidencePackage) -> set[str]:
    """可用于文案的 evidence_id 集合。

    规则：只包含**未被拒绝的 claim** 所引用的 evidence。
    这样被拒的 claim 不会通过它引用过的 evidence 间接进入文案。
    """

    usable_claims = [
        c for c in package.claims if c.status is not ClaimStatus.REJECTED
    ]
    referenced = set()
    for claim in usable_claims:
        referenced.update(claim.evidence_ids)

    verified = {
        e.evidence_id for e in package.evidence_items
    }
    return referenced & verified


def validate_citations(
    package: EvidencePackage,
    evidence_ids: list[str],
    *,
    report: GuardReport | None = None,
) -> set[str]:
    """校验一组 evidence_id 是否可用于文案。

    Args:
        package: 证据包
        evidence_ids: 待校验的引用
        report: 可选，用于累积结果

    Returns:
        可用的 evidence_id 集合

    Raises:
        CitationViolation: 存在不可用的引用
    """

    report = report or GuardReport()
    usable = _usable_evidence_ids(package)
    report.evidence_available = len(usable)
    report.citations_checked += len(evidence_ids)

    problems = []
    for evidence_id in evidence_ids:
        if evidence_id not in {e.evidence_id for e in package.evidence_items}:
            problems.append(f"{evidence_id}：证据包中不存在")
        elif evidence_id not in usable:
            problems.append(
                f"{evidence_id}：仅被已拒绝的 claim 引用，不可使用"
            )

    if problems:
        raise CitationViolation("引用校验失败：" + "；".join(problems))

    return set(evidence_ids)


# ---------------------------------------------------------------- 禁用内容


# 高风险模式。每条都是 (code, category, regex, severity)
_FORBIDDEN_PATTERNS: list[tuple[str, str, re.Pattern, str]] = [
    # 承诺节省比例 / 收益 —— 必须来自证据，否则是典型虚构
    (
        "SAVINGS_PROMISE",
        "节省比例承诺",
        re.compile(r"(?:节省|降低|减少|节约)[^。，；\n]{0,8}\d+(?:\.\d+)?\s*(?:%|％|个百分点|成)"),
        "error",
    ),
    (
        "REVENUE_PROMISE",
        "收益承诺",
        re.compile(r"(?:增收|盈利|回报|收益)[^。，；\n]{0,8}\d+(?:\.\d+)?\s*(?:%|％|万元|元)"),
        "error",
    ),
    # 具体报价
    (
        "PRICE_QUOTE",
        "具体报价",
        re.compile(r"\d+(?:\.\d+)?\s*元\s*/\s*(?:千瓦时|度|kWh|MWh)"),
        "error",
    ),
    # 企业服务承诺
    (
        "SERVICE_PROMISE",
        "企业服务承诺",
        re.compile(
            r"(?:我方|本公司|我们|我司)\s*(?:可以|能够|将|承诺|保证|提供)"
            r"[^。，；\n]{0,20}(?:服务|方案|代办|代理|咨询|优惠)"
        ),
        "error",
    ),
    # 客户案例
    (
        "CLIENT_CASE",
        "客户案例",
        re.compile(
            r"(?:某|该|一家|多家)[^。，；\n]{0,6}(?:企业|公司|工厂|园区)"
            r"[^。，；\n]{0,20}(?:通过|借助|采用|实现)"
        ),
        "error",
    ),
    (
        "SUCCESS_CASE",
        "成功案例暗示",
        re.compile(r"(?:成功案例|服务过|已帮助|众多客户|客户见证)"),
        "error",
    ),
    # 编造政策条款 / 有效期 / 适用地区
    (
        "POLICY_CLAUSE",
        "政策条款引用",
        re.compile(r"第[一二三四五六七八九十百零〇\d]{1,4}条"),
        "warn",
    ),
    (
        "POLICY_VALIDITY",
        "政策有效期",
        re.compile(
            r"(?:有效期|施行期|执行期)[^。，；\n]{0,10}"
            r"(?:\d+\s*年|长期有效|永久有效|至\s*\d{4}\s*年)"
            r"|(?:长期有效|永久有效|一直有效)"
        ),
        "warn",
    ),
    (
        "POLICY_REGION",
        "政策适用地区",
        re.compile(
            r"(?:适用于|适用范围(?:包括|为)?)"
            r"[^。，；\n]{0,20}(?:全国|各省|江西省?|安徽省?|广东省?|山西省?)"
        ),
        "warn",
    ),
    # 绝对化表述 —— 政策文案的常见风险
    (
        "ABSOLUTE_CLAIM",
        "绝对化表述",
        re.compile(
            r"(?:一定|必然|绝对|100%)[^。，；\n]{0,12}"
            r"(?:成功|通过|获批|有效|可行)"
        ),
        "warn",
    ),
]


def _evidence_corpus(package: EvidencePackage) -> str:
    """证据包中所有原文的拼接，用于回溯检查。"""

    return "\n".join(e.exact_quote for e in package.evidence_items)


def check_forbidden_content(
    package: EvidencePackage,
    text: str,
    *,
    report: GuardReport | None = None,
) -> GuardReport:
    """检测文案中的高风险表述。

    对每个命中项，检查该片段是否能被证据原文**字面**支持。
    找不到支持的标记为问题。

    Args:
        package: 证据包（用于回溯）
        text: 待检测文本（通常是标题 + 正文）
        report: 可选，累积结果

    Returns:
        更新后的 GuardReport
    """

    report = report or GuardReport()
    corpus = _evidence_corpus(package)

    for code, category, pattern, severity in _FORBIDDEN_PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            # 回溯：该片段是否出现在证据原文中
            if snippet and snippet in corpus:
                continue  # 有原文依据，不报
            report.add(
                GuardFinding(
                    code=code,
                    category=category,
                    snippet=snippet[:80],
                    detail=(
                        f"检测到「{category}」表述，但证据包原文中找不到依据。"
                        f"若确有政策依据，请在证据包中补充对应条款"
                    ),
                    severity=severity,
                )
            )

    return report


# ---------------------------------------------------------------- 综合检查


def run_guards(
    package: EvidencePackage,
    title: str,
    body: str,
    key_points: list[dict],
) -> GuardReport:
    """执行全部安全检查（引用 + 禁用内容）。"""

    report = GuardReport()

    # 1. 引用校验（硬失败）
    for point in key_points:
        validate_citations(
            package, list(point.get("evidence_ids") or []), report=report
        )

    # 2. 禁用内容（软/硬混合）
    check_forbidden_content(package, f"{title}\n{body}", report=report)

    return report


__all__ = [
    "CitationViolation",
    "ForbiddenContentError",
    "GuardFinding",
    "GuardReport",
    "check_forbidden_content",
    "run_guards",
    "validate_citations",
]
