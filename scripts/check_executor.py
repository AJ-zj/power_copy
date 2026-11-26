"""Tool Executor 八项验收脚本。

全部离线，不联网、不调用真实模型。

用法：
    python scripts/check_executor.py

验收八项：
    1. 能够执行合法的字典及 JSON 字符串参数
    2. 未知工具返回 TOOL_NOT_FOUND
    3. 非法 JSON 返回 INVALID_JSON
    4. 非法参数返回 INVALID_ARGUMENTS
    5. 参数校验失败时 handler 绝不执行
    6. 业务函数异常能够被捕获并返回 EXECUTION_ERROR
    7. 统一返回 ToolResult 并记录耗时
    8. 新增测试及全量回归通过（回归由 pytest 保证）

退出码：
    0  七项行为验收全部通过
    1  有项目未通过
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from powercopy.tools.base import Tool  # noqa: E402
from powercopy.tools.builtin.policy import policy_search_tool  # noqa: E402
from powercopy.tools.executor import ToolExecutor  # noqa: E402
from powercopy.tools.registry import ToolRegistry  # noqa: E402
from powercopy.tools.result import ToolResult  # noqa: E402


class EchoInput(BaseModel):
    text: str = Field(min_length=1, description="要返回的文本")


def build_executor(spy_sink=None):
    """构造带 spy 工具的执行器，用于验证 handler 是否被调用。"""
    registry = ToolRegistry()
    registry.register(policy_search_tool)

    def echo_handler(text: str):
        if spy_sink is not None:
            spy_sink.append(text)
        return {"echo": text}

    registry.register(
        Tool(
            name="echo",
            description="原样返回输入文本",
            args_schema=EchoInput,
            handler=echo_handler,
        )
    )
    return ToolExecutor(registry)


def check_valid_arguments() -> bool:
    """验收 1：字典和 JSON 字符串参数都能执行。"""
    print("\n[1/8] 能够执行合法的字典及 JSON 字符串参数")

    executor = build_executor()

    by_dict = executor.execute(
        "search_policy", {"region": "江西省", "keyword": "峰谷电价"}
    )
    by_json = executor.execute(
        "search_policy",
        json.dumps({"region": "安徽省", "keyword": "分时电价"}, ensure_ascii=False),
    )
    by_echo = executor.execute("echo", {"text": "hello"})

    checks = {
        "字典参数成功": by_dict.success and by_dict.data["region"] == "江西省",
        "JSON 字符串成功": by_json.success and by_json.data["region"] == "安徽省",
        "echo 工具成功": by_echo.success and by_echo.data == {"echo": "hello"},
        "返回 mock 数据": by_dict.data["mock"] is True,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_tool_not_found() -> bool:
    """验收 2：未知工具返回 TOOL_NOT_FOUND。"""
    print("\n[2/8] 未知工具返回 TOOL_NOT_FOUND")

    executor = build_executor()
    result = executor.execute("ghost_tool", {"a": 1})

    checks = {
        "success 为 False": result.success is False,
        "error_type 正确": result.error_type == "TOOL_NOT_FOUND",
        "error_message 正确": result.error_message == "指定工具未注册",
        "data 为 None": result.data is None,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_invalid_json() -> bool:
    """验收 3：非法 JSON 返回 INVALID_JSON。"""
    print("\n[3/8] 非法 JSON 返回 INVALID_JSON")

    executor = build_executor()
    cases = {
        "纯文本": "这不是JSON",
        "单引号": "{'region': '江西'}",
        "未闭合": '{"region": ',
        "空字符串": "",
        "数组": [1, 2, 3],
        "整数": 42,
    }
    all_ok = True
    for label, payload in cases.items():
        result = executor.execute("search_policy", payload)
        ok = result.success is False and result.error_type == "INVALID_JSON"
        all_ok = all_ok and ok
        print(f"  [{'x' if ok else ' '}] {label}：{result.error_type}")

    return all_ok


def check_invalid_arguments() -> bool:
    """验收 4：非法参数返回 INVALID_ARGUMENTS。"""
    print("\n[4/8] 非法参数返回 INVALID_ARGUMENTS")

    executor = build_executor()
    cases = {
        "空 region": {"region": "", "keyword": "电价"},
        "纯空格 region": {"region": "   ", "keyword": "电价"},
        "缺 keyword": {"region": "江西省"},
        "多余字段": {"region": "江西省", "keyword": "电价", "extra": 1},
        "类型错误": {"region": 123, "keyword": "电价"},
        "空对象": {},
    }
    all_ok = True
    for label, payload in cases.items():
        result = executor.execute("search_policy", payload)
        ok = result.success is False and result.error_type == "INVALID_ARGUMENTS"
        all_ok = all_ok and ok
        print(f"  [{'x' if ok else ' '}] {label}：{result.error_type}")

    return all_ok


def check_handler_not_called() -> bool:
    """验收 5：参数校验失败时 handler 绝不执行。"""
    print("\n[5/8] 参数校验失败时 handler 绝不执行")

    calls = []
    executor = build_executor(spy_sink=calls)

    invalid_args = executor.execute("echo", {"text": ""})
    calls_after_args = list(calls)

    invalid_json = executor.execute("echo", "坏JSON")
    calls_after_json = list(calls)

    success = executor.execute("echo", {"text": "正常调用"})
    calls_after_success = list(calls)

    checks = {
        "非法参数时未执行": calls_after_args == [],
        "非法 JSON 时未执行": calls_after_json == [],
        "合法参数时执行一次": calls_after_success == ["正常调用"],
        "非法参数返回 INVALID_ARGUMENTS": invalid_args.error_type
        == "INVALID_ARGUMENTS",
        "非法 JSON 返回 INVALID_JSON": invalid_json.error_type == "INVALID_JSON",
        "合法调用成功": success.success is True,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_execution_error() -> bool:
    """验收 6：业务函数异常被捕获并返回 EXECUTION_ERROR。"""
    print("\n[6/8] 业务函数异常能够被捕获并返回 EXECUTION_ERROR")

    registry = ToolRegistry()

    def boom(text: str):
        raise RuntimeError("模拟业务炸了")

    registry.register(
        Tool(
            name="boom",
            description="总是抛错",
            args_schema=EchoInput,
            handler=boom,
        )
    )
    executor = ToolExecutor(registry)
    result = executor.execute("boom", {"text": "x"})

    checks = {
        "success 为 False": result.success is False,
        "error_type 正确": result.error_type == "EXECUTION_ERROR",
        "error_message 正确": result.error_message == "工具执行失败",
        "data 为 None": result.data is None,
        "异常未向外抛出": isinstance(result, ToolResult),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_tool_result_and_latency() -> bool:
    """验收 7：统一返回 ToolResult 并记录耗时。"""
    print("\n[7/8] 统一返回 ToolResult 并记录耗时")

    registry = ToolRegistry()

    def slow(text: str):
        time.sleep(0.05)
        return {"echo": text}

    registry.register(
        Tool(
            name="slow",
            description="慢工具",
            args_schema=EchoInput,
            handler=slow,
        )
    )
    executor = ToolExecutor(registry)

    success = executor.execute("slow", {"text": "x"})
    failure = executor.execute("ghost", {})

    payload = success.to_dict()
    checks = {
        "成功结果是 ToolResult": isinstance(success, ToolResult),
        "失败结果也是 ToolResult": isinstance(failure, ToolResult),
        "tool_name 正确": success.tool_name == "slow",
        "success 字段正确": success.success is True,
        "耗时是浮点数": isinstance(success.latency_ms, float),
        "耗时覆盖 handler 时间（>=50ms）": success.latency_ms >= 50,
        "失败结果也记录耗时": isinstance(failure.latency_ms, float),
        "to_dict 字段齐全": set(payload)
        == {
            "tool_name",
            "success",
            "data",
            "error_type",
            "error_message",
            "latency_ms",
        },
        "to_dict 可 JSON 序列化": bool(json.dumps(payload, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · Tool Executor 验收")
    print("全部离线，不联网、不调用真实模型")
    print("=" * 64)

    checks = [
        ("能够执行合法的字典及 JSON 字符串参数", check_valid_arguments()),
        ("未知工具返回 TOOL_NOT_FOUND", check_tool_not_found()),
        ("非法 JSON 返回 INVALID_JSON", check_invalid_json()),
        ("非法参数返回 INVALID_ARGUMENTS", check_invalid_arguments()),
        ("参数校验失败时 handler 绝不执行", check_handler_not_called()),
        ("业务函数异常能够被捕获并返回 EXECUTION_ERROR", check_execution_error()),
        ("统一返回 ToolResult 并记录耗时", check_tool_result_and_latency()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"行为验收：{passed}/{len(checks)} 通过（第 8 项全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
