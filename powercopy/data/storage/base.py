from typing import Protocol

from ..models import Document


class Storage(Protocol):
    """存储契约；SQLite、Postgres、对象存储都应实现这套最小接口。"""

    def save(self, document: Document) -> None:
        ...

    def get(self, document_id: str) -> Document | None:
        ...

    def list(self, *, limit: int = 20) -> list[Document]:
        ...
