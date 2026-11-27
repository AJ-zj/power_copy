"""定时增量采集调度（MVP-05-C）。

设计边界
--------
**这不是一个通用调度框架**，只解决本项目的一个具体问题：
「按天扫列表页、按周抓详情页，把新政策自动入库」。

因此刻意做小：

- 不做 cron 表达式解析（只有 daily / weekly / manual 三种）
- 不做分布式协调（单进程，靠 SQLite 状态表防重）
- 不做任务队列（顺序执行，与既有 `IngestionPipeline` 一致）
- **不伪造采集审批**。审批仍由操作员在 `config/ingestion_approval.json`
  里逐条填写，调度器只是决定「哪些审批过的目标该跑了」

为什么用「到期判断」而不是「后台常驻」
--------------------------------------
常驻进程需要处理崩溃重启、时钟漂移、单实例锁等一堆问题。
用「一次性执行 + 操作系统计划任务定时调用」更简单可靠：
每次运行只处理到期的目标，跑完退出。状态落在 SQLite，
所以重复调用不会重复采集（内容哈希未变也不产生新版本）。

时间基准
--------
调度判断用 **UTC** 存储、按 **UTC+8** 计算到期，因为业务方在国内。
「每周」按北京时间的自然周（周一 00:00 起算）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

BEIJING = timezone(timedelta(hours=8))

# 到期判定留一点余量，避免「差 3 秒不到期」导致整天不跑
_DAILY_TOLERANCE = timedelta(hours=1)
_WEEKLY_TOLERANCE = timedelta(hours=6)


class Frequency(StrEnum):
    """采集频率。"""

    DAILY = "daily"
    """每天一次（用于列表页发现新政策）"""

    WEEKLY = "weekly"
    """每周一次（用于详情页正文抓取）"""

    MANUAL = "manual"
    """只在 --force 时采集"""


class TargetSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str = Field(min_length=1, max_length=60)
    target_key: str = Field(min_length=1, max_length=60)
    frequency: Frequency = Frequency.WEEKLY
    enabled: bool = True
    note: str = Field(default="", max_length=200)

    @property
    def key(self) -> str:
        return f"{self.source_key}/{self.target_key}"


class IngestSchedule(BaseModel):
    """调度配置。"""

    model_config = ConfigDict(extra="forbid")

    targets: list[TargetSchedule] = Field(default_factory=list)
    max_requests_per_run: int = Field(default=8, ge=1, le=100)
    interval_seconds: float = Field(default=2.0, ge=2.0, le=60.0)
    pause_after_failures: int = Field(
        default=3,
        ge=1,
        le=10,
        description="连续失败多少次后暂停该目标（需人工介入）",
    )

    def by_key(self) -> dict[str, TargetSchedule]:
        return {t.key: t for t in self.targets}

    def enabled_targets(self) -> list[TargetSchedule]:
        return [t for t in self.targets if t.enabled]


def default_schedule() -> IngestSchedule:
    """按当前已注册的源生成默认调度。

    列表页每天扫（发现新政策），详情页每周抓（正文可能更新）。

    这与本项目的实际数据形态一致：政策列表经常更新，
    但已发布的政策正文很少改动。
    """

    return IngestSchedule(
        targets=[
            TargetSchedule(
                source_key="ndrc",
                target_key="policy_index",
                frequency=Frequency.DAILY,
                note="发改委政策列表，发现新发布",
            ),
            TargetSchedule(
                source_key="ndrc",
                target_key="market_rules",
                frequency=Frequency.WEEKLY,
                note="电力市场运行基本规则正文",
            ),
            TargetSchedule(
                source_key="nea",
                target_key="registration_rules",
                frequency=Frequency.WEEKLY,
                note="电力市场注册基本规则正文",
            ),
            TargetSchedule(
                source_key="nea_east",
                target_key="policy_index",
                frequency=Frequency.DAILY,
                note="华东监管局规范性文件列表",
            ),
            TargetSchedule(
                source_key="nea_east",
                target_key="anhui_market_news",
                frequency=Frequency.WEEKLY,
                note="安徽市场动态（仅归档，不入政策库）",
            ),
        ]
    )


# ---------------------------------------------------------------- 状态存储

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_state (
    target_key       TEXT PRIMARY KEY,
    source_key       TEXT NOT NULL,
    target_key_part  TEXT NOT NULL,
    last_attempt_at  TEXT,
    last_success_at  TEXT,
    last_status      TEXT,
    last_run_id      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    paused           INTEGER NOT NULL DEFAULT 0,
    paused_reason    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    trigger      TEXT NOT NULL,
    status       TEXT NOT NULL,
    due_count    INTEGER NOT NULL DEFAULT 0,
    report_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON ingest_runs(started_at);
"""


