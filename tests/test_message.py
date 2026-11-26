from powercopy.core.message import Message, Messages, Role, Usage


def test_can_add_system_user_assistant_messages():
    """验收 1：能够正确添加 system、user、assistant 消息。"""
    history = Messages("你是PowerCopy售电文案助手。")

    history.add_user("你好，请介绍一下你自己。")
    history.add_assistant("我是售电文案助手，可以帮你写推广文案。")
    history.add("system", "补充规则：回答不超过 50 字。")

    roles = [item["role"] for item in history.get_messages()]
    assert roles == ["system", "user", "assistant", "system"]

    contents = [item["content"] for item in history.get_messages()]
    assert contents[0] == "你是PowerCopy售电文案助手。"
    assert contents[1] == "你好，请介绍一下你自己。"
    assert contents[2] == "我是售电文案助手，可以帮你写推广文案。"
    assert contents[3] == "补充规则：回答不超过 50 字。"

    # 便捷方法也应可用
    history2 = Messages()
    history2.add_system("系统提示")
    history2.add_user("用户消息")
    history2.add_assistant("助手消息")
    assert [m["role"] for m in history2.get_messages()] == ["system", "user", "assistant"]


def test_second_round_can_use_first_round_information():
    """验收 2：第二轮对话能够利用第一轮提供的信息。"""
    history = Messages("你是售电文案助手，回答简洁。")
    history.add_user("我叫张建，负责江西园区的售电业务。")
    history.add_assistant("好的张建，我记住了您在江西园区负责售电业务。")
    history.add_user("我刚才说我负责哪个省的业务？")

    messages = history.get_messages()

    # 第一轮的用户信息必须仍在历史里，模型才能据此回答
    assert any("张建" in item["content"] for item in messages)
    assert any("江西园区" in item["content"] for item in messages)

    # 历史和顺序：system -> user -> assistant -> user
    assert [item["role"] for item in messages] == [
        "system", "user", "assistant", "user"
    ]

    # 最后一条是第二轮提问
    assert messages[-1]["content"] == "我刚才说我负责哪个省的业务？"

    # 构造给 LLM 的完整上下文里，第一轮信息在第二轮提问之前
    first_round_index = next(
        index for index, item in enumerate(messages) if "江西园区" in item["content"]
    )
    second_round_index = len(messages) - 1
    assert first_round_index < second_round_index


def test_get_messages_returns_copy_of_full_history():
    """验收 3：get_messages() 能返回完整消息历史的副本。"""
    history = Messages("系统提示")
    history.add_user("第一问")
    history.add_assistant("第一答")
    history.add_user("第二问")

    snapshot = history.get_messages()
    assert len(snapshot) == 4

    # 修改返回值不应污染内部历史
    snapshot.append({"role": "user", "content": "外部追加"})
    snapshot[0]["content"] = "被改掉的系统提示"
    snapshot[1]["role"] = "assistant"

    again = history.get_messages()
    assert len(again) == 4
    assert again[0]["content"] == "系统提示"
    assert again[1]["role"] == "user"

    # 两次调用返回的是不同列表对象
    assert history.get_messages() is not history.get_messages()

    # 内容等价
    assert again == [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]


def test_clear_removes_history_but_keeps_initial_system_prompt():
    """验收 4：clear() 能清除历史并保留初始系统提示词。"""
    history = Messages("你是PowerCopy售电文案助手。")
    history.add_user("第一问")
    history.add_assistant("第一答")
    assert len(history) == 3

    history.clear()

    after = history.get_messages()
    assert len(after) == 1
    assert after[0]["role"] == "system"
    assert after[0]["content"] == "你是PowerCopy售电文案助手。"

    # clear 后可以继续对话
    history.add_user("清空后的新问题")
    assert [item["role"] for item in history.get_messages()] == ["system", "user"]
    assert history.get_messages()[-1]["content"] == "清空后的新问题"


def test_clear_without_system_prompt_leaves_empty_history():
    """补充：没有初始系统提示词时，clear() 后应为空。"""
    history = Messages()
    history.add_user("你好")
    history.clear()
    assert history.get_messages() == []
    assert len(history) == 0


def test_message_to_dict_supports_tool_fields():
    """补充：Message.to_dict() 保留工具相关字段，供后续工具调用使用。"""
    message = Message(role=Role.TOOL, content="工具结果", name="echo", tool_call_id="call_1")
    payload = message.to_dict()
    assert payload == {
        "role": "tool",
        "content": "工具结果",
        "name": "echo",
        "tool_call_id": "call_1",
    }
