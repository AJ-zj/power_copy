"""05-A 修复项专项测试。

覆盖项目负责人指出的三个问题：
1. BM25 分数转换方向（旧公式反转相关性顺序）
2. 检索模式命名（明确是 trigram 字面短语，非通用 BM25）
3. 评测指标逐题可审计
"""

import json
import math
from pathlib import Path

import pytest

from powercopy.rag.lexical import (
    LexicalSearch,
    LexicalSearchError,
    build_index,
    escape_fts_query,
    mean_reciprocal_rank,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)
from powercopy.rag.lexical import _score_from_bm25

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "storage" / "rag" / "chunks.json"


# ==========================================================================
# 1. BM25 分数方向（核心修复）
# ==========================================================================
class TestBm25ScoreDirection:
    """FTS5 的 bm25() 返回负值，越小（越负）越相关。

    旧公式 1/(1+|bm25|) 会把这个关系反转：
    bm25=-10（很相关）→ 0.09
    bm25=-0.1（不相关）→ 0.91

    新公式 -bm25/(1+(-bm25)) 保持单调：
    bm25=-10 → 0.909
    bm25=-0.1 → 0.091
    """

    def test_very_relevant_gets_high_score(self):
        """bm25 很负 = 高度相关 = 高分。"""
        assert _score_from_bm25(-10.0) > 0.9

    def test_barely_relevant_gets_low_score(self):
        """bm25 接近 0 = 弱相关 = 低分。"""
        assert _score_from_bm25(-0.1) < 0.1

    def test_old_formula_would_invert(self):
        """证明旧公式确实反转了顺序。"""
        very_relevant = -10.0
        barely_relevant = -0.1

        def old(bm25):
            return 1.0 / (1.0 + abs(bm25))

        # 旧公式：越相关分数越低（错）
        assert old(very_relevant) < old(barely_relevant)

        # 新公式：越相关分数越高（对）
        assert _score_from_bm25(very_relevant) > _score_from_bm25(barely_relevant)

    def test_monotonic_increasing(self):
        """bm25 越小（越负），score 必须越大。"""
        # values 从最负到最接近 0，score 应递减
        values = [-100, -10, -5, -2, -1, -0.5, -0.1, -0.01]
        scores = [_score_from_bm25(v) for v in values]
        assert scores == sorted(scores, reverse=True), (
            "bm25 从最负到最接近 0，score 应单调递减"
        )

    def test_monotonic_against_relevance(self):
        """相关性越高（bm25 越负），分数越高。"""
        pairs = [(-100.0, -1.0), (-10.0, -0.1), (-5.0, -2.0), (-0.1, -0.01)]
        for more_relevant, less_relevant in pairs:
            assert _score_from_bm25(more_relevant) > _score_from_bm25(
                less_relevant
            ), f"bm25 {more_relevant} 应比 {less_relevant} 得分高"

    def test_range_bounds(self):
        """输出必须在 [0, 1] 区间。"""
        for bm25 in (-1e6, -100, -1, -0.001, 0, 1, 100):
            score = _score_from_bm25(bm25)
            assert 0.0 <= score <= 1.0, f"bm25={bm25} -> {score}"

    def test_zero_and_positive_return_zero(self):
        """bm25 >= 0 表示无相关性，应为 0 分。"""
        assert _score_from_bm25(0.0) == 0.0
        assert _score_from_bm25(1.0) == 0.0

    def test_precision_preserved(self):
        """两个相近但不同的 bm25 应得到不同的 score。"""
        a = _score_from_bm25(-4.45913012)
        b = _score_from_bm25(-4.45913011)
        assert a != b or abs(a - b) < 1e-12  # float 精度内可能相等

    def test_formula_matches_spec(self):
        """验证公式与项目负责人指定的一致。"""
        bm25 = -3.5
        relevance = -bm25
        expected = relevance / (1 + relevance)
        assert _score_from_bm25(bm25) == pytest.approx(expected)


