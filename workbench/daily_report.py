"""Read local Content Workflow exports and development records for the daily report."""

from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import tomllib

from fastapi import APIRouter


REPORT_ZONE = timezone(timedelta(hours=8))
MAX_EXPORT_BYTES = 4 * 1024 * 1024
MAX_COMMITS = 100
WORKFLOW_STATUSES = {
    "draft_ready", "pending_review", "needs_manual_review", "aborted", "budget_exhausted", "failed",
}
REVIEW_STATUSES = {"passed", "needs_revision", "pending_review", "failed"}
TASK_ID = re.compile(r"task-[0-9a-f]{16}")


def _local_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("Timestamp must include its offset")
    return timestamp.astimezone(REPORT_ZONE)


def _workflow_rows(project: Path, day: date) -> tuple[list[dict], int]:
    storage = project / "storage" / "workflow"
    if storage.resolve() != storage:
        raise ValueError("Unexpected workflow archive location")
    rows = {}
    ignored = 0
    for export in sorted(storage.glob("*.json")):
        try:
            if export.resolve() != export or not export.is_file():
                continue
            with export.open("rb") as handle:
                raw = handle.read(MAX_EXPORT_BYTES + 1)
            if len(raw) > MAX_EXPORT_BYTES:
                raise ValueError("Workflow export is too large")
            record = json.loads(raw.decode("utf-8-sig"))
            if record["schema_version"] != "content-workflow-v1":
                raise ValueError("Unknown workflow schema")
            created = _local_timestamp(record["created_at"])
            task_id = record["task_id"]
            mode, status = record["mode"], record["final_status"]
            if not TASK_ID.fullmatch(task_id) or mode not in {"mock", "live"} or status not in WORKFLOW_STATUSES:
                raise ValueError("Invalid workflow metadata")
            if created.date() != day:
                continue
            draft, review = record.get("draft"), record.get("review")
            title = draft.get("title") if draft and draft.get("status") == "pending_review" else None
            review_status = review.get("status") if review else None
            if title is not None and not isinstance(title, str):
                raise ValueError("Invalid draft title")
            if review_status is not None and review_status not in REVIEW_STATUSES:
                raise ValueError("Invalid review status")
            rows.setdefault(task_id, {
                "task_id": task_id,
                "created_at": created.isoformat(),
                "mode": mode,
                "status": status,
                "draft_title": title,
                "review_status": review_status,
            })
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            ignored += 1
    return sorted(rows.values(), key=lambda row: (row["created_at"], row["task_id"]), reverse=True), ignored


def _git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=project, capture_output=True, encoding="utf-8", errors="replace",
        check=True, timeout=5,
    )
    return result.stdout.rstrip("\r\n")


def _git_report(project: Path, day: date) -> dict | None:
    start = datetime.combine(day, time.min, REPORT_ZONE)
    try:
        end = start + timedelta(days=1, seconds=-1)
        output = _git(project, "log", "--no-color", f"--max-count={MAX_COMMITS + 1}",
                      f"--since-as-filter={start.isoformat()}", f"--until={end.isoformat()}",
                      "--format=%H%x09%cI%x09%s", "HEAD", "--")
        rows = []
        for line in output.splitlines():
            sha, timestamp, subject = line.split("\t", 2)
            if _local_timestamp(timestamp).date() == day:
                rows.append({"sha": sha, "committed_at": timestamp, "subject": subject})
        return {"head": _git(project, "rev-parse", "HEAD"),
                "commits": rows[:MAX_COMMITS], "truncated": len(rows) > MAX_COMMITS}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _test_report(workspace: Path, day: date) -> dict | None:
    try:
        path = workspace / "docs" / "workbench-test-results.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        if _local_timestamp(report["verified_at"]).date() != day:
            return None
        return {field: report[field] for field in ("verified_at", "scope", "suites")}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def build_daily_report_router(project: Path, workspace: Path) -> APIRouter:
    router = APIRouter(prefix="/reports")

    @router.get("/daily")
    def daily_report(day: date | None = None):
        selected_day = day or datetime.now(REPORT_ZONE).date()
        try:
            runs, ignored = _workflow_rows(project, selected_day)
        except (OSError, ValueError):
            runs, ignored = None, None
        with (project / "pyproject.toml").open("rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
        return {"data": {
            "day": selected_day.isoformat(),
            "timezone": "Asia/Shanghai",
            "current_version": version,
            "git": _git_report(project, selected_day),
            "test_snapshot": _test_report(workspace, selected_day),
            "workflow_runs": runs,
            "ignored_workflow_exports": ignored,
            "approval_available": False,
            "cost_accounting_available": False,
        }}

    return router
