"""Tool Definition 六项验收脚本。

全部离线，不联网、不调用真实模型。

用法：
    python scripts/check_tool_definition.py

验收六项：
    1. 成功创建统一 Tool 类
    2. 正确生成 Function Calling Schema
    3. 工具描述不包含本地 handler 对象
    4. Pydantic 正确校验合法及非法参数
    5. 本地 handler 可以正常执行
    6. 新增测试及原有全量回归通过（回归由 pytest 保证）

退出码：
    0  五项行为验收全部通过
    1  有项目未通过
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from powercopy.tools.base import Tool  # noqa: E402
from powercopy.tools.builtin.policy import (  # noqa: E402
    PolicySearchInput,
    policy_search_tool,
)


def check_tool_created() -> bool:
    """验收 1：能创建统一 Tool 类，字段齐全，且是数据类。"""
    print("\n[1/6] 成功创建统一 Tool 类")

    tool = Tool(
        name="demo_tool",
        description="演示工具",
        args_schema=PolicySearchInput,
        handler=lambda **kw: kw,
    )

    import dataclasses

    checks = {
        "Tool 是数据类": dataclasses.is_dataclass(Tool),
        "name 字段可用": tool.name == "demo_tool",
        "description 字段可用": tool.description == "演示工具",
        "args_schema 字段可用": tool.args_schema is PolicySearchInput,
        "handler 字段可用": callable(tool.handler),
        "内置工具已实例化": isinstance(policy_search_tool, Tool),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_function_calling_schema() -> bool:
    """验收 2：生成的 schema 符合 OpenAI Function Calling 结构。"""
    print("\n[2/6] 正确生成 Function Calling Schema")

    schema = policy_search_tool.to_openai_schema()
    function = schema.get("function", {})
    params = function.get("parameters", {})

    checks = {
        "顶层 type=function": schema.get("type") == "function",
        "function 只含 name/description/parameters": set(function)
        == {"name", "description", "parameters"},
        "name 与工具一致": function.get("name") == "search_policy",
        "description 非空": bool(function.get("description")),
        "parameters 是 object": params.get("type") == "object",
        "参数名正确": set(params.get("properties", {}))
        == {"region", "keyword"},
        "必填列表正确": params.get("required") == ["region", "keyword"],
        "禁止多余字段": params.get("additionalProperties") is False,
        "参数带 description": all(
            "description" in value
            for value in params.get("properties", {}).values()
        ),
        "可 JSON 序列化": bool(json.dumps(schema, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_no_handler_leak() -> bool:
    """验收 3：schema 里不能出现本地 handler 对象或函数名。"""
    print("\n[3/6] 工具描述不包含本地 handler 对象")

    schema = policy_search_tool.to_openai_schema()
    text = json.dumps(schema, ensure_ascii=False, default=str)

    checks = {
        "不含函数内存地址": "0x" not in text,
        "不含 <function": "<function" not in text,
        "不含 handler 字段": "handler" not in text,
        "不含 search_policy(": "search_policy(" not in text,
        "不含 args_schema 字段": "args_schema" not in text,
        "序列化可逆": json.loads(text) == schema,
        "只用字符串/数字/布尔/对象/数组": all(
            isinstance(value, (str, int, float, bool, dict, list, type(None)))
            for value in _flatten(schema)
        ),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def _flatten(obj):
    """递归取出所有值，用于检查是否混入非 JSON 类型。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _flatten(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _flatten(item)
    else:
        yield obj


def check_pydantic_validation() -> bool:
    """验收 4：合法参数通过，非法参数被拒绝。"""
    print("\n[4/6] Pydantic 正确校验合法及非法参数")

    valid = PolicySearchInput(region="江西省", keyword="峰谷电价")
    print(f"  合法参数通过：region={valid.region}, keyword={valid.keyword}")

    illegal = (
        ("空 region", {"region": "", "keyword": "电价"}),
        ("空 keyword", {"region": "江西省", "keyword": ""}),
        ("纯空格 region", {"region": "   ", "keyword": "电价"}),
        ("纯制表符 region", {"region": "\t\n", "keyword": "电价"}),
        ("纯空格 keyword", {"region": "江西省", "keyword": "   "}),
        ("多余字段", {"region": "江西省", "keyword": "电价", "extra": 1}),
        ("类型错误 region", {"region": 123, "keyword": "电价"}),
        ("缺 keyword", {"region": "江西省"}),
        ("缺 region", {"keyword": "电价"}),
    )
    rejected = 0
    for label, payload in illegal:
        try:
            PolicySearchInput(**payload)
            print(f"  [ ] {label}：未拒绝")
        except ValidationError:
            rejected += 1
            print(f"  [x] {label}：ValidationError")

    trimmed = PolicySearchInput(region="  江西省  ", keyword="  峰谷电价  ")
    strip_ok = trimmed.region == "江西省" and trimmed.keyword == "峰谷电价"
    print(f"  [{'x' if strip_ok else ' '}] 首尾空白自动清洗：{trimmed.region!r} / {trimmed.keyword!r}")

    ok = rejected == len(illegal) and strip_ok
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_handler_execution() -> bool:
    """验收 5：本地 handler 能正常执行并返回预期结构。"""
    print("\n[5/6] 本地 handler 可以正常执行")

    result = policy_search_tool.handler(region="安徽省", keyword="分时电价")

    checks = {
        "返回 dict": isinstance(result, dict),
        "mock 标记为 True": result.get("mock") is True,
        "回显 region": result.get("region") == "安徽省",
        "回显 keyword": result.get("keyword") == "分时电价",
        "documents 是列表": isinstance(result.get("documents"), list),
        "documents 非空": bool(result.get("documents")),
        "首条含 id": result["documents"][0].get("id") == "mock-001",
        "结果可 JSON 序列化": bool(json.dumps(result, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")

    # 通过 args_schema 端到端走一遍
    args = policy_search_tool.args_schema(region="江苏省", keyword="绿电")
    via_schema = policy_search_tool.handler(**args.model_dump())
    e2e_ok = via_schema["region"] == "江苏省" and via_schema["mock"] is True
    print(f"  [{'x' if e2e_ok else ' '}] 经 args_schema 端到端执行")

    return all(checks.values()) and e2e_ok


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · Tool Definition 验收")
    print("全部离线，不联网、不调用真实模型")
    print("=" * 64)

    checks = [
        ("成功创建统一 Tool 类", check_tool_created()),
        ("正确生成 Function Calling Schema", check_function_calling_schema()),
        ("工具描述不包含本地 handler 对象", check_no_handler_leak()),
        ("Pydantic 正确校验合法及非法参数", check_pydantic_validation()),
        ("本地 handler 可以正常执行", check_handler_execution()),
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
