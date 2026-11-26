"""词法检索（BM25）基线。

05-A：为阶段05 建立可量化的词法检索基线。

为什么需要基线
--------------
目标架构包含「自适应检索路由」和「对照评测」。没有基线数字，
就无法判断向量检索或图检索是否真的更好。

实现选择
--------
用 SQLite 内置 FTS5 的 `trigram` tokenizer + `bm25()` 排序。
零新增依赖（标准库 `sqlite3` 自带）。

trigram 的关键限制（实测）
--------------------------
`trigram` 对中文按 3 字符滑窗切分，因此：

    查询「电」    (1 字) → 0 条
    查询「电价」  (2 字) → 0 条
    查询「电力市」(3 字) → 命中
    查询「电力市场」(4 字) → 命中

**少于 3 个字符的查询无法命中**。这是 tokenizer 的硬约束，不是 bug。

项目负责人要求必须设计回退机制，因此：

- 查询长度 >= 3 → FTS5 MATCH（BM25 排序）
- 查询长度 < 3  → 回退到 `instr()` 全表扫描（字面子串）

回退对当前规模（26 切片）完全够用，扫描耗时 < 1ms。

查询安全
--------
用户输入一律包成「双引号短语」并转义内部双引号：

    \"电力市场\"        → 短语匹配
    \"AND\"             → 字面匹配 AND，不当布尔运算符
    \"\\\"\"             → 字面双引号

这样既避免 FTS5 语法错误，也避免查询语法注入。

评分归一化
----------
FTS5 的 `bm25()` 返回负值，**越小越相关**。为便于与未来向量分数融合，
统一转成「越大越相关」的 0-1 分：

    fts:      score = 1 / (1 + |bm25|)
    fallback: score = 命中次数 / (命中次数 + 1)

两者都是单调映射，保持原始排序不变。
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# trigram tokenizer 的最小匹配长度
TRIGRAM_MIN_CHARS = 3

SCHEMA_VERSION = "policy-lexical-index-v1"

DDL = (
    # 主表：存切片全文与溯源字段（fallback 扫描用）
    '''CREATE TABLE IF NOT EXISTS chunks (
        chunk_id TEXT PRIMARY KEY NOT NULL,
        policy_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        page INTEGER,
        start INTEGER NOT NULL,
        end INTEGER NOT NULL,
        title TEXT,
        source_key TEXT,
        source_url TEXT,
        content_sha256 TEXT,
        text TEXT NOT NULL
    )''',
    # FTS5 索引表：只索引 text，chunk_id 作为 unindexed 键
    '''CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        chunk_id UNINDEXED,
        text,
        tokenize='trigram'
    )''',
    '''CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY NOT NULL,
        value TEXT NOT NULL
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_chunks_policy
       ON chunks(policy_id, version)''',
)


class LexicalSearchError(Exception):
    pass


@dataclass(frozen=True)
class SearchHit:
    """单条检索结果。"""

    chunk_id: str
    policy_id: str
    document_id: str
    version: int
    page: int | None
    start: int
    end: int
    title: str | None
    source_key: str | None
    source_url: str | None
    content_sha256: str | None
    text: str
    score: float
    match_mode: str
    bm25: float | None = None
    """FTS5 原始 bm25 分值（负值，越小越相关）。回退模式为 None。"""

    rank: int = 0
    """在本次检索结果中的排名，从 1 开始。"""


@dataclass
class SearchOutcome:
    """一次检索的完整结果。"""

    query: str
    match_mode: str
    hits: list[SearchHit] = field(default_factory=list)
    notice: str = ""

    @property
    def returned(self) -> int:
        return len(self.hits)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "match_mode": self.match_mode,
            "returned": self.returned,
            "notice": self.notice,
            "hits": [
                {
                    "rank": h.rank,
                    "chunk_id": h.chunk_id,
                    "policy_id": h.policy_id,
                    "document_id": h.document_id,
                    "version": h.version,
                    "page": h.page,
                    "start": h.start,
                    "end": h.end,
                    "title": h.title,
                    "source_key": h.source_key,
                    "source_url": h.source_url,
                    "content_sha256": h.content_sha256,
                    "text": h.text,
                    "score": round(h.score, 6),
                    "bm25": round(h.bm25, 8) if h.bm25 is not None else None,
                    "match_mode": h.match_mode,
                }
                for h in self.hits
            ],
        }


def escape_fts_query(query: str) -> str:
    """把用户输入转成安全的 FTS5 短语查询。

    包成双引号短语，内部双引号加倍转义。这样：
    - 特殊字符（AND / OR / * / - / ( / ) / NEAR）都按字面处理
    - 不会触发 FTS5 语法错误
    - 不会形成查询注入
    """

    return '"' + query.replace('"', '""') + '"'


def _score_from_bm25(bm25: float) -> float:
    """把 FTS5 bm25 的负值转成 0-1、越大越相关。

    FTS5 的约定：`bm25()` 返回负值，**越小（越负）越相关**。
    SQL 里应当 `ORDER BY bm25(...) ASC`。

    旧实现用了 `1 / (1 + abs(bm25))`，会把越相关的项算成越小的分数，
    **反转相关性顺序**。这是一个真实缺陷，05-A 修复时更正。

    正确映射（单调递增，保持原始排序）：

        relevance = -bm25            # 转成正值，越大越相关
        score     = relevance / (1 + relevance)

    性质：
        - bm25 越负 → relevance 越大 → score 越接近 1
        - bm25 = 0   → score = 0
        - 严格单调，不改变排序
    """

    relevance = -bm25
    if relevance <= 0:
        return 0.0
    return relevance / (1.0 + relevance)


def _score_from_count(count: int) -> float:
    """回退模式：按命中次数归一化。"""

    if count <= 0:
        return 0.0
    return count / (count + 1.0)


def build_index(
    manifest_path: str | Path,
    db_path: str | Path,
    *,
    overwrite: bool = True,
) -> dict:
    """从切片清单构建 FTS5 索引。

    Args:
        manifest_path: `build_chunks.py` 产出的清单 JSON
        db_path: 索引数据库路径（独立于业务库）
        overwrite: 已存在时是否重建

    Returns:
        构建统计信息
    """

    manifest_path = Path(manifest_path)
    db_path = Path(db_path)

    if not manifest_path.is_file():
        raise LexicalSearchError(f"清单不存在：{manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LexicalSearchError(f"清单读取失败：{exc}") from exc

    if manifest.get("schema") != "policy-chunk-manifest-v1":
        raise LexicalSearchError(
            f"清单 schema 不支持：{manifest.get('schema')}"
        )

    chunks = manifest.get("chunks") or []
    if not chunks:
        raise LexicalSearchError("清单中没有切片")

    if db_path.exists():
        if not overwrite:
            raise LexicalSearchError(f"索引已存在：{db_path}")
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(db_path)) as conn, conn:
        for statement in DDL:
            conn.execute(statement)

        rows = []
        fts_rows = []
        for chunk in chunks:
            rows.append(
                (
                    chunk["chunk_id"],
                    chunk["policy_id"],
                    chunk["document_id"],
                    chunk["version"],
                    chunk.get("page"),
                    chunk["start"],
                    chunk["end"],
                    chunk.get("title"),
                    chunk.get("source_key"),
                    chunk.get("source_url"),
                    chunk.get("content_sha256"),
                    chunk["text"],
                )
            )
            fts_rows.append((chunk["chunk_id"], chunk["text"]))

        conn.executemany(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.executemany(
            "INSERT INTO chunks_fts(chunk_id, text) VALUES (?,?)", fts_rows
        )

        meta = {
            "schema": SCHEMA_VERSION,
            "source_manifest": str(manifest_path),
            "manifest_sha256": manifest.get("manifest_sha256", ""),
            "chunk_count": str(len(chunks)),
            "document_count": str(len(manifest.get("documents") or [])),
            "tokenizer": "trigram",
            "note": (
                "trigram 需 >= 3 字符；更短查询回退到 instr 扫描"
            ),
        }
        conn.executemany(
            "INSERT INTO meta(key, value) VALUES (?,?)", list(meta.items())
        )

    return {
        "db_path": str(db_path),
        "chunks": len(chunks),
        "documents": len(manifest.get("documents") or []),
        "manifest_sha256": manifest.get("manifest_sha256", ""),
    }


class LexicalSearch:
    """BM25 词法检索，带短查询回退。"""

    def __init__(self, db_path: str | Path):
        # as_uri() 需要绝对路径，这里统一 resolve
        self.db_path = Path(db_path).resolve()
        if not self.db_path.is_file():
            raise LexicalSearchError(f"索引不存在：{self.db_path}")

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path.as_uri() + "?mode=ro", uri=True, timeout=10
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def describe(self) -> dict:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # ---- 检索 ----

    def search(self, query: str, *, top_k: int = 10) -> SearchOutcome:
        """检索切片。

        短查询（< 3 字符）自动回退到字面扫描。
        """

        if top_k < 1:
            raise LexicalSearchError("top_k 必须大于零")

        query = (query or "").strip()
        if not query:
            return SearchOutcome(
                query="",
                match_mode="empty_query",
                notice="查询为空，未执行检索。",
            )

        with closing(self._connect()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

            if len(query) >= TRIGRAM_MIN_CHARS:
                hits = self._search_fts(conn, query, top_k)
                mode = "fts5_trigram_phrase"
                notice = (
                    f"字面短语检索（FTS5 trigram + BM25 打分），"
                    f"索引 {total} 个切片。"
                    f"这是 trigram 字面匹配，不代表所有 BM25 方案的能力。"
                    f"未命中不等于政策不存在。"
                )
            else:
                hits = self._search_fallback(conn, query, top_k)
                mode = "fallback_instr_scan"
                notice = (
                    f"查询仅 {len(query)} 个字符，"
                    f"低于 trigram 的 {TRIGRAM_MIN_CHARS} 字符下限，"
                    f"已回退到 instr 字面扫描。"
                    f"未命中不等于政策不存在。"
                )

        return SearchOutcome(
            query=query, match_mode=mode, hits=hits, notice=notice
        )

    def _search_fts(
        self, conn: sqlite3.Connection, query: str, top_k: int
    ) -> list[SearchHit]:
        # FTS5 的 bm25() 返回负值，越小（越负）越相关 → 必须 ASC。
        # 再加 chunk_id 作为二级排序键，保证同分时结果稳定可复现。
        sql = """
            SELECT c.*, bm25(chunks_fts) AS bm25_score
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ?
            ORDER BY bm25_score ASC, c.chunk_id ASC
            LIMIT ?
        """
        try:
            rows = conn.execute(
                sql, (escape_fts_query(query), top_k)
            ).fetchall()
        except sqlite3.Error as exc:
            raise LexicalSearchError(f"FTS 检索失败：{exc}") from exc

        return [
            self._row_to_hit(
                row,
                _score_from_bm25(row["bm25_score"]),
                "fts5_bm25",
                bm25=row["bm25_score"],
                rank=index,
            )
            for index, row in enumerate(rows, start=1)
        ]

    def _search_fallback(
        self, conn: sqlite3.Connection, query: str, top_k: int
    ) -> list[SearchHit]:
        """短查询回退：字面子串扫描。"""

        rows = conn.execute(
            "SELECT * FROM chunks WHERE instr(text, ?) > 0", (query,)
        ).fetchall()

        if not rows:
            return []

        # 按命中次数排序；次数相同时按 start 升序，保证稳定
        scored = []
        for row in rows:
            count = row["text"].count(query)
            scored.append((count, row))

        scored.sort(key=lambda item: (-item[0], item[1]["start"]))

        return [
            self._row_to_hit(
                row,
                _score_from_count(count),
                "fallback_scan",
                bm25=None,
                rank=index,
            )
            for index, (count, row) in enumerate(scored[:top_k], start=1)
        ]

    @staticmethod
    def _row_to_hit(
        row,
        score: float,
        mode: str,
        *,
        bm25: float | None = None,
        rank: int = 0,
    ) -> SearchHit:
        return SearchHit(
            chunk_id=row["chunk_id"],
            policy_id=row["policy_id"],
            document_id=row["document_id"],
            version=row["version"],
            page=row["page"],
            start=row["start"],
            end=row["end"],
            title=row["title"],
            source_key=row["source_key"],
            source_url=row["source_url"],
            content_sha256=row["content_sha256"],
            text=row["text"],
            score=score,
            match_mode=mode,
            bm25=bm25,
            rank=rank,
        )

    # ---- 评测辅助 ----

    def search_ids(self, query: str, *, top_k: int = 10) -> list[str]:
        """只返回 chunk_id 列表，供评测使用。"""

        return [hit.chunk_id for hit in self.search(query, top_k=top_k).hits]

    def chunk_count(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def get_chunk(self, chunk_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------- 评测指标
def recall_at_k(
    relevant: set[str], retrieved: Sequence[str], k: int
) -> float:
    """Recall@K：前 K 条中命中的相关项占全部相关项的比例。"""

    if not relevant:
        raise LexicalSearchError("relevant 不能为空")
    top = list(retrieved[:k])
    return len(relevant & set(top)) / len(relevant)


def reciprocal_rank(
    relevant: set[str], retrieved: Sequence[str]
) -> float:
    """RR：第一个相关结果排名的倒数。无命中返回 0。"""

    for index, chunk_id in enumerate(retrieved, start=1):
        if chunk_id in relevant:
            return 1.0 / index
    return 0.0


def mean_reciprocal_rank(
    cases: Sequence[tuple[set[str], Sequence[str]]]
) -> float:
    """MRR：多条查询 RR 的均值。"""

    if not cases:
        raise LexicalSearchError("cases 不能为空")
    return sum(reciprocal_rank(rel, got) for rel, got in cases) / len(cases)


def ndcg_at_k(
    relevant: set[str], retrieved: Sequence[str], k: int
) -> float:
    """nDCG@K：二值相关性下的归一化折损累计增益。

    理想排序下全部相关项都在最前面，因此 IDCG 可以解析计算。
    """

    if not relevant:
        raise LexicalSearchError("relevant 不能为空")

    dcg = 0.0
    for index, chunk_id in enumerate(list(retrieved[:k]), start=1):
        if chunk_id in relevant:
            dcg += 1.0 / math.log2(index + 1)

    ideal_hits = min(len(relevant), k)
    idcg = sum(
        1.0 / math.log2(index + 1)
        for index in range(1, ideal_hits + 1)
    )

    if idcg == 0:
        return 0.0
    return dcg / idcg


__all__ = [
    "LexicalSearch",
    "LexicalSearchError",
    "SCHEMA_VERSION",
    "SearchHit",
    "SearchOutcome",
    "TRIGRAM_MIN_CHARS",
    "build_index",
    "escape_fts_query",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
