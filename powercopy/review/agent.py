"""审核 Agent：审核 + 有限自动修订。

流程
----
```text
审核 → 有可修复问题？
        ├─ 否 → 返回
        └─ 是 → 修订（最多 1 次）→ 重新审核
                 └─ 仍不通过 → 停止，请求人工
```

硬性约束
--------
- **默认最多 1 次自动修订**（`max_revisions=1`）
- 修订后**必须重新审核**
- 以下情况**立即停止**，不尝试修订：
  - 预算不足
  - 存在 blocking 问题（如引用了不存在的证据 —— 这不是"改写"能修的，
    需要重新走研究/生成）
  - 无可修复问题
- **禁止无限循环**：修订次数由参数硬限制，且 `RevisionResult.attempt`
  契约层限制 ≤ 3

为什么 blocking 问题不自动修订
------------------------------
引用不存在的证据这类问题，根因在**上游**（研究或生成阶段）。
让模型"改写文案"来绕过它，只会掩盖问题。因此必须交回人工或重跑上游。

自动修订只处理「表述层面」的问题：
- 无依据的宣传用语
- 过强的绝对化措辞
- 缺失的时效免责声明
"""

from __future__ import annotations

import json
import uuid

from powercopy.copywriting.contracts import ContentDraft, CopywritingStatus
from powercopy.core.budget import BudgetExceeded
from powercopy.research.contracts import EvidencePackage
from powercopy.review.contracts import (
    IssueSeverity,
    ReviewReport,
    ReviewStatus,
    ReviewVerdict,
    RevisionResult,
)
from powercopy.review.verifier import ContentVerifier

def _extract_json_object(text: str) -> dict | None:
    """从模型输出中提取 JSON 对象。

    真实实验中发现的缺陷：模型可能返回「好的，以下是订正结果：{...}」
    这类带前后缀的内容，直接 json.loads 会失败。
    这里先尝试整体解析，失败则扫描第一个平衡的 {...}。
    """

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 扫描第一个平衡的 JSON 对象
    start = text.find("{")
    while start >= 0:
        depth = 0
        for index in range(start, len(text)):
            ch = text[index]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        break
        start = text.find("{", start + 1)

    return None


REVISION_SYSTEM_PROMPT = """你是政策文案订正助手。

【任务】
根据审核发现的问题，对文案做**最小必要的修改**。

【严格约束】
1. 只修改审核指出的问题，不要重写无关内容
2. 保留所有原文引用（政策原文引用必须逐字不变）
3. 不得引入任何新的事实、数字、条款编号或地区限定
4. 不得删除政策时效与适用范围声明
5. 无法在保持引用真实的前提下修正的问题，保持原样并在 note 中说明
6. 只返回 JSON"""

REVISION_USER_TEMPLATE = """请订正以下文案。

【待订正文案】
标题：{title}

正文：
{body}

【审核发现的问题】
{issues}

【输出 JSON 格式】
{{
  "title": "订正后的标题",
  "body": "订正后的正文",
  "addressed": ["已修正的 issue_id"],
  "note": "无法修正的部分及原因"
}}"""


