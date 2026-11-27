"""MVP-02-B 政策研究与证据包专项测试。

覆盖项目负责人验收清单：
- 契约严格校验（拒绝路径/URL 等额外字段）
- evidence_id 由程序生成、防编造
- 证据核验（偏移 / 引文 / 哈希 / 文档版本）
- claim 必须引用有效证据
- 「原文匹配 ≠ 语义成立」的分层状态
- 地区适用性
- 政策时效不宣称「现行有效」
- 异常：无结果 / 索引过期 / 预算耗尽 / 结构化输出错误 / 部分完成
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from powercopy.research.agent import ResearchAgent
from powercopy.research.contracts import (
    MAX_QUESTIONS,
    Claim,
    ClaimStatus,
    EvidenceItem,
    EvidencePackage,
    EvidenceStatus,
    PolicyValidityStatus,
    ResearchBudget,
    ResearchQuestion,
    ResearchRequest,
    ResearchStatus,
    compute_evidence_id,
)
from powercopy.research.mock import (
    MockExtractor,
    MockPlanner,
    MockResearchLLM,
)
from powercopy.research.verification import (
    EvidenceVerifier,
    PolicyCorpus,
    VerificationReport,
)

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "policies_versioned.db"
MANIFEST = ROOT / "storage" / "rag" / "chunks.json"

HAS_REAL = DB.is_file() and MANIFEST.is_file()

needs_real = pytest.mark.skipif(
    not HAS_REAL, reason="缺少真实政策库或切片清单"
)


# ---------------------------------------------------------------- 夹具


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _first_chunk() -> dict:
    return _manifest()["chunks"][0]


@pytest.fixture(scope="module")
def corpus():
    if not HAS_REAL:
        pytest.skip("缺少真实数据")
    return PolicyCorpus(DB, MANIFEST)


@pytest.fixture(scope="module")
def verifier(corpus):
    return EvidenceVerifier(corpus)


def _make_item(chunk: dict, body: str, **overrides) -> EvidenceItem:
    start = chunk["start"]
    end = chunk["end"]
    quote = body[start:end]
    eid = compute_evidence_id(
        policy_id=chunk["policy_id"],
        version=chunk["version"],
        chunk_id=chunk["chunk_id"],
        start=start,
        end=end,
    )
    payload = {
        "evidence_id": eid,
        "policy_id": chunk["policy_id"],
        "document_id": chunk["document_id"],
        "version": chunk["version"],
        "chunk_id": chunk["chunk_id"],
        "source_url": chunk.get("source_url") or "",
        "title": chunk.get("title") or "",
        "exact_quote": quote,
        "start": start,
        "end": end,
        "page": chunk.get("page"),
        "content_sha256": chunk["content_sha256"],
    }
    payload.update(overrides)
    return EvidenceItem(**payload)


# ==========================================================================
# 1. 输入契约
# ==========================================================================
class TestResearchRequestContract:
    def test_minimal_valid(self):
        req = ResearchRequest(topic="电力市场注册规则")
        assert req.topic == "电力市场注册规则"
        assert req.required_questions == []

    def test_rejects_empty_topic(self):
        with pytest.raises(ValidationError):
            ResearchRequest(topic="")

    def test_rejects_topic_over_limit(self):
        with pytest.raises(ValidationError):
            ResearchRequest(topic="x" * 201)

    @pytest.mark.parametrize(
        "forbidden",
        [
            {"db_path": "/etc/passwd"},
            {"index_path": "storage/rag/dense.npz"},
            {"url": "https://evil.invalid/a"},
            {"file": "C:/secret.txt"},
            {"llm_api_key": "sk-xxx"},
            {"model_path": "/models/evil"},
        ],
    )
    def test_rejects_path_and_url_fields(self, forbidden):
        """研究任务不允许指定任意路径、URL 或密钥。"""
        with pytest.raises(ValidationError):
            ResearchRequest.model_validate({"topic": "x", **forbidden})

    def test_rejects_duplicate_questions(self):
        with pytest.raises(ValidationError, match="重复问题"):
            ResearchRequest(
                topic="x", required_questions=["同一问题", "同一问题"]
            )

    def test_rejects_too_many_questions(self):
        with pytest.raises(ValidationError):
            ResearchRequest(
                topic="x",
                required_questions=[f"问题{i}" for i in range(MAX_QUESTIONS + 1)],
            )

    def test_budget_bounds(self):
        with pytest.raises(ValidationError):
            ResearchBudget(max_tool_calls=0)
        with pytest.raises(ValidationError):
            ResearchBudget(max_tool_calls=999)
        with pytest.raises(ValidationError):
            ResearchBudget(max_questions=99)

    def test_strict_no_coercion(self):
        """strict=True：不接受字符串冒充整数。"""
        with pytest.raises(ValidationError):
            ResearchBudget(max_rounds="6")


# ==========================================================================
# 2. evidence_id 生成
# ==========================================================================
class TestEvidenceIdGeneration:
    def test_deterministic(self):
        a = compute_evidence_id(
            policy_id="p", version=1, chunk_id="c", start=0, end=10
        )
        b = compute_evidence_id(
            policy_id="p", version=1, chunk_id="c", start=0, end=10
        )
        assert a == b
        assert len(a) == 16

    def test_changes_with_input(self):
        base = compute_evidence_id(
            policy_id="p", version=1, chunk_id="c", start=0, end=10
        )
        assert base != compute_evidence_id(
            policy_id="p", version=2, chunk_id="c", start=0, end=10
        )
        assert base != compute_evidence_id(
            policy_id="p", version=1, chunk_id="c", start=1, end=11
        )
        assert base != compute_evidence_id(
            policy_id="q", version=1, chunk_id="c", start=0, end=10
        )

    @needs_real
    def test_fabricated_id_rejected(self, verifier):
        """模型编造的 evidence_id 必须被拒绝。"""
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        forged = item.model_copy(update={"evidence_id": "0" * 16})

        report = VerificationReport()
        out = verifier.verify_evidence(forged, report)
        assert out is None
        assert any(
            i.code == "EVIDENCE_ID_MISMATCH" for i in report.issues
        )


# ==========================================================================
# 3. 证据核验
# ==========================================================================
@needs_real
class TestEvidenceVerification:
    def test_valid_evidence_passes(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])

        report = VerificationReport()
        out = verifier.verify_evidence(item, report)

        assert out is not None
        assert out.status is EvidenceStatus.VERIFIED
        assert report.evidence_verified == 1

    def test_quote_mismatch_rejected(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        # 保持长度不变但改一个字，模拟断章取义
        tampered = item.exact_quote[:-1] + "X"
        bad = item.model_copy(update={"exact_quote": tampered})

        report = VerificationReport()
        assert verifier.verify_evidence(bad, report) is None
        assert any(i.code == "QUOTE_MISMATCH" for i in report.issues)

    def test_offset_out_of_range_rejected(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        length = len(record["body_text"])
        # 用超长偏移但保持引文长度一致
        bad = item.model_copy(
            update={
                "start": length - 1,
                "end": length - 1 + len(item.exact_quote),
            }
        )
        report = VerificationReport()
        # 偏移与清单不符，会先命中 EVIDENCE_ID_MISMATCH
        assert verifier.verify_evidence(bad, report) is None

    def test_hash_mismatch_rejected(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        bad = item.model_copy(update={"content_sha256": "f" * 64})
        report = VerificationReport()
        assert verifier.verify_evidence(bad, report) is None
        assert any(i.code == "CONTENT_SHA_MISMATCH" for i in report.issues)

    def test_unknown_version_rejected(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        # 用不存在的 version，同时保持其它字段自洽
        fake_version = 999
        eid = compute_evidence_id(
            policy_id=chunk["policy_id"],
            version=fake_version,
            chunk_id=chunk["chunk_id"],
            start=chunk["start"],
            end=chunk["end"],
        )
        bad = EvidenceItem(
            evidence_id=eid,
            policy_id=chunk["policy_id"],
            document_id=chunk["document_id"],
            version=fake_version,
            chunk_id=chunk["chunk_id"],
            exact_quote=record["body_text"][chunk["start"]:chunk["end"]],
            start=chunk["start"],
            end=chunk["end"],
            content_sha256=chunk["content_sha256"],
        )
        report = VerificationReport()
        assert verifier.verify_evidence(bad, report) is None
        assert any(
            i.code in ("VERSION_NOT_FOUND", "EVIDENCE_ID_MISMATCH")
            for i in report.issues
        )

    def test_unknown_chunk_rejected(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        bad = item.model_copy(update={"chunk_id": "not-a-real-chunk"})
        report = VerificationReport()
        assert verifier.verify_evidence(bad, report) is None
        assert any(
            i.code == "CHUNK_NOT_IN_MANIFEST" for i in report.issues
        )

    def test_all_real_chunks_pass(self, verifier):
        """全部 26 个切片的偏移与引文都能通过核验。"""
        passed = 0
        for chunk in _manifest()["chunks"]:
            record = verifier.corpus.fetch_version(
                chunk["policy_id"], chunk["version"]
            )
            item = _make_item(chunk, record["body_text"])
            report = VerificationReport()
            if verifier.verify_evidence(item, report) is not None:
                passed += 1
        assert passed == len(_manifest()["chunks"])


# ==========================================================================
# 4. Claim 引用核验
# ==========================================================================
@needs_real
class TestClaimVerification:
    def test_claim_with_valid_evidence_pending_review(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        report = VerificationReport()
        verified = verifier.verify_evidence(item, report)
        assert verified is not None

        claim = Claim(
            claim_id="c1",
            question_id="q1",
            statement="测试事实",
            evidence_ids=[verified.evidence_id],
        )
        results = verifier.verify_claims(
            [claim], {verified.evidence_id: verified}, report
        )

        assert len(results) == 1
        # 关键：引文核验通过 ≠ 语义成立
        assert results[0].status is ClaimStatus.PENDING_REVIEW
        assert "语义支持性" in results[0].support_note

    def test_claim_with_unknown_evidence_rejected(self, verifier):
        report = VerificationReport()
        claim = Claim(
            claim_id="c1",
            question_id="q1",
            statement="编造的事实",
            evidence_ids=["0" * 16],
        )
        results = verifier.verify_claims([claim], {}, report)

        assert results[0].status is ClaimStatus.REJECTED
        assert report.claims_rejected == 1

    def test_claim_requires_evidence(self):
        """Claim 契约层就要求至少一个 evidence_id。"""
        with pytest.raises(ValidationError):
            Claim(
                claim_id="c1",
                question_id="q1",
                statement="x",
                evidence_ids=[],
            )

    def test_claim_rejects_duplicate_evidence_ids(self):
        with pytest.raises(ValidationError, match="重复"):
            Claim(
                claim_id="c1",
                question_id="q1",
                statement="x",
                evidence_ids=["a" * 16, "a" * 16],
            )


# ==========================================================================
# 5. 时效与地区
# ==========================================================================
@needs_real
class TestValidityAndRegion:
    def test_never_claims_currently_effective(self, verifier):
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        report = VerificationReport()
        verified = verifier.verify_evidence(item, report)

        status, note = verifier.assess_validity([verified])

        assert status in (
            PolicyValidityStatus.LATEST_OBSERVED,
            PolicyValidityStatus.SUPERSEDED_IN_CORPUS,
        )
        # 明确禁止「现行有效」的表述
        assert "现行有效" not in note or "不代表" in note
        assert "官方公告" in note

    def test_no_evidence_unknown(self, verifier):
        status, note = verifier.assess_validity([])
        assert status is PolicyValidityStatus.UNKNOWN

    def test_region_not_requested_is_applicable(self, verifier):
        info = verifier.assess_region_applicability("", [])
        assert info["applicable"] is True
        assert info["status"] == "no_region_requested"

    def test_region_unlabeled_not_applicable(self, verifier):
        """库中政策 region 均为 None：不得声称适用于江西。"""
        chunk = _first_chunk()
        record = verifier.corpus.fetch_version(
            chunk["policy_id"], chunk["version"]
        )
        item = _make_item(chunk, record["body_text"])
        report = VerificationReport()
        verified = verifier.verify_evidence(item, report)

        info = verifier.assess_region_applicability("江西", [verified])

        assert info["applicable"] is False
        assert info["status"] in ("unlabeled", "unmatched")
        assert "不等于" in info["detail"]


# ==========================================================================
# 6. 端到端（Mock LLM + 真实库）
# ==========================================================================
@needs_real
class TestEndToEnd:
    @pytest.fixture(scope="class")
    @classmethod
    def registry(cls):
        from powercopy.tools.builtin.policy_tools import build_live_registry

        return build_live_registry(DB)

    def _run(self, registry, request, **mock_kwargs):
        agent = ResearchAgent(
            MockResearchLLM(**mock_kwargs),
            registry,
            PolicyCorpus(DB, MANIFEST),
            budget=request.task_budget,
        )
        return agent.run(request)

    def test_default_scenario(self, registry):
        request = ResearchRequest(
            topic="工商业企业参与电力市场前，需要了解哪些基本规则？",
            target_audience="工商业企业",
            task_budget=ResearchBudget(max_questions=3, max_tool_calls=6),
        )
        pkg = self._run(registry, request, max_questions=3)

        assert isinstance(pkg, EvidencePackage)
        assert pkg.research_id.startswith("res-")
        assert pkg.claims, "应产出候选事实"
        assert pkg.evidence_items, "应产出证据"
        # 所有 claim 都引用了有效证据
        verified_ids = {e.evidence_id for e in pkg.evidence_items}
        for claim in pkg.claims:
            assert set(claim.evidence_ids) <= verified_ids
        # 语义支持性一律待确认
        assert all(
            c.status is ClaimStatus.PENDING_REVIEW for c in pkg.claims
        )

    def test_all_evidence_verified(self, registry):
        request = ResearchRequest(
            topic="电力市场注册规则",
            task_budget=ResearchBudget(max_questions=2, max_tool_calls=4),
        )
        pkg = self._run(registry, request, max_questions=2)
        assert pkg.evidence_items
        assert all(
            e.status is EvidenceStatus.VERIFIED for e in pkg.evidence_items
        )

    def test_insufficient_region_reverts_questions(self, registry):
        """地区无法确认时，问题必须退回未解决。"""
        request = ResearchRequest(
            topic="江西省售电公司参与电力现货市场的交易规则是什么？",
            region="江西",
            task_budget=ResearchBudget(max_questions=2, max_tool_calls=4),
        )
        pkg = self._run(registry, request, max_questions=2)

        assert pkg.research_status is ResearchStatus.PARTIAL
        assert pkg.unanswered_questions
        assert all(not q.answered for q in pkg.research_questions)
        assert any("地区适用性" in u for u in pkg.uncertainties)

    def test_validity_note_present(self, registry):
        request = ResearchRequest(
            topic="电力市场注册规则",
            task_budget=ResearchBudget(max_questions=2, max_tool_calls=4),
        )
        pkg = self._run(registry, request, max_questions=2)
        assert pkg.policy_validity_note
        assert "不代表" in pkg.policy_validity_note

    def test_json_serializable(self, registry):
        request = ResearchRequest(
            topic="电力市场注册规则",
            task_budget=ResearchBudget(max_questions=2, max_tool_calls=4),
        )
        pkg = self._run(registry, request, max_questions=2)
        text = json.dumps(pkg.model_dump(mode="json"), ensure_ascii=False)
        assert "research_id" in text
        # 可反序列化回契约
        restored = EvidencePackage.model_validate(json.loads(text))
        assert restored.research_id == pkg.research_id


# ==========================================================================
# 7. 异常处理
# ==========================================================================
@needs_real
class TestFailureHandling:
    @pytest.fixture(scope="class")
    @classmethod
    def registry(cls):
        from powercopy.tools.builtin.policy_tools import build_live_registry

        return build_live_registry(DB)

    def test_invalid_structured_output(self, registry):
        """模型输出不可解析时必须产出带原因的证据包，不崩溃。"""
        request = ResearchRequest(
            topic="电力市场注册规则",
            task_budget=ResearchBudget(max_questions=2, max_tool_calls=4),
        )
        agent = ResearchAgent(
            MockResearchLLM(fail_mode="invalid_json"),
            registry,
            PolicyCorpus(DB, MANIFEST),
            budget=request.task_budget,
        )
        pkg = agent.run(request)

        assert pkg.research_status is ResearchStatus.FAILED
        assert pkg.status_reason
        assert pkg.uncertainties

    def test_empty_model_output(self, registry):
        request = ResearchRequest(
            topic="电力市场注册规则",
            task_budget=ResearchBudget(max_questions=2, max_tool_calls=4),
        )
        agent = ResearchAgent(
            MockResearchLLM(fail_mode="empty"),
            registry,
            PolicyCorpus(DB, MANIFEST),
            budget=request.task_budget,
        )
        pkg = agent.run(request)
        assert pkg.research_status is ResearchStatus.FAILED

    def test_budget_exhausted_partial_report(self, registry):
        """工具预算为 1 时，只能处理一个问题，其余记入未解决。"""
        request = ResearchRequest(
            topic="电力市场注册规则",
            task_budget=ResearchBudget(
                max_questions=4, max_tool_calls=1
            ),
        )
        agent = ResearchAgent(
            MockResearchLLM(max_questions=4),
            registry,
            PolicyCorpus(DB, MANIFEST),
            budget=request.task_budget,
        )
        pkg = agent.run(request)

        assert pkg.research_status is ResearchStatus.PARTIAL
        assert pkg.unanswered_questions
        assert any("预算" in u for u in pkg.uncertainties)

    def test_no_search_hit(self, registry):
        """检索候选与问题无关时，不编造事实。

        混合检索必然返回 Top-K，即使问题与语料完全无关。
        Mock 用字面重叠做闸门（非语义判断），因此这里用一段
        与中文政策语料不可能有字符重叠的查询。
        """
        request = ResearchRequest(
            topic="zzzz qqqq xxxx vvvv",
            required_questions=["zzzzqqqq xxxxvvvv wwwwyyyy"],
            task_budget=ResearchBudget(max_questions=1, max_tool_calls=2),
        )
        agent = ResearchAgent(
            MockResearchLLM(max_questions=1),
            registry,
            PolicyCorpus(DB, MANIFEST),
            budget=request.task_budget,
        )
        pkg = agent.run(request)

        # 无命中时不应有 claim（或 claim 引用全部失效被拒）
        assert all(
            c.status is ClaimStatus.REJECTED for c in pkg.claims
        ) or not pkg.claims

    def test_required_questions_respected(self, registry):
        """用户指定问题时，不调用规划器。"""
        request = ResearchRequest(
            topic="电力市场注册规则",
            required_questions=["注册需要哪些材料？"],
            task_budget=ResearchBudget(max_questions=3, max_tool_calls=4),
        )
        agent = ResearchAgent(
            MockResearchLLM(max_questions=3),
            registry,
            PolicyCorpus(DB, MANIFEST),
            budget=request.task_budget,
        )
        pkg = agent.run(request)

        assert len(pkg.research_questions) == 1
        assert pkg.research_questions[0].text == "注册需要哪些材料？"


# ==========================================================================
# 8. Mock 组件
# ==========================================================================
class TestMockComponents:
    def test_planner_deterministic(self):
        from types import SimpleNamespace

        planner = MockPlanner(max_questions=3)
        request = SimpleNamespace(
            topic="测试主题", target_audience="企业", region=""
        )
        a = planner.plan(request)
        b = planner.plan(request)
        assert a == b
        assert len(a) <= 3
        assert all("text" in q for q in a)

    def test_extractor_quotes_are_substrings(self):
        """Mock 提取的引文必须是原文子串，否则会被核验器拒绝。"""
        extractor = MockExtractor(quote_chars=20)
        text = "第一条 交易周期为15分钟。第二条 电价为3元。"
        hits = [
            {
                "policy_id": "p",
                "version": 1,
                "chunk_id": "c",
                "start": 0,
                "end": len(text),
                "text": text,
            }
        ]
        eid = compute_evidence_id(
            policy_id="p", version=1, chunk_id="c", start=0, end=len(text)
        )
        claims = extractor.extract(
            "交易周期", hits, {eid: object()}
        )
        assert claims
        quote = claims[0]["statement"].split("：", 1)[1]
        assert quote.strip() in " ".join(text.split())

    def test_extractor_skips_unknown_evidence(self):
        extractor = MockExtractor()
        hits = [
            {
                "policy_id": "p",
                "version": 1,
                "chunk_id": "c",
                "start": 0,
                "end": 10,
                "text": "xxxxxxxxxx",
            }
        ]
        # 空 evidence_by_id → 不产出 claim
        assert extractor.extract("q", hits, {}) == []


# ==========================================================================
# 9. 数据契约完整性
# ==========================================================================
class TestPackageContract:
    def test_required_fields(self):
        pkg = EvidencePackage(
            research_id="res-abcdefgh",
            topic="测试主题",
            research_status=ResearchStatus.FAILED,
        )
        for field in (
            "research_id",
            "topic",
            "research_questions",
            "claims",
            "evidence_items",
            "unanswered_questions",
            "uncertainties",
            "policy_validity_status",
            "research_status",
            "trace_reference",
            "created_at",
        ):
            assert hasattr(pkg, field), field

    def test_created_at_is_iso(self):
        pkg = EvidencePackage(
            research_id="res-abcdefgh", topic="x"
        )
        assert "T" in pkg.created_at

    def test_summary_shape(self):
        pkg = EvidencePackage(
            research_id="res-abcdefgh", topic="x"
        )
        summary = pkg.summary()
        assert summary["research_id"] == "res-abcdefgh"
        assert summary["claims_total"] == 0

    def test_evidence_item_span_consistency(self):
        with pytest.raises(ValidationError, match="引文长度"):
            EvidenceItem(
                evidence_id="a" * 16,
                policy_id="p",
                document_id="d",
                version=1,
                chunk_id="c",
                exact_quote="短引文",
                start=0,
                end=999,
                content_sha256="0" * 64,
            )

    def test_evidence_item_end_must_exceed_start(self):
        with pytest.raises(ValidationError, match="end 必须大于 start"):
            EvidenceItem(
                evidence_id="a" * 16,
                policy_id="p",
                document_id="d",
                version=1,
                chunk_id="c",
                exact_quote="abcdef",
                start=5,
                end=5,
                content_sha256="0" * 64,
            )
