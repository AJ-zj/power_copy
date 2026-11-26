"""研究任务输入契约与证据包输出契约。

设计原则
--------
1. **输入不允许指定路径**：`ResearchRequest` 里没有任何 path / url / db 字段，
   且 `extra="forbid"`。模型或 API 用户无法通过研究任务影响文件系统或网络。
2. **证据 ID 由程序生成**：`EvidenceItem.evidence_id` 不接受模型赋值，
   由 `EvidenceVerifier` 根据 (policy_id, version, chunk_id, start, end) 计算。
3. **分层证据状态**：区分
   - `CANDIDATE`   仅来自检索候选，未核验
   - `VERIFIED`    原文偏移与引文已通过程序核验
   - `SUPPORTED`   通过支持性审核（本轮不实现，恒为 pending_review）
4. **不把「最新观测版本」当「现行有效」**：`PolicyValidityStatus` 只有
   `latest_observed` 一个默认值，且必须配 `validity_note`。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

# ---------------------------------------------------------------- 常量

MAX_TOPIC_CHARS = 200
MAX_AUDIENCE_CHARS = 100
MAX_REGION_CHARS = 40
MAX_OBJECTIVE_CHARS = 1000
MAX_QUESTION_CHARS = 300
MAX_QUESTIONS = 12
MAX_CLAIMS = 60
MAX_EVIDENCE_PER_CLAIM = 8


# ---------------------------------------------------------------- 枚举


class ResearchStatus(StrEnum):
    """研究任务的完成度。"""

    COMPLETED = "completed"
    """所有研究问题都有至少一条已核验证据支撑的回答。"""

    PARTIAL = "partial"
    """部分完成：有未解决的问题或未核验的证据。必须附未完成说明。"""

    FAILED = "failed"
    """研究失败：检索不可用、预算耗尽且无有效产出等。"""


class ClaimStatus(StrEnum):
    """单条候选事实的状态。

    注意：这里刻意区分「原文存在」与「语义成立」。
    `VERIFIED` 只表示**原文引文核验通过**，不表示该事实在语义上被充分支持。
    """

    CANDIDATE = "candidate"
    """仅由模型提出，尚未核验。"""

    VERIFIED = "verified"
    """原文偏移、引文、哈希均已程序核验。语义支持性未判定。"""

    PENDING_REVIEW = "pending_review"
    """语义支持性需人工确认。当引文核验通过但无法判定语义支持时使用。"""

    REJECTED = "rejected"
    """引文核验失败（偏移越界、引文不匹配、哈希不符等）。"""


class EvidenceStatus(StrEnum):
    """单条证据的状态。"""

    CANDIDATE = "candidate"
    """仅来自检索候选，未做原文字符核验。"""

    VERIFIED = "verified"
    """原文已重新读取，偏移与引文均已核验。"""


class PolicyValidityStatus(StrEnum):
    """政策时效状态。

    **只有 latest_observed 是当前能给出的判断**：
    本地库只记录「最近一次观测到的版本」，无法判断政策是否已被废止。
    """

    LATEST_OBSERVED = "latest_observed"
    """本地库中该 policy 的最新观测版本。不表示现行有效。"""

    SUPERSEDED_IN_CORPUS = "superseded_in_corpus"
    """库中该 policy 存在更新的版本。"""

    UNKNOWN = "unknown"
    """无法确认（库不可读、政策不在库中等）。"""


# ---------------------------------------------------------------- 基础类型

TopicText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TOPIC_CHARS)
]
AudienceText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_AUDIENCE_CHARS)
]
RegionText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_REGION_CHARS)
]
ObjectiveText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_OBJECTIVE_CHARS)
]
QuestionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
]


class ResearchBudget(BaseModel):
    """研究任务预算。由程序强制，模型无法突破。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    max_rounds: int = Field(default=6, ge=1, le=12)
    max_tool_calls: int = Field(default=12, ge=1, le=30)
    max_api_attempts: int = Field(default=10, ge=1, le=30)
    max_known_tokens: int = Field(default=30_000, ge=1_000, le=200_000)
    max_questions: int = Field(default=8, ge=1, le=MAX_QUESTIONS)
    max_evidence_per_question: int = Field(default=5, ge=1, le=10)


class ResearchRequest(BaseModel):
    """研究任务输入。

    严格校验：拒绝任何未声明字段（包括 path / url / db 等）。
    """

    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    topic: TopicText = Field(
        description="研究主题，例如「工商业企业参与电力市场前需要了解的基本规则」"
    )
    target_audience: AudienceText = Field(
        default="", description="目标客户群体，可留空"
    )
    region: RegionText = Field(
        default="", description="目标地区。留空表示不限地区（不代表全国适用）"
    )
    research_objective: ObjectiveText = Field(
        default="", description="研究目的，用于约束输出侧重"
    )
    required_questions: list[QuestionText] = Field(
        default_factory=list,
        max_length=MAX_QUESTIONS,
        description="用户指定的研究问题。为空时由模型自行拆解",
    )
    task_budget: ResearchBudget = Field(default_factory=ResearchBudget)

    @model_validator(mode="after")
    def _check_questions_unique(self):
        if len(set(self.required_questions)) != len(self.required_questions):
            raise ValueError("required_questions 中存在重复问题")
        return self


