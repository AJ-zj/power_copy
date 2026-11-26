"""本地 Embedding 编码器测试。

使用真实的 BGE-small-zh-v1.5 模型（已下载到 storage/models）。
若模型不可用则整组跳过，不影响其他测试。
"""

import math
from pathlib import Path

import pytest

from powercopy.rag.embedding import (
    EMBEDDING_DIM,
    MAX_TOKENS,
    MODEL_NAME,
    QUERY_INSTRUCTION,
    BGEEmbedder,
    EmbeddingError,
    cosine_similarity,
    rank_by_similarity,
)

ROOT = Path(__file__).resolve().parents[1]


def _model_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _model_available(),
    reason="未安装 torch/transformers，跳过本地 Embedding 测试",
)


@pytest.fixture(scope="module")
def embedder():
    return BGEEmbedder()


# ==========================================================================
# 1. 基础编码
# ==========================================================================
class TestBasicEncoding:
    def test_dimension(self, embedder):
        vector = embedder.encode_documents(["电力市场"])
        assert len(vector) == 1
        assert len(vector[0]) == EMBEDDING_DIM == 512

    def test_l2_normalized(self, embedder):
        vector = embedder.encode_documents(["电价形成机制"])[0]
        norm = math.sqrt(sum(v * v for v in vector))
        assert abs(norm - 1.0) < 1e-5

    def test_empty_input(self, embedder):
        assert embedder.encode_documents([]) == []

    def test_batch_consistency(self, embedder):
        """批处理与单条编码结果应一致。"""
        texts = ["第一条 交易周期为15分钟。", "第二条 电价为3元/千瓦时。"]

        batched = embedder.encode_documents(texts)
        singles = [embedder.encode_documents([t])[0] for t in texts]

        for a, b in zip(batched, singles):
            for x, y in zip(a, b):
                assert abs(x - y) < 1e-5

    def test_deterministic(self, embedder):
        a = embedder.encode_documents(["政策文案测试"])[0]
        b = embedder.encode_documents(["政策文案测试"])[0]
        assert a == b

    def test_different_text_different_vector(self, embedder):
        a = embedder.encode_documents(["电价"])[0]
        b = embedder.encode_documents(["注册"])[0]
        assert a != b

    def test_non_string_rejected(self, embedder):
        with pytest.raises(EmbeddingError):
            embedder.encode_documents([123])  # type: ignore[list-item]

    def test_describe(self, embedder):
        info = embedder.describe()
        assert info["model"] == MODEL_NAME
        assert info["dim"] == EMBEDDING_DIM
        assert info["max_tokens"] == MAX_TOKENS
        assert info["device"] == "cpu"


# ==========================================================================
# 2. 查询前缀
# ==========================================================================
class TestQueryInstruction:
    def test_query_uses_instruction(self, embedder):
        """查询加了前缀，文档不加，两者向量应不同。"""
        text = "电价形成机制"
        as_query = embedder.encode([text], as_query=True)[0]
        as_doc = embedder.encode([text], as_query=False)[0]
        assert as_query != as_doc

    def test_encode_query_helper(self, embedder):
        a = embedder.encode_query("电价")
        b = embedder.encode(["电价"], as_query=True)[0]
        assert a == b

    def test_encode_documents_helper(self, embedder):
        a = embedder.encode_documents(["电价"])[0]
        b = embedder.encode(["电价"], as_query=False)[0]
        assert a == b

    def test_instruction_text(self):
        assert QUERY_INSTRUCTION == "为这个句子生成表示以用于检索相关文章："


