"""05-B 向量检索测试。

覆盖：
- 向量索引构建与元信息
- 512 维与数值有限性校验
- 归一化校验
- Token 长度校验（禁止静默截断）
- NumPy 精确相似度检索
- Top-K 排序与排名
- 向量缓存一致性
- 索引与清单指纹校验
- 原文证据元信息
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from powercopy.rag.dense import (
    CACHE_SCHEMA,
    SCHEMA_VERSION,
    CacheNamespace,
    DenseSearch,
    DenseSearchError,
    EmbeddingCache,
    build_dense_index,
)
from powercopy.rag.embedding import (
    EMBEDDING_DIM,
    MAX_TOKENS,
    QUERY_INSTRUCTION,
    BGEEmbedder,
    EmbeddingError,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "storage" / "rag" / "chunks.json"


def _model_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _model_available(), reason="未安装 torch/transformers"
)


def _chunk(cid, text, policy="p1", doc="d1", version=1, start=0):
    return {
        "chunk_id": cid,
        "policy_id": policy,
        "document_id": doc,
        "version": version,
        "page": None,
        "start": start,
        "end": start + len(text),
        "title": "测试政策",
        "source_key": "demo",
        "source_url": "https://example.invalid/a",
        "content_sha256": "0" * 64,
        "text": text,
    }


def _write_manifest(tmp_path, chunks, manifest_sha="test-hash"):
    path = tmp_path / "chunks.json"
    path.write_text(
        json.dumps(
            {
                "schema": "policy-chunk-manifest-v1",
                "documents": [{"policy_id": "p1", "document_id": "d1"}],
                "chunks": chunks,
                "manifest_sha256": manifest_sha,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(scope="module")
def embedder():
    return BGEEmbedder()


@pytest.fixture(scope="module")
def small_index(tmp_path_factory, embedder):
    folder = tmp_path_factory.mktemp("dense")
    chunks = [
        _chunk("c1", "第一条 交易周期为15分钟。", start=0),
        _chunk("c2", "第二条 电价为3元/千瓦时。", start=100),
        _chunk("c3", "经营主体应当在电力交易机构办理市场注册。", start=200),
        _chunk("c4", "电力市场运行基本规则", start=300),
        _chunk("c5", "经营主体存在严重失信行为，纳入失信管理。", start=400),
    ]
    manifest = _write_manifest(folder, chunks)
    index = folder / "dense.npz"
    build_dense_index(manifest, index, embedder=embedder)
    return DenseSearch(index, manifest_path=manifest, embedder=embedder), chunks


# ==========================================================================
# 1. 索引构建
# ==========================================================================
class TestBuildIndex:
    def test_creates_npz(self, tmp_path, embedder):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "dense.npz"

        stats = build_dense_index(manifest, index, embedder=embedder)

        assert index.is_file()
        assert stats["chunks"] == 1
        assert stats["dim"] == EMBEDDING_DIM == 512
        assert stats["size_bytes"] > 0

    def test_meta_recorded(self, small_index):
        search, _ = small_index
        meta = search.meta

        assert meta["schema"] == SCHEMA_VERSION
        assert meta["dim"] == 512
        assert meta["pooling"] == "cls_l2"
        assert meta["normalized"] is True
        assert meta["max_tokens"] == MAX_TOKENS
        assert meta["model"]

    def test_fingerprint_stable(self, tmp_path, embedder):
        """同输入重复构建，指纹应一致。"""
        chunks = [_chunk("c1", "电力市场")]
        manifest = _write_manifest(tmp_path, chunks)

        i1 = tmp_path / "a.npz"
        i2 = tmp_path / "b.npz"
        s1 = build_dense_index(manifest, i1, embedder=embedder)
        s2 = build_dense_index(manifest, i2, embedder=embedder)

        assert s1["fingerprint"] == s2["fingerprint"]

    def test_missing_manifest(self, tmp_path):
        with pytest.raises(DenseSearchError, match="清单不存在"):
            build_dense_index(tmp_path / "no.json", tmp_path / "i.npz")

    def test_wrong_schema(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"schema": "other", "chunks": [{}]}))
        with pytest.raises(DenseSearchError, match="schema 不支持"):
            build_dense_index(path, tmp_path / "i.npz")

    def test_empty_chunks(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text(
            json.dumps({"schema": "policy-chunk-manifest-v1", "chunks": []})
        )
        with pytest.raises(DenseSearchError, match="没有切片"):
            build_dense_index(path, tmp_path / "i.npz")

    def test_no_overwrite(self, tmp_path, embedder):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "i.npz"
        build_dense_index(manifest, index, embedder=embedder)

        with pytest.raises(DenseSearchError, match="索引已存在"):
            build_dense_index(
                manifest, index, embedder=embedder, overwrite=False
            )


# ==========================================================================
# 2. 向量数值校验
# ==========================================================================
class TestVectorValidation:
    def test_dimension(self, small_index):
        search, _ = small_index
        assert search._vectors.shape == (5, 512)

    def test_normalized(self, small_index):
        search, _ = small_index
        norms = np.linalg.norm(search._vectors, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-4)

    def test_finite(self, small_index):
        search, _ = small_index
        assert np.isfinite(search._vectors).all()

    def test_rejects_wrong_dim(self):
        from powercopy.rag.dense import _validate_matrix

        bad = np.random.rand(5, 256).astype(np.float32)
        with pytest.raises(DenseSearchError, match="向量维度不符"):
            _validate_matrix(bad, 512)

    def test_rejects_nan(self):
        from powercopy.rag.dense import _validate_matrix

        bad = np.full((3, 512), np.nan, dtype=np.float32)
        with pytest.raises(DenseSearchError, match="NaN 或 Inf"):
            _validate_matrix(bad, 512)

    def test_rejects_unnormalized(self):
        from powercopy.rag.dense import _validate_matrix

        bad = np.full((3, 512), 0.5, dtype=np.float32)
        with pytest.raises(DenseSearchError, match="未正确归一化"):
            _validate_matrix(bad, 512)


# ==========================================================================
# 3. Token 长度校验（禁止静默截断）
# ==========================================================================
class TestTokenLength:
    def test_token_lengths(self, embedder):
        lengths = embedder.token_lengths(["电力市场", "电力市场运行基本规则"])
        assert len(lengths) == 2
        assert all(isinstance(n, int) and n > 0 for n in lengths)
        assert lengths[1] > lengths[0]

    def test_check_lengths_reports_over(self, embedder):
        long_text = "电力市场" * 600  # 明显超过 512 token
        over = embedder.check_lengths([long_text])
        assert len(over) == 1
        assert over[0]["index"] == 0
        assert over[0]["tokens"] > MAX_TOKENS
        assert over[0]["limit"] == MAX_TOKENS

    def test_check_lengths_ok(self, embedder):
        assert embedder.check_lengths(["电力市场"]) == []

    def test_encode_rejects_too_long_by_default(self, embedder):
        """默认禁止静默截断。"""
        long_text = "电力市场" * 600
        with pytest.raises(EmbeddingError, match="拒绝静默截断"):
            embedder.encode_documents([long_text])

    def test_encode_allows_explicit_truncation(self, embedder):
        """显式关闭 strict_length 才允许截断。"""
        long_text = "电力市场" * 600
        vectors = embedder.encode_documents.__self__.encode(
            [long_text], as_query=False, strict_length=False
        )
        assert len(vectors) == 1
        assert len(vectors[0]) == 512

    def test_real_chunks_within_limit(self, embedder):
        """真实切片不应超过模型上限。"""
        if not MANIFEST.is_file():
            pytest.skip("缺少切片清单")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        texts = [c["text"] for c in manifest["chunks"]]
        assert embedder.check_lengths(texts) == []


# ==========================================================================
# 4. 检索
# ==========================================================================
class TestDenseSearch:
    def test_search_returns_hits(self, small_index):
        search, _ = small_index
        out = search.search("电价", top_k=3)
        assert out.returned == 3
        assert all(h.score > 0 for h in out.hits)

    def test_scores_descending(self, small_index):
        search, _ = small_index
        out = search.search("电力市场注册", top_k=5)
        scores = [h.score for h in out.hits]
        assert scores == sorted(scores, reverse=True)

    def test_rank_sequence(self, small_index):
        search, _ = small_index
        out = search.search("电力市场", top_k=5)
        assert [h.rank for h in out.hits] == list(range(1, out.returned + 1))

    def test_top_k_respected(self, small_index):
        search, _ = small_index
        assert search.search("电力市场", top_k=2).returned == 2

    def test_scores_in_range(self, small_index):
        """归一化向量的余弦相似度应在 [-1, 1]。"""
        search, _ = small_index
        out = search.search("电力市场", top_k=5)
        for hit in out.hits:
            assert -1.0 <= hit.score <= 1.0

    def test_semantic_match(self, small_index):
        """语义相近的查询应命中对应切片。"""
        search, _ = small_index
        out = search.search("失信行为会有什么后果", top_k=2)
        assert out.returned >= 1
        combined = "".join(h.text for h in out.hits)
        assert "失信" in combined

    def test_empty_query(self, small_index):
        search, _ = small_index
        out = search.search("   ", top_k=3)
        assert out.returned == 0

    def test_invalid_top_k(self, small_index):
        search, _ = small_index
        with pytest.raises(DenseSearchError):
            search.search("电力市场", top_k=0)

    def test_hit_has_provenance(self, small_index):
        search, _ = small_index
        hit = search.search("电力市场", top_k=1).hits[0]
        assert hit.policy_id
        assert hit.document_id
        assert hit.version >= 1
        assert hit.start < hit.end
        assert hit.content_sha256

    def test_notice_disclaims_evidence(self, small_index):
        """相似度高不等于证据充分，notice 必须声明。"""
        search, _ = small_index
        out = search.search("电力市场", top_k=2)
        assert "不代表证据充分" in out.notice

    def test_deterministic(self, small_index):
        """同查询重复检索结果一致。"""
        search, _ = small_index
        a = [h.chunk_id for h in search.search("电力市场", top_k=3).hits]
        b = [h.chunk_id for h in search.search("电力市场", top_k=3).hits]
        assert a == b


# ==========================================================================
# 5. 缓存
# ==========================================================================
def _full_namespace(**overrides):
    """构造一个字段完整的命名空间（commit / tokenizer 指纹都不为空）。"""
    base = {
        "model": "BAAI/bge-small-zh-v1.5",
        "commit": "7999e1d3359715c523056ef9478215996d62a620",
        "tokenizer_fingerprint": "a" * 64,
        "dim": 512,
        "pooling": "cls_l2",
        "normalized": True,
        "encode_mode": "document",
        "max_tokens": 512,
    }
    base.update(overrides)
    return CacheNamespace(**base)


class TestEmbeddingCache:
    def test_put_get(self, tmp_path):
        cache = EmbeddingCache(tmp_path / "c.json", namespace=_full_namespace())
        vector = [0.1] * 512
        cache.put("电力市场", vector)
        assert cache.get("电力市场") == vector

    def test_miss_returns_none(self, tmp_path):
        cache = EmbeddingCache(tmp_path / "c.json", namespace=_full_namespace())
        assert cache.get("不存在") is None

    def test_persist_and_reload(self, tmp_path):
        path = tmp_path / "c.json"
        ns = _full_namespace()
        cache = EmbeddingCache(path, namespace=ns)
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        reloaded = EmbeddingCache(path, namespace=ns)
        assert reloaded.get("电力市场") == [0.5] * 512
        assert reloaded.rejected_reason is None

    def test_cache_uses_hash_not_plaintext(self, tmp_path):
        """缓存 key 是 sha256，不是原文。"""
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=_full_namespace())
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        raw = path.read_text(encoding="utf-8")
        assert "电力市场" not in raw

    def test_wrong_schema_ignored(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"schema": "other", "vectors": {"a": [1]}}))
        cache = EmbeddingCache(path, namespace=_full_namespace())
        assert len(cache) == 0
        assert cache.rejected_reason == "schema_mismatch"

    # ---- 新增：命名空间隔离 ----

    def test_incomplete_namespace_rejects_cache(self, tmp_path):
        """命名空间缺少 commit / tokenizer 指纹时，拒绝复用任何旧向量。"""
        path = tmp_path / "c.json"
        good = _full_namespace()
        cache = EmbeddingCache(path, namespace=good)
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        # 默认命名空间（无 embedder）缺 commit 与 tokenizer
        naive = EmbeddingCache(path)
        assert len(naive) == 0
        assert naive.rejected_reason is not None
        assert "namespace_incomplete" in naive.rejected_reason

    def test_commit_mismatch_invalidates(self, tmp_path):
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=_full_namespace())
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        other = _full_namespace(commit="b" * 40)
        reloaded = EmbeddingCache(path, namespace=other)
        assert len(reloaded) == 0
        assert reloaded.rejected_reason == "commit_mismatch"

    def test_tokenizer_mismatch_invalidates(self, tmp_path):
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=_full_namespace())
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        other = _full_namespace(tokenizer_fingerprint="b" * 64)
        reloaded = EmbeddingCache(path, namespace=other)
        assert len(reloaded) == 0
        assert reloaded.rejected_reason == "tokenizer_fingerprint_mismatch"

    def test_pooling_mismatch_invalidates(self, tmp_path):
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=_full_namespace())
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        other = _full_namespace(pooling="mean_l2")
        reloaded = EmbeddingCache(path, namespace=other)
        assert len(reloaded) == 0
        assert reloaded.rejected_reason == "pooling_mismatch"

    def test_normalized_mismatch_invalidates(self, tmp_path):
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=_full_namespace())
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        other = _full_namespace(normalized=False)
        reloaded = EmbeddingCache(path, namespace=other)
        assert len(reloaded) == 0
        assert reloaded.rejected_reason == "normalized_mismatch"

    def test_encode_mode_mismatch_invalidates(self, tmp_path):
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=_full_namespace())
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        other = _full_namespace(encode_mode="query")
        reloaded = EmbeddingCache(path, namespace=other)
        assert len(reloaded) == 0
        assert reloaded.rejected_reason == "encode_mode_mismatch"

    def test_dim_alone_is_not_enough(self, tmp_path):
        """仅维度相同不足以复用缓存。"""
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=_full_namespace())
        cache.put("电力市场", [0.5] * 512)
        cache.save()

        same_dim_other_model = _full_namespace(
            model="other/model", commit="c" * 40
        )
        reloaded = EmbeddingCache(path, namespace=same_dim_other_model)
        assert len(reloaded) == 0

    def test_build_uses_cache(self, tmp_path, embedder):
        """第二次构建应全部命中缓存。"""
        chunks = [_chunk("c1", "电力市场"), _chunk("c2", "电价")]
        manifest = _write_manifest(tmp_path, chunks)
        cache = tmp_path / "c.json"

        s1 = build_dense_index(
            manifest, tmp_path / "a.npz", cache_path=cache, embedder=embedder
        )
        s2 = build_dense_index(
            manifest, tmp_path / "b.npz", cache_path=cache, embedder=embedder
        )

        assert s1["encoded"] == 2
        assert s1["cached"] == 0
        assert s2["encoded"] == 0
        assert s2["cached"] == 2


# ==========================================================================
# 6. 一致性检查
# ==========================================================================
class TestConsistency:
    def test_missing_index(self, tmp_path):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "a")])
        with pytest.raises(DenseSearchError, match="索引不存在"):
            DenseSearch(tmp_path / "no.npz", manifest_path=manifest)

    def test_missing_manifest(self, tmp_path, embedder):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "i.npz"
        build_dense_index(manifest, index, embedder=embedder)

        with pytest.raises(DenseSearchError, match="清单不存在"):
            DenseSearch(index, manifest_path=tmp_path / "no.json")

    def test_manifest_mismatch_rejected(self, tmp_path, embedder):
        """清单变了但索引没重建 → 必须拒绝。"""
        manifest = _write_manifest(
            tmp_path, [_chunk("c1", "电力市场")], manifest_sha="hash-a"
        )
        index = tmp_path / "i.npz"
        build_dense_index(manifest, index, embedder=embedder)

        changed = _write_manifest(
            tmp_path, [_chunk("c1", "电力市场")], manifest_sha="hash-b"
        )

        with pytest.raises(DenseSearchError, match="不匹配"):
            DenseSearch(index, manifest_path=changed)

    def test_describe(self, small_index):
        search, _ = small_index
        info = search.describe()
        assert info["dim"] == 512
        assert info["pooling"] == "cls_l2"
        assert info["chunks"] == 5
        assert info["fingerprint"]


# ==========================================================================
# 7. 真实数据
# ==========================================================================
@pytest.mark.skipif(not MANIFEST.is_file(), reason="缺少切片清单")
class TestRealData:
    @pytest.fixture(scope="class")
    @classmethod
    def real_index(cls, tmp_path_factory, embedder):
        folder = tmp_path_factory.mktemp("real")
        index = folder / "dense.npz"
        build_dense_index(
            MANIFEST, index, cache_path=folder / "c.json", embedder=embedder
        )
        return DenseSearch(index, manifest_path=MANIFEST, embedder=embedder)

    def test_chunk_count(self, real_index):
        assert real_index.chunk_count() == 26

    def test_dim(self, real_index):
        assert real_index.meta["dim"] == 512

    def test_real_query(self, real_index):
        out = real_index.search("电力市场注册需要什么条件", top_k=5)
        assert out.returned == 5
        assert out.hits[0].score > 0.4

    def test_provenance_complete(self, real_index):
        hit = real_index.search("电力市场", top_k=1).hits[0]
        assert len(hit.content_sha256) == 64
        assert hit.source_url.startswith("https://")
        assert hit.version == 1

    def test_no_false_confidence_on_absent_topic(self, real_index):
        """库里没有安徽政策，但向量检索仍会返回 Top-K。

        这不是 bug，而是必须如实记录的行为：
        相似度高不代表存在证据。
        """
        out = real_index.search("安徽电力现货市场交易规则", top_k=5)
        # 向量检索必然返回结果
        assert out.returned == 5
        # 但不应命中任何含"安徽"的切片（库里没有）
        assert not any("安徽" in h.text for h in out.hits)
        # notice 必须声明
        assert "不代表证据充分" in out.notice
