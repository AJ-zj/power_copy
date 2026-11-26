import json
import time
import logging

from pydantic import ValidationError

from powercopy.tools.registry import ToolRegistry
from powercopy.tools.result import ToolResult
from powercopy.core.trace import trace_tool


logger = logging.getLogger(__name__)


class ToolExecutor:
    """统一工具执行器。"""

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
            success,
            data=None,
            error_type=None,
            error_message=None
        ):
            return ToolResult(
                tool_name=name,
                success=success,
                data=data,
                error_type=error_type,
                error_message=error_message,
                latency_ms=round(
                    (time.perf_counter() - start) * 1000,
                    2
                )
            )

        # 1. 查找工具
        try:
            tool = self.registry.get(name)

        except KeyError:
            return finish(
                success=False,
                error_type="TOOL_NOT_FOUND",
                error_message="指定工具未注册"
            )

        # 2. 解析工具参数
        try:

            if isinstance(arguments, str):
                arguments = json.loads(arguments)

            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是 JSON 对象")

        except (json.JSONDecodeError, ValueError):

            return finish(
                success=False,
                error_type="INVALID_JSON",
                error_message="工具参数不是合法的 JSON 对象"
            )

        # 3. 校验工具参数
        try:

            validated_args = (
                tool.args_schema.model_validate(arguments)
            )

        except ValidationError:

            return finish(
                success=False,
                error_type="INVALID_ARGUMENTS",
                error_message="工具参数不符合 Schema"
            )

        # 4. 执行工具
        try:

            data = tool.handler(
                **validated_args.model_dump()
            )

        except Exception as error:

            logger.warning(
                "Tool execution failed: tool=%s error=%s",
                name,
                type(error).__name__
            )

            return finish(
                success=False,
                error_type="EXECUTION_ERROR",
                error_message="工具执行失败"
            )

        # 5. 返回统一结果
        return finish(
            success=True,
            data=data
        )
