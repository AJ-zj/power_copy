"""Read-only workbench adapter for policy retrieval and saved benchmarks."""

from datetime import datetime, timezone
from functools import lru_cache
import json
import logging
import math
from os import environ, fstat
from pathlib import Path
import re
from threading import Lock
from time import perf_counter
from typing import Literal

from fastapi import APIRouter
from pydantic import ConfigDict

from powercopy.rag.chunking import digest
from powercopy.rag.dense import DenseSearch, SCHEMA_VERSION as DENSE_SCHEMA
from powercopy.rag.embedding import BGEEmbedder, MODEL_NAME
from powercopy.rag.hybrid import HybridRetriever
from powercopy.rag.lexical import LexicalSearch, SCHEMA_VERSION as LEXICAL_SCHEMA
from powercopy.tools.builtin.retrieval import (
    DEGRADATION_MESSAGES,
    FAILED_DEGRADATIONS,
    INDEX_DIR_ENV,
    PolicyEvidenceService,
    RetrievalPaths,
    SearchPolicyEvidenceInput,
)


logger = logging.getLogger(__name__)
MODES = ("lexical", "dense", "hybrid")
PATHS = ("lexical", "dense")
MAX_JSON_BYTES = 8 * 1024 * 1024
MANIFEST_SCHEMA = "policy-chunk-manifest-v1"
BENCHMARK_FILE = "compare-hybrid-final.json"
FRESHNESS_MESSAGES = {
    "ok": "索引清单与政策库的最近观测版本一致。",
    "stale": "索引清单与政策库的最近观测版本有差异。",
    "db_unavailable": "政策库不可用，无法确认索引新鲜度。",
    "db_read_error": "政策库读取失败，无法确认索引新鲜度。",
    "manifest_missing": "切片清单缺失，无法确认索引新鲜度。",
    "manifest_unreadable": "切片清单无效，无法确认索引新鲜度。",
}
SEARCH_MESSAGES = {
    "RAG_INDEX_UNAVAILABLE": "请求的检索索引不可用。请核对知识库状态。",
    "RAG_INDEX_FINGERPRINT_MISSING": "索引指纹缺失，无法确认候选的来源版本。",
    "RAG_INDEX_VERSION_CONFLICT": "索引与切片清单的版本不一致，检索已拒绝。",
    "RAG_RETRIEVAL_FAILED": "本次检索执行失败。请核对索引和本地编码器状态。",
    "NO_EVIDENCE_CANDIDATE": "本次检索没有返回候选片段。",
    "INDEX_MAY_BE_STALE": "索引新鲜度未通过核对，候选可能来自历史版本。",
}


class RetrievalRequest(SearchPolicyEvidenceInput):
    model_config = ConfigDict(str_strip_whitespace=True)

    mode: Literal["lexical", "dense", "hybrid"] = "hybrid"


def resolve_workbench_rag_dir(project_root: Path) -> Path:
    return (project_root / (environ.get(INDEX_DIR_ENV) or "storage/rag")).resolve()


def _read_json(path: Path) -> tuple[dict, float]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_JSON_BYTES + 1)
        modified_at = fstat(handle.fileno()).st_mtime
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds workbench limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value, modified_at


def _sha(value, length: int = 64) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) else None


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


@lru_cache(maxsize=4)
def _load_manifest(path: Path, signature: tuple[int, int, int]) -> dict:
    manifest, _ = _read_json(path)
    fingerprint = _sha(manifest.get("manifest_sha256"))
    content = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or not isinstance(manifest.get("documents"), list)
        or not isinstance(manifest.get("chunks"), list)
        or not fingerprint
        or fingerprint != digest(content)
    ):
        raise ValueError("Invalid manifest")
    return manifest


@lru_cache(maxsize=4)
def _dense_metadata(path: Path, signature: tuple[int, int, int]) -> dict:
    # Read the header even if the index clashes with the current manifest.
    # DenseSearch validates the arrays and is the only code that searches them.
    import numpy as np

    with np.load(path, allow_pickle=False) as archive:
        meta = json.loads(str(archive["meta_json"]))
    if not isinstance(meta, dict):
        raise ValueError("Invalid index metadata")
    return meta


