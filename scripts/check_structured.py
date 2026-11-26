"""Structured Output 异常验收脚本。

用法：
    python scripts/check_structured.py           # 6 项离线验收（不联网、不花钱）
    python scripts/check_structured.py --live    # 额外跑一次真实模型调用

验收六项：
    1. 标准 JSON 正常解析
    2. Markdown JSON 正常解析
    3. 非法 JSON 正确抛出异常
    4. 空内容正确抛出异常
    5. 缺失字段被 Pydantic 拒绝
    6. 错误字段类型被 Pydantic 拒绝

退出码：
    0  六项全部通过
    1  有项目未通过
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from powercopy.core.llm import parse_json_content  # noqa: E402
from powercopy.core.schemas import PolicyAnalysis  # noqa: E402

VALID_DATA = {
    "policy_title": "安徽省工商业峰谷分时电价通知",
    "summary": "明确 100 千伏安及以上工商业用户执行峰谷分时电价。",
    "key_points": ["低谷下浮 61.8%", "夏冬季高峰上浮 84.3%"],
    "relevant": True,
}
VALID_JSON = json.dumps(VALID_DATA, ensure_ascii=False)


def check_plain_json() -> bool:
    print("\n[1/6] 标准 JSON 正常解析")
    try:
        data = parse_json_content(VALID_JSON)
        validated = PolicyAnalysis.model_validate(data)
        ok = validated.policy_title == VALID_DATA["policy_title"]
        print(f"  解析结果类型：{type(data).__name__}")
        print(f"  字段数：{len(data)}")
        print(f"  Pydantic 校验：{type(validated).__name__}")
        print(f"  policy_title：{validated.policy_title}")
        print("  结果:", "PASS" if ok else "FAIL")
        return ok
    except Exception as error:
        print(f"  异常：{type(error).__name__}: {error}")
        print("  结果: FAIL")
        return False


def check_markdown_json() -> bool:
    print("\n[2/6] Markdown JSON 正常解析")
    fenced = f"```json\n{VALID_JSON}\n```"
    try:
        data = parse_json_content(fenced)
        validated = PolicyAnalysis.model_validate(data)
        ok = validated.relevant is True and len(validated.key_points) == 2
        print(f"  输入：```json 围栏包裹")
        print(f"  解析字段数：{len(data)}")
        print(f"  relevant：{validated.relevant}，key_points：{len(validated.key_points)} 条")
        print("  结果:", "PASS" if ok else "FAIL")
        return ok
    except Exception as error:
        print(f"  异常：{type(error).__name__}: {error}")
        print("  结果: FAIL")
        return False


def check_invalid_json() -> bool:
    print("\n[3/6] 非法 JSON 正确抛出异常")
    bad_inputs = [
        ("纯文本", "这不是 JSON"),
        ("模型自言自语", "好的，以下是分析结果：{}\n希望有帮助"),
        ("截断的 JSON", '{"policy_title": "安徽…", "summary":'),
        ("单引号 JSON", "{'policy_title': '安徽', 'relevant': true}"),
    ]
    all_ok = True
    for name, text in bad_inputs:
        try:
            parse_json_content(text)
            print(f"  [ ] {name}：未抛出异常（不符合预期）")
            all_ok = False
        except json.JSONDecodeError:
            print(f"  [x] {name}：JSONDecodeError")
        except Exception as error:
            print(f"  [x] {name}：{type(error).__name__}（非 JSONDecodeError）")
            all_ok = False
    print("  结果:", "PASS" if all_ok else "FAIL")
    return all_ok


def check_empty_content() -> bool:
    print("\n[4/6] 空内容正确抛出异常")
    empty_inputs = [
        ("空字符串", ""),
        ("纯空白", "   \n\t  "),
        ("None", None),
        ("整数", 123),
        ("空列表", []),
    ]
    all_ok = True
    for name, value in empty_inputs:
        try:
            parse_json_content(value)
            print(f"  [ ] {name}：未抛出异常（不符合预期）")
            all_ok = False
        except ValueError as error:
            print(f"  [x] {name}：ValueError({error})")
        except Exception as error:
            print(f"  [x] {name}：{type(error).__name__}（非 ValueError）")
            all_ok = False
    print("  结果:", "PASS" if all_ok else "FAIL")
    return all_ok


def check_missing_field() -> bool:
    print("\n[5/6] 缺失字段被 Pydantic 拒绝")
    cases = [
        ("缺 relevant", {k: v for k, v in VALID_DATA.items() if k != "relevant"}),
        ("缺 key_points", {k: v for k, v in VALID_DATA.items() if k != "key_points"}),
        ("缺 policy_title", {k: v for k, v in VALID_DATA.items() if k != "policy_title"}),
        ("全空对象", {}),
    ]
    all_ok = True
    for name, payload in cases:
        try:
            PolicyAnalysis.model_validate(payload)
            print(f"  [ ] {name}：未拒绝（不符合预期）")
            all_ok = False
        except ValidationError:
            print(f"  [x] {name}：ValidationError")
        except Exception as error:
            print(f"  [x] {name}：{type(error).__name__}（非 ValidationError）")
            all_ok = False
    print("  结果:", "PASS" if all_ok else "FAIL")
    return all_ok


def check_wrong_type() -> bool:
    print("\n[6/6] 错误字段类型被 Pydantic 拒绝")
    cases = [
        ("relevant 传字符串", {**VALID_DATA, "relevant": "是"}),
        ("key_points 传字符串", {**VALID_DATA, "key_points": "低谷下浮61.8%"}),
        ("key_points 传数字列表", {**VALID_DATA, "key_points": [618]}),
        ("policy_title 传数字", {**VALID_DATA, "policy_title": 2026}),
    ]
    all_ok = True
    for name, payload in cases:
        try:
            PolicyAnalysis.model_validate(payload)
            print(f"  [ ] {name}：未拒绝（不符合预期）")
            all_ok = False
        except ValidationError:
            print(f"  [x] {name}：ValidationError")
        except Exception as error:
            print(f"  [x] {name}：{type(error).__name__}（非 ValidationError）")
            all_ok = False
    print("  结果:", "PASS" if all_ok else "FAIL")
    return all_ok


def run_live() -> bool:
    """额外：真实模型调用，确认整条结构化链路正常。"""
    print("\n[附加] 真实模型结构化输出")
    from powercopy.core.llm import LLMClient

    llm = LLMClient()
    messages = [
        {"role": "system", "content": "你是电力政策分析助手。"},
        {
            "role": "user",
            "content": (
                "请分析：安徽明确 100 千伏安及以上工商业用户执行峰谷分时电价，"
                "低谷电价下浮 61.8%，夏冬季高峰上浮 84.3%，春秋季高峰上浮 74%。"
            ),
        },
    ]
    try:
        result = llm.invoke_structured(messages, PolicyAnalysis)
        data = result["data"]
        ok = isinstance(data, PolicyAnalysis) and bool(data.policy_title)
        print(f"  返回类型：{type(data).__name__}")
        print(f"  policy_title：{data.policy_title[:40]}")
        print(f"  key_points：{len(data.key_points)} 条")
        print(f"  tokens：{result['input_tokens']} + {result['output_tokens']}")
        print("  结果:", "PASS" if ok else "FAIL")
        return ok
    except Exception as error:
        print(f"  异常：{type(error).__name__}: {str(error)[:150]}")
        print("  结果: FAIL")
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Structured Output 异常验收")
    parser.add_argument("--live", action="store_true", help="额外跑一次真实模型调用")
    args = parser.parse_args(argv)

    print("=" * 64)
    print("PowerCopy V2 · Structured Output 异常验收")
    print("=" * 64)

    checks = [
        ("标准 JSON 正常解析", check_plain_json()),
        ("Markdown JSON 正常解析", check_markdown_json()),
        ("非法 JSON 正确抛出异常", check_invalid_json()),
        ("空内容正确抛出异常", check_empty_content()),
        ("缺失字段被 Pydantic 拒绝", check_missing_field()),
        ("错误字段类型被 Pydantic 拒绝", check_wrong_type()),
    ]

    if args.live:
        run_live()

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"验收结果：{passed}/6 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == 6 else 1


if __name__ == "__main__":
    sys.exit(main())
