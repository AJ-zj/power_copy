"""Execution Trace 十一项验收脚本。

全部使用 Mock，不联网。

用法：
    python scripts/check_trace.py

验收十一项：
    1. 每个任务生成唯一 task_id
    2. 模型调用事件正确记录
    3. 工具调用事件正确记录
    4. 结构化工具失败正确识别
    5. 模型异常与重试事件正确记录
    6. 任务状态、停止原因及预算快照正确
    7. 任务与单次调用耗时正确记录
    8. 不同任务 Trace 隔离
    9. 异常退出时保留轨迹并清理上下文
    10. 真实 Agent Runtime Mock 接线验证
    11. 全量回归（由 pytest 保证）

退出码：
    0  十项行为验收全部通过
    1  有项目未通过
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2 as httpx  # noqa: E402
from openai import APIStatusError  # noqa: E402

from powercopy.core import trace as trace_module  # noqa: E402
from powercopy.core.agent import Agent  # noqa: E402
from powercopy.core.llm import LLMClient  # noqa: E402
from powercopy.core.trace import (  # noqa: E402
    ExecutionTrace,
    get_current_trace,
    trace_task,
)
from powercopy.tools.builtin.policy import policy_search_tool  # noqa: E402
from powercopy.tools.registry import ToolRegistry  # noqa: E402

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")


def status_error(code):
    return APIStatusError(
        message="mock", response=httpx.Response(code, request=REQUEST), body=None
    )


def tool_call(call_id="c1", name="search_policy", arguments=None):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments
            if isinstance(arguments, str)
            else json.dumps(
                arguments or {"region": "安徽省", "keyword": "电价"},
                ensure_ascii=False,
            ),
        },
    }


def build_client(scripted):
    """真实 LLMClient + 脚本化底层 create，让装饰器真实生效。"""
    import os

    os.environ.setdefault("LLM_API_KEY", "sk-test")
    os.environ.setdefault("LLM_BASE_URL", "https://example.com/v1")
    os.environ.setdefault("LLM_MODEL", "glm-5.3-flash")
    os.environ.setdefault("LLM_TIMEOUT", "60")

    client = LLMClient()
    state = {"i": 0}

    def fake_create(**kwargs):
        index = state["i"]
        state["i"] += 1
        behavior = scripted[min(index, len(scripted) - 1)]
        if isinstance(behavior, Exception):
            raise behavior

        tokens = behavior.get("total_tokens", 15)
        calls = behavior.get("tool_calls")
        tool_calls = None
        if calls:
            tool_calls = [
                SimpleNamespace(
                    id=c["id"],
                    type=c["type"],
                    function=SimpleNamespace(
                        name=c["function"]["name"],
                        arguments=c["function"]["arguments"],
                    ),
                )
                for c in calls
            ]
        message = SimpleNamespace(
            content=behavior.get("content"), tool_calls=tool_calls
        )
        choice = SimpleNamespace(
            message=message, finish_reason=behavior.get("finish_reason", "stop")
        )
        usage = SimpleNamespace(
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - tokens // 2,
            total_tokens=tokens,
        )
        return SimpleNamespace(choices=[choice], usage=usage, model="glm-5.3-flash")

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    return client, state


def run_agent(scripted, **agent_kwargs):
    registry = ToolRegistry()
    registry.register(policy_search_tool)
    client, state = build_client(scripted)
    agent = Agent(
        llm=client, registry=registry, system_prompt="提示", **agent_kwargs
    )
    return agent.run("查询安徽电价"), state


def events_of(trace, event_type):
    return [e for e in trace["events"] if e["event_type"] == event_type]


def check_task_id() -> bool:
    print("\n[1/11] 每个任务生成唯一 task_id")

    first, _ = run_agent([{"content": "完成", "finish_reason": "stop"}])
    second, _ = run_agent([{"content": "完成", "finish_reason": "stop"}])

    checks = {
        "task_id 非空": bool(first.get("task_id")),
        "task_id 长度 32": len(first.get("task_id", "")) == 32,
        "两次任务不同": first["task_id"] != second["task_id"],
        "trace 中 task_id 一致": first["trace"]["task_id"] == first["task_id"],
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_model_events() -> bool:
    print("\n[2/11] 模型调用事件正确记录")

    result, _ = run_agent(
        [
            {"content": None, "tool_calls": [tool_call()], "finish_reason": "tool_calls"},
            {"content": "完成", "finish_reason": "stop"},
        ]
    )
    attempts = events_of(result["trace"], "model_attempt")

    checks = {
        "记录 2 次模型调用": len(attempts) == 2,
        "均为 success": all(a["status"] == "success" for a in attempts),
        "含 duration_ms": all(isinstance(a["duration_ms"], float) for a in attempts),
        "含 offset_ms": all("offset_ms" in a for a in attempts),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_tool_events() -> bool:
    print("\n[3/11] 工具调用事件正确记录")

    result, _ = run_agent(
        [
            {"content": None, "tool_calls": [tool_call()], "finish_reason": "tool_calls"},
            {"content": "完成", "finish_reason": "stop"},
        ]
    )
    tools = events_of(result["trace"], "tool_execution")

    checks = {
        "记录 1 次工具调用": len(tools) == 1,
        "tool_name 正确": tools and tools[0]["tool_name"] == "search_policy",
        "status 为 success": tools and tools[0]["status"] == "success",
        "error_type 为 None": tools and tools[0]["error_type"] is None,
        "含耗时": tools and isinstance(tools[0]["duration_ms"], float),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_structured_failure() -> bool:
    print("\n[4/11] 结构化工具失败正确识别")

    # 非法参数：execute 正常返回 success=False，不抛异常
    bad_args, _ = run_agent(
        [
            {
                "content": None,
                "tool_calls": [tool_call(arguments={"region": "", "keyword": ""})],
                "finish_reason": "tool_calls",
            },
            {"content": "完成", "finish_reason": "stop"},
        ]
    )
    bad_tool = events_of(bad_args["trace"], "tool_execution")

    # 未知工具
    ghost, _ = run_agent(
        [
            {"content": None, "tool_calls": [tool_call(name="ghost")], "finish_reason": "tool_calls"},
            {"content": "完成", "finish_reason": "stop"},
        ]
    )
    ghost_tool = events_of(ghost["trace"], "tool_execution")

    checks = {
        "非法参数记为 failed": bad_tool and bad_tool[0]["status"] == "failed",
        "error_type=INVALID_ARGUMENTS": bad_tool
        and bad_tool[0]["error_type"] == "INVALID_ARGUMENTS",
        "未抛 Python 异常（任务仍完成）": bad_args["status"] == "completed",
        "未知工具记为 failed": ghost_tool and ghost_tool[0]["status"] == "failed",
        "error_type=TOOL_NOT_FOUND": ghost_tool
        and ghost_tool[0]["error_type"] == "TOOL_NOT_FOUND",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_model_failure_events() -> bool:
    print("\n[5/11] 模型异常与重试事件正确记录")

    # 真实 client：第一次 503，第二次成功
    client, _ = build_client(
        [status_error(503), {"content": "ok", "finish_reason": "stop", "total_tokens": 10}]
    )
    import powercopy.core.llm as llm_module

    original_sleep = llm_module.time.sleep
    llm_module.time.sleep = lambda s: None

    trace = ExecutionTrace()
    token = trace_module._current_trace.set(trace)
    try:
        client.invoke([{"role": "user", "content": "hi"}])
    finally:
        trace_module._current_trace.reset(token)
        llm_module.time.sleep = original_sleep

    attempts = [e for e in trace.events if e["event_type"] == "model_attempt"]

    checks = {
        "记录 2 次尝试": len(attempts) == 2,
        "第一次 failed": attempts and attempts[0]["status"] == "failed",
        "第一次 error_type": attempts
        and attempts[0].get("error_type") == "APIStatusError",
        "第二次 success": attempts and attempts[1]["status"] == "success",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_status_and_budget() -> bool:
    print("\n[6/11] 任务状态、停止原因及预算快照")

    # 正常完成
    completed, _ = run_agent(
        [{"content": "完成", "finish_reason": "stop"}],
        max_api_attempts=5,
        max_known_tokens=1000,
    )
    # 达到最大轮数
    maxed, _ = run_agent(
        [
            {"content": None, "tool_calls": [tool_call()], "finish_reason": "tool_calls"}
        ] * 3,
        max_rounds=1,
    )

    checks = {
        "completed -> success": completed["trace"]["status"] == "success",
        "stop_reason=completed": completed["trace"]["stop_reason"] == "completed",
        "预算上限正确": completed["trace"]["budget"]["max_api_attempts"] == 5
        and completed["trace"]["budget"]["max_known_tokens"] == 1000,
        "预算消耗正确": completed["trace"]["budget"]["api_attempts"] == 1,
        "max_rounds -> stopped": maxed["trace"]["status"] == "stopped",
        "stop_reason=max_rounds": maxed["trace"]["stop_reason"] == "max_rounds",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_durations() -> bool:
    print("\n[7/11] 任务与单次调用耗时")

    result, _ = run_agent(
        [
            {"content": None, "tool_calls": [tool_call()], "finish_reason": "tool_calls"},
            {"content": "完成", "finish_reason": "stop"},
        ]
    )
    trace = result["trace"]
    offsets = [e["offset_ms"] for e in trace["events"]]

    checks = {
        "任务耗时存在": isinstance(trace["duration_ms"], float),
        "任务耗时 >= 0": trace["duration_ms"] >= 0,
        "事件偏移单调": offsets == sorted(offsets),
        "事件偏移 >= 0": all(o >= 0 for o in offsets),
        "模型尝试含耗时": all(
            isinstance(e["duration_ms"], float)
            for e in trace["events"]
            if e["event_type"] == "model_attempt"
        ),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_isolation() -> bool:
    print("\n[8/11] 不同任务 Trace 隔离")

    first, _ = run_agent([{"content": "完成一", "finish_reason": "stop"}])
    second, _ = run_agent([{"content": "完成二", "finish_reason": "stop"}])

    cleaned = get_current_trace() is None

    checks = {
        "task_id 不同": first["task_id"] != second["task_id"],
        "事件列表不同": first["trace"]["events"] != second["trace"]["events"],
        "任务结束后上下文已清理": cleaned,
        "各自状态独立": first["trace"]["status"] == "success"
        and second["trace"]["status"] == "success",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_exception_cleanup() -> bool:
    print("\n[9/11] 异常退出时保留轨迹并清理上下文")

    captured = {}

    @trace_task
    def broken():
        captured["trace"] = get_current_trace()
        raise ValueError("炸了")

    raised = None
    try:
        broken()
    except ValueError as error:
        raised = error

    cleaned = get_current_trace() is None
    trace = captured.get("trace")

    checks = {
        "异常正常抛出": isinstance(raised, ValueError),
        "上下文已清理": cleaned,
        "轨迹记录了 task_error": trace is not None
        and any(e["event_type"] == "task_error" for e in trace.events),
        "状态为 failed": trace is not None and trace.status == "failed",
        "stop_reason 为异常类型": trace is not None
        and trace.stop_reason == "ValueError",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_runtime_wiring() -> bool:
    print("\n[10/11] 真实 Agent Runtime Mock 接线验证")

    result, _ = run_agent(
        [
            {"content": None, "tool_calls": [tool_call()], "finish_reason": "tool_calls"},
            {"content": "最终回答", "finish_reason": "stop"},
        ],
        max_api_attempts=4,
    )

    trace = result["trace"]
    types = [e["event_type"] for e in trace["events"]]

    checks = {
        "任务完成": result["status"] == "completed",
        "有 task_started": "task_started" in types,
        "有 round_started": "round_started" in types,
        "有 2 次 model_attempt": types.count("model_attempt") == 2,
        "有 1 次 tool_execution": types.count("tool_execution") == 1,
        "有 task_completed": "task_completed" in types,
        "有 task_finished": "task_finished" in types,
        "trace 可 JSON 序列化": bool(json.dumps(trace, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · Execution Trace 验收")
    print("全部使用 Mock，不联网")
    print("=" * 64)

    checks = [
        ("每个任务生成唯一 task_id", check_task_id()),
        ("模型调用事件正确记录", check_model_events()),
        ("工具调用事件正确记录", check_tool_events()),
        ("结构化工具失败正确识别", check_structured_failure()),
        ("模型异常与重试事件正确记录", check_model_failure_events()),
        ("任务状态、停止原因及预算快照", check_status_and_budget()),
        ("任务与单次调用耗时", check_durations()),
        ("不同任务 Trace 隔离", check_isolation()),
        ("异常退出时保留轨迹并清理上下文", check_exception_cleanup()),
        ("真实 Agent Runtime Mock 接线验证", check_runtime_wiring()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"行为验收：{passed}/{len(checks)} 通过（第 11 项全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
