"""Mount the workbench on the existing PowerCopy FastAPI application."""

import json
import logging
import os
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

WORKSPACE = Path(__file__).resolve().parents[1]
PROJECT = Path(os.environ.get("POWERCOPY_PROJECT_ROOT") or WORKSPACE).resolve()
if not (PROJECT / "powercopy" / "core" / "trace.py").is_file():
    raise RuntimeError("请将 POWERCOPY_PROJECT_ROOT 指向含 Agent Trace 和政策数据中心的 PowerCopy V2 项目")
sys.path.insert(0, str(PROJECT))
if os.environ.get("POWERCOPY_WORKBENCH_ALLOW_LIVE") == "1":
    load_dotenv(PROJECT / ".env", override=False)

from powercopy.api.main import app
from workbench.content_api import build_content_router
from workbench.data_api import build_data_router, overview_data
from workbench.retrieval_api import build_retrieval_router
from workbench.runtime_api import build_runtime_router

PREFIX = "/api/workbench"
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])


def failure(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.middleware("http")
async def workbench_requests(request: Request, call_next):
    if not request.url.path.startswith(PREFIX + "/"):
        return await call_next(request)
    if request.method == "POST":
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            return failure(415, "JSON_REQUIRED", "请求需要使用 JSON 格式。")
    try:
        response = await call_next(request)
    except Exception:
        logging.getLogger(__name__).exception("Workbench request failed")
        return failure(500, "INTERNAL_ERROR", "工作台请求失败，请检查本地服务日志。")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError):
    return failure(422, "INVALID_INPUT", "请求参数无效，请检查类型、必填项和取值范围。")


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    messages = {404: "未找到请求的资源。", 405: "此资源不支持当前请求方法。"}
    if isinstance(exc.detail, dict) and {"code", "message"} <= exc.detail.keys():
        return failure(exc.status_code, exc.detail["code"], exc.detail["message"])
    return failure(exc.status_code, f"HTTP_{exc.status_code}", messages.get(exc.status_code, "请求未能完成。"))


app.include_router(build_data_router(PROJECT), prefix=PREFIX)
app.include_router(build_runtime_router(PROJECT), prefix=PREFIX)
app.include_router(build_retrieval_router(PROJECT), prefix=PREFIX)
app.include_router(build_content_router(PROJECT), prefix=PREFIX)


@app.get(PREFIX + "/overview")
def overview():
    with (PROJECT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    acceptance_file = WORKSPACE / "docs" / "workbench-test-results.json"
    report = json.loads(acceptance_file.read_text(encoding="utf-8")) if acceptance_file.is_file() else {}
    return {"data": {"version": version, "acceptance": report.get("suites", []), **overview_data(PROJECT)}}
