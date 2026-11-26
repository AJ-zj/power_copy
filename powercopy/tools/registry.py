from powercopy.tools.base import Tool


class ToolRegistry:
    """统一工具注册中心。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        """注册工具。"""
        if not isinstance(tool, Tool):
            raise TypeError("只能注册 Tool 对象")
        if tool.name in self._tools:
            raise ValueError(
                f"工具已存在: {tool.name}"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """根据名称获取工具。"""
        if name not in self._tools:
            raise KeyError(f"工具不存在: {name}")
        return self._tools[name]

    def list_tools(self) -> list[str]:
        """返回已注册的工具名称。"""
        return list(self._tools.keys())

    def to_openai_schemas(self) -> list[dict]:
        """批量生成模型可识别的工具描述。"""
        return [
            tool.to_openai_schema()
            for tool in self._tools.values()
        ]

    # 与课程代码一致的别名
    def get_schemas(self) -> list[dict]:
        """批量生成模型可识别的工具描述（别名）。"""
        return self.to_openai_schemas()

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