def _index_state(status: str, metadata: dict | None = None) -> dict:
    meta = metadata or {}
    schema = meta.get("schema")
    dimension = meta.get("dim")
    return {
        "status": status,
        "schema": schema if schema in (LEXICAL_SCHEMA, DENSE_SCHEMA) else None,
        "fingerprint": _sha(meta.get("fingerprint")),
        "manifest_fingerprint": _sha(meta.get("manifest_sha256")),
        "model": MODEL_NAME if meta.get("model") == MODEL_NAME else None,
        "model_commit": _sha(meta.get("model_commit"), 40),
        "dimension": dimension if type(dimension) is int and dimension > 0 else None,
    }


def _path_result(status: str, mode: str | None = None, returned: int = 0) -> dict:
    return {"status": status, "match_mode": mode, "returned": returned}


def _candidate(row: dict, mode: str) -> dict:
    if mode == "hybrid":
        row["score"] = row.pop("rrf_score")
    else:
        row["contributions"] = [{
            "path": mode,
            "rank": row["rank"],
            "score": row["score"],
            "match_mode": row.get("match_mode", "dense_bge"),
            "rrf": None,
        }]
    return row


class RetrievalStudio:
    def __init__(self, project_root: Path):
        self.paths = RetrievalPaths.resolve(resolve_workbench_rag_dir(project_root))
        db_path = project_root / "storage" / "policies_versioned.db"
        self.policy_service = PolicyEvidenceService(self.paths, db_path=db_path)
        self.embedder = BGEEmbedder(local_files_only=True)
        self._search_lock = Lock()

    @lru_cache(maxsize=1)
    def _dense(self, index_signature, manifest_signature) -> DenseSearch:
        return DenseSearch(
            self.paths.dense_npz,
            manifest_path=self.paths.manifest,
            embedder=self.embedder,
            verify_model=False,
        )

    def _inspect(self, requested: tuple[str, ...]) -> tuple[dict, dict]:
        try:
            manifest_sig = _signature(self.paths.manifest)
            manifest = _load_manifest(self.paths.manifest, manifest_sig) if manifest_sig else None
        except (ValueError, OSError, TypeError):
            manifest = None
            manifest_status = "manifest_unreadable"
        else:
            manifest_status = "ok" if manifest is not None else "manifest_missing"

        manifest_fingerprint = manifest["manifest_sha256"] if manifest is not None else None
        states = {}
        retrievers = {}
        for path in requested:
            index_path = self.paths.lexical_db if path == "lexical" else self.paths.dense_npz
            metadata = {}
            try:
                index_sig = _signature(index_path)
                if index_sig is None:
                    states[path] = _index_state("missing")
                    continue
                if path == "lexical":
                    retriever = LexicalSearch(index_path)
                    metadata = retriever.describe()
                    if (
                        metadata.get("schema") != LEXICAL_SCHEMA
                        or _sha(metadata.get("manifest_sha256")) != manifest_fingerprint
                        or not manifest_fingerprint
                    ):
                        raise ValueError("Invalid lexical schema or manifest")
                    retriever.chunk_count()
                else:
                    metadata = _dense_metadata(index_path, index_sig)
                    if metadata.get("schema") != DENSE_SCHEMA or manifest is None:
                        raise ValueError("Invalid dense schema or manifest")
                    retriever = self._dense(index_sig, manifest_sig)
                states[path] = _index_state("ready", metadata)
                retrievers[path] = retriever
            except Exception as exc:
                logger.debug("Retrieval index inspection failed for %s (%s)", path, type(exc).__name__)
                states[path] = _index_state("invalid", metadata)

        consistency = self._consistency(states, manifest_fingerprint, manifest_status)
        if manifest is None:
            freshness_status = manifest_status
        else:
            try:
                freshness_status = self.policy_service.check_index_freshness()["status"]
            except (OSError, ValueError, KeyError, TypeError):
                freshness_status = "manifest_unreadable"
        freshness = {
            "status": freshness_status,
            "detail": FRESHNESS_MESSAGES.get(freshness_status, "无法确认索引新鲜度。"),
        }
        return {
            "documents": len(manifest["documents"]) if manifest is not None else None,
            "chunks": len(manifest["chunks"]) if manifest is not None else None,
            "manifest_fingerprint": manifest_fingerprint,
            "freshness": freshness,
            "indexes": states,
            "consistency_status": consistency,
        }, retrievers

    @staticmethod
    def _consistency(states: dict, manifest_fingerprint: str | None, manifest_status: str) -> str:
        if manifest_status != "ok":
            return manifest_status
        fingerprints = [state["manifest_fingerprint"] for state in states.values()]
        if any(value and value != manifest_fingerprint for value in fingerprints):
            return "version_conflict"
        if any(state["schema"] and not state["manifest_fingerprint"] for state in states.values()):
            return "fingerprint_missing"
        ready = sum(state["status"] == "ready" for state in states.values())
        if ready == 0:
            return "index_unavailable"
        if ready < len(states):
            return "single_path_unconfigured"
        return "ok"

    def status(self) -> dict:
        status, _ = self._inspect(PATHS)
        return status

    def search(self, request: RetrievalRequest) -> dict:
        started = perf_counter()
        requested = PATHS if request.mode == "hybrid" else (request.mode,)
        overview, retrievers = self._inspect(requested)
        paths = {path: _path_result("not_requested") for path in PATHS}
        for path, state in overview["indexes"].items():
            paths[path] = _path_result("not_run" if state["status"] == "ready" else state["status"])
        result = {
            "query": request.query,
            "mode": request.mode,
            "top_k": request.top_k,
            "status": "failed",
            "code": None,
            "message": None,
            "match_mode": None,
            "consistency_status": overview["consistency_status"],
            "freshness": overview["freshness"],
            "degraded_status": None,
            "paths": paths,
            "candidates_are_not_facts": True,
            "candidates": [],
        }

        blocking_codes = {
            "version_conflict": "RAG_INDEX_VERSION_CONFLICT",
            "fingerprint_missing": "RAG_INDEX_FINGERPRINT_MISSING",
            "manifest_missing": "RAG_INDEX_UNAVAILABLE",
            "manifest_unreadable": "RAG_INDEX_UNAVAILABLE",
            "index_unavailable": "RAG_INDEX_UNAVAILABLE",
        }
        code = blocking_codes.get(result["consistency_status"])
        if code is None:
            result.update(self._execute(request, retrievers, paths))
            code = result["code"]
        if code is None and result["freshness"]["status"] != "ok":
            code = "INDEX_MAY_BE_STALE"
        return {
            **result,
            "code": code,
            "message": (
                DEGRADATION_MESSAGES.get(result["degraded_status"])
                if code == "RAG_DEGRADED" else SEARCH_MESSAGES.get(code)
            ),
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        }

    def _execute(self, request: RetrievalRequest, retrievers: dict, paths: dict) -> dict:
        mode = request.mode
        if mode == "hybrid":
            return self._hybrid_search(request, retrievers, paths)
        try:
            if mode == "dense":
                with self._search_lock:
                    outcome = retrievers[mode].search(request.query, top_k=request.top_k)
            else:
                outcome = retrievers[mode].search(request.query, top_k=request.top_k)
        except Exception as exc:
            logger.warning("Retrieval failed for %s (%s)", mode, type(exc).__name__)
            paths[mode] = _path_result("failed")
            return {"code": "RAG_RETRIEVAL_FAILED"}
        paths[mode] = _path_result(
            "success" if outcome.returned else "empty", outcome.match_mode, outcome.returned,
        )
        result = {
            "match_mode": outcome.match_mode,
            "status": "success" if outcome.returned else "empty",
            "candidates": [_candidate(row, mode) for row in outcome.to_dict()["hits"]],
        }
        if not outcome.returned:
            result["code"] = "NO_EVIDENCE_CANDIDATE"
        return result

    def _hybrid_search(self, request: RetrievalRequest, retrievers: dict, paths: dict) -> dict:
        retriever = HybridRetriever(
            retrievers.get("lexical"), retrievers.get("dense"), allow_degrade=True,
        )
        try:
            candidate_k = max(request.top_k * 2, 10)
            with self._search_lock:
                outcome = retriever.search(
                    request.query,
                    lexical_candidate_k=candidate_k,
                    dense_candidate_k=candidate_k,
                    final_top_k=request.top_k,
                )
        except Exception as exc:
            logger.warning("Hybrid retrieval failed (%s)", type(exc).__name__)
            return {"code": "RAG_RETRIEVAL_FAILED"}

        degraded = outcome.degraded
        for path in PATHS:
            if path not in retrievers:
                continue
            match_mode = getattr(outcome, f"{path}_mode")
            returned = getattr(outcome, f"{path}_returned")
            if match_mode is not None:
                paths[path] = _path_result("success" if returned else "empty", match_mode, returned)
            elif degraded != "fingerprint_missing":
                paths[path] = _path_result("failed")
        result = {
            "match_mode": outcome.match_mode,
            "degraded_status": degraded,
            "consistency_status": outcome.consistency_status,
            "candidates": [_candidate(row, "hybrid") for row in outcome.to_dict()["hits"]],
        }
        if degraded in FAILED_DEGRADATIONS:
            code = "RAG_INDEX_FINGERPRINT_MISSING" if degraded == "fingerprint_missing" else "RAG_RETRIEVAL_FAILED"
            return {**result, "status": "failed", "code": code}
        if degraded:
            return {**result, "status": "degraded", "code": "RAG_DEGRADED"}
        return {
            **result,
            "status": "success" if outcome.returned else "empty",
            "code": None if outcome.returned else "NO_EVIDENCE_CANDIDATE",
        }


