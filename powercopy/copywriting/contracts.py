"""文案生成的数据契约（V2.1 MVP-03-B）。

设计原则
--------
1. **文案默认 `pending_review`**：程序生成的内容不能自动视为可发布。
2. **每条事实陈述必须绑定证据 ID**：`key_points[].evidence_ids` 必须指向
   证据包中**未被拒绝**的 claim 所引用的 evidence。
3. **不混淆「原文核验」与「语义确认」**：文案中的事实引用来源是
   `pending_review` 的 claim，`ContentDraft.statistics` 明确分开统计。
4. **输入不接受路径**：`CopywritingRequest` 无 path / url 字段，
   `extra="forbid"`。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from powercopy.research.contracts import EvidencePackage, utc_now_iso

# ---------------------------------------------------------------- 常量

MAX_TOPIC_CHARS = 200
MAX_AUDIENCE_CHARS = 100
MAX_PLATFORM_CHARS = 40
MAX_STYLE_CHARS = 200
MAX_PURPOSE_CHARS = 500

MIN_TARGET_CHARS = 100
MAX_TARGET_CHARS = 5_000

MAX_TITLE_CHARS = 100
MAX_BODY_CHARS = 20_000
MAX_POINTS = 20
MAX_NOTES = 30

PROMPT_VERSION = "copywriting-v1"


# ---------------------------------------------------------------- 枚举


class CopywritingStatus(StrEnum):
    """文案生成状态。"""

    PENDING_REVIEW = "pending_review"
    """已生成，等待人工审核。**默认且正常的状态**。"""

    FAILED = "failed"
    """生成失败（证据不足、模型输出无效、引用校验失败等）。"""

    ABORTED = "aborted"
    """因证据不足以支撑目标范围，主动停止（不是错误，是负责的行为）。"""


class Platform(StrEnum):
    """发布平台。不同平台影响排版与语气。"""

    WECHAT_MP = "wechat_mp"
    """微信公众号"""

    WEBSITE = "website"
    """官网/落地页"""

    LINKEDIN = "linkedin"
    """LinkedIn"""

    GENERAL = "general"
    """通用"""


class GenerationMode(StrEnum):
    """生成模式。"""

    MOCK = "mock"
    """离线 Mock，不产生费用"""

    LIVE = "live"
    """真实 LLM，需显式授权"""


# ---------------------------------------------------------------- 基础类型

TopicText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TOPIC_CHARS),
]
AudienceText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_AUDIENCE_CHARS)
]
StyleText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_STYLE_CHARS)
]
PurposeText = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_PURPOSE_CHARS)
]


class CopywritingBudget(BaseModel):
    """文案生成的模型调用预算。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    max_api_attempts: int = Field(default=4, ge=1, le=20)
    max_known_tokens: int = Field(default=12_000, ge=500, le=100_000)


class CopywritingRequest(BaseModel):
    """文案生成输入。

    严格校验：拒绝任何未声明字段（路径 / URL / 密钥）。
    `evidence_package` 直接嵌入，不接受引用外部文件。
    """

    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    topic: TopicText = Field(
        description="文案主题，通常与研究主题一致"
    )
    target_audience: AudienceText = Field(
        default="", description="目标客户"
    )
    platform: Platform = Field(
        default=Platform.GENERAL, description="发布平台"
    )
    target_chars: int = Field(
        default=800,
        ge=MIN_TARGET_CHARS,
        le=MAX_TARGET_CHARS,
        description="目标字数（中文字符数）",
    )
    style: StyleText = Field(
        default="专业、通俗、可信", description="表达风格"
    )
    purpose: PurposeText = Field(
        default="", description="创作目的"
    )
    evidence_package: EvidencePackage = Field(
        description="研究阶段产出的证据包。文案只能基于它的事实"
    )
    budget: CopywritingBudget = Field(default_factory=CopywritingBudget)