class ResearchQuestion(BaseModel):
    """一条研究问题及其解决状态。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=64)
    text: QuestionText
    answered: bool = False
    answer: str = Field(default="", max_length=4_000)
    claim_ids: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """一条证据。

    `evidence_id` 由程序生成，模型不能赋值 —— 构造时若传入非法 ID 会被
    校验拒绝，`EvidenceVerifier` 会用 canonical 字段重算。
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=16, max_length=32)
    policy_id: str = Field(min_length=1, max_length=64)
    document_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    chunk_id: str = Field(min_length=1, max_length=128)
    source_url: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=300)
    exact_quote: str = Field(min_length=1, max_length=2_000)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    page: int | None = None
    content_sha256: str = Field(min_length=64, max_length=64)
    status: EvidenceStatus = EvidenceStatus.CANDIDATE

    @model_validator(mode="after")
    def _check_span(self):
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        if self.end - self.start != len(self.exact_quote):
            raise ValueError(
                f"引文长度（{len(self.exact_quote)}）与区间长度"
                f"（{self.end - self.start}）不一致"
            )
        return self


class Claim(BaseModel):
    """一条候选事实。

    必须关联至少一个 evidence_id；`EvidenceVerifier` 会校验这些 ID 真实存在。
    """

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=64)
    question_id: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[str] = Field(min_length=1, max_length=MAX_EVIDENCE_PER_CLAIM)
    status: ClaimStatus = ClaimStatus.CANDIDATE
    support_note: str = Field(
        default="",
        max_length=500,
        description="语义支持性说明。程序无法自动判定，需人工或后续机制确认",
    )

    @model_validator(mode="after")
    def _check_evidence_ids(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids 中存在重复")
        return self


class VerificationIssue(BaseModel):
    """核验发现的问题。"""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(description="claim / evidence / package")
    target_id: str = Field(default="")
    code: str = Field(description="问题代码")
    detail: str = Field(default="", max_length=1_000)


# ---------------------------------------------------------------- 证据包


def compute_evidence_id(
    *, policy_id: str, version: int, chunk_id: str, start: int, end: int
) -> str:
    """由程序生成稳定的 evidence_id。

    只依赖 canonical 字段，相同证据在任何运行中都会得到相同 ID。
    模型无法通过编造任意字符串伪造一个「看起来合法」的 ID —— 除非它
    恰好命中真实证据的 canonical 组合。
    """

    payload = f"{policy_id}|{version}|{chunk_id}|{start}|{end}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidencePackage(BaseModel):
    """结构化证据包 —— 本阶段的最终交付物。

    这是「研究结果」，不是「文案」。下游（MVP-03）基于它生成文案，
    并据 `claims[].evidence_ids` 追溯每条事实的依据。

    关于 `strict`：本契约**不用** `strict=True`。
    原因是它需要支持 JSON 往返（`model_dump(mode="json")` →
    `model_validate(...)`），而 strict 模式下 StrEnum 字段不接受
    普通字符串。输入契约（`ResearchRequest`）仍保持 strict，
    因为那是外部可控的边界。输出契约的可信度由**构造方**保证，
    不依赖 Pydantic 的类型强制。
    """

    model_config = ConfigDict(extra="forbid")

    research_id: str = Field(min_length=8, max_length=64)
    topic: TopicText
    region: str = Field(default="", max_length=MAX_REGION_CHARS)
    target_audience: str = Field(default="", max_length=MAX_AUDIENCE_CHARS)
    research_questions: list[ResearchQuestion] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    policy_validity_status: PolicyValidityStatus = PolicyValidityStatus.UNKNOWN
    policy_validity_note: str = Field(default="", max_length=600)
    research_status: ResearchStatus = ResearchStatus.FAILED
    status_reason: str = Field(default="", max_length=600)
    verification_issues: list[VerificationIssue] = Field(default_factory=list)
    trace_reference: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    schema_version: str = Field(default="evidence-package-v1")

    # ---- 便捷视图 ----

    def evidence_by_id(self) -> dict[str, EvidenceItem]:
        return {item.evidence_id: item for item in self.evidence_items}

    def verified_claim_count(self) -> int:
        return sum(
            1
            for c in self.claims
            if c.status in (ClaimStatus.VERIFIED, ClaimStatus.PENDING_REVIEW)
        )

    def rejected_claim_count(self) -> int:
        return sum(1 for c in self.claims if c.status is ClaimStatus.REJECTED)

    def summary(self) -> dict:
        """供 CLI / API 展示的摘要。"""

        return {
            "research_id": self.research_id,
            "topic": self.topic,
            "research_status": str(self.research_status),
            "questions_total": len(self.research_questions),
            "questions_answered": sum(
                1 for q in self.research_questions if q.answered
            ),
            "claims_total": len(self.claims),
            "claims_verified": self.verified_claim_count(),
            "claims_rejected": self.rejected_claim_count(),
            "evidence_total": len(self.evidence_items),
            "evidence_verified": sum(
                1
                for e in self.evidence_items
                if e.status is EvidenceStatus.VERIFIED
            ),
            "unanswered": len(self.unanswered_questions),
            "issues": len(self.verification_issues),
            "policy_validity_status": str(self.policy_validity_status),
        }


__all__ = [
    "Claim",
    "ClaimStatus",
    "EvidenceItem",
    "EvidencePackage",
    "EvidenceStatus",
    "MAX_CLAIMS",
    "MAX_EVIDENCE_PER_CLAIM",
    "MAX_QUESTIONS",
    "PolicyValidityStatus",
    "ResearchBudget",
    "ResearchQuestion",
    "ResearchRequest",
    "ResearchStatus",
    "VerificationIssue",
    "compute_evidence_id",
    "utc_now_iso",
]
