"""统一内容业务流程（V2.1 MVP-04-B）。

供 CLI 与 workbench API 共用的服务层。

流程
----
```text
ContentWorkflowRequest
    │
    ├─ 阶段 1  研究        ResearchAgent   → EvidencePackage
    ├─ 阶段 2  文案生成    CopywritingAgent → ContentDraft
    ├─ 阶段 3  审核        ReviewAgent     → ReviewReport
    └─ 阶段 4  有限修订    ReviewAgent     → RevisionResult（默认最多 1 次）
            │
            ▼
ContentWorkflowResult
```

预算
----
**整个流程共享一个 `SharedBudget`**，四个阶段依次消耗剩余额度。
不再出现「研究拿满额、创作另给一半」的拆分。

所有 LLM 调用都通过 `llm.invoke(messages, budget=budget)` 传递预算，
由 `LLMClient._execute` 内部负责预占与累计 —— 外部不再手动预占。

状态
----
`final_status` 的取值刻意区分「流程跑完」与「内容可用」：

| 值 | 含义 |
|---|---|
| `draft_ready` | 已生成草稿，未审核（`enable_review=False`） |
| `pending_review` | 已审核，需人工确认（**正常终态**） |
| `needs_manual_review` | 审核发现阻断级问题或修订后仍不通过 |
| `aborted` | 证据不足，主动停止 |
| `budget_exhausted` | 预算耗尽，流程中断 |
| `failed` | 程序错误 |

**没有任何一个状态表示「可以自动发布」。**
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from powercopy.copywriting.agent import CopywritingAgent
from powercopy.copywriting.contracts import (
    ContentDraft,
    CopywritingBudget,
    CopywritingRequest,
    CopywritingStatus,
    GenerationMode,
    Platform,
)
from powercopy.copywriting.mock import MockCopywriterLLM
from powercopy.core.shared_budget import SharedBudget
from powercopy.research.agent import ResearchAgent
from powercopy.research.contracts import (
    EvidencePackage,
    ResearchBudget,
    ResearchRequest,
    ResearchStatus,
    utc_now_iso,
)
from powercopy.research.mock import MockResearchLLM
from powercopy.research.verification import PolicyCorpus
from powercopy.review.agent import ReviewAgent
from powercopy.review.contracts import (
    ReviewReport,
    ReviewStatus,
    RevisionResult,
)
from powercopy.review.mock import MockReviewerLLM, MockReviserLLM
from powercopy.tools.registry import ToolRegistry


class WorkflowStatus(StrEnum):
    """流程终态。**没有一个值表示可自动发布。**"""

    DRAFT_READY = "draft_ready"
    PENDING_REVIEW = "pending_review"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"
    ABORTED = "aborted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class ContentWorkflowRequest(BaseModel):
    """统一业务输入。

    严格校验：拒绝任何未声明字段（路径 / URL / 密钥）。

    关于 `strict`：本契约**不用** `strict=True`。
    原因是它是 API 边界，`workbench` 收到的输入来自 JSON，
    `platform` 必然是字符串 `"wechat_mp"` 而不是 `Platform` 枚举实例。
    `strict=True` 会让合法的 JSON 请求被拒绝。
    安全边界由 `extra="forbid"` 保证（拒绝任何未声明的字段）。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    topic: str = Field(min_length=1, max_length=200)
    target_audience: str = Field(default="", max_length=100)
    region: str = Field(default="", max_length=40)
    platform: Platform = Field(default=Platform.WECHAT_MP)
    target_chars: int = Field(default=800, ge=100, le=5_000)
    style: str = Field(default="专业、通俗、可信", max_length=200)
    purpose: str = Field(default="", max_length=500)

    mode: GenerationMode = Field(
        default=GenerationMode.MOCK,
        description="mock 不产生费用；live 需显式授权",
    )

    # 是否执行审核阶段（可先只跑 research → draft）
    enable_review: bool = Field(default=True)
    enable_revision: bool = Field(default=True)
    max_revisions: int = Field(default=1, ge=0, le=3)

    # 整个任务的共享预算
    max_api_attempts: int = Field(default=12, ge=1, le=60)
    max_known_tokens: int = Field(default=40_000, ge=1_000, le=400_000)

    # 阶段内部上限（受共享预算约束，不额外给额度）
    max_questions: int = Field(default=5, ge=1, le=12)
    max_tool_calls: int = Field(default=8, ge=1, le=30)

    @model_validator(mode="after")
    def _check(self):
        if self.max_revisions > self.max_api_attempts:
            raise ValueError("max_revisions 不能超过 max_api_attempts")
        return self


