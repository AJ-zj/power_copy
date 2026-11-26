"""检索评测脚本测试。

验证：相关集解析、指标计算、评测集格式、空结果用例。
"""

import json
from pathlib import Path

import pytest

from powercopy.rag.lexical import (
    LexicalSearch,
    build_index,
)
from scripts.eval_retrieval import evaluate, get_anchors, resolve_relevant

ROOT = Path(__file__).resolve().parents[1]
EVAL_V2 = ROOT / "evaluations" / "retrieval-eval-v2.json"
MANIFEST = ROOT / "storage" / "rag" / "chunks.json"


def _chunk(cid, text):
    return {
        "chunk_id": cid,
        "policy_id": "p1",
        "document_id": "d1",
        "version": 1,
        "page": None,
        "start": 0,
        "end": len(text),
        "title": "t",
        "source_key": "demo",
        "source_url": "https://example.invalid/a",
        "content_sha256": "0" * 64,
        "text": text,
    }


# ==========================================================================
# 1. 相关集解析
# ==========================================================================
class TestResolveRelevant:
    def test_anchor_hit(self):
        chunks = [_chunk("c1", "电价形成机制"), _chunk("c2", "注册流程")]
        assert resolve_relevant(["电价"], chunks) == {"c1"}

    def test_multiple_anchors(self):
        chunks = [_chunk("c1", "电价形成机制"), _chunk("c2", "注册流程")]
        assert resolve_relevant(["电价", "注册"], chunks) == {"c1", "c2"}

    def test_no_anchor_returns_empty(self):
        chunks = [_chunk("c1", "电价")]
        assert resolve_relevant([], chunks) == set()

    def test_anchor_not_found(self):
        chunks = [_chunk("c1", "电价")]
        assert resolve_relevant(["不存在"], chunks) == set()

    def test_anchor_is_literal_not_fuzzy(self):
        """锚点是字面匹配，'电价' 不应匹配 '电费'。"""
        chunks = [_chunk("c1", "电费结算")]
        assert resolve_relevant(["电价"], chunks) == set()

    def test_get_anchors_prefers_anchors(self):
        case = {"anchors": ["a"], "relevant_keywords": ["b"]}
        assert get_anchors(case) == ["a"]

    def test_get_anchors_falls_back_to_keywords(self):
        """兼容 v1 的 relevant_keywords 字段。"""
        assert get_anchors({"relevant_keywords": ["b"]}) == ["b"]

    def test_get_anchors_empty(self):
        assert get_anchors({}) == []


# ==========================================================================
# 2. 评测流程
# ==========================================================================
class TestEvaluate:
    @pytest.fixture
    def index(self, tmp_path):
        chunks = [
            _chunk("c1", "电力市场运行基本规则"),
            _chunk("c2", "电力市场注册基本规则"),
            _chunk("c3", "电能计量点位于产权分界点"),
        ]
        manifest = tmp_path / "chunks.json"
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
        return LexicalSearch(db), chunks

    def test_perfect_case(self, index):
        search, chunks = index
        cases = [
            {
                "id": "t1",
                "type": "exact_keyword",
                "query": "电能计量点",
                "anchors": ["电能计量点"],
                "expect_empty": False,
            }
        ]
        report = evaluate(search, cases, chunks, top_k=5)

        assert report["summary"]["cases_scored"] == 1
        assert report["cases"][0]["recall"] == 1.0
        assert report["cases"][0]["rr"] == 1.0

    def test_empty_expectation_pass(self, index):
        search, chunks = index
        cases = [
            {
                "id": "e1",
                "type": "insufficient_evidence",
                "query": "完全不存在的内容",
                "anchors": [],
                "expect_empty": True,
            }
        ]
        report = evaluate(search, cases, chunks, top_k=5)
        assert report["summary"]["empty_pass"] == 1
        assert report["cases"][0]["empty_ok"] is True

    def test_empty_expectation_fail(self, index):
        search, chunks = index
        cases = [
            {
                "id": "e2",
                "type": "insufficient_evidence",
                "query": "电能计量点",
                "anchors": [],
                "expect_empty": True,
            }
        ]
        report = evaluate(search, cases, chunks, top_k=5)
        assert report["summary"]["empty_pass"] == 0
        assert report["cases"][0]["empty_ok"] is False

    def test_missing_anchor_warns(self, index):
        search, chunks = index
        cases = [
            {
                "id": "w1",
                "type": "semantic",
                "query": "电力市场",
                "anchors": ["绝不存在的锚点"],
                "expect_empty": False,
            }
        ]
        report = evaluate(search, cases, chunks, top_k=5)
        assert report["cases"][0].get("warning")
        assert report["summary"]["cases_scored"] == 0

    def test_type_breakdown(self, index):
        search, chunks = index
        cases = [
            {
                "id": "a",
                "type": "exact_keyword",
                "query": "电能计量点",
                "anchors": ["电能计量点"],
                "expect_empty": False,
            },
            {
                "id": "b",
                "type": "semantic",
                "query": "电力市场",
                "anchors": ["电力市场运行基本规则"],
                "expect_empty": False,
            },
        ]
        report = evaluate(search, cases, chunks, top_k=5)
        assert "exact_keyword" in report["summary"]["by_type"]
        assert "semantic" in report["summary"]["by_type"]

    def test_summary_has_all_metrics(self, index):
        search, chunks = index
        cases = [
            {
                "id": "m",
                "type": "exact_keyword",
                "query": "电能计量点",
                "anchors": ["电能计量点"],
                "expect_empty": False,
            }
        ]
        summary = evaluate(search, cases, chunks, top_k=5)["summary"]
        assert "recall_at_k" in summary
        assert "mrr" in summary
        assert "ndcg_at_k" in summary
        assert "hit_rate" in summary