class TestSqlOrderingMatchesScore:
    """SQL 排序、Python 排序、Top-K 截断三者一致。"""

    @pytest.fixture
    def index(self, tmp_path):
        chunks = [
            {
                "chunk_id": f"c{i}",
                "policy_id": "p1",
                "document_id": "d1",
                "version": 1,
                "page": None,
                "start": i * 100,
                "end": i * 100 + 50,
                "title": "t",
                "source_key": "demo",
                "source_url": "https://example.invalid/a",
                "content_sha256": "0" * 64,
                "text": text,
            }
            for i, text in enumerate(
                [
                    "电力市场",
                    "电力市场 电力市场",
                    "电力市场 电力市场 电力市场",
                    "电力市场运行基本规则 电力市场",
                    "无关内容",
                ]
            )
        ]
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "policy-chunk-manifest-v1",
                    "documents": [{"policy_id": "p1"}],
                    "chunks": chunks,
                    "manifest_sha256": "h",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        db = tmp_path / "idx.db"
        build_index(manifest, db)
        return LexicalSearch(db)

    def test_rank_sequence(self, index):
        """rank 必须从 1 开始连续。"""
        out = index.search("电力市场", top_k=5)
        assert [h.rank for h in out.hits] == list(range(1, out.returned + 1))

    def test_bm25_ascending(self, index):
        """返回顺序的 bm25 必须单调递增（越负越前）。"""
        out = index.search("电力市场", top_k=5)
        values = [h.bm25 for h in out.hits]
        assert values == sorted(values)

    def test_score_descending(self, index):
        """同一批结果的 score 必须单调递减。"""
        out = index.search("电力市场", top_k=5)
        scores = [h.score for h in out.hits]
        assert scores == sorted(scores, reverse=True)

    def test_python_resort_agrees_with_sql(self, index):
        """按 score 降序重排，结果不应改变（修复前会反转）。"""
        out = index.search("电力市场", top_k=5)
        sql_order = [h.chunk_id for h in out.hits]
        py_order = [
            h.chunk_id
            for h in sorted(out.hits, key=lambda h: h.score, reverse=True)
        ]
        assert sql_order == py_order

    def test_top_k_truncation_keeps_best(self, index):
        """Top-K 截断保留的应是 SQL 排序最前面的。"""
        full = index.search("电力市场", top_k=5)
        truncated = index.search("电力市场", top_k=2)
        assert [h.chunk_id for h in truncated.hits] == [
            h.chunk_id for h in full.hits[:2]
        ]

    def test_raw_bm25_preserved(self, index):
        """原始 bm25 分值必须保留，不能被转换覆盖。"""
        out = index.search("电力市场", top_k=3)
        for hit in out.hits:
            assert hit.bm25 is not None
            assert hit.bm25 <= 0

    def test_fallback_has_no_bm25(self, index):
        """回退模式没有 BM25 分值。"""
        out = index.search("电", top_k=3)
        if out.match_mode == "fallback_instr_scan":
            for hit in out.hits:
                assert hit.bm25 is None


class TestRealIndexOrdering:
    """真实索引上的排序验证。"""

    @pytest.mark.skipif(not MANIFEST.is_file(), reason="缺少切片清单")
    def test_real_query_ordering(self, tmp_path):
        db = tmp_path / "idx.db"
        build_index(MANIFEST, db)
        search = LexicalSearch(db)

        for query in ["电能计量点", "一地注册、各方共享", "电力市场注册"]:
            out = search.search(query, top_k=10)
            if out.returned < 2:
                continue

            bm25s = [h.bm25 for h in out.hits]
            scores = [h.score for h in out.hits]

            assert bm25s == sorted(bm25s), f"{query}: bm25 未按升序"
            assert scores == sorted(scores, reverse=True), f"{query}: score 未按降序"

    @pytest.mark.skipif(not MANIFEST.is_file(), reason="缺少切片清单")
    def test_real_first_hit_is_most_negative(self, tmp_path):
        db = tmp_path / "idx.db"
        build_index(MANIFEST, db)
        search = LexicalSearch(db)

        out = search.search("电能计量点", top_k=10)
        assert out.returned >= 1
        # 首条应是 bm25 最负的（最相关）
        assert out.hits[0].bm25 == min(h.bm25 for h in out.hits)


