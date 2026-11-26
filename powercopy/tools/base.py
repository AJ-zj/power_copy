from dataclasses import dataclass
from typing import Callable, Any

from pydantic import BaseModel


@dataclass
class Tool:
    """统一工具定义。"""

    name: str
    description: str
    args_schema: type[BaseModel]
    handler: Callable[..., Any]

    def to_openai_schema(self) -> dict:
        """转换为模型能够识别的工具描述。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": (
                    self.args_schema.model_json_schema()
                )
            }
        }
