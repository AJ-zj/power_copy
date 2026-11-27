"""文案生成（V2.1 MVP-03-B）。

本包只依赖 MVP-02-B 的公开接口：
- `powercopy.research.contracts`（EvidencePackage / Claim / EvidenceItem）
- `powercopy.core.llm`、`powercopy.core.budget`

不修改 research / rag / tools / workbench / frontend。

模块
----
contracts   输入契约（CopywritingRequest）与输出契约（ContentDraft）
guards      文案生成的安全约束（禁用内容检测、引用校验）
agent       CopywritingAgent：基于证据包生成文案
mock        离线 Mock 生成器（供测试与演示）
"""

from powercopy.copywriting.contracts import (
    ContentDraft,
    CopywritingBudget,
    CopywritingRequest,
    CopywritingStatus,
    GenerationMode,
    KeyPoint,
    Platform,
)
from powercopy.copywriting.guards import (
    CitationViolation,
    ForbiddenContentError,
    GuardReport,
    check_forbidden_content,
    validate_citations,
)

__all__ = [
    "CitationViolation",
    "ContentDraft",
    "CopywritingBudget",
    "CopywritingRequest",
    "CopywritingStatus",
    "ForbiddenContentError",
    "GenerationMode",
    "GuardReport",
    "KeyPoint",
    "Platform",
    "check_forbidden_content",
    "validate_citations",
]
