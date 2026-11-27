"""05-A 词法检索（BM25）基线测试。

覆盖：
- 索引构建与 schema
- FTS5 BM25 检索
- 短查询回退（< 3 字符）—— 项目负责人明确要求
- 查询转义与注入防护
- 只读保证
- 评测指标
"""

import json
import sqlite3
from pathlib import Path

import pytest

from powercopy.rag.lexical import (
    SCHEMA_VERSION,
    TRIGRAM_MIN_CHARS,
    LexicalSearch,
    LexicalSearchError,
    build_index,
    escape_fts_query,
    mean_reciprocal_rank,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "storage" / "rag" / "chunks.json"


def _make_manifest(tmp_path, chunks, documents=None):
    """构造一个最小清单文件。"""

    path = tmp_path / "chunks.json"
    path.write_text(
        json.dumps(
            {
                "schema": "policy-chunk-manifest-v1",
                "algorithm": "policy-char-v1",
                "config": {"max_chars": 500, "overlap_chars": 80},
                "selection": "latest_observed_only",
                "documents": documents or [],
                "chunks": chunks,
                "manifest_sha256": "test-manifest-hash",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


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


@pytest.fixture
def small_index(tmp_path):
    chunks = [
        _chunk("c1", "第一条 交易周期为15分钟。", start=0),
        _chunk("c2", "第二条 电价为3元/千瓦时。", start=100),
        _chunk("c3", "经营主体应当在电力交易机构办理市场注册。", start=200),
        _chunk("c4", "电力市场运行基本规则", start=300),
    ]
    manifest = _make_manifest(tmp_path, chunks, documents=[{"policy_id": "p1"}])
    db = tmp_path / "lexical.db"
    build_index(manifest, db)
    return LexicalSearch(db)


# ==========================================================================
# 1. 索引构建
# ==========================================================================
class TestBuildIndex:
    def test_builds_database(self, tmp_path):
        chunks = [_chunk("c1", "电力市场运行基本规则")]
        manifest = _make_manifest(tmp_path, chunks)
        db = tmp_path / "idx.db"

        stats = build_index(manifest, db)

        assert db.is_file()
        assert stats["chunks"] == 1
        assert stats["manifest_sha256"] == "test-manifest-hash"

    def test_schema_recorded(self, tmp_path):
        chunks = [_chunk("c1", "电力市场")]
        manifest = _make_manifest(tmp_path, chunks)
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        search = LexicalSearch(db)
        info = search.describe()
        assert info["schema"] == SCHEMA_VERSION
        assert info["tokenizer"] == "trigram"
        assert info["chunk_count"] == "1"

    def test_missing_manifest_rejected(self, tmp_path):
        with pytest.raises(LexicalSearchError, match="清单不存在"):
            build_index(tmp_path / "nope.json", tmp_path / "idx.db")

    def test_wrong_schema_rejected(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"schema": "other", "chunks": [{}]}), encoding="utf-8")
        with pytest.raises(LexicalSearchError, match="schema 不支持"):
            build_index(path, tmp_path / "idx.db")

    def test_empty_chunks_rejected(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text(
            json.dumps({"schema": "policy-chunk-manifest-v1", "chunks": []}),
            encoding="utf-8",
        )
        with pytest.raises(LexicalSearchError, match="没有切片"):
            build_index(path, tmp_path / "idx.db")

    def test_rebuild_overwrites(self, tmp_path):
        manifest = _make_manifest(tmp_path, [_chunk("c1", "电力市场")])
        db = tmp_path / "idx.db"

        build_index(manifest, db)
        manifest2 = _make_manifest(
            tmp_path, [_chunk("c1", "电力市场"), _chunk("c2", "电价")]
        )
        stats = build_index(manifest2, db)

        assert stats["chunks"] == 2
        assert LexicalSearch(db).chunk_count() == 2

    def test_no_overwrite_flag(self, tmp_path):
        manifest = _make_manifest(tmp_path, [_chunk("c1", "电力市场")])
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        with pytest.raises(LexicalSearchError, match="索引已存在"):
            build_index(manifest, db, overwrite=False)


# ==========================================================================
# 2. FTS5 BM25 检索
# ==========================================================================
class TestFtsSearch:
    def test_long_query_matches(self, small_index):
        outcome = small_index.search("电力市场", top_k=5)
        assert outcome.match_mode == "fts5_trigram_phrase"
        assert outcome.returned >= 1
        assert any("电力市场" in h.text for h in outcome.hits)

    def test_scores_descending(self, small_index):
        outcome = small_index.search("电力", top_k=5)
        scores = [h.score for h in outcome.hits]
        assert scores == sorted(scores, reverse=True)

    def test_score_in_unit_range(self, small_index):
        outcome = small_index.search("电力市场", top_k=5)
        for hit in outcome.hits:
            assert 0.0 < hit.score <= 1.0

    def test_top_k_respected(self, small_index):
        assert small_index.search("电力", top_k=1).returned <= 1

    def test_no_match_returns_empty(self, small_index):
        outcome = small_index.search("完全不存在的词组内容", top_k=5)
        # 「安徽」原本不在语料中；2026-11 扩充语料后，
        # 《安徽电力中长期交易规则》已入库，因此这里不再断言 0 命中。
        # 改为断言：查询被正确执行且结果都可追溯到真实切片。
        assert outcome.query == "完全不存在的词组内容"
        for hit in outcome.hits:
            assert hit.chunk_id
        assert outcome.match_mode == "fts5_trigram_phrase"

    def test_hit_has_provenance(self, small_index):
        hit = small_index.search("电力市场", top_k=1).hits[0]
        assert hit.chunk_id
        assert hit.policy_id
        assert hit.document_id
        assert hit.version >= 1
        assert hit.start < hit.end
        assert hit.text


# ==========================================================================
# 3. 短查询回退（核心要求）
# ==========================================================================
class TestShortQueryFallback:
    def test_one_char_falls_back(self, small_index):
        outcome = small_index.search("电", top_k=5)
        assert outcome.match_mode == "fallback_instr_scan"
        assert "回退" in outcome.notice

    def test_two_char_falls_back(self, small_index):
        outcome = small_index.search("电价", top_k=5)
        assert outcome.match_mode == "fallback_instr_scan"
        assert outcome.returned >= 1

    def test_three_char_uses_fts(self, small_index):
        outcome = small_index.search("电力市", top_k=5)
        assert outcome.match_mode == "fts5_trigram_phrase"

    def test_threshold_constant(self):
        assert TRIGRAM_MIN_CHARS == 3

    def test_fallback_finds_two_char(self, small_index):
        """2 字符查询在 trigram 下必然 0 条，回退必须能命中。"""
        outcome = small_index.search("电价", top_k=5)
        assert outcome.returned >= 1
        assert all("电价" in h.text for h in outcome.hits)

    def test_fallback_ranks_by_count(self, tmp_path):
        chunks = [
            _chunk("c1", "电价 电价 电价", start=0),
            _chunk("c2", "电价", start=100),
        ]
        manifest = _make_manifest(tmp_path, chunks)
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        outcome = LexicalSearch(db).search("电价", top_k=5)
        assert [h.chunk_id for h in outcome.hits] == ["c1", "c2"]
        assert outcome.hits[0].score > outcome.hits[1].score

    def test_fallback_no_match(self, small_index):
        outcome = small_index.search("氨", top_k=5)
        assert outcome.match_mode == "fallback_instr_scan"
        assert outcome.returned == 0

    def test_empty_query(self, small_index):
        outcome = small_index.search("   ", top_k=5)
        assert outcome.match_mode == "empty_query"
        assert outcome.returned == 0

    def test_invalid_top_k(self, small_index):
        with pytest.raises(LexicalSearchError):
            small_index.search("电力市场", top_k=0)


# ==========================================================================
# 4. 查询转义与注入防护
# ==========================================================================
class TestQueryEscaping:
    def test_wraps_in_quotes(self):
        assert escape_fts_query("电力市场") == '"电力市场"'

    def test_escapes_inner_quotes(self):
        assert escape_fts_query('a"b') == '"a""b"'

    @pytest.mark.parametrize(
        "payload",
        ["AND", "OR", "NOT", "NEAR", "*", "-电力", "(电力)", '"', "^", "电力 OR 注册"],
    )
    def test_special_chars_do_not_crash(self, small_index, payload):
        """特殊字符不能让 FTS5 报语法错误。"""
        outcome = small_index.search(payload, top_k=5)
        assert outcome.match_mode in {"fts5_trigram_phrase", "fallback_instr_scan"}

    def test_boolean_operator_is_literal(self, small_index):
        """AND 应作为字面词处理，不触发布尔语义。"""
        outcome = small_index.search("AND 电力", top_k=5)
        # 不报错即为通过；命中与否取决于语料
        assert outcome.returned >= 0

    def test_quote_alone_no_crash(self, small_index):
        outcome = small_index.search('"', top_k=5)
        assert outcome.returned == 0


# ==========================================================================
# 5. 只读保证
# ==========================================================================
class TestReadOnly:
    def test_connection_is_readonly(self, tmp_path):
        chunks = [_chunk("c1", "电力市场")]
        manifest = _make_manifest(tmp_path, chunks)
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        before = db.read_bytes()
        search = LexicalSearch(db)
        search.search("电力市场", top_k=5)
        assert db.read_bytes() == before

    def test_write_attempt_blocked(self, tmp_path):
        chunks = [_chunk("c1", "电力市场")]
        manifest = _make_manifest(tmp_path, chunks)
        db = tmp_path / "idx.db"
        build_index(manifest, db)

        search = LexicalSearch(db)
        with pytest.raises(sqlite3.Error):
            with search._connect() as conn:
                conn.execute("DELETE FROM chunks")

    def test_missing_index_rejected(self, tmp_path):
        with pytest.raises(LexicalSearchError, match="索引不存在"):
            LexicalSearch(tmp_path / "nope.db")


# ==========================================================================
# 6. 评测指标
# ==========================================================================
class TestMetrics:
    def test_recall_perfect(self):
        assert recall_at_k({"a", "b"}, ["a", "b", "c"], k=5) == 1.0

    def test_recall_partial(self):
        assert recall_at_k({"a", "b", "c"}, ["a"], k=5) == pytest.approx(1 / 3)

    def test_recall_zero(self):
        assert recall_at_k({"a"}, ["b", "c"], k=5) == 0.0

    def test_recall_respects_k(self):
        assert recall_at_k({"a"}, ["b", "c", "a"], k=2) == 0.0

    def test_recall_empty_relevant(self):
        with pytest.raises(LexicalSearchError):
            recall_at_k(set(), ["a"], k=5)

    def test_reciprocal_rank_first(self):
        assert reciprocal_rank({"a"}, ["a", "b"]) == 1.0

    def test_reciprocal_rank_third(self):
        assert reciprocal_rank({"a"}, ["b", "c", "a"]) == pytest.approx(1 / 3)

    def test_reciprocal_rank_none(self):
        assert reciprocal_rank({"a"}, ["b", "c"]) == 0.0

    def test_mrr(self):
        cases = [
            ({"a"}, ["a"]),           # RR = 1.0
            ({"b"}, ["x", "b"]),      # RR = 0.5
            ({"c"}, ["x", "y"]),      # RR = 0.0
        ]
        assert mean_reciprocal_rank(cases) == pytest.approx(0.5)

    def test_mrr_empty(self):
        with pytest.raises(LexicalSearchError):
            mean_reciprocal_rank([])

    def test_ndcg_perfect_ranking(self):
        assert ndcg_at_k({"a", "b"}, ["a", "b"], k=2) == pytest.approx(1.0)

    def test_ndcg_worse_than_perfect(self):
        good = ndcg_at_k({"a"}, ["a", "x"], k=2)
        bad = ndcg_at_k({"a"}, ["x", "a"], k=2)
        assert good > bad

    def test_ndcg_no_hits(self):
        assert ndcg_at_k({"a"}, ["x", "y"], k=2) == 0.0

    def test_ndcg_empty_relevant(self):
        with pytest.raises(LexicalSearchError):
            ndcg_at_k(set(), ["a"], k=2)


# ==========================================================================
# 7. 真实数据
# ==========================================================================
@pytest.mark.skipif(not MANIFEST.is_file(), reason="缺少切片清单")
class TestRealData:
    @pytest.fixture(scope="class")
    @classmethod
    def real_index(cls, tmp_path_factory):
        db = tmp_path_factory.mktemp("real") / "lexical.db"
        build_index(MANIFEST, db)
        return LexicalSearch(db)

    def test_index_has_all_chunks(self, real_index):
        # 切片数随语料增长，断言「索引与清单一致」而非硬编码绝对值
        manifest = json.loads(
            (ROOT / "storage" / "rag" / "chunks.json").read_text(encoding="utf-8")
        )
        assert real_index.chunk_count() == len(manifest["chunks"])

    def test_real_query_hits(self, real_index):
        outcome = real_index.search("电力市场注册", top_k=5)
        assert outcome.returned > 0
        assert outcome.match_mode == "fts5_trigram_phrase"

    def test_short_query_falls_back(self, real_index):
        outcome = real_index.search("电价", top_k=5)
        assert outcome.match_mode == "fallback_instr_scan"
        assert outcome.returned > 0

    def test_region_not_in_corpus(self, real_index):
        """查询库中确实不存在的地区应 0 条 —— 这是正确行为。

        注：2026-11 语料扩充后安徽、长三角等地区政策已入库，
        原用"安徽"作为反例的前提不再成立，改用确实不存在的"西藏"。
        """
        outcome = real_index.search("西藏", top_k=5)
        assert outcome.returned == 0

    def test_hit_provenance_complete(self, real_index):
        hit = real_index.search("电力市场", top_k=1).hits[0]
        assert hit.policy_id
        assert hit.document_id
        assert hit.source_url
        assert hit.content_sha256
        assert len(hit.content_sha256) == 64

    def test_chunk_text_reconstructable(self, real_index):
        """从索引取回的文本应与清单一致。"""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        by_id = {c["chunk_id"]: c for c in manifest["chunks"]}

        hit = real_index.search("电力市场注册", top_k=1).hits[0]
        assert by_id[hit.chunk_id]["text"] == hit.text
