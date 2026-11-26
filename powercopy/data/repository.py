import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

from powercopy.data.models import PolicyDocument


class PolicyRepository:
    """政策文档的数据访问层。

    职责边界：
    - 只负责持久化与读取，不做来源真实性校验；
    - 使用参数化 SQL，输入永远作为参数传入；
    - 只用 INSERT，不用 REPLACE/UPSERT，避免历史快照被覆盖。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS policy_documents (
                    document_id TEXT PRIMARY KEY NOT NULL,
                    source_key TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body_text TEXT NOT NULL,
                    region TEXT,
                    issuer TEXT,
                    published_on TEXT,
                    retrieved_at TEXT NOT NULL
                )
            """)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, document: PolicyDocument) -> UUID:
        """写入一份文档快照，返回 document_id。

        相同 document_id 重复写入会被主键约束拒绝，
        不会覆盖已有快照。
        """

        values = document.model_dump(mode="json")

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO policy_documents (
                    document_id,
                    source_key,
                    source_url,
                    title,
                    body_text,
                    region,
                    issuer,
                    published_on,
                    retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["document_id"],
                    values["source_key"],
                    values["source_url"],
                    values["title"],
                    values["body_text"],
                    values["region"],
                    values["issuer"],
                    values["published_on"],
                    values["retrieved_at"],
                ),
            )

        return document.document_id

    def get(self, document_id: UUID | str) -> PolicyDocument | None:
        """按主键读取文档；不存在时返回 None。"""

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM policy_documents WHERE document_id = ?",
                (str(document_id),),
            ).fetchone()

        if row is None:
            return None

        return PolicyDocument.model_validate(dict(row))

    def find_by_title(self, title: str) -> list[PolicyDocument]:
        """按标题精确查询（参数化，避免 SQL 注入）。"""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM policy_documents WHERE title = ?",
                (title,),
            ).fetchall()

        return [PolicyDocument.model_validate(dict(row)) for row in rows]
