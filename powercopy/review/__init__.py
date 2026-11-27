"""业务审核（V2.1 MVP-04-B）。

审核维度
--------
| 维度 | 能否程序判定 | 说明 |
|---|---|---|
| 引用完整性 | ✅ 完全可以 | 证据 ID / 原文 / 版本 / 偏移 / 引用关系 |
| 事实支持性 | ⚠️ 部分（需 LLM） | 程序无法判断语义，只能靠模型或人工 |
| 宣传合规 | ⚠️ 部分（字面匹配） | 复用 guards，无法识别语义等价改写 |
| 政策适用性 | ✅ 部分 | 时效/地区可程序检查，现行有效性不可 |

**关键立场**

`ReviewReport.status = passed` **不等于**文案可以发布。它只表示：
- 引用完整性通过（程序可确认）
- 没有命中已知的禁用宣传模式
- 没有明确的政策适用性冲突

而「事实是否真的被证据支持」这个判断，如果由 LLM 做出，
**属于机器审核意见，不代表专家确认**。

模块
----
contracts    ReviewReport / ReviewIssue / RevisionResult
verifier     四维审核的实现
agent        ReviewAgent（含有限自动修订）
"""

from powercopy.review.contracts import (
    IssueSeverity,
    ReviewDimension,
    ReviewIssue,
    ReviewReport,
    ReviewStatus,
    ReviewVerdict,
    RevisionResult,
)
from powercopy.review.verifier import ContentVerifier

__all__ = [
    "ContentVerifier",
    "IssueSeverity",
    "ReviewDimension",
    "ReviewIssue",
    "ReviewReport",
    "ReviewStatus",
    "ReviewVerdict",
    "RevisionResult",
]
