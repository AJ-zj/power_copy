"""定时增量采集专项测试。

覆盖：
- 到期判断（daily / weekly / manual / 暂停 / 禁用）
- 计划生成
- 状态存储（记录、暂停、恢复）
- 执行器（无审批跳过、审批齐全、失败计数、403 暂停）
- 预算耗尽
- 配置加载与序列化

**不发起任何真实网络请求**：所有 fetcher/pipeline 都是替身。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from powercopy.data.scheduling import (
    Frequency,
    IngestSchedule,
    SchedulerState,
    TargetSchedule,
    build_plan,
    default_schedule,
    is_due,
    load_schedule,
    next_due,
    schedule_to_dict,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def state():
    tmp = Path(tempfile.mkdtemp())
    return SchedulerState(tmp / "schedule.db")


def target(
    source: str = "ndrc",
    key: str = "policy_index",
    freq: Frequency = Frequency.DAILY,
    enabled: bool = True,
) -> TargetSchedule:
    return TargetSchedule(
        source_key=source, target_key=key,
        frequency=freq, enabled=enabled,
    )


# ==========================================================================
# 1. 到期判断
# ==========================================================================
class TestDueCalculation:
    def test_never_run_is_due(self):
        assert is_due(target(), None, now=NOW) is True

    def test_daily_not_due_within_24h(self):
        state_row = {
            "last_success_at": (NOW - timedelta(hours=2)).isoformat(),
            "paused": 0,
        }
        assert is_due(target(), state_row, now=NOW) is False

    def test_daily_due_after_24h(self):
        state_row = {
            "last_success_at": (NOW - timedelta(days=1, hours=2)).isoformat(),
            "paused": 0,
        }
        assert is_due(target(), state_row, now=NOW) is True

    def test_weekly_not_due_within_week(self):
        state_row = {
            "last_success_at": (NOW - timedelta(days=6)).isoformat(),
            "paused": 0,
        }
        assert is_due(target(freq=Frequency.WEEKLY), state_row, now=NOW) is False

    def test_weekly_due_after_week(self):
        state_row = {
            "last_success_at": (NOW - timedelta(days=7, hours=7)).isoformat(),
            "paused": 0,
        }
        assert is_due(target(freq=Frequency.WEEKLY), state_row, now=NOW) is True

    def test_manual_never_due(self):
        assert is_due(target(freq=Frequency.MANUAL), None, now=NOW) is False

    def test_paused_not_due(self):
        state_row = {
            "last_success_at": (NOW - timedelta(days=30)).isoformat(),
            "paused": 1,
        }
        assert is_due(target(), state_row, now=NOW) is False

    def test_disabled_not_due(self):
        assert is_due(target(enabled=False), None, now=NOW) is False

    def test_next_due_uses_last_success_not_attempt(self):
        """失败后仍应重试：到期基准是上次成功，不是上次尝试。"""
        state_row = {
            "last_success_at": (NOW - timedelta(days=3)).isoformat(),
            "last_attempt_at": (NOW - timedelta(minutes=5)).isoformat(),
            "paused": 0,
        }
        # 上次成功是 3 天前，daily 应已到期
        assert is_due(target(), state_row, now=NOW) is True

    def test_tolerance_prevents_boundary_skip(self):
        """差几分钟不到 24 小时也应算到期，避免整天不跑。"""
        state_row = {
            "last_success_at": (NOW - timedelta(hours=23)).isoformat(),
            "paused": 0,
        }
        assert is_due(target(), state_row, now=NOW) is True


# ==========================================================================
# 2. 计划生成
# ==========================================================================
class TestBuildPlan:
    def test_plan_includes_all_targets(self):
        schedule = default_schedule()
        plan = build_plan(schedule, {}, now=NOW)
        assert len(plan) == len(schedule.targets)

    def test_plan_marks_due(self):
        schedule = IngestSchedule(targets=[target()])
        plan = build_plan(schedule, {}, now=NOW)
        assert plan[0]["due"] is True
        assert plan[0]["reason"] == "已到期"

    def test_force_runs_manual(self):
        schedule = IngestSchedule(
            targets=[target(freq=Frequency.MANUAL)]
        )
        plan = build_plan(schedule, {}, now=NOW, force=False)
        assert plan[0]["due"] is False

        plan = build_plan(schedule, {}, now=NOW, force=True)
        assert plan[0]["due"] is True

    def test_plan_reports_paused_reason(self):
        schedule = IngestSchedule(targets=[target()])
        states = {
            "ndrc/policy_index": {
                "paused": 1, "paused_reason": "连续失败 3 次",
                "last_success_at": None,
            }
        }
        plan = build_plan(schedule, states, now=NOW)
        assert plan[0]["due"] is False
        assert "已暂停" in plan[0]["reason"]
        assert "连续失败 3 次" in plan[0]["reason"]

    def test_plan_reports_disabled(self):
        schedule = IngestSchedule(targets=[target(enabled=False)])
        plan = build_plan(schedule, {}, now=NOW)
        assert plan[0]["due"] is False
        assert plan[0]["reason"] == "已禁用"


# ==========================================================================
# 3. 状态存储
# ==========================================================================
class TestSchedulerState:
    def test_record_success(self, state):
        state.record_attempt("ndrc/policy_index", status="success", run_id="r1")
        row = state.get("ndrc/policy_index")
        assert row["last_status"] == "success"
        assert row["last_success_at"] is not None
        assert row["consecutive_failures"] == 0

    def test_failure_increments_counter(self, state):
        for _ in range(3):
            state.record_attempt("ndrc/policy_index", status="failed")
        row = state.get("ndrc/policy_index")
        assert row["consecutive_failures"] == 3

    def test_success_resets_counter(self, state):
        state.record_attempt("ndrc/policy_index", status="failed")
        state.record_attempt("ndrc/policy_index", status="failed")
        state.record_attempt("ndrc/policy_index", status="success")
        assert state.get("ndrc/policy_index")["consecutive_failures"] == 0

    def test_success_keeps_last_success(self, state):
        state.record_attempt("ndrc/policy_index", status="success")
        first = state.get("ndrc/policy_index")["last_success_at"]
        state.record_attempt("ndrc/policy_index", status="failed")
        assert state.get("ndrc/policy_index")["last_success_at"] == first

    def test_pause_and_resume(self, state):
        state.record_attempt("ndrc/policy_index", status="failed")
        state.pause("ndrc/policy_index", "test reason")
        assert state.get("ndrc/policy_index")["paused"] == 1

        assert state.resume("ndrc/policy_index") is True
        row = state.get("ndrc/policy_index")
        assert row["paused"] == 0
        assert row["consecutive_failures"] == 0

    def test_resume_unknown_target(self, state):
        assert state.resume("nope/nope") is False

    def test_record_and_read_runs(self, state):
        state.record_run(
            "run-1",
            started_at=NOW.isoformat(),
            finished_at=NOW.isoformat(),
            trigger="manual",
            status="success",
            due_count=2,
            report={"a": 1},
        )
        runs = state.recent_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "success"
        assert runs[0]["due_count"] == 2

    def test_all_states(self, state):
        state.record_attempt("a/b", status="success")
        state.record_attempt("c/d", status="failed")
        assert set(state.all_states().keys()) == {"a/b", "c/d"}


# ==========================================================================
# 4. 配置
# ==========================================================================
class TestScheduleConfig:
    def test_default_has_daily_index_and_weekly_detail(self):
        schedule = default_schedule()
        by_key = schedule.by_key()

        # 列表页按天
        assert by_key["ndrc/policy_index"].frequency is Frequency.DAILY
        # 详情页按周
        assert by_key["ndrc/market_rules"].frequency is Frequency.WEEKLY
        assert by_key["nea/registration_rules"].frequency is Frequency.WEEKLY

    def test_serialize(self):
        payload = schedule_to_dict(default_schedule())
        assert payload["max_requests_per_run"] >= 1
        assert len(payload["targets"]) >= 3
        assert all("frequency" in t for t in payload["targets"])

    def test_load_from_json(self):
        tmp = Path(tempfile.mkdtemp()) / "sched.json"
        tmp.write_text(
            json.dumps(
                {
                    "max_requests_per_run": 4,
                    "targets": [
                        {"target": "ndrc/policy_index", "frequency": "daily"},
                        {
                            "source_key": "nea",
                            "target_key": "registration_rules",
                            "frequency": "weekly",
                            "enabled": False,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        schedule = load_schedule(tmp)
        assert schedule.max_requests_per_run == 4
        assert len(schedule.targets) == 2
        assert schedule.by_key()["nea/registration_rules"].enabled is False

    def test_missing_file_uses_default(self):
        schedule = load_schedule(Path(tempfile.mkdtemp()) / "nope.json")
        assert len(schedule.targets) == len(default_schedule().targets)

    def test_invalid_frequency_rejected(self):
        with pytest.raises((ValidationError, ValueError)):
            TargetSchedule(
                source_key="a", target_key="b", frequency="hourly"
            )

    def test_bounds_enforced(self):
        with pytest.raises(ValidationError):
            IngestSchedule(max_requests_per_run=0)
        with pytest.raises(ValidationError):
            IngestSchedule(interval_seconds=0.5)


# ==========================================================================
# 5. 执行器
# ==========================================================================
class FakePipeline:
    """替身：不发网络请求。"""

    def __init__(self, results: dict | None = None, raise_error: str | None = None):
        self.results = results or {}
        self.raise_error = raise_error
        self.calls: list[list] = []

    def live(self, targets, index_selectors=None):
        self.calls.append(list(targets))
        if self.raise_error:
            from powercopy.data.pipeline import PipelineError

            raise PipelineError(self.raise_error)
        items = []
        for source, key in targets:
            name = f"{source}/{key}"
            preset = self.results.get(name, {"status": "success"})
            items.append(
                {
                    "source_key": source,
                    "target_key": key,
                    "status": preset["status"],
                    "attempts": preset.get("attempts", []),
                    "error_code": preset.get("error_code"),
                }
            )
        return {
            "status": "success",
            "requests": len(targets),
            "items": items,
        }


def build_runner(state, pipeline, approvals):
    from powercopy.data.schedule_runner import ScheduledIngestRunner

    return ScheduledIngestRunner(
        pipeline=pipeline,
        state=state,
        schedule=IngestSchedule(
            targets=[
                target("ndrc", "policy_index", Frequency.DAILY),
                target("nea", "registration_rules", Frequency.WEEKLY),
            ],
            max_requests_per_run=4,
            pause_after_failures=2,
        ),
        approvals=approvals,
    )


APPROVALS = {
    "ndrc/policy_index": {
        "url": "https://example.invalid/a",
        "approved": True,
        "evidence": "已核对 robots.txt",
    },
    "nea/registration_rules": {
        "url": "https://example.invalid/b",
        "approved": True,
        "evidence": "已核对使用条款",
    },
}


class TestScheduledRunner:
    def test_dry_run_sends_no_request(self, state):
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, {})
        result = runner.run(dry_run=True, now=NOW)

        assert result["dry_run"] is True
        assert pipeline.calls == []
        assert result["due_count"] == 2

    def test_nothing_due(self, state):
        for key in ("ndrc/policy_index", "nea/registration_rules"):
            state.record_attempt(
                key, status="success", at=NOW.isoformat()
            )
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, APPROVALS)
        result = runner.run(now=NOW)

        assert result["status"] == "nothing_due"
        assert pipeline.calls == []

    def test_no_approval_skips_all(self, state):
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, {})
        result = runner.run(now=NOW)

        assert result["status"] == "no_approved_targets"
        assert pipeline.calls == [], "无审批时绝不能发出请求"
        assert len(result["skipped"]) == 2
        assert all(
            s["reason"] == "NO_OPERATOR_APPROVAL" for s in result["skipped"]
        )

    def test_partial_approval_runs_only_approved(self, state):
        pipeline = FakePipeline()
        only_ndrc = {"ndrc/policy_index": APPROVALS["ndrc/policy_index"]}
        runner = build_runner(state, pipeline, only_ndrc)
        result = runner.run(now=NOW)

        assert len(pipeline.calls[0]) == 1
        assert pipeline.calls[0][0] == ("ndrc", "policy_index")
        assert len(result["skipped"]) == 1

    def test_success_written_to_state(self, state):
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, APPROVALS)
        runner.run(now=NOW)

        row = state.get("ndrc/policy_index")
        assert row["last_status"] == "success"
        assert row["last_success_at"] is not None

    def test_failure_counted(self, state):
        pipeline = FakePipeline(
            {"ndrc/policy_index": {"status": "failed", "error_code": "http_500"}}
        )
        runner = build_runner(state, pipeline, APPROVALS)
        runner.run(now=NOW)

        row = state.get("ndrc/policy_index")
        assert row["last_status"] == "failed"
        assert row["consecutive_failures"] == 1

    def test_auto_pause_after_threshold(self, state):
        pipeline = FakePipeline(
            {"ndrc/policy_index": {"status": "failed", "error_code": "http_500"}}
        )
        runner = build_runner(state, pipeline, APPROVALS)

        # pause_after_failures=2
        runner.run(now=NOW)
        runner.run(now=NOW)

        row = state.get("ndrc/policy_index")
        assert row["paused"] == 1
        assert "连续失败" in row["paused_reason"]

    def test_403_pauses_immediately(self, state):
        pipeline = FakePipeline(
            {
                "ndrc/policy_index": {
                    "status": "failed",
                    "attempts": [{"http_status": 403}],
                }
            }
        )
        runner = build_runner(state, pipeline, APPROVALS)
        runner.run(now=NOW)

        row = state.get("ndrc/policy_index")
        assert row["paused"] == 1
        assert "403" in row["paused_reason"]

    def test_pipeline_error_recorded(self, state):
        pipeline = FakePipeline(raise_error="REQUEST_BUDGET_EXHAUSTED")
        runner = build_runner(state, pipeline, APPROVALS)
        result = runner.run(now=NOW)

        assert result["status"] == "pipeline_error"
        assert "REQUEST_BUDGET_EXHAUSTED" in result["error"]

    def test_force_reruns_after_success(self, state):
        state.record_attempt(
            "ndrc/policy_index", status="success", at=NOW.isoformat()
        )
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, APPROVALS)
        result = runner.run(force=True, now=NOW)

        assert result["status"] == "success"
        assert len(pipeline.calls[0]) >= 1

    def test_run_recorded(self, state):
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, APPROVALS)
        result = runner.run(now=NOW)

        runs = state.recent_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == result["run_id"]

    def test_approval_requires_evidence(self, state):
        """审批缺 evidence 视为无效，不能放行。"""
        bad = {
            "ndrc/policy_index": {
                "url": "https://example.invalid/a",
                "approved": True,
                "evidence": "   ",   # 空白
            }
        }
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, bad)
        result = runner.run(now=NOW)

        assert result["status"] == "no_approved_targets"
        assert pipeline.calls == []

    def test_approval_requires_url(self, state):
        bad = {
            "ndrc/policy_index": {
                "approved": True,
                "evidence": "已核对",
            }
        }
        pipeline = FakePipeline()
        runner = build_runner(state, pipeline, bad)
        result = runner.run(now=NOW)
        assert pipeline.calls == []


# ==========================================================================
# 6. 采集审批模板
# ==========================================================================
class TestApprovalTemplate:
    def test_example_template_exists(self):
        example = ROOT / "config" / "ingestion_approval.example.json"
        if not example.is_file():
            pytest.skip("模板不存在")
        payload = json.loads(example.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)

    def test_real_approval_not_committed(self):
        """真实审批文件不得入库（含操作员确认信息）。"""
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "ingestion_approval.json" in gitignore


# ==========================================================================
# 7. 调度配置文件可入仓库
# ==========================================================================
class TestScheduleFileCommitted:
    def test_schedule_config_not_gitignored(self):
        """调度配置不含敏感信息，应可提交。"""
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "ingest_schedule.json" not in gitignore
