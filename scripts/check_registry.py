"""Tool Registry 六项验收脚本。

全部离线，不联网、不调用真实模型。

用法：
    python scripts/check_registry.py

验收六项：
    1. 能够注册 Tool 对象
    2. 能够根据名称获取指定工具
    3. 重复注册时拒绝覆盖原工具
    4. 不存在的工具正确抛出异常
    5. 能够管理多个工具并批量生成 Schema
    6. 新增测试及全量回归通过（回归由 pytest 保证）

退出码：
    0  五项行为验收全部通过
    1  有项目未通过
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from powercopy.tools.base import Tool  # noqa: E402
from powercopy.tools.builtin.policy import policy_search_tool  # noqa: E402
from powercopy.tools.registry import ToolRegistry  # noqa: E402


class EchoInput(BaseModel):
    text: str = Field(min_length=1, description="要返回的文本")


def make_echo_tool(name: str = "echo") -> Tool:
    return Tool(
        name=name,
        description="原样返回输入文本",
        args_schema=EchoInput,
        handler=lambda text: {"echo": text},
    )


def check_register() -> bool:
    """验收 1：能注册 Tool 对象，且拒绝非 Tool。"""
    print("\n[1/6] 能够注册 Tool 对象")

    registry = ToolRegistry()
    registry.register(policy_search_tool)
    print(f"  注册后工具列表：{registry.list_tools()}")

    type_error_ok = False
    try:
        registry.register("不是工具")
    except TypeError as error:
        type_error_ok = "只能注册 Tool 对象" in str(error)
        print(f"  [x] 非 Tool 对象被拒绝：{error}")

    checks = {
        "注册成功": registry.list_tools() == ["search_policy"],
        "返回值为 None": registry.register(make_echo_tool()) is None,
        "非 Tool 被拒绝": type_error_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_get() -> bool:
    """验收 2：按名称获取工具，拿到同一个对象。"""
    print("\n[2/6] 能够根据名称获取指定工具")

    registry = ToolRegistry()
    echo = make_echo_tool()
    registry.register(policy_search_tool)
    registry.register(echo)

    got_policy = registry.get("search_policy")
    got_echo = registry.get("echo")

    checks = {
        "取到 policy 工具": got_policy is policy_search_tool,
        "取到 echo 工具": got_echo is echo,
        "类型是 Tool": isinstance(got_policy, Tool),
        "args_schema 正确": got_policy.args_schema.__name__ == "PolicySearchInput",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_duplicate() -> bool:
    """验收 3：同名工具重复注册必须拒绝，且原工具不被覆盖。"""
    print("\n[3/6] 重复注册时拒绝覆盖原工具")

    registry = ToolRegistry()
    registry.register(policy_search_tool)
    original = registry.get("search_policy")

    raised = False
    try:
        registry.register(make_echo_tool(name="search_policy"))
    except ValueError as error:
        raised = True
        print(f"  [x] 重复注册被拒绝：{error}")

    still_original = registry.get("search_policy") is original
    schema_unchanged = (
        registry.get("search_policy").args_schema.__name__ == "PolicySearchInput"
    )

    checks = {
        "重复注册抛出 ValueError": raised,
        "原工具对象未被替换": still_original,
        "原工具参数模型未被替换": schema_unchanged,
        "工具数量仍为 1": len(registry) == 1,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_not_found() -> bool:
    """验收 4：取不存在的工具抛 KeyError。"""
    print("\n[4/6] 不存在的工具正确抛出异常")

    registry = ToolRegistry()
    registry.register(policy_search_tool)

    raised = False
    message_ok = False
    try:
        registry.get("ghost_tool")
    except KeyError as error:
        raised = True
        message_ok = "工具不存在" in str(error) and "ghost_tool" in str(error)
        print(f"  [x] 抛出 KeyError：{error}")

    checks = {
        "抛出 KeyError": raised,
        "错误信息含名称": message_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_multiple_and_schemas() -> bool:
    """验收 5：管理多个工具并批量生成 Schema。"""
    print("\n[5/6] 能够管理多个工具并批量生成 Schema")

    registry = ToolRegistry()
    registry.register(policy_search_tool)
    registry.register(make_echo_tool())

    schemas = registry.to_openai_schemas()
    names = [item["function"]["name"] for item in schemas]

    checks = {
        "工具数量为 2": len(registry) == 2,
        "list_tools 顺序正确": registry.list_tools() == ["search_policy", "echo"],
        "批量 schema 数量为 2": len(schemas) == 2,
        "schema 名称顺序正确": names == ["search_policy", "echo"],
        "每个都是 function 类型": all(
            item["type"] == "function" for item in schemas
        ),
        "每个都有 parameters": all(
            "parameters" in item["function"] for item in schemas
        ),
        "全部可 JSON 序列化": bool(json.dumps(schemas, ensure_ascii=False)),
        "空注册中心返回空列表": ToolRegistry().to_openai_schemas() == [],
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · Tool Registry 验收")
    print("全部离线，不联网、不调用真实模型")
    print("=" * 64)

    checks = [
        ("能够注册 Tool 对象", check_register()),
        ("能够根据名称获取指定工具", check_get()),
        ("重复注册时拒绝覆盖原工具", check_duplicate()),
        ("不存在的工具正确抛出异常", check_not_found()),
        ("能够管理多个工具并批量生成 Schema", check_multiple_and_schemas()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"行为验收：{passed}/{len(checks)} 通过（第 6 项全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
