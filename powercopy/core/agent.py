import json

from powercopy.core.budget import (
    TaskBudget,
    BudgetExceeded
)
from powercopy.core.llm import LLMClient
from powercopy.core.message import MessageManager
from powercopy.core.trace import (
    trace_task,
    attach_trace_budget,
    record_trace_event,
)
from powercopy.tools.registry import ToolRegistry
from powercopy.tools.executor import ToolExecutor


class Agent:

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        system_prompt: str,
        max_rounds: int = 5,
        max_tool_calls: int = 8,
        max_api_attempts: int = 8,
        max_known_tokens: int = 20000
    ):

        if max_rounds < 1 or max_tool_calls < 1:
            raise ValueError("运行预算必须大于零")

        if max_api_attempts < 1 or max_known_tokens < 1:
            raise ValueError("模型调用预算必须大于零")

        self.llm = llm
        self.registry = registry
        self.executor = ToolExecutor(registry)

        self.system_prompt = system_prompt
        self.max_rounds = max_rounds
        self.max_tool_calls = max_tool_calls
        self.max_api_attempts = max_api_attempts
        self.max_known_tokens = max_known_tokens

    @trace_task
    def run(self, task: str) -> dict:
        """执行一个完整的 Agent 任务。"""

        # 1. 初始化当前任务
        #
        # 预算必须在 run() 内创建：每次任务独立计数，
        # 不能放到 __init__ 里被多次 run 复用。
        budget = TaskBudget(
            max_api_attempts=self.max_api_attempts,
            max_known_tokens=self.max_known_tokens
        )

        attach_trace_budget(budget)

        manager = MessageManager(
            system_prompt=self.system_prompt
        )

        manager.add_user(task)

        tools = self.registry.get_schemas()

        if not tools:
            raise ValueError("当前 Agent 没有注册工具")

        tool_requests = 0

        def finish(status, content=None):
            return {
                "status": status,
                "content": content,
                "rounds": round_no,
                "tool_requests": tool_requests,
                "budget": budget.snapshot()
            }

        # 2. Agent Loop
        for round_no in range(1, self.max_rounds + 1):

            record_trace_event("round_started", round=round_no)

            # Decision：模型决策
            try:
                result = self.llm.invoke(
                    messages=manager.get_messages(),
                    tools=tools,
                    budget=budget
                )
            except BudgetExceeded as error:
                record_trace_event(
                    "budget_exceeded", round=round_no, reason=error.reason
                )
                return finish(error.reason)

            tool_calls = result.get("tool_calls", [])
            content = result.get("content")
            reason = result.get("finish_reason")

            # 3. 检查响应是否完整
            if reason in {"length", "content_filter"}:
                record_trace_event(
                    "incomplete_response", round=round_no, finish_reason=reason
                )
                return finish("incomplete_response")

            # 4. 没有工具调用，判断是否正常结束
            if not tool_calls:

                if (
                    reason == "stop"
                    and isinstance(content, str)
                    and content.strip()
                ):
                    manager.add_assistant(content)

                    record_trace_event(
                        "task_completed",
                        round=round_no,
                        content_length=len(content),
                    )

                    return finish(
                        status="completed",
                        content=content
                    )

                return finish("invalid_response")

            # 5. 检查工具调用协议
            if (
                reason != "tool_calls"
                or not self._valid_tool_calls(tool_calls)
            ):
                return finish("invalid_response")

            # 最后一轮不再启动无法反馈给模型的工具调用
            if round_no == self.max_rounds:
                return finish("max_rounds")

            # 整批工具请求必须在剩余预算内
            if (
                tool_requests + len(tool_calls)
                > self.max_tool_calls
            ):
                return finish("tool_budget_exhausted")

            # 模型还要继续调用工具，但已没有后续模型请求预算时，
            # 执行了工具也无法反馈给模型，因此直接停止。
            reason = budget.stop_reason()
            if reason is not None:
                return finish(reason)

            # 6. 保存模型提出的工具调用请求
            manager.add_assistant_tool_calls(
                content=content,
                tool_calls=tool_calls
            )

            # 7. Action：执行模型请求的工具
            for call in tool_calls:

                tool_requests += 1

                tool_result = self.executor.execute(
                    name=call["function"]["name"],
                    arguments=call["function"]["arguments"]
                )

                # 8. Observation：封装工具结果
                try:
                    observation = json.dumps(
                        tool_result.to_dict(),
                        ensure_ascii=False,
                        allow_nan=False
                    )

                except (TypeError, ValueError, RecursionError):
                    observation = json.dumps({
                        "success": False,
                        "error_type": "SERIALIZATION_ERROR",
                        "error_message": (
                            "工具结果无法序列化，"
                            "工具可能已经执行"
                        )
                    }, ensure_ascii=False)

                # 9. 回填结果，供下一轮模型使用
                manager.add_tool(
                    tool_call_id=call["id"],
                    content=observation
                )

        return finish("max_rounds")

    @staticmethod
    def _valid_tool_calls(calls) -> bool:
        """检查工具调用协议及 ID 的有效性。"""

        if not isinstance(calls, list):
            return False

        ids = set()

        for call in calls:

            if not isinstance(call, dict):
                return False

            call_id = call.get("id")
            function = call.get("function")

            if (
                not isinstance(call_id, str)
                or not call_id.strip()
                or call_id in ids
                or call.get("type") != "function"
                or not isinstance(function, dict)
            ):
                return False

            if (
                not isinstance(function.get("name"), str)
                or not function["name"].strip()
                or not isinstance(
                    function.get("arguments"), str
                )
            ):
                return False

            ids.add(call_id)

        return True
