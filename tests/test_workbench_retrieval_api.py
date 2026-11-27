import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
from fastapi import FastAPI
import numpy as np
import pytest

from powercopy.rag.chunking import digest
from powercopy.rag.dense import SCHEMA_VERSION
from powercopy.rag.lexical import build_index
from workbench.retrieval_api import RetrievalRequest, RetrievalStudio, build_retrieval_router


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def rag_project(tmp_path, monkeypatch):
    monkeypatch.delenv("POWERCOPY_RAG_DIR", raising=False)
    storage = tmp_path / "storage"
    rag = storage / "rag"
    rag.mkdir(parents=True)
    chunk = {
        "chunk_id": "1" * 64,
        "policy_id": "2" * 32,
        "document_id": "doc-1",
        "version": 1,
        "page": 2,
        "start": 20,
        "title": "市场规则",
        "text": "电力市场实行注册制度。",
        "source_url": "https://example.invalid/policy",
        "content_sha256": "3" * 64,
    }
    chunk["end"] = chunk["start"] + len(chunk["text"])
    manifest = {
        "schema": "policy-chunk-manifest-v1",
        "documents": [{name: chunk[name] for name in ("policy_id", "version", "content_sha256")}],
        "chunks": [chunk],
    }
    manifest["manifest_sha256"] = digest(manifest)
    _write_json(rag / "chunks.json", manifest)
    build_index(rag / "chunks.json", rag / "lexical.db")
    with sqlite3.connect(storage / "policies_versioned.db") as database:
        database.execute("CREATE TABLE policy_versions (policy_id TEXT, version INT, content_sha256 TEXT)")
        database.execute(
            "INSERT INTO policy_versions VALUES (?, ?, ?)",
            tuple(chunk[name] for name in ("policy_id", "version", "content_sha256")),
        )
    return tmp_path, rag, chunk, manifest


def test_lexical_returns_full_candidate_and_coordinates(rag_project):
    project, _, chunk, _ = rag_project
    studio = RetrievalStudio(project)
    outcome = studio.search(RetrievalRequest(query="电力市场", mode="lexical", top_k=1))
    assert outcome["status"] == "success"
    assert outcome["freshness"]["status"] == "ok"
    assert outcome["paths"]["dense"]["status"] == "not_requested"
    assert outcome["candidates_are_not_facts"] is True
    candidate, = outcome["candidates"]
    for key in ("page", "start", "end", "text", "document_id", "source_url"):
        assert candidate[key] == chunk[key]
    assert candidate["contributions"][0]["rrf"] is None


def test_lexical_empty_and_hybrid_degraded_are_distinct(rag_project):
    project, _, _, _ = rag_project
    studio = RetrievalStudio(project)
    empty = studio.search(RetrievalRequest(query="广东现货成交价", mode="lexical"))
    assert empty["status"] == "empty" and empty["candidates"] == []
    degraded = studio.search(RetrievalRequest(query="电力市场", mode="hybrid"))
    assert degraded["status"] == "degraded"
    assert degraded["paths"]["dense"]["status"] == "missing"
    assert degraded["candidates"][0]["contributions"][0]["rrf"] > 0


@pytest.mark.parametrize("fingerprint", ["", "4" * 64])
def test_hybrid_rejects_missing_or_conflicting_index_identity(rag_project, fingerprint):
    project, rag, _, _ = rag_project
    # A valid manifest header is enough to classify a conflict, irrespective of vectors.
    meta = {"schema": SCHEMA_VERSION, "manifest_sha256": fingerprint}
    np.savez(rag / "dense.npz", meta_json=np.asarray(json.dumps(meta)))
    outcome = RetrievalStudio(project).search(RetrievalRequest(query="电力市场", mode="hybrid"))
    assert outcome["status"] == "failed" and outcome["candidates"] == []
    expected = "fingerprint_missing" if not fingerprint else "version_conflict"
    assert outcome["consistency_status"] == expected


def test_corrupt_index_and_stale_source_remain_visible(rag_project):
    project, rag, chunk, _ = rag_project
    (rag / "dense.npz").write_bytes(b"not a valid index")
    with sqlite3.connect(project / "storage" / "policies_versioned.db") as database:
        database.execute(
            "INSERT INTO policy_versions VALUES (?, ?, ?)",
            (chunk["policy_id"], 2, "0" * 64),
        )
    studio = RetrievalStudio(project)
    status = studio.status()
    assert status["indexes"]["dense"]["status"] == "invalid"
    assert status["freshness"]["status"] == "stale"
    outcome = studio.search(RetrievalRequest(query="电力市场", mode="dense"))
    assert outcome["status"] == "failed"
    assert str(rag) not in json.dumps(outcome, ensure_ascii=False)
    lexical = studio.search(RetrievalRequest(query="电力市场", mode="lexical"))
    assert lexical["code"] == "INDEX_MAY_BE_STALE"


@pytest.mark.parametrize("request_body", [
    {"query": "   "},
    {"query": "a" * 201},
    {"query": "政策", "top_k": 21},
    {"query": "政策", "top_k": "5"},
    {"query": "政策", "mode": "invalid"},
    {"query": "政策", "index_path": "another-file"},
])
def test_request_schema_rejects_invalid_input(tmp_path, request_body):
    app = FastAPI()
    app.include_router(build_retrieval_router(tmp_path))
    with TestClient(app) as client:
        assert client.post("/rag/search", json=request_body).status_code == 422


def test_saved_benchmark_uses_only_aggregates_and_preserves_absent_metrics(rag_project):
    project, rag, _, _ = rag_project
    app = FastAPI()
    app.include_router(build_retrieval_router(project))
    report = {
        "eval_set": "retrieval-eval-v2.json",
        "eval_version": 2,
        "corpus": {"documents": 2, "chunks": 26, "manifest": "private-directory"},
    }
    summary = {"recall_at_k": 1.0, "by_type": {"semantic": {"count": 6, "hit_rate": 1.0}}}
    for mode in ("hybrid", "lexical", "dense"):
        report[mode] = {"at_1": summary, "at_10": summary, "cases": ["private-case"]}
    with TestClient(app) as client:
        assert client.get("/rag/benchmark").json()["data"]["status"] == "missing"
        _write_json(rag / "compare-hybrid-final.json", report)
        benchmark = client.get("/rag/benchmark").json()["data"]
        assert benchmark["status"] == "ready"
        assert benchmark["evaluated_at"] is None and benchmark["report_modified_at"]
        assert benchmark["modes"]["hybrid"]["at_10"]["recall_at_k"] == 1.0
        assert "at_5" not in benchmark["modes"]["lexical"]
        assert "private-" not in json.dumps(benchmark)
