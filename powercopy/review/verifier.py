"""四维审核的实现。

一、引用完整性（程序可完全判定）
--------------------------------
- 每个 `key_points[].evidence_ids` 必须存在于证据包
- 每个引用必须来自**未被拒绝**的 claim
- `ContentDraft.evidence_refs` 必须与 key_points 的实际引用一致
- 正文中引用的证据必须真实（若正文含 evidence_id 形式的标注）

二、事实支持性（需语义判断，程序不可靠）
----------------------------------------
**这一维度是本模块最需要克制的地方。**

任务书要求「检查标题和完整正文中的事实性陈述，而不仅是模型自行列出的
key_points」。程序能做的是：

1. 把正文切成陈述句
2. 找出其中的「事实性陈述」（含条款表述、数字、责任主体等）
3. **判断每条陈述是否被证据支持** —— 这一步程序做不了

因此：
- 无 LLM 时：把所有事实性陈述标为 `pending_review`，说明「未做语义审核」
- 有 LLM 时：把陈述与候选证据一起给模型，由模型给出
  `supported` / `contradicted` / `insufficient`
  结论写入 `verdict = MACHINE_SUPPORTED` 等，并明确标注
  **「这是机器审核，不代表专家确认」**

程序能做的一件有用的辅助：**字面回溯**。对含明显数字或条款编号的陈述，
检查该片段是否在证据原文中出现。找不到的标为 `insufficient`（这是
可靠的信号：数字凭空出现）。

三、宣传合规（复用 guards）
----------------------------
直接调用 `powercopy.copywriting.guards.check_forbidden_content`，
不重复实现规则。

四、政策适用性（部分可判）
--------------------------
- 地区：证据包 `region` 与草稿 `region_applicability` 是否一致
- 时效：`policy_validity_status` 是否为 `latest_observed`（不能宣称现行有效）
- 若证据包 `region` 为空但文案提到具体地区 → 报问题
- **现行有效性一律标记待人工确认**（程序无法判断政策是否被废止）
"""

from __future__ import annotations

import re
import uuid

from powercopy.copywriting.contracts import ContentDraft
from powercopy.copywriting.guards import (
    GuardReport,
    check_forbidden_content,
    validate_citations,
    _usable_evidence_ids,
)
from powercopy.copywriting.guards import CitationViolation
from powercopy.research.contracts import (
    ClaimStatus,
    EvidencePackage,
    PolicyValidityStatus,
)
from powercopy.review.contracts import (
    DimensionResult,
    IssueSeverity,
    ReviewDimension,
    ReviewIssue,
    ReviewReport,
    ReviewStatus,
    ReviewVerdict,
)

# 事实性陈述的启发式特征
_FACTUAL_MARKERS = re.compile(
    r"第[一二三四五六七八九十百零〇\d]{1,4}条"
    r"|\d+(?:\.\d+)?\s*(?:%|％|元|万元|千瓦时|度|个工作日|年|月|日)"
    r"|(?:应当|必须|不得|禁止|可以|有权|负责|承担)"
)

# 句子切分
_SENTENCE_SPLIT = re.compile(r"[。！？\n]+")


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_SPLIT.split(text)]
    return [s for s in parts if len(s) >= 8]


