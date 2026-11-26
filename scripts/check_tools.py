"""Tool System 验收脚本。

用「模拟的模型工具调用」验证工具机制，不连接真实政策库、不发送真实请求。

流程：
    1. 工具生成 OpenAI schema
    2. 模拟模型返回 tool_call（JSON 字符串形式的 arguments）
    3. 用 args_schema 校验入参
    4. 执行 handler
    5. 组装成 tool 消息，供下一轮交给模型

用法：
    python scripts/check_tools.py

退出码：
    0  全部通过
    1  有项目未通过
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from powercopy.core.message import Message, Role, ToolCall  # noqa: E402
from powercopy.tools.builtin.policy import policy_search_tool  # noqa: E402

# 模拟模型返回的工具调用（模型侧只会给字符串参数）
SIMULATED_TOOL_CALL = {
    "id": "call_mock_001",
    "type": "function",
    "function": {
        "name": "search_policy",
        "arguments": json.dumps(
            {"region": "江西省", "keyword": "峰谷分时电价"},
            ensure_ascii=False,
        ),
    },
}


def check_schema() -> bool:
    print("\n[1/5] 工具导出 OpenAI schema")
    schema = policy_search_tool.to_openai_schema()

    checks = {
        "type 为 function": schema.get("type") == "function",
        "name 正确": schema["function"]["name"] == "search_policy",
        "description 提示模拟": "模拟" in schema["function"]["description"],
        "parameters 有两个字段": set(schema["function"]["parameters"]["properties"])
        == {"region", "keyword"},
        "两个字段都必填": schema["function"]["parameters"]["required"]
        == ["region", "keyword"],
        "禁止多余字段": schema["function"]["parameters"]["additionalProperties"]
        is False,
        "可 JSON 序列化": bool(json.dumps(schema, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_simulated_tool_call() -> bool:
    print("\n[2/5] 模拟模型返回 tool_call，解析参数")
    call = SIMULATED_TOOL_CALL
    name = call["function"]["name"]
    arguments = json.loads(call["function"]["arguments"])

    ok = (
        name == policy_search_tool.name
        and arguments == {"region": "江西省", "keyword": "峰谷分时电价"}
    )
    print(f"  工具名：{name}")
    print(f"  解析后的参数：{arguments}")
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_argument_validation() -> bool:
    print("\n[3/5] 用 args_schema 校验入参")
    valid = policy_search_tool.args_schema(
        **{"region": "江西省", "keyword": "峰谷分时电价"}
    )
    print(f"  合法入参通过：region={valid.region}, keyword={valid.keyword}")

    rejected = []
    cases = (
        ("空 region", {"region": "", "keyword": "电价"}),
        ("空 keyword", {"region": "江西省", "keyword": ""}),
        ("纯空格 region", {"region": "   ", "keyword": "电价"}),
        ("纯制表换行 region", {"region": "\t\n ", "keyword": "电价"}),
        ("纯空格 keyword", {"region": "江西省", "keyword": "   "}),
        ("多余字段", {"region": "江西省", "keyword": "电价", "extra": 1}),
        ("类型错误", {"region": 123, "keyword": "电价"}),
        ("缺字段", {"region": "江西省"}),
    )
    for label, bad in cases:
        try:
            policy_search_tool.args_schema(**bad)
            print(f"  [ ] {label}：未拒绝")
            return False
        except ValidationError:
            rejected.append(label)
            print(f"  [x] {label}：ValidationError")

    # 首尾空白应被清洗掉，而不是直接拒绝
    trimmed = policy_search_tool.args_schema(
        region="  江西省  ", keyword="  峰谷电价  "
    )
    strip_ok = trimmed.region == "江西省" and trimmed.keyword == "峰谷电价"
    print(f"  [{'x' if strip_ok else ' '}] 首尾空白被清洗：{trimmed.region!r} / {trimmed.keyword!r}")

    ok = len(rejected) == len(cases) and strip_ok
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_handler_execution() -> bool:
    print("\n[4/5] 执行 handler 并检查模拟结果")
    args = policy_search_tool.args_schema(
        region="江西省", keyword="峰谷分时电价"
    )
    result = policy_search_tool.handler(**args.model_dump())

    checks = {
        "返回 mock=True": result.get("mock") is True,
        "回显 region": result.get("region") == "江西省",
        "回显 keyword": result.get("keyword") == "峰谷分时电价",
        "documents 是列表": isinstance(result.get("documents"), list),
        "documents 非空": bool(result.get("documents")),
        "条目含 id": result["documents"][0].get("id") == "mock-001",
        "结果可 JSON 序列化": bool(json.dumps(result, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_message_assembly() -> bool:
    print("\n[5/5] 组装 tool 消息（供下一轮交给模型）")
    args = policy_search_tool.args_schema(
        region="江西省", keyword="峰谷分时电价"
    )
    result = policy_search_tool.handler(**args.model_dump())

    # 模型侧返回的 assistant 消息
    assistant = Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[
            ToolCall(
                id=SIMULATED_TOOL_CALL["id"],
                name=SIMULATED_TOOL_CALL["function"]["name"],
                arguments=json.loads(
                    SIMULATED_TOOL_CALL["function"]["arguments"]
                ),
            )
        ],
    )
    # 工具执行结果消息
    tool_message = Message(
        role=Role.TOOL,
        content=json.dumps(result, ensure_ascii=False),
        name=policy_search_tool.name,
        tool_call_id=SIMULATED_TOOL_CALL["id"],
    )

    payloads = [assistant.to_dict(), tool_message.to_dict()]
    checks = {
        "assistant 带 tool_calls": "tool_calls" in payloads[0],
        "tool_call id 一致": payloads[0]["tool_calls"][0]["id"]
        == payloads[1]["tool_call_id"],
        "tool 消息 role 正确": payloads[1]["role"] == "tool",
        "tool 消息 name 正确": payloads[1]["name"] == "search_policy",
        "tool 消息内容可解析": json.loads(payloads[1]["content"])["mock"] is True,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · Tool System 验收（模拟工具调用）")
    print("不连接真实政策数据库，不发送真实模型请求")
    print("=" * 64)

    checks = [
        ("工具导出 OpenAI schema", check_schema()),
        ("模拟模型返回 tool_call，解析参数", check_simulated_tool_call()),
        ("用 args_schema 校验入参", check_argument_validation()),
        ("执行 handler 并检查模拟结果", check_handler_execution()),
        ("组装 tool 消息", check_message_assembly()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"验收结果：{passed}/{len(checks)} 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
