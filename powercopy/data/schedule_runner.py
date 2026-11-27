"""增量采集执行器：把到期目标交给 IngestionPipeline。

职责划分
--------
- `scheduling.py` 决定**哪些该跑**
- 本模块负责**怎么跑**，并把结果写回调度状态

关键约束
--------
1. **每次运行有总请求预算**（`max_requests_per_run`）。
   预算耗尽即停止，不会因为「今天目标多」就无限抓。
2. **403/429 立即停止本轮全部目标** —— 对方已经明确拒绝，
   继续抓其他目标既无礼也可能被封 IP。
3. **连续失败达阈值自动暂停该目标**，需人工 `--resume` 才能恢复。
   避免对已下线/改版的页面无休止重试。
4. **不伪造审批**。审批文件缺项的目标直接跳过并记录原因，
   不降级为「无审批也抓」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from powercopy.data.pipeline import IngestionPipeline, PipelineError
from powercopy.data.scheduling import (
    IngestSchedule,
    SchedulerState,
    build_plan,
)


class ScheduledIngestRunner:
    """按调度执行增量采集。"""

    def __init__(
        self,
        *,
        pipeline: IngestionPipeline,
        state: SchedulerState,
        schedule: IngestSchedule,
        approvals: dict | None = None,
    ):
        self.pipeline = pipeline
        self.state = state
        self.schedule = schedule
        self.approvals = approvals or {}

    def run(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        trigger: str = "manual",
        now: datetime | None = None,
    ) -> dict:
        """执行一轮。

        Args:
            force: 忽略到期判断，跑所有启用的目标
            dry_run: 只输出计划，不实际采集
            trigger: manual / schedule，写入运行记录
        """

        now = now or datetime.now(timezone.utc)
        run_id = uuid4().hex
        started_at = now.isoformat()

        states = self.state.all_states()
        plan = build_plan(self.schedule, states, now=now, force=force)
        due = [item for item in plan if item["due"]]

        if dry_run:
            return {
                "run_id": run_id,
                "dry_run": True,
                "trigger": trigger,
                "plan": plan,
                "due_count": len(due),
                "note": "dry-run 未发出任何网络请求",
            }

        if not due:
            self.state.record_run(
                run_id,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                trigger=trigger,
                status="nothing_due",
                due_count=0,
                report={"plan": plan},
            )
            return {
                "run_id": run_id,
                "status": "nothing_due",
                "due_count": 0,
                "plan": plan,
                "note": "没有到期的目标",
            }

        # ---- 审批与预算过滤 ----
        runnable: list[tuple[str, str]] = []
        skipped: list[dict] = []

        for item in due:
            key = item["target"]
            source_key, _, target_key = key.partition("/")

            approval = self.approvals.get(key) or {}
            if not (
                approval.get("approved") is True
                and isinstance(approval.get("evidence"), str)
                and approval["evidence"].strip()
                and approval.get("url")
            ):
                skipped.append(
                    {
                        "target": key,
                        "reason": "NO_OPERATOR_APPROVAL",
                        "detail": (
                            "config/ingestion_approval.json 中缺少该目标的有效审批"
                            "（需要 url / approved=true / evidence）"
                        ),
                    }
                )
                continue

            runnable.append((source_key, target_key))

        if not runnable:
            self.state.record_run(
                run_id,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                trigger=trigger,
                status="no_approved_targets",
                due_count=len(due),
                report={"plan": plan, "skipped": skipped},
            )
            return {
                "run_id": run_id,
                "status": "no_approved_targets",
                "due_count": len(due),
                "skipped": skipped,
                "plan": plan,
                "note": (
                    "到期的目标都缺少操作员审批，未发出任何请求。"
                    "请在 config/ingestion_approval.json 中补齐"
                ),
            }

        # ---- 执行 ----
        report: dict
        try:
            report = self.pipeline.live(
                runnable,
                index_selectors=_load_index_selectors(),
            )
        except PipelineError as exc:
            # 预算耗尽 / RUN_PAUSED 等
            self.state.record_run(
                run_id,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                trigger=trigger,
                status="pipeline_error",
                due_count=len(due),
                report={"plan": plan, "skipped": skipped, "error": str(exc)},
            )
            return {
                "run_id": run_id,
                "status": "pipeline_error",
                "error": str(exc),
                "due_count": len(due),
                "plan": plan,
                "skipped": skipped,
            }

        # ---- 回写状态 ----
        by_target = {}
        for item in report.get("items") or []:
            key = f"{item.get('source_key')}/{item.get('target_key')}"
            by_target[key] = item

            status = item.get("status") or "failed"
            self.state.record_attempt(key, status=status, run_id=run_id)

            if status != "success":
                state_row = self.state.get(key) or {}
                failures = int(state_row.get("consecutive_failures") or 0)
                if failures >= self.schedule.pause_after_failures:
                    self.state.pause(
                        key,
                        f"连续失败 {failures} 次，已自动暂停；"
                        f"排查后执行 --resume {key}",
                    )

            # 403/429 → 暂停该目标并标记整轮应停止
            for attempt in item.get("attempts") or []:
                if attempt.get("http_status") in (403, 429):
                    self.state.pause(
                        key,
                        f"目标站返回 {attempt.get('http_status')}，"
                        f"已暂停采集；请人工确认后再 --resume",
                    )

        summary = {
            "run_id": run_id,
            "status": report.get("status"),
            "due_count": len(due),
            "ran": len(runnable),
            "requests": report.get("requests"),
            "items": [
                {
                    "target": f"{i.get('source_key')}/{i.get('target_key')}",
                    "status": i.get("status"),
                    "error_code": i.get("error_code"),
                }
                for i in report.get("items") or []
            ],
            "skipped": skipped,
            "plan": plan,
        }

        self.state.record_run(
            run_id,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            trigger=trigger,
            status=str(report.get("status")),
            due_count=len(due),
            report=summary,
        )

        return summary


def _load_index_selectors() -> dict:
    """加载列表页的选择器配置。

    缺少该文件时返回空 dict —— pipeline 会对 index 类目标报
    `MISSING_INDEX_SELECTOR`，这是明确失败而非静默跳过。
    """

    import json

    root = Path(__file__).resolve().parents[2]
    path = root / "config" / "index_selectors.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


__all__ = ["ScheduledIngestRunner"]
