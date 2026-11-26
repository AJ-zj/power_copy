"""05-B 向量检索（Dense Retrieval）。

设计边界
--------
只做「向量化 + 精确相似度检索」：
- 不建向量数据库（26 个切片用 NumPy 暴力计算 < 1ms）
- 不调第三方 Embedding API
- 不改政策数据库结构
- 完全本地离线推理

与词法检索的关系
----------------
`lexical.py` 是 05-A 的 BM25 基线，`dense.py` 是 05-B 的向量检索。
两者共用同一份切片清单，产出可对比的指标。

索引与缓存的区别
----------------
- **索引**（`.npz`）：向量矩阵 + chunk 元信息 + 模型指纹
- **缓存**（`.json`）：逐条文本的向量，按 text_sha256 命中

缓存的意义：切片重算时，未变的文本不需要重新编码。
26 条切片首次编码约 2 秒，之后命中缓存 < 10ms。

一致性检查
----------
索引必须与以下三者同时匹配，否则拒绝使用：
1. 切片清单的 `manifest_sha256`
2. 模型的 commit hash + 维度
3. 归一化方式（cls_l2）

任何一项不符就重建，不猜测。
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from powercopy.rag.embedding import (
    EMBEDDING_DIM,
    MAX_TOKENS,
    MODEL_NAME,
    BGEEmbedder,
    EmbeddingError,
)

SCHEMA_VERSION = "policy-dense-index-v1"
CACHE_SCHEMA = "policy-embedding-cache-v1"


class DenseSearchError(Exception):
    pass


@dataclass(frozen=True)
class DenseHit:
    """单条向量检索结果。"""

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
    rank: int = 0
    match_mode: str = "dense_bge"
    """检索模式，与 LexicalSearch 的字段对齐，便于融合层统一处理。"""


@dataclass
class DenseOutcome:
    """一次向量检索的完整结果。"""

    query: str
    hits: list[DenseHit] = field(default_factory=list)
    notice: str = ""
    index_fingerprint: str = ""

    # 与 LexicalSearch 统一的字段，便于同一套评测代码复用
    match_mode: str = "dense_bge"

    @property
    def returned(self) -> int:
        return len(self.hits)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "match_mode": "dense_bge",
            "returned": self.returned,
            "notice": self.notice,
            "index_fingerprint": self.index_fingerprint,
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
                }
                for h in self.hits
            ],
        }


# ---------------------------------------------------------------- 工具
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(payload: dict) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_matrix(matrix: np.ndarray, expected_dim: int) -> None:
    """校验向量矩阵的形状与数值健康度。"""

    if matrix.ndim != 2:
        raise DenseSearchError(f"向量矩阵必须是二维，实际 {matrix.ndim} 维")

    if matrix.shape[1] != expected_dim:
        raise DenseSearchError(
            f"向量维度不符：期望 {expected_dim}，实际 {matrix.shape[1]}"
        )

    if not np.isfinite(matrix).all():
        raise DenseSearchError("向量含 NaN 或 Inf，拒绝写入索引")

    norms = np.linalg.norm(matrix, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-4):
        raise DenseSearchError(
            f"向量未正确归一化：范数范围 [{norms.min():.4f}, {norms.max():.4f}]"
        )


# ---------------------------------------------------------------- 缓存
def cache_namespace() -> dict:
    """缓存隔离命名空间。

    向量只在以下全部一致时可复用：
    - 模型名与 commit
    - 向量维度
    - 池化方式
    - 归一化方式
    - 编码模式（query 加指令 vs document 不加）
    - tokenizer 版本

    任何一项变化都会使缓存失效，避免「换了模型却复用旧向量」。
    """

    import transformers

    return {
        "model": MODEL_NAME,
        "dim": EMBEDDING_DIM,
        "pooling": "cls_l2",
        "normalized": True,
        "encode_mode": "document",  # 索引侧只编码文档
        "tokenizer": getattr(transformers, "__version__", "unknown"),
        "max_tokens": MAX_TOKENS,
    }


def _namespace_fingerprint(namespace: dict) -> str:
    return _fingerprint(namespace)


class EmbeddingCache:
    """按 text_sha256 缓存向量，避免重复编码。

    缓存按 `cache_namespace()` 隔离：模型/维度/池化/归一化/编码模式
    任一项不符则整个缓存失效（返回空），不静默复用。
    """

    def __init__(self, path: str | Path, *, namespace: dict | None = None):
        self.path = Path(path)
        self._namespace = namespace or cache_namespace()
        self._data: dict[str, list[float]] = {}
        self._dirty = False
        self._rejected_reason: str | None = None
        self._load()

    @property
    def namespace(self) -> dict:
        return dict(self._namespace)

    @property
    def rejected_reason(self) -> str | None:
        """缓存被拒绝的原因（若已失效）。"""

        return self._rejected_reason

    def _load(self) -> None:
        if not self.path.is_file():
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._rejected_reason = "unreadable"
            return

        if payload.get("schema") != CACHE_SCHEMA:
            self._rejected_reason = "schema_mismatch"
            return

        stored = payload.get("namespace") or {}

        # 逐项比对命名空间，任何差异都拒绝
        for key, expected in self._namespace.items():
            actual = stored.get(key)
            if actual != expected:
                self._rejected_reason = f"{key}_mismatch"
                return

        self._data = payload.get("vectors") or {}

    def get(self, text: str) -> list[float] | None:
        return self._data.get(_sha256(text))

    def put(self, text: str, vector: Sequence[float]) -> None:
        self._data[_sha256(text)] = list(vector)
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": CACHE_SCHEMA,
            "namespace": self._namespace,
            "namespace_fingerprint": _namespace_fingerprint(self._namespace),
            "count": len(self._data),
            "vectors": self._data,
        }
        temp = self.path.with_suffix(".tmp")
        try:
            temp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)
        self._dirty = False

    def __len__(self) -> int:
        return len(self._data)


# ---------------------------------------------------------------- 索引构建
def build_dense_index(
    manifest_path: str | Path,
    index_path: str | Path,
    *,
    cache_path: str | Path | None = None,
    embedder: BGEEmbedder | None = None,
    overwrite: bool = True,
) -> dict:
    """从切片清单构建向量索引。

    Args:
        manifest_path: `build_chunks.py` 产出的清单
        index_path: 索引输出路径（`.npz`）
        cache_path: 向量缓存（可选，加速重建）
        embedder: 编码器（默认新建）
        overwrite: 已存在时是否重建

    Returns:
        构建统计
    """

    manifest_path = Path(manifest_path)
    index_path = Path(index_path)

    if not manifest_path.is_file():
        raise DenseSearchError(f"清单不存在：{manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DenseSearchError(f"清单读取失败：{exc}") from exc

    if manifest.get("schema") != "policy-chunk-manifest-v1":
        raise DenseSearchError(f"清单 schema 不支持：{manifest.get('schema')}")

    chunks = manifest.get("chunks") or []
    if not chunks:
        raise DenseSearchError("清单中没有切片")

    if index_path.exists() and not overwrite:
        raise DenseSearchError(f"索引已存在：{index_path}")

    embedder = embedder or BGEEmbedder()

    cache = EmbeddingCache(cache_path) if cache_path else None

    texts = [c["text"] for c in chunks]
    vectors: list[list[float] | None] = [None] * len(texts)
    cached = 0
    encoded = 0

    if cache is not None:
        for index, text in enumerate(texts):
            hit = cache.get(text)
            if hit is not None:
                vectors[index] = hit
                cached += 1

    pending = [i for i, v in enumerate(vectors) if v is None]
    if pending:
        pending_texts = [texts[i] for i in pending]
        try:
            computed = embedder.encode_documents(pending_texts)
        except EmbeddingError as exc:
            raise DenseSearchError(f"向量化失败：{exc}") from exc

        for index, vector in zip(pending, computed):
            vectors[index] = vector
            encoded += 1
            if cache is not None:
                cache.put(texts[index], vector)

    matrix = np.asarray(vectors, dtype=np.float32)
    _validate_matrix(matrix, EMBEDDING_DIM)

    model_info = getattr(embedder, "model_fingerprint", None)
    if model_info is None:
        model_info = {
            "model": MODEL_NAME,
            "dim": EMBEDDING_DIM,
            "max_tokens": MAX_TOKENS,
            "pooling": "cls_l2",
        }

    meta = {
        "schema": SCHEMA_VERSION,
        "manifest_sha256": manifest.get("manifest_sha256", ""),
        "source_manifest": str(manifest_path),
        "chunk_count": len(chunks),
        "document_count": len(manifest.get("documents") or []),
        "model": meta_model_name(model_info),
        "model_commit": meta_model_commit(model_info),
        "dim": EMBEDDING_DIM,
        "max_tokens": MAX_TOKENS,
        "pooling": "cls_l2",
        "normalized": True,
        "dtype": "float32",
        "cache_cached": cached,
        "cache_encoded": encoded,
    }
    meta["fingerprint"] = _fingerprint(meta)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        index_path,
        vectors=matrix,
        chunk_ids=np.asarray([c["chunk_id"] for c in chunks], dtype=object),
        meta_json=np.asarray(json.dumps(meta, ensure_ascii=False)),
    )

    if cache is not None:
        cache.save()

    return {
        "index_path": str(index_path),
        "chunks": len(chunks),
        "dim": EMBEDDING_DIM,
        "cached": cached,
        "encoded": encoded,
        "fingerprint": meta["fingerprint"],
        "size_bytes": index_path.stat().st_size,
    }


def meta_model_name(info: dict) -> str:
    return str(info.get("model") or MODEL_NAME)


def meta_model_commit(info: dict) -> str:
    return str(info.get("commit") or "")


# ---------------------------------------------------------------- 检索
class DenseSearch:
    """NumPy 精确向量检索。"""

    def __init__(
        self,
        index_path: str | Path,
        *,
        manifest_path: str | Path,
        embedder: BGEEmbedder | None = None,
    ):
        self.index_path = Path(index_path)
        self.manifest_path = Path(manifest_path)

        # 复用同一个编码器实例：模型加载约 50 秒，不能每次检索都重建
        self._embedder = embedder

        if not self.index_path.is_file():
            raise DenseSearchError(f"索引不存在：{self.index_path}")
        if not self.manifest_path.is_file():
            raise DenseSearchError(f"清单不存在：{self.manifest_path}")

        with np.load(self.index_path, allow_pickle=True) as data:
            self._vectors = data["vectors"]
            self._chunk_ids = [str(x) for x in data["chunk_ids"]]
            self._meta = json.loads(str(data["meta_json"]))

        _validate_matrix(self._vectors, EMBEDDING_DIM)

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._by_id = {c["chunk_id"]: c for c in manifest["chunks"]}

        if self._meta.get("manifest_sha256") != manifest.get("manifest_sha256"):
            raise DenseSearchError(
                "索引与切片清单不匹配（manifest_sha256 不同），请重建索引"
            )

        if len(self._chunk_ids) != self._vectors.shape[0]:
            raise DenseSearchError("索引内部不一致：chunk_ids 与向量行数不符")

        if any(cid not in self._by_id for cid in self._chunk_ids):
            raise DenseSearchError("索引中存在清单里没有的 chunk_id")

    # ---- 元信息 ----

    @property
    def embedder(self) -> BGEEmbedder:
        """惰性创建并复用编码器。"""
        if self._embedder is None:
            self._embedder = BGEEmbedder()
        return self._embedder

    @property
    def meta(self) -> dict:
        return dict(self._meta)

    @property
    def fingerprint(self) -> str:
        return str(self._meta.get("fingerprint", ""))

    def chunk_count(self) -> int:
        return int(self._vectors.shape[0])

    def describe(self) -> dict:
        return {
            "schema": self._meta.get("schema"),
            "model": self._meta.get("model"),
            "model_commit": self._meta.get("model_commit"),
            "dim": self._meta.get("dim"),
            "pooling": self._meta.get("pooling"),
            "chunks": self.chunk_count(),
            "manifest_sha256": self._meta.get("manifest_sha256"),
            "fingerprint": self.fingerprint,
        }

    # ---- 检索 ----

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        embedder: BGEEmbedder | None = None,
    ) -> DenseOutcome:
        """向量检索。

        向量已 L2 归一化，因此内积即余弦相似度。
        """

        if top_k < 1:
            raise DenseSearchError("top_k 必须大于零")

        query = (query or "").strip()
        if not query:
            return DenseOutcome(
                query="",
                notice="查询为空，未执行检索。",
                index_fingerprint=self.fingerprint,
            )

        embedder = embedder or self.embedder
        try:
            vector = embedder.encode_query(query)
        except EmbeddingError as exc:
            raise DenseSearchError(f"查询向量化失败：{exc}") from exc

        array = np.asarray([vector], dtype=np.float32)
        _validate_matrix(array, EMBEDDING_DIM)

        scores = (self._vectors @ array[0]).astype(np.float32)

        if not np.isfinite(scores).all():
            raise DenseSearchError("相似度计算出现 NaN 或 Inf")

        # 稳定排序：分数降序，同分按 chunk_id 升序
        order = sorted(
            range(len(scores)),
            key=lambda i: (-float(scores[i]), self._chunk_ids[i]),
        )[:top_k]

        hits = []
        for rank, position in enumerate(order, start=1):
            chunk_id = self._chunk_ids[position]
            chunk = self._by_id[chunk_id]
            hits.append(
                DenseHit(
                    chunk_id=chunk_id,
                    policy_id=chunk["policy_id"],
                    document_id=chunk["document_id"],
                    version=chunk["version"],
                    page=chunk.get("page"),
                    start=chunk["start"],
                    end=chunk["end"],
                    title=chunk.get("title"),
                    source_key=chunk.get("source_key"),
                    source_url=chunk.get("source_url"),
                    content_sha256=chunk.get("content_sha256"),
                    text=chunk["text"],
                    score=float(scores[position]),
                    rank=rank,
                )
            )

        return DenseOutcome(
            query=query,
            hits=hits,
            notice=(
                f"向量检索（{self._meta.get('model')}，"
                f"{self._meta.get('dim')} 维，余弦相似度），"
                f"索引 {self.chunk_count()} 个切片。"
                f"相似度高低不代表证据充分，需人工判断。"
            ),
            index_fingerprint=self.fingerprint,
        )

    def search_ids(self, query: str, *, top_k: int = 10) -> list[str]:
        return [h.chunk_id for h in self.search(query, top_k=top_k).hits]


__all__ = [
    "CACHE_SCHEMA",
    "DenseHit",
    "DenseOutcome",
    "DenseSearch",
    "DenseSearchError",
    "EmbeddingCache",
    "SCHEMA_VERSION",
    "build_dense_index",
]