class KeyPoint(BaseModel):
    """文案中的一个事实性要点。

    必须绑定证据 ID。`evidence_ids` 必须指向证据包中未被拒绝的 claim
    所引用的 evidence —— 由 `CopywritingAgent` 校验。
    """

    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(min_length=1, max_length=6)
    """至少一个证据 ID。空列表会被契约层拒绝。"""

    claim_ids: list[str] = Field(default_factory=list, max_length=6)
    """对应的 claim ID（可追溯）"""

    semantic_status: str = Field(
        default="pending_review",
        max_length=40,
        description=(
            "语义支持性状态。生成阶段恒为 pending_review —— "
            "程序无法判定引文是否真的支持该表述"
        ),
    )

    @model_validator(mode="after")
    def _check_evidence_ids(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids 中存在重复")
        return self


class ContentDraft(BaseModel):
    """文案草稿 —— MVP-03-B 的交付物。

    这是「待审核草稿」，不是「可发布内容」。
    `status` 默认 `pending_review`，`review_notes` 记录必须由人确认的事项。
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str = Field(min_length=8, max_length=64)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    key_points: list[KeyPoint] = Field(default_factory=list, max_length=MAX_POINTS)

    topic: str = Field(default="", max_length=MAX_TOPIC_CHARS)
    target_audience: str = Field(default="", max_length=MAX_AUDIENCE_CHARS)
    platform: str = Field(default="", max_length=MAX_PLATFORM_CHARS)
    style: str = Field(default="", max_length=MAX_STYLE_CHARS)
    target_chars: int = Field(default=0, ge=0)

    review_notes: list[str] = Field(default_factory=list, max_length=MAX_NOTES)
    """必须由人确认的事项（时效、适用范围、措辞风险等）"""

    status: CopywritingStatus = CopywritingStatus.PENDING_REVIEW
    status_reason: str = Field(default="", max_length=600)

    evidence_refs: list[str] = Field(default_factory=list)
    """文案整体引用的 evidence_id 集合（去重）"""

    research_id: str = Field(default="", max_length=64)
    """来源证据包的研究 ID，用于追溯"""

    policy_validity_status: str = Field(default="", max_length=40)
    policy_validity_note: str = Field(default="", max_length=600)
    region_applicability: str = Field(default="", max_length=400)

    generation_mode: GenerationMode = GenerationMode.MOCK
    model_name: str = Field(default="", max_length=100)
    prompt_version: str = Field(default=PROMPT_VERSION, max_length=40)
    token_usage: dict = Field(default_factory=dict)
    """{"input_tokens": .., "output_tokens": .., "total_tokens": ..,
        "api_attempts": .., "usage_incomplete": bool}"""

    created_at: str = Field(default_factory=utc_now_iso)
    schema_version: str = Field(default="content-draft-v1")

    # ---- 便捷视图 ----

    def char_count(self) -> int:
        """正文字符数（不含空白）。"""

        return len("".join(self.body.split()))

    def statistics(self) -> dict:
        """分项统计。刻意不合并成一个「可信事实数」。"""

        text_verified = sum(
            1 for p in self.key_points if p.evidence_ids
        )
        pending = sum(
            1 for p in self.key_points if p.semantic_status == "pending_review"
        )
        return {
            "key_points_total": len(self.key_points),
            "key_points_bound_to_evidence": text_verified,
            "key_points_pending_semantic_review": pending,
            "key_points_human_confirmed": 0,
            "human_confirmation_available": False,
            "review_notes": len(self.review_notes),
            "char_count": self.char_count(),
            "note": (
                "bound_to_evidence 仅表示已绑定真实证据 ID；"
                "语义支持性未判定。human_confirmed 恒为 0。"
            ),
        }

    def summary(self) -> dict:
        stats = self.statistics()
        return {
            "draft_id": self.draft_id,
            "title": self.title,
            "status": str(self.status),
            "status_reason": self.status_reason,
            "generation_mode": str(self.generation_mode),
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "char_count": stats["char_count"],
            "target_chars": self.target_chars,
            "key_points_total": stats["key_points_total"],
            "key_points_pending_semantic_review": stats[
                "key_points_pending_semantic_review"
            ],
            "review_notes": stats["review_notes"],
            "evidence_refs": len(self.evidence_refs),
            "research_id": self.research_id,
        }


__all__ = [
    "ContentDraft",
    "CopywritingBudget",
    "CopywritingRequest",
    "CopywritingStatus",
    "GenerationMode",
    "KeyPoint",
    "MAX_TARGET_CHARS",
    "MIN_TARGET_CHARS",
    "PROMPT_VERSION",
    "Platform",
]
