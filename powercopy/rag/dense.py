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

SCHEMA_VERSION = "policy-dense-index-v2"
"""索引格式版本。

v1: chunk_ids 用 dtype=object 存储，加载需 allow_pickle=True（有反序列化风险）
v2: chunk_ids 为定长 Unicode 数组，allow_pickle=False 即可加载；
    并新增 model_commit / tokenizer_fingerprint 等模型身份字段

旧 v1 索引**不会**被自动加载，必须用 scripts/build_dense_index.py --rebuild 重建。
"""

CACHE_SCHEMA = "policy-embedding-cache-v2"
"""缓存格式版本。v2 起命名空间绑定真实模型 commit 与 tokenizer 指纹。"""


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


@dataclass
class CacheNamespace:
    """缓存命名空间：向量可复用的前提条件。

    只承载「会改变向量数值」的因素。任何一项不同，
    旧向量都不能与新向量混合使用。
    """

    model: str
    commit: str
    tokenizer_fingerprint: str
    dim: int
    pooling: str
    normalized: bool
    encode_mode: str
    max_tokens: int

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "commit": self.commit,
            "tokenizer_fingerprint": self.tokenizer_fingerprint,
            "dim": self.dim,
            "pooling": self.pooling,
            "normalized": self.normalized,
            "encode_mode": self.encode_mode,
            "max_tokens": self.max_tokens,
        }

    def missing_fields(self) -> list[str]:
        """返回缺失（空）的必需字段。

        commit 与 tokenizer_fingerprint 是判断模型一致性的关键，
        缺失时不能当作「一致」。
        """

        empty = []
        for key in ("model", "commit", "tokenizer_fingerprint"):
            if not getattr(self, key):
                empty.append(key)
        return empty


def cache_namespace(embedder: "BGEEmbedder | None" = None) -> CacheNamespace:
    """构造缓存命名空间，绑定**实际**模型指纹。

    Args:
        embedder: 编码器实例。传入时会读取其真实 commit 与 tokenizer 指纹；
            None 时只能读到模型名与维度，commit/tokenizer 为空，
            此时该命名空间不应被视为可用于验证兼容性。

    注意：旧实现只记录 transformers 库版本号作为 "tokenizer"，
    这不足以判断 tokenizer 是否真的相同（库版本相同但词表可能不同）。
    现在改为记录**实际 tokenizer 的指纹**。
    """

    if embedder is not None:
        fp = embedder.model_fingerprint
        return CacheNamespace(
            model=str(fp.get("model") or ""),
            commit=str(fp.get("commit") or ""),
            tokenizer_fingerprint=str(fp.get("tokenizer_fingerprint") or ""),
            dim=int(fp.get("dim") or 0),
            pooling=str(fp.get("pooling") or ""),
            normalized=bool(fp.get("normalized")),
            encode_mode="document",
            max_tokens=int(fp.get("max_tokens") or 0),
        )

    return CacheNamespace(
        model=MODEL_NAME,
        commit="",
        tokenizer_fingerprint="",
        dim=EMBEDDING_DIM,
        pooling="cls_l2",
        normalized=True,
        encode_mode="document",
        max_tokens=MAX_TOKENS,
    )


def _namespace_fingerprint(namespace: CacheNamespace) -> str:
    return _fingerprint(namespace.as_dict())


