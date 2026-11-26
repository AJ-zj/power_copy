from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ToolResult:
    """统一工具执行结果。"""

    tool_name: str
    success: bool

    data: Any = None

    error_type: str | None = None
    error_message: str | None = None

    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)