# ==========================================================================
# 3. 语义合理性
# ==========================================================================
class TestSemanticQuality:
    def test_similar_beats_unrelated(self, embedder):
        """近义表达必须比无关内容更相似。"""
        query = embedder.encode_query("电价是怎么确定的")
        docs = embedder.encode_documents(
            [
                "电价形成机制由市场决定。",
                "经营主体应当办理市场注册手续。",
            ]
        )

        related = cosine_similarity(query, docs[0])
        unrelated = cosine_similarity(query, docs[1])

        assert related > unrelated, (
            f"近义({related:.3f}) 应高于无关({unrelated:.3f})"
        )

    def test_real_policy_semantics(self, embedder):
        """真实政策标题之间的语义关系。"""
        query = embedder.encode_query("电力市场运行规则")
        docs = embedder.encode_documents(
            [
                "电力市场运行基本规则",
                "电力市场注册基本规则",
                "安徽电力现货市场转入正式运行",
            ]
        )

        scores = [cosine_similarity(query, d) for d in docs]
        # 两个"规则"类应高于新闻
        assert scores[0] > scores[2]
        assert scores[1] > scores[2]

    def test_same_text_similarity_is_one(self, embedder):
        v = embedder.encode_documents(["电力市场"])[0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-5


# ==========================================================================
# 4. 相似度工具函数
# ==========================================================================
class TestSimilarityHelpers:
    def test_cosine_same_vector(self):
        v = [1.0, 0.0, 0.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-9

    def test_cosine_orthogonal(self):
        assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_cosine_opposite(self):
        assert abs(cosine_similarity([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-9

    def test_dimension_mismatch(self):
        with pytest.raises(EmbeddingError):
            cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])

    def test_rank_by_similarity(self):
        query = [1.0, 0.0]
        candidates = [
            ("low", [0.0, 1.0]),
            ("high", [1.0, 0.0]),
            ("mid", [0.7071, 0.7071]),
        ]
        ranked = rank_by_similarity(query, candidates, top_k=3)

        assert [k for k, _ in ranked] == ["high", "mid", "low"]
        assert abs(ranked[0][1] - 1.0) < 1e-3

    def test_rank_top_k(self):
        query = [1.0, 0.0]
        candidates = [(str(i), [1.0, float(i) / 10]) for i in range(5)]
        ranked = rank_by_similarity(query, candidates, top_k=2)
        assert len(ranked) == 2

    def test_rank_invalid_k(self):
        with pytest.raises(EmbeddingError):
            rank_by_similarity([1.0], [], top_k=0)

    def test_rank_empty_candidates(self):
        assert rank_by_similarity([1.0, 0.0], [], top_k=3) == []


# ==========================================================================
# 5. 惰性加载
# ==========================================================================
class TestLazyLoading:
    def test_not_loaded_before_encode(self):
        fresh = BGEEmbedder()
        assert fresh.loaded is False

    def test_loaded_after_encode(self, embedder):
        embedder.encode_documents(["触发加载"])
        assert embedder.loaded is True

    def test_describe_reports_loaded_state(self):
        fresh = BGEEmbedder()
        assert fresh.describe()["loaded"] is False


# ==========================================================================
# 6. 真实切片编码
# ==========================================================================
@pytest.mark.skipif(
    not (ROOT / "storage" / "rag" / "chunks.json").is_file(),
    reason="缺少切片清单，跳过真实数据编码测试",
)
class TestRealChunks:
    @pytest.fixture(scope="class")
    @classmethod
    def manifest(cls):
        import json

        return json.loads(
            (ROOT / "storage" / "rag" / "chunks.json").read_text(
                encoding="utf-8"
            )
        )

    def test_encode_all_chunks(self, embedder, manifest):
        chunks = manifest["chunks"]
        assert len(chunks) > 0

        texts = [c["text"] for c in chunks]
        vectors = embedder.encode_documents(texts)

        assert len(vectors) == len(chunks)
        assert all(len(v) == EMBEDDING_DIM for v in vectors)

    def test_real_query_ranks_sensibly(self, embedder, manifest):
        """用真实查询检索真实切片，检查排序合理。"""
        chunks = manifest["chunks"]
        texts = [c["text"] for c in chunks]
        vectors = embedder.encode_documents(texts)

        query = embedder.encode_query("电力市场注册需要什么条件")
        candidates = [(c["chunk_id"], v) for c, v in zip(chunks, vectors)]
        ranked = rank_by_similarity(query, candidates, top_k=3)

        assert len(ranked) == 3
        # 分数应递减
        scores = [s for _, s in ranked]
        assert scores == sorted(scores, reverse=True)
        # 最高分应大于 0.4（相关内容的合理下界）
        assert scores[0] > 0.4
