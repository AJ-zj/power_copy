"""统一执行器 _execute() 验收脚本。

全部使用 Mock，不向真实 API 发送任何失败请求。

用法：
    python scripts/check_execute.py

验收十一项：
    1. 正常文本调用
    2. 正常结构化调用
    3. JSON 错误后成功
    4. Schema 校验失败后成功
    5. 混合异常
    6. 总预算限制
    7. 格式预算限制
    8. Token 累计
    9. 耗时统计
    10. 消息隔离
    11. 全量回归（另行执行 pytest）

退出码：
    0  十项行为验收全部通过
    1  有项目未通过
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2 as httpx  # noqa: E402
from openai import APIStatusError  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from powercopy.core.llm import LLMClient  # noqa: E402
from powercopy.core.schemas import PolicyAnalysis  # noqa: E402

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")

VALID = json.dumps(
    {
        "policy_title": "安徽电价通知",
        "summary": "摘要",
        "key_points": ["要点一", "要点二"],
        "relevant": True,
    },
    ensure_ascii=False,
)
BAD_JSON = "这不是 JSON"
BAD_SCHEMA = json.dumps({"policy_title": "只有标题"}, ensure_ascii=False)


def status_error(code: int, body=None) -> APIStatusError:
    return APIStatusError(
        message="mock",
        response=httpx.Response(code, request=REQUEST),
        body=body,
    )


def ok(content: str = "ok", tokens: int = 15) -> dict:
    return {
        "content": content,
        "model": "glm-5.3-flash",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": tokens,
    }


class Harness:
    """在同一个客户端上脚本化 _invoke_once 的行为。"""

    def __init__(self):
        import os

        os.environ.setdefault("LLM_API_KEY", "sk-mock")
        os.environ.setdefault("LLM_BASE_URL", "https://example.com/v1")
        os.environ.setdefault("LLM_MODEL", "glm-5.3-flash")
        os.environ.setdefault("LLM_TIMEOUT", "60")
        self.client = LLMClient()
        self.client._original_sleep = time.sleep
        self.delays: list[float] = []
        self.calls = 0
        self.snapshots: list[list[dict]] = []

    def mock(self, behaviors, *, real_sleep=False):
        index = {"i": 0, "c": 0}

        def fake_once(messages, model=None):
            i = index["i"]
            index["i"] += 1
            self.calls += 1
            self.snapshots.append([dict(item) for item in messages])
            behavior = behaviors[min(i, len(behaviors) - 1)]
            if isinstance(behavior, Exception):
                raise behavior
            return behavior

        self.client._invoke_once = fake_once

        def fake_sleep(seconds):
            self.delays.append(seconds)
            if real_sleep:
                self.client._original_sleep(0.05)

        time.sleep = fake_sleep
        return self

    def restore(self):
        time.sleep = self.client._original_sleep


def check_normal_text() -> bool:
    print("\n[1/11] 正常文本调用")
    h = Harness().mock([ok("纯文本")])
    try:
        result = h.client.invoke([{"role": "user", "content": "hi"}])
    finally:
        h.restore()

    ok_flag = (
        h.calls == 1
        and result["content"] == "纯文本"
        and result["attempts"] == 1
        and result["format_retries"] == 0
        and "data" not in result
    )
    print(f"  请求次数：{h.calls}（期望 1）")
    print(f"  content：{result['content']}，attempts：{result['attempts']}")
    print(f"  兼容原接口字段：{'content' in result and 'model' in result}")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_normal_structured() -> bool:
    print("\n[2/11] 正常结构化调用")
    h = Harness().mock([ok(VALID)])
    try:
        result = h.client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )
    finally:
        h.restore()

    data = result.get("data")
    ok_flag = (
        h.calls == 1
        and isinstance(data, PolicyAnalysis)
        and data.policy_title == "安徽电价通知"
    )
    print(f"  请求次数：{h.calls}（期望 1）")
    print(f"  data 类型：{type(data).__name__}")
    print(f"  policy_title：{getattr(data, 'policy_title', None)}")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_json_error_then_success() -> bool:
    print("\n[3/11] JSON 错误后成功")
    h = Harness().mock([ok(BAD_JSON), ok(VALID)])
    try:
        result = h.client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )
    finally:
        h.restore()

    ok_flag = (
        h.calls == 2
        and result["format_retries"] == 1
        and isinstance(result["data"], PolicyAnalysis)
    )
    print(f"  请求次数：{h.calls}（期望 2）")
    print(f"  format_retries：{result['format_retries']}（期望 1）")
    print(f"  最终 data：{type(result['data']).__name__}")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_schema_fail_then_success() -> bool:
    print("\n[4/11] Schema 校验失败后成功")
    h = Harness().mock([ok(BAD_SCHEMA), ok(VALID)])
    try:
        result = h.client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )
    finally:
        h.restore()

    ok_flag = (
        h.calls == 2
        and result["format_retries"] == 1
        and isinstance(result["data"], PolicyAnalysis)
        and result["data"].relevant is True
    )
    print(f"  请求次数：{h.calls}（期望 2）")
    print(f"  format_retries：{result['format_retries']}")
    print(f"  返回合法结构化数据：{isinstance(result['data'], PolicyAnalysis)}")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_mixed_errors() -> bool:
    print("\n[5/11] 混合异常（503 → 非法 JSON → 合法 JSON）")
    h = Harness().mock([status_error(503), ok(BAD_JSON), ok(VALID)])
    try:
        result = h.client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )
    finally:
        h.restore()

    ok_flag = (
        h.calls == 3
        and result["attempts"] == 3
        and result["format_retries"] == 1
        and isinstance(result["data"], PolicyAnalysis)
    )
    print(f"  请求次数：{h.calls}（期望 3，最多三次）")
    print(f"  attempts：{result['attempts']}，format_retries：{result['format_retries']}")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_total_budget() -> bool:
    print("\n[6/11] 总预算限制（连续失败不允许第 4 次）")
    h = Harness().mock([status_error(503)] * 10)
    raised = False
    try:
        try:
            h.client.invoke([{"role": "user", "content": "hi"}])
        except APIStatusError:
            raised = True
    finally:
        h.restore()

    ok_flag = raised and h.calls == 3
    print(f"  是否抛出异常：{raised}")
    print(f"  请求次数：{h.calls}（期望 3，不允许第 4 次）")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_format_budget() -> bool:
    print("\n[7/11] 格式预算限制（连续两次非法 JSON 直接抛出）")
    h = Harness().mock([ok(BAD_JSON), ok(BAD_JSON)])
    raised = None
    try:
        try:
            h.client.invoke_structured(
                [{"role": "user", "content": "分析"}], PolicyAnalysis
            )
        except Exception as error:
            raised = error
    finally:
        h.restore()

    ok_flag = raised is not None and h.calls == 2
    print(f"  抛出异常：{type(raised).__name__ if raised else '无'}")
    print(f"  请求次数：{h.calls}（期望 2，不无限重试）")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_token_accumulation() -> bool:
    print("\n[8/11] Token 累计")
    # 第一次 503（无 usage），第二次成功 40 token
    h = Harness().mock([status_error(503), ok(VALID, tokens=40)])
    try:
        result = h.client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )
    finally:
        h.restore()

    ok_flag = result["known_total_tokens"] == 40 and result["total_tokens"] == 40

    # 再验证 usage 缺失时被标记
    h2 = Harness().mock([ok("ok", tokens=None)])
    try:
        result2 = h2.client.invoke([{"role": "user", "content": "hi"}])
    finally:
        h2.restore()
    incomplete_ok = (
        result2["usage_incomplete"] is True
        and result2["total_tokens"] is None
        and result2["known_total_tokens"] == 0
    )

    ok_flag = ok_flag and incomplete_ok
    print(f"  已知 Token 累计：{result['known_total_tokens']}（期望 40）")
    print(f"  总 Token：{result['total_tokens']}")
    print(f"  usage 缺失时标记 incomplete：{incomplete_ok}")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_latency() -> bool:
    print("\n[9/11] 耗时统计（包含失败请求与退避等待）")
    h = Harness().mock(
        [status_error(503), status_error(503), ok(VALID)], real_sleep=True
    )
    try:
        result = h.client.invoke_structured(
            [{"role": "user", "content": "分析"}], PolicyAnalysis
        )
    finally:
        h.restore()

    # 两次退避各真实等待 0.05s，整体耗时必须覆盖这段
    ok_flag = (
        result["total_latency_ms"] >= 100
        and result["total_latency_ms"] >= result["latency_ms"]
    )
    print(f"  退避次数：{len(h.delays)}")
    print(f"  整体耗时：{result['total_latency_ms']} ms（应 >= 100）")
    print(f"  最后一次请求耗时：{result['latency_ms']} ms")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def check_message_isolation() -> bool:
    print("\n[10/11] 消息隔离（重试指令不修改调用者原列表）")
    original = [{"role": "user", "content": "分析"}]
    snapshot = [dict(item) for item in original]

    h = Harness().mock([ok(BAD_JSON), ok(VALID)])
    try:
        h.client.invoke_structured(original, PolicyAnalysis)
    finally:
        h.restore()

    unchanged = original == snapshot and len(original) == 1
    first_len = len(h.snapshots[0])
    second_len = len(h.snapshots[1])
    grew_internally = second_len == first_len + 1
    has_repair = "合法 JSON" in h.snapshots[1][-1]["content"]

    ok_flag = unchanged and grew_internally and has_repair
    print(f"  调用者列表未被修改：{unchanged}")
    print(f"  内部第二轮消息数：{first_len} -> {second_len}（+1 修复指令）")
    print(f"  修复指令内容存在：{has_repair}")
    print("  结果:", "PASS" if ok_flag else "FAIL")
    return ok_flag


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · 统一执行器 _execute() 验收")
    print("全部使用 Mock，不发送真实失败请求")
    print("=" * 64)

    checks = [
        ("正常文本调用", check_normal_text()),
        ("正常结构化调用", check_normal_structured()),
        ("JSON 错误后成功", check_json_error_then_success()),
        ("Schema 校验失败后成功", check_schema_fail_then_success()),
        ("混合异常", check_mixed_errors()),
        ("总预算限制", check_total_budget()),
        ("格式预算限制", check_format_budget()),
        ("Token 累计", check_token_accumulation()),
        ("耗时统计", check_latency()),
        ("消息隔离", check_message_isolation()),
    ]

    passed = sum(1 for _, ok_flag in checks if ok_flag)
    print("\n" + "=" * 64)
    print(f"行为验收：{passed}/10 通过（第 11 项为全量回归，见 pytest）")
    for name, ok_flag in checks:
        print(f"  [{'x' if ok_flag else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