class ReviewAgent:
    """审核 + 有限自动修订。"""

    def __init__(
        self,
        package: EvidencePackage,
        *,
        reviewer_llm=None,
        reviser_llm=None,
        max_revisions: int = 1,
        model_name: str = "",
        review_mode: str = "mock",
        budget=None,
    ):
        """
        Args:
            package: 证据包
            reviewer_llm: 用于事实支持性判断的 LLM。None 则跳过语义审核
            reviser_llm: 用于自动修订的 LLM。None 则不做自动修订
            max_revisions: 最多自动修订次数（默认 1，硬上限 3）
            model_name: 模型名
            review_mode: mock / live
            budget: 共享预算（SharedBudget 或 TaskBudget）
        """

        if max_revisions < 0:
            raise ValueError("max_revisions 不能为负")
        if max_revisions > 3:
            raise ValueError("max_revisions 不得超过 3")

        self.package = package
        self.reviewer_llm = reviewer_llm
        self.reviser_llm = reviser_llm
        self.max_revisions = max_revisions
        self.model_name = model_name
        self.review_mode = review_mode
        self.budget = budget
        self._last_revision_error = ""

    # ---- 审核 ----

    def review(self, draft: ContentDraft) -> ReviewReport:
        verifier = ContentVerifier(
            self.package,
            draft,
            fact_support_llm=self.reviewer_llm,
            model_name=self.model_name,
            review_mode=self.review_mode,
        )
        report = verifier.run()
        if self.budget is not None:
            report.budget_snapshot = self._budget_dict()
        return report

    # ---- 审核 + 修订 ----

    def run(
        self, draft: ContentDraft
    ) -> tuple[ReviewReport, ContentDraft, RevisionResult]:
        """审核，必要时修订一次并重审。

        Returns:
            (最终审核报告, 最终草稿, 修订记录)
        """

        report = self.review(draft)

        revision = RevisionResult(
            revision_id=f"revn-{uuid.uuid4().hex[:16]}",
            draft_id=draft.draft_id,
            attempt=0,
            status="skipped",
            status_reason="未触发修订",
            before_title=draft.title,
            before_body=draft.body,
            after_title=draft.title,
            after_body=draft.body,
        )

        if report.status is ReviewStatus.PASSED:
            revision.status_reason = "审核通过，无需修订"
            return report, draft, revision

        # ---- 判断是否适合自动修订 ----
        stop_reason = self._should_stop(report)
        if stop_reason is not None:
            revision.status = stop_reason[0]
            revision.status_reason = stop_reason[1]
            revision.remaining_issue_ids = [
                i.issue_id for i in report.issues
            ]
            return report, draft, revision

        # ---- 执行修订 ----
        fixed_reports = [report]
        current_draft = draft

        for attempt in range(1, self.max_revisions + 1):
            if self.budget is not None and self.budget.stop_reason() is not None:
                revision.status = "budget_exhausted"
                revision.status_reason = "预算耗尽，停止自动修订"
                revision.attempt = attempt - 1
                return fixed_reports[-1], current_draft, revision

            revised = self._revise(current_draft, fixed_reports[-1], attempt)
            if revised is None:
                revision.status = "failed"
                revision.status_reason = (
                    "模型未能给出可用的订正结果："
                    + (self._last_revision_error or "原因未知")
                )
                revision.attempt = attempt - 1
                revision.remaining_issue_ids = [
                    i.issue_id for i in fixed_reports[-1].issues
                ]
                return fixed_reports[-1], current_draft, revision

            new_draft, addressed, note = revised

            # 引用必须仍然有效 —— 修订不能破坏引用
            post_report = self.review(new_draft)

            revision = RevisionResult(
                revision_id=revision.revision_id,
                draft_id=new_draft.draft_id,
                attempt=attempt,
                status="revised",
                status_reason=note or "已执行自动修订",
                addressed_issue_ids=addressed,
                remaining_issue_ids=[
                    i.issue_id for i in post_report.issues
                ],
                before_title=current_draft.title,
                before_body=current_draft.body,
                after_title=new_draft.title,
                after_body=new_draft.body,
                changed=(
                    new_draft.title != current_draft.title
                    or new_draft.body != current_draft.body
                ),
                post_review_status=str(post_report.status),
                post_review_blocking=len(post_report.blocking_issues()),
                model_name=self.model_name,
                budget_snapshot=self._budget_dict(),
            )

            current_draft = new_draft
            fixed_reports.append(post_report)

            # 修订后通过则停止
            if post_report.status is ReviewStatus.PASSED:
                return post_report, current_draft, revision

            # 修订后出现 blocking 或不可修复问题则停止
            if post_report.blocking_issues():
                revision.status = "revised"
                revision.status_reason = (
                    (note or "已执行自动修订")
                    + "；修订后仍存在阻断级问题，停止自动修订"
                )
                return post_report, current_draft, revision

            if not post_report.auto_fixable_issues():
                revision.status_reason = (
                    (note or "已执行自动修订")
                    + "；无更多可自动修复的问题"
                )
                return post_report, current_draft, revision

        # 达到修订上限
        final_report = fixed_reports[-1]
        revision.status_reason = (
            revision.status_reason
            + f"；已达自动修订上限 {self.max_revisions} 次，需人工处理"
        )
        return final_report, current_draft, revision

    # ---- 内部 ----

    def _should_stop(
        self, report: ReviewReport
    ) -> tuple[str, str] | None:
        """判断是否应该停止而不修订。

        判定标准是「这个问题能不能靠改写文案修好」，而不是严重度：

        | 情况 | 能否改写修复 |
        |---|---|
        | 引用不存在的证据 | ❌ 根因在上游，改写只会掩盖 |
        | 引用被拒 claim 的证据 | ❌ 同上 |
        | 宣传违规（节省承诺等） | ✅ 删除该句即可 |
        | 绝对化措辞 / 缺免责声明 | ✅ 改写即可 |
        | 事实与证据矛盾 | ✅ 删改该陈述即可 |

        因此 blocking 里也分「可修」与「不可修」两类。
        """

        if self.budget is not None and self.budget.stop_reason() is not None:
            return ("budget_exhausted", "预算耗尽，停止自动修订")

        # 不可修复的阻断级问题：引用层面的错误根因在上游
        unfixable_blocking = [
            i
            for i in report.blocking_issues()
            if not i.auto_fixable
        ]
        if unfixable_blocking:
            codes = {i.code for i in unfixable_blocking}
            return (
                "skipped",
                f"存在 {len(unfixable_blocking)} 项无法通过改写修复的阻断级问题"
                f"（{', '.join(sorted(codes)[:3])}），"
                f"根因在上游，需人工处理或重跑研究/生成阶段",
            )

        if self.reviser_llm is None:
            return ("skipped", "未配置修订模型，跳过自动修订")

        if not report.auto_fixable_issues():
            return ("skipped", "没有可自动修复的问题")

        return None

    def _revise(
        self, draft: ContentDraft, report: ReviewReport, attempt: int
    ) -> tuple[ContentDraft, list[str], str] | None:
        """调用模型做一次针对性修订。

        失败时返回 None，并通过 `self._last_revision_error` 记录原因
        （真实实验中发现的缺陷：原先静默返回 None，无法诊断）。
        """

        self._last_revision_error = ""

        issues_text = "\n".join(
            f"- [{i.issue_id}] ({i.dimension}/{i.severity}) {i.message}"
            + (f"\n  片段：{i.snippet}" if i.snippet else "")
            + (f"\n  建议：{i.suggestion}" if i.suggestion else "")
            for i in report.issues[:20]
        )

        prompt = REVISION_USER_TEMPLATE.format(
            title=draft.title,
            body=draft.body,
            issues=issues_text,
        )

        messages = [
            {"role": "system", "content": REVISION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            response = self.reviser_llm.invoke(
                messages, budget=self.budget
            )
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_revision_error = (
                f"模型调用异常（{type(exc).__name__}）：{exc}"
            )
            return None

        raw = response.get("content") if isinstance(response, dict) else None
        if not raw:
            self._last_revision_error = "模型返回空内容"
            return None

        text = str(raw).strip()
        if text.startswith("```"):
            parts = text.split("```", 2)
            if len(parts) >= 2:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

        parsed = _extract_json_object(text)
        if parsed is None:
            preview = text[:200].replace("\n", " ")
            self._last_revision_error = (
                f"模型结构化输出无法解析为 JSON 对象；"
                f"原始响应前 200 字符：{preview!r}"
            )
            return None

        new_title = str(parsed.get("title") or "").strip()
        new_body = str(parsed.get("body") or "").strip()
        if not new_title or not new_body:
            self._last_revision_error = (
                "模型返回的 JSON 缺少 title 或 body 字段"
            )
            return None

        addressed = [
            str(x) for x in (parsed.get("addressed") or [])
        ]
        note = str(parsed.get("note") or "")

        # 保留原 draft 的引用映射与状态 —— 修订只改文字，不重新建立引用
        revised = draft.model_copy(
            update={
                "title": new_title[:100],
                "body": new_body[:20_000],
                "status": CopywritingStatus.PENDING_REVIEW,
                "status_reason": (
                    f"第 {attempt} 次自动修订后的待审核草稿"
                ),
            }
        )
        return revised, addressed, note

    def _budget_dict(self) -> dict:
        if self.budget is None:
            return {}
        if hasattr(self.budget, "to_dict"):
            return self.budget.to_dict()
        try:
            return self.budget.snapshot()
        except Exception:
            return {}


__all__ = [
    "REVISION_SYSTEM_PROMPT",
    "REVISION_USER_TEMPLATE",
    "ReviewAgent",
]
