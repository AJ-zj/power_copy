"""MVP-01 检索集成专项测试。

覆盖项目负责人验收清单：
- 双索引正常融合
- 指纹缺失及版本不一致
- 模型 commit 不一致
- 缓存失效
- 旧索引格式拒绝
- 完全离线模型加载
- 原政策数据库保持只读
- 工具正常成功、空结果、异常及降级
- Agent Runtime 完整工具调用链路
- 原有政策查询能力无回归
- 前端工具清单能正确反映实际运行时工具
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from powercopy.rag.dense import (
    CACHE_SCHEMA,
    CacheNamespace,
    DenseSearch,
    DenseSearchError,
    EmbeddingCache,
    build_dense_index,
)
from powercopy.rag.embedding import BGEEmbedder, EmbeddingError
from powercopy.rag.hybrid import HybridError, HybridOutcome, HybridRetriever
from powercopy.tools.builtin.policy_tools import build_live_registry
from powercopy.tools.builtin.retrieval import (
    DEFAULT_TOP_K,
    MAX_QUERY_CHARS,
    MAX_TEXT_CHARS,
    MAX_TOP_K,
    PolicyEvidenceService,
    RetrievalPaths,
    SearchPolicyEvidenceInput,
    build_evidence_tool,
)
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.result import BusinessStatus, ExecutionStatus

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "policies_versioned.db"
RAG_DIR = ROOT / "storage" / "rag"
MANIFEST = RAG_DIR / "chunks.json"
LEXICAL_DB = RAG_DIR / "lexical.db"
DENSE_NPZ = RAG_DIR / "dense.npz"

HAS_INDEX = MANIFEST.is_file() and LEXICAL_DB.is_file() and DENSE_NPZ.is_file()

needs_index = pytest.mark.skipif(
    not HAS_INDEX, reason="缺少检索索引，先运行 build_dense_index.py 与 eval_retrieval.py"
)
needs_db = pytest.mark.skipif(not DB.is_file(), reason="缺少真实政策库")


def _model_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


needs_model = pytest.mark.skipif(
    not _model_available(), reason="未安装 torch/transformers"
)


# ---------------------------------------------------------------- 替身


class FakeHit:
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
    def __init__(self, hits=None, *, error=None, mode="fake_mode", manifest_sha="hash-a"):
        self._hits = hits or []
        self._error = error
        self._mode = mode
        self._manifest_sha = manifest_sha

    def search(self, query, *, top_k=10):
        if self._error is not None:
            raise self._error
        selected = self._hits[:top_k]
        return type(
            "Outcome",
            (),
            {
                "hits": selected,
                "match_mode": self._mode,
                "returned": len(selected),
            },
        )()

    def describe(self):
        return {"manifest_sha256": self._manifest_sha}

    @property
    def meta(self):
        return {"manifest_sha256": self._manifest_sha, "dim": 512}


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
    """模块级复用同一个编码器（模型加载约 30 秒）。"""
    return BGEEmbedder(local_files_only=True)


# ==========================================================================
# 1. Hybrid 索引一致性（项目负责人重点）
# ==========================================================================
class TestHybridIndexConsistency:
    def test_ok_when_same_fingerprint(self):
        hybrid = HybridRetriever(
            FakeRetriever(manifest_sha="same"),
            FakeRetriever(manifest_sha="same"),
        )
        info = hybrid.check_index_consistency()
        assert info["consistent"] is True
        assert info["status"] == "ok"

    def test_missing_fingerprint_never_consistent(self):
        """两路都在但任一指纹缺失 → 不得判定一致。"""
        for lex_sha, dense_sha in (("", "b"), ("a", ""), ("", "")):
            hybrid = HybridRetriever(
                FakeRetriever(manifest_sha=lex_sha),
                FakeRetriever(manifest_sha=dense_sha),
                allow_degrade=True,
            )
            info = hybrid.check_index_consistency()
            assert info["consistent"] is False, (lex_sha, dense_sha)
            assert info["status"] == "fingerprint_missing"

    def test_missing_fingerprint_raises_without_degrade(self):
        hybrid = HybridRetriever(
            FakeRetriever(manifest_sha=""),
            FakeRetriever(manifest_sha="b"),
            allow_degrade=False,
        )
        with pytest.raises(HybridError, match="指纹缺失"):
            hybrid.check_index_consistency()

    def test_version_conflict_rejected_even_with_degrade(self):
        """版本冲突时即使 allow_degrade=True 也禁止融合。"""
        hybrid = HybridRetriever(
            FakeRetriever([FakeHit("c1", 1, 0.9)], manifest_sha="a"),
            FakeRetriever([FakeHit("c2", 1, 0.9)], manifest_sha="b"),
            allow_degrade=True,
        )
        with pytest.raises(HybridError, match="版本冲突"):
            hybrid.search("q")

    def test_version_conflict_rejected_without_degrade(self):
        hybrid = HybridRetriever(
            FakeRetriever(manifest_sha="a"),
            FakeRetriever(manifest_sha="b"),
            allow_degrade=False,
        )
        with pytest.raises(HybridError, match="版本冲突"):
            hybrid.search("q")

    def test_single_path_unconfigured_distinguished(self):
        """单路未配置与「一致」是不同状态。"""
        hybrid = HybridRetriever(
            FakeRetriever([FakeHit("c1", 1, 0.9)], manifest_sha="a"), None
        )
        info = hybrid.check_index_consistency()
        assert info["status"] == "single_path_unconfigured"
        assert info["consistent"] is False
        assert info["degraded_path"] == "lexical"

    def test_single_path_search_degraded(self):
        hybrid = HybridRetriever(
            FakeRetriever([FakeHit("c1", 1, 0.9)], manifest_sha="a"), None
        )
        out = hybrid.search("q", final_top_k=5)
        assert out.returned == 1
        assert out.degraded == "single_path_unconfigured"
        assert out.consistency_status == "single_path_unconfigured"
        assert out.is_complete is False

    def test_fingerprint_missing_returns_empty(self):
        """指纹缺失 + 允许降级：返回空结果，不返回不可信候选。"""
        hybrid = HybridRetriever(
            FakeRetriever([FakeHit("c1", 1, 0.9)], manifest_sha=""),
            FakeRetriever([FakeHit("c2", 1, 0.9)], manifest_sha="b"),
            allow_degrade=True,
        )
        out = hybrid.search("q", final_top_k=5)
        assert out.returned == 0
        assert out.degraded == "fingerprint_missing"

    def test_four_states_are_distinct(self):
        """四种状态必须可区分。"""

        def status(lex, dns, degrade=True):
            h = HybridRetriever(
                FakeRetriever(FakeHit("c1", 1, 0.9) and [FakeHit("c1", 1, 0.9)], manifest_sha=lex)
                if lex is not None
                else None,
                FakeRetriever([FakeHit("c2", 1, 0.9)], manifest_sha=dns)
                if dns is not None
                else None,
                allow_degrade=degrade,
            )
            try:
                return h.check_index_consistency()["status"]
            except HybridError:
                return "version_conflict"

        assert status("a", "a") == "ok"
        assert status("a", None) == "single_path_unconfigured"
        assert status("", "b") == "fingerprint_missing"
        assert status("a", "b") == "version_conflict"


# ==========================================================================
# 2. Dense 模型一致性
# ==========================================================================
@needs_model
class TestDenseModelConsistency:
    def test_index_records_commit_and_tokenizer(self, tmp_path, embedder):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "d.npz"
        build_dense_index(manifest, index, embedder=embedder)

        search = DenseSearch(index, manifest_path=manifest, embedder=embedder)
        meta = search.meta
        assert meta["model_commit"]
        assert meta["tokenizer_fingerprint"]
        assert meta["schema"].endswith("-v2")

    def test_dim_alone_not_enough(self, tmp_path, embedder):
        """仅维度相同不足以认定模型兼容。"""
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "d.npz"
        build_dense_index(manifest, index, embedder=embedder)

        search = DenseSearch(index, manifest_path=manifest, embedder=embedder)
        assert search.meta["dim"] == 512

        # 篡改 commit 后必须被拒绝
        search._meta["model_commit"] = "deadbeef" * 5
        with pytest.raises(DenseSearchError, match="commit 不一致"):
            search.check_model_consistency(embedder)

    def test_tokenizer_mismatch_rejected(self, tmp_path, embedder):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "d.npz"
        build_dense_index(manifest, index, embedder=embedder)

        search = DenseSearch(index, manifest_path=manifest, embedder=embedder)
        search._meta["tokenizer_fingerprint"] = "f" * 64
        with pytest.raises(DenseSearchError, match="tokenizer 指纹不一致"):
            search.check_model_consistency(embedder)

    def test_pooling_mismatch_rejected(self, tmp_path, embedder):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "d.npz"
        build_dense_index(manifest, index, embedder=embedder)

        search = DenseSearch(index, manifest_path=manifest, embedder=embedder)
        search._meta["pooling"] = "mean_l2"
        with pytest.raises(DenseSearchError, match="池化方式不一致"):
            search.check_model_consistency(embedder)

    def test_missing_identity_rejected(self, tmp_path, embedder):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "d.npz"
        build_dense_index(manifest, index, embedder=embedder)

        search = DenseSearch(index, manifest_path=manifest, embedder=embedder)
        search._meta["model_commit"] = ""
        with pytest.raises(DenseSearchError, match="缺少模型身份字段"):
            search._require_model_identity()

    def test_search_revalidates_each_call(self, tmp_path, embedder):
        """每次检索都重新校验，防止中途换模型。"""
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "d.npz"
        build_dense_index(manifest, index, embedder=embedder)

        search = DenseSearch(index, manifest_path=manifest, embedder=embedder)
        search.search("电力市场", top_k=1)

        search._meta["model_commit"] = "x" * 40
        with pytest.raises(DenseSearchError, match="commit 不一致"):
            search.search("电力市场", top_k=1)


# ==========================================================================
# 3. NPZ 安全与旧格式拒绝
# ==========================================================================
class TestNpzSafety:
    def test_builder_writes_no_object_array(self, tmp_path):
        """chunk_ids 必须是定长 Unicode，不能是 object。"""
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "d.npz"

        # 用不依赖模型的方式直接检查：若没有模型就跳过
        if not _model_available():
            pytest.skip("未安装 torch")

        build_dense_index(manifest, index, embedder=BGEEmbedder())

        with np.load(index, allow_pickle=False) as data:
            assert data["chunk_ids"].dtype != object
            assert data["vectors"].dtype == np.float32

    def test_old_object_index_rejected(self, tmp_path):
        """旧格式（object 数组）必须拒绝，并给出重建指引。"""
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "old.npz"

        vectors = np.eye(1, 512, dtype=np.float32)
        np.savez_compressed(
            index,
            vectors=vectors,
            chunk_ids=np.asarray(["c1"], dtype=object),
            meta_json=np.asarray(json.dumps({"schema": "policy-dense-index-v1"})),
        )

        with pytest.raises(DenseSearchError, match="索引格式不受支持"):
            DenseSearch(index, manifest_path=manifest, verify_model=False)

    def test_missing_field_rejected(self, tmp_path):
        manifest = _write_manifest(tmp_path, [_chunk("c1", "电力市场")])
        index = tmp_path / "bad.npz"
        np.savez_compressed(
            index,
            vectors=np.eye(1, 512, dtype=np.float32),
        )
        with pytest.raises(DenseSearchError, match="缺少必需字段"):
            DenseSearch(index, manifest_path=manifest, verify_model=False)


# ==========================================================================
# 4. 离线加载
# ==========================================================================
@needs_model
class TestOfflineLoading:
    def test_local_files_only_default(self):
        embedder = BGEEmbedder()
        assert embedder.local_files_only is True

    def test_missing_model_raises_without_network(self):
        """模型名不存在时必须报错，不联网自动下载。"""
        embedder = BGEEmbedder(
            model_name="definitely-not-a-real-model-xyz-123",
            local_files_only=True,
        )
        with pytest.raises(EmbeddingError, match="本地模型不可用"):
            embedder._ensure_loaded()

    def test_offline_load_works_when_cached(self):
        embedder = BGEEmbedder()
        embedder._ensure_loaded()
        assert embedder.loaded is True
        assert embedder._resolved_commit
        assert embedder._tokenizer_fingerprint

    def test_fingerprint_has_all_fields(self):
        embedder = BGEEmbedder()
        embedder._ensure_loaded()
        fp = embedder.model_fingerprint
        for key in (
            "model",
            "commit",
            "tokenizer_fingerprint",
            "dim",
            "pooling",
            "normalized",
            "encode_mode_query",
            "encode_mode_document",
        ):
            assert key in fp
            assert fp[key] not in (None, "")


# ==========================================================================
# 5. 缓存失效
# ==========================================================================
class TestCacheInvalidation:
    def _ns(self, **over):
        base = {
            "model": "m",
            "commit": "c" * 40,
            "tokenizer_fingerprint": "t" * 64,
            "dim": 512,
            "pooling": "cls_l2",
            "normalized": True,
            "encode_mode": "document",
            "max_tokens": 512,
        }
        base.update(over)
        return CacheNamespace(**base)

    def test_incomplete_namespace_rejects(self, tmp_path):
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=self._ns())
        cache.put("x", [0.5] * 512)
        cache.save()

        naive = EmbeddingCache(path)
        assert len(naive) == 0
        assert "namespace_incomplete" in naive.rejected_reason

    @pytest.mark.parametrize(
        "field,value",
        [
            ("commit", "d" * 40),
            ("tokenizer_fingerprint", "z" * 64),
            ("pooling", "mean"),
            ("normalized", False),
            ("encode_mode", "query"),
            ("model", "other"),
        ],
    )
    def test_any_field_mismatch_invalidates(self, tmp_path, field, value):
        path = tmp_path / "c.json"
        cache = EmbeddingCache(path, namespace=self._ns())
        cache.put("x", [0.5] * 512)
        cache.save()

        reloaded = EmbeddingCache(path, namespace=self._ns(**{field: value}))
        assert len(reloaded) == 0
        assert field in reloaded.rejected_reason


# ==========================================================================
# 6. 检索工具
# ==========================================================================
class TestEvidenceTool:
    def test_tool_schema_bounds(self):
        schema = SearchPolicyEvidenceInput.model_json_schema()
        props = schema["properties"]
        assert props["query"]["maxLength"] == MAX_QUERY_CHARS
        assert props["top_k"]["maximum"] == MAX_TOP_K
        assert props["top_k"]["minimum"] == 1
        assert schema.get("additionalProperties") is False

    def test_rejects_extra_fields(self):
        """模型不能传入路径类参数。"""
        with pytest.raises(Exception):
            SearchPolicyEvidenceInput.model_validate(
                {"query": "x", "index_path": "/etc/passwd"}
            )

    def test_rejects_empty_query(self):
        with pytest.raises(Exception):
            SearchPolicyEvidenceInput.model_validate({"query": ""})

    def test_rejects_too_long_query(self):
        with pytest.raises(Exception):
            SearchPolicyEvidenceInput.model_validate(
                {"query": "x" * (MAX_QUERY_CHARS + 1)}
            )

    def test_rejects_top_k_over_limit(self):
        with pytest.raises(Exception):
            SearchPolicyEvidenceInput.model_validate(
                {"query": "x", "top_k": MAX_TOP_K + 1}
            )

    def test_paths_resolve_from_config(self):
        """路径来自配置，不是模型参数。"""
        paths = RetrievalPaths.resolve()
        assert paths.manifest.name == "chunks.json"
        assert paths.dense_npz.name == "dense.npz"

    def test_missing_index_returns_business_error(self, tmp_path):
        service = PolicyEvidenceService(
            RetrievalPaths.resolve(tmp_path / "nothing")
        )
        with pytest.raises(Exception) as exc:
            service.search("电力市场", 3)
        assert "RAG_INDEX_UNAVAILABLE" in str(exc.value)


@needs_index
@needs_db
@needs_model
class TestEvidenceToolLive:
    @pytest.fixture(scope="class")
    @classmethod
    def service(cls):
        return PolicyEvidenceService(
            RetrievalPaths.resolve(RAG_DIR), db_path=DB
        )

    def test_success(self, service):
        r = service.search("电力市场注册需要什么条件", 3)
        assert r.business_status is BusinessStatus.SUCCESS
        data = r.data
        assert data["candidate_count"] >= 1
        assert data["retrieval_mode"] == "hybrid_rrf"
        assert data["candidates_are_not_facts"] is True
        assert "不是已核实的事实依据" in data["notice"]

    def test_candidate_fields_complete(self, service):
        r = service.search("电力市场注册", 3)
        c = r.data["evidence_candidates"][0]
        for key in (
            "evidence_id",
            "chunk_id",
            "policy_id",
            "document_id",
            "version",
            "title",
            "source_url",
            "start",
            "end",
            "page",
            "content_sha256",
            "text",
            "retrieval_rank",
            "retrieval_score",
        ):
            assert key in c, key
        assert c["start"] < c["end"]
        assert len(c["content_sha256"]) == 64

    def test_text_truncation_flagged(self, service):
        r = service.search("电力市场注册", 5)
        for c in r.data["evidence_candidates"]:
            assert len(c["text"]) <= MAX_TEXT_CHARS
            if c["text_truncated"]:
                assert len(c["text"]) == MAX_TEXT_CHARS

    def test_freshness_ok(self, service):
        r = service.search("电力市场", 3)
        assert r.data["index_freshness"] == "ok"

    def test_index_readonly(self, service):
        """检索前后政策库字节不变。"""
        before = DB.read_bytes()
        service.search("电力市场", 3)
        assert DB.read_bytes() == before


# ==========================================================================
# 7. 工具经 Executor 的三层状态
# ==========================================================================
@needs_index
@needs_db
@needs_model
class TestEvidenceToolStates:
    def test_ok_success_via_executor(self):
        registry = build_live_registry(DB)
        executor = ToolExecutor(registry)
        r = executor.execute(
            "search_policy_evidence",
            json.dumps({"query": "电力市场注册", "top_k": 3}, ensure_ascii=False),
        )
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.SUCCESS
        assert r.data["candidate_count"] >= 1

    def test_invalid_arguments(self):
        registry = build_live_registry(DB)
        executor = ToolExecutor(registry)
        r = executor.execute(
            "search_policy_evidence", '{"query": "x", "top_k": 999}'
        )
        assert r.execution_status is ExecutionStatus.INVALID_ARGUMENTS

    def test_original_tools_still_work(self):
        """原有政策查询能力无回归。"""
        registry = build_live_registry(DB)
        executor = ToolExecutor(registry)

        r = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        )
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.SUCCESS
        assert r.data["items"]

        empty = executor.execute(
            "search_policy",
            json.dumps({"region": "", "keyword": "绝不存在的内容xyz"}, ensure_ascii=False),
        )
        assert empty.business_status is BusinessStatus.EMPTY


# ==========================================================================
# 8. Agent Runtime 端到端
# ==========================================================================
@needs_index
@needs_db
@needs_model
class TestAgentRuntimeIntegration:
    def test_full_loop_with_retrieval(self):
        from powercopy.core.agent import Agent
        from workbench.runtime_api import (
            LocalPolicyLLM,
            ObservedLLM,
            ObservedToolExecutor,
            RunRequest,
        )

        req = RunRequest(
            task="查询电力市场注册条件",
            keyword="电力市场注册",
            use_retrieval=True,
        )
        client = LocalPolicyLLM(req)
        registry = build_live_registry(DB)
        observed = ObservedLLM(client)
        executor = ObservedToolExecutor(registry)
        limits = req.model_dump(
            include={
                "max_rounds",
                "max_tool_calls",
                "max_api_attempts",
                "max_known_tokens",
            }
        )
        agent = Agent(observed, registry, "你是政策研究助手。", **limits)
        agent.executor = executor

        result = agent.run(req.task)

        assert result["status"] == "completed"
        assert executor.calls, "必须实际调用了工具"
        assert executor.calls[0]["name"] == "search_policy_evidence"
        assert executor.calls[0]["execution_status"] == "ok"
        assert executor.calls[0]["business_status"] == "success"
        # 输出不得把候选表述为事实
        assert "候选" in (result["content"] or "")

    def test_default_path_unchanged(self):
        """use_retrieval=False 时仍走原有 search_policy 流程。"""
        from powercopy.core.agent import Agent
        from workbench.runtime_api import (
            LocalPolicyLLM,
            ObservedLLM,
            ObservedToolExecutor,
            RunRequest,
        )

        req = RunRequest(
            task="查询电力政策", keyword="电力", use_retrieval=False
        )
        client = LocalPolicyLLM(req)
        registry = build_live_registry(DB)
        observed = ObservedLLM(client)
        executor = ObservedToolExecutor(registry)
        limits = req.model_dump(
            include={
                "max_rounds",
                "max_tool_calls",
                "max_api_attempts",
                "max_known_tokens",
            }
        )
        agent = Agent(observed, registry, "你是政策研究助手。", **limits)
        agent.executor = executor

        result = agent.run(req.task)

        assert result["status"] == "completed"
        assert executor.calls[0]["name"] == "search_policy"

    def test_encoder_reused_across_calls(self):
        """多次检索不能重复加载模型。"""
        service = PolicyEvidenceService(
            RetrievalPaths.resolve(RAG_DIR), db_path=DB
        )
        service.search("电力市场", 2)
        first = id(service.embedder)
        service.search("电价", 2)
        assert id(service.embedder) == first

    def test_hybrid_retriever_reused(self):
        service = PolicyEvidenceService(
            RetrievalPaths.resolve(RAG_DIR), db_path=DB
        )
        service.search("电力市场", 2)
        first = id(service._hybrid)
        service.search("电价", 2)
        assert id(service._hybrid) == first


# ==========================================================================
# 9. 前端工具清单
# ==========================================================================
@needs_db
class TestToolListReflectsRuntime:
    def test_registry_exposes_three_tools(self):
        registry = build_live_registry(DB)
        assert sorted(registry.list_tools()) == [
            "get_policy_detail",
            "search_policy",
            "search_policy_evidence",
        ]

    def test_evidence_tool_has_schema(self):
        registry = build_live_registry(DB)
        tool = registry.get("search_policy_evidence")
        schema = tool.args_schema.model_json_schema()
        assert "query" in schema["properties"]
        assert "top_k" in schema["properties"]
        assert schema.get("additionalProperties") is False

    def test_evidence_tool_description_disclaims_facts(self):
        registry = build_live_registry(DB)
        tool = registry.get("search_policy_evidence")
        assert "候选" in tool.description
        assert "不是已核实的事实依据" in tool.description


# ==========================================================================
# 10. /tools 端点（前端消费的数据源）
# ==========================================================================
@needs_db
class TestToolsEndpoint:
    """前端 ToolsPage 通过 GET /api/workbench/tools 渲染工具清单。

    这里直接请求真实端点，确保「前端看到的」与「Agent 运行时实际注册的」
    是同一份数据，不会出现文档/前端与运行时漂移。
    """

    @pytest.fixture(scope="class")
    @classmethod
    def client(cls):
        from fastapi.testclient import TestClient

        from workbench.main import app

        return TestClient(app)

    def test_endpoint_lists_three_tools(self, client):
        r = client.get("/api/workbench/tools")
        assert r.status_code == 200
        names = sorted(t["name"] for t in r.json()["data"]["items"])
        assert names == [
            "get_policy_detail",
            "search_policy",
            "search_policy_evidence",
        ]

    def test_evidence_tool_schema_exposed(self, client):
        items = client.get("/api/workbench/tools").json()["data"]["items"]
        tool = next(
            t for t in items if t["name"] == "search_policy_evidence"
        )
        props = tool["parameters"]["properties"]
        assert "query" in props
        assert "top_k" in props
        assert tool["parameters"].get("additionalProperties") is False

    def test_evidence_tool_declares_limits(self, client):
        items = client.get("/api/workbench/tools").json()["data"]["items"]
        tool = next(
            t for t in items if t["name"] == "search_policy_evidence"
        )
        limits = " ".join(tool["limitations"])
        assert "候选" in limits
        assert "非已核实事实" in limits

    def test_matches_registry(self, client):
        """端点返回的工具名必须与 registry 完全一致。"""
        items = client.get("/api/workbench/tools").json()["data"]["items"]
        endpoint_names = sorted(t["name"] for t in items)
        registry_names = sorted(build_live_registry(DB).list_tools())
        assert endpoint_names == registry_names
