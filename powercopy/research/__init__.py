"""政策研究与证据包（V2.1 MVP-02-B）。

本包只依赖已有的公开接口：
- `powercopy.tools.builtin.retrieval`（search_policy_evidence）
- `powercopy.tools.builtin.policy_tools`（search_policy / get_policy_detail）
- `powercopy.core.llm`、`powercopy.core.trace`

不修改 Agent Runtime、Tool 层、RAG 层或 workbench。

模块
----
contracts      输入契约（ResearchRequest）与输出契约（EvidencePackage）
verification   最小证据核验（偏移 / 引文 / 哈希 / 引用完整性）
agent          ResearchAgent：有限研究流程
mock           离线 Mock 研究规划器（供测试与演示）
"""

from powercopy.research.contracts import (
    Claim,
    ClaimStatus,
    EvidenceItem,
    EvidencePackage,
    EvidenceStatus,
    PolicyValidityStatus,
    ResearchBudget,
    ResearchQuestion,
    ResearchRequest,
    ResearchStatus,
    VerificationIssue,
)
from powercopy.research.verification import (
    EvidenceVerifier,
    VerificationReport,
)

__all__ = [
    "Claim",
    "ClaimStatus",
    "EvidenceItem",
    "EvidencePackage",
    "EvidenceStatus",
    "EvidenceVerifier",
    "PolicyValidityStatus",
    "ResearchBudget",
    "ResearchQuestion",
    "ResearchRequest",
    "ResearchStatus",
    "VerificationIssue",
    "VerificationReport",
]
