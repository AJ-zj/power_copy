"""最小证据核验。

核验什么，不核验什么
--------------------
**程序能可靠判断的**（本模块做）：

1. `evidence_id` 是否等于由 canonical 字段重算的值 —— 防止模型编造 ID
2. 引用的政策 / 文档 / 版本是否真实存在于本地库
3. 字符偏移是否在该政策原文范围内
4. `exact_quote` 是否与原文 `[start, end)` 完全一致
5. `content_sha256` 是否与库中记录一致

**程序无法可靠判断的**（本模块不做，标记 `pending_review`）：

- 该引文在**语义上**是否支持某条 claim

这一点必须反复说明：**原文匹配成功 ≠ 事实在语义上成立**。
例如引文确实是政策原文，但被断章取义、或与结论方向相反，
程序无法发现。因此 `VERIFIED` 只表示「引文真实存在」，
`ClaimStatus.PENDING_REVIEW` 表示「引文真实，但语义支持需人工确认」。

数据来源
--------
不重新发明读取逻辑，直接使用本地政策库（只读）与切片清单：

- 原文来自 `policy_documents.body_text`（配合 manifest 的 start/end 偏移）
- 校验信息来自 `policy_versions`（document_id / content_sha256）
- chunk 元信息来自切片清单 `chunks.json`

切片偏移是相对该政策**完整正文**的字符位置，因此可以直接用
`body_text[start:end]` 取到引文。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from powercopy.research.contracts import (
    Claim,
    ClaimStatus,
    EvidenceItem,
    EvidencePackage,
    EvidenceStatus,
    PolicyValidityStatus,
    ResearchStatus,
    VerificationIssue,
    compute_evidence_id,
)

# 问题代码
ISSUE_UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE_ID"
ISSUE_EVIDENCE_ID_MISMATCH = "EVIDENCE_ID_MISMATCH"
ISSUE_POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
ISSUE_VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
ISSUE_DOCUMENT_MISMATCH = "DOCUMENT_ID_MISMATCH"
ISSUE_OFFSET_OUT_OF_RANGE = "OFFSET_OUT_OF_RANGE"
ISSUE_QUOTE_MISMATCH = "QUOTE_MISMATCH"
ISSUE_HASH_MISMATCH = "CONTENT_SHA_MISMATCH"
ISSUE_CLAIM_NO_EVIDENCE = "CLAIM_WITHOUT_EVIDENCE"
ISSUE_CLAIM_BAD_REFERENCE = "CLAIM_REFERENCES_BAD_EVIDENCE"
ISSUE_CHUNK_NOT_IN_MANIFEST = "CHUNK_NOT_IN_MANIFEST"


@dataclass
class VerificationReport:
    """核验结果。

    `issues` 是**问题清单**，不是异常 —— 核验不通过不是程序错误，
    而是研究结果的一部分（该证据被标记 REJECTED 并记录原因）。
    """

    issues: list[VerificationIssue] = field(default_factory=list)
    evidence_verified: int = 0
    evidence_rejected: int = 0
    claims_verified: int = 0
    claims_pending: int = 0
    claims_rejected: int = 0

    def add(self, scope: str, code: str, target_id: str = "", detail: str = "") -> None:
        self.issues.append(
            VerificationIssue(
                scope=scope, target_id=target_id, code=code, detail=detail
            )
        )

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": [i.model_dump() for i in self.issues],
            "evidence_verified": self.evidence_verified,
            "evidence_rejected": self.evidence_rejected,
            "claims_verified": self.claims_verified,
            "claims_pending": self.claims_pending,
            "claims_rejected": self.claims_rejected,
        }


class PolicyCorpus:
    """本地政策库（只读）与切片清单的封装。

    所有查询都用参数化 SQL；连接以 `mode=ro` + `PRAGMA query_only=ON` 打开。
    """

    def __init__(
        self,
        db_path: str | Path,
        manifest_path: str | Path,
    ):
        self.db_path = Path(db_path).resolve()
        self.manifest_path = Path(manifest_path)
        self._manifest: dict | None = None
        self._chunks_by_id: dict[str, dict] = {}

    # ---- 清单 ----

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            if not self.manifest_path.is_file():
                raise FileNotFoundError(f"切片清单不存在：{self.manifest_path}")
            self._manifest = json.loads(
                self.manifest_path.read_text(encoding="utf-8")
            )
            for chunk in self._manifest.get("chunks") or []:
                self._chunks_by_id[chunk["chunk_id"]] = chunk
        return self._manifest

    def chunk(self, chunk_id: str) -> dict | None:
        self.manifest  # 触发加载
        return self._chunks_by_id.get(chunk_id)

    # ---- 数据库 ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path.as_uri() + "?mode=ro", uri=True, timeout=10
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def fetch_version(
        self, policy_id: str, version: int
    ) -> dict | None:
        """取某个政策版本及其正文。"""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT v.policy_id, v.version, v.document_id,
                       v.content_sha256, d.title, d.body_text,
                       d.source_url
                FROM policy_versions v
                JOIN policy_documents d ON d.document_id = v.document_id
                WHERE v.policy_id = ? AND v.version = ?
                """,
                (policy_id, version),
            ).fetchone()
        return dict(row) if row else None

    def latest_version(self, policy_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM policy_versions WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
        return int(row["v"]) if row and row["v"] is not None else None

    def policy_exists(self, policy_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM policy_identities WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
        return row is not None

    def available(self) -> bool:
        return self.db_path.is_file()


class EvidenceVerifier:
    """对证据包做最小核验，并回填 canonical evidence_id。

    使用顺序（由 ResearchAgent 调用）：

    1. `verify_evidence(...)` —— 逐条核验证据，返回通过的证据
    2. `verify_claims(...)` —— 校验 claim 引用的 evidence_id 都真实有效
    3. `finalize(...)` —— 汇总状态、判定 research_status
    """

    def __init__(self, corpus: PolicyCorpus):
        self.corpus = corpus

    # ---- 单条证据 ----

    def verify_evidence(
        self, item: EvidenceItem, report: VerificationReport
    ) -> EvidenceItem | None:
        """核验一条证据。通过返回 VERIFIED 的副本，失败返回 None。"""

        # 1. chunk 必须在清单中
        chunk = self.corpus.chunk(item.chunk_id)
        if chunk is None:
            report.add(
                "evidence", ISSUE_CHUNK_NOT_IN_MANIFEST, item.chunk_id,
                "该 chunk_id 不在当前切片清单中，索引可能已过期",
            )
            report.evidence_rejected += 1
            return None

        # 2. 清单记录的元信息必须与证据一致
        mismatches = []
        if chunk["policy_id"] != item.policy_id:
            mismatches.append(
                f"policy_id 清单={chunk['policy_id']} 证据={item.policy_id}"
            )
        if int(chunk["version"]) != item.version:
            mismatches.append(
                f"version 清单={chunk['version']} 证据={item.version}"
            )
        if chunk["document_id"] != item.document_id:
            mismatches.append(
                f"document_id 清单={chunk['document_id']} 证据={item.document_id}"
            )
        if int(chunk["start"]) != item.start or int(chunk["end"]) != item.end:
            mismatches.append(
                f"偏移 清单=[{chunk['start']},{chunk['end']}) "
                f"证据=[{item.start},{item.end})"
            )
        if mismatches:
            report.add(
                "evidence", ISSUE_EVIDENCE_ID_MISMATCH, item.evidence_id,
                "；".join(mismatches),
            )
            report.evidence_rejected += 1
            return None

        # 3. 政策版本必须真实存在
        record = self.corpus.fetch_version(item.policy_id, item.version)
        if record is None:
            code = (
                ISSUE_POLICY_NOT_FOUND
                if not self.corpus.policy_exists(item.policy_id)
                else ISSUE_VERSION_NOT_FOUND
            )
            report.add(
                "evidence", code, item.evidence_id,
                f"policy_id={item.policy_id} version={item.version}",
            )
            report.evidence_rejected += 1
            return None

        # 4. document_id 与 content_sha256 必须与库一致
        if record["document_id"] != item.document_id:
            report.add(
                "evidence", ISSUE_DOCUMENT_MISMATCH, item.evidence_id,
                f"库={record['document_id']} 证据={item.document_id}",
            )
            report.evidence_rejected += 1
            return None

        if record["content_sha256"] != item.content_sha256:
            report.add(
                "evidence", ISSUE_HASH_MISMATCH, item.evidence_id,
                f"库={record['content_sha256'][:16]}... "
                f"证据={item.content_sha256[:16]}...",
            )
            report.evidence_rejected += 1
            return None

        # 5. 偏移必须在正文范围内
        body = record["body_text"] or ""
        if item.start < 0 or item.end > len(body):
            report.add(
                "evidence", ISSUE_OFFSET_OUT_OF_RANGE, item.evidence_id,
                f"区间 [{item.start},{item.end}) 超出正文长度 {len(body)}",
            )
            report.evidence_rejected += 1
            return None

        # 6. 引文必须与原文逐字一致
        actual = body[item.start : item.end]
        if actual != item.exact_quote:
            report.add(
                "evidence", ISSUE_QUOTE_MISMATCH, item.evidence_id,
                f"原文={actual[:40]!r} 引文={item.exact_quote[:40]!r}",
            )
            report.evidence_rejected += 1
            return None

        # 7. evidence_id 必须等于 canonical 值（防编造）
        canonical = compute_evidence_id(
            policy_id=item.policy_id,
            version=item.version,
            chunk_id=item.chunk_id,
            start=item.start,
            end=item.end,
        )
        if item.evidence_id != canonical:
            report.add(
                "evidence", ISSUE_EVIDENCE_ID_MISMATCH, item.evidence_id,
                f"canonical={canonical}",
            )
            report.evidence_rejected += 1
            return None

        report.evidence_verified += 1
        return item.model_copy(
            update={
                "status": EvidenceStatus.VERIFIED,
                # 用库里的权威字段覆盖，避免模型传错
                "title": record["title"] or item.title,
                "source_url": record["source_url"] or item.source_url,
            }
        )

    # ---- claim 引用 ----

    def verify_claims(
        self,
        claims: list[Claim],
        verified_evidence: dict[str, EvidenceItem],
        report: VerificationReport,
    ) -> list[Claim]:
        """校验 claim 引用的证据 ID 均真实有效。

        注意：这里**只**校验引用完整性。语义支持性一律保持 pending_review，
        因为程序无法可靠判断「这段引文是否支持这个结论」。
        """

        results: list[Claim] = []

        for claim in claims:
            if not claim.evidence_ids:
                report.add(
                    "claim", ISSUE_CLAIM_NO_EVIDENCE, claim.claim_id,
                    "claim 未关联任何证据",
                )
                report.claims_rejected += 1
                results.append(
                    claim.model_copy(
                        update={
                            "status": ClaimStatus.REJECTED,
                            "support_note": "未关联证据",
                        }
                    )
                )
                continue

            bad = [
                eid for eid in claim.evidence_ids if eid not in verified_evidence
            ]
            if bad:
                report.add(
                    "claim", ISSUE_CLAIM_BAD_REFERENCE, claim.claim_id,
                    f"引用了无效证据 ID：{bad}",
                )
                report.claims_rejected += 1
                results.append(
                    claim.model_copy(
                        update={
                            "status": ClaimStatus.REJECTED,
                            "support_note": f"引用了未通过核验的证据：{bad}",
                        }
                    )
                )
                continue

            # 引文真实存在，但语义支持性无法自动判定
            report.claims_pending += 1
            results.append(
                claim.model_copy(
                    update={
                        "status": ClaimStatus.PENDING_REVIEW,
                        "support_note": (
                            claim.support_note
                            or "引文已核验与原文一致；语义支持性需人工确认"
                        ),
                    }
                )
            )

        return results

    # ---- 地区适用性 ----

    def assess_region_applicability(
        self,
        region: str,
        evidence: list[EvidenceItem],
    ) -> dict:
        """评估证据是否适用于指定地区。

        **这解决一个真实问题**：库里可能只有国家级政策，`region` 字段为
        `None`（未标注）。此时检索仍会返回候选，但「未标注地区」不等于
        「全国适用」，更不等于「适用于江西」。

        Returns:
            {
              "applicable": bool,
              "status": str,     # no_region_requested / matched /
                                 # unmatched / unlabeled
              "detail": str,
              "matched": int,
              "unlabeled": int,
              "mismatched": int,
            }
        """

        if not region:
            return {
                "applicable": True,
                "status": "no_region_requested",
                "detail": "本次研究未指定地区",
                "matched": 0,
                "unlabeled": 0,
                "mismatched": 0,
            }

        if not evidence:
            return {
                "applicable": False,
                "status": "unmatched",
                "detail": f"无证据可用于判断是否适用于「{region}」",
                "matched": 0,
                "unlabeled": 0,
                "mismatched": 0,
            }

        matched = 0
        unlabeled = 0
        mismatched = 0

        for item in evidence:
            chunk = self.corpus.chunk(item.chunk_id)
            chunk_region = (chunk or {}).get("region")
            if not chunk_region:
                unlabeled += 1
            elif region in str(chunk_region):
                matched += 1
            else:
                mismatched += 1

        if matched and not unlabeled and not mismatched:
            return {
                "applicable": True,
                "status": "matched",
                "detail": f"全部 {matched} 条证据的地区标注与「{region}」一致",
                "matched": matched,
                "unlabeled": 0,
                "mismatched": 0,
            }

        if matched:
            return {
                "applicable": True,
                "status": "matched",
                "detail": (
                    f"{matched} 条证据地区匹配「{region}」，"
                    f"另有 {unlabeled} 条未标注地区、{mismatched} 条地区不符"
                ),
                "matched": matched,
                "unlabeled": unlabeled,
                "mismatched": mismatched,
            }

        # 没有任何一条证据能确认适用于该地区
        detail = (
            f"没有任何证据的地区标注匹配「{region}」："
            f"{unlabeled} 条未标注地区、{mismatched} 条地区不符。"
            f"未标注地区不等于全国适用，更不等于适用于「{region}」"
        )
        return {
            "applicable": False,
            "status": "unlabeled" if unlabeled and not mismatched else "unmatched",
            "detail": detail,
            "matched": 0,
            "unlabeled": unlabeled,
            "mismatched": mismatched,
        }

    # ---- 汇总 ----

    def assess_validity(
        self, evidence: list[EvidenceItem]
    ) -> tuple[PolicyValidityStatus, str]:
        """评估证据涉及政策的时效状态。

        **绝不说「现行有效」**：本地库只有观测记录，没有废止信息。
        """

        if not evidence:
            return (
                PolicyValidityStatus.UNKNOWN,
                "无证据，无法判断政策时效",
            )

        superseded: list[str] = []
        for item in evidence:
            latest = self.corpus.latest_version(item.policy_id)
            if latest is not None and latest > item.version:
                superseded.append(f"{item.policy_id[:12]} v{item.version}")

        if superseded:
            return (
                PolicyValidityStatus.SUPERSEDED_IN_CORPUS,
                "库中存在更新的政策版本：" + "、".join(sorted(set(superseded))),
            )

        return (
            PolicyValidityStatus.LATEST_OBSERVED,
            "引用的是本地库中该政策的最新观测版本；"
            "这不代表该政策现行有效，是否废止需以官方公告为准",
        )

    def decide_status(
        self,
        *,
        questions: list,
        claims: list[Claim],
        unanswered: list[str],
        report: VerificationReport,
    ) -> tuple[ResearchStatus, str]:
        """判定研究完成度。"""

        if not claims and not unanswered and not questions:
            return ResearchStatus.FAILED, "研究未产生任何问题或事实"

        answered = [q for q in questions if getattr(q, "answered", False)]
        pending = report.claims_pending
        rejected = report.claims_rejected

        if not questions:
            return ResearchStatus.FAILED, "未拆解出任何研究问题"

        if len(answered) == len(questions) and rejected == 0 and not unanswered:
            if pending:
                return (
                    ResearchStatus.PARTIAL,
                    f"全部问题均有证据支撑，但 {pending} 条事实的语义支持性"
                    f"待人工确认",
                )
            return ResearchStatus.COMPLETED, "全部研究问题均已解决"

        parts = []
        if unanswered:
            parts.append(f"{len(unanswered)} 个问题未解决")
        if rejected:
            parts.append(f"{rejected} 条事实因证据核验失败被拒绝")
        if pending:
            parts.append(f"{pending} 条事实待人工确认语义支持性")
        if not parts:
            parts.append("研究未完整覆盖全部问题")

        return ResearchStatus.PARTIAL, "；".join(parts)


def build_package(
    *,
    research_id: str,
    request,
    questions: list,
    claims: list[Claim],
    evidence: list[EvidenceItem],
    unanswered: list[str],
    uncertainties: list[str],
    report: VerificationReport,
    trace_reference: dict,
    status: ResearchStatus,
    status_reason: str,
    validity: PolicyValidityStatus,
    validity_note: str,
) -> EvidencePackage:
    """组装最终证据包。"""

    return EvidencePackage(
        research_id=research_id,
        topic=request.topic,
        region=request.region,
        target_audience=request.target_audience,
        research_questions=questions,
        claims=claims,
        evidence_items=evidence,
        unanswered_questions=unanswered,
        uncertainties=uncertainties,
        policy_validity_status=validity,
        policy_validity_note=validity_note,
        research_status=status,
        status_reason=status_reason,
        verification_issues=report.issues,
        trace_reference=trace_reference,
    )


__all__ = [
    "EvidenceVerifier",
    "PolicyCorpus",
    "VerificationReport",
    "build_package",
]
