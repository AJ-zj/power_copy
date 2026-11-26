"""05.5-A 混合检索（等权 RRF）测试。

覆盖项目负责人要求的全部场景：
- 双路命中贡献累加
- 单路命中计算
- 重复 Chunk 去重
- 同分稳定排序
- 空列表处理
- 检索器异常及降级状态
- 非法参数拒绝
- 不同候选深度
- 索引指纹不一致
- 证据元信息完整性
"""

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from powercopy.rag.hybrid import (
    DEFAULT_CANDIDATE_K,
    DEFAULT_FINAL_TOP_K,
    DEFAULT_RANK_CONSTANT,
    HybridError,
    HybridRetriever,
    RetrieverFailure,
    rrf_contribution,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 测试替身
class FakeHit:
    """模拟 LexicalHit / DenseHit。"""

    def __init__(self, chunk_id, rank, score, match_mode="fake", **extra):
        self.chunk_id = chunk_id
        self.rank = rank
        self.score = score
        self.match_mode = match_mode
        self.policy_id = extra.get("policy_id", "p1")
        self.document_id = extra.get("document_id", "d1")
        self.version = extra.get("version", 1)
        self.page = extra.get("page")
        self.start = extra.get("start", 0)
        self.end = extra.get("end", 100)
        self.title = extra.get("title", "测试政策")
        self.source_key = extra.get("source_key", "demo")
        self.source_url = extra.get("source_url", "https://example.invalid/a")
        self.content_sha256 = extra.get("content_sha256", "0" * 64)
        self.text = extra.get("text", f"文本 {chunk_id}")


class FakeRetriever:
    """模拟检索器。"""

    def __init__(self, hits=None, *, error=None, mode="fake_mode",
                 manifest_sha="hash-a"):
        self._hits = hits or []
        self._error = error
        self._mode = mode
        self._manifest_sha = manifest_sha
        self.calls = []

    def search(self, query, *, top_k=10):
        self.calls.append((query, top_k))
        if self._error is not None:
            raise self._error
        selected = self._hits[:top_k]
        return SimpleNamespace(
            hits=selected,
            match_mode=self._mode,
            returned=len(selected),
        )

    def describe(self):
        return {"manifest_sha256": self._manifest_sha, "tokenizer": "trigram"}

    @property
    def meta(self):
        return {"manifest_sha256": self._manifest_sha, "dim": 512}


def make_hybrid(lexical_hits=None, dense_hits=None, **kwargs):
    lex = FakeRetriever(lexical_hits, mode="fts5_trigram_phrase")
    dns = FakeRetriever(dense_hits, mode="dense_bge")
    return HybridRetriever(lex, dns, **kwargs)


# ==========================================================================
# 1. RRF 公式
# ==========================================================================
class TestRrfFormula:
    def test_rank_one(self):
        assert rrf_contribution(1, 60, 1.0) == pytest.approx(1 / 61)

    def test_rank_two(self):
        assert rrf_contribution(2, 60, 1.0) == pytest.approx(1 / 62)

    def test_rank_ten(self):
        assert rrf_contribution(10, 60, 1.0) == pytest.approx(1 / 70)

    def test_not_retrieved_is_zero(self):
        assert rrf_contribution(None, 60, 1.0) == 0.0

    def test_weight_scales(self):
        assert rrf_contribution(1, 60, 2.0) == pytest.approx(2 / 61)

    def test_monotonic_decreasing(self):
        prev = rrf_contribution(1, 60, 1.0)
        for rank in range(2, 20):
            cur = rrf_contribution(rank, 60, 1.0)
            assert cur < prev
            prev = cur

    def test_rank_zero_rejected(self):
        with pytest.raises(HybridError, match="rank 必须从 1 开始"):
            rrf_contribution(0, 60, 1.0)

    def test_negative_rank_rejected(self):
        with pytest.raises(HybridError):
            rrf_contribution(-1, 60, 1.0)

    def test_rank_constant_zero_rejected(self):
        with pytest.raises(HybridError, match="rank_constant"):
            rrf_contribution(1, 0, 1.0)

    def test_negative_weight_rejected(self):
        with pytest.raises(HybridError, match="weight"):
            rrf_contribution(1, 60, -1.0)

    def test_default_constant_is_60(self):
        assert DEFAULT_RANK_CONSTANT == 60


# ==========================================================================
# 2. 双路命中累加
# ==========================================================================
class TestBothPaths:
    def test_contribution_accumulates(self):
        """同一 chunk 在两路都命中，RRF 应相加。"""
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)],
            dense_hits=[FakeHit("c1", 2, 0.8)],
        )
        out = hybrid.search("q", final_top_k=5)

        assert out.returned == 1
        hit = out.hits[0]
        expected = 1 / 61 + 1 / 62
        assert hit.rrf_score == pytest.approx(expected)
        assert len(hit.contributions) == 2

    def test_contributions_recorded_separately(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 3, 0.5, match_mode="fts5_trigram_phrase")],
            dense_hits=[FakeHit("c1", 1, 0.9, match_mode="dense_bge")],
        )
        out = hybrid.search("q", final_top_k=5)
        hit = out.hits[0]

        by_path = {c.path: c for c in hit.contributions}
        assert by_path["lexical"].rank == 3
        assert by_path["lexical"].score == 0.5
        assert by_path["lexical"].match_mode == "fts5_trigram_phrase"
        assert by_path["dense"].rank == 1
        assert by_path["dense"].score == 0.9
        assert by_path["dense"].match_mode == "dense_bge"

    def test_both_paths_beats_single(self):
        """两路都命中的 chunk 应排在两路各自命中的 chunk 前面。"""
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("both", 1, 0.9), FakeHit("lex_only", 2, 0.8)],
            dense_hits=[FakeHit("both", 1, 0.9), FakeHit("dense_only", 2, 0.8)],
        )
        out = hybrid.search("q", final_top_k=5)

        assert out.hits[0].chunk_id == "both"
        assert out.hits[0].rrf_score > out.hits[1].rrf_score


