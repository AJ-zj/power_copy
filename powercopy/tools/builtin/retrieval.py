"""检索证据工具：把已实现的混合检索接入 Tool System。

设计边界
--------
本工具**只做检索**，不做事实断言：

    「检索到候选」≠「已核实的事实依据」

因此返回结构里每个候选都带完整溯源（policy_id / document_id / version /
字符偏移 / 内容哈希 / 来源 URL），并显式声明 `candidates_are_not_facts: true`。
模型必须自己引用证据，不能把候选当成结论。

路径安全
--------
索引路径、清单路径、数据库路径**全部来自程序配置**，
不接受模型或 API 用户传入。模型只能控制 `query` 与 `top_k`。

资源限制
--------
- query 长度上限 200 字符
- top_k 上限 20（默认 5）
- 单个候选正文上限 800 字符（超出截断并标注）
- 候选总数受 top_k 约束

三层状态语义（沿用 04.9）
------------------------
| 情况 | execution_status | business_status | 模型看到 |
|---|---|---|---|
| 正常召回 | ok | success | 候选列表 |
| 检索成功但无候选 | ok | empty | 空列表 + 说明 |
| 索引不可用 / 版本不一致 | ok | failed | 业务错误码 |
| 已降级为单路 | ok | success | 候选 + degraded 标注 |
| 参数不合法 | invalid_arguments | not_applicable | Schema 错误 |
"""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from powercopy.rag.dense import DenseSearch, DenseSearchError
from powercopy.rag.hybrid import HybridError, HybridRetriever
from powercopy.rag.lexical import LexicalSearch, LexicalSearchError
from powercopy.tools.base import Tool
from powercopy.tools.result import (
    BusinessStatus,
    ToolBusinessError,
    ToolOutcome,
)

# ---------------------------------------------------------------- 配置常量

DEFAULT_INDEX_DIR = "storage/rag"
INDEX_DIR_ENV = "POWERCOPY_RAG_DIR"

DEFAULT_MANIFEST = "chunks.json"
DEFAULT_LEXICAL_DB = "lexical.db"
DEFAULT_DENSE_NPZ = "dense.npz"

MAX_QUERY_CHARS = 200
MAX_TOP_K = 20
DEFAULT_TOP_K = 5
MAX_TEXT_CHARS = 800

# 面向模型的固定文案，不含任何内部路径或堆栈
SAFE_MESSAGES = {
    "RAG_INDEX_UNAVAILABLE": (
        "本地检索索引当前不可用。可以改用 search_policy 做关键词查询，"
        "或告知用户检索能力暂不可用。"
    ),
    "RAG_INDEX_STALE": (
        "检索索引与当前政策库版本不一致，索引可能已过期。"
        "结果仅供参考，不代表最新政策；请提示用户核实。"
    ),
    "RAG_RETRIEVAL_FAILED": "检索执行失败，请调整问题表述后重试。",
    "RAG_MANIFEST_MISSING": "检索索引的切片清单缺失，无法确认索引与政策的对应关系。",
}
DEFAULT_SAFE_MESSAGE = "检索失败，请调整参数后重试。"


@dataclass(frozen=True)
class RetrievalPaths:
    """检索相关路径。全部来自程序配置，不可由模型指定。"""

    manifest: Path
    lexical_db: Path
    dense_npz: Path

    @classmethod
    def resolve(cls, rag_dir: str | Path | None = None) -> "RetrievalPaths":
        base = Path(rag_dir or environ.get(INDEX_DIR_ENV) or DEFAULT_INDEX_DIR)
        return cls(
            manifest=base / DEFAULT_MANIFEST,
            lexical_db=base / DEFAULT_LEXICAL_DB,
            dense_npz=base / DEFAULT_DENSE_NPZ,
        )


# ---------------------------------------------------------------- 参数模型


