from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


# ---------------------------------------------------------------------------
# 兼容保留：采集/解析/清洗管线内部传递用的轻量文档
#
# 这个 dataclass 是早期骨架，被 powercopy/data/{collectors,parsers,storage}
# 的接口契约引用。本节新增的 PolicyDocument 是持久化用的严格数据契约，
# 两者职责不同，因此保留本类不做改动。
# ---------------------------------------------------------------------------


@dataclass
class Document:
    """所有采集器/解析器之间传递的统一文档。"""

    id: str
    title: str
    content: str
    url: str = ""
    source: str = ""
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 政策文档数据契约
# ---------------------------------------------------------------------------


class PolicyDocument(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    document_id: UUID = Field(default_factory=uuid4)
    source_key: str
    source_url: AnyHttpUrl
    title: str
    body_text: str
    region: str | None = None
    issuer: str | None = None
    published_on: date | None = None
    retrieved_at: AwareDatetime

    @field_validator("source_key", "title")
    @classmethod
    def clean_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("body_text")
    @classmethod
    def check_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be blank")
        return value
