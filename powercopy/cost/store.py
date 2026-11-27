"""成本台账 SQLite 存储。

表
--
`model_calls`        每次实际 API 请求尝试一行
`reconciliations`    对账批次（账单导入 / 人工录入）
`task_outcomes`      任务级结果（用于日报的成功/失败/部分完成统计）

设计约束
--------
- 金额存 TEXT（Decimal 的字符串形式），避免 SQLite REAL 的浮点误差
- 时间存 ISO 8601 UTC 字符串，索引后用 `day_beijing` 生成的
  `beijing_day` 冗余列做按日查询（避免跨时区查询时逐行转换）
- **写入即可，不更新**：请求记录是不可变事件，重算对账时另写
  reconciliation 记录并在查询侧 join，不修改原始行
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from powercopy.cost.contracts import (
    CostStatus,
    ModelCallRecord,
    RequestOutcome,
    Usage,
)

SCHEMA_VERSION = "cost-accounting-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    request_id      TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL DEFAULT '',
    call_index      INTEGER NOT NULL DEFAULT 1,
    occurred_at     TEXT NOT NULL,
    beijing_day     TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT '',
    provider        TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cached_tokens   INTEGER,
    total_tokens    INTEGER,
    usage_complete  INTEGER NOT NULL DEFAULT 0,
    outcome         TEXT NOT NULL,
    retry_index     INTEGER NOT NULL DEFAULT 0,
    latency_ms      REAL NOT NULL DEFAULT 0,
    price_version   TEXT NOT NULL DEFAULT '',
    input_price     TEXT,
    output_price    TEXT,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    estimated_cost  TEXT,
    billed_cost     TEXT,
    cost_status     TEXT NOT NULL DEFAULT 'unknown',
    mode            TEXT NOT NULL DEFAULT 'mock',
    billed_ref      TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    prompt_chars    INTEGER NOT NULL DEFAULT 0,
    prompt_sha256   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_calls_day   ON model_calls(beijing_day);
CREATE INDEX IF NOT EXISTS idx_calls_task  ON model_calls(task_id);
CREATE INDEX IF NOT EXISTS idx_calls_stage ON model_calls(beijing_day, stage);

CREATE TABLE IF NOT EXISTS reconciliations (
    billed_ref    TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    source        TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    total_amount  TEXT NOT NULL,
    line_count    INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    detail_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS task_outcomes (
    task_id       TEXT PRIMARY KEY,
    beijing_day   TEXT NOT NULL,
    final_status  TEXT NOT NULL,
    drafts        INTEGER NOT NULL DEFAULT 0,
    mode          TEXT NOT NULL DEFAULT 'mock',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_day ON task_outcomes(beijing_day);
"""


