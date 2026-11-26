"""Agent Loop 真实调用脚本。

用法：
    python -m scripts.check_agent

会真实调用模型，完成一次自主工具调用任务。
"""

import json

from powercopy.core.agent import Agent
from powercopy.core.llm import LLMClient

from powercopy.tools.registry import ToolRegistry
from powercopy.tools.builtin.policy import policy_search_tool


def main() -> int:

    # 1. 初始化组件
    llm = LLMClient()

    registry = ToolRegistry()
    registry.register(policy_search_tool)

    agent = Agent(
        llm=llm,
        registry=registry,
        system_prompt=(
            "你是PowerCopy售电政策研究助手。"
            "查询政策时必须使用提供的工具。"
            "工具返回的是模拟数据时，必须明确说明，"
            "不得将模拟数据当作真实政策。"
        ),
        max_rounds=4,
        max_tool_calls=4,
    )

    # 2. 执行任务
    result = agent.run(
        "帮我查询安徽省的峰谷分时电价政策，并说明数据来源是否可靠。"
    )

    # 3. 打印结果
    print("=" * 64)
    print("Agent 执行结果")
    print("=" * 64)
    print(f"状态      ：{result['status']}")
    print(f"轮数      ：{result['rounds']}")
    print(f"工具调用数：{result['tool_requests']}")
    print("-" * 64)
    print("最终回答：")
    print(result["content"] or "（无内容）")
    print("=" * 64)

    # 4. 基本校验
    ok = True

    if result["status"] != "completed":
        print(f"[FAIL] 状态不是 completed：{result['status']}")
        ok = False
    else:
        print("[OK] 状态 completed")

    if result["tool_requests"] < 1:
        print("[FAIL] 没有发生工具调用，未能验证自主调用能力")
        ok = False
    else:
        print(f"[OK] 自主发起了 {result['tool_requests']} 次工具调用")

    content = result["content"] or ""
    if "模拟" not in content:
        print("[WARN] 回答未明确提示模拟数据，请检查系统提示效果")
    else:
        print("[OK] 回答明确标注了模拟数据")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