class ContentVerifier:
    """四维审核器。"""

    def __init__(
        self,
        package: EvidencePackage,
        draft: ContentDraft,
        *,
        fact_support_llm=None,
        model_name: str = "",
        review_mode: str = "mock",
    ):
        """
        Args:
            package: 证据包（审核依据）
            draft: 待审文案
            fact_support_llm: 可选的 LLM，用于事实支持性判断。
                None 表示不做语义审核，该维度全部 pending_review
            model_name: 模型名（写入报告）
            review_mode: mock / live
        """

        self.package = package
        self.draft = draft
        self.fact_support_llm = fact_support_llm
        self.model_name = model_name
        self.review_mode = review_mode
        self._issues: list[ReviewIssue] = []
        self._counter = 0

    # ---- 问题记录 ----

    def _add(
        self,
        dimension: ReviewDimension,
        severity: IssueSeverity,
        code: str,
        message: str,
        *,
        location: str = "",
        snippet: str = "",
        evidence_ids: list[str] | None = None,
        suggestion: str = "",
        auto_fixable: bool = False,
    ) -> ReviewIssue:
        self._counter += 1
        issue = ReviewIssue(
            issue_id=f"i{self._counter}",
            dimension=dimension,
            severity=severity,
            code=code,
            message=message,
            location=location[:200],
            snippet=snippet[:400],
            evidence_ids=evidence_ids or [],
            suggestion=suggestion[:600],
            auto_fixable=auto_fixable,
        )
        self._issues.append(issue)
        return issue

    # ---- 维度一：引用完整性 ----

    def check_citation(self) -> DimensionResult:
        """程序可完全判定的维度。"""

        before = len(self._issues)
        usable = _usable_evidence_ids(self.package)
        all_ids = {e.evidence_id for e in self.package.evidence_items}

        checked = 0

        # 1. 逐要点校验
        for point in self.draft.key_points:
            checked += 1
            location = f"key_point {point.point_id}"
            try:
                validate_citations(
                    self.package, point.evidence_ids
                )
            except CitationViolation as exc:
                for evidence_id in point.evidence_ids:
                    if evidence_id not in all_ids:
                        self._add(
                            ReviewDimension.CITATION,
                            IssueSeverity.BLOCKING,
                            "EVIDENCE_NOT_FOUND",
                            f"引用了不存在的证据 ID：{evidence_id}",
                            location=location,
                            evidence_ids=[evidence_id],
                            suggestion="删除该引用或替换为真实证据",
                            auto_fixable=False,
                        )
                    elif evidence_id not in usable:
                        self._add(
                            ReviewDimension.CITATION,
                            IssueSeverity.BLOCKING,
                            "EVIDENCE_FROM_REJECTED_CLAIM",
                            f"证据 {evidence_id} 仅被已拒绝的 claim 引用，不可使用",
                            location=location,
                            evidence_ids=[evidence_id],
                            suggestion="删除该引用",
                            auto_fixable=False,
                        )
                continue

            # 2. 引用的证据必须确实与 claim 关联
            for evidence_id in point.evidence_ids:
                related_claims = [
                    c
                    for c in self.package.claims
                    if c.status is not ClaimStatus.REJECTED
                    and evidence_id in c.evidence_ids
                ]
                if not related_claims:
                    self._add(
                        ReviewDimension.CITATION,
                        IssueSeverity.BLOCKING,
                        "EVIDENCE_WITHOUT_CLAIM",
                        f"证据 {evidence_id} 没有关联的有效 claim",
                        location=location,
                        evidence_ids=[evidence_id],
                        auto_fixable=False,
                    )

        # 3. evidence_refs 与实际引用必须一致
        actual_refs = {
            eid for p in self.draft.key_points for eid in p.evidence_ids
        }
        declared = set(self.draft.evidence_refs)
        if declared != actual_refs:
            missing = actual_refs - declared
            extra = declared - actual_refs
            parts = []
            if missing:
                parts.append(f"漏记 {sorted(missing)}")
            if extra:
                parts.append(f"多记 {sorted(extra)}")
            self._add(
                ReviewDimension.CITATION,
                IssueSeverity.MAJOR,
                "EVIDENCE_REFS_MISMATCH",
                "evidence_refs 与要点实际引用不一致：" + "；".join(parts),
                location="evidence_refs",
                auto_fixable=False,
            )

        # 4. 正文中出现但未被任何证据支持的 evidence_id 形式标注
        cited_in_body = set(
            re.findall(r"\b[0-9a-f]{16}\b", self.draft.body)
        )
        unknown = cited_in_body - all_ids
        if unknown:
            self._add(
                ReviewDimension.CITATION,
                IssueSeverity.BLOCKING,
                "BODY_CITES_UNKNOWN_EVIDENCE",
                f"正文中出现未知证据 ID：{sorted(unknown)[:5]}",
                location="body",
                auto_fixable=False,
            )
            checked += len(unknown)

        count = len(self._issues) - before
        verdict = (
            ReviewVerdict.PASSED if count == 0 else ReviewVerdict.FAILED
        )
        return DimensionResult(
            dimension=ReviewDimension.CITATION,
            verdict=verdict,
            checked_items=checked,
            issue_count=count,
            detail=(
                f"校验 {checked} 项引用，"
                f"{'全部通过' if count == 0 else f'{count} 项有问题'}"
            ),
        )

    # ---- 维度二：事实支持性 ----

    def check_fact_support(self) -> DimensionResult:
        """检查标题与完整正文中的事实性陈述。

        **程序不能判断语义支持性**，因此：

        - 无 LLM：全部 `pending_review`，明确说明未做语义审核
        - 有 LLM：交给模型判断，结论标记为机器意见

        程序唯一可靠的辅助判断是「字面回溯」：事实性陈述中的数字、
        条款编号若在证据原文中完全找不到，那是可靠的「无依据」信号。
        """

        before = len(self._issues)
        corpus = "\n".join(e.exact_quote for e in self.package.evidence_items)

        sentences = _split_sentences(f"{self.draft.title}\n{self.draft.body}")
        factual = [
            s for s in sentences if _FACTUAL_MARKERS.search(s)
        ]

        checked = len(factual)
        literal_unsupported = 0

        # ---- 字面回溯（可靠信号）----
        for sentence in factual:
            # 提取数字与条款编号，检查是否在证据原文中
            anchors = re.findall(
                r"第[一二三四五六七八九十百零〇\d]{1,4}条"
                r"|\d+(?:\.\d+)?\s*(?:%|％|元|万元|千瓦时|度|个工作日)",
                sentence,
            )
            missing = [
                a for a in anchors if a.replace(" ", "") not in corpus.replace(" ", "")
            ]
            if missing:
                literal_unsupported += 1
                self._add(
                    ReviewDimension.FACT_SUPPORT,
                    IssueSeverity.MAJOR,
                    "UNSUPPORTED_LITERAL_ANCHOR",
                    "事实性陈述中的关键数字/条款在证据原文中找不到："
                    + "、".join(missing[:3]),
                    location="body",
                    snippet=sentence[:200],
                    suggestion="核对该陈述是否有政策依据，无依据则应删除",
                    auto_fixable=True,
                )

        # ---- 语义判断 ----
        llm_used = False
        llm_judged = 0
        if self.fact_support_llm is not None and factual:
            llm_used = True
            verdicts = self._llm_fact_check(factual, corpus)
            llm_judged = len(verdicts)
            for sentence, verdict, note in verdicts:
                if verdict == "contradicted":
                    self._add(
                        ReviewDimension.FACT_SUPPORT,
                        IssueSeverity.BLOCKING,
                        "MACHINE_CONTRADICTED",
                        f"机器判断该陈述与证据矛盾：{note}",
                        location="body",
                        snippet=sentence[:200],
                        suggestion="必须修正或删除该陈述",
                        auto_fixable=True,
                    )
                elif verdict == "insufficient":
                    self._add(
                        ReviewDimension.FACT_SUPPORT,
                        IssueSeverity.MAJOR,
                        "MACHINE_INSUFFICIENT",
                        f"机器判断证据不足以支持该陈述：{note}",
                        location="body",
                        snippet=sentence[:200],
                        suggestion="补充证据或改为更保守的表述",
                        auto_fixable=True,
                    )

        count = len(self._issues) - before

        # 关键：只有「模型确实逐条判断过，且没有发现问题」才是
        # machine_supported。模型返回空结果（表示"无法判断"）必须
        # 保持 pending_review —— 否则会把"没有判断"伪装成"机器认为支持"。
        if not llm_used or llm_judged == 0:
            verdict = ReviewVerdict.PENDING_REVIEW
            parts = [
                f"检查 {checked} 条事实性陈述",
                f"程序字面回溯发现 {literal_unsupported} 条无依据",
            ]
            if llm_used and llm_judged == 0:
                parts.append("模型未能给出任何判断，语义支持性未审核")
            else:
                parts.append("未配置语义审核模型，语义支持性未审核")
            parts.append("需人工确认")
            detail = "；".join(parts)
        elif count == 0:
            verdict = ReviewVerdict.MACHINE_SUPPORTED
            detail = (
                f"检查 {checked} 条事实性陈述，机器逐条判断 "
                f"{llm_judged} 条均未发现问题；"
                f"**这是机器审核意见，不代表专家确认**"
            )
        else:
            verdict = ReviewVerdict.FAILED
            detail = (
                f"检查 {checked} 条事实性陈述（机器判断 {llm_judged} 条），"
                f"发现 {count} 个问题"
            )

        return DimensionResult(
            dimension=ReviewDimension.FACT_SUPPORT,
            verdict=verdict,
            checked_items=checked,
            issue_count=count,
            detail=detail,
        )

    def _llm_fact_check(
        self, sentences: list[str], corpus: str
    ) -> list[tuple[str, str, str]]:
        """让模型判断每条陈述是否被证据支持。"""

        import json as _json

        prompt = (
            "判断下列陈述是否被给定证据支持。\n"
            "只返回 JSON："
            '{"verdicts":[{"index":0,"verdict":"supported|contradicted|insufficient",'
            '"note":"简短理由"}]}\n\n'
            "证据原文：\n"
            + corpus[:4000]
            + "\n\n待判断陈述：\n"
            + _json.dumps(sentences[:12], ensure_ascii=False, indent=2)
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是政策文案的事实核查助手。只依据给定证据判断，"
                    "不要引入外部知识。只返回 JSON。"
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = self.fact_support_llm.invoke(messages)
        except Exception:
            return []

        raw = response.get("content") if isinstance(response, dict) else None
        if not raw:
            return []

        text = str(raw).strip()
        if text.startswith("```"):
            parts = text.split("```", 2)
            if len(parts) >= 2:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

        try:
            parsed = _json.loads(text)
        except Exception:
            return []

        results = []
        for item in parsed.get("verdicts") or []:
            try:
                index = int(item.get("index", -1))
                if 0 <= index < len(sentences):
                    results.append(
                        (
                            sentences[index],
                            str(item.get("verdict") or ""),
                            str(item.get("note") or ""),
                        )
                    )
            except Exception:
                continue
        return results

    # ---- 维度三：宣传合规 ----

    def check_compliance(self) -> DimensionResult:
        """复用 guards，不重复实现规则。"""

        before = len(self._issues)
        guard_report = GuardReport()
        check_forbidden_content(
            self.package,
            f"{self.draft.title}\n{self.draft.body}",
            report=guard_report,
        )

        checked = 1
        for finding in guard_report.errors:
            self._add(
                ReviewDimension.COMPLIANCE,
                IssueSeverity.BLOCKING,
                finding.code,
                f"{finding.category}：{finding.detail}",
                location="body",
                snippet=finding.snippet,
                suggestion="删除该表述或补充政策依据",
                auto_fixable=True,
            )
        for finding in guard_report.warnings:
            self._add(
                ReviewDimension.COMPLIANCE,
                IssueSeverity.MAJOR,
                finding.code,
                f"{finding.category}：{finding.detail}",
                location="body",
                snippet=finding.snippet,
                suggestion="核对是否有政策依据",
                auto_fixable=True,
            )

        count = len(self._issues) - before
        if count == 0:
            verdict = ReviewVerdict.PASSED
            detail = "未命中已知的禁用宣传模式"
        elif any(
            i.severity is IssueSeverity.BLOCKING
            for i in self._issues[-count:] if count
        ):
            verdict = ReviewVerdict.FAILED
            detail = f"发现 {count} 项宣传合规问题（含阻断级）"
        else:
            verdict = ReviewVerdict.PENDING_REVIEW
            detail = f"发现 {count} 项需核对的宣传表述"

        return DimensionResult(
            dimension=ReviewDimension.COMPLIANCE,
            verdict=verdict,
            checked_items=checked,
            issue_count=count,
            detail=detail
            + "；字面匹配，无法识别语义等价改写与暗示性承诺",
        )

    # ---- 维度四：政策适用性 ----

    def check_applicability(self) -> DimensionResult:
        """检查时间、地区、政策版本限制。

        **现行有效性程序无法判断**，一律标记待人工确认。
        """

        before = len(self._issues)
        checked = 0

        package_region = (self.package.region or "").strip()
        draft_region = (self.draft.region_applicability or "").strip()
        body_text = f"{self.draft.title}\n{self.draft.body}"

        # 1. 地区一致性
        checked += 1
        if package_region and not draft_region:
            self._add(
                ReviewDimension.APPLICABILITY,
                IssueSeverity.MAJOR,
                "REGION_CONTEXT_LOST",
                f"证据包限定地区「{package_region}」，但文案未声明适用范围",
                location="draft",
                suggestion="在文案中声明地区限定，或确认无需限定",
                auto_fixable=True,
            )

        # 2. 文案「声称适用范围」但证据包未限定地区
        #
        # 关键区别：仅仅**提到**某地区名（如引文里的「全国统一大市场」）
        # 不等于**声称适用范围**。只有出现「适用于 / 适用范围 / 本政策适用」
        # 这类限定语时，才构成地区主张。
        checked += 1
        region_claim = re.search(
            r"(?:适用于|适用范围(?:包括|为|是)?|本(?:政策|规则|办法)适用)"
            r"[^。，；\n]{0,20}(江西|安徽|广东|山西|全国|各省|各地)",
            body_text,
        )
        if region_claim and not package_region:
            self._add(
                ReviewDimension.APPLICABILITY,
                IssueSeverity.MAJOR,
                "REGION_CLAIMED_WITHOUT_BASIS",
                f"文案声称适用于「{region_claim.group(1)}」，"
                f"但证据包未标注适用地区",
                location="body",
                snippet=region_claim.group(0)[:120],
                suggestion="删除地区限定，或补充该地区的政策证据",
                auto_fixable=True,
            )

        # 3. 时效状态
        checked += 1
        validity = str(self.package.policy_validity_status)
        if validity != str(PolicyValidityStatus.LATEST_OBSERVED):
            self._add(
                ReviewDimension.APPLICABILITY,
                IssueSeverity.MAJOR,
                "VALIDITY_NOT_LATEST",
                f"证据包时效状态为 {validity}，引用版本可能不是最新",
                location="draft",
                suggestion="确认引用版本，或更新为最新观测版本",
                auto_fixable=False,
            )

        # 4. 文案声称现行有效（禁止）
        #
        # 关键：必须排除**否定式**表述。像「不代表该政策现行有效」
        # 「是否生效需以官方公告为准」这类免责声明本身就是正确写法，
        # 不能因为含有"现行有效"四个字就被判为违规。
        checked += 1
        for match in re.finditer(
            r"(?:现行有效|仍然有效|持续有效|长期有效)", body_text
        ):
            # 取该词前 12 个字符作为上下文，检查是否定式
            start = max(0, match.start() - 12)
            context = body_text[start : match.start()]
            if re.search(
                r"(?:不代表|不表示|不意味着|无法确认|不能确认|是否|需以|"
                r"以官方|以.{0,4}公告为准|不构成)",
                context,
            ):
                continue  # 免责式表述，不是违规

            self._add(
                ReviewDimension.APPLICABILITY,
                IssueSeverity.BLOCKING,
                "CLAIMS_STILL_EFFECTIVE",
                "文案声称政策现行有效，但程序无法确认政策是否被废止",
                location="body",
                snippet=body_text[match.start() : match.start() + 40],
                suggestion="改为「以官方公告为准」等保守表述",
                auto_fixable=True,
            )
            break

        # 5. 时效说明是否保留
        checked += 1
        if "官方公告" not in body_text and "不代表" not in body_text:
            self._add(
                ReviewDimension.APPLICABILITY,
                IssueSeverity.MINOR,
                "VALIDITY_DISCLAIMER_MISSING",
                "文案未保留政策时效免责说明",
                location="body",
                suggestion="在结尾声明「具体以官方发布为准」",
                auto_fixable=True,
            )

        count = len(self._issues) - before

        if count == 0:
            verdict = ReviewVerdict.PASSED
        elif any(
            i.severity is IssueSeverity.BLOCKING
            for i in self._issues[-count:]
        ):
            verdict = ReviewVerdict.FAILED
        else:
            verdict = ReviewVerdict.PENDING_REVIEW

        return DimensionResult(
            dimension=ReviewDimension.APPLICABILITY,
            verdict=verdict,
            checked_items=checked,
            issue_count=count,
            detail=(
                f"检查 {checked} 项；"
                f"**政策是否现行有效程序无法判断，需人工确认官方公告**"
            ),
        )

    # ---- 汇总 ----

    def run(self) -> ReviewReport:
        """执行四维审核，产出报告。"""

        results = [
            self.check_citation(),
            self.check_fact_support(),
            self.check_compliance(),
            self.check_applicability(),
        ]

        status, reason = self._decide_status(results)

        return ReviewReport(
            review_id=f"rev-{uuid.uuid4().hex[:16]}",
            draft_id=self.draft.draft_id,
            research_id=self.package.research_id,
            status=status,
            status_reason=reason,
            dimension_results=results,
            issues=self._issues[:80],
            citation_check_available=True,
            fact_support_llm_used=(
                self.fact_support_llm is not None
            ),
            human_confirmation_available=False,            review_mode=self.review_mode,
            model_name=self.model_name,
        )

    @staticmethod
    def _decide_status(
        results: list[DimensionResult],
    ) -> tuple[ReviewStatus, str]:
        def verdict_of(dimension: ReviewDimension) -> ReviewVerdict:
            for result in results:
                if result.dimension is dimension:
                    return result.verdict
            return ReviewVerdict.SKIPPED

        blocking_dims = [
            str(r.dimension)
            for r in results
            if r.verdict is ReviewVerdict.FAILED
        ]

        if blocking_dims:
            return (
                ReviewStatus.FAILED,
                f"维度 {blocking_dims} 审核未通过，需处理后再审",
            )

        pending = [
            str(r.dimension)
            for r in results
            if r.verdict is ReviewVerdict.PENDING_REVIEW
        ]

        major_count = sum(
            r.issue_count for r in results if r.issue_count > 0
        )

        if major_count and not blocking_dims:
            return (
                ReviewStatus.NEEDS_REVISION,
                f"发现 {major_count} 项可修复问题，建议订正后重新审核",
            )

        if pending:
            return (
                ReviewStatus.PENDING_REVIEW,
                f"维度 {pending} 需人工确认，程序无法判定",
            )

        return (
            ReviewStatus.PASSED,
            "程序未发现已知硬性问题。**这不代表文案可发布**，"
            "事实支持性仍需专家确认",
        )


__all__ = ["ContentVerifier"]