class CostStore:
    """SQLite 成本台账。

    线程安全说明：每次操作独立开连接（WAL 模式下并发读友好）。
    不做连接池 —— 本项目是单机 CLI/工作台，连接开销可忽略。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES(?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )

    # ---- 写入 ----

    def record_call(self, record: ModelCallRecord) -> None:
        """写入一条请求记录。

        同一 request_id 重复写入时**忽略**（幂等），
        因为重跑不应重复计费。
        """

        usage = record.usage
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO model_calls (
                    request_id, task_id, call_index, occurred_at, beijing_day,
                    stage, provider, model,
                    input_tokens, output_tokens, cached_tokens, total_tokens,
                    usage_complete, outcome, retry_index, latency_ms,
                    price_version, input_price, output_price, currency,
                    estimated_cost, billed_cost, cost_status, mode,
                    billed_ref, note, prompt_chars, prompt_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.request_id,
                    record.task_id,
                    record.call_index,
                    record.occurred_at,
                    record.day_beijing(),
                    record.stage,
                    record.provider,
                    record.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                    usage.total_tokens,
                    1 if usage.complete else 0,
                    str(record.outcome),
                    record.retry_index,
                    record.latency_ms,
                    record.price_version,
                    _dec_to_text(record.input_price_per_million),
                    _dec_to_text(record.output_price_per_million),
                    record.currency,
                    _dec_to_text(record.estimated_cost),
                    _dec_to_text(record.billed_cost),
                    str(record.cost_status),
                    record.mode,
                    record.billed_ref,
                    record.note,
                    record.prompt_chars,
                    record.prompt_sha256,
                ),
            )

    def record_task_outcome(
        self,
        task_id: str,
        beijing_day: str,
        final_status: str,
        *,
        drafts: int = 0,
        mode: str = "mock",
        created_at: str | None = None,
    ) -> None:
        from powercopy.cost.contracts import utc_now_iso

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_outcomes
                    (task_id, beijing_day, final_status, drafts, mode, created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    final_status=excluded.final_status,
                    drafts=excluded.drafts
                """,
                (
                    task_id,
                    beijing_day,
                    final_status,
                    drafts,
                    mode,
                    created_at or utc_now_iso(),
                ),
            )

    # ---- 查询 ----

    def calls_for_day(self, day: str) -> list[ModelCallRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM model_calls WHERE beijing_day = ? "
                "ORDER BY occurred_at, call_index",
                (day,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def calls_for_task(self, task_id: str) -> list[ModelCallRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM model_calls WHERE task_id = ? "
                "ORDER BY occurred_at, call_index",
                (task_id,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def all_calls(self, limit: int = 10_000) -> list[ModelCallRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM model_calls ORDER BY occurred_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def task_outcomes_for_day(self, day: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_outcomes WHERE beijing_day = ?",
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def available_days(self, limit: int = 30) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT beijing_day FROM model_calls "
                "ORDER BY beijing_day DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row["beijing_day"] for row in rows]

    def count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM model_calls").fetchone()
        return int(row["n"])

    # ---- 对账 ----

    def record_reconciliation(
        self,
        billed_ref: str,
        *,
        source: str,
        total_amount: Decimal,
        line_count: int = 0,
        currency: str = "CNY",
        note: str = "",
        detail: dict | None = None,
        created_at: str | None = None,
    ) -> None:
        from powercopy.cost.contracts import utc_now_iso

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO reconciliations
                    (billed_ref, created_at, source, currency,
                     total_amount, line_count, note, detail_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    billed_ref,
                    created_at or utc_now_iso(),
                    source,
                    currency,
                    str(total_amount),
                    line_count,
                    note,
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )

    def reconciliations(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reconciliations ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_billed(
        self,
        request_ids: list[str],
        billed_ref: str,
        amounts: dict[str, Decimal] | None = None,
    ) -> int:
        """把指定请求标记为已对账，并写入账单金额。

        只更新 `billed_ref` / `cost_status` / `billed_cost`，
        **不覆盖 `estimated_cost`** —— 估算值必须保留，才能算差异。
        """

        if not request_ids:
            return 0

        placeholders = ",".join("?" for _ in request_ids)
        updated = 0

        with self._connect() as conn:
            if amounts:
                for request_id in request_ids:
                    amount = amounts.get(request_id)
                    if amount is None:
                        cursor = conn.execute(
                            f"UPDATE model_calls SET billed_ref = ?, "
                            f"cost_status = ? WHERE request_id = ?",
                            (billed_ref, str(CostStatus.BILLED), request_id),
                        )
                    else:
                        cursor = conn.execute(
                            f"UPDATE model_calls SET billed_ref = ?, "
                            f"cost_status = ?, billed_cost = ? "
                            f"WHERE request_id = ?",
                            (
                                billed_ref,
                                str(CostStatus.BILLED),
                                str(amount),
                                request_id,
                            ),
                        )
                    updated += max(0, cursor.rowcount)
            else:
                cursor = conn.execute(
                    f"UPDATE model_calls SET billed_ref = ?, cost_status = ? "
                    f"WHERE request_id IN ({placeholders})",
                    (billed_ref, str(CostStatus.BILLED), *request_ids),
                )
                updated = cursor.rowcount

        return updated


# ---------------------------------------------------------------- 内部


def _dec_to_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text_to_dec(text) -> Decimal | None:
    if text is None or text == "":
        return None
    return Decimal(str(text))


def _row_to_record(row: sqlite3.Row) -> ModelCallRecord:
    return ModelCallRecord(
        request_id=row["request_id"],
        task_id=row["task_id"],
        call_index=row["call_index"],
        occurred_at=row["occurred_at"],
        stage=row["stage"],
        provider=row["provider"],
        model=row["model"],
        usage=Usage(
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cached_tokens=row["cached_tokens"],
            total_tokens=row["total_tokens"],
            complete=bool(row["usage_complete"]),
        ),
        outcome=RequestOutcome(row["outcome"]),
        retry_index=row["retry_index"],
        latency_ms=row["latency_ms"],
        price_version=row["price_version"],
        input_price_per_million=_text_to_dec(row["input_price"]),
        output_price_per_million=_text_to_dec(row["output_price"]),
        currency=row["currency"],
        estimated_cost=_text_to_dec(row["estimated_cost"]),
        billed_cost=_text_to_dec(row["billed_cost"]),
        cost_status=CostStatus(row["cost_status"]),
        mode=row["mode"],
        billed_ref=row["billed_ref"],
        note=row["note"],
        prompt_chars=row["prompt_chars"],
        prompt_sha256=row["prompt_sha256"],
    )


__all__ = ["CostStore", "SCHEMA_VERSION"]
