from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterator


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


def _loads(arguments: Any) -> dict[str, Any]:
    """把模型返回的 arguments 统一成 dict（可能是 JSON 字符串）。"""
    import json

    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


@dataclass
class Message:
    role: Role | str
    content: str = ""
    name: str = ""
    tool_call_id: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        role = self.role.value if isinstance(self.role, Role) else str(self.role)
        payload: dict[str, Any] = {"role": role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [call.to_openai() for call in self.tool_calls]
        return payload


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "Usage":
        payload = payload or {}
        return cls(
            prompt_tokens=int(payload.get("prompt_tokens") or 0),
            completion_tokens=int(payload.get("completion_tokens") or 0),
            total_tokens=int(payload.get("total_tokens") or 0),
        )


class Messages:
    """对话消息管理。

    职责：
        1. 按角色追加消息（system / user / assistant / tool）；
        2. 生成可直接交给 LLMClient.invoke() 的历史副本；
        3. clear() 重置对话，但保留构造时传入的初始系统提示词。

    设计要点：
        - 内部保存 Message 对象，对外只给字典副本，避免调用方改坏历史；
        - 初始系统提示词单独记住一份，clear() 靠它恢复。
    """

    def __init__(self, system_prompt: str = "") -> None:
        self._initial_system_prompt = (system_prompt or "").strip()
        self._messages: list[Message] = []
        if self._initial_system_prompt:
            self._messages.append(
                Message(role=Role.SYSTEM, content=self._initial_system_prompt)
            )

    # ---- 读取 ----

    @property
    def system_prompt(self) -> str:
        """构造时传入的初始系统提示词。"""
        return self._initial_system_prompt

    def get_messages(self) -> list[dict[str, Any]]:
        """返回完整消息历史的副本。

        每次调用都重新生成字典列表，调用方修改返回值不会影响内部历史，
        可以直接传给 LLMClient.invoke()。
        """
        return [message.to_dict() for message in self._messages]

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages)

    def __repr__(self) -> str:
        return f"Messages(count={len(self._messages)}, system={self._initial_system_prompt!r})"

    # ---- 写入 ----

    def add(self, role: Role | str, content: str) -> Message:
        """按角色追加一条消息。"""
        message = Message(role=role, content=content)
        self._messages.append(message)
        return message

    def add_system(self, content: str) -> Message:
        return self.add(Role.SYSTEM, content)

    def add_user(self, content: str) -> Message:
        return self.add(Role.USER, content)

    def add_assistant(self, content: str) -> Message:
        return self.add(Role.ASSISTANT, content)

    def add_assistant_tool_calls(
        self,
        content: str | None,
        tool_calls: list[dict]
    ):
        """保存模型发出的工具调用请求。"""

        message = Message(
            role=Role.ASSISTANT,
            content=content or "",
            tool_calls=[
                ToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=_loads(call["function"]["arguments"]),
                )
                for call in deepcopy(tool_calls)
            ],
        )
        self._messages.append(message)
        return message

    def add_tool(
        self,
        tool_call_id: str = "",
        content: str = "",
        *,
        name: str = ""
    ) -> Message:
        """保存工具执行结果。"""
        message = Message(
            role=Role.TOOL, content=content, name=name, tool_call_id=tool_call_id
        )
        self._messages.append(message)
        return message

    def add_message(self, message: Message) -> Message:
        """直接追加已有的 Message 对象（例如从 Tool 流程回填）。"""
        self._messages.append(message)
        return message

    # ---- 重置 ----

    def clear(self) -> None:
        """清空对话历史，保留初始系统提示词。"""
        self._messages.clear()
        if self._initial_system_prompt:
            self._messages.append(
                Message(role=Role.SYSTEM, content=self._initial_system_prompt)
            )


# 课程代码中的类名是 MessageManager，这里提供别名，
# 两种写法指向同一个实现。
MessageManager = Messages