# ==========================================================================
# 3. 真实评测集
# ==========================================================================
@pytest.mark.skipif(
    not EVAL_V2.is_file() or not MANIFEST.is_file(),
    reason="缺少评测集或切片清单",
)
class TestRealEvalSet:
    @pytest.fixture(scope="class")
    @classmethod
    def eval_set(cls):
        return json.loads(EVAL_V2.read_text(encoding="utf-8"))

    def test_has_30_cases(self, eval_set):
        assert len(eval_set["cases"]) == 30

    def test_declares_frozen(self, eval_set):
        assert eval_set["frozen"] is True

    def test_covers_six_types(self, eval_set):
        types = {c["type"] for c in eval_set["cases"]}
        assert types == {
            "exact_keyword",
            "short_word",
            "semantic",
            "cross_clause",
            "policy_relation",
            "insufficient_evidence",
        }

    def test_ids_unique(self, eval_set):
        ids = [c["id"] for c in eval_set["cases"]]
        assert len(set(ids)) == len(ids)

    def test_anchors_use_literal_text(self, eval_set):
        """锚点必须是原文里真实存在的字符串。"""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        corpus = "\n".join(c["text"] for c in manifest["chunks"])

        missing = []
        for case in eval_set["cases"]:
            for anchor in get_anchors(case):
                if anchor not in corpus:
                    missing.append((case["id"], anchor))

        assert not missing, f"锚点在语料中不存在: {missing}"

    def test_annotations_not_too_broad(self, eval_set):
        """相关集过宽会让 Recall@K 数学上限过低，指标失真。"""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        chunks = manifest["chunks"]
        total = len(chunks)

        too_broad = []
        for case in eval_set["cases"]:
            anchors = get_anchors(case)
            if not anchors:
                continue
            n = len(resolve_relevant(anchors, chunks))
            if n / total > 0.5:
                too_broad.append((case["id"], n, total))

        assert not too_broad, f"相关集过宽: {too_broad}"

    def test_empty_cases_have_no_anchors(self, eval_set):
        for case in eval_set["cases"]:
            if case["type"] == "insufficient_evidence":
                assert get_anchors(case) == []
                assert case["expect_empty"] is True

    def test_baseline_metrics_computable(self, eval_set):
        """跑一次真实评测，确认指标可计算且在合法范围。"""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "idx.db"
            build_index(MANIFEST, db)
            search = LexicalSearch(db)
            report = evaluate(
                search, eval_set["cases"], manifest["chunks"], top_k=10
            )

        summary = report["summary"]
        assert summary["cases_total"] == 30
        assert 0.0 <= summary["recall_at_k"] <= 1.0
        assert 0.0 <= summary["mrr"] <= 1.0
        assert 0.0 <= summary["ndcg_at_k"] <= 1.0
        assert summary["empty_pass"] == summary["empty_cases"] == 4
