from typing import Any, Protocol

from ..models import Document


class Parser(Protocol):
    """解析器契约；输入原始字节或文本，输出统一 Document。"""

    name: str

    def parse(self, raw: bytes | str, *, metadata: dict[str, Any] | None = None) -> Document:
        ...
