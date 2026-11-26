"""LLM Client 验收脚本。

用法：
    python scripts/check_llm.py            # 完整验收（会发一次真实请求）
    python scripts/check_llm.py --check    # 只检查配置，不联网

验收四项：
    1. 成功读取 .env 配置
    2. 成功调用易拓AI模型
    3. 成功获取模型回复
    4. 成功获取 Token 用量或确认平台未提供

退出码：
    0  四项全部通过
    1  有项目未通过
    2  配置不完整或配置有误（未发送请求）
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

ENV_PATH = ROOT / ".env"
REQUIRED = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT")
TEST_MESSAGES = [
    {"role": "system", "content": "你是PowerCopy售电文案助手。"},
    {"role": "user", "content": "请只回复四个字：连接成功"},
]


def mask(value: str) -> str:
    """隐藏密钥中间部分。"""
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def load_config():
    """读取 .env，返回 (配置字典, 缺失项列表)。"""
    if not ENV_PATH.is_file():
        return {}, ["文件不存在：" + str(ENV_PATH)]

    load_dotenv(ENV_PATH, override=True)
    config = {name: (os.getenv(name) or "").strip() for name in REQUIRED}
    missing = [name for name, value in config.items() if not value]
    return config, missing


def check_offline(config) -> int:
    """只检查配置，不联网。"""
    print("配置检查（未联网）")
    print(f"  .env        : {ENV_PATH}")
    print(f"  LLM_API_KEY : {mask(config['LLM_API_KEY'])}")
    print(f"  LLM_BASE_URL: {config['LLM_BASE_URL']}")
    print(f"  LLM_MODEL   : {config['LLM_MODEL']}")
    print(f"  LLM_TIMEOUT : {config['LLM_TIMEOUT']}")
    try:
        float(config["LLM_TIMEOUT"])
    except ValueError:
        print("\nLLM_TIMEOUT 必须是数字。", file=sys.stderr)
        return 2
    print("\n配置完整，未发送请求。去掉 --check 可执行完整验收。")
    return 0


def run_acceptance() -> int:
    print("=" * 64)
    print("PowerCopy V2 · LLM Client 验收")
    print("=" * 64)

    # ---- 1. 读取配置 ----
    print("\n[1/4] 读取 .env 配置")
    config, missing = load_config()
    if missing:
        print("  缺失或为空：", "、".join(missing), file=sys.stderr)
        print("  结果: FAIL", file=sys.stderr)
        return 2
    try:
        timeout = float(config["LLM_TIMEOUT"])
    except ValueError:
        print("  LLM_TIMEOUT 不是数字", file=sys.stderr)
        print("  结果: FAIL", file=sys.stderr)
        return 2
    print(f"  LLM_API_KEY  = {mask(config['LLM_API_KEY'])}")
    print(f"  LLM_BASE_URL = {config['LLM_BASE_URL']}")
    print(f"  LLM_MODEL    = {config['LLM_MODEL']}")
    print(f"  LLM_TIMEOUT  = {config['LLM_TIMEOUT']}")
    print("  结果: PASS")
    ok_config = True

    # ---- 2. 调用模型 ----
    print("\n[2/4] 调用易拓AI模型")
    from powercopy.core.llm import LLMClient

    try:
        client = LLMClient()
    except ValueError as error:
        print(f"  客户端初始化失败：{error}", file=sys.stderr)
        print("  结果: FAIL", file=sys.stderr)
        return 2

    raw = None
    try:
        raw = client.client.chat.completions.create(
            model=client.model,
            messages=TEST_MESSAGES,
            timeout=timeout,
        )
        print(f"  HTTP 请求成功，请求模型：{client.model}")
        print("  结果: PASS")
        ok_call = True
    except Exception as error:  # 网络、鉴权、限流都可能在这里
        print(f"  调用失败：{type(error).__name__}: {str(error)[:200]}", file=sys.stderr)
        print("  结果: FAIL", file=sys.stderr)
        ok_call = False

    # ---- 3. 模型回复 ----
    print("\n[3/4] 获取模型回复")
    if raw is not None:
        content = (raw.choices[0].message.content or "").strip()
        print(f"  回复内容：{content[:80]!r}")
        print(f"  响应模型：{raw.model}")
        ok_reply = bool(content)
        print("  结果:", "PASS" if ok_reply else "FAIL")
    else:
        ok_reply = False
        print("  无回复")
        print("  结果: FAIL")

    # ---- 4. Token 用量 ----
    print("\n[4/4] Token 用量")
    usage = getattr(raw, "usage", None) if raw is not None else None
    if usage is not None:
        print(f"  输入：{usage.prompt_tokens}")
        print(f"  输出：{usage.completion_tokens}")
        print(f"  合计：{usage.total_tokens}")
        print("  结果: PASS（平台已提供）")
        ok_usage = True
    else:
        print("  平台未返回 usage 字段，按验收标准视为已确认未提供")
        print("  结果: PASS（已确认平台未提供）")
        ok_usage = True

    # ---- 汇总 ----
    checks = [
        ("成功读取 .env 配置", ok_config),
        ("成功调用易拓AI模型", ok_call),
        ("成功获取模型回复", ok_reply),
        ("成功获取 Token 用量", ok_usage),
    ]
    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"验收结果：{passed}/4 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == 4 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PowerCopy V2 LLM Client 验收")
    parser.add_argument("--check", action="store_true", help="只检查配置，不联网")
    args = parser.parse_args(argv)

    config, missing = load_config()
    if missing:
        print("配置不完整：", "、".join(missing), file=sys.stderr)
        print(f"请检查 {ENV_PATH}", file=sys.stderr)
        print("未发送请求。", file=sys.stderr)
        return 2
    if args.check:
        return check_offline(config)
    return run_acceptance()


if __name__ == "__main__":
    sys.exit(main())
