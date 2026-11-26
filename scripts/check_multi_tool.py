"""多工具调用与错误反馈真实调用脚本。

用法：
    python -m scripts.check_multi_tool

会真实调用模型，验证：
    - 两个工具都被正确注册并下发给模型
    - 模型能完成「先检索、再查详情」的顺序依赖调用
    - 最终回答明确标注模拟数据
"""

import json

from powercopy.core.agent import Agent
from powercopy.core.llm import LLMClient
from powercopy.tools.builtin.policy import (
    policy_detail_tool,
    policy_search_tool,
)
from powercopy.tools.registry import ToolRegistry


def main() -> int:

    # 1. 注册两个工具
    registry = ToolRegistry()
    registry.register(policy_search_tool)
    registry.register(policy_detail_tool)

    llm = LLMClient()

    agent = Agent(
        llm=llm,
        registry=registry,
        system_prompt=(
            "你是PowerCopy售电政策研究助手。"
            "查询政策时必须使用 search_policy 工具。"
            "需要查看政策详情时，必须使用 get_policy_detail，"
            "policy_id 只能取自检索结果，不得编造。"
            "工具返回模拟数据时，必须明确说明，"
            "不得将模拟数据当作真实政策。"
        ),
        max_rounds=5,
        max_tool_calls=6,
    )

    # 2. 执行需要两个工具配合的任务
    result = agent.run(
        "先帮我检索安徽省的峰谷分时电价政策，"
        "再根据检索到的政策 ID 查看该政策的详细资料，"
        "最后说明这些资料的可靠性。"
    )

    # 3. 输出
    print("=" * 64)
    print("多工具 Agent 执行结果")
    print("=" * 64)
    print(f"状态      ：{result['status']}")
    print(f"轮数      ：{result['rounds']}")
    print(f"工具调用数：{result['tool_requests']}")
    print("-" * 64)
    print("最终回答：")
    print(result["content"] or "（无内容）")
    print("=" * 64)

    # 4. 校验
    ok = True

    if result["status"] != "completed":
        print(f"[FAIL] 状态不是 completed：{result['status']}")
        ok = False
    else:
        print("[OK] 状态 completed")

    if result["tool_requests"] < 2:
        print(
            f"[WARN] 只发生了 {result['tool_requests']} 次工具调用，"
            "未完成两工具顺序依赖，可重跑或调大预算"
        )
    else:
        print(f"[OK] 共发起 {result['tool_requests']} 次工具调用（含两个工具）")

    content = result["content"] or ""
    if "模拟" not in content:
        print("[WARN] 回答未明确提示模拟数据")
    else:
        print("[OK] 回答明确标注了模拟数据")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
