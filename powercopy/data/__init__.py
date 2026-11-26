"""数据中心：采集、解析、清洗、存储四层可替换管线。

两个模型职责不同：
- ``Document``：管线内部传递用的轻量 dataclass（采集器/解析器契约）。
- ``PolicyDocument``：持久化用的严格数据契约（Pydantic V2，不可变快照）。
"""

from .models import Document, PolicyDocument

__all__ = ["Document", "PolicyDocument"]
