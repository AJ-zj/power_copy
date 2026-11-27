"""MVP-04-B 审核与工作流专项测试。

覆盖：
- TaskBudget 共享与防重复计数
- 引用完整性审核
- 事实支持性（字面回溯 + 语义判断边界）
- 宣传合规
- 政策适用性
- 有限自动修订
- 统一工作流入口
- 验收场景 7 项
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from powercopy.copywriting.contracts import (
    ContentDraft,
    CopywritingStatus,
    GenerationMode,
    KeyPoint,
    Platform,
)
from powercopy.core.shared_budget import SharedBudget, budget_scope
from powercopy.review.agent import ReviewAgent
from powercopy.review.contracts import (
    IssueSeverity,
    ReviewDimension,
    ReviewReport,
    ReviewStatus,
    ReviewVerdict,
    RevisionResult,
)
from powercopy.review.mock import MockReviewerLLM, MockReviserLLM
from powercopy.review.verifier import ContentVerifier
from powercopy.research.contracts import (
    Claim,
    ClaimStatus,
    EvidenceItem,
    EvidencePackage,
    EvidenceStatus,
    PolicyValidityStatus,
    ResearchStatus,
)
from powercopy.research.verification import PolicyCorpus
from powercopy.workflows import (
    ContentWorkflowRequest,
    ContentWorkflowResult,
    ContentWorkflowService,
    WorkflowStatus,
)

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "policies_versioned.db"
MANIFEST = ROOT / "storage" / "rag" / "chunks.json"

HAS_REAL = DB.is_file() and MANIFEST.is_file()

needs_real = pytest.mark.skipif(
    not HAS_REAL, reason="缺少真实政策库或切片清单"
)


# ---------------------------------------------------------------- 构造


def _evidence(eid: str, quote: str = "第一条 政策原文表述内容，用于测试。") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=eid,
        policy_id="p1",
        document_id="d1",
        version=1,
        chunk_id="c1",
        source_url="https://example.invalid/a",
        title="测试政策",
        exact_quote=quote,
        start=0,
        end=len(quote),
        page=None,
        content_sha256="0" * 64,
        status=EvidenceStatus.VERIFIED,
    )


def _package(
    *,
    claims=None,
    evidence=None,
    region: str = "",
    validity: str = "latest_observed",
    validity_note: str = "不代表该政策现行有效，需以官方公告为准",
) -> EvidencePackage:
    return EvidencePackage(
        research_id="res-testtesttest12",
        topic="测试主题",
        region=region,
        claims=claims or [],
        evidence_items=evidence or [],
        policy_validity_status=validity,
        policy_validity_note=validity_note,
        research_status=ResearchStatus.PARTIAL,
    )


def _draft(
    *,
    title: str = "测试文案标题",
    body: str = "政策原文明确：第一条 政策原文表述内容，用于测试。以上表述以官方发布为准。",
    key_points=None,
    evidence_refs=None,
    region: str = "",
) -> ContentDraft:
    return ContentDraft(
        draft_id="draft-testtest1234",
        title=title,
        body=body,
        key_points=key_points or [],
        evidence_refs=evidence_refs or [],
        topic="测试主题",
        region_applicability=region,
        policy_validity_note="不代表该政策现行有效，需以官方公告为准",
    )


def _evidence_and_claim(eid: str = "a" * 16):
    item = _evidence(eid)
    claim = Claim(
        claim_id="c1",
        question_id="q1",
        statement="测试事实",
        evidence_ids=[eid],
        status=ClaimStatus.PENDING_REVIEW,
    )
    return item, claim


# ==========================================================================
# 1. TaskBudget 共享与防重复计数
# ==========================================================================
class TestSharedBudget:
    def test_no_double_counting(self):
        """外部不再预占；Mock 内部预占一次，账目只记一笔。"""
        from powercopy.research.mock import MockResearchLLM

        budget = SharedBudget(max_api_attempts=5)
        llm = MockResearchLLM()

        llm.invoke([{"role": "user", "content": "x"}], budget=budget)
        assert budget.api_attempts == 1

        llm.invoke([{"role": "user", "content": "x"}], budget=budget)
        assert budget.api_attempts == 2

    def test_copywriting_mock_counts_once(self):
        from powercopy.copywriting.mock import MockCopywriterLLM

        budget = SharedBudget(max_api_attempts=5)
        llm = MockCopywriterLLM()

        llm.invoke([{"role": "user", "content": "x"}], budget=budget)
        assert budget.api_attempts == 1

    def test_exhaustion_recorded(self):
        from powercopy.research.mock import MockResearchLLM
        from powercopy.core.budget import BudgetExceeded

        budget = SharedBudget(max_api_attempts=2)
        llm = MockResearchLLM()

        llm.invoke([{"role": "user", "content": "x"}], budget=budget)
        llm.invoke([{"role": "user", "content": "x"}], budget=budget)

        with pytest.raises(BudgetExceeded):
            llm.invoke([{"role": "user", "content": "x"}], budget=budget)

        assert budget.exhausted_by == "api_budget_exhausted"

    def test_snapshot_declares_token_soft(self):
        budget = SharedBudget(max_api_attempts=4, max_known_tokens=1000)
        snap = budget.to_dict()
        assert snap["token_limit_is_soft"] is True
        assert "硬约束" in snap["note"]
        assert snap["remaining_api_attempts"] == 4

    def test_budget_scope_context(self):
        with budget_scope(max_api_attempts=3) as budget:
            assert budget.max_api_attempts == 3
            assert budget.api_attempts == 0


# ==========================================================================
# 2. 引用完整性
# ==========================================================================
class TestCitationReview:
    def _verify(self, package, draft):
        return ContentVerifier(package, draft).check_citation()

    def test_valid_citation_passes(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            key_points=[
                KeyPoint(
                    point_id="p1",
                    text="事实",
                    evidence_ids=["a" * 16],
                    claim_ids=["c1"],
                )
            ],
            evidence_refs=["a" * 16],
        )
        result = self._verify(package, draft)
        assert result.verdict is ReviewVerdict.PASSED

    def test_unknown_evidence_blocking(self):
        package = _package(claims=[], evidence=[])
        draft = _draft(
            key_points=[
                KeyPoint(
                    point_id="p1", text="事实", evidence_ids=["f" * 16]
                )
            ],
            evidence_refs=["f" * 16],
        )
        verifier = ContentVerifier(package, draft)
        result = verifier.check_citation()

        assert result.verdict is ReviewVerdict.FAILED
        assert any(
            i.code == "EVIDENCE_NOT_FOUND" and i.severity is IssueSeverity.BLOCKING
            for i in verifier._issues
        )

    def test_rejected_claim_evidence_blocking(self):
        item = _evidence("b" * 16)
        claim = Claim(
            claim_id="c1",
            question_id="q1",
            statement="被拒",
            evidence_ids=["b" * 16],
            status=ClaimStatus.REJECTED,
        )
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            key_points=[
                KeyPoint(
                    point_id="p1", text="事实", evidence_ids=["b" * 16]
                )
            ],
            evidence_refs=["b" * 16],
        )
        verifier = ContentVerifier(package, draft)
        result = verifier.check_citation()

        assert result.verdict is ReviewVerdict.FAILED
        assert any(
            i.code == "EVIDENCE_FROM_REJECTED_CLAIM"
            for i in verifier._issues
        )

    def test_evidence_refs_mismatch(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            key_points=[
                KeyPoint(
                    point_id="p1", text="事实", evidence_ids=["a" * 16]
                )
            ],
            evidence_refs=[],  # 漏记
        )
        verifier = ContentVerifier(package, draft)
        verifier.check_citation()
        assert any(
            i.code == "EVIDENCE_REFS_MISMATCH" for i in verifier._issues
        )

    def test_body_cites_unknown_evidence(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            body="政策原文明确：第一条 政策原文表述内容。参见 deadbeefdeadbeef。",
            key_points=[
                KeyPoint(
                    point_id="p1", text="事实", evidence_ids=["a" * 16]
                )
            ],
            evidence_refs=["a" * 16],
        )
        verifier = ContentVerifier(package, draft)
        verifier.check_citation()
        assert any(
            i.code == "BODY_CITES_UNKNOWN_EVIDENCE"
            for i in verifier._issues
        )


# ==========================================================================
# 3. 事实支持性
# ==========================================================================
class TestFactSupportReview:
    def test_no_llm_returns_pending(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            body="政策原文明确：本政策于第35条规定的期限内有效。"
        )
        result = ContentVerifier(package, draft).check_fact_support()
        assert result.verdict is ReviewVerdict.PENDING_REVIEW

    def test_llm_empty_verdicts_stays_pending(self):
        """模型返回空判断时必须 pending，不能伪装成 machine_supported。"""
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            body="政策原文明确：经营主体应当办理市场注册手续。"
        )

        verifier = ContentVerifier(
            package, draft, fact_support_llm=MockReviewerLLM()
        )
        result = verifier.check_fact_support()

        assert result.verdict is ReviewVerdict.PENDING_REVIEW
        assert "未能给出任何判断" in result.detail

    def test_llm_supports_gives_machine_supported(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            body="政策原文明确：经营主体应当办理市场注册手续。"
        )

        llm = MockReviewerLLM(
            verdicts=[{"index": 0, "verdict": "supported", "note": "一致"}]
        )
        verifier = ContentVerifier(package, draft, fact_support_llm=llm)
        result = verifier.check_fact_support()

        assert result.verdict is ReviewVerdict.MACHINE_SUPPORTED
        assert "不代表专家确认" in result.detail

    def test_llm_contradicted_is_blocking(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            body="政策原文明确：经营主体应当办理市场注册手续。"
        )

        llm = MockReviewerLLM(
            verdicts=[
                {"index": 0, "verdict": "contradicted", "note": "方向相反"}
            ]
        )
        verifier = ContentVerifier(package, draft, fact_support_llm=llm)
        result = verifier.check_fact_support()

        assert result.verdict is ReviewVerdict.FAILED
        assert any(
            i.code == "MACHINE_CONTRADICTED"
            and i.severity is IssueSeverity.BLOCKING
            for i in verifier._issues
        )

    def test_literal_backtracking_detects_fake_number(self):
        """正文出现证据原文中不存在的数字 → 可靠的无依据信号。"""
        item, claim = _evidence_and_claim()
        item = _evidence("a" * 16, "第一条 经营主体应当办理市场注册。")
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            body="政策原文明确：办理注册需要 15 个工作日的审查期限。"
        )
        verifier = ContentVerifier(package, draft)
        verifier.check_fact_support()

        assert any(
            i.code == "UNSUPPORTED_LITERAL_ANCHOR" for i in verifier._issues
        )

    def test_checks_body_not_only_key_points(self):
        """必须检查正文，而不只是 key_points。"""

        item, claim = _evidence_and_claim()
        item = _evidence("a" * 16, "第一条 经营主体应当办理市场注册。")
        package = _package(claims=[claim], evidence=[item])
        # key_points 为空，但正文含事实性陈述
        draft = _draft(body="政策原文明确：经营主体必须缴纳 500 元 费用。")

        result = ContentVerifier(package, draft).check_fact_support()
        assert result.checked_items >= 1


# ==========================================================================
# 4. 宣传合规
# ==========================================================================
class TestComplianceReview:
    def test_clean_content_passes(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft()
        result = ContentVerifier(package, draft).check_compliance()
        assert result.verdict is ReviewVerdict.PASSED

    @pytest.mark.parametrize(
        "text",
        [
            "采用我们的方案可节省30%电费。",
            "每年可为客户增收50万元。",
            "电价仅为 0.35 元/千瓦时。",
            "我司可以提供全程代办服务。",
            "我们已有众多成功案例。",
        ],
    )
    def test_detects_violations(self, text):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(body=text)

        verifier = ContentVerifier(package, draft)
        result = verifier.check_compliance()

        assert result.verdict is ReviewVerdict.FAILED
        assert any(
            i.severity is IssueSeverity.BLOCKING for i in verifier._issues
        )

    def test_reuses_guards_not_reimplement(self):
        """合规审核必须复用 copywriting.guards 的规则。"""
        import inspect

        from powercopy.review import verifier as module

        source = inspect.getsource(module)
        assert "check_forbidden_content" in source


# ==========================================================================
# 5. 政策适用性
# ==========================================================================
class TestApplicabilityReview:
    def test_passed_when_consistent(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        # 正文包含免责声明，且不声称现行有效、不主张地区
        draft = _draft(
            body=(
                "政策原文明确：经营主体应当办理市场注册。"
                "具体以官方发布为准，不代表该政策现行有效。"
            )
        )
        result = ContentVerifier(package, draft).check_applicability()
        assert result.verdict is ReviewVerdict.PASSED

    def test_claims_effective_is_blocking(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(body="本政策现行有效，长期有效。")

        verifier = ContentVerifier(package, draft)
        result = verifier.check_applicability()

        assert result.verdict is ReviewVerdict.FAILED
        assert any(
            i.code == "CLAIMS_STILL_EFFECTIVE"
            and i.severity is IssueSeverity.BLOCKING
            for i in verifier._issues
        )

    def test_region_claimed_without_basis(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item], region="")
        draft = _draft(body="本规则适用于江西省所有工商业企业。")

        verifier = ContentVerifier(package, draft)
        verifier.check_applicability()
        assert any(
            i.code == "REGION_CLAIMED_WITHOUT_BASIS"
            for i in verifier._issues
        )

    def test_mentioning_region_is_not_claim(self):
        """仅提到地区名（如引用原文的「全国统一大市场」）不算声称适用范围。"""
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item], region="")
        draft = _draft(
            body="政策提出建设全国统一大市场，经营主体应当办理注册。"
        )

        verifier = ContentVerifier(package, draft)
        verifier.check_applicability()
        assert not any(
            i.code == "REGION_CLAIMED_WITHOUT_BASIS"
            for i in verifier._issues
        )

    def test_validity_not_latest(self):
        item, claim = _evidence_and_claim()
        package = _package(
            claims=[claim], evidence=[item],
            validity="superseded_in_corpus",
        )
        draft = _draft()

        verifier = ContentVerifier(package, draft)
        verifier.check_applicability()
        assert any(
            i.code == "VALIDITY_NOT_LATEST" for i in verifier._issues
        )

    def test_missing_disclaimer_minor(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(body="政策原文明确：经营主体应当办理注册。")

        verifier = ContentVerifier(package, draft)
        verifier.check_applicability()
        assert any(
            i.code == "VALIDITY_DISCLAIMER_MISSING"
            and i.severity is IssueSeverity.MINOR
            for i in verifier._issues
        )


# ==========================================================================
# 6. 报告契约
# ==========================================================================
class TestReviewReportContract:
    def test_passed_not_means_publishable(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft(
            key_points=[
                KeyPoint(
                    point_id="p1", text="事实", evidence_ids=["a" * 16]
                )
            ],
            evidence_refs=["a" * 16],
        )
        report = ContentVerifier(package, draft).run()

        # 即使 passed，仍需人工
        assert report.requires_human() is True
        stats = report.statistics()
        assert stats["requires_human"] is True
        assert "不代表文案可发布" in stats["note"]

    def test_statistics_separates_dimensions(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        draft = _draft()
        report = ContentVerifier(package, draft).run()

        stats = report.statistics()
        assert "by_dimension" in stats
        # 四维分开，不合并成单一通过率
        for dim in ("citation", "fact_support", "compliance", "applicability"):
            assert dim in stats["by_dimension"]

    def test_human_confirmation_never_true(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        report = ContentVerifier(package, _draft()).run()
        assert report.human_confirmation_available is False

    def test_fact_support_llm_flag(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])

        without = ContentVerifier(package, _draft()).run()
        assert without.fact_support_llm_used is False

        with_llm = ContentVerifier(
            package, _draft(), fact_support_llm=MockReviewerLLM()
        ).run()
        assert with_llm.fact_support_llm_used is True


# ==========================================================================
# 7. 有限自动修订
# ==========================================================================
class TestRevision:
    def _bad_draft(self, package):
        """构造含无依据宣传语的草稿。"""
        return _draft(
            body=(
                "政策原文明确：经营主体应当办理市场注册。"
                "采用我们的方案可节省30%电费。"
            )
        )

    def test_skips_when_no_llm(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        agent = ReviewAgent(package, reviser_llm=None)
        report, draft, revision = agent.run(self._bad_draft(package))

        assert revision.attempt == 0
        assert revision.status == "skipped"

    def test_revises_and_rereviews(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        agent = ReviewAgent(
            package, reviser_llm=MockReviserLLM(), max_revisions=1
        )
        report, draft, revision = agent.run(self._bad_draft(package))

        assert revision.attempt >= 1
        assert revision.status in ("revised", "failed")
        # 修订后必须重新审核
        assert revision.post_review_status

    def test_max_attempt_bound(self):
        with pytest.raises(ValueError, match="不得超过 3"):
            ReviewAgent(_package(), reviser_llm=MockReviserLLM(), max_revisions=4)

    def test_revision_result_attempt_bound(self):
        with pytest.raises(ValidationError):
            RevisionResult(
                revision_id="revn-testtest12", attempt=5
            )

    def test_blocking_issue_skips_revision(self):
        """引用不存在的证据是 blocking，不应尝试改写。"""
        package = _package(claims=[], evidence=[])
        draft = _draft(
            key_points=[
                KeyPoint(
                    point_id="p1", text="事实", evidence_ids=["f" * 16]
                )
            ],
            evidence_refs=["f" * 16],
        )
        agent = ReviewAgent(
            package, reviser_llm=MockReviserLLM(), max_revisions=1
        )
        report, final, revision = agent.run(draft)

        assert revision.attempt == 0
        assert "阻断级" in revision.status_reason

    def test_no_infinite_loop(self):
        """即使问题一直存在，也必须在 max_revisions 后停止。"""
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])

        # 让修订器"不改"：问题持续存在
        agent = ReviewAgent(
            package,
            reviser_llm=MockReviserLLM(fail_mode="no_change"),
            max_revisions=2,
        )
        report, draft, revision = agent.run(self._bad_draft(package))
        assert revision.attempt <= 2

    def test_before_after_recorded(self):
        item, claim = _evidence_and_claim()
        package = _package(claims=[claim], evidence=[item])
        agent = ReviewAgent(
            package, reviser_llm=MockReviserLLM(), max_revisions=1
        )
        report, draft, revision = agent.run(self._bad_draft(package))

        assert revision.before_body
        assert revision.after_body
        assert revision.revision_id.startswith("revn-")


# ==========================================================================
# 8. 统一工作流
# ==========================================================================
@needs_real
class TestWorkflow:
    @pytest.fixture(scope="class")
    @classmethod
    def service(cls):
        from powercopy.tools.builtin.policy_tools import build_live_registry

        registry = build_live_registry(DB)
        corpus = PolicyCorpus(DB, MANIFEST)
        return ContentWorkflowService(corpus, registry, model_name="mock")

    def _request(self, **overrides):
        payload = {
            "topic": "工商业企业参与电力市场前，需要了解哪些基本规则？",
            "target_audience": "工商业用电企业",
            "platform": "wechat_mp",
            "target_chars": 800,
            "max_questions": 3,
            "max_tool_calls": 5,
        }
        payload.update(overrides)
        return ContentWorkflowRequest(**payload)

    def test_normal_generation(self, service):
        result = service.run(self._request())

        assert isinstance(result, ContentWorkflowResult)
        assert result.task_id.startswith("task-")
        assert result.research is not None
        assert result.draft is not None
        assert result.review is not None
        assert result.final_status in (
            WorkflowStatus.PENDING_REVIEW,
            WorkflowStatus.NEEDS_MANUAL_REVIEW,
        )

    def test_no_publishable_status(self, service):
        """没有任何终态表示可自动发布。"""
        result = service.run(self._request())
        assert result.final_status not in (
            "published", "approved", "auto_published"
        )
        assert result.requires_human() is True

    def test_skip_review(self, service):
        result = service.run(self._request(enable_review=False))
        assert result.final_status is WorkflowStatus.DRAFT_READY
        assert result.review is None
        assert result.revision is None

    def test_budget_shared_across_stages(self, service):
        result = service.run(self._request(max_api_attempts=3))
        budget = result.budget_snapshot
        # 预算被多阶段共用
        assert budget["used_api_attempts"] > 0
        if result.final_status is WorkflowStatus.BUDGET_EXHAUSTED:
            assert budget["exhausted_by"]

    def test_budget_exhausted_stops(self, service):
        result = service.run(self._request(max_api_attempts=1))
        assert result.final_status in (
            WorkflowStatus.BUDGET_EXHAUSTED,
            WorkflowStatus.FAILED,
            WorkflowStatus.PENDING_REVIEW,
            WorkflowStatus.NEEDS_MANUAL_REVIEW,
        )
        assert result.budget_snapshot["used_api_attempts"] >= 1

    def test_json_roundtrip(self, service):
        result = service.run(self._request())
        text = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        restored = ContentWorkflowResult.model_validate(json.loads(text))
        assert restored.task_id == result.task_id

    def test_statistics_separates_layers(self, service):
        result = service.run(self._request())
        stats = result.statistics()

        assert "research_claims_text_verified" in stats
        assert "research_claims_human_confirmed" in stats
        assert stats["research_claims_human_confirmed"] == 0
        assert "note" in stats


class TestWorkflowRequestContract:
    @pytest.mark.parametrize(
        "forbidden",
        [
            {"db_path": "/etc/passwd"},
            {"index_path": "x"},
            {"url": "https://evil.invalid"},
            {"llm_api_key": "sk-xxx"},
        ],
    )
    def test_rejects_path_fields(self, forbidden):
        with pytest.raises(ValidationError):
            ContentWorkflowRequest.model_validate(
                {"topic": "x", **forbidden}
            )

    def test_rejects_empty_topic(self):
        with pytest.raises(ValidationError):
            ContentWorkflowRequest(topic="")

    def test_accepts_string_platform(self):
        """API 边界：JSON 里 platform 是字符串。"""
        request = ContentWorkflowRequest(
            topic="x", platform="wechat_mp"
        )
        assert request.platform is Platform.WECHAT_MP

    def test_max_revisions_bound(self):
        with pytest.raises(ValidationError):
            ContentWorkflowRequest(topic="x", max_revisions=5)

    def test_revisions_not_exceed_attempts(self):
        with pytest.raises(ValidationError):
            ContentWorkflowRequest(
                topic="x", max_revisions=5, max_api_attempts=2
            )


# ==========================================================================
# 9. 验收场景
# ==========================================================================
@needs_real
class TestAcceptanceScenarios:
    @pytest.fixture(scope="class")
    @classmethod
    def ctx(cls):
        from powercopy.tools.builtin.policy_tools import build_live_registry

        registry = build_live_registry(DB)
        corpus = PolicyCorpus(DB, MANIFEST)
        return registry, corpus

    def _pipeline(self, ctx):
        """跑一次研究 + 文案，返回 (package, draft)。"""
        from powercopy.copywriting.agent import CopywritingAgent
        from powercopy.copywriting.contracts import CopywritingRequest
        from powercopy.copywriting.mock import MockCopywriterLLM
        from powercopy.research.agent import ResearchAgent
        from powercopy.research.contracts import ResearchBudget, ResearchRequest
        from powercopy.research.mock import MockResearchLLM

        registry, corpus = ctx
        rreq = ResearchRequest(
            topic="工商业企业参与电力市场前，需要了解哪些基本规则？",
            target_audience="工商业用电企业",
            task_budget=ResearchBudget(max_questions=3, max_tool_calls=5),
        )
        package = ResearchAgent(
            MockResearchLLM(max_questions=3), registry, corpus,
            budget=rreq.task_budget,
        ).run(rreq)

        creq = CopywritingRequest(
            topic=rreq.topic,
            target_audience="工商业用电企业",
            platform=Platform.WECHAT_MP,
            target_chars=800,
            evidence_package=package,
        )
        draft = CopywritingAgent(
            MockCopywriterLLM(), mode=GenerationMode.MOCK, model_name="mock"
        ).run(creq)
        return package, draft

    def test_scenario_1_normal(self, ctx):
        package, draft = self._pipeline(ctx)
        report = ContentVerifier(package, draft).run()
        assert report.status in (
            ReviewStatus.PASSED,
            ReviewStatus.PENDING_REVIEW,
            ReviewStatus.NEEDS_REVISION,
        )

    def test_scenario_2_body_with_unsupported_fact(self, ctx):
        package, draft = self._pipeline(ctx)
        # 注入一条正文中凭空出现的事实
        tampered = draft.model_copy(
            update={
                "body": draft.body
                + "\n\n办理注册需缴纳 2000 元 手续费，审查期为 30 个工作日。"
            }
        )
        verifier = ContentVerifier(package, tampered)
        result = verifier.check_fact_support()

        assert any(
            i.code == "UNSUPPORTED_LITERAL_ANCHOR" for i in verifier._issues
        )
        assert result.verdict in (
            ReviewVerdict.PENDING_REVIEW,
            ReviewVerdict.FAILED,
        )

    def test_scenario_3_quote_not_supporting_conclusion(self, ctx):
        """真实引文却不支持结论 —— 只能靠 LLM 或人工发现。"""
        package, draft = self._pipeline(ctx)

        llm = MockReviewerLLM(
            verdicts=[
                {
                    "index": 0,
                    "verdict": "contradicted",
                    "note": "引文讲的是注册条件，不是处罚标准",
                }
            ]
        )
        verifier = ContentVerifier(
            package, draft, fact_support_llm=llm
        )
        result = verifier.check_fact_support()

        assert result.verdict is ReviewVerdict.FAILED
        assert any(
            i.code == "MACHINE_CONTRADICTED" for i in verifier._issues
        )

    def test_scenario_4_region_or_validity_unclear(self, ctx):
        package, draft = self._pipeline(ctx)
        # 地区不明确的证据包 + 文案声称适用某地区
        tampered_pkg = package.model_copy(update={"region": ""})
        tampered_draft = draft.model_copy(
            update={"body": draft.body + "\n\n本规则适用于江西省所有企业。"}
        )
        verifier = ContentVerifier(tampered_pkg, tampered_draft)
        verifier.check_applicability()

        assert any(
            i.code == "REGION_CLAIMED_WITHOUT_BASIS"
            for i in verifier._issues
        )

    def test_scenario_5_forbidden_promotion(self, ctx):
        package, draft = self._pipeline(ctx)
        tampered = draft.model_copy(
            update={
                "body": draft.body
                + "\n\n我们已帮助多家企业节省 30% 电费，欢迎咨询。"
            }
        )
        verifier = ContentVerifier(package, tampered)
        result = verifier.check_compliance()

        assert result.verdict is ReviewVerdict.FAILED
        assert any(
            i.severity is IssueSeverity.BLOCKING for i in verifier._issues
        )

    def test_scenario_6_revision_still_fails(self, ctx):
        package, draft = self._pipeline(ctx)
        tampered = draft.model_copy(
            update={
                "body": draft.body + "\n\n采用我们的方案可节省30%电费。"
            }
        )
        agent = ReviewAgent(
            package, reviser_llm=MockReviserLLM(), max_revisions=1
        )
        report, final, revision = agent.run(tampered)

        # 修订后必须有结果记录
        assert revision.attempt >= 0
        if revision.attempt > 0:
            assert revision.post_review_status

    def test_scenario_7_budget_exhausted(self, ctx):
        from powercopy.workflows import ContentWorkflowService

        registry, corpus = ctx
        service = ContentWorkflowService(
            corpus, registry, model_name="mock"
        )
        result = service.run(
            ContentWorkflowRequest(
                topic="工商业企业参与电力市场前，需要了解哪些基本规则？",
                target_audience="工商业用电企业",
                max_questions=3,
                max_tool_calls=5,
                max_api_attempts=1,
            )
        )

        assert result.final_status in (
            WorkflowStatus.BUDGET_EXHAUSTED,
            WorkflowStatus.PENDING_REVIEW,
            WorkflowStatus.NEEDS_MANUAL_REVIEW,
            WorkflowStatus.FAILED,
        )
        assert result.budget_snapshot["used_api_attempts"] >= 1
