import json

from powercopy.core.llm import LLMClient
from powercopy.core.message import MessageManager

from powercopy.tools.registry import ToolRegistry
from powercopy.tools.executor import ToolExecutor

from powercopy.tools.builtin.policy import (
    policy_search_tool
)


# 1. 初始化核心组件
llm = LLMClient()

registry = ToolRegistry()
registry.register(policy_search_tool)

executor = ToolExecutor(registry)

manager = MessageManager(
    system_prompt=(
        "你是PowerCopy售电政策研究助手。"
        "查询政策时必须使用提供的工具。"
        "工具返回的是模拟数据时，必须明确说明，"
        "不得将模拟数据当作真实政策。"
    )
)


# 2. 添加用户任务
manager.add_user(
    "帮我查询安徽省的峰谷分时电价政策。"
)

tools = registry.get_schemas()


# 3. 第一次调用 LLM
result = llm.invoke(
    messages=manager.get_messages(),
    tools=tools
)

print("第一次模型回复：", result["content"])

tool_calls = result["tool_calls"]

print("工具调用请求：", tool_calls)


# 4. 检查模型是否发出了有效工具请求
if not tool_calls:
    raise RuntimeError("模型未发起工具调用")

if result["finish_reason"] in {
    "length", "content_filter"
}:
    raise RuntimeError("模型输出不完整，停止执行")

call_ids = [call["id"] for call in tool_calls]

if (
    not all(call_ids)
    or len(set(call_ids)) != len(call_ids)
):
    raise RuntimeError("工具调用 ID 无效或重复")

if any(call["type"] != "function" for call in tool_calls):
    raise RuntimeError("不支持的工具调用类型")


# 5. 保存模型的工具调用消息
manager.add_assistant_tool_calls(
    content=result["content"],
    tool_calls=tool_calls
)


# 6. 执行模型请求的每一个工具
for call in tool_calls:

    name = call["function"]["name"]

    arguments = call["function"]["arguments"]

    tool_result = executor.execute(
        name=name,
        arguments=arguments
    )

    print("工具执行结果：", tool_result.to_dict())

    # 将执行结果转换成 JSON 字符串
    content = json.dumps(
        tool_result.to_dict(),
        ensure_ascii=False,
        allow_nan=False
    )

    # 将工具结果加入消息历史
    manager.add_tool(
        tool_call_id=call["id"],
        content=content
    )


# 7. 第二次调用 LLM
final_result = llm.invoke(
    messages=manager.get_messages(),
    tools=tools
)

# 本节只执行一轮工具调用，不继续循环
if final_result["tool_calls"]:
    raise RuntimeError(
        "模型要求继续调用工具，留待 Agent Loop 阶段处理"
    )

if (
    final_result["finish_reason"] in {
        "length", "content_filter"
    }
    or not final_result["content"]
):
    raise RuntimeError("未获得完整的最终文本")


# 8. 保存最终回答
manager.add_assistant(
    final_result["content"]
)

print("最终回答：", final_result["content"])


# 9. 查看完整消息历史
print(
    json.dumps(
        manager.get_messages(),
        ensure_ascii=False,
        indent=2
    )
)
