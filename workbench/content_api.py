"""Conditional workbench adapter for the shared content workflow."""

from __future__ import annotations

from importlib import import_module
import logging
from pathlib import Path
from types import ModuleType
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from powercopy.copywriting.contracts import GenerationMode, Platform
from powercopy.core.llm import LLMClient
from powercopy.research.verification import PolicyCorpus
from powercopy.tools.builtin.policy_tools import build_live_registry
from workbench.retrieval_api import resolve_workbench_rag_dir
from workbench.runtime_api import database_path, live_configured


logger = logging.getLogger(__name__)
WORKFLOW_MODULE = "powercopy.workflows.content"
UNAVAILABLE = {"code": "CONTENT_WORKFLOW_UNAVAILABLE", "message": "统一内容工作流尚未接入当前后端。"}
DATA_UNAVAILABLE = {"code": "CONTENT_DATA_UNAVAILABLE", "message": "政策库或研究索引未就绪，无法启动创作。"}
LIVE_DISABLED = {"code": "LIVE_MODEL_DISABLED", "message": "真实模型需要服务端授权并确认本次调用。"}
RUN_FAILED = {"code": "CONTENT_RUN_FAILED", "message": "内容工作流执行失败，请查看本地服务日志。"}


class ContentRunRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    topic: str = Field(min_length=1, max_length=200)
    target_audience: str = Field(default="", max_length=100)
    region: str = Field(default="", max_length=40)
    platform: Literal["wechat_mp", "website", "linkedin", "general"] = "wechat_mp"
    target_chars: int = Field(default=800, ge=100, le=5_000)
    style: str = Field(default="专业、通俗、可信", max_length=200)
    purpose: str = Field(default="", max_length=500)
    mode: Literal["mock", "live"] = "mock"
    confirm_live: bool = False
    enable_review: bool = True
    enable_revision: bool = True
    max_revisions: int = Field(default=1, ge=0, le=3)
    max_api_attempts: int = Field(default=12, ge=1, le=20)
    max_known_tokens: int = Field(default=40_000, ge=1_000, le=100_000)
    max_questions: int = Field(default=5, ge=1, le=12)
    max_tool_calls: int = Field(default=8, ge=1, le=30)

    @model_validator(mode="after")
    def validate_revision(self):
        if self.enable_revision and not self.enable_review:
            raise ValueError("自动修订需要启用审核")
        if self.max_revisions > self.max_api_attempts:
            raise ValueError("自动修订次数不能超过 API 尝试次数")
        return self

    def service_payload(self) -> dict:
        values = self.model_dump(exclude={"confirm_live"})
        values["mode"] = GenerationMode(self.mode)
        values["platform"] = Platform(self.platform)
        return values


def _load_workflow() -> ModuleType | None:
    try:
        module = import_module(WORKFLOW_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name not in {"powercopy.workflows", WORKFLOW_MODULE}:
            raise
        return None
    required = ("ContentWorkflowRequest", "ContentWorkflowService")
    return module if all(callable(getattr(module, name, None)) for name in required) else None


def _data_ready(project_root: Path) -> bool:
    rag = resolve_workbench_rag_dir(project_root)
    manifest_ready = (rag / "chunks.json").is_file()
    search_index_ready = (rag / "lexical.db").is_file() and (rag / "dense.npz").is_file()
    return database_path(project_root).is_file() and manifest_ready and search_index_ready


def _execute_workflow(project_root: Path, request: ContentRunRequest, workflow: ModuleType):
    workflow_request = workflow.ContentWorkflowRequest(**request.service_payload())
    db_path = database_path(project_root)
    rag_dir = resolve_workbench_rag_dir(project_root)
    registry = build_live_registry(db_path, rag_dir=rag_dir)
    corpus = PolicyCorpus(db_path, rag_dir / "chunks.json")
    if request.mode == "mock":
        return workflow.ContentWorkflowService(corpus, registry).run(workflow_request)

    llm = LLMClient()
    try:
        service = workflow.ContentWorkflowService(
            corpus,
            registry,
            research_llm=llm,
            copywriting_llm=llm,
            reviewer_llm=llm,
            reviser_llm=llm,
            model_name=llm.model,
        )
        return service.run(workflow_request)
    finally:
        llm.client.close()


def _serialize_result(result) -> dict:
    research = result.research
    draft = result.draft
    review = result.review
    payload = result.model_dump(mode="json")
    if payload["mode"] == "live" and payload["research"]:
        research_data = payload["research"]
        # The research service may include provider exception details in diagnostics.
        if research_data["research_status"] == "failed":
            research_data["status_reason"] = "研究未能完成。"
        research_data["uncertainties"] = [
            "部分研究步骤未完成，请在服务端检查运行情况。"
        ] if research_data["uncertainties"] else []
        research_data["unanswered_questions"] = [
            question["text"] for question in research_data["research_questions"] if not question["answered"]
        ]
    if payload["mode"] == "live" and payload["draft"] and payload["draft"]["status"] == "failed":
        draft_data = payload["draft"]
        draft_data["status_reason"] = "文案生成未能完成。"
        draft_data["body"] = draft_data["status_reason"]
        draft_data["review_notes"] = []
    return {
        "task_id": result.task_id,
        "status": payload["final_status"],
        "status_reason": (
            "内容工作流未能完成。" if payload["mode"] == "live" and payload["final_status"] == "failed"
            else payload["status_reason"]
        ),
        "generation_mode": payload["mode"],
        "evidence_package": payload["research"],
        "content_draft": payload["draft"],
        "research_statistics": research.statistics() if research is not None else None,
        "draft_statistics": draft.statistics() if draft is not None else None,
        "review_report": payload["review"],
        "review_statistics": review.statistics() if review is not None else None,
        "revision": payload["revision"],
        "revision_records": [payload["revision"]] if payload["revision"] else [],
        "budget": payload["budget_snapshot"],
        "stage_timings_ms": payload["stage_timings_ms"],
        "requires_human": result.requires_human(),
        "schema_version": payload["schema_version"],
    }


def build_content_router(project_root: Path) -> APIRouter:
    router = APIRouter(prefix="/content", tags=["Content Studio"])
    project = project_root.resolve()

    @router.get("/status")
    def status():
        workflow_available = _load_workflow() is not None
        data_available = _data_ready(project)
        available = workflow_available and data_available
        return {"data": {
            "workflow_available": workflow_available,
            "data_available": data_available,
            "mock_available": available,
            "live_available": available and live_configured(project),
            "review_available": available,
            "maximum_automatic_revisions": 1,
            "human_confirmation_available": False,
            "release_available": False,
        }}

    @router.post("/runs")
    def run(request: ContentRunRequest):
        if request.mode == "live" and (not request.confirm_live or not live_configured(project)):
            raise HTTPException(403, detail=LIVE_DISABLED)

        workflow = _load_workflow()
        if workflow is None:
            raise HTTPException(503, detail=UNAVAILABLE)
        if not _data_ready(project):
            raise HTTPException(503, detail=DATA_UNAVAILABLE)

        try:
            result = _execute_workflow(project, request, workflow)
            payload = _serialize_result(result)
        except Exception as error:
            logger.warning("Content workflow execution failed (%s)", type(error).__name__)
            raise HTTPException(500, detail=RUN_FAILED) from None
        return {"data": payload}

    return router


__all__ = ["ContentRunRequest", "build_content_router"]
