"""定时增量采集 CLI。

用法
----
    # 查看计划（不发请求）
    python scripts/run_ingestion_schedule.py --plan

    # 执行到期的目标
    python scripts/run_ingestion_schedule.py

    # 强制跑所有启用目标
    python scripts/run_ingestion_schedule.py --force

    # 查看最近运行记录
    python scripts/run_ingestion_schedule.py --history

    # 查看/恢复被暂停的目标
    python scripts/run_ingestion_schedule.py --status
    python scripts/run_ingestion_schedule.py --resume ndrc/policy_index

    # 导出调度配置（生成 config/ingest_schedule.json 模板）
    python scripts/run_ingestion_schedule.py --write-default

安全约束
--------
- 默认 **dry-run 不需要审批**，但真实采集必须有审批
- 审批文件：`config/ingestion_approval.json`（gitignore，不入公开仓库）
- 无审批的目标直接跳过，不降级、不伪造
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.pipeline import IngestionPipeline  # noqa: E402
from powercopy.data.schedule_runner import ScheduledIngestRunner  # noqa: E402
from powercopy.data.scheduling import (  # noqa: E402
    SchedulerState,
    build_plan,
    default_schedule,
    load_schedule,
    schedule_to_dict,
)
from powercopy.data.sources import build_source_registry  # noqa: E402
from powercopy.data.versioning import VersionedPolicyStore  # noqa: E402

DEFAULT_DB = ROOT / "storage" / "policies_versioned.db"
DEFAULT_STATE = ROOT / "storage" / "ingest" / "schedule.db"
DEFAULT_APPROVAL = ROOT / "config" / "ingestion_approval.json"
DEFAULT_SCHEDULE = ROOT / "config" / "ingest_schedule.json"
DEFAULT_PROFILES = ROOT / "config" / "parser_profiles.json"
DEFAULT_SELECTORS = ROOT / "config" / "index_selectors.json"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="定时增量采集")
    parser.add_argument("--plan", action="store_true", help="只输出计划")
    parser.add_argument("--force", action="store_true", help="忽略到期判断")
    parser.add_argument("--history", action="store_true", help="最近运行记录")
    parser.add_argument("--status", action="store_true", help="各目标状态")
    parser.add_argument("--resume", metavar="TARGET", help="恢复被暂停的目标")
    parser.add_argument("--write-default", action="store_true",
                        help="写出默认调度配置模板")
    parser.add_argument("--json", type=Path, help="把结果写入文件")

    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    parser.add_argument("--approval", type=Path, default=DEFAULT_APPROVAL)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--selectors", type=Path, default=DEFAULT_SELECTORS)
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "storage" / "raw")
    parser.add_argument("--run-dir", type=Path,
                        default=ROOT / "storage" / "ingestion_runs")
    args = parser.parse_args()

    schedule = load_schedule(
        args.schedule if args.schedule.is_file() else None
    )
    state = SchedulerState(args.state_db)

    # ---- 只写配置模板 ----
    if args.write_default:
        args.schedule.parent.mkdir(parents=True, exist_ok=True)
        args.schedule.write_text(
            json.dumps(schedule_to_dict(schedule), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已写入调度配置模板：{args.schedule}")
        print()
        print("提示：frequency 可选 daily / weekly / manual；")
        print("      该文件可以提交到仓库（不含任何敏感信息）。")
        return 0

    # ---- 恢复暂停 ----
    if args.resume:
        if state.resume(args.resume):
            print(f"已恢复：{args.resume}")
            return 0
        print(f"未找到该目标的记录：{args.resume}", file=sys.stderr)
        return 2

    # ---- 状态 ----
    if args.status:
        states = state.all_states()
        plan = build_plan(schedule, states)
        print(f"{'目标':<32}{'频率':<10}{'状态':<12}{'上次成功':<26}{'下次到期'}")
        print("-" * 110)
        for item in plan:
            states_row = states.get(item["target"]) or {}
            paused = "已暂停" if states_row.get("paused") else "正常"
            print(
                f"{item['target']:<32}{item['frequency']:<10}{paused:<12}"
                f"{(item['last_success_at'] or '从未')[:19]:<26}"
                f"{(item['next_due_at'] or '—')[:19]}"
            )
        if not states:
            print("（尚无任何采集记录）")
        return 0

    # ---- 历史 ----
    if args.history:
        runs = state.recent_runs(limit=15)
        if not runs:
            print("（尚无运行记录）")
            return 0
        print(f"{'run_id':<34}{'开始时间':<26}{'触发':<10}{'状态':<18}{'到期数'}")
        print("-" * 100)
        for run in runs:
            print(
                f"{run['run_id']:<34}{run['started_at'][:19]:<26}"
                f"{run['trigger']:<10}{run['status']:<18}{run['due_count']}"
            )
        return 0

    # ---- 计划 ----
    if args.plan:
        states = state.all_states()
        plan = build_plan(schedule, states, force=args.force)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print()
        due = [p for p in plan if p["due"]]
        print(f"到期目标数：{len(due)} / {len(plan)}")
        if due:
            print("将执行：")
            for item in due:
                print(f"  - {item['target']}（{item['reason']}）")
        return 0

    # ---- 实际执行 ----
    approvals = _read_json(args.approval)
    if not approvals:
        print(
            f"注意：未找到审批文件 {args.approval}\n"
            f"      没有任何目标会被执行。\n"
            f"      模板见 config/ingestion_approval.example.json",
            file=sys.stderr,
        )

    profiles = _read_json(args.profiles)
    selectors = _read_json(args.selectors)
    registry = build_source_registry()

    from powercopy.data.fetcher import HttpFetcher

    pipeline = IngestionPipeline(
        VersionedPolicyStore(args.db),
        profiles,
        raw_dir=args.raw_dir,
        run_dir=args.run_dir,
        registry=registry,
        fetcher=HttpFetcher(registry),
        approvals=approvals,
        max_requests=schedule.max_requests_per_run,
        interval=schedule.interval_seconds,
    )

    runner = ScheduledIngestRunner(
        pipeline=pipeline,
        state=state,
        schedule=schedule,
        approvals=approvals,
    )

    result = runner.run(force=args.force, trigger="manual")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写入：{args.json}")

    status = result.get("status")
    return 0 if status in ("success", "nothing_due", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