class ContentWorkflowResult(BaseModel):
    """统一业务输出。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=8, max_length=64)
    topic: str
    mode: str

    final_status: WorkflowStatus = WorkflowStatus.FAILED
    status_reason: str = Field(default="", max_length=1_000)

    # 阶段产物
    research: EvidencePackage | None = None
    draft: ContentDraft | None = None
    review: ReviewReport | None = None
    revision: RevisionResult | None = None

    # 运行信息
    budget_snapshot: dict = Field(default_factory=dict)
    stage_timings_ms: dict = Field(default_factory=dict)
    model_name: str = Field(default="", max_length=100)
    created_at: str = Field(default_factory=utc_now_iso)
    schema_version: str = Field(default="content-workflow-v1")

    # ---- 视图 ----

    def requires_human(self) -> bool:
        """是否需要人工处理（本阶段几乎所有正常结果都需要）。"""

        if self.final_status in (
            WorkflowStatus.NEEDS_MANUAL_REVIEW,
            WorkflowStatus.BUDGET_EXHAUSTED,
            WorkflowStatus.FAILED,
        ):
            return True
        if self.review is not None:
            return self.review.requires_human()
        return True

    def statistics(self) -> dict:
        research_stats = (
            self.research.statistics() if self.research else {}
        )
        review_stats = self.review.statistics() if self.review else {}

        return {
            "task_id": self.task_id,
            "final_status": str(self.final_status),
            "mode": self.mode,
            "research_status": (
                str(self.research.research_status) if self.research else None
            ),
            "research_claims_text_verified": research_stats.get(
                "claims_text_verified"
            ),
            "research_claims_human_confirmed": research_stats.get(
                "claims_human_confirmed"
            ),
            "draft_status": (
                str(self.draft.status) if self.draft else None
            ),
            "draft_char_count": (
                self.draft.char_count() if self.draft else 0
            ),
            "review_status": (
                str(self.review.status) if self.review else None
            ),
            "review_blocking": review_stats.get("blocking"),
            "revision_attempt": (
                self.revision.attempt if self.revision else 0
            ),
            "revision_changed": (
                self.revision.changed if self.revision else False
            ),
            "requires_human": self.requires_human(),
            "budget": self.budget_snapshot,
            "note": (
                "final_status 没有任何值表示可自动发布；"
                "所有产物默认待人工审核"
            ),
        }

    def summary(self) -> dict:
        return {
            "task_id": self.task_id,
            "topic": self.topic,
            "mode": self.mode,
            "final_status": str(self.final_status),
            "status_reason": self.status_reason,
            "has_research": self.research is not None,
            "has_draft": self.draft is not None,
            "has_review": self.review is not None,
            "revision_attempt": (
                self.revision.attempt if self.revision else 0
            ),
            "requires_human": self.requires_human(),
        }


class ContentWorkflowService:
    """统一业务服务。

    CLI 与 workbench API 都通过它调用，避免各自拼装流程。
    """

    def __init__(
        self,
        corpus: PolicyCorpus,
        registry: ToolRegistry,
        *,
        research_llm=None,
        copywriting_llm=None,
        reviewer_llm=None,
        reviser_llm=None,
        model_name: str = "",
    ):
        """
        Args:
            corpus: 政策库与切片清单
            registry: 工具注册表
            research_llm / copywriting_llm / reviewer_llm / reviser_llm:
                各阶段的模型客户端。None 时按 mode 使用 Mock
            model_name: 模型名（写入结果）
        """

        self.corpus = corpus
        self.registry = registry
        self.research_llm = research_llm
        self.copywriting_llm = copywriting_llm
        self.reviewer_llm = reviewer_llm
        self.reviser_llm = reviser_llm
        self.model_name = model_name

    def run(self, request: ContentWorkflowRequest) -> ContentWorkflowResult:
        """执行完整流程。"""

        task_id = f"task-{uuid.uuid4().hex[:16]}"
        timings: dict[str, float] = {}

        budget = SharedBudget(
            max_api_attempts=request.max_api_attempts,
            max_known_tokens=request.max_known_tokens,
        )

        # ---- 阶段 1：研究 ----
        started = time.perf_counter()

        # ResearchBudget 的字段上限（max_api_attempts ≤ 30、
        # max_known_tokens ≤ 200000）比 ContentWorkflowRequest 更小。
        # 真正的约束是共享预算，这里的阶段字段只是契约占位，
        # 因此按契约上限取小值，避免透传触发 ValidationError。
        research_request = ResearchRequest(
            topic=request.topic,
            target_audience=request.target_audience,
            region=request.region,
            research_objective=request.purpose,
            task_budget=ResearchBudget(
                max_questions=request.max_questions,
                max_tool_calls=request.max_tool_calls,
                max_api_attempts=min(request.max_api_attempts, 30),
                max_known_tokens=min(request.max_known_tokens, 200_000),
            ),
        )

        research_llm = self.research_llm or MockResearchLLM(
            max_questions=request.max_questions
        )
        research_agent = ResearchAgent(
            research_llm,
            self.registry,
            self.corpus,
            budget=research_request.task_budget,
            shared_budget=budget,
        )
        research = research_agent.run(research_request)
        timings["research_ms"] = round((time.perf_counter() - started) * 1000, 1)

        if budget.exhausted:
            return self._result(
                task_id, request, research=research,
                final_status=WorkflowStatus.BUDGET_EXHAUSTED,
                status_reason=(
                    f"研究阶段耗尽预算（{budget.exhausted_by}），流程中断"
                ),
                budget=budget, timings=timings,
            )

        # ---- 阶段 2：文案 ----
        started = time.perf_counter()

        # CopywritingBudget 的字段上限比 ContentWorkflowRequest 更小
        # （max_known_tokens ≤ 100000，max_api_attempts ≤ 20）。
        # 共享预算才是真正的约束，这里的阶段上限只是契约占位，
        # 因此按契约上限取小值，避免 Transparent 透传触发 ValidationError。
        copy_request = CopywritingRequest(
            topic=request.topic,
            target_audience=request.target_audience,
            platform=request.platform,
            target_chars=request.target_chars,
            style=request.style,
            purpose=request.purpose,
            evidence_package=research,
            budget=CopywritingBudget(
                max_api_attempts=min(request.max_api_attempts, 20),
                max_known_tokens=min(request.max_known_tokens, 100_000),
            ),
        )

        copy_llm = self.copywriting_llm or MockCopywriterLLM()
        copy_agent = CopywritingAgent(
            copy_llm, mode=request.mode, model_name=self.model_name or "mock"
        )
        draft = copy_agent.run(copy_request)
        timings["draft_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if draft.status is CopywritingStatus.ABORTED:
            return self._result(
                task_id, request, research=research, draft=draft,
                final_status=WorkflowStatus.ABORTED,
                status_reason=draft.status_reason,
                budget=budget, timings=timings,
            )

        if draft.status is CopywritingStatus.FAILED:
            return self._result(
                task_id, request, research=research, draft=draft,
                final_status=WorkflowStatus.FAILED,
                status_reason=draft.status_reason,
                budget=budget, timings=timings,
            )

        if budget.exhausted:
            return self._result(
                task_id, request, research=research, draft=draft,
                final_status=WorkflowStatus.BUDGET_EXHAUSTED,
                status_reason=(
                    f"文案阶段耗尽预算（{budget.exhausted_by}），"
                    f"未执行审核"
                ),
                budget=budget, timings=timings,
            )

        # ---- 阶段 3-4：审核 + 修订 ----
        if not request.enable_review:
            return self._result(
                task_id, request, research=research, draft=draft,
                final_status=WorkflowStatus.DRAFT_READY,
                status_reason="已生成草稿；按请求跳过审核，需人工审核",
                budget=budget, timings=timings,
            )

        started = time.perf_counter()
        review_agent = ReviewAgent(
            research,
            reviewer_llm=self.reviewer_llm or MockReviewerLLM(),
            reviser_llm=(
                self.reviser_llm or MockReviserLLM()
                if request.enable_revision
                else None
            ),
            max_revisions=request.max_revisions,
            model_name=self.model_name or "mock",
            review_mode=str(request.mode),
            budget=budget,
        )
        report, final_draft, revision = review_agent.run(draft)
        timings["review_ms"] = round((time.perf_counter() - started) * 1000, 1)

        # ---- 判定终态 ----
        if budget.exhausted:
            status = WorkflowStatus.BUDGET_EXHAUSTED
            reason = f"审核阶段耗尽预算（{budget.exhausted_by}）"
        elif report.status is ReviewStatus.FAILED:
            status = WorkflowStatus.NEEDS_MANUAL_REVIEW
            reason = f"审核未通过：{report.status_reason}"
        elif report.status is ReviewStatus.NEEDS_REVISION:
            status = WorkflowStatus.NEEDS_MANUAL_REVIEW
            reason = (
                f"修订后仍有待处理问题：{report.status_reason}"
                if revision.attempt
                else report.status_reason
            )
        elif report.blocking_issues():
            status = WorkflowStatus.NEEDS_MANUAL_REVIEW
            reason = f"存在 {len(report.blocking_issues())} 项阻断级问题"
        else:
            status = WorkflowStatus.PENDING_REVIEW
            reason = (
                "流程完成，产物待人工审核。"
                "审核仅表示程序未发现已知硬性问题，不代表内容可发布"
            )

        return self._result(
            task_id, request, research=research, draft=final_draft,
            review=report, revision=revision,
            final_status=status, status_reason=reason,
            budget=budget, timings=timings,
        )

    # ---- 内部 ----

    def _result(
        self,
        task_id: str,
        request: ContentWorkflowRequest,
        *,
        final_status: WorkflowStatus,
        status_reason: str,
        budget: SharedBudget,
        timings: dict,
        research: EvidencePackage | None = None,
        draft: ContentDraft | None = None,
        review: ReviewReport | None = None,
        revision: RevisionResult | None = None,
    ) -> ContentWorkflowResult:
        return ContentWorkflowResult(
            task_id=task_id,
            topic=request.topic,
            mode=str(request.mode),
            final_status=final_status,
            status_reason=status_reason,
            research=research,
            draft=draft,
            review=review,
            revision=revision,
            budget_snapshot=budget.to_dict(),
            stage_timings_ms=timings,
            model_name=self.model_name or "mock",
        )


__all__ = [
    "ContentWorkflowRequest",
    "ContentWorkflowResult",
    "ContentWorkflowService",
    "WorkflowStatus",
]
