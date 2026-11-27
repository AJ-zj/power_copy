"""审核数据契约。

设计原则
--------
1. **`passed` 不是「可发布」**：`ReviewReport.status` 只表达「程序未发现
   已知的硬性问题」，不表达「内容正确」。
2. **四维必须分开**：引用完整性 / 事实支持性 / 宣传合规 / 政策适用性
   的判定能力完全不同，合并成一个分数会掩盖差异。
3. **机器审核 ≠ 专家确认**：`fact_support` 维度由 LLM 判断时，
   verdict 只能是 `pending_review` 或 `machine_supported`，绝不能是
   `human_confirmed`。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from powercopy.research.contracts import utc_now_iso

MAX_ISSUES = 80
MAX_QUOTE_CHARS = 400
MAX_TEXT_CHARS = 2_000


class ReviewDimension(StrEnum):
    """四个审核维度。"""

    CITATION = "citation"
    """引用完整性 —— 程序可完全判定"""

    FACT_SUPPORT = "fact_support"
    """事实支持性 —— 需语义判断，程序不可靠"""

    COMPLIANCE = "compliance"
    """宣传合规 —— 字面匹配，有漏报"""

    APPLICABILITY = "applicability"
    """政策适用性 —— 时效/地区部分可判，现行有效性不可"""


class IssueSeverity(StrEnum):
    """问题严重度。

    区分依据是「该问题能否被程序确定」：
    - `blocking`：程序可确定这是错的（如引用不存在的证据）
    - `major`：很可能是问题，但需要人确认（如检测到无依据的节省承诺）
    - `minor`：提示性，不影响结论
    """

    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"


class ReviewVerdict(StrEnum):
    """单个维度的判定。"""

    PASSED = "passed"
    """程序未发现问题"""

    FAILED = "failed"
    """程序确认存在问题"""

    PENDING_REVIEW = "pending_review"
    """需人工确认（程序无法可靠判断）"""

    MACHINE_SUPPORTED = "machine_supported"
    """机器判断为支持 —— **不是专家确认**"""

    SKIPPED = "skipped"
    """未执行该维度（如无 LLM 可用）"""


class ReviewStatus(StrEnum):
    """整体审核状态。"""

    PASSED = "passed"
    """四维均通过或仅有 minor 问题。**不等于可发布。**"""

    NEEDS_REVISION = "needs_revision"
    """存在可修复的 major 问题，建议自动修订"""

    FAILED = "failed"
    """存在 blocking 问题，必须人工处理"""

    PENDING_REVIEW = "pending_review"
    """存在无法程序判定的部分，需人工确认"""


class ReviewIssue(BaseModel):
    """一条审核发现。"""

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1, max_length=64)
    dimension: ReviewDimension
    severity: IssueSeverity
    code: str = Field(min_length=1, max_length=60)
    message: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    location: str = Field(
        default="", max_length=200, description="问题位置（如 key_point p2 / 正文第3段）"
    )
    snippet: str = Field(default="", max_length=MAX_QUOTE_CHARS)
    evidence_ids: list[str] = Field(default_factory=list)
    suggestion: str = Field(default="", max_length=600)
    auto_fixable: bool = Field(
        default=False, description="是否适合交给模型做针对性修订"
    )


class DimensionResult(BaseModel):
    """单个维度的审核结果。"""

    model_config = ConfigDict(extra="forbid")

    dimension: ReviewDimension
    verdict: ReviewVerdict
    checked_items: int = Field(default=0, ge=0)
    issue_count: int = Field(default=0, ge=0)
    detail: str = Field(default="", max_length=600)


class ReviewReport(BaseModel):
    """审核报告。"""

    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(min_length=8, max_length=64)
    draft_id: str = Field(default="", max_length=64)
    research_id: str = Field(default="", max_length=64)

    status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    status_reason: str = Field(default="", max_length=1_000)

    dimension_results: list[DimensionResult] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=MAX_ISSUES)

    # 审核能力声明 —— 必须随报告一起输出
    citation_check_available: bool = True
    fact_support_llm_used: bool = False
    """是否用 LLM 做过事实支持性判断。True 时结论仅为机器意见"""

    human_confirmation_available: bool = False
    """人工确认通道是否已执行。本阶段恒为 False"""

    review_mode: str = Field(default="mock", max_length=20)
    model_name: str = Field(default="", max_length=100)
    prompt_version: str = Field(default="review-v1", max_length=40)
    budget_snapshot: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    schema_version: str = Field(default="review-report-v1")

    # ---- 便捷视图 ----

    def issues_by(self, dimension: ReviewDimension) -> list[ReviewIssue]:
        return [i for i in self.issues if i.dimension is dimension]

    def blocking_issues(self) -> list[ReviewIssue]:
        return [i for i in self.issues if i.severity is IssueSeverity.BLOCKING]

    def auto_fixable_issues(self) -> list[ReviewIssue]:
        return [
            i
            for i in self.issues
            if i.auto_fixable and i.severity is not IssueSeverity.MINOR
        ]

    def requires_human(self) -> bool:
        """是否需要人工处理（不能被自动修订解决）。

        任一条件成立即为需要：
        - 存在 blocking 问题
        - 存在无法程序判定的维度（pending_review）
        - 事实支持性未由人工确认（本阶段恒成立）
        """

        if self.blocking_issues():
            return True
        if any(
            r.verdict is ReviewVerdict.PENDING_REVIEW
            for r in self.dimension_results
        ):
            return True
        return not self.human_confirmation_available

    def statistics(self) -> dict:
        """分项统计。刻意不合并成「通过率」。"""

        by_dimension = {}
        for dimension in ReviewDimension:
            items = self.issues_by(dimension)
            by_dimension[str(dimension)] = {
                "total": len(items),
                "blocking": sum(
                    1 for i in items if i.severity is IssueSeverity.BLOCKING
                ),
                "major": sum(
                    1 for i in items if i.severity is IssueSeverity.MAJOR
                ),
                "minor": sum(
                    1 for i in items if i.severity is IssueSeverity.MINOR
                ),
            }

        return {
            "review_id": self.review_id,
            "status": str(self.status),
            "dimension_results": {
                str(r.dimension): {
                    "verdict": str(r.verdict),
                    "checked": r.checked_items,
                    "issues": r.issue_count,
                }
                for r in self.dimension_results
            },
            "by_dimension": by_dimension,
            "blocking": len(self.blocking_issues()),
            "auto_fixable": len(self.auto_fixable_issues()),
            "requires_human": self.requires_human(),
            "fact_support_llm_used": self.fact_support_llm_used,
            "human_confirmation_available": self.human_confirmation_available,
            "note": (
                "status=passed 仅表示程序未发现已知硬性问题，"
                "不代表文案可发布；事实支持性若由 LLM 判断，"
                "属于机器意见，不代表专家确认"
            ),
        }

    def summary(self) -> dict:
        stats = self.statistics()
        return {
            "review_id": self.review_id,
            "draft_id": self.draft_id,
            "status": str(self.status),
            "status_reason": self.status_reason,
            "issues_total": len(self.issues),
            "blocking": stats["blocking"],
            "auto_fixable": stats["auto_fixable"],
            "requires_human": stats["requires_human"],
            "fact_support_llm_used": self.fact_support_llm_used,
            "review_mode": self.review_mode,
        }


class RevisionResult(BaseModel):
    """一次自动修订的结果。"""

    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=8, max_length=64)
    draft_id: str = Field(default="", max_length=64)
    attempt: int = Field(default=1, ge=0, le=3)
    """第几次修订。0 表示未执行"""

    status: str = Field(default="skipped", max_length=40)
    """skipped / revised / unchanged / failed / budget_exhausted"""

    status_reason: str = Field(default="", max_length=1_000)

    addressed_issue_ids: list[str] = Field(default_factory=list)
    remaining_issue_ids: list[str] = Field(default_factory=list)

    # 修改前后内容
    before_title: str = Field(default="", max_length=200)
    before_body: str = Field(default="", max_length=20_000)
    after_title: str = Field(default="", max_length=200)
    after_body: str = Field(default="", max_length=20_000)
    changed: bool = False

    # 修订后重审结果
    post_review_status: str = Field(default="", max_length=40)
    post_review_blocking: int = Field(default=0, ge=0)

    model_name: str = Field(default="", max_length=100)
    budget_snapshot: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)

    @model_validator(mode="after")
    def _check_attempt_bound(self):
        if self.attempt > 3:
            raise ValueError("自动修订次数不得超过 3")
        return self

    def to_dict(self) -> dict:
        return {
            "revision_id": self.revision_id,
            "draft_id": self.draft_id,
            "attempt": self.attempt,
            "status": self.status,
            "status_reason": self.status_reason,
            "addressed_issue_ids": self.addressed_issue_ids,
            "remaining_issue_ids": self.remaining_issue_ids,
            "changed": self.changed,
            "post_review_status": self.post_review_status,
            "post_review_blocking": self.post_review_blocking,
            "before_char_count": len(self.before_body),
            "after_char_count": len(self.after_body),
            "model_name": self.model_name,
            "note": (
                "修订后的内容已重新审核；post_review_status 仍为"
                "程序判定结果，不代表可发布"
            ),
        }


__all__ = [
    "DimensionResult",
    "IssueSeverity",
    "MAX_ISSUES",
    "ReviewDimension",
    "ReviewIssue",
    "ReviewReport",
    "ReviewStatus",
    "ReviewVerdict",
    "RevisionResult",
]
