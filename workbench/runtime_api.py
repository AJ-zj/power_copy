"""HTTP adapter for the existing PowerCopy Agent and read-only policy tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from powercopy.core.agent import Agent
from powercopy.core.llm import LLMClient
from powercopy.core.retry import RetryPolicy
from powercopy.core.trace import get_current_trace, record_trace_event
from powercopy.tools.builtin.policy_tools import build_live_registry
from powercopy.tools.executor import ToolExecutor

from workbench.retrieval_api import resolve_workbench_rag_dir


SYSTEM_PROMPT = (
    "你是 PowerCopy V2 的在库政策研究助手。使用已注册的政策工具核对事实，"
    "引用标题、已观察版本和官方链接。明确指出证据、地区和时效限制。"
)
DEFAULT_BUDGET = {
    "max_rounds": 5,
    "max_tool_calls": 8,
    "max_api_attempts": 8,
    "max_known_tokens": 20_000,
}
STOP_MESSAGES = {
    "max_rounds": "执行轮次已达到上限。",
    "tool_budget_exhausted": "下一批工具请求超出工具预算。",
    "api_budget_exhausted": "模型尝试预算已用完。",
    "token_budget_exhausted": "累计已知 Token 已达到后续请求阈值。",
    "invalid_response": "模型未返回 Agent 可处理的响应。",
    "incomplete_response": "模型响应不完整。",
}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    task: str = Field(min_length=1, max_length=4_000)
    mode: Literal["local", "live"] = "local"
    keyword: str = Field(default="", max_length=200)
    region: str = Field(default="", max_length=80)
    use_retrieval: bool = Field(
        default=False,
        description=(
            "是否让本地 Mock LLM 走混合检索工具（search_policy_evidence）。"
            "默认 False，保持原有 search_policy → get_policy_detail 流程不变。"
        ),
    )
    max_rounds: int = Field(default=DEFAULT_BUDGET["max_rounds"], ge=1, le=12)
    max_tool_calls: int = Field(default=DEFAULT_BUDGET["max_tool_calls"], ge=1, le=24)
    max_api_attempts: int = Field(default=DEFAULT_BUDGET["max_api_attempts"], ge=1, le=24)
    max_known_tokens: int = Field(
        default=DEFAULT_BUDGET["max_known_tokens"], ge=1, le=200_000
    )
    confirm_live: bool = False


class LocalPolicyLLM(LLMClient):
    """Scripted completion provider. The Agent and policy tools still run normally."""

    def __init__(self, request: RunRequest):
        self.model = "local-policy-mock"
        self.retry_policy = RetryPolicy(max_attempts=1)
        self.keyword = request.keyword or request.task[:200].strip()
        self.region = request.region
        # 是否走混合检索工具；默认走原有 search_policy 流程
        self.use_retrieval = request.use_retrieval
        self.read_detail = (
            min(request.max_rounds, request.max_api_attempts) >= 3 and request.max_tool_calls >= 2
        )
        # Replace the transport before a live client can be created. The normal LLMClient
        # budget and @trace_model_attempt path still applies to this local provider.
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.create_completion))
        )

    def create_completion(self, *, model, messages, tools):
        observations = [
            json.loads(message["content"]) for message in messages if message["role"] == "tool"
        ]
        if not observations:
            if self.use_retrieval:
                return self.tool_response("search_policy_evidence", {
                    "query": self.keyword, "top_k": 5,
                })
            return self.tool_response("search_policy", {
                "keyword": self.keyword, "region": self.region, "limit": 5,
            })

        if self.use_retrieval:
            return self._respond_retrieval(observations)

        search = observations[0]
        search_data = search.get("data") or {}

        # 04.9：直接读状态字段，不再从 payload 反推
        if search.get("execution_status") != "ok":
            error = search.get("error_message")
            return self.response(f"本地政策工具执行失败。{error or '请核对工具结果。'}")

        if search.get("business_status") == "failed":
            error = search.get("error_message")
            return self.response(f"本地政策查询失败。{error or '请核对工具结果。'}")

        items = search_data.get("items") or []
        if not items:
            return self.response(f"本地政策库未检索到含「{self.keyword}」的记录。")

        first = items[0]
        if self.read_detail and len(observations) == 1:
            return self.tool_response("get_policy_detail", {
                "policy_id": first["policy_id"], "version": first["version"],
            })

        content = (
            f"本地政策查询返回 {len(items)} 条记录。"
            f"首条为《{first['title']}》，已观测版本 v{first['version']}。"
        )
        if len(observations) == 2:
            detail = observations[1]
            if (
                detail.get("execution_status") == "ok"
                and detail.get("business_status") in ("success", "empty")
            ):
                content += "该版本正文片段和官方来源见证据区。"
            else:
                content += "本次未能读取该版本正文，可在工具调用中查看原因。"
        return self.response(content)

    def _respond_retrieval(self, observations: list) -> object:
        """检索工具链路的 Mock 应答。

        只读三条状态：execution_status / business_status / business_code。
        不把候选当事实，输出里明确提示需要核对原文。
        """

        result = observations[0]

        if result.get("execution_status") != "ok":
            return self.response(
                f"本地检索工具执行失败。{result.get('error_message') or '请核对工具结果。'}"
            )

        status = result.get("business_status")
        data = result.get("data") or {}

        if status == "failed":
            return self.response(
                f"本地检索不可用。{result.get('error_message') or '请核对工具结果。'}"
            )

        candidates = data.get("evidence_candidates") or []
        mode = data.get("retrieval_mode") or "unknown"
        degraded = data.get("degraded_status")
        freshness = data.get("index_freshness")

        if not candidates:
            return self.response(
                f"本地混合检索（{mode}）未返回候选片段。"
                f"这不等于政策不存在，可换关键词或改用 search_policy 粗查。"
            )

        first = candidates[0]
        content = (
            f"本地混合检索（{mode}）返回 {len(candidates)} 个候选片段。"
            f"首条来自 policy_id={first['policy_id']} 的 v{first['version']}，"
            f"字符区间 [{first['start']}, {first['end']})。"
            f"这些是检索候选而非已核实事实，需核对原文后引用。"
        )

        if degraded:
            content += f"本次为降级模式（{degraded}），不是完整混合检索。"
        if freshness and freshness != "ok":
            content += f"索引状态为 {freshness}，结果可能不是最新政策。"

        return self.response(content)

    def tool_response(self, name: str, arguments: dict):
        call = SimpleNamespace(
            id=f"local-{name}",
            type="function",
            function=SimpleNamespace(
                name=name, arguments=json.dumps(arguments, ensure_ascii=False)
            ),
        )
        return self.response(None, call)

    def response(self, content: str | None, call=None):
        message = SimpleNamespace(content=content, tool_calls=[call] if call else [])
        reason = "tool_calls" if call else "stop"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=reason)],
            usage=None,
            model=self.model,
        )


class ObservedLLM:
    def __init__(self, client: LLMClient):
        self.client = client
        self.trace = None
        self.responses = []

    def invoke(self, *args, **kwargs):
        self.trace = get_current_trace()
        initial_event_count = len(self.trace.events) if self.trace else 0
        try:
            response = self.client.invoke(*args, **kwargs)
        finally:
            if self.trace is not None:
                attempt = 0
                for event in self.trace.events[initial_event_count:]:
                    if event["event_type"] == "model_attempt":
                        attempt += 1
                        event.update(attempt=attempt, is_retry=attempt > 1)
        usage = {key: response[key] for key in ("input_tokens", "output_tokens", "total_tokens")}
        self.responses.append(usage)
        record_trace_event(
            "model_response", model=response["model"], retry_count=response["retry_count"], **usage
        )
        return response

    def usage(self, budget: dict) -> dict:
        usage = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            values = [response[key] for response in self.responses]
            complete = values and all(type(value) is int and value >= 0 for value in values)
            usage[key] = sum(values) if complete and not budget.get("usage_incomplete") else None
        return {
            **usage,
            "known_total_tokens": budget.get("known_total_tokens", 0),
            "usage_incomplete": budget.get("usage_incomplete", True),
        }


class ObservedToolExecutor(ToolExecutor):
    def __init__(self, registry):
        super().__init__(registry)
        self.calls = []

    def execute(self, name: str, arguments: str | dict):
        result = super().execute(name, arguments)
        try:
            display_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            # Core accepts Python's permissive JSON parser. Preserve the original model
            # text if it cannot be represented in the workbench's JSON response.
            json.dumps(display_arguments, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            display_arguments = arguments
        # 04.9：状态直接来自 ToolResult，不再从 payload 反推。
        # executor 已经区分了执行层与业务层，这里只做展示映射。
        self.calls.append({
            "name": name,
            "arguments": display_arguments,
            "execution_status": str(result.execution_status),
            "business_status": str(result.business_status),
            "business_code": result.business_code,
            "retryable": result.retryable,
            "result": result.data,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "latency_ms": result.latency_ms,
        })
        return result

    def evidence(self) -> list[dict]:
        items = {}
        for call in self.calls:
            # 04.9：只有业务成功才有证据；空结果与失败都不产生证据
            if call["business_status"] != "success":
                continue
            payload = call["result"]
            if not isinstance(payload, dict):
                continue
            rows = payload.get("items") if call["name"] == "search_policy" else [payload]
            for row in rows or []:
                if not isinstance(row, dict) or "policy_id" not in row:
                    continue
                key = (row["policy_id"], row["version"])
                fields = ("policy_id", "version", "title", "source_key", "fetch_id", "content_sha256")
                evidence = {field: row[field] for field in fields}
                evidence["source_url"] = safe_source_url(row.get("source_url"))
                if "body_text" in row:
                    evidence.update(kind="detail", snippet=row["body_text"][:400])
                else:
                    if key in items:
                        continue
                    evidence.update(kind="search", snippet=None)
                items[key] = evidence
        return list(items.values())


def safe_source_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return url
    except ValueError:
        pass
    return None


def database_path(project_root: Path) -> Path:
    return project_root / "storage" / "policies_versioned.db"


def live_configured(project_root: Path) -> bool:
    if os.environ.get("POWERCOPY_WORKBENCH_ALLOW_LIVE") != "1":
        return False
    file_settings = dotenv_values(project_root / ".env")
    required = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
    return all(os.environ.get(name, file_settings.get(name)) for name in required)


def execute_run(request: RunRequest, project_root: Path) -> dict:
    limits = request.model_dump(include=set(DEFAULT_BUDGET))
    if request.mode == "live":
        if not request.confirm_live or not live_configured(project_root):
            raise HTTPException(403, detail={
                "code": "LIVE_MODEL_DISABLED",
                "message": "真实模型需要服务端开启，并在本次任务中确认授权。",
            })
        try:
            client = LLMClient()
        except Exception:
            raise HTTPException(503, detail={
                "code": "LIVE_MODEL_UNAVAILABLE", "message": "真实模型配置不可用。",
            }) from None
    else:
        client = LocalPolicyLLM(request)

    registry = build_live_registry(database_path(project_root), rag_dir=resolve_workbench_rag_dir(project_root))
    observed_llm = ObservedLLM(client)
    executor = ObservedToolExecutor(registry)
    agent = Agent(observed_llm, registry, SYSTEM_PROMPT, **limits)
    agent.executor = executor
    failure = None

    try:
        result = agent.run(request.task)
    except Exception:
        # The core Trace supplies the task ID on failures even when Agent.run raises.
        if observed_llm.trace is None:
            raise HTTPException(500, detail={
                "code": "AGENT_RUN_FAILED", "message": "Agent 未能启动当前任务。",
            }) from None
        trace = observed_llm.trace.snapshot()
        result = {
            "task_id": trace["task_id"],
            "status": "failed",
            "content": None,
            "rounds": sum(event["event_type"] == "round_started" for event in trace["events"]),
            "tool_requests": len(executor.calls),
            "budget": trace["budget"],
            "trace": trace,
        }
        failure = {"code": "AGENT_RUN_FAILED", "message": "Agent 执行异常，可查看当前任务轨迹。"}
    else:
        status = result["status"]
        if status != "completed":
            failure = {"code": status, "message": STOP_MESSAGES.get(status, "Agent 提前结束。")}
    finally:
        if request.mode == "live":
            client.client.close()

    model_calls = result["budget"].get("api_attempts", 0)
    return {
        **result,
        "mode": request.mode,
        "limits": limits,
        "model_calls": model_calls,
        "external_model_calls": model_calls if request.mode == "live" else 0,
        "usage": observed_llm.usage(result["budget"]),
        "tool_calls": executor.calls,
        "evidence": executor.evidence(),
        "failure": failure,
    }


def build_runtime_router(project_root: Path) -> APIRouter:
    router = APIRouter(tags=["Agent Runtime"])

    @router.get("/runtime")
    def runtime_info():
        return {"data": {
            "local_available": True,
            "live_enabled": live_configured(project_root),
            "streaming": False,
            "history": False,
            "budget": DEFAULT_BUDGET,
        }}

    @router.get("/tools")
    def tools():
        registry = build_live_registry(database_path(project_root), rag_dir=resolve_workbench_rag_dir(project_root))
        limits = {
            "search_policy": ["本地 SQLite 只读", "字面关键词匹配", "每次最多 10 条"],
            "get_policy_detail": ["本地 SQLite 只读", "每段最多 8,000 字符", "支持指定已观测版本"],
            "search_policy_evidence": [
                "本地混合检索（词法 + 向量 + RRF）",
                "返回检索候选，非已核实事实",
                "query 最长 200 字符，top_k 最多 20",
                "单条候选正文最多 800 字符",
                "索引过期时会标注，不代表最新政策",
            ],
        }
        items = []
        for name in registry.list_tools():
            tool = registry.get(name)
            items.append({
                "name": name,
                "description": tool.description,
                "parameters": tool.args_schema.model_json_schema(),
                "limitations": limits.get(name, []),
            })
        return {"data": {"items": items}}

    @router.post("/runs")
    def run(request: RunRequest):
        return {"data": execute_run(request, project_root)}

    return router
