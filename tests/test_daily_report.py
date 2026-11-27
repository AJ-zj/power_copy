import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from workbench.daily_report import build_daily_report_router


def test_daily_report_filters_deduplicates_and_exposes_only_summary(tmp_path):
    app = FastAPI()
    app.include_router(build_daily_report_router(tmp_path, tmp_path))
    client = TestClient(app)
    archive = tmp_path / "storage" / "workflow"
    archive.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.1.0"', encoding="utf-8")
    record = {
        "schema_version": "content-workflow-v1",
        "task_id": "task-0123456789abcdef",
        "created_at": "2026-11-26T16:05:00+00:00",
        "mode": "mock",
        "final_status": "pending_review",
        "draft": {"title": "测试草稿", "status": "pending_review", "body": "完整正文"},
        "review": {"status": "pending_review"},
        "budget_snapshot": {"known_total_tokens": 0},
    }
    for filename in ("export.json", "export-copy.json"):
        (archive / filename).write_text(json.dumps(record), encoding="utf-8")
    (archive / "invalid.json").write_text("broken", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "workbench-test-results.json").write_text(json.dumps({
        "verified_at": "2026-11-26T15:55:00+00:00",
        "scope": "历史测试",
        "suites": [{"label": "验收", "passed": 1, "failed": 0, "skipped": 0}],
    }), encoding="utf-8")

    snapshot = client.get("/reports/daily", params={"day": "2026-11-27"}).json()["data"]
    assert snapshot["timezone"] == "Asia/Shanghai"
    assert snapshot["ignored_workflow_exports"] == 1
    assert snapshot["workflow_runs"] == [{
        "task_id": record["task_id"], "created_at": "2026-11-27T00:05:00+08:00",
        "mode": "mock", "status": "pending_review",
        "draft_title": "测试草稿", "review_status": "pending_review",
    }]
    assert snapshot["test_snapshot"] is None
    assert snapshot["cost_accounting_available"] is False
    assert snapshot["approval_available"] is False

    previous = client.get("/reports/daily", params={"day": "2026-11-26"}).json()["data"]
    assert previous["workflow_runs"] == []
    assert previous["test_snapshot"]["suites"][0]["passed"] == 1
    assert client.get("/reports/daily", params={"day": "2026-11-31"}).status_code == 422