SUMMARY_FIELDS = (
    "cases_total", "cases_scored", "empty_cases", "empty_pass", "top_k",
    "recall_at_k", "mrr", "ndcg_at_k", "hit_rate", "complete_evidence_rate",
    "multi_evidence_cases", "multi_evidence_complete_rate",
)
TYPE_FIELDS = ("count", "recall_at_k", "hit_rate", "complete_evidence_rate")
CASE_TYPES = ("exact_keyword", "short_word", "semantic", "cross_clause", "policy_relation")


def _numeric_fields(source: dict, fields: tuple[str, ...]) -> dict:
    if not isinstance(source, dict):
        raise ValueError("Expected benchmark fields")
    return {
        name: value
        for name in fields if type(value := source.get(name)) in (int, float) and math.isfinite(value)
    }


def _benchmark_summary(report: dict) -> dict:
    summary = _numeric_fields(report, SUMMARY_FIELDS)
    categories = report["by_type"]
    summary["by_type"] = {
        case: _numeric_fields(categories[case], TYPE_FIELDS)
        for case in CASE_TYPES if case in categories
    }
    return summary


def _benchmark(path: Path) -> dict:
    empty = {
        "eval_set": None, "eval_version": None, "evaluated_at": None,
        "report_modified_at": None, "corpus": None, "kind": "development", "modes": {},
    }
    try:
        report, modified_at = _read_json(path)
        dataset = report["eval_set"]
        if not re.fullmatch(r"retrieval-eval-v[0-9]+\.json", dataset) or type(report["eval_version"]) is not int:
            raise ValueError("Unknown evaluation dataset")
        modes = {}
        for mode in MODES:
            source = report[mode]
            summaries = {
                f"at_{k}": _benchmark_summary(source[f"at_{k}"])
                for k in (1, 5, 10) if f"at_{k}" in source
            }
            if "at_1" not in summaries or "at_10" not in summaries:
                raise ValueError("Incomplete benchmark report")
            modes[mode] = {
                **summaries,
                "timing_ms": _numeric_fields(source.get("timing_ms", {}), ("avg", "min", "max")),
            }
        evaluated_at = report.get("evaluated_at")
        if evaluated_at is not None:
            evaluated_at = datetime.fromisoformat(evaluated_at).isoformat()
        return {
            "status": "ready",
            "eval_set": dataset,
            "eval_version": report["eval_version"],
            "evaluated_at": evaluated_at,
            "report_modified_at": datetime.fromtimestamp(modified_at, tz=timezone.utc).isoformat(),
            "corpus": _numeric_fields(report["corpus"], ("documents", "chunks")),
            "kind": "development",
            "modes": modes,
        }
    except FileNotFoundError:
        return {**empty, "status": "missing"}
    except (ValueError, OSError, KeyError, TypeError, OverflowError):
        return {**empty, "status": "invalid"}


def build_retrieval_router(project_root: Path) -> APIRouter:
    studio = RetrievalStudio(project_root)
    router = APIRouter(prefix="/rag", tags=["RAG Studio"])

    @router.get("/status")
    def status():
        return {"data": studio.status()}

    @router.get("/benchmark")
    def benchmark():
        return {"data": _benchmark(studio.paths.manifest.parent / BENCHMARK_FILE)}

    @router.post("/search")
    def search(request: RetrievalRequest):
        return {"data": studio.search(request)}

    return router