class SearchPolicyEvidenceInput(BaseModel):
    """检索证据工具的入参。

    `strict=True` + `extra="forbid"`：拒绝任何未声明字段，
    防止模型尝试传入路径类参数。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description=(
            "研究问题或检索语句。用自然语言描述你要找的政策依据，"
            f"最长 {MAX_QUERY_CHARS} 字符。"
        ),
    )

    top_k: int = Field(
        default=DEFAULT_TOP_K,
        ge=1,
        le=MAX_TOP_K,
        description=(
            f"返回的候选数量，1-{MAX_TOP_K}，默认 {DEFAULT_TOP_K}。"
            "候选越多越可能包含无关内容。"
        ),
    )


# ---------------------------------------------------------------- 服务


def _business_error(code: str, message: str | None = None) -> ToolBusinessError:
    return ToolBusinessError(
        code,
        message or SAFE_MESSAGES.get(code, DEFAULT_SAFE_MESSAGE),
        retryable=False,
    )


class PolicyEvidenceService:
    """混合检索服务，持有检索器与编码器单例。

    编码器在首次检索时加载，之后复用 —— 模型加载约 30 秒，
    绝不能每次工具调用都重新加载。
    """

    def __init__(
        self,
        paths: RetrievalPaths,
        *,
        db_path: str | Path | None = None,
        embedder: Any = None,
    ):
        self.paths = paths
        # as_uri() 需要绝对路径
        self.db_path = Path(db_path).resolve() if db_path else None
        self._embedder = embedder
        self._hybrid: HybridRetriever | None = None
        self._available: bool | None = None
        self._unavailable_reason = ""

    # ---- 懒加载 ----

    @property
    def embedder(self):
        """复用同一个编码器实例。"""
        if self._embedder is None:
            from powercopy.rag.embedding import BGEEmbedder

            self._embedder = BGEEmbedder()
        return self._embedder

    def _build_hybrid(self) -> HybridRetriever:
        if self._hybrid is not None:
            return self._hybrid

        missing = []
        if not self.paths.manifest.is_file():
            missing.append("manifest")
        if not self.paths.lexical_db.is_file():
            missing.append("lexical")
        if not self.paths.dense_npz.is_file():
            missing.append("dense")

        if missing:
            self._available = False
            self._unavailable_reason = (
                "索引文件缺失：" + "、".join(missing)
            )
            raise _business_error("RAG_INDEX_UNAVAILABLE")

        lexical: LexicalSearch | None = None
        dense: DenseSearch | None = None
        problems: list[str] = []

        try:
            lexical = LexicalSearch(self.paths.lexical_db)
        except LexicalSearchError as exc:
            problems.append(f"lexical: {exc}")

        try:
            dense = DenseSearch(
                self.paths.dense_npz,
                manifest_path=self.paths.manifest,
                embedder=self.embedder,
            )
        except DenseSearchError as exc:
            problems.append(f"dense: {exc}")

        if lexical is None and dense is None:
            self._available = False
            self._unavailable_reason = "；".join(problems)
            raise _business_error("RAG_INDEX_UNAVAILABLE")

        # allow_degrade=True 让单路故障不至于完全不可用，
        # 但 HybridRetriever 会把降级状态显式带出来
        self._hybrid = HybridRetriever(lexical, dense, allow_degrade=True)
        self._available = True
        return self._hybrid

    # ---- 索引与数据库版本一致性 ----

    def check_index_freshness(self) -> dict:
        """检查索引是否对应当前政策库的最新版本。

        做法：从政策库读出当前每个 policy_id 的最大 version 与 content_sha256，
        再与索引清单里的 (policy_id, version, content_sha256) 比对。

        Returns:
            {"fresh": bool, "status": str, "detail": str}
            无法确认时 `fresh` 为 False（宁可标注不可信，也不默认最新）。
        """

        if self.db_path is None or not self.db_path.is_file():
            return {
                "fresh": False,
                "status": "db_unavailable",
                "detail": "政策库不可读，无法校验索引是否最新",
            }

        if not self.paths.manifest.is_file():
            return {
                "fresh": False,
                "status": "manifest_missing",
                "detail": "切片清单缺失，无法校验",
            }

        import json as _json
        import sqlite3

        try:
            manifest = _json.loads(
                self.paths.manifest.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            return {
                "fresh": False,
                "status": "manifest_unreadable",
                "detail": f"切片清单无法读取：{exc}",
            }

        indexed = {
            (c["policy_id"], int(c["version"]), c.get("content_sha256") or "")
            for c in manifest.get("chunks") or []
        }
        indexed_versions = {}
        for policy_id, version, content_sha in indexed:
            indexed_versions.setdefault(policy_id, set()).add(
                (version, content_sha)
            )

        try:
            # 只读打开，绝不写库
            conn = sqlite3.connect(
                self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5
            )
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                # content_sha256 在 policy_versions 上（业务哈希），
                # 不在 policy_documents 上
                rows = conn.execute(
                    """
                    SELECT policy_id, version, content_sha256
                    FROM policy_versions
                    """
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return {
                "fresh": False,
                "status": "db_read_error",
                "detail": f"政策库读取失败：{exc}",
            }

        current = {}
        for row in rows:
            current.setdefault(row["policy_id"], set()).add(
                (int(row["version"]), row["content_sha256"] or "")
            )

        # 索引里有、库里没有 → 索引过期（政策被删除或重建）
        stale_policies = []
        for policy_id, entries in indexed_versions.items():
            if policy_id not in current:
                stale_policies.append(policy_id)
                continue
            if not entries & current[policy_id]:
                stale_policies.append(policy_id)

        # 库里有更新的版本，索引没有 → 索引过期
        newer_available = []
        for policy_id, entries in current.items():
            max_version = max(v for v, _ in entries)
            indexed_max = max(
                (v for v, _ in indexed_versions.get(policy_id, set())),
                default=0,
            )
            if indexed_max < max_version:
                newer_available.append(policy_id)

        if stale_policies or newer_available:
            parts = []
            if stale_policies:
                parts.append(f"索引中有 {len(stale_policies)} 个政策与库不符")
            if newer_available:
                parts.append(f"库中有 {len(newer_available)} 个政策存在更新版本")
            return {
                "fresh": False,
                "status": "stale",
                "detail": "；".join(parts),
            }

        return {
            "fresh": True,
            "status": "ok",
            "detail": "索引与政策库版本一致",
        }

    # ---- 检索 ----

    def search(self, query: str, top_k: int) -> dict:
        """执行检索并组装工具输出。"""

        hybrid = self._build_hybrid()

        try:
            outcome = hybrid.search(
                query,
                lexical_candidate_k=max(top_k * 2, 10),
                dense_candidate_k=max(top_k * 2, 10),
                final_top_k=top_k,
            )
        except (HybridError, LexicalSearchError, DenseSearchError) as exc:
            raise _business_error(
                "RAG_RETRIEVAL_FAILED", f"检索执行失败：{exc}"
            ) from exc

        freshness = self.check_index_freshness()

        candidates = [
            self._to_candidate(hit) for hit in outcome.hits
        ]

        payload = {
            "query": outcome.query,
            "retrieval_mode": outcome.match_mode,
            "index_fingerprint": self._index_fingerprint(outcome),
            "degraded_status": outcome.degraded,
            "degraded_detail": outcome.degraded_detail,
            "consistency_status": outcome.consistency_status,
            "index_freshness": freshness["status"],
            "index_freshness_detail": freshness["detail"],
            "candidate_count": len(candidates),
            "evidence_candidates": candidates,
            "candidates_are_not_facts": True,
            "notice": self._build_notice(outcome, freshness),
        }

        # 业务状态判定
        if outcome.returned == 0:
            return ToolOutcome(
                data=payload,
                business_status=BusinessStatus.EMPTY,
                business_code="NO_EVIDENCE_CANDIDATE",
            )

        if not freshness["fresh"]:
            # 索引可能过期：仍返回候选，但用 business_code 明确标注
            return ToolOutcome(
                data=payload,
                business_status=BusinessStatus.SUCCESS,
                business_code="INDEX_MAY_BE_STALE",
            )

        return ToolOutcome(
            data=payload, business_status=BusinessStatus.SUCCESS
        )

    # ---- 内部 ----

    def _index_fingerprint(self, outcome) -> str:
        fps = outcome.index_fingerprints or {}
        if len(set(fps.values())) == 1 and fps:
            return next(iter(fps.values()))
        # 两路指纹不同或缺一路：返回组合指纹，便于审计
        if fps:
            return "|".join(f"{k}={v}" for k, v in sorted(fps.items()))
        return ""

    @staticmethod
    def _to_candidate(hit) -> dict:
        text = hit.text or ""
        truncated = len(text) > MAX_TEXT_CHARS
        if truncated:
            text = text[:MAX_TEXT_CHARS]

        return {
            "evidence_id": hit.chunk_id[:16],
            "chunk_id": hit.chunk_id,
            "policy_id": hit.policy_id,
            "document_id": hit.document_id,
            "version": hit.version,
            "title": hit.title,
            "source_url": hit.source_url,
            "start": hit.start,
            "end": hit.end,
            "page": hit.page,
            "content_sha256": hit.content_sha256,
            "text": text,
            "text_truncated": truncated,
            "retrieval_rank": hit.rank,
            "retrieval_score": round(float(hit.rrf_score), 6),
            "path_contributions": [
                {
                    "path": c.path,
                    "rank": c.rank,
                    "score": round(c.score, 6) if c.score is not None else None,
                    "match_mode": c.match_mode,
                    "rrf": round(c.rrf, 8),
                }
                for c in hit.contributions
            ],
        }

    @staticmethod
    def _build_notice(outcome, freshness: dict) -> str:
        parts = [
            "以下内容是**检索候选**，不是已核实的事实依据。"
            "引用前必须核对原文与适用范围。"
        ]

        if outcome.degraded is not None:
            parts.append(
                f"⚠ 降级模式（{outcome.degraded}）："
                f"本次不是完整混合检索。{outcome.degraded_detail or ''}"
            )

        if not freshness["fresh"]:
            parts.append(
                f"⚠ 索引状态：{freshness['detail']}。"
                f"结果可能不是最新政策。"
            )

        parts.append(
            "检索未命中不等于政策不存在；"
            "候选相似度高也不代表与问题相关。"
        )

        return "".join(parts)


# ---------------------------------------------------------------- 工具构造


def build_evidence_tool(service: PolicyEvidenceService) -> Tool:
    """把服务封装成标准 Tool。

    注意：handler 是闭包，只暴露 query 与 top_k；
    路径与数据库连接全部由 service 持有，模型无法影响。
    """

    def handler(query: str, top_k: int = DEFAULT_TOP_K):
        return service.search(query, top_k)

    return Tool(
        name="search_policy_evidence",
        description=(
            "在本地政策库的切片索引中检索与问题相关的原文证据片段。"
            "返回的是**候选**片段（含 policy_id、版本、字符偏移、来源 URL、内容哈希），"
            "不是已核实的事实依据，必须核对原文后再引用。"
            "适用于需要引用政策原文条款、跨条款比较的研究问题。"
            "若只需按省份和关键词粗查政策清单，用 search_policy 更合适。"
            f"query 最长 {MAX_QUERY_CHARS} 字符，top_k 最多 {MAX_TOP_K}。"
        ),
        args_schema=SearchPolicyEvidenceInput,
        handler=handler,
    )


def register_evidence_tool(
    registry,
    service: PolicyEvidenceService,
) -> str:
    """注册检索证据工具，返回工具名。"""

    tool = build_evidence_tool(service)
    registry.register(tool)
    return tool.name


__all__ = [
    "DEFAULT_TOP_K",
    "INDEX_DIR_ENV",
    "MAX_QUERY_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_TOP_K",
    "PolicyEvidenceService",
    "RetrievalPaths",
    "SAFE_MESSAGES",
    "SearchPolicyEvidenceInput",
    "build_evidence_tool",
    "register_evidence_tool",
]