# ==========================================================================
# 3. 单路命中
# ==========================================================================
class TestSinglePath:
    def test_lexical_only(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)],
            dense_hits=[],
        )
        out = hybrid.search("q", final_top_k=5)

        assert out.returned == 1
        hit = out.hits[0]
        assert hit.rrf_score == pytest.approx(1 / 61)
        # 只有一路贡献
        assert len(hit.contributions) == 1
        assert hit.contributions[0].path == "lexical"

    def test_dense_only(self):
        hybrid = make_hybrid(
            lexical_hits=[],
            dense_hits=[FakeHit("c1", 1, 0.9)],
        )
        out = hybrid.search("q", final_top_k=5)

        assert out.returned == 1
        assert out.hits[0].rrf_score == pytest.approx(1 / 61)
        assert out.hits[0].contributions[0].path == "dense"

    def test_unretrieved_contributes_zero(self):
        """某路未召回的候选，该路贡献为 0（不出现在 contributions 中）。"""
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("lex", 1, 0.9)],
            dense_hits=[FakeHit("dns", 1, 0.9)],
        )
        out = hybrid.search("q", final_top_k=5)

        for hit in out.hits:
            paths = [c.path for c in hit.contributions]
            assert len(paths) == 1, "每路各命中不同 chunk，不应有多路贡献"


# ==========================================================================
# 4. 去重
# ==========================================================================
class TestDeduplication:
    def test_same_chunk_merged(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)],
            dense_hits=[FakeHit("c1", 1, 0.8)],
        )
        out = hybrid.search("q", final_top_k=10)

        assert out.returned == 1
        assert len([h for h in out.hits if h.chunk_id == "c1"]) == 1

    def test_no_duplicate_chunk_ids(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit(f"c{i}", i, 1.0 - i * 0.1) for i in range(1, 6)],
            dense_hits=[FakeHit(f"c{i}", i, 1.0 - i * 0.1) for i in range(1, 6)],
        )
        out = hybrid.search("q", final_top_k=10)

        ids = [h.chunk_id for h in out.hits]
        assert len(ids) == len(set(ids)) == 5


