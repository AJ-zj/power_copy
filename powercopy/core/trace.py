from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from uuid import uuid4
from datetime import datetime, timezone
from copy import deepcopy
import json
import logging


logger = logging.getLogger(__name__)


class ExecutionTrace:
    """单次 Agent 任务的运行轨迹。"""

    def __init__(self):
        self.task_id = uuid4().hex
        self.status = "running"
        self.stop_reason = None

        self.started_at = datetime.now(
            timezone.utc
        ).isoformat()

        self._started = perf_counter()
        self.duration_ms = None
        self.events = []

        self._budget = None
        self.budget_snapshot = {}

    def attach_budget(self, budget):
        self._budget = budget

    def record(self, event_type: str, **data):
        event = {
            "event_id": len(self.events) + 1,
            "event_type": event_type,
            "offset_ms": round(
                (perf_counter() - self._started) * 1000,
                3
            ),
            **data,
        }

        self.events.append(event)

    def finish(self, status: str, stop_reason: str):
        self.status = status
        self.stop_reason = stop_reason

        if self._budget is not None:
            self.budget_snapshot = self._budget.snapshot()

        self.duration_ms = round(
            (perf_counter() - self._started) * 1000,
            3
        )

        self.record(
            "task_finished",
            status=status,
            stop_reason=stop_reason,
        )

    def snapshot(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "budget": deepcopy(self.budget_snapshot),
            "events": deepcopy(self.events),
        }


# 当前执行上下文中的任务 Trace
_current_trace = ContextVar(
    "powercopy_execution_trace",
    default=None
)


def get_current_trace():
    return _current_trace.get()


def attach_trace_budget(budget):
    trace = get_current_trace()

    if trace is not None:
        trace.attach_budget(budget)


def record_trace_event(event_type: str, **data):
    trace = get_current_trace()

    if trace is not None:
        trace.record(event_type, **data)


def trace_task(func):
    """观察完整 Agent 任务，不修改原有控制流程。"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        trace = ExecutionTrace()
        token = _current_trace.set(trace)

        trace.record("task_started")

        try:
            result = func(*args, **kwargs)

            reason = result.get("stop_reason") or result["status"]

            if reason == "completed":
                status = "success"

            elif reason in {
                "max_rounds",
                "tool_budget_exhausted",
                "api_budget_exhausted",
                "token_budget_exhausted",
            }:
                status = "stopped"

            else:
                status = "failed"

            trace.finish(status, reason)

            result["task_id"] = trace.task_id
            result["trace"] = trace.snapshot()

            return result

        except Exception as exc:
            trace.record(
                "task_error",
                error_type=type(exc).__name__,
            )

            trace.finish(
                "failed",
                type(exc).__name__,
            )

            # 保留原有异常抛出行为，同时输出运行轨迹
            logger.error(
                "agent_task_failed trace=%s",
                json.dumps(
                    trace.snapshot(),
                    ensure_ascii=False,
                ),
            )

            raise

        finally:
            _current_trace.reset(token)

    return wrapper


def trace_model_attempt(func):
    """观察单次实际模型请求。"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        trace = get_current_trace()

        if trace is None:
            return func(*args, **kwargs)

        started = perf_counter()

        try:
            result = func(*args, **kwargs)

        except Exception as exc:
            trace.record(
                "model_attempt",
                status="failed",
                error_type=type(exc).__name__,
                duration_ms=round(
                    (perf_counter() - started) * 1000,
                    3
                ),
            )

            raise

        trace.record(
            "model_attempt",
            status="success",
            duration_ms=round(
                (perf_counter() - started) * 1000,
                3
            ),
        )

        return result

    return wrapper


def trace_tool(func):
    """观察工具执行。

    04.9：事件同时记录执行层与业务层状态。
    `status` 保持旧语义（执行层），新增 `business_status` 与
    `business_code` 以区分「工具跑通了但业务失败」与「工具崩溃」。
    任务层状态不在此处，由 `trace_task` 的 finish 统一决定。
    """

    @wraps(func)
    def wrapper(self, name, arguments):
        trace = get_current_trace()

        if trace is None:
            return func(self, name, arguments)

        started = perf_counter()

        try:
            result = func(self, name, arguments)

        except Exception as exc:
            trace.record(
                "tool_execution",
                tool_name=name,
                status="failed",
                error_type=type(exc).__name__,
                duration_ms=round(
                    (perf_counter() - started) * 1000,
                    3
                ),
            )

            raise

        # 执行层状态沿用旧字段名，保证既有断言与前端不变
        execution_status = getattr(result, "execution_status", None)
        business_status = getattr(result, "business_status", None)

        trace.record(
            "tool_execution",
            tool_name=name,
            status=(
                "success" if result.success else "failed"
            ),
            error_type=result.error_type,
            execution_status=(
                str(execution_status) if execution_status is not None else None
            ),
            business_status=(
                str(business_status) if business_status is not None else None
            ),
            business_code=getattr(result, "business_code", None),
            duration_ms=round(
                (perf_counter() - started) * 1000,
                3
            ),
        )

        return result

    return wrapper
