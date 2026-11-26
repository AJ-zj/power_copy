import json
import time
import logging

from pydantic import ValidationError

from powercopy.tools.registry import ToolRegistry
from powercopy.tools.result import (
    BusinessStatus,
    ExecutionStatus,
    ToolBusinessError,
    ToolOutcome,
    ToolResult,
)
from powercopy.core.trace import trace_tool


logger = logging.getLogger(__name__)


class ToolExecutor:
    """统一工具执行器。

    职责边界（04.9）：
    - 只负责**执行层**状态：参数解析、Schema 校验、handler 调用
    - **业务层**状态由 handler 声明，executor 只做转写，不替 handler 判断
    - 不涉及**任务层**状态，那是 Agent 的职责
    """

    def __init__(self, registry: ToolRegistry):

        self.registry = registry

    @trace_tool
    def execute(
        self,
        name: str,
        arguments: str | dict
    ) -> ToolResult:

        start = time.perf_counter()

        def finish(
            execution: ExecutionStatus,
            business: BusinessStatus,
            data=None,
            business_code=None,
            error_message=None,
            retryable=False,
        ):
            return ToolResult(
                tool_name=name,
                execution_status=execution,
                business_status=business,
                data=data,
                business_code=business_code,
                error_message=error_message,
                retryable=retryable,
                latency_ms=round(
                    (time.perf_counter() - start) * 1000,
                    2
                )
            )

        # 1. 查找工具（执行层失败）
        try:
            tool = self.registry.get(name)

        except KeyError:
            return finish(
                ExecutionStatus.TOOL_NOT_FOUND,
                BusinessStatus.NOT_APPLICABLE,
                error_message="指定工具未注册"
            )

        # 2. 解析工具参数（执行层失败）
        try:

            if isinstance(arguments, str):
                arguments = json.loads(arguments)

            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")

        except (json.JSONDecodeError, ValueError):

            return finish(
                ExecutionStatus.INVALID_JSON,
                BusinessStatus.NOT_APPLICABLE,
                error_message="工具参数不是合法的 JSON 对象"
            )

        # 3. 校验工具参数（执行层失败）
        try:

            validated_args = (
                tool.args_schema.model_validate(arguments)
            )

        except ValidationError:

            return finish(
                ExecutionStatus.INVALID_ARGUMENTS,
                BusinessStatus.NOT_APPLICABLE,
                error_message="工具参数不符合 Schema"
            )

        # 4. 执行工具
        #
        # 这里区分两类异常：
        #   ToolBusinessError  → handler 正常跑完，业务上判定失败
        #   其他异常           → handler 崩溃，执行层失败
        try:

            outcome = tool.handler(
                **validated_args.model_dump()
            )

        except ToolBusinessError as error:
            # 业务失败：执行层是 OK 的，业务层是 FAILED
            logger.info(
                "Tool business failure: tool=%s code=%s",
                name,
                error.code
            )
            return finish(
                ExecutionStatus.OK,
                BusinessStatus.FAILED,
                business_code=error.code,
                error_message=error.message or error.code,
                retryable=error.retryable,
            )

        except Exception as error:

            logger.warning(
                "Tool execution failed: tool=%s error=%s",
                name,
                type(error).__name__
            )

            return finish(
                ExecutionStatus.EXECUTION_ERROR,
                BusinessStatus.NOT_APPLICABLE,
                error_message="工具执行失败"
            )

        # 5. handler 可以返回 ToolOutcome 显式声明业务状态；
        #    直接返回其他值视为业务成功（向后兼容）
        if isinstance(outcome, ToolOutcome):
            return finish(
                ExecutionStatus.OK,
                outcome.business_status,
                data=outcome.data,
                business_code=outcome.business_code,
            )

        return finish(
            ExecutionStatus.OK,
            BusinessStatus.SUCCESS,
            data=outcome
        )
