"""Messages 模块验收脚本。

用法：
    python scripts/check_messages.py            # 完整验收（第 2 项会发真实请求）
    python scripts/check_messages.py --check    # 只做离线检查，不联网

验收四项：
    1. 能够正确添加 system、user、assistant 消息
    2. 第二轮对话能够利用第一轮提供的信息
    3. get_messages() 能返回完整消息历史的副本
    4. clear() 能清除历史并保留初始系统提示词

退出码：
    0  四项全部通过
    1  有项目未通过
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.core.message import Messages, Role  # noqa: E402

INITIAL_PROMPT = "你是售电文案助手，回答尽量简短，不超过 40 字。"
PROFILE = "我叫张建，负责江西省南昌市青山湖区的售电业务。"


def check_add_messages() -> bool:
    print("\n[1/4] 添加 system、user、assistant 消息")
    history = Messages(INITIAL_PROMPT)
    history.add_user("你好")
    history.add_assistant("你好，我是售电文案助手")
    history.add_system("补充规则：回答不超过 40 字")

    roles = [item["role"] for item in history.get_messages()]
    expected = ["system", "user", "assistant", "system"]
    print(f"  实际角色序列：{roles}")
    print(f"  期望角色序列：{expected}")
    ok = roles == expected
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_get_messages_copy() -> bool:
    print("\n[3/4] get_messages() 返回历史副本")
    history = Messages(INITIAL_PROMPT)
    history.add_user("第一问")
    history.add_assistant("第一答")
    history.add_user("第二问")

    snapshot = history.get_messages()
    snapshot.append({"role": "user", "content": "外部追加"})
    snapshot[0]["content"] = "被篡改"

    again = history.get_messages()
    unchanged = len(again) == 4 and again[0]["content"] == INITIAL_PROMPT
    distinct = history.get_messages() is not history.get_messages()
    print(f"  返回条数：{len(again)}（期望 4）")
    print(f"  外部修改后内部历史未受影响：{unchanged}")
    print(f"  每次返回新列表对象：{distinct}")
    ok = unchanged and distinct
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def check_clear() -> bool:
    print("\n[4/4] clear() 清空历史并保留初始系统提示")
    history = Messages(INITIAL_PROMPT)
    history.add_user("第一问")
    history.add_assistant("第一答")
    before = len(history)

    history.clear()
    after = history.get_messages()
    kept_prompt = len(after) == 1 and after[0]["content"] == INITIAL_PROMPT
    print(f"  clear 前条数：{before}")
    print(f"  clear 后条数：{len(after)}（期望 1）")
    print(f"  保留初始系统提示：{kept_prompt}")
    print(f"  保留内容：{after[0]['content'] if after else ''}")
    print("  结果:", "PASS" if kept_prompt else "FAIL")
    return kept_prompt


def check_second_round_offline() -> bool:
    """离线验证：第二轮提问时，第一轮信息仍在上下文里且顺序正确。"""
    history = Messages(INITIAL_PROMPT)
    history.add_user(f"记住：{PROFILE}请只回复：已记住。")
    history.add_assistant("已记住。")
    history.add_user("我刚说我负责哪个省、哪个市、哪个区？")

    messages = history.get_messages()
    has_profile = any("青山湖" in item["content"] for item in messages)
    order_ok = messages[-1]["role"] == "user" and "哪个省" in messages[-1]["content"]
    print(f"  第一轮信息仍在上下文：{has_profile}")
    print(f"  第二轮提问位于历史末尾：{order_ok}")
    return has_profile and order_ok


def check_second_round_live() -> bool:
    """真实请求：让模型回答一个只有记住第一轮信息才能答对的问题。"""
    from powercopy.core.llm import LLMClient

    llm = LLMClient()
    history = Messages(INITIAL_PROMPT)
    history.add_user(f"记住：{PROFILE}请只回复：已记住。")
    first = llm.invoke(history.get_messages(), model="glm-5.3-flash")
    history.add_assistant(first["content"] or "")

    history.add_user("我刚说我负责哪个省、哪个市、哪个区？只回答地名。")
    second = llm.invoke(history.get_messages(), model="glm-5.3-flash")
    history.add_assistant(second["content"] or "")

    answer = second["content"] or ""
    hits = [name for name in ("江西", "南昌", "青山湖") if name in answer]
    print(f"  第一轮模型回复：{(first['content'] or '').strip()[:40]}")
    print(f"  第二轮提问：我刚说我负责哪个省、哪个市、哪个区？")
    print(f"  第二轮模型回复：{answer.strip()[:60]}")
    print(f"  命中第一轮信息：{hits}")
    ok = len(hits) >= 2
    print("  结果:", "PASS" if ok else "FAIL")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PowerCopy V2 Messages 模块验收")
    parser.add_argument("--check", action="store_true", help="只做离线检查，不联网")
    args = parser.parse_args(argv)

    print("=" * 64)
    print("PowerCopy V2 · Messages 模块验收")
    print("=" * 64)

    ok_add = check_add_messages()

    if args.check:
        print("\n[2/4] 第二轮对话利用第一轮信息（离线检查）")
        ok_second = check_second_round_offline()
        print("  结果:", "PASS" if ok_second else "FAIL")
        print("  （未联网；去掉 --check 可做真实多轮验证）")
    else:
        print("\n[2/4] 第二轮对话利用第一轮信息（真实请求）")
        try:
            ok_second = check_second_round_live()
        except Exception as error:
            print(f"  调用失败：{type(error).__name__}: {str(error)[:120]}")
            print("  结果: FAIL")
            ok_second = False

    ok_copy = check_get_messages_copy()
    ok_clear = check_clear()

    checks = [
        ("能够正确添加 system、user、assistant 消息", ok_add),
        ("第二轮对话能够利用第一轮提供的信息", ok_second),
        ("get_messages() 能返回完整消息历史的副本", ok_copy),
        ("clear() 能清除历史并保留初始系统提示词", ok_clear),
    ]
    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"验收结果：{passed}/4 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == 4 else 1


if __name__ == "__main__":
    sys.exit(main())
