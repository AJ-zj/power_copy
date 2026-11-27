from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
import pytest

from workbench import content_api
from workbench.content_api import ContentRunRequest, build_content_router


class Mode(StrEnum):
    MOCK = "mock"
    LIVE = "live"


class Platform(StrEnum):
    GENERAL = "general"
    WECHAT_MP = "wechat_mp"


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str
    region: str
    mode: Mode
    platform: Platform
    enable_review: bool
    enable_revision: bool
    max_revisions: int


def fake_workflow():
    return SimpleNamespace(
        ContentWorkflowRequest=WorkflowRequest,
        ContentWorkflowService=object,
        GenerationMode=Mode,
        Platform=Platform,
    )


class WorkflowResult:
    task_id = "task-1234567890abcdef"
    research = SimpleNamespace(statistics=lambda: {"claims_text_verified": 1})
    draft = SimpleNamespace(statistics=lambda: {"human_confirmation_available": False})
    review = SimpleNamespace(statistics=lambda: {
        "fact_support_llm_used": True,
        "human_confirmation_available": False,
        "requires_human": True,
    })

    def requires_human(self):
        return True

    def model_dump(self, mode="python"):
        assert mode == "json"
        return {
            "task_id": self.task_id,
            "topic": "电力市场",
            "mode": "mock",
            "final_status": "pending_review",
            "status_reason": "待人工审核",
            "research": {"research_id": "res-12345678", "claims": []},
            "draft": {"draft_id": "draft-12345678", "body": "草稿正文"},
            "review": {
                "fact_support_llm_used": True,
                "human_confirmation_available": False,
                "requires_human": True,
            },
            "revision": None,
            "budget_snapshot": {"used_api_attempts": 3, "usage_incomplete": False},
            "stage_timings_ms": {"research_ms": 12.5, "draft_ms": 2.1, "review_ms": 1.4},
            "schema_version": "content-workflow-v1",
        }


@pytest.fixture
def project(tmp_path):
    (tmp_path / "storage" / "rag").mkdir(parents=True)
    (tmp_path / "storage" / "policies_versioned.db").touch()
    for name in ("chunks.json", "lexical.db", "dense.npz"):
        (tmp_path / "storage" / "rag" / name).touch()
    return tmp_path


@pytest.fixture
def install_client(project, monkeypatch):
    monkeypatch.setattr(content_api, "_execute_workflow", lambda *_: WorkflowResult())
    app = FastAPI()
    app.include_router(build_content_router(project))
    return TestClient(app)


def test_missing_workflow_is_explicit_and_never_returns_fixture(project, monkeypatch):
    monkeypatch.setattr(content_api, "_load_workflow", lambda: None)
    app = FastAPI()
    app.include_router(build_content_router(project))
    client = TestClient(app)

    status = client.get("/content/status").json()["data"]
    assert status["workflow_available"] is False
    assert status["mock_available"] is False
    response = client.post("/content/runs", json={"topic": "电力市场"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CONTENT_WORKFLOW_UNAVAILABLE"


def test_status_separates_data_readiness_from_workflow_readiness(project, monkeypatch):
    monkeypatch.setattr(content_api, "_load_workflow", fake_workflow)
    (project / "storage" / "rag" / "dense.npz").unlink()
    app = FastAPI()
    app.include_router(build_content_router(project))

    status = TestClient(app).get("/content/status").json()["data"]
    assert status["workflow_available"] is True
    assert status["data_available"] is False
    assert status["mock_available"] is False


def test_default_mock_run_uses_shared_workflow_defaults_and_human_review(install_client, monkeypatch):
    stub = fake_workflow()
    monkeypatch.setattr(content_api, "_load_workflow", lambda: stub)
    seen = []

    def run(_project: Path, request: ContentRunRequest, workflow):
        assert workflow is stub
        seen.append(workflow.ContentWorkflowRequest(**request.service_payload()))
        return WorkflowResult()

    monkeypatch.setattr(content_api, "_execute_workflow", run)
    response = install_client.post("/content/runs", json={"topic": "电力市场"})
    assert response.status_code == 200
    request, = seen
    assert request.mode is Mode.MOCK
    assert request.platform is Platform.WECHAT_MP
    assert request.enable_review is True
    assert request.enable_revision is True
    assert request.max_revisions == 1
    data = response.json()["data"]
    assert data["task_id"] == WorkflowResult.task_id
    assert data["status"] == "pending_review"
    assert data["evidence_package"]["research_id"] == "res-12345678"
    assert data["content_draft"]["body"] == "草稿正文"
    assert data["review_statistics"]["fact_support_llm_used"] is True
    assert data["review_statistics"]["human_confirmation_available"] is False
    assert data["requires_human"] is True
    assert data["budget"]["used_api_attempts"] == 3
    assert data["stage_timings_ms"]["research_ms"] == 12.5


def test_live_requires_both_server_gate_and_per_request_confirmation(install_client, monkeypatch):
    monkeypatch.setattr(content_api, "_load_workflow", fake_workflow)
    monkeypatch.setattr(content_api, "live_configured", lambda _project: False)
    response = install_client.post("/content/runs", json={"topic": "电力市场", "mode": "live", "confirm_live": True})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LIVE_MODEL_DISABLED"

    monkeypatch.setattr(content_api, "live_configured", lambda _project: True)
    response = install_client.post("/content/runs", json={"topic": "电力市场", "mode": "live"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "LIVE_MODEL_DISABLED"


@pytest.mark.parametrize("field", ["db_path", "url", "llm_api_key", "model_path"])
def test_rejects_untrusted_input_fields(install_client, field):
    response = install_client.post("/content/runs", json={"topic": "电力市场", field: "blocked"})
    assert response.status_code == 422


def test_invalid_revision_configuration_is_rejected(install_client):
    response = install_client.post("/content/runs", json={
        "topic": "电力市场", "enable_review": False, "enable_revision": True,
    })
    assert response.status_code == 422


def test_workflow_failure_returns_fixed_non_sensitive_message(install_client, monkeypatch):
    monkeypatch.setattr(content_api, "_load_workflow", fake_workflow)

    def fail(*_args):
        raise RuntimeError(r"C:\sensitive\model-cache")

    monkeypatch.setattr(content_api, "_execute_workflow", fail)
    response = install_client.post("/content/runs", json={"topic": "电力市场"})
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "CONTENT_RUN_FAILED"
    assert r"C:\sensitive\model-cache" not in response.text