class SchedulerState:
    """调度状态（SQLite）。

    与 `VersionedPolicyStore` 共用同一个 db 文件也可以，但默认分开：
    `storage/ingest/schedule.db`。理由是采集状态是运行期数据，
    与政策库的生命周期不同（政策库需要备份，采集状态丢了只是重跑一次）。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM ingest_state WHERE target_key = ?", (key,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def all_states(self) -> dict[str, dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM ingest_state").fetchall()
            return {row["target_key"]: dict(row) for row in rows}
        finally:
            conn.close()

    def record_attempt(
        self,
        key: str,
        *,
        status: str,
        run_id: str = "",
        at: str | None = None,
    ) -> None:
        """记录一次采集尝试。

        成功会把 consecutive_failures 清零并解暂停；
        失败会累加，达到阈值后置 paused。
        """

        now = at or datetime.now(timezone.utc).isoformat()
        source_key, _, target_key = key.partition("/")

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM ingest_state WHERE target_key = ?", (key,)
            ).fetchone()

            if row is None:
                failures = 0 if status == "success" else 1
                conn.execute(
                    """
                    INSERT INTO ingest_state
                        (target_key, source_key, target_key_part,
                         last_attempt_at, last_success_at, last_status,
                         last_run_id, consecutive_failures, paused, paused_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        source_key,
                        target_key,
                        now,
                        now if status == "success" else None,
                        status,
                        run_id,
                        failures,
                        0,
                        "",
                    ),
                )
            else:
                failures = (
                    0 if status == "success"
                    else int(row["consecutive_failures"]) + 1
                )
                last_success = (
                    now if status == "success" else row["last_success_at"]
                )
                conn.execute(
                    """
                    UPDATE ingest_state SET
                        last_attempt_at = ?,
                        last_success_at = ?,
                        last_status = ?,
                        last_run_id = ?,
                        consecutive_failures = ?
                    WHERE target_key = ?
                    """,
                    (now, last_success, status, run_id, failures, key),
                )
            conn.commit()
        finally:
            conn.close()

    def pause(self, key: str, reason: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE ingest_state SET paused = 1, paused_reason = ? "
                "WHERE target_key = ?",
                (reason[:200], key),
            )
            conn.commit()
        finally:
            conn.close()

    def resume(self, key: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE ingest_state SET paused = 0, paused_reason = '', "
                "consecutive_failures = 0 WHERE target_key = ?",
                (key,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def record_run(
        self,
        run_id: str,
        *,
        started_at: str,
        trigger: str,
        status: str,
        due_count: int,
        finished_at: str | None = None,
        report: dict | None = None,
    ) -> None:
        import json

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO ingest_runs
                    (run_id, started_at, finished_at, trigger, status,
                     due_count, report_json)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    started_at,
                    finished_at,
                    trigger,
                    status,
                    due_count,
                    json.dumps(report or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def recent_runs(self, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------- 到期判断


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def next_due(
    schedule: TargetSchedule,
    state: dict | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """计算下次到期时间。None 表示永不到期（manual 或已暂停）。

    到期基准是 **上次成功**，不是上次尝试：
    失败后仍会在下次运行时重试（除非已暂停）。
    """

    if schedule.frequency is Frequency.MANUAL:
        return None

    now = now or datetime.now(timezone.utc)

    if state and state.get("paused"):
        return None

    last_success = _parse(state.get("last_success_at")) if state else None

    if last_success is None:
        # 从未成功过 → 立即到期
        return now

    if schedule.frequency is Frequency.DAILY:
        return last_success + timedelta(days=1) - _DAILY_TOLERANCE

    if schedule.frequency is Frequency.WEEKLY:
        return last_success + timedelta(days=7) - _WEEKLY_TOLERANCE

    return None


def is_due(
    schedule: TargetSchedule,
    state: dict | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not schedule.enabled:
        return False
    due = next_due(schedule, state, now=now)
    if due is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now >= due


def build_plan(
    schedule: IngestSchedule,
    states: dict[str, dict],
    *,
    now: datetime | None = None,
    force: bool = False,
) -> list[dict]:
    """生成执行计划（不执行，只判断）。

    Returns:
        每个目标一条，含 `due` / `reason` / `next_due_at`
    """

    now = now or datetime.now(timezone.utc)
    plan = []

    for target in schedule.targets:
        state = states.get(target.key)
        due = is_due(target, state, now=now)
        due_at = next_due(target, state, now=now)

        if not target.enabled:
            reason = "已禁用"
            run = False
        elif state and state.get("paused"):
            reason = f"已暂停：{state.get('paused_reason') or '连续失败'}"
            run = False
        elif target.frequency is Frequency.MANUAL:
            reason = "仅手动触发" if not force else "强制运行"
            run = force
        elif force:
            reason = "强制运行"
            run = True
        elif due:
            reason = "已到期"
            run = True
        else:
            reason = "未到期"
            run = False

        plan.append(
            {
                "target": target.key,
                "frequency": str(target.frequency),
                "due": run,
                "reason": reason,
                "next_due_at": due_at.isoformat() if due_at else None,
                "last_success_at": state.get("last_success_at") if state else None,
                "last_status": state.get("last_status") if state else None,
                "consecutive_failures": (
                    state.get("consecutive_failures", 0) if state else 0
                ),
            }
        )

    return plan


def schedule_to_dict(schedule: IngestSchedule) -> dict:
    return {
        "max_requests_per_run": schedule.max_requests_per_run,
        "interval_seconds": schedule.interval_seconds,
        "pause_after_failures": schedule.pause_after_failures,
        "targets": [
            {
                "target": t.key,
                "frequency": str(t.frequency),
                "enabled": t.enabled,
                "note": t.note,
            }
            for t in schedule.targets
        ],
    }


def load_schedule(path: str | Path | None) -> IngestSchedule:
    """从 JSON 加载调度配置。None 或缺文件时用默认调度。"""

    if path is None:
        return default_schedule()

    file = Path(path)
    if not file.is_file():
        return default_schedule()

    import json

    payload = json.loads(file.read_text(encoding="utf-8"))

    targets = []
    for item in payload.get("targets") or []:
        # 允许简写 {"target": "ndrc/policy_index", ...}
        if "target" in item and "source_key" not in item:
            source_key, _, target_key = str(item["target"]).partition("/")
        else:
            source_key = item["source_key"]
            target_key = item["target_key"]
        targets.append(
            TargetSchedule(
                source_key=source_key,
                target_key=target_key,
                frequency=Frequency(item.get("frequency", "weekly")),
                enabled=bool(item.get("enabled", True)),
                note=str(item.get("note", "")),
            )
        )

    if not targets:
        return default_schedule()

    return IngestSchedule(
        targets=targets,
        max_requests_per_run=int(
            payload.get("max_requests_per_run",
                        default_schedule().max_requests_per_run)
        ),
        interval_seconds=float(
            payload.get("interval_seconds",
                        default_schedule().interval_seconds)
        ),
        pause_after_failures=int(
            payload.get("pause_after_failures",
                        default_schedule().pause_after_failures)
        ),
    )


__all__ = [
    "BEIJING",
    "Frequency",
    "IngestSchedule",
    "SchedulerState",
    "TargetSchedule",
    "build_plan",
    "default_schedule",
    "is_due",
    "load_schedule",
    "next_due",
    "schedule_to_dict",
]