class EmbeddingCache:
    """按 text_sha256 缓存向量，避免重复编码。

    缓存按 `CacheNamespace` 隔离：模型 / commit / tokenizer 指纹 /
    维度 / 池化 / 归一化 / 编码模式任一项不符则整个缓存失效（返回空），
    不静默复用旧向量。

    若传入的命名空间本身缺少 commit 或 tokenizer 指纹（即无法确认
    模型身份），缓存**拒绝加载**，避免在身份不明时复用旧向量。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        namespace: CacheNamespace | dict | None = None,
    ):
        self.path = Path(path)
        if namespace is None:
            namespace = cache_namespace()
        elif isinstance(namespace, dict):
            namespace = CacheNamespace(**namespace)
        self._namespace = namespace
        self._data: dict[str, list[float]] = {}
        self._dirty = False
        self._rejected_reason: str | None = None
        self._load()

    @property
    def namespace(self) -> dict:
        return self._namespace.as_dict()

    @property
    def rejected_reason(self) -> str | None:
        """缓存被拒绝的原因（若已失效）。"""

        return self._rejected_reason

    def _load(self) -> None:
        # 命名空间本身无法确认模型身份 → 不复用任何旧向量
        missing = self._namespace.missing_fields()
        if missing:
            self._rejected_reason = "namespace_incomplete:" + ",".join(missing)
            return

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
        expected = self._namespace.as_dict()

        # 逐项比对命名空间，任何差异都拒绝
        for key, want in expected.items():
            actual = stored.get(key)
            if actual != want:
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
            "namespace": self._namespace.as_dict(),
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

    # 先触发模型加载，才能拿到真实 commit 与 tokenizer 指纹
    embedder._ensure_loaded()  # noqa: SLF001 - 同一包内的受控访问
    model_info = embedder.model_fingerprint

    missing = [
        key
        for key in ("model", "commit", "tokenizer_fingerprint")
        if not model_info.get(key)
    ]
    if missing:
        raise DenseSearchError(
            f"无法确认模型身份，拒绝构建索引：缺少 {missing}。"
            f"请检查模型缓存是否完整（storage/models）。"
        )

    namespace = CacheNamespace(
        model=str(model_info["model"]),
        commit=str(model_info["commit"]),
        tokenizer_fingerprint=str(model_info["tokenizer_fingerprint"]),
        dim=int(model_info["dim"]),
        pooling=str(model_info["pooling"]),
        normalized=bool(model_info["normalized"]),
        encode_mode="document",
        max_tokens=int(model_info["max_tokens"]),
    )

    cache = (
        EmbeddingCache(cache_path, namespace=namespace) if cache_path else None
    )

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

    # chunk_id 唯一性：重复 ID 会让融合去重与追溯失效
    chunk_ids = [c["chunk_id"] for c in chunks]
    if len(set(chunk_ids)) != len(chunk_ids):
        duplicates = sorted(
            {cid for cid in chunk_ids if chunk_ids.count(cid) > 1}
        )
        raise DenseSearchError(
            f"清单中存在重复 chunk_id：{duplicates[:5]}"
        )

    meta = {
        "schema": SCHEMA_VERSION,
        "manifest_sha256": manifest.get("manifest_sha256", ""),
        "source_manifest": str(manifest_path),
        "chunk_count": len(chunks),
        "document_count": len(manifest.get("documents") or []),
        "model": meta_model_name(model_info),
        "model_commit": meta_model_commit(model_info),
        "tokenizer_fingerprint": str(
            model_info.get("tokenizer_fingerprint") or ""
        ),
        "dim": EMBEDDING_DIM,
        "max_tokens": MAX_TOKENS,
        "pooling": str(model_info.get("pooling") or "cls_l2"),
        "normalized": bool(model_info.get("normalized")),
        "encode_mode_document": str(
            model_info.get("encode_mode_document") or "document_no_instruction"
        ),
        "query_instruction": str(
            model_info.get("query_instruction") or ""
        ),
        "dtype": "float32",
        "cache_cached": cached,
        "cache_encoded": encoded,
    }
    meta["fingerprint"] = _fingerprint(meta)

    index_path.parent.mkdir(parents=True, exist_ok=True)

    # chunk_ids 存为定长 Unicode 数组（dtype='<U...'），不依赖 pickle。
    # 旧实现用 dtype=object，np.load 必须 allow_pickle=True，存在反序列化风险。
    np.savez_compressed(
        index_path,
        vectors=matrix,
        chunk_ids=np.asarray(chunk_ids, dtype=str),
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
        verify_model: bool = True,
    ):
        """加载向量索引。

        Args:
            index_path: `.npz` 索引路径
            manifest_path: 切片清单路径
            embedder: 查询编码器。传入时会在加载后立即校验模型一致性
            verify_model: 是否校验索引记录的模型与编码器一致

        Raises:
            DenseSearchError: 索引格式不被支持、与清单不匹配、
                或模型身份无法确认/不一致
        """

        self.index_path = Path(index_path)
        self.manifest_path = Path(manifest_path)

        # 复用同一个编码器实例：模型加载约 50 秒，不能每次检索都重建
        self._embedder = embedder

        if not self.index_path.is_file():
            raise DenseSearchError(f"索引不存在：{self.index_path}")
        if not self.manifest_path.is_file():
            raise DenseSearchError(f"清单不存在：{self.manifest_path}")

        # allow_pickle=False：拒绝任何需要反序列化的对象数组。
        # 旧版索引用 dtype=object 存 chunk_ids，必须重建；
        # 这里不自动兼容，避免加载不可信旧格式。
        try:
            with np.load(self.index_path, allow_pickle=False) as data:
                self._vectors = data["vectors"]
                self._chunk_ids = [str(x) for x in data["chunk_ids"]]
                self._meta = json.loads(str(data["meta_json"]))
        except ValueError as exc:
            raise DenseSearchError(
                "索引格式不受支持（可能由旧版本生成）。"
                "出于安全考虑不允许反序列化 pickle 内容，请重建索引："
                "python scripts/build_dense_index.py --rebuild。"
                f"原始错误：{exc}"
            ) from exc
        except KeyError as exc:
            raise DenseSearchError(
                f"索引缺少必需字段：{exc}。请重建索引。"
            ) from exc

        _validate_matrix(self._vectors, EMBEDDING_DIM)

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._by_id = {c["chunk_id"]: c for c in manifest["chunks"]}

        if self._meta.get("manifest_sha256") != manifest.get("manifest_sha256"):
            raise DenseSearchError(
                "索引与切片清单不匹配（manifest_sha256 不同），请重建索引"
            )

        if len(self._chunk_ids) != self._vectors.shape[0]:
            raise DenseSearchError("索引内部不一致：chunk_ids 与向量行数不符")

        # chunk_id 唯一性
        if len(set(self._chunk_ids)) != len(self._chunk_ids):
            duplicates = sorted(
                {cid for cid in self._chunk_ids if self._chunk_ids.count(cid) > 1}
            )
            raise DenseSearchError(
                f"索引中存在重复 chunk_id：{duplicates[:5]}，请重建索引"
            )

        # 与清单的双向完整对应
        missing_in_manifest = [
            cid for cid in self._chunk_ids if cid not in self._by_id
        ]
        if missing_in_manifest:
            raise DenseSearchError(
                f"索引中存在清单里没有的 chunk_id：{missing_in_manifest[:5]}"
            )

        manifest_ids = set(self._by_id)
        index_ids = set(self._chunk_ids)
        not_indexed = manifest_ids - index_ids
        if not_indexed:
            raise DenseSearchError(
                f"清单中有 {len(not_indexed)} 个切片未被索引"
                f"（如 {sorted(not_indexed)[:3]}），请重建索引"
            )

        # 模型一致性：索引记录的 commit / tokenizer 指纹必须可确认
        self._require_model_identity()

        if verify_model and self._embedder is not None:
            self.check_model_consistency(self._embedder)

    def _require_model_identity(self) -> None:
        """索引必须记录可确认的模型身份。

        缺失 commit 或 tokenizer 指纹的旧索引一律拒绝：
        无法证明「构建索引的模型」与「查询编码器」是同一个。
        """

        missing = [
            key
            for key in ("model", "model_commit", "tokenizer_fingerprint")
            if not self._meta.get(key)
        ]
        if missing:
            raise DenseSearchError(
                f"索引缺少模型身份字段 {missing}，无法确认模型一致性。"
                f"请重建索引：python scripts/build_dense_index.py --rebuild"
            )

    def check_model_consistency(self, embedder: BGEEmbedder) -> dict:
        """校验索引构建模型与查询编码器是否一致。

        比对：模型名、commit hash、tokenizer 指纹、维度、池化、归一化。
        **不允许仅凭维度相同就认定兼容。**

        Raises:
            DenseSearchError: 任一关键字段不一致或无法确认
        """

        # 触发加载以拿到真实指纹
        embedder._ensure_loaded()  # noqa: SLF001
        actual = embedder.model_fingerprint

        index_commit = str(self._meta.get("model_commit") or "")
        actual_commit = str(actual.get("commit") or "")

        index_tok = str(self._meta.get("tokenizer_fingerprint") or "")
        actual_tok = str(actual.get("tokenizer_fingerprint") or "")

        problems = []

        if not actual_commit:
            problems.append("查询编码器的模型 commit 无法确认")
        elif index_commit != actual_commit:
            problems.append(
                f"模型 commit 不一致：索引={index_commit[:12]}，"
                f"编码器={actual_commit[:12]}"
            )

        if not actual_tok:
            problems.append("查询编码器的 tokenizer 指纹无法确认")
        elif index_tok != actual_tok:
            problems.append(
                f"tokenizer 指纹不一致：索引={index_tok[:12]}，"
                f"编码器={actual_tok[:12]}"
            )

        index_model = str(self._meta.get("model") or "")
        actual_model = str(actual.get("model") or "")
        if index_model != actual_model:
            problems.append(
                f"模型名不一致：索引={index_model}，编码器={actual_model}"
            )

        index_dim = int(self._meta.get("dim") or 0)
        if index_dim != int(actual.get("dim") or 0):
            problems.append(
                f"向量维度不一致：索引={index_dim}，"
                f"编码器={actual.get('dim')}"
            )

        index_pooling = str(self._meta.get("pooling") or "")
        if index_pooling != str(actual.get("pooling") or ""):
            problems.append(
                f"池化方式不一致：索引={index_pooling}，"
                f"编码器={actual.get('pooling')}"
            )

        if bool(self._meta.get("normalized")) is not bool(
            actual.get("normalized")
        ):
            problems.append("归一化方式不一致")

        if problems:
            raise DenseSearchError(
                "索引与查询编码器不兼容，拒绝计算相似度：" + "；".join(problems)
            )

        return {
            "consistent": True,
            "model": index_model,
            "commit": index_commit,
            "tokenizer_fingerprint": index_tok,
        }

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
            "tokenizer_fingerprint": self._meta.get("tokenizer_fingerprint"),
            "dim": self._meta.get("dim"),
            "pooling": self._meta.get("pooling"),
            "normalized": self._meta.get("normalized"),
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

        # 每次检索前校验编码器与索引模型一致，防止换了模型却用旧向量比较
        self.check_model_consistency(embedder)

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
    "CacheNamespace",
    "DenseHit",
    "DenseOutcome",
    "DenseSearch",
    "DenseSearchError",
    "EmbeddingCache",
    "SCHEMA_VERSION",
    "build_dense_index",
    "cache_namespace",
]