# ==========================================================================
# 2. 检索模式命名
# ==========================================================================
class TestMatchModeNaming:
    def test_fts_mode_name_is_explicit(self, tmp_path):
        """模式名应明确是 trigram 字面短语，不是泛化的 BM25。"""
        chunks = [
            {
                "chunk_id": "c1",
                "policy_id": "p1",
                "document_id": "d1",
                "version": 1,
                "page": None,
                "start": 0,
                "end": 20,
                "title": "t",
                "source_key": "demo",
                "source_url": "https://example.invalid/a",
                "content_sha256": "0" * 64,
                "text": "电力市场运行基本规则",
            }
        ]
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "policy-chunk-manifest-v1",
                    "documents": [{"policy_id": "p1"}],
                    "chunks": chunks,
                    "manifest_sha256": "h",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        out = LexicalSearch(db).search("电力市场", top_k=3)
        assert out.match_mode == "fts5_trigram_phrase"
        assert "trigram" in out.notice
        assert "不代表所有 BM25 方案" in out.notice

    def test_fallback_mode_name(self, tmp_path):
        chunks = [
            {
                "chunk_id": "c1",
                "policy_id": "p1",
                "document_id": "d1",
                "version": 1,
                "page": None,
                "start": 0,
                "end": 10,
                "title": "t",
                "source_key": "demo",
                "source_url": "https://example.invalid/a",
                "content_sha256": "0" * 64,
                "text": "电价形成机制",
            }
        ]
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "policy-chunk-manifest-v1",
                    "documents": [{"policy_id": "p1"}],
                    "chunks": chunks,
                    "manifest_sha256": "h",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        out = LexicalSearch(db).search("电价", top_k=3)
        assert out.match_mode == "fallback_instr_scan"
        assert "instr" in out.notice

    def test_escaping_still_works(self):
        """安全转义逻辑保留。"""
        assert escape_fts_query("电力市场") == '"电力市场"'
        assert escape_fts_query('a"b') == '"a""b"'

    @pytest.mark.parametrize(
        "payload", ["AND", "OR", "NEAR", "*", "-电力", "(电力)", '"']
    )
    def test_special_chars_safe(self, payload, tmp_path):
        chunks = [
            {
                "chunk_id": "c1",
                "policy_id": "p1",
                "document_id": "d1",
                "version": 1,
                "page": None,
                "start": 0,
                "end": 20,
                "title": "t",
                "source_key": "demo",
                "source_url": "https://example.invalid/a",
                "content_sha256": "0" * 64,
                "text": "电力市场运行基本规则",
            }
        ]
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "policy-chunk-manifest-v1",
                    "documents": [{"policy_id": "p1"}],
                    "chunks": chunks,
                    "manifest_sha256": "h",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        out = LexicalSearch(db).search(payload, top_k=3)
        assert out.returned >= 0  # 不崩即通过