# ==========================================================================
# 5. 同分稳定排序
# ==========================================================================
class TestStableTieBreak:
    def test_tie_broken_by_chunk_id(self):
        """RRF 同分时按 chunk_id 升序。"""
        # 两路各有独立 chunk，排名相同 → RRF 相同
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("zzz", 1, 0.9)],
            dense_hits=[FakeHit("aaa", 1, 0.9)],
        )
        out = hybrid.search("q", final_top_k=10)

        assert out.returned == 2
        assert out.hits[0].rrf_score == out.hits[1].rrf_score
        # chunk_id 升序
        assert out.hits[0].chunk_id == "aaa"
        assert out.hits[1].chunk_id == "zzz"

    def test_deterministic(self):
        """重复检索结果一致。"""
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("b", 1, 0.9), FakeHit("a", 1, 0.9)],
            dense_hits=[FakeHit("c", 1, 0.9)],
        )
        first = [h.chunk_id for h in hybrid.search("q", final_top_k=10).hits]
        second = [h.chunk_id for h in hybrid.search("q", final_top_k=10).hits]
        assert first == second

    def test_rank_sequence(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit(f"c{i}", i, 1.0) for i in range(1, 6)],
            dense_hits=[],
        )
        out = hybrid.search("q", final_top_k=5)
        assert [h.rank for h in out.hits] == [1, 2, 3, 4, 5]

    def test_scores_descending(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit(f"c{i}", i, 1.0) for i in range(1, 6)],
            dense_hits=[FakeHit(f"d{i}", i, 1.0) for i in range(1, 6)],
        )
        out = hybrid.search("q", final_top_k=10)
        scores = [h.rrf_score for h in out.hits]
        assert scores == sorted(scores, reverse=True)


# ==========================================================================
# 6. 空列表处理
# ==========================================================================
class TestEmptyHandling:
    def test_both_empty(self):
        hybrid = make_hybrid(lexical_hits=[], dense_hits=[])
        out = hybrid.search("q", final_top_k=5)

        assert out.returned == 0
        assert out.is_complete is True
        assert out.lexical_returned == 0
        assert out.dense_returned == 0

    def test_empty_query(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)], dense_hits=[]
        )
        out = hybrid.search("   ", final_top_k=5)

        assert out.returned == 0
        assert "为空" in out.notice

    def test_final_top_k_zero_rejected(self):
        hybrid = make_hybrid()
        with pytest.raises(HybridError):
            hybrid.search("q", final_top_k=0)

    def test_final_top_k_larger_than_candidates(self):
        """final_top_k > 候选数时返回实际数量。"""
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)], dense_hits=[]
        )
        out = hybrid.search("q", final_top_k=100)
        assert out.returned == 1


# ==========================================================================
# 7. 检索器异常与降级
# ==========================================================================
class TestFailureAndDegrade:
    def test_failure_without_degrade_raises(self):
        """默认不允许降级，单路异常直接抛错。"""
        lex = FakeRetriever(error=ValueError("boom"))
        dns = FakeRetriever([FakeHit("c1", 1, 0.9)])
        hybrid = HybridRetriever(lex, dns, allow_degrade=False)

        with pytest.raises(Exception):
            hybrid.search("q")

    def test_degrade_lexical_failure(self):
        """允许降级时，显式返回降级状态。"""
        lex = FakeRetriever(error=ValueError("boom"))
        dns = FakeRetriever([FakeHit("c1", 1, 0.9)], mode="dense_bge")
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        out = hybrid.search("q", final_top_k=5)

        assert out.is_complete is False
        assert out.degraded == "lexical_failed"
        assert "降级" in out.degraded_detail
        assert out.returned == 1

    def test_degrade_dense_failure(self):
        lex = FakeRetriever([FakeHit("c1", 1, 0.9)], mode="fts5_trigram_phrase")
        dns = FakeRetriever(error=ValueError("boom"))
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        out = hybrid.search("q", final_top_k=5)

        assert out.is_complete is False
        assert out.degraded == "dense_failed"
        assert out.returned == 1

    def test_both_paths_failed(self):
        lex = FakeRetriever(error=ValueError("lex boom"))
        dns = FakeRetriever(error=ValueError("dense boom"))
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        out = hybrid.search("q", final_top_k=5)

        assert out.is_complete is False
        assert out.degraded == "both_paths_failed"
        assert out.returned == 0

    def test_degraded_notice_is_explicit(self):
        """降级不能伪装成完整混合检索。"""
        lex = FakeRetriever(error=ValueError("boom"))
        dns = FakeRetriever([FakeHit("c1", 1, 0.9)])
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        out = hybrid.search("q", final_top_k=5)
        payload = out.to_dict()

        assert payload["match_mode"] == "hybrid_rrf_degraded"
        assert payload["degraded"] is not None
        assert "⚠ 降级模式" in out.notice

    def test_normal_empty_not_confused_with_failure(self):
        """正常空结果不是异常。"""
        lex = FakeRetriever([])
        dns = FakeRetriever([])
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        out = hybrid.search("q", final_top_k=5)

        assert out.returned == 0
        assert out.is_complete is True, "空结果是正常状态，不应标记降级"
        assert out.degraded is None


