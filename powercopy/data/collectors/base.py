from typing import Protocol

from ..models import Document


class Collector(Protocol):
    """采集器契约；实现可以是官网爬取、API 拉取或本地文件扫描。"""

    name: str

    async def collect(self) -> list[Document]:
        ...
