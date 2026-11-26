"""Task Budget 九项验收脚本。

全部使用 Mock，不联网、不产生真实请求。

用法：
    python scripts/check_budget.py

验收九项：
    1. API 硬限制
    2. 局部与全局预算
    3. 跨轮共享
    4. 累计 Token
    5. Token 停止阈值
    6. 未知用量
    7. 安全退出
    8. 任务隔离
    9. 全量回归（由 pytest 保证）

退出码：
    0  八项行为验收全部通过
    1  有项目未通过
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2 as httpx  # noqa: E402
from openai import APIStatusError  # noqa: E402

from powercopy.core.agent import Agent  # noqa: E402
from powercopy.core.budget import BudgetExceeded, TaskBudget  # noqa: E402
from powercopy.core.llm import LLMClient  # noqa: E402
from powercopy.core.schemas import PolicyAnalysis  # noqa: E402
from powercopy.tools.builtin.policy import policy_search_tool  # noqa: E402
from powercopy.tools.registry import ToolRegistry  # noqa: E402

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")
VALID = json.dumps(
    {"policy_title": "标题", "summary": "摘要", "key_points": ["要点"], "relevant": True},
    ensure_ascii=False,
)


def status_error(code):
    return APIStatusError(
        message="mock", response=httpx.Response(code, request=REQUEST), body=None
    )


def ok_result(tokens=100, content="ok"):
    if tokens is None:
        return {"content": content, "model": "m", "input_tokens": None,
                "output_tokens": None, "total_tokens": None}
    return {"content": content, "model": "m", "input_tokens": tokens // 2,
            "output_tokens": tokens // 2, "total_tokens": tokens}


class Harness:
    def __init__(self):
        self.client = LLMClient()
        self.calls = 0
        self.delays = []
        import time

        self._original_sleep = time.sleep

    def script(self, behaviors, real_sleep=False):
        state = {"i": 0}

        def fake_once(messages, model=None, **kwargs):
            i = state["i"]
            state["i"] += 1
            self.calls += 1
            behavior = behaviors[min(i, len(behaviors) - 1)]
            if isinstance(behavior, Exception):
                raise behavior
            return behavior

        self.client._invoke_once = fake_once

        def fake_sleep(seconds):
            self.delays.append(seconds)
            if real_sleep:
                self._original_sleep(0.01)

        import powercopy.core.llm as llm_module

        llm_module.time.sleep = fake_sleep
        return self

    def restore(self):
        import powercopy.core.llm as llm_module

        llm_module.time.sleep = self._original_sleep


def check_api_hard_limit() -> bool:
    print("\n[1/9] API 硬限制")
    h = Harness().script([ok_result()] * 20)
    try:
        budget = TaskBudget(max_api_attempts=3, max_known_tokens=10**9)
        for _ in range(3):
            h.client.invoke([{"role": "user", "content": "hi"}], budget=budget)
        before = h.calls
        raised = None
        try:
            h.client.invoke([{"role": "user", "content": "hi"}], budget=budget)
        except BudgetExceeded as error:
            raised = error
    finally:
        h.restore()

    checks = {
        "前 3 次请求成功": before == 3,
        "第 4 次抛 BudgetExceeded": raised is not None,
        "异常原因正确": raised is not None and raised.reason == "api_budget_exhausted",
        "未发送第 4 次请求": h.calls == 3,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_local_vs_global() -> bool:
    print("\n[2/9] 局部与全局预算")
    h = Harness().script([status_error(503)] * 10)
    try:
        # 局部 RetryPolicy 允许 3 次，全局只剩 2 次
        budget = TaskBudget(max_api_attempts=2, max_known_tokens=10**9)
        raised = None
        try:
            h.client.invoke([{"role": "user", "content": "hi"}], budget=budget)
        except BudgetExceeded as error:
            raised = error
    finally:
        h.restore()

    checks = {
        "抛出 BudgetExceeded": raised is not None,
        "原因 api_budget_exhausted": raised is not None
        and raised.reason == "api_budget_exhausted",
        "实际只请求 2 次（不是 3 次）": h.calls == 2,
        "预算记录一致": budget.api_attempts == 2,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_cross_round_sharing() -> bool:
    print("\n[3/9] 跨轮共享同一预算")
    registry = ToolRegistry()
    registry.register(policy_search_tool)
    budgets = []

    class FakeLLM:
        def __init__(self):
            self.round = 0

        def invoke(self, messages=None, model=None, *, tools=None, budget=None):
            self.round += 1
            budgets.append(budget)
            budget.reserve_api_attempt()
            budget.record_usage(100)
            if self.round == 1:
                return {
                    "content": None,
                    "tool_calls": [{
                        "id": "c1", "type": "function",
                        "function": {"name": "search_policy",
                                     "arguments": '{"region":"安徽省","keyword":"电价"}'},
                    }],
                    "finish_reason": "tool_calls",
                }
            return {"content": "完成", "tool_calls": [], "finish_reason": "stop"}

    agent = Agent(llm=FakeLLM(), registry=registry, system_prompt="提示",
                  max_api_attempts=5)
    result = agent.run("查询")

    checks = {
        "任务完成": result["status"] == "completed",
        "两轮共用一个实例": len(budgets) == 2 and budgets[0] is budgets[1],
        "累计两次请求": result["budget"]["api_attempts"] == 2,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_token_accumulation() -> bool:
    print("\n[4/9] 累计 Token")
    h = Harness().script([ok_result(tokens=100), ok_result(tokens=200)])
    try:
        budget = TaskBudget(max_api_attempts=10, max_known_tokens=10**9)
        h.client.invoke([{"role": "user", "content": "a"}], budget=budget)
        h.client.invoke([{"role": "user", "content": "b"}], budget=budget)
    finally:
        h.restore()

    # 格式校验失败的 Token 也累计
    h2 = Harness().script([ok_result(tokens=50, content="坏JSON"), ok_result(tokens=70, content=VALID)])
    try:
        budget2 = TaskBudget(max_api_attempts=10, max_known_tokens=10**9)
        h2.client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis, budget=budget2
        )
    finally:
        h2.restore()

    checks = {
        "两轮累计 300": budget.known_total_tokens == 300,
        "格式重试累计 120": budget2.known_total_tokens == 120,
        "格式重试占用 2 次预算": budget2.api_attempts == 2,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_token_threshold() -> bool:
    print("\n[5/9] Token 停止阈值")
    h = Harness().script([ok_result(tokens=600)])
    try:
        budget = TaskBudget(max_api_attempts=10, max_known_tokens=500)
        h.client.invoke([{"role": "user", "content": "a"}], budget=budget)
        raised = None
        try:
            h.client.invoke([{"role": "user", "content": "b"}], budget=budget)
        except BudgetExceeded as error:
            raised = error
    finally:
        h.restore()

    checks = {
        "第一次累计 600": budget.known_total_tokens == 600,
        "第二次被拒绝": raised is not None,
        "原因为 token_budget_exhausted": raised is not None
        and raised.reason == "token_budget_exhausted",
        "未发送第二次请求": h.calls == 1,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_unknown_usage() -> bool:
    print("\n[6/9] 未知用量标记")
    # 网络失败后成功
    h = Harness().script([status_error(503), ok_result(tokens=100)])
    try:
        budget = TaskBudget(max_api_attempts=5, max_known_tokens=10**9)
        h.client.invoke([{"role": "user", "content": "hi"}], budget=budget)
    finally:
        h.restore()

    # usage 缺失
    h2 = Harness().script([ok_result(tokens=None)])
    try:
        budget2 = TaskBudget()
        h2.client.invoke([{"role": "user", "content": "hi"}], budget=budget2)
    finally:
        h2.restore()

    checks = {
        "网络失败标记 incomplete": budget.usage_incomplete is True,
        "成功那次 token 仍累计": budget.known_total_tokens == 100,
        "失败也占用预算": budget.api_attempts == 2,
        "usage 缺失标记 incomplete": budget2.usage_incomplete is True,
        "缺失时累计为 0": budget2.known_total_tokens == 0,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_safe_exit() -> bool:
    print("\n[7/9] 安全退出")
    registry = ToolRegistry()
    registry.register(policy_search_tool)
    executed = []

    class FakeLLM:
        def invoke(self, messages=None, model=None, *, tools=None, budget=None):
            budget.reserve_api_attempt()
            budget.record_usage(100)
            return {
                "content": None,
                "tool_calls": [{
                    "id": f"c{budget.api_attempts}", "type": "function",
                    "function": {"name": "search_policy",
                                 "arguments": '{"region":"安徽省","keyword":"电价"}'},
                }],
                "finish_reason": "tool_calls",
            }

    agent = Agent(llm=FakeLLM(), registry=registry, system_prompt="提示",
                  max_rounds=10, max_tool_calls=10, max_api_attempts=1)
    original = agent.executor.execute

    def spy(*args, **kwargs):
        executed.append(args)
        return original(*args, **kwargs)

    agent.executor.execute = spy
    result = agent.run("查询")

    checks = {
        "状态为 api_budget_exhausted": result["status"] == "api_budget_exhausted",
        "只跑了 1 轮": result["rounds"] == 1,
        "未执行无法反馈的工具": executed == [],
        "预算快照存在": "budget" in result,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_task_isolation() -> bool:
    print("\n[8/9] 任务隔离")
    registry = ToolRegistry()
    registry.register(policy_search_tool)
    budgets = []

    class FakeLLM:
        def invoke(self, messages=None, model=None, *, tools=None, budget=None):
            budgets.append(budget)
            budget.reserve_api_attempt()
            return {"content": "完成", "tool_calls": [], "finish_reason": "stop"}

    agent = Agent(llm=FakeLLM(), registry=registry, system_prompt="提示",
                  max_api_attempts=1)

    first = agent.run("任务一")
    second = agent.run("任务二")

    checks = {
        "两次 run 各建一个预算": len(budgets) == 2 and budgets[0] is not budgets[1],
        "第一个任务成功": first["status"] == "completed",
        "第二个任务也成功（未被第一次拖累）": second["status"] == "completed",
        "各自消耗 1 次": first["budget"]["api_attempts"] == 1
        and second["budget"]["api_attempts"] == 1,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · Task Budget 验收")
    print("全部使用 Mock，不联网、不产生真实请求")
    print("=" * 64)

    checks = [
        ("API 硬限制", check_api_hard_limit()),
        ("局部与全局预算", check_local_vs_global()),
        ("跨轮共享", check_cross_round_sharing()),
        ("累计 Token", check_token_accumulation()),
        ("Token 停止阈值", check_token_threshold()),
        ("未知用量", check_unknown_usage()),
        ("安全退出", check_safe_exit()),
        ("任务隔离", check_task_isolation()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"行为验收：{passed}/{len(checks)} 通过（第 9 项全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
