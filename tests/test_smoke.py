import json
from unittest import TestCase

from pydantic import BaseModel

from powercopy.core.message import Message, Role, ToolCall
from powercopy.tools.base import Tool


class FakeArgs(BaseModel):
    text: str


class MessageTest(TestCase):
    def test_tool_call_round_trip(self):
        message = Message(
            role=Role.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})],
        )
        payload = message.to_dict()
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["tool_calls"][0]["function"]["name"], "echo")


class ToolTest(TestCase):
    def test_schema_is_openai_compatible(self):
        tool = Tool(
            name="echo",
            description="原样返回输入",
            args_schema=FakeArgs,
            handler=lambda **kwargs: kwargs["text"],
        )
        schema = tool.to_openai_schema()
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "echo")
        self.assertEqual(schema["function"]["parameters"]["required"], ["text"])
        json.dumps(schema)

    def test_handler_executes(self):
        tool = Tool(
            name="echo",
            description="原样返回输入",
            args_schema=FakeArgs,
            handler=lambda **kwargs: kwargs["text"],
        )
        self.assertEqual(tool.handler(text="hello"), "hello")