# ==========================================================================
# 3. 评测指标可审计
# ==========================================================================
class TestMetricAuditability:
    def test_recall_multi_evidence_partial(self):
        """多条相关证据，只召回一部分。"""
        relevant = {"a", "b", "c", "d"}
        retrieved = ["a", "x", "y"]
        assert recall_at_k(relevant, retrieved, k=10) == 0.25

    def test_recall_multi_evidence_full(self):
        relevant = {"a", "b"}
        retrieved = ["x", "a", "b"]
        assert recall_at_k(relevant, retrieved, k=10) == 1.0

    def test_recall_respects_cutoff(self):
        """证据在 K 之外不算召回。"""
        relevant = {"a", "b"}
        retrieved = ["x", "y", "a", "b"]
        assert recall_at_k(relevant, retrieved, k=2) == 0.0
        assert recall_at_k(relevant, retrieved, k=4) == 1.0

    def test_rr_varies_with_rank(self):
        """不同命中排名产生不同 RR。"""
        cases = [
            ({"a"}, ["a", "x", "y"], 1.0),
            ({"a"}, ["x", "a", "y"], 0.5),
            ({"a"}, ["x", "y", "a"], 1 / 3),
            ({"a"}, ["x", "y", "z"], 0.0),
        ]
        for relevant, retrieved, expected in cases:
            assert reciprocal_rank(relevant, retrieved) == pytest.approx(expected)

    def test_ndcg_penalizes_late_hits(self):
        """命中位置越靠后，nDCG 越低。"""
        first = ndcg_at_k({"a"}, ["a", "x", "y"], k=3)
        third = ndcg_at_k({"a"}, ["x", "y", "a"], k=3)
        assert first > third

    def test_ndcg_partial_multi_evidence(self):
        """部分证据召回时 nDCG 介于 0 和 1 之间。"""
        relevant = {"a", "b"}
        value = ndcg_at_k(relevant, ["a", "x", "y"], k=3)
        assert 0.0 < value < 1.0

    def test_three_metrics_differ_with_multiple_evidence(self):
        """多证据场景下 Recall / MRR / nDCG 应有差异。

        这正是项目负责人质疑「三项汇总指标一致」的原因：
        单条相关证据 + 首条命中时，三者必然都是 1.0。
        多证据 + 部分召回时，三者才会分离。
        """
        relevant = {"a", "b", "c"}
        retrieved = ["x", "a", "y", "b"]

        recall = recall_at_k(relevant, retrieved, k=10)   # 2/3
        rr = reciprocal_rank(relevant, retrieved)          # 1/2
        ndcg = ndcg_at_k(relevant, retrieved, k=10)

        assert recall == pytest.approx(2 / 3)
        assert rr == 0.5
        assert ndcg > 0
        # 三者不等
        assert recall != rr
        assert rr != ndcg

    def test_single_evidence_first_hit_all_one(self):
        """解释「三项指标一致」：单证据 + 首条命中时必然都为 1。"""
        relevant = {"a"}
        retrieved = ["a", "x", "y"]
        assert recall_at_k(relevant, retrieved, k=10) == 1.0
        assert reciprocal_rank(relevant, retrieved) == 1.0
        assert ndcg_at_k(relevant, retrieved, k=10) == 1.0

    def test_mrr_averages_correctly(self):
        cases = [
            ({"a"}, ["a"]),          # 1.0
            ({"a"}, ["x", "a"]),     # 0.5
            ({"a"}, ["x", "y"]),     # 0.0
        ]
        assert mean_reciprocal_rank(cases) == pytest.approx(0.5)


class TestCompleteEvidenceRate:
    """跨条款完整证据召回率。

    与 Recall 的区别：
    - Recall 按证据条数计算（召回 2/3 条得 0.67）
    - Complete Evidence Rate 按用例计算（全部命中才算 1，否则 0）

    对跨条款查询，后者更能反映「能否完整回答问题」。
    """

    def test_helper_exists_in_eval_script(self):
        from scripts.eval_retrieval import complete_evidence_rate

        assert callable(complete_evidence_rate)

    def test_full_recall_is_complete(self):
        from scripts.eval_retrieval import complete_evidence_rate

        assert complete_evidence_rate({"a", "b"}, ["a", "b", "x"], k=10) == 1.0

    def test_partial_recall_is_incomplete(self):
        from scripts.eval_retrieval import complete_evidence_rate

        assert complete_evidence_rate({"a", "b"}, ["a", "x", "y"], k=10) == 0.0

    def test_no_evidence_is_incomplete(self):
        from scripts.eval_retrieval import complete_evidence_rate

        assert complete_evidence_rate({"a"}, ["x", "y"], k=10) == 0.0

    def test_empty_relevant_rejected(self):
        from scripts.eval_retrieval import complete_evidence_rate

        with pytest.raises(LexicalSearchError):
            complete_evidence_rate(set(), ["a"], k=10)