# ==========================================================================
# 8. 非法参数
# ==========================================================================
class TestInvalidConfig:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"rank_constant": 0},
            {"rank_constant": -1},
            {"rank_constant": 1.5},
            {"rank_constant": 99999},
        ],
    )
    def test_invalid_rank_constant(self, kwargs):
        with pytest.raises(HybridError):
            HybridRetriever(
                FakeRetriever(), FakeRetriever(), **kwargs
            )

    @pytest.mark.parametrize(
        "weights",
        [
            {"lexical": 1.0},
            {"lexical": 1.0, "dense": 1.0, "extra": 1.0},
            {"lexical": -1.0, "dense": 1.0},
            {"lexical": 0, "dense": 0},
        ],
    )
    def test_invalid_weights(self, weights):
        with pytest.raises(HybridError):
            HybridRetriever(
                FakeRetriever(), FakeRetriever(), weights=weights
            )

    @pytest.mark.parametrize("k", [0, -1, 500])
    def test_invalid_candidate_k(self, k):
        hybrid = make_hybrid()
        with pytest.raises(HybridError):
            hybrid.search("q", lexical_candidate_k=k)

    def test_both_retrievers_none(self):
        with pytest.raises(HybridError, match="至少需要一路"):
            HybridRetriever(None, None)


# ==========================================================================
# 9. 候选深度
# ==========================================================================
class TestCandidateDepth:
    def test_lexical_depth_controls_candidates(self):
        lex = FakeRetriever([FakeHit(f"c{i}", i, 1.0) for i in range(1, 11)])
        dns = FakeRetriever([])
        hybrid = HybridRetriever(lex, dns)

        hybrid.search("q", lexical_candidate_k=3, final_top_k=10)
        assert lex.calls[-1] == ("q", 3)

    def test_dense_depth_controls_candidates(self):
        lex = FakeRetriever([])
        dns = FakeRetriever([FakeHit(f"c{i}", i, 1.0) for i in range(1, 11)])
        hybrid = HybridRetriever(lex, dns)

        hybrid.search("q", dense_candidate_k=2, final_top_k=10)
        assert dns.calls[-1] == ("q", 2)

    def test_deeper_candidates_more_results(self):
        hits = [FakeHit(f"c{i}", i, 1.0) for i in range(1, 11)]
        hybrid = make_hybrid(lexical_hits=hits, dense_hits=[])

        shallow = hybrid.search("q", lexical_candidate_k=2, final_top_k=10)
        deep = hybrid.search("q", lexical_candidate_k=8, final_top_k=10)

        assert shallow.returned == 2
        assert deep.returned == 8

    def test_final_top_k_independent(self):
        hits = [FakeHit(f"c{i}", i, 1.0) for i in range(1, 11)]
        hybrid = make_hybrid(lexical_hits=hits, dense_hits=[])

        out = hybrid.search("q", lexical_candidate_k=10, final_top_k=3)
        assert out.returned == 3
        assert out.lexical_candidate_k == 10
        assert out.final_top_k == 3


