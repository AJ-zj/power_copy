"""MVP-05-B Token 计费与成本控制专项测试。

覆盖：
- 用量未知不得填零
- Decimal 货币计算的精确性
- 未知单价不套用其他平台
- 台账持久化与幂等
- 每日预算（请求硬上限 / Token 软预算）与预警
- 日报统计口径（已知 vs 未知、三档费用）
- 账单 CSV 导入与人工对账
- LLMClient 接入（Mock 与失败路径）
- 导出格式
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from powercopy.cost import (
    CostAccounting,
    CostStatus,
    CostStore,
    DailyBudget,
    DailyBudgetGuard,
    ModelCallRecord,
    PriceRule,
    PriceTable,
    Reconciler,
    RequestOutcome,
    Usage,
    build_daily_report,
    calls_to_csv,
    compute_cost,
    load_price_table,
    report_to_csv,
    report_to_markdown,
)
from powercopy.cost.contracts import (
    prompt_fingerprint,
    to_beijing_day,
)
from powercopy.cost.daily import DailyBudgetExceeded
from powercopy.cost.recorder import (
    CallRecorder,
    outcome_from_error,
    usage_from_response,
)
from powercopy.cost.reconcile import ReconciliationError, parse_bill_csv

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store():
    tmp = Path(tempfile.mkdtemp())
    return CostStore(tmp / "cost.db")


@pytest.fixture()
def priced_store():
    """带单价的存储 + 记录器工厂。"""

    tmp = Path(tempfile.mkdtemp())
    store = CostStore(tmp / "cost.db")
    table = PriceTable([
        PriceRule(
            provider="test-provider",
            model="test-model",
            input_per_million=Decimal("2.00"),
            output_per_million=Decimal("8.00"),
            price_version="v1",
            source="测试用，非真实价格",
        )
    ])
    return store, table


def make_record(
    *,
    request_id: str = "req-0000000000000001",
    stage: str = "research",
    usage: Usage | None = None,
    outcome: RequestOutcome = RequestOutcome.SUCCESS,
    cost: Decimal | None = None,
    status: CostStatus = CostStatus.UNRECONCILED,
    mode: str = "live",
) -> ModelCallRecord:
    return ModelCallRecord(
        request_id=request_id,
        task_id="task-test",
        stage=stage,
        provider="test-provider",
        model="test-model",
        usage=usage or Usage(input_tokens=1000, output_tokens=500),
        outcome=outcome,
        estimated_cost=cost,
        cost_status=status,
        mode=mode,
    )


# ==========================================================================
# 1. 用量契约：未知不等于零
# ==========================================================================
class TestUsageContract:
    def test_complete_when_breakdown_present(self):
        usage = Usage(input_tokens=100, output_tokens=50)
        assert usage.total_tokens == 150
        assert usage.complete is True

    def test_incomplete_when_no_total(self):
        usage = Usage()
        assert usage.total_tokens is None
        assert usage.complete is False

    def test_incomplete_when_only_total(self):
        """只有 total 没有分项 → 无法按输入输出分别计价，仍算不完整。"""
        usage = Usage(total_tokens=100)
        assert usage.complete is False

    def test_zero_is_not_none(self):
        usage = Usage(input_tokens=0, output_tokens=0)
        assert usage.total_tokens == 0
        assert usage.complete is True
        # 0 是「明确知道消耗 0」，与 None 不同
        assert usage.total_tokens is not None

    def test_negative_rejected(self):
        with pytest.raises(ValidationError):
            Usage(input_tokens=-1)


# ==========================================================================
# 2. 费用计算（Decimal）
# ==========================================================================
class TestPricing:
    def test_basic_computation(self):
        rule = PriceRule(
            provider="p", model="m",
            input_per_million=Decimal("2"), output_per_million=Decimal("8"),
        )
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        cost, note = compute_cost(usage, rule)
        assert cost == Decimal("10.00000000")
        assert note == "ok"

    def test_decimal_no_float_error(self):
        """0.1 + 0.2 必须精确等于 0.3。"""
        rule = PriceRule(
            provider="p", model="m",
            input_per_million=Decimal("0.1"),
            output_per_million=Decimal("0.2"),
        )
        a, _ = compute_cost(
            Usage(input_tokens=1_000_000, output_tokens=0), rule
        )
        b, _ = compute_cost(
            Usage(input_tokens=0, output_tokens=1_000_000), rule
        )
        assert a + b == Decimal("0.30000000")

    def test_no_rule_returns_none(self):
        usage = Usage(input_tokens=100, output_tokens=50)
        cost, note = compute_cost(usage, None)
        assert cost is None
        assert note == "no_price_rule"

    def test_incomplete_usage_returns_none(self):
        rule = PriceRule(
            provider="p", model="m",
            input_per_million=Decimal("2"), output_per_million=Decimal("8"),
        )
        cost, note = compute_cost(Usage(complete=False), rule)
        assert cost is None
        assert note == "usage_incomplete"

    def test_cached_tokens_use_separate_price(self):
        rule = PriceRule(
            provider="p", model="m",
            input_per_million=Decimal("2"),
            output_per_million=Decimal("8"),
            cached_input_per_million=Decimal("0.5"),
        )
        usage = Usage(
            input_tokens=1_000_000, output_tokens=0, cached_tokens=1_000_000
        )
        cost, note = compute_cost(usage, rule)
        # 全部输入都是缓存命中 → 按 0.5 计
        assert cost == Decimal("0.50000000")
        assert note == "ok_cached_price"

    def test_cached_without_price_falls_back_to_input(self):
        rule = PriceRule(
            provider="p", model="m",
            input_per_million=Decimal("2"), output_per_million=Decimal("8"),
        )
        usage = Usage(
            input_tokens=1_000_000, output_tokens=0, cached_tokens=500_000
        )
        cost, note = compute_cost(usage, rule)
        assert cost == Decimal("2.00000000")
        assert "input_price" in note


class TestPriceTable:
    def test_lookup_exact_version(self):
        table = PriceTable([
            PriceRule(provider="p", model="m",
                      input_per_million=Decimal("1"),
                      output_per_million=Decimal("2"),
                      price_version="v1"),
        ])
        assert table.lookup("p", "m", "v1") is not None
        assert table.lookup("p", "m", "v2") is None

    def test_lookup_latest_by_default(self):
        table = PriceTable([
            PriceRule(provider="p", model="m",
                      input_per_million=Decimal("1"),
                      output_per_million=Decimal("2"),
                      price_version="v1",
                      effective_from="2026-01-01T00:00:00+00:00"),
            PriceRule(provider="p", model="m",
                      input_per_million=Decimal("3"),
                      output_per_million=Decimal("4"),
                      price_version="v2",
                      effective_from="2026-06-01T00:00:00+00:00"),
        ])
        rule = table.lookup("p", "m")
        assert rule.price_version == "v2"

    def test_unknown_model_returns_none(self):
        table = PriceTable()
        assert table.lookup("unknown", "unknown") is None

    def test_no_cross_platform_fallback(self):
        """配了 A 平台，查 B 平台必须返回 None，不能套用。"""
        table = PriceTable([
            PriceRule(provider="platform-a", model="m",
                      input_per_million=Decimal("1"),
                      output_per_million=Decimal("2")),
        ])
        assert table.lookup("platform-b", "m") is None

    def test_models_without_price(self):
        table = PriceTable([
            PriceRule(provider="p", model="m1",
                      input_per_million=Decimal("1"),
                      output_per_million=Decimal("2")),
        ])
        missing = table.models_without_price(
            [("p", "m1"), ("p", "m2"), ("q", "m1")]
        )
        assert ("p", "m2") in missing
        assert ("q", "m1") in missing
        assert ("p", "m1") not in missing


# ==========================================================================
# 3. 台账存储
# ==========================================================================
class TestCostStore:
    def test_record_and_read(self, store):
        store.record_call(make_record(cost=Decimal("0.01")))
        assert store.count() == 1

        records = store.calls_for_task("task-test")
        assert len(records) == 1
        assert records[0].estimated_cost == Decimal("0.01")

    def test_idempotent_by_request_id(self, store):
        record = make_record()
        store.record_call(record)
        store.record_call(record)
        assert store.count() == 1

    def test_unknown_usage_persisted_as_null(self, store):
        store.record_call(
            make_record(
                usage=Usage(complete=False),
                outcome=RequestOutcome.API_ERROR,
                cost=None,
                status=CostStatus.UNKNOWN,
            )
        )
        record = store.calls_for_task("task-test")[0]
        assert record.usage.total_tokens is None
        assert record.usage.complete is False
        assert record.cost_status is CostStatus.UNKNOWN
        assert record.estimated_cost is None

    def test_decimal_roundtrip_exact(self, store):
        precise = Decimal("0.12345678")
        store.record_call(make_record(cost=precise))
        assert store.calls_for_task("task-test")[0].estimated_cost == precise

    def test_mock_forced_zero_cost(self, store):
        record = ModelCallRecord(
            request_id="req-mock00000000001",
            usage=Usage(input_tokens=100, output_tokens=50),
            mode="mock",
        )
        assert record.estimated_cost == Decimal("0")
        assert record.cost_status is CostStatus.ESTIMATED

    def test_day_beijing(self):
        # UTC 2026-11-27T20:00 → 北京 2026-11-28
        assert to_beijing_day("2026-11-27T20:00:00+00:00") == "2026-11-28"
        # UTC 2026-11-27T15:59 → 北京 2026-11-27
        assert to_beijing_day("2026-11-27T15:59:00+00:00") == "2026-11-27"

    def test_task_outcome_upsert(self, store):
        store.record_task_outcome("t1", "2026-11-27", "pending_review", drafts=1)
        store.record_task_outcome("t1", "2026-11-27", "failed", drafts=1)
        rows = store.task_outcomes_for_day("2026-11-27")
        assert len(rows) == 1
        assert rows[0]["final_status"] == "failed"


# ==========================================================================
# 4. 记录器
# ==========================================================================
class TestCallRecorder:
    def test_unknown_price_records_unknown_status(self, priced_store):
        store, table = priced_store
        recorder = CallRecorder(
            store, table, task_id="t1", stage="research",
            mode="live", provider="unknown-provider", model="unknown-model",
        )
        record = recorder.record_attempt(
            outcome=RequestOutcome.SUCCESS,
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        assert record.cost_status is CostStatus.UNKNOWN
        assert record.estimated_cost is None
        assert "未配置单价" in record.note
        assert "不得套用其他平台价格" in record.note

    def test_known_price_records_unreconciled(self, priced_store):
        store, table = priced_store
        recorder = CallRecorder(
            store, table, task_id="t1", stage="research",
            mode="live", provider="test-provider", model="test-model",
        )
        record = recorder.record_attempt(
            outcome=RequestOutcome.SUCCESS,
            usage=Usage(input_tokens=1_000_000, output_tokens=0),
        )
        assert record.cost_status is CostStatus.UNRECONCILED
        assert record.estimated_cost == Decimal("2.00000000")
        assert record.price_version == "v1"

    def test_failed_request_not_zero_cost(self, priced_store):
        """失败请求用量未知 → 费用 unknown，绝不记 0。"""
        store, table = priced_store
        recorder = CallRecorder(
            store, table, task_id="t1", stage="research",
            mode="live", provider="test-provider", model="test-model",
        )
        record = recorder.record_attempt(
            outcome=RequestOutcome.API_ERROR,
            usage=Usage(complete=False),
        )
        assert record.estimated_cost is None
        assert record.cost_status is CostStatus.UNKNOWN
        assert record.usage.total_tokens is None

    def test_call_index_increments(self, priced_store):
        store, table = priced_store
        recorder = CallRecorder(
            store, table, task_id="t1", stage="research", mode="live",
        )
        a = recorder.record_attempt(outcome=RequestOutcome.SUCCESS)
        b = recorder.record_attempt(outcome=RequestOutcome.SUCCESS)
        assert (a.call_index, b.call_index) == (1, 2)

    def test_stage_switch_shares_call_index(self, priced_store):
        store, table = priced_store
        root = CallRecorder(
            store, table, task_id="t1", stage="research", mode="live",
        )
        root.record_attempt(outcome=RequestOutcome.SUCCESS)

        child = root.for_stage("copywriting")
        record = child.record_attempt(outcome=RequestOutcome.SUCCESS)
        assert record.stage == "copywriting"
        assert record.call_index == 2

    def test_mock_mode_zero_cost(self, store):
        recorder = CallRecorder(
            store, PriceTable(), task_id="t1", stage="research", mode="mock",
        )
        record = recorder.record_attempt(
            outcome=RequestOutcome.SUCCESS,
            usage=Usage(input_tokens=500, output_tokens=200),
        )
        assert record.estimated_cost == Decimal("0")
        assert record.mode == "mock"

    def test_prompt_not_stored_in_full(self, priced_store):
        store, table = priced_store
        recorder = CallRecorder(
            store, table, task_id="t1", stage="research", mode="live",
        )
        secret_prompt = "这是完整的提示词内容，包含业务信息" * 10
        record = recorder.record_attempt(
            outcome=RequestOutcome.SUCCESS,
            usage=Usage(input_tokens=100, output_tokens=50),
            prompt_text=secret_prompt,
        )
        assert record.prompt_chars == len(secret_prompt)
        assert len(record.prompt_sha256) == 16
        # 台账里不应出现原文
        assert secret_prompt not in record.note
        assert "完整的提示词内容" not in record.note

    def test_summary(self, priced_store):
        store, table = priced_store
        recorder = CallRecorder(
            store, table, task_id="t1", stage="research",
            mode="live", provider="test-provider", model="test-model",
        )
        recorder.record_attempt(
            outcome=RequestOutcome.SUCCESS,
            usage=Usage(input_tokens=1000, output_tokens=500),
        )
        recorder.record_attempt(
            outcome=RequestOutcome.API_ERROR, usage=Usage(complete=False)
        )
        summary = recorder.summary()
        assert summary["requests"] == 2
        assert summary["known_tokens"] == 1500
        assert summary["unknown_usage_requests"] == 1


class TestUsageFromResponse:
    def test_complete(self):
        usage = usage_from_response(
            {"total_tokens": 150, "input_tokens": 100, "output_tokens": 50}
        )
        assert usage.complete is True
        assert usage.total_tokens == 150

    def test_missing_total_is_incomplete(self):
        usage = usage_from_response({"total_tokens": None})
        assert usage.complete is False
        assert usage.total_tokens is None

    def test_total_without_breakdown_incomplete(self):
        usage = usage_from_response({"total_tokens": 100})
        assert usage.complete is False


class TestOutcomeFromError:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("APIConnectionError", RequestOutcome.API_ERROR),
            ("APITimeoutError", RequestOutcome.API_ERROR),
            ("RateLimitError", RequestOutcome.RATE_LIMITED),
            ("AuthenticationError", RequestOutcome.QUOTA_EXCEEDED),
        ],
    )
    def test_mapping_by_name(self, name, expected):
        error = type(name, (Exception,), {})()
        assert outcome_from_error(error) is expected

    def test_mapping_by_status(self):
        error = Exception()
        error.status_code = 500
        assert outcome_from_error(error) is RequestOutcome.API_ERROR

        error2 = Exception()
        error2.status_code = 429
        assert outcome_from_error(error2) is RequestOutcome.RATE_LIMITED


# ==========================================================================
# 5. 每日预算
# ==========================================================================
class TestDailyBudget:
    def test_request_limit_is_hard(self, store):
        guard = DailyBudgetGuard(
            DailyBudget(max_requests_per_day=2), store
        )
        store.record_call(make_record(request_id="req-0000000000000011"))
        store.record_call(make_record(request_id="req-0000000000000012"))

        with pytest.raises(DailyBudgetExceeded) as info:
            guard.check()
        assert info.value.reason == "daily_request_budget_exhausted"

    def test_token_limit_is_soft(self, store):
        guard = DailyBudgetGuard(
            DailyBudget(max_requests_per_day=100, max_tokens_per_day=1000),
            store,
        )
        store.record_call(
            make_record(
                usage=Usage(input_tokens=600, output_tokens=600)
            )
        )
        with pytest.raises(DailyBudgetExceeded) as info:
            guard.check()
        assert info.value.reason == "daily_token_budget_exhausted"

    def test_warning_at_threshold(self, store):
        guard = DailyBudgetGuard(
            DailyBudget(max_requests_per_day=10, warn_at_requests=0.8), store
        )
        for index in range(8):
            store.record_call(
                make_record(request_id=f"req-000000000000{index:04d}")
            )
        warnings = guard.check()
        assert any(w.kind == "requests_near_limit" for w in warnings)

    def test_unknown_usage_flagged(self, store):
        guard = DailyBudgetGuard(DailyBudget(), store)
        store.record_call(
            make_record(
                usage=Usage(complete=False),
                outcome=RequestOutcome.API_ERROR,
                cost=None,
                status=CostStatus.UNKNOWN,
            )
        )
        warnings = guard.check()
        assert any(w.kind == "tokens_usage_incomplete" for w in warnings)

    def test_blocked_requests_not_counted(self, store):
        """budget_blocked 表示请求没发出，不应计入已用请求数。"""
        record = make_record(
            request_id="req-blocked00000001",
            outcome=RequestOutcome.BUDGET_BLOCKED,
        )
        store.record_call(record)
        guard = DailyBudgetGuard(DailyBudget(max_requests_per_day=1), store)
        # 只有 1 条 blocked 记录，不应触发耗尽
        assert guard.current_usage()["requests"] == 0
        guard.check()

    def test_projected_over_warning(self, store):
        guard = DailyBudgetGuard(
            DailyBudget(max_requests_per_day=100, max_tokens_per_day=1000),
            store,
        )
        store.record_call(
            make_record(usage=Usage(input_tokens=400, output_tokens=400))
        )
        warnings = guard.check(upcoming_tokens_hint=500)
        assert any(w.kind == "tokens_projected_over" for w in warnings)

    def test_remaining(self, store):
        guard = DailyBudgetGuard(
            DailyBudget(max_requests_per_day=10, max_tokens_per_day=1000),
            store,
        )
        store.record_call(
            make_record(usage=Usage(input_tokens=100, output_tokens=100))
        )
        assert guard.remaining_requests() == 9
        assert guard.remaining_tokens() == 800


# ==========================================================================
# 6. 日报
# ==========================================================================
class TestDailyReport:
    def _seed(self, store, day: str):
        from powercopy.cost.contracts import utc_now_iso

        records = [
            make_record(
                request_id="req-s00000000000001",
                usage=Usage(input_tokens=1000, output_tokens=500),
                cost=Decimal("0.01"),
            ),
            make_record(
                request_id="req-s00000000000002",
                stage="copywriting",
                usage=Usage(input_tokens=2000, output_tokens=1000),
                cost=Decimal("0.02"),
            ),
            make_record(
                request_id="req-f00000000000001",
                outcome=RequestOutcome.API_ERROR,
                usage=Usage(complete=False),
                cost=None,
                status=CostStatus.UNKNOWN,
            ),
        ]
        for record in records:
            store.record_call(record)

        store.record_task_outcome(
            "task-test", day, "pending_review", drafts=2
        )

    def test_counts(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        self._seed(store, day)
        report = build_daily_report(store, day)

        assert report.requests_total == 3
        assert report.requests_success == 2
        assert report.requests_failed == 1
        assert report.tokens_unknown_requests == 1
        assert report.tasks_started == 1
        assert report.drafts_generated == 2

    def test_known_tokens_exclude_unknown(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        self._seed(store, day)
        report = build_daily_report(store, day)
        # 只有两条成功记录计入
        assert report.tokens_known_total == 4500

    def test_tokens_by_stage(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        self._seed(store, day)
        report = build_daily_report(store, day)
        assert report.tokens_by_stage["research"] == 1500
        assert report.tokens_by_stage["copywriting"] == 3000

    def test_costs(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        self._seed(store, day)
        report = build_daily_report(store, day)
        assert report.estimated_cost == Decimal("0.03")
        assert report.unreconciled_cost == Decimal("0.03")
        assert report.billed_cost == Decimal("0")
        assert report.unknown_cost_requests == 1

    def test_avg_cost_per_draft(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        self._seed(store, day)
        report = build_daily_report(store, day)
        # 0.03 / 2 = 0.015
        assert report.avg_cost_per_draft == Decimal("0.015000")

    def test_no_drafts_avg_is_none(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        store.record_call(make_record(cost=Decimal("0.01")))
        report = build_daily_report(store, day)
        assert report.avg_cost_per_draft is None

    def test_warnings_about_unknown(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        self._seed(store, day)
        report = build_daily_report(store, day)
        text = " ".join(report.warnings)
        assert "未返回用量" in text
        assert "无法计算费用" in text


# ==========================================================================
# 7. 导出
# ==========================================================================
class TestExport:
    def test_markdown_contains_disclaimers(self, store):
        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        store.record_call(make_record(cost=Decimal("0.01")))
        report = build_daily_report(store, day)
        text = report_to_markdown(report)

        assert "Token 与费用日报" in text
        assert "不按 0 消耗处理" in text
        assert "软预算" in text
        assert day in text

    def test_csv_parsable(self, store):
        import csv
        import io

        day = to_beijing_day(__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat())
        store.record_call(make_record(cost=Decimal("0.01")))
        report = build_daily_report(store, day)
        text = report_to_csv(report)

        rows = list(csv.reader(io.StringIO(text)))
        assert len(rows) == 2
        assert rows[0][0] == "day"
        assert rows[1][0] == day

    def test_detail_csv_no_prompt_text(self, store):
        store.record_call(
            make_record(cost=Decimal("0.01"))
        )
        text = calls_to_csv(store.all_calls())
        assert "prompt_chars" in text
        assert "prompt_sha256" in text
        # 不应有提示词内容列
        assert "prompt_text" not in text


# ==========================================================================
# 8. 对账
# ==========================================================================
class TestReconciliation:
    def test_parse_csv(self):
        lines, total = parse_bill_csv(
            "request_id,amount\nreq-a,0.01\nreq-b,0.02\n"
        )
        assert len(lines) == 2
        assert total == Decimal("0.03")

    def test_parse_chinese_headers(self):
        lines, total = parse_bill_csv("请求ID,金额\nreq-a,0.05\n")
        assert lines[0]["request_id"] == "req-a"
        assert total == Decimal("0.05")

    def test_missing_amount_column(self):
        with pytest.raises(ReconciliationError, match="缺少金额列"):
            parse_bill_csv("request_id,foo\nreq-a,1\n")

    def test_negative_amount_rejected(self):
        with pytest.raises(ReconciliationError, match="金额为负"):
            parse_bill_csv("request_id,amount\nreq-a,-1\n")

    def test_invalid_amount(self):
        with pytest.raises(ReconciliationError, match="无法解析"):
            parse_bill_csv("request_id,amount\nreq-a,abc\n")

    def test_empty_csv(self):
        with pytest.raises(ReconciliationError, match="为空"):
            parse_bill_csv("")

    def test_import_csv_marks_billed_with_amount(self, store):
        store.record_call(
            make_record(
                request_id="req-import000000001",
                cost=Decimal("0.01"),
            )
        )
        tmp = Path(tempfile.mkdtemp()) / "bill.csv"
        tmp.write_text(
            "request_id,amount\nreq-import000000001,0.0121\n", encoding="utf-8"
        )

        reconciler = Reconciler(store)
        result = reconciler.import_csv(tmp, source="test")

        assert result["matched"] == 1
        record = store.all_calls()[0]
        assert record.cost_status is CostStatus.BILLED
        assert record.billed_cost == Decimal("0.0121")
        # 估算值必须保留
        assert record.estimated_cost == Decimal("0.01")

    def test_manual_import(self, store):
        reconciler = Reconciler(store)
        result = reconciler.import_manual(
            total=Decimal("12.34"), note="手工核对"
        )
        assert result["total"] == "12.34"
        assert len(store.reconciliations()) == 1

    def test_variance_computed(self, store):
        store.record_call(
            make_record(
                request_id="req-variance0000001",
                cost=Decimal("0.01"),
            )
        )
        reconciler = Reconciler(store)
        tmp = Path(tempfile.mkdtemp()) / "b.csv"
        tmp.write_text(
            "request_id,amount\nreq-variance0000001,0.015\n", encoding="utf-8"
        )
        reconciler.import_csv(tmp)

        summary = reconciler.summary()
        assert decimal_equals(summary["variance"], "0.005")

    def test_missing_file(self, store):
        reconciler = Reconciler(store)
        with pytest.raises(ReconciliationError, match="不存在"):
            reconciler.import_csv(Path(tempfile.mkdtemp()) / "nope.csv")


def decimal_equals(text: str, expected: str) -> bool:
    return Decimal(text) == Decimal(expected)


# ==========================================================================
# 9. 单价配置加载
# ==========================================================================
class TestPriceConfig:
    def test_load_from_json(self):
        tmp = Path(tempfile.mkdtemp()) / "pricing.json"
        tmp.write_text(
            json.dumps(
                {
                    "currency": "CNY",
                    "rules": [
                        {
                            "provider": "p",
                            "model": "m",
                            "input_per_million": "1.5",
                            "output_per_million": "3.0",
                            "price_version": "v1",
                            "source": "test",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        table = load_price_table(tmp)
        rule = table.lookup("p", "m")
        assert rule.input_per_million == Decimal("1.5")
        assert rule.output_per_million == Decimal("3.0")

    def test_example_config_is_valid(self):
        example = ROOT / "config" / "pricing.example.json"
        if not example.is_file():
            pytest.skip("示例配置不存在")
        table = load_price_table(example)
        assert len(table.rules()) >= 1

    def test_default_table_is_empty(self):
        from powercopy.cost.ledger import default_price_table

        table = default_price_table()
        assert table.rules() == []


# ==========================================================================
# 10. 门面与端到端
# ==========================================================================
class TestCostAccounting:
    def test_end_to_end(self):
        tmp = Path(tempfile.mkdtemp())
        accounting = CostAccounting(
            tmp / "cost.db",
            price_table=PriceTable([
                PriceRule(
                    provider="p", model="m",
                    input_per_million=Decimal("2"),
                    output_per_million=Decimal("8"),
                )
            ]),
            mode="live",
            provider="p",
            model="m",
        )

        recorder = accounting.recorder_for("task-e2e", "research")
        recorder.record_attempt(
            outcome=RequestOutcome.SUCCESS,
            usage=Usage(input_tokens=1_000_000, output_tokens=0),
        )

        accounting.task_outcome("task-e2e", "pending_review", drafts=1)

        report = accounting.daily_report()
        assert report.requests_total == 1
        assert report.drafts_generated == 1
        assert report.estimated_cost == Decimal("2.00000000")

        markdown = accounting.export_markdown()
        assert "Token 与费用日报" in markdown

        csv_text = accounting.export_csv()
        assert "day," in csv_text

    def test_no_price_table_all_unknown(self):
        tmp = Path(tempfile.mkdtemp())
        accounting = CostAccounting(tmp / "cost.db", mode="live",
                                    provider="p", model="m")
        recorder = accounting.recorder_for("task-x", "research")
        record = recorder.record_attempt(
            outcome=RequestOutcome.SUCCESS,
            usage=Usage(input_tokens=100, output_tokens=50),
        )
        assert record.cost_status is CostStatus.UNKNOWN
        assert record.estimated_cost is None


# ==========================================================================
# 11. 与 LLMClient 的接入
# ==========================================================================
class TestLLMIntegration:
    def _client(self):
        from powercopy.core.llm import LLMClient

        try:
            return LLMClient()
        except ValueError:
            pytest.skip("LLM 配置不完整，跳过")

    def test_mock_client_records_with_hook(self, priced_store, monkeypatch):
        """用假 _invoke_once 验证记录逻辑，不发起真实请求。"""
        store, table = priced_store
        client = self._client()

        def fake_invoke_once(messages, model=None, **kwargs):
            return {
                "content": "ok",
                "model": "test-model",
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            }

        monkeypatch.setattr(client, "_invoke_once", fake_invoke_once)

        recorder = CallRecorder(
            store, table, task_id="t-llm", stage="research",
            mode="live", provider="test-provider", model="test-model",
        )
        client.invoke(
            [{"role": "user", "content": "hi"}],
            cost_hook=recorder,
        )

        records = store.calls_for_task("t-llm")
        assert len(records) == 1
        assert records[0].usage.total_tokens == 150
        assert records[0].outcome is RequestOutcome.SUCCESS

    def test_failed_request_records_unknown(self, priced_store, monkeypatch):
        """失败请求必须记 unknown，不能记 0。"""
        from openai import APIConnectionError

        store, table = priced_store
        client = self._client()

        call_count = {"n": 0}

        from unittest.mock import MagicMock

        fake_request = MagicMock()
        fake_request.headers = {}

        def failing_invoke_once(messages, model=None, **kwargs):
            call_count["n"] += 1
            raise APIConnectionError(request=fake_request)

        monkeypatch.setattr(client, "_invoke_once", failing_invoke_once)
        # 只尝试一次，避免测试耗时
        client.retry_policy.max_attempts = 1

        recorder = CallRecorder(
            store, table, task_id="t-fail", stage="research",
            mode="live", provider="test-provider", model="test-model",
        )

        with pytest.raises(APIConnectionError):
            client.invoke(
                [{"role": "user", "content": "hi"}],
                cost_hook=recorder,
            )

        records = store.calls_for_task("t-fail")
        assert len(records) == 1
        assert records[0].outcome is RequestOutcome.API_ERROR
        assert records[0].usage.total_tokens is None
        assert records[0].estimated_cost is None
        assert records[0].cost_status is CostStatus.UNKNOWN

    def test_no_hook_no_records(self, priced_store, monkeypatch):
        store, table = priced_store
        client = self._client()

        monkeypatch.setattr(
            client,
            "_invoke_once",
            lambda messages, model=None, **kw: {
                "content": "ok", "model": "m",
                "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
            },
        )
        client.invoke([{"role": "user", "content": "hi"}])
        assert store.count() == 0

    def test_daily_guard_blocks_before_request(
        self, priced_store, monkeypatch
    ):
        store, table = priced_store
        client = self._client()

        called = {"n": 0}

        def counting_invoke(messages, model=None, **kwargs):
            called["n"] += 1
            return {
                "content": "ok", "model": "m",
                "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
            }

        monkeypatch.setattr(client, "_invoke_once", counting_invoke)

        guard = DailyBudgetGuard(
            DailyBudget(max_requests_per_day=1), store
        )
        store.record_call(make_record(request_id="req-existing00000001"))

        with pytest.raises(DailyBudgetExceeded):
            client.invoke(
                [{"role": "user", "content": "hi"}], daily_guard=guard
            )
        assert called["n"] == 0, "预算耗尽时不应发出请求"
