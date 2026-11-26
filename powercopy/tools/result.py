"""工具执行结果的统一数据契约。

三层状态分离（04.9）
====================
旧实现只有一个 `success: bool`，把「工具跑通了」和「业务办成了」混在一起。
一次 `POLICY_VERSION_NOT_FOUND` 和一次 `TypeError` 都变成
`success=False / EXECUTION_ERROR`，模型拿不到可行动的失败原因。

现在拆成三层，各有唯一拥有者：

1. **Execution Status**（执行层）
   - 问题：参数是不是合法 JSON？符合 Schema 吗？handler 调用有没有崩？
   - 拥有者：`ToolExecutor`。handler 无法伪造。
   - 取值：`ok` / `tool_not_found` / `invalid_json` / `invalid_arguments` / `execution_error`

2. **Business Status**（业务层）
   - 问题：业务目标达成了吗？查到东西了吗？还是业务上根本不存在？
   - 拥有者：handler（返回值或抛 `ToolBusinessError`），由 `ToolExecutor` 转写。
   - 取值：`success` / `empty` / `failed` / `not_applicable`

3. **Task Status**（任务层）
   - 问题：整个 Agent 任务成功了吗？
   - 拥有者：`Agent`。**不属于 ToolResult**。
   - 任何单次工具结果都不能直接决定任务状态。

`empty` 与 `failed` 的区别是语义核心：
搜索没命中是**正常业务结果**（`empty`），不是错误；
只有"该做的事做不了"（如数据库不存在、版本不存在）才是 `failed`。

向后兼容
--------
`success` 保留为派生属性，等价于 `execution_status is OK`，
语义是「工具执行层未失败」，与旧行为一致。
`error_type` 保留为派生属性，返回旧的大写错误码，
使既有测试与前端无需改动即可继续工作。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ExecutionStatus(StrEnum):
    """工具执行层状态。由 ToolExecutor 独占写入。"""

    OK = "ok"
    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_JSON = "invalid_json"
    INVALID_ARGUMENTS = "invalid_arguments"
    EXECUTION_ERROR = "execution_error"


class BusinessStatus(StrEnum):
    """业务操作层状态。由 handler 声明，ToolExecutor 转写。"""

    SUCCESS = "success"
    """业务目标达成，且有结果。"""

    EMPTY = "empty"
    """业务执行正常，但没有命中任何结果。这是正常结果，不是错误。"""

    FAILED = "failed"
    """业务失败。必须带 business_code 说明原因。"""

    NOT_APPLICABLE = "not_applicable"
    """执行层已失败，业务层未进入评估。"""


# 旧错误码 → 新执行状态的映射（保持既有断言与前端可用）
_LEGACY_ERROR_TYPE = {
    ExecutionStatus.OK: None,
    ExecutionStatus.TOOL_NOT_FOUND: "TOOL_NOT_FOUND",
    ExecutionStatus.INVALID_JSON: "INVALID_JSON",
    ExecutionStatus.INVALID_ARGUMENTS: "INVALID_ARGUMENTS",
    ExecutionStatus.EXECUTION_ERROR: "EXECUTION_ERROR",
}


class ToolBusinessError(Exception):
    """handler 用这个异常声明「执行成功，但业务失败」。

    ToolExecutor 捕获后：
    - execution_status 记为 OK（因为 handler 确实跑完并给出了结论）
    - business_status 记为 FAILED
    - business_code 保留原始业务错误码，透传给模型

    这与「handler 崩溃」（execution_error）是两个不同的失败类型。
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
    ):
        if not code or not code.strip():
            raise ValueError("业务错误码不能为空")

        self.code = code
        self.message = message
        self.retryable = retryable

        super().__init__(code)


@dataclass(frozen=True)
class ToolOutcome:
    """handler 可以选择返回它，用来显式声明业务状态。

    不返回 ToolOutcome 的 handler 视为业务成功（向后兼容）。
    """

    data: Any
    business_status: BusinessStatus = BusinessStatus.SUCCESS
    business_code: str | None = None


@dataclass
class ToolResult:
    """统一工具执行结果（三层状态中的前两层）。"""

    tool_name: str

    execution_status: ExecutionStatus
    business_status: BusinessStatus

    data: Any = None

    business_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    latency_ms: float = 0.0

    # ---- 派生属性：只为向后兼容保留，新代码请直接读上面两个状态 ----

    @property
    def success(self) -> bool:
        """执行层是否成功。等价于旧语义，不含业务判断。"""
        return self.execution_status is ExecutionStatus.OK

    @property
    def error_type(self) -> str | None:
        """旧的大写错误码。仅由执行层状态派生。"""
        return _LEGACY_ERROR_TYPE[self.execution_status]

    # ---- 语义辅助 ----

    @property
    def business_ok(self) -> bool:
        """业务是否正常（成功或空结果都算正常）。"""
        return self.business_status in {
            BusinessStatus.SUCCESS,
            BusinessStatus.EMPTY,
        }

    @property
    def has_business_failure(self) -> bool:
        return self.business_status is BusinessStatus.FAILED

    def to_dict(self) -> dict:
        """序列化给模型与前端。

        保留旧字段名（success / error_type）以免破坏既有调用方，
        同时输出新的结构化字段。
        """
        return {
            # 新字段
            "execution_status": str(self.execution_status),
            "business_status": str(self.business_status),
            "business_code": self.business_code,
            "retryable": self.retryable,
            # 兼容字段
            "tool_name": self.tool_name,
            "success": self.success,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "data": self.data,
            "latency_ms": self.latency_ms,
        }
