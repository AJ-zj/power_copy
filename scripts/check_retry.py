"""Retry 接入 LLM Client 的验收脚本。

全部使用 Mock 模拟异常，不向真实 API 发送任何失败请求。

用法：
    python scripts/check_retry.py

验收七项：
    1. 正常请求只调用一次 API
    2. 前两次失败、第三次成功，正确返回结果
    3. HTTP 401 直接抛出异常，不重试
    4. HTTP 429 或 503 能触发有限次重试
    5. 连续三次失败后停止，不再发起第四次请求
    6. 指数退避与随机抖动符合配置范围
    7. 正确记录调用次数、Token、耗时与异常类型

退出码：
    0  七项全部通过
    1  有项目未通过
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2 as httpx  # noqa: E402
from openai import APIStatusError  # noqa: E402

from powercopy.core.llm import LLMClient  # noqa: E402
from powercopy.core.retry import RetryPolicy  # noqa: E402

REQUEST = httpx.Request("POST", "https://example.com/v1/chat/completions")


def status_error(code: int, body=None) -> APIStatusError:
    """构造一个假的 HTTP 状态异常，不发真实请求。"""
    return APIStatusError(
        message="mock error",
        response=httpx.Response(code, request=REQUEST),
        body=body,
    )


def fake_success(content: str = "ok") -> dict:
    return {
        "content": content,
        "model": "glm-5.3-flash",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


class FakeClock:
    """记录 sleep 的时长，但不真的等待。"""

    def __init__(self):
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_client():
    """构造一个不读 .env、不联网的客户端。

    直接把配置写死在实例上，避免依赖本机 .env 内容。
    """
    import os

    os.environ.setdefault("LLM_API_KEY", "sk-mock")
    os.environ.setdefault("LLM_BASE_URL", "https://example.com/v1")
    os.environ.setdefault("LLM_MODEL", "glm-5.3-flash")
    os.environ.setdefault("LLM_TIMEOUT", "60")
    return LLMClient()


def run_with_mocks(client, scripted, clock, monkeypatch_sleep=None):
    """用脚本化的行为替换 _invoke_once。

    scripted: 列表，元素是 "ok" 或 HTTP 状态码，按顺序消费。
    返回 (结果或异常, 实际调用次数)。
    """
    calls = {"count": 0}

    def fake_once(messages, model=None):
        index = calls["count"]
        calls["count"] += 1
        behavior = scripted[index] if index < len(scripted) else scripted[-1]
        if behavior == "ok":
            return fake_success("第{}次成功".format(calls["count"]))
        raise status_error(behavior)

    client._invoke_once = fake_once
    return calls


def check_single_call() -> bool:
    """验收 1：正常请求只调用一次 API。"""
    print("\n[1/7] 正常请求只调用一次 API")

    client = make_client()
    clock = FakeClock()
    original_sleep = sys.modules["time"].sleep
    sys.modules["time"].sleep = clock.sleep
    try:
        calls = run_with_mocks(client, ["ok"], clock)
        result = client.invoke([{"role": "user", "content": "hi"}])
    finally:
        sys.modules["time"].sleep = original_sleep

    ok = calls["count"] == 1 and result["attempts"] == 1 and clock.delays == []
    print(f"  API 调用次数：{calls['count']}（期望 1）")
    print(f"  attempts：{result['attempts']}，retry_count：{result['retry_count']}")
    print(f"  是否发生等待：{bool(clock.delays)}（期望 False）")
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_two_failures_then_success() -> bool:
    """验收 2：前两次失败、第三次成功。"""
    print("\n[2/7] 前两次失败、第三次成功")

    client = make_client()
    clock = FakeClock()
    original_sleep = sys.modules["time"].sleep
    sys.modules["time"].sleep = clock.sleep
    try:
        calls = run_with_mocks(client, [503, 503, "ok"], clock)
        result = client.invoke([{"role": "user", "content": "hi"}])
    finally:
        sys.modules["time"].sleep = original_sleep

    ok = (
        calls["count"] == 3
        and result["attempts"] == 3
        and result["retry_count"] == 2
        and len(clock.delays) == 2
    )
    print(f"  API 调用次数：{calls['count']}（期望 3）")
    print(f"  返回内容：{result['content']}")
    print(f"  attempts：{result['attempts']}，retry_count：{result['retry_count']}")
    print(f"  退避次数：{len(clock.delays)}（期望 2）")
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_401_no_retry() -> bool:
    """验收 3：HTTP 401 直接抛出，不重试。"""
    print("\n[3/7] HTTP 401 直接抛出异常，不重试")

    client = make_client()
    clock = FakeClock()
    original_sleep = sys.modules["time"].sleep
    sys.modules["time"].sleep = clock.sleep
    raised = None
    try:
        calls = run_with_mocks(client, [401], clock)
        try:
            client.invoke([{"role": "user", "content": "hi"}])
        except APIStatusError as error:
            raised = error
    finally:
        sys.modules["time"].sleep = original_sleep

    ok = (
        raised is not None
        and calls["count"] == 1
        and clock.delays == []
    )
    print(f"  是否抛出异常：{raised is not None}")
    print(f"  API 调用次数：{calls['count']}（期望 1，不重试）")
    print(f"  是否等待：{bool(clock.delays)}（期望 False）")
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_retryable_codes() -> bool:
    """验收 4：HTTP 429 / 503 能触发有限次重试。"""
    print("\n[4/7] HTTP 429 或 503 能触发有限次重试")

    results = {}
    for code in (429, 503):
        client = make_client()
        clock = FakeClock()
        original_sleep = sys.modules["time"].sleep
        sys.modules["time"].sleep = clock.sleep
        try:
            calls = run_with_mocks(client, [code, "ok"], clock)
            result = client.invoke([{"role": "user", "content": "hi"}])
        finally:
            sys.modules["time"].sleep = original_sleep

        ok = calls["count"] == 2 and result["attempts"] == 2
        results[code] = ok
        print(f"  HTTP {code}：调用 {calls['count']} 次，attempts={result['attempts']} -> {'PASS' if ok else 'FAIL'}")

    ok = all(results.values())
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_max_attempts() -> bool:
    """验收 5：连续三次失败后停止，不发起第四次。"""
    print("\n[5/7] 连续三次失败后停止，不再发起第四次请求")

    client = make_client()
    clock = FakeClock()
    original_sleep = sys.modules["time"].sleep
    sys.modules["time"].sleep = clock.sleep
    raised = None
    try:
        calls = run_with_mocks(client, [503, 503, 503, 503, 503], clock)
        try:
            client.invoke([{"role": "user", "content": "hi"}])
        except APIStatusError as error:
            raised = error
    finally:
        sys.modules["time"].sleep = original_sleep

    ok = (
        raised is not None
        and calls["count"] == 3
        and len(clock.delays) == 2
    )
    print(f"  是否抛出异常：{raised is not None}")
    print(f"  API 调用次数：{calls['count']}（期望 3，不能有第 4 次）")
    print(f"  退避次数：{len(clock.delays)}（期望 2）")
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_backoff() -> bool:
    """验收 6：指数退避与随机抖动符合配置范围。"""
    print("\n[6/7] 指数退避与随机抖动符合配置范围")

    client = make_client()
    clock = FakeClock()
    original_sleep = sys.modules["time"].sleep
    sys.modules["time"].sleep = clock.sleep
    try:
        run_with_mocks(client, [503, 503, 503], clock)
        try:
            client.invoke([{"role": "user", "content": "hi"}])
        except APIStatusError:
            pass
    finally:
        sys.modules["time"].sleep = original_sleep

    policy = client.retry_policy
    first_ok = policy.base_delay <= clock.delays[0] <= policy.base_delay * 1.1 + 1e-9
    second_ok = (
        policy.base_delay * 2 <= clock.delays[1] <= policy.base_delay * 2 * 1.1 + 1e-9
    )
    capped = all(delay <= policy.max_delay for delay in clock.delays)

    print(f"  配置：base={policy.base_delay}s，max={policy.max_delay}s")
    print(f"  第 1 次退避：{clock.delays[0]:.3f}s（期望 1.0~1.1）")
    print(f"  第 2 次退避：{clock.delays[1]:.3f}s（期望 2.0~2.2）")
    print(f"  均不超过 max_delay：{capped}")

    # 再验证抖动确实随机，且封顶生效
    samples = {policy.get_delay(1) for _ in range(10)}
    jitter_random = len(samples) > 1
    capped_long = policy.get_delay(20) <= policy.max_delay
    print(f"  抖动随机性（10 次取值不重复）：{jitter_random}")
    print(f"  长期退避封顶在 {policy.max_delay}s：{policy.get_delay(20):.2f}s")

    ok = first_ok and second_ok and capped and jitter_random and capped_long
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_logging() -> bool:
    """验收 7：记录调用次数、Token、耗时与异常类型。"""
    print("\n[7/7] 正确记录调用次数、Token、耗时与异常类型")

    client = make_client()
    clock = FakeClock()
    original_sleep = sys.modules["time"].sleep
    sys.modules["time"].sleep = clock.sleep

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    logger = logging.getLogger("powercopy.core.llm")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        run_with_mocks(client, [503, "ok"], clock)
        result = client.invoke([{"role": "user", "content": "hi"}])
    finally:
        logger.removeHandler(handler)
        sys.modules["time"].sleep = original_sleep

    text = "\n".join(records)
    has_fail = any("LLM failed" in line and "error_type=APIStatusError" in line for line in records)
    has_success = any(
        "LLM success" in line and "total_tokens=15" in line and "latency_ms=" in line
        for line in records
    )
    has_wait = any("LLM retry waiting" in line for line in records)
    has_attempt = any("attempt=2" in line for line in records)
    result_ok = (
        result["attempts"] == 2
        and result["retry_count"] == 1
        and isinstance(result["latency_ms"], float)
        and result["total_tokens"] == 15
    )

    ok = has_fail and has_success and has_wait and has_attempt and result_ok
    print(f"  失败日志（含异常类型）：{has_fail}")
    print(f"  成功日志（含 Token 与耗时）：{has_success}")
    print(f"  重试等待日志：{has_wait}")
    print(f"  attempt 计数：{has_attempt}")
    print(f"  返回字段 attempts/retry_count/latency_ms/total_tokens：{result_ok}")
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · Retry 接入 LLM Client 验收")
    print("全部使用 Mock 模拟异常，不发送真实失败请求")
    print("=" * 64)

    checks = [
        ("正常请求只调用一次 API", check_single_call()),
        ("前两次失败、第三次成功，正确返回结果", check_two_failures_then_success()),
        ("HTTP 401 直接抛出异常，不重试", check_401_no_retry()),
        ("HTTP 429 或 503 能触发有限次重试", check_retryable_codes()),
        ("连续三次失败后停止，不再发起第四次请求", check_max_attempts()),
        ("指数退避与随机抖动符合配置范围", check_backoff()),
        ("正确记录调用次数、Token、耗时与异常类型", check_logging()),
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