# ==========================================================================
# 10. 索引一致性
# ==========================================================================
class TestIndexConsistency:
    def test_consistent_when_same_hash(self):
        lex = FakeRetriever(manifest_sha="same")
        dns = FakeRetriever(manifest_sha="same")
        hybrid = HybridRetriever(lex, dns)

        info = hybrid.check_index_consistency()
        assert info["consistent"] is True
        assert info["status"] == "ok"

    def test_mismatch_rejected_by_default(self):
        lex = FakeRetriever(manifest_sha="hash-a")
        dns = FakeRetriever(manifest_sha="hash-b")
        hybrid = HybridRetriever(lex, dns)

        with pytest.raises(HybridError, match="版本冲突"):
            hybrid.search("q")

    def test_mismatch_rejected_even_with_degrade(self):
        """版本冲突时即使开启降级也必须拒绝融合。

        跨版本的 chunk_id 无法保证指向同一份内容，融合结果不可追溯。
        """
        lex = FakeRetriever(
            [FakeHit("c1", 1, 0.9)], manifest_sha="hash-a"
        )
        dns = FakeRetriever(
            [FakeHit("c2", 1, 0.9)], manifest_sha="hash-b"
        )
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        with pytest.raises(HybridError, match="版本冲突"):
            hybrid.search("q")

        with pytest.raises(HybridError, match="版本冲突"):
            hybrid.check_index_consistency()

    def test_fingerprint_missing_not_consistent(self):
        """两路都在但任一指纹缺失，不得判定一致。"""
        lex = FakeRetriever(manifest_sha="")
        dns = FakeRetriever(manifest_sha="hash-b")
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        info = hybrid.check_index_consistency()
        assert info["consistent"] is False
        assert info["status"] == "fingerprint_missing"

    def test_fingerprint_missing_rejected_without_degrade(self):
        lex = FakeRetriever(manifest_sha="")
        dns = FakeRetriever(manifest_sha="hash-b")
        hybrid = HybridRetriever(lex, dns, allow_degrade=False)

        with pytest.raises(HybridError, match="指纹缺失"):
            hybrid.check_index_consistency()

    def test_fingerprint_missing_returns_empty(self):
        """指纹缺失 + 允许降级：返回空结果并显式标注。"""
        lex = FakeRetriever([FakeHit("c1", 1, 0.9)], manifest_sha="")
        dns = FakeRetriever([FakeHit("c2", 1, 0.9)], manifest_sha="hash-b")
        hybrid = HybridRetriever(lex, dns, allow_degrade=True)

        out = hybrid.search("q", final_top_k=5)
        assert out.is_complete is False
        assert out.degraded == "fingerprint_missing"
        assert out.returned == 0
        assert out.consistency_status == "fingerprint_missing"

    def test_single_path_unconfigured_not_consistent(self):
        """只配置一路时，不得判定为「一致」，应显式标注降级。"""
        lex = FakeRetriever([FakeHit("c1", 1, 0.9)], manifest_sha="hash-a")
        hybrid = HybridRetriever(lex, None)

        info = hybrid.check_index_consistency()
        assert info["consistent"] is False
        assert info["status"] == "single_path_unconfigured"
        assert info["degraded_path"] == "lexical"

    def test_single_path_search_is_degraded(self):
        lex = FakeRetriever([FakeHit("c1", 1, 0.9)], manifest_sha="hash-a")
        hybrid = HybridRetriever(lex, None)

        out = hybrid.search("q", final_top_k=5)
        assert out.returned == 1
        assert out.is_complete is False
        assert out.degraded == "single_path_unconfigured"
        assert "不是完整混合检索" in out.notice


