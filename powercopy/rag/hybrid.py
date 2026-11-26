"""05.5-A 混合检索：等权 RRF 融合。

为什么用 RRF 而不是分数相加
--------------------------
BM25 分数（负值，量纲随语料变化）与余弦相似度（-1..1）**不可比**。
直接把两者归一化后相加，等价于假设"BM25 的 0.8 和余弦的 0.8 意义相同"，
这个假设没有依据。

RRF（Reciprocal Rank Fusion）只用**排名**，不碰分数，避开量纲问题：

    RRF(d) = Σ_path  weight_path / (rank_constant + rank_path(d))

标准取法 `rank_constant=60`，出自 Cormack et al. 2009 的原始论文，
也是 Elasticsearch / Qdrant / OpenSearch 的默认值。

参考：
- Elasticsearch RRF: https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion
- Qdrant Hybrid Queries: https://qdrant.tech/documentation/hybrid-queries/
- OpenSearch RRF: https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/

设计约束
--------
- 同一 Query 跑两路，各自可配候选深度
- 以 chunk_id 去重
- 排名从 1 开始；某路未召回则该路贡献为 0
- 默认等权（weight=1.0）
- 最终 Top-K 独立配置
- 稳定排序：RRF 分数降序，同分按 chunk_id 升序
- 保留各路原始分数、排名、模式与 RRF 贡献
- 索引指纹不一致时拒绝融合
- 区分「单路正常空结果」与「检索器异常」
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from powercopy.rag.dense import DenseSearch, DenseSearchError
from powercopy.rag.lexical import LexicalSearch, LexicalSearchError

SCHEMA_VERSION = "policy-hybrid-fusion-v1"

DEFAULT_RANK_CONSTANT = 60
DEFAULT_CANDIDATE_K = 10
DEFAULT_FINAL_TOP_K = 10


class HybridError(Exception):
    """融合配置或输入错误。"""


class RetrieverFailure(Exception):
    """单路检索器异常。用于区分「正常空结果」与「检索出错」。"""


@dataclass(frozen=True)
class PathContribution:
    """单路检索对某个 chunk 的贡献。"""

    path: str
    """lexical 或 dense"""

    rank: int | None
    """该路中的排名，从 1 开始。未召回为 None。"""

    score: float | None
    """该路原始分数。未召回为 None。"""

    match_mode: str | None
    """该路的检索模式（fts5_trigram_phrase / fallback_instr_scan / dense_bge）。"""

    rrf: float
    """该路的 RRF 贡献。未召回为 0.0。"""


@dataclass
class HybridHit:
    """融合后的一条结果。"""

    chunk_id: str
    rrf_score: float
    rank: int

    # 证据元信息（与两路索引共用同一份 manifest）
    policy_id: str
    document_id: str
    version: int
    page: int | None
    start: int
    end: int
    title: str | None
    source_key: str | None
    source_url: str | None
    content_sha256: str | None
    text: str

    contributions: list[PathContribution] = field(default_factory=list)


@dataclass
class HybridOutcome:
    """一次混合检索的完整结果。"""

    query: str
    hits: list[HybridHit] = field(default_factory=list)

    rank_constant: int = DEFAULT_RANK_CONSTANT
    lexical_candidate_k: int = DEFAULT_CANDIDATE_K
    dense_candidate_k: int = DEFAULT_CANDIDATE_K
    final_top_k: int = DEFAULT_FINAL_TOP_K
    weights: dict[str, float] = field(default_factory=dict)

    # 各路的原始状态
    lexical_mode: str | None = None
    dense_mode: str | None = None
    lexical_returned: int = 0
    dense_returned: int = 0

    # 降级状态（任一非 None 即表示不是完整混合检索）
    degraded: str | None = None
    """None = 完整混合；否则为降级原因。"""

    degraded_detail: str | None = None
    """降级说明，面向人类阅读。"""

    notice: str = ""
    index_fingerprints: dict[str, str] = field(default_factory=dict)

    @property
    def match_mode(self) -> str:
        """与 LexicalSearch / DenseSearch 对齐的模式名，供评测代码复用。"""

        return "hybrid_rrf" if self.is_complete else "hybrid_rrf_degraded"

    @property
    def returned(self) -> int:
        return len(self.hits)

    @property
    def is_complete(self) -> bool:
        return self.degraded is None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "match_mode": "hybrid_rrf" if self.is_complete else "hybrid_rrf_degraded",
            "returned": self.returned,
            "degraded": self.degraded,
            "degraded_detail": self.degraded_detail,
            "rank_constant": self.rank_constant,
            "lexical_candidate_k": self.lexical_candidate_k,
            "dense_candidate_k": self.dense_candidate_k,
            "final_top_k": self.final_top_k,
            "weights": self.weights,
            "lexical_mode": self.lexical_mode,
            "dense_mode": self.dense_mode,
            "lexical_returned": self.lexical_returned,
            "dense_returned": self.dense_returned,
            "notice": self.notice,
            "index_fingerprints": self.index_fingerprints,
            "hits": [
                {
                    "rank": h.rank,
                    "chunk_id": h.chunk_id,
                    "rrf_score": round(h.rrf_score, 8),
                    "policy_id": h.policy_id,
                    "document_id": h.document_id,
                    "version": h.version,
                    "page": h.page,
                    "start": h.start,
                    "end": h.end,
                    "title": h.title,
                    "source_key": h.source_key,
                    "source_url": h.source_url,
                    "content_sha256": h.content_sha256,
                    "text": h.text,
                    "contributions": [
                        {
                            "path": c.path,
                            "rank": c.rank,
                            "score": round(c.score, 6) if c.score is not None else None,
                            "match_mode": c.match_mode,
                            "rrf": round(c.rrf, 8),
                        }
                        for c in h.contributions
                    ],
                }
                for h in self.hits
            ],
        }


def rrf_contribution(
    rank: int | None, rank_constant: int, weight: float
) -> float:
    """计算单路 RRF 贡献。

    Args:
        rank: 该路中的排名（从 1 开始）。None 表示未召回。
        rank_constant: 平滑常数，标准值 60。
        weight: 该路权重。

    Returns:
        RRF 贡献。未召回返回 0.0。
    """

    if rank is None:
        return 0.0
    if rank < 1:
        raise HybridError(f"rank 必须从 1 开始，实际 {rank}")
    if rank_constant < 1:
        raise HybridError(f"rank_constant 必须 >= 1，实际 {rank_constant}")
    if weight < 0:
        raise HybridError(f"weight 不能为负，实际 {weight}")
    return weight / (rank_constant + rank)


def _validate_config(
    *,
    rank_constant: int,
    lexical_candidate_k: int,
    dense_candidate_k: int,
    final_top_k: int,
    weights: dict[str, float],
) -> None:
    """参数校验，非法直接拒绝。"""

    if not isinstance(rank_constant, int) or rank_constant < 1:
        raise HybridError(f"rank_constant 必须是 >= 1 的整数，实际 {rank_constant!r}")

    if rank_constant > 10000:
        raise HybridError(f"rank_constant 过大（{rank_constant}），疑似配置错误")

    for name, value in (
        ("lexical_candidate_k", lexical_candidate_k),
        ("dense_candidate_k", dense_candidate_k),
        ("final_top_k", final_top_k),
    ):
        if not isinstance(value, int) or value < 1:
            raise HybridError(f"{name} 必须是 >= 1 的整数，实际 {value!r}")
        if value > 200:
            raise HybridError(f"{name} 过大（{value}），疑似配置错误")

    if set(weights) != {"lexical", "dense"}:
        raise HybridError(
            f"weights 必须恰好含 lexical 与 dense 两个键，实际 {sorted(weights)}"
        )

    for key, value in weights.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise HybridError(f"weights[{key!r}] 必须是非负数，实际 {value!r}")

    if weights["lexical"] == 0 and weights["dense"] == 0:
        raise HybridError("weights 不能同时为 0，否则无法融合")


class HybridRetriever:
    """等权 RRF 混合检索。"""

    def __init__(
        self,
        lexical: LexicalSearch | None,
        dense: DenseSearch | None,
        *,
        rank_constant: int = DEFAULT_RANK_CONSTANT,
        weights: dict[str, float] | None = None,
        allow_degrade: bool = False,
    ):
        """构造混合检索器。

        Args:
            lexical: 词法检索器。None 表示该路不可用。
            dense: 向量检索器。None 表示该路不可用。
            rank_constant: RRF 平滑常数。
            weights: 两路权重，默认等权。
            allow_degrade: 单路异常时是否降级为单路检索。
                False（默认）时单路异常直接抛错，避免把降级伪装成完整混合。
        """

        weights = weights or {"lexical": 1.0, "dense": 1.0}

        _validate_config(
            rank_constant=rank_constant,
            lexical_candidate_k=DEFAULT_CANDIDATE_K,
            dense_candidate_k=DEFAULT_CANDIDATE_K,
            final_top_k=DEFAULT_FINAL_TOP_K,
            weights=weights,
        )

        if lexical is None and dense is None:
            raise HybridError("至少需要一路检索器可用")

        self.lexical = lexical
        self.dense = dense
        self.rank_constant = rank_constant
        self.weights = dict(weights)
        self.allow_degrade = allow_degrade

    # ---- 一致性检查 ----

    def check_index_consistency(self) -> dict:
        """校验两路索引来自同一份切片清单。

        Returns:
            {"consistent": bool, "detail": str}

        Raises:
            HybridError: 索引版本不一致且不可降级时。
        """

        fingerprints = {}

        if self.lexical is not None:
            meta = self.lexical.describe()
            fingerprints["lexical"] = meta.get("manifest_sha256") or ""

        if self.dense is not None:
            meta = self.dense.meta
            fingerprints["dense"] = meta.get("manifest_sha256") or ""

        values = [v for v in fingerprints.values() if v]

        if len(values) < 2:
            return {
                "consistent": True,
                "detail": "仅一路可用，无需一致性检查",
                "fingerprints": fingerprints,
            }

        if len(set(values)) == 1:
            return {
                "consistent": True,
                "detail": "两路索引来自同一份切片清单",
                "fingerprints": fingerprints,
            }

        detail = "两路索引的 manifest_sha256 不一致：" + "，".join(
            f"{k}={v[:12]}..." for k, v in fingerprints.items()
        )

        if not self.allow_degrade:
            raise HybridError(f"{detail}（拒绝融合）")

        return {
            "consistent": False,
            "detail": detail,
            "fingerprints": fingerprints,
        }

    # ---- 检索 ----

    def search(
        self,
        query: str,
        *,
        lexical_candidate_k: int = DEFAULT_CANDIDATE_K,
        dense_candidate_k: int = DEFAULT_CANDIDATE_K,
        final_top_k: int = DEFAULT_FINAL_TOP_K,
    ) -> HybridOutcome:
        """执行混合检索。

        Args:
            query: 查询文本
            lexical_candidate_k: 词法候选深度
            dense_candidate_k: 向量候选深度
            final_top_k: 最终返回条数

        Returns:
            HybridOutcome，含各路贡献与降级状态
        """

        _validate_config(
            rank_constant=self.rank_constant,
            lexical_candidate_k=lexical_candidate_k,
            dense_candidate_k=dense_candidate_k,
            final_top_k=final_top_k,
            weights=self.weights,
        )

        query = (query or "").strip()
        if not query:
            return HybridOutcome(
                query="",
                rank_constant=self.rank_constant,
                lexical_candidate_k=lexical_candidate_k,
                dense_candidate_k=dense_candidate_k,
                final_top_k=final_top_k,
                weights=dict(self.weights),
                notice="查询为空，未执行检索。",
            )

        # 一致性检查：不一致直接拒绝，不静默融合
        consistency = self.check_index_consistency()

        # 两路检索
        #
        # 捕获范围说明：不只捕获 LexicalSearchError / DenseSearchError。
        # 真实检索器可能抛出任意异常（编码失败、文件损坏、第三方库错误），
        # 若只捕获特定类型，降级机制在真实故障时会失效。
        # 因此这里捕获 Exception，但通过 error_kind 记录异常类型名，
        # 不吞掉信息。
        lexical_hits = []
        dense_hits = []
        lexical_mode = None
        dense_mode = None
        failed_paths: list[tuple[str, str]] = []

        if self.lexical is not None:
            try:
                lex_out = self.lexical.search(
                    query, top_k=lexical_candidate_k
                )
                lexical_hits = lex_out.hits
                lexical_mode = lex_out.match_mode
            except Exception as exc:  # noqa: BLE001 - 见上方说明
                if not self.allow_degrade:
                    raise RetrieverFailure(
                        f"词法检索失败（{type(exc).__name__}）：{exc}"
                    ) from exc
                failed_paths.append(("lexical", f"{type(exc).__name__}: {exc}"))

        if self.dense is not None:
            try:
                dense_out = self.dense.search(query, top_k=dense_candidate_k)
                dense_hits = dense_out.hits
                dense_mode = dense_out.match_mode
            except Exception as exc:  # noqa: BLE001
                if not self.allow_degrade:
                    raise RetrieverFailure(
                        f"向量检索失败（{type(exc).__name__}）：{exc}"
                    ) from exc
                failed_paths.append(("dense", f"{type(exc).__name__}: {exc}"))

        degraded, degraded_detail = self._classify_degradation(failed_paths)

        # 融合
        hits = self._fuse(
            lexical_hits=lexical_hits,
            dense_hits=dense_hits,
            final_top_k=final_top_k,
        )

        notice = self._build_notice(
            degraded=degraded,
            lexical_mode=lexical_mode,
            dense_mode=dense_mode,
            consistency=consistency,
        )

        return HybridOutcome(
            query=query,
            hits=hits,
            rank_constant=self.rank_constant,
            lexical_candidate_k=lexical_candidate_k,
            dense_candidate_k=dense_candidate_k,
            final_top_k=final_top_k,
            weights=dict(self.weights),
            lexical_mode=lexical_mode,
            dense_mode=dense_mode,
            lexical_returned=len(lexical_hits),
            dense_returned=len(dense_hits),
            degraded=degraded,
            degraded_detail=degraded_detail,
            notice=notice,
            index_fingerprints=consistency.get("fingerprints", {}),
        )

    # ---- 内部 ----

    @staticmethod
    def _classify_degradation(
        failed_paths: list[tuple[str, str]],
    ) -> tuple[str | None, str | None]:
        """根据失败的路数生成降级状态。

        Returns:
            (degraded, detail)。无失败时返回 (None, None)。
        """

        if not failed_paths:
            return None, None

        if len(failed_paths) == 2:
            detail = "两路检索均异常，无法融合：" + "；".join(
                f"{name}({err})" for name, err in failed_paths
            )
            return "both_paths_failed", detail

        name, err = failed_paths[0]
        if name == "lexical":
            return (
                "lexical_failed",
                f"词法检索异常，已降级为纯向量检索：{err}",
            )
        return (
            "dense_failed",
            f"向量检索异常，已降级为纯词法检索：{err}",
        )

    def _fuse(
        self,
        *,
        lexical_hits: Sequence,
        dense_hits: Sequence,
        final_top_k: int,
    ) -> list[HybridHit]:
        """RRF 融合两路候选。"""

        # chunk_id → 各路贡献
        contributions: dict[str, list[PathContribution]] = {}
        # chunk_id → 证据元信息（任一路给出即可，同一份 manifest）
        evidence: dict[str, object] = {}

        for path_name, hits, weight in (
            ("lexical", lexical_hits, self.weights["lexical"]),
            ("dense", dense_hits, self.weights["dense"]),
        ):
            for hit in hits:
                chunk_id = hit.chunk_id
                rrf = rrf_contribution(hit.rank, self.rank_constant, weight)

                contributions.setdefault(chunk_id, []).append(
                    PathContribution(
                        path=path_name,
                        rank=hit.rank,
                        score=hit.score,
                        match_mode=hit.match_mode,
                        rrf=rrf,
                    )
                )
                evidence.setdefault(chunk_id, hit)

        # 汇总 RRF 分数
        scored = []
        for chunk_id, items in contributions.items():
            total = sum(item.rrf for item in items)
            scored.append((chunk_id, total, items, evidence[chunk_id]))

        # 稳定排序：RRF 降序，同分按 chunk_id 升序
        scored.sort(key=lambda x: (-x[1], x[0]))

        results = []
        for rank, (chunk_id, total, items, hit) in enumerate(
            scored[:final_top_k], start=1
        ):
            # 贡献按固定顺序输出，便于审计
            items_sorted = sorted(items, key=lambda c: c.path)

            results.append(
                HybridHit(
                    chunk_id=chunk_id,
                    rrf_score=total,
                    rank=rank,
                    policy_id=hit.policy_id,
                    document_id=hit.document_id,
                    version=hit.version,
                    page=hit.page,
                    start=hit.start,
                    end=hit.end,
                    title=hit.title,
                    source_key=hit.source_key,
                    source_url=hit.source_url,
                    content_sha256=hit.content_sha256,
                    text=hit.text,
                    contributions=items_sorted,
                )
            )

        return results

    def _build_notice(
        self,
        *,
        degraded: str | None,
        lexical_mode: str | None,
        dense_mode: str | None,
        consistency: dict,
    ) -> str:
        """构造面向模型的说明文案。"""

        parts = [
            f"混合检索（等权 RRF，rank_constant={self.rank_constant}）"
        ]

        if degraded is not None:
            parts.append(
                f"⚠ 降级模式（{degraded}）：本次不是完整混合检索"
            )
        else:
            parts.append(
                f"词法模式={lexical_mode}，向量模式={dense_mode}"
            )

        if not consistency.get("consistent", True):
            parts.append(f"索引不一致：{consistency.get('detail')}")

        parts.append("RRF 只融合排名，不判断证据充分性；未命中不等于政策不存在")

        return "；".join(parts)


__all__ = [
    "DEFAULT_CANDIDATE_K",
    "DEFAULT_FINAL_TOP_K",
    "DEFAULT_RANK_CONSTANT",
    "HybridError",
    "HybridHit",
    "HybridOutcome",
    "HybridRetriever",
    "PathContribution",
    "RetrieverFailure",
    "SCHEMA_VERSION",
    "rrf_contribution",
]