# ==========================================================================
# 11. 证据元信息
# ==========================================================================
class TestEvidenceMetadata:
    def test_all_fields_preserved(self):
        hit = FakeHit(
            "c1",
            1,
            0.9,
            policy_id="policy-abc",
            document_id="doc-xyz",
            version=3,
            page=7,
            start=100,
            end=450,
            title="测试政策标题",
            source_key="nea",
            source_url="https://example.invalid/p",
            content_sha256="a" * 64,
            text="正文内容",
        )
        hybrid = make_hybrid(lexical_hits=[hit], dense_hits=[])
        out = hybrid.search("q", final_top_k=5)

        h = out.hits[0]
        assert h.policy_id == "policy-abc"
        assert h.document_id == "doc-xyz"
        assert h.version == 3
        assert h.page == 7
        assert h.start == 100
        assert h.end == 450
        assert h.title == "测试政策标题"
        assert h.source_key == "nea"
        assert h.source_url == "https://example.invalid/p"
        assert h.content_sha256 == "a" * 64
        assert h.text == "正文内容"

    def test_to_dict_serializable(self):
        import json

        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)],
            dense_hits=[FakeHit("c1", 2, 0.8)],
        )
        out = hybrid.search("q", final_top_k=5)
        text = json.dumps(out.to_dict(), ensure_ascii=False)
        assert "c1" in text

    def test_contributions_in_dict(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)],
            dense_hits=[FakeHit("c1", 3, 0.7)],
        )
        payload = hybrid.search("q", final_top_k=5).to_dict()
        contribs = payload["hits"][0]["contributions"]

        assert len(contribs) == 2
        assert {c["path"] for c in contribs} == {"lexical", "dense"}
        for c in contribs:
            assert "rank" in c
            assert "score" in c
            assert "match_mode" in c
            assert "rrf" in c


# ==========================================================================
# 12. 权重
# ==========================================================================
class TestWeights:
    def test_equal_weights_default(self):
        lex = FakeRetriever()
        dns = FakeRetriever()
        hybrid = HybridRetriever(lex, dns)
        assert hybrid.weights == {"lexical": 1.0, "dense": 1.0}

    def test_custom_weights(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)],
            dense_hits=[],
            weights={"lexical": 2.0, "dense": 1.0},
        )
        out = hybrid.search("q", final_top_k=5)
        assert out.hits[0].rrf_score == pytest.approx(2 / 61)

    def test_zero_weight_ignores_path(self):
        hybrid = make_hybrid(
            lexical_hits=[FakeHit("c1", 1, 0.9)],
            dense_hits=[FakeHit("c2", 1, 0.9)],
            weights={"lexical": 0.0, "dense": 1.0},
        )
        out = hybrid.search("q", final_top_k=5)

        # lexical 权重 0，其贡献为 0
        c1 = next(h for h in out.hits if h.chunk_id == "c1")
        assert c1.rrf_score == 0.0
        # c2 有贡献
        c2 = next(h for h in out.hits if h.chunk_id == "c2")
        assert c2.rrf_score > 0


# ==========================================================================
# 13. 真实索引集成
# ==========================================================================
@pytest.mark.skipif(
    not (ROOT / "storage" / "rag" / "lexical.db").is_file()
    or not (ROOT / "storage" / "rag" / "dense.npz").is_file(),
    reason="缺少真实索引",
)
class TestRealIntegration:
    @pytest.fixture(scope="class")
    @classmethod
    def hybrid(cls):
        from powercopy.rag.dense import DenseSearch
        from powercopy.rag.lexical import LexicalSearch

        lexical = LexicalSearch(ROOT / "storage" / "rag" / "lexical.db")
        dense = DenseSearch(
            ROOT / "storage" / "rag" / "dense.npz",
            manifest_path=ROOT / "storage" / "rag" / "chunks.json",
        )
        return HybridRetriever(lexical, dense)

    def test_indexes_consistent(self, hybrid):
        info = hybrid.check_index_consistency()
        assert info["consistent"] is True

    def test_real_query(self, hybrid):
        out = hybrid.search("电力市场注册", final_top_k=10)
        assert out.returned == 10
        assert out.is_complete is True

    def test_hybrid_recalls_both_paths(self, hybrid):
        """混合结果应覆盖两路各自的部分候选。"""
        out = hybrid.search("电力市场注册", final_top_k=10)

        has_both = sum(1 for h in out.hits if len(h.contributions) == 2)
        assert has_both > 0, "应有 chunk 被两路同时命中"

    def test_evidence_metadata_complete(self, hybrid):
        hit = hybrid.search("电力市场注册", final_top_k=1).hits[0]
        assert hit.policy_id
        assert hit.document_id
        assert hit.version >= 1
        assert hit.start < hit.end
        assert hit.content_sha256
        assert "/" not in hit.policy_id  # policy_id 应为 32 位 hex

    def test_notice_disclaims_evidence_sufficiency(self, hybrid):
        out = hybrid.search("电力市场", final_top_k=5)
        assert "不判断证据充分性" in out.notice
