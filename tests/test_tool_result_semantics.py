"""04.9 Tool Result Semantics 专项测试。

验证三层状态分离：
- Execution Status：由 ToolExecutor 独占写入
- Business Status：由 handler 声明
- Task Status：由 Agent 决定，不由单次工具结果直接决定

核心语义：
- 搜索无命中 = empty（正常结果），不是 failed
- 业务失败 = execution_ok + business_failed，错误码保留
- 执行崩溃 = execution_error，业务层 not_applicable
"""

import json

import pytest
from pydantic import BaseModel, Field

from powercopy.core.agent import Agent
from powercopy.core.llm import LLMClient
from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy_tools import (
    SAFE_MESSAGES,
    build_live_registry,
    wrap_handler,
)
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.registry import ToolRegistry
from powercopy.tools.result import (
    BusinessStatus,
    ExecutionStatus,
    ToolBusinessError,
    ToolOutcome,
    ToolResult,
)
from powercopy.data.query import PolicyQueryError, PolicyQueryService

import json as _json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "policies_versioned.db"
NDRC_POLICY_ID = "70c4a432f8ca4201b8514bc5f33a2ab6"


# ==========================================================================
# 1. 状态枚举与数据契约
# ==========================================================================
class TestStatusEnums:
    def test_execution_status_values(self):
        assert {str(s) for s in ExecutionStatus} == {
            "ok",
            "tool_not_found",
            "invalid_json",
            "invalid_arguments",
            "execution_error",
        }

    def test_business_status_values(self):
        assert {str(s) for s in BusinessStatus} == {
            "success",
            "empty",
            "failed",
            "not_applicable",
        }

    def test_empty_is_not_error(self):
        """empty 是正常业务结果，business_ok 应为真。"""
        result = ToolResult(
            tool_name="t",
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.EMPTY,
            data={"items": []},
        )
        assert result.business_ok is True
        assert result.has_business_failure is False
        assert result.success is True

    def test_failed_is_error(self):
        result = ToolResult(
            tool_name="t",
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.FAILED,
            business_code="POLICY_VERSION_NOT_FOUND",
        )
        assert result.business_ok is False
        assert result.has_business_failure is True
        # 执行层没失败
        assert result.success is True

    def test_execution_error_is_not_applicable(self):
        result = ToolResult(
            tool_name="t",
            execution_status=ExecutionStatus.EXECUTION_ERROR,
            business_status=BusinessStatus.NOT_APPLICABLE,
        )
        assert result.success is False
        assert result.error_type == "EXECUTION_ERROR"
        assert result.business_ok is False

    def test_to_dict_has_both_layers(self):
        result = ToolResult(
            tool_name="t",
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.FAILED,
            business_code="X",
            data={"a": 1},
        )
        payload = result.to_dict()
        assert payload["execution_status"] == "ok"
        assert payload["business_status"] == "failed"
        assert payload["business_code"] == "X"
        assert payload["success"] is True  # 兼容属性

    def test_to_dict_json_serializable(self):
        result = ToolResult(
            tool_name="t",
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.SUCCESS,
            data={"中文": "值"},
        )
        text = _json.dumps(result.to_dict(), ensure_ascii=False)
        assert "中文" in text


# ==========================================================================
# 2. ToolExecutor 状态映射
# ==========================================================================
class TestExecutorMapping:
    @pytest.fixture
    def registry(self):
        def ok(**kwargs):
            return {"value": 1}

        def empty(**kwargs):
            return ToolOutcome(
                data={"items": []},
                business_status=BusinessStatus.EMPTY,
                business_code="NO_MATCH",
            )

        def fails(**kwargs):
            raise ToolBusinessError("BIZ_CODE", "业务失败说明")

        def crashes(**kwargs):
            raise RuntimeError("内部崩溃")

        class Args(BaseModel):
            text: str = Field(default="x")

        reg = ToolRegistry()
        reg.register(Tool(name="ok", description="d", args_schema=Args, handler=ok))
        reg.register(Tool(name="empty", description="d", args_schema=Args, handler=empty))
        reg.register(Tool(name="fails", description="d", args_schema=Args, handler=fails))
        reg.register(Tool(name="crashes", description="d", args_schema=Args, handler=crashes))
        return reg

    def test_success(self, registry):
        r = ToolExecutor(registry).execute("ok", "{}")
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.SUCCESS
        assert r.data == {"value": 1}

    def test_empty_reported_as_empty(self, registry):
        r = ToolExecutor(registry).execute("empty", "{}")
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.EMPTY
        assert r.business_code == "NO_MATCH"
        assert r.business_ok is True

    def test_business_error_keeps_code(self, registry):
        r = ToolExecutor(registry).execute("fails", "{}")
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.FAILED
        assert r.business_code == "BIZ_CODE"
        assert r.error_message == "业务失败说明"
        assert r.success is True  # 执行层未失败

    def test_crash_is_execution_error(self, registry):
        r = ToolExecutor(registry).execute("crashes", "{}")
        assert r.execution_status is ExecutionStatus.EXECUTION_ERROR
        assert r.business_status is BusinessStatus.NOT_APPLICABLE
        assert r.error_type == "EXECUTION_ERROR"
        # 崩溃信息不透传给模型
        assert "内部崩溃" not in (r.error_message or "")

    def test_tool_not_found(self, registry):
        r = ToolExecutor(registry).execute("ghost", "{}")
        assert r.execution_status is ExecutionStatus.TOOL_NOT_FOUND
        assert r.business_status is BusinessStatus.NOT_APPLICABLE
        assert r.error_type == "TOOL_NOT_FOUND"

    def test_invalid_json(self, registry):
        r = ToolExecutor(registry).execute("ok", "not json")
        assert r.execution_status is ExecutionStatus.INVALID_JSON
        assert r.business_status is BusinessStatus.NOT_APPLICABLE

    def test_invalid_arguments(self, registry):
        # 用严格 Schema 的工具测试：extra=forbid 才拒绝未知字段
        class Strict(BaseModel):
            model_config = {"extra": "forbid"}
            required_text: str

        def handler(**kwargs):
            return {}

        reg = ToolRegistry()
        reg.register(
            Tool(name="strict", description="d", args_schema=Strict, handler=handler)
        )
        r = ToolExecutor(reg).execute("strict", '{"unknown": 1}')
        assert r.execution_status is ExecutionStatus.INVALID_ARGUMENTS
        assert r.business_status is BusinessStatus.NOT_APPLICABLE

    def test_business_error_is_not_folded_into_execution(self, registry):
        """关键：业务失败不能被归为执行失败。"""
        r = ToolExecutor(registry).execute("fails", "{}")
        assert r.execution_status != ExecutionStatus.EXECUTION_ERROR
        assert r.error_type != "EXECUTION_ERROR"


# ==========================================================================
# 3. ToolBusinessError 契约
# ==========================================================================
class TestBusinessErrorContract:
    def test_requires_code(self):
        with pytest.raises(ValueError):
            ToolBusinessError("")

    def test_blank_code_rejected(self):
        with pytest.raises(ValueError):
            ToolBusinessError("   ")

    def test_retryable_flag(self):
        error = ToolBusinessError("X", "msg", retryable=True)
        assert error.retryable is True
        assert error.code == "X"

    def test_message_defaults_to_code(self):
        error = ToolBusinessError("ONLY_CODE")
        assert str(error) == "ONLY_CODE"


# ==========================================================================
# 4. 真实政策工具的状态语义
# ==========================================================================
@pytest.mark.skipif(not DB.is_file(), reason="缺少真实政策库")
class TestPolicyToolSemantics:
    @pytest.fixture
    def executor(self):
        return ToolExecutor(build_live_registry(DB))

    def test_normal_search_is_success(self, executor):
        r = executor.execute("search_policy", '{"region": "", "keyword": "电力"}')
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.SUCCESS

    def test_no_match_is_empty_not_failed(self, executor):
        """核心用例：搜索无命中必须标记 empty，不是 failed。"""
        r = executor.execute(
            "search_policy",
            json.dumps({"region": "", "keyword": "绝不存在的关键词内容"}, ensure_ascii=False),
        )
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.EMPTY
        assert r.business_code == "NO_MATCH"
        assert r.business_ok is True, "空结果应视为业务正常"
        assert r.has_business_failure is False
        assert r.data["items"] == []

    def test_unknown_policy_is_business_failed(self, executor):
        r = executor.execute(
            "get_policy_detail", '{"policy_id": "00000000000000000000000000000000"}'
        )
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.FAILED
        assert r.business_code == "POLICY_VERSION_NOT_FOUND"

    def test_unknown_version_is_business_failed(self, executor):
        r = executor.execute(
            "get_policy_detail",
            json.dumps({"policy_id": NDRC_POLICY_ID, "version": 99}),
        )
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.FAILED
        assert r.business_code == "POLICY_VERSION_NOT_FOUND"

    def test_offset_out_of_range(self, executor):
        r = executor.execute(
            "get_policy_detail",
            json.dumps({"policy_id": NDRC_POLICY_ID, "offset": 999999}),
        )
        assert r.business_status is BusinessStatus.FAILED
        assert r.business_code == "BODY_OFFSET_OUT_OF_RANGE"

    def test_detail_success(self, executor):
        r = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID})
        )
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.SUCCESS
        assert r.data["version"] == 1

    def test_missing_db_is_business_failed(self, tmp_path):
        from powercopy.tools.builtin.policy_tools import register_policy_tools

        reg = ToolRegistry()
        register_policy_tools(reg, PolicyQueryService(tmp_path / "absent.db"))
        r = ToolExecutor(reg).execute("search_policy", '{"region": "", "keyword": "x"}')
        assert r.execution_status is ExecutionStatus.OK
        assert r.business_status is BusinessStatus.FAILED
        assert r.business_code == "POLICY_DB_NOT_FOUND"
        assert not (tmp_path / "absent.db").exists()

    def test_business_error_message_is_safe(self, executor):
        r = executor.execute(
            "get_policy_detail", '{"policy_id": "00000000000000000000000000000000"}'
        )
        assert r.error_message == SAFE_MESSAGES["POLICY_VERSION_NOT_FOUND"]
        text = json.dumps(r.to_dict(), ensure_ascii=False)
        for leak in ("sqlite", "SELECT", "Traceback", "/storage/"):
            assert leak not in text


# ==========================================================================
# 5. wrap_handler 行为
# ==========================================================================
class TestWrapHandler:
    def test_policy_error_becomes_business_error(self):
        def boom(**kwargs):
            raise PolicyQueryError("POLICY_DB_READ_ERROR")

        with pytest.raises(ToolBusinessError) as excinfo:
            wrap_handler(boom)(**{})

        assert excinfo.value.code == "POLICY_DB_READ_ERROR"
        assert excinfo.value.message == SAFE_MESSAGES["POLICY_DB_READ_ERROR"]

    def test_unknown_code_uses_generic_message(self):
        def boom(**kwargs):
            raise PolicyQueryError("WEIRD_INTERNAL_CODE")

        with pytest.raises(ToolBusinessError) as excinfo:
            wrap_handler(boom)(**{})
        assert excinfo.value.code == "WEIRD_INTERNAL_CODE"
        assert "WEIRD_INTERNAL" not in excinfo.value.message

    def test_empty_search_wrapped_as_empty_outcome(self):
        def search(**kwargs):
            return {"items": [], "returned": 0, "search_mode": "literal_substring"}

        outcome = wrap_handler(search)(**{})
        assert isinstance(outcome, ToolOutcome)
        assert outcome.business_status is BusinessStatus.EMPTY

    def test_non_empty_search_passes_through(self):
        def search(**kwargs):
            return {"items": [{"a": 1}], "search_mode": "literal_substring"}

        result = wrap_handler(search)(**{})
        assert not isinstance(result, ToolOutcome)
        assert result["items"]

    def test_plain_dict_without_search_mode_passes_through(self):
        """detail 之类不含 search_mode 的返回不受空结果逻辑影响。"""

        def detail(**kwargs):
            return {"policy_id": "x", "body_text": ""}

        result = wrap_handler(detail)(**{})
        assert not isinstance(result, ToolOutcome)


# ==========================================================================
# 6. Trace 记录
# ==========================================================================
class TestTraceRecording:
    def _run_with_trace(self, registry, name, arguments):
        from powercopy.core.trace import ExecutionTrace, _current_trace

        trace = ExecutionTrace()
        token = _current_trace.set(trace)
        try:
            ToolExecutor(registry).execute(name, arguments)
        finally:
            _current_trace.reset(token)
        return [e for e in trace.events if e["event_type"] == "tool_execution"]

    def test_success_event_has_both_statuses(self):
        class Args(BaseModel):
            pass

        def ok(**kwargs):
            return {"v": 1}

        reg = ToolRegistry()
        reg.register(Tool(name="ok", description="d", args_schema=Args, handler=ok))
        events = self._run_with_trace(reg, "ok", "{}")

        assert len(events) == 1
        event = events[0]
        assert event["status"] == "success"
        assert event["execution_status"] == "ok"
        assert event["business_status"] == "success"

    def test_business_failure_event(self):
        class Args(BaseModel):
            pass

        def fails(**kwargs):
            raise ToolBusinessError("BIZ", "msg")

        reg = ToolRegistry()
        reg.register(Tool(name="f", description="d", args_schema=Args, handler=fails))
        event = self._run_with_trace(reg, "f", "{}")[0]

        # 执行层仍然成功
        assert event["status"] == "success"
        assert event["execution_status"] == "ok"
        assert event["business_status"] == "failed"
        assert event["business_code"] == "BIZ"

    def test_execution_error_event(self):
        class Args(BaseModel):
            pass

        def crash(**kwargs):
            raise RuntimeError("boom")

        reg = ToolRegistry()
        reg.register(Tool(name="c", description="d", args_schema=Args, handler=crash))
        event = self._run_with_trace(reg, "c", "{}")[0]

        assert event["status"] == "failed"
        assert event["execution_status"] == "execution_error"
        assert event["business_status"] == "not_applicable"


# ==========================================================================
# 7. Agent 任务层状态独立性
# ==========================================================================
def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments, ensure_ascii=False)
        ),
    )


def _response(content=None, calls=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=calls)
    choice = SimpleNamespace(message=message, finish_reason=finish)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[choice], usage=usage, model="mock")


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _make_client(responses):
    client = LLMClient()
    completions = _FakeCompletions(responses)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("powercopy.core.llm.time.sleep", lambda s: None)


@pytest.mark.skipif(not DB.is_file(), reason="缺少真实政策库")
class TestTaskStatusIndependence:
    def test_business_failure_does_not_fail_task(self, no_sleep):
        """业务失败后模型继续作答，任务应 completed。"""
        client, completions = _make_client([
            _response(
                content=None,
                calls=[_tool_call("c1", "get_policy_detail", {"policy_id": "0" * 32})],
                finish="tool_calls",
            ),
            _response(content="未找到该政策，建议核对编号。", finish="stop"),
        ])
        agent = Agent(
            llm=client,
            registry=build_live_registry(DB),
            system_prompt="助手",
            max_rounds=3,
        )
        result = agent.run("查一个不存在的政策")

        assert result["status"] == "completed"
        payload = json.loads(completions.requests[1]["messages"][-1]["content"])
        assert payload["business_status"] == "failed"
        assert payload["execution_status"] == "ok"

    def test_empty_result_does_not_fail_task(self, no_sleep):
        client, completions = _make_client([
            _response(
                content=None,
                calls=[_tool_call("c1", "search_policy",
                                  {"region": "", "keyword": "完全不存在的词"})],
                finish="tool_calls",
            ),
            _response(content="没有检索到相关记录。", finish="stop"),
        ])
        agent = Agent(
            llm=client,
            registry=build_live_registry(DB),
            system_prompt="助手",
            max_rounds=3,
        )
        result = agent.run("查一下")

        assert result["status"] == "completed"
        payload = json.loads(completions.requests[1]["messages"][-1]["content"])
        assert payload["business_status"] == "empty"
        assert payload["business_code"] == "NO_MATCH"

    def test_tool_budget_still_decides_task_status(self, no_sleep):
        """任务层状态仍由 Agent 控制，不受工具结果影响。"""
        client, _ = _make_client([
            _response(
                content=None,
                calls=[
                    _tool_call("c1", "search_policy", {"region": "", "keyword": "电"}),
                    _tool_call("c2", "search_policy", {"region": "", "keyword": "力"}),
                    _tool_call("c3", "search_policy", {"region": "", "keyword": "市"}),
                ],
                finish="tool_calls",
            ),
        ])
        agent = Agent(
            llm=client,
            registry=build_live_registry(DB),
            system_prompt="助手",
            max_tool_calls=2,
        )
        result = agent.run("查")
        assert result["status"] == "tool_budget_exhausted"
        assert result["tool_requests"] == 0


# ==========================================================================
# 8. 向后兼容
# ==========================================================================
class TestBackwardCompatibility:
    def test_legacy_success_attribute(self):
        """旧代码读 result.success 仍可用。"""
        ok = ToolResult(
            tool_name="t",
            execution_status=ExecutionStatus.OK,
            business_status=BusinessStatus.SUCCESS,
        )
        assert ok.success is True

        bad = ToolResult(
            tool_name="t",
            execution_status=ExecutionStatus.INVALID_JSON,
            business_status=BusinessStatus.NOT_APPLICABLE,
        )
        assert bad.success is False

    def test_legacy_error_type_mapping(self):
        for status, legacy in [
            (ExecutionStatus.TOOL_NOT_FOUND, "TOOL_NOT_FOUND"),
            (ExecutionStatus.INVALID_JSON, "INVALID_JSON"),
            (ExecutionStatus.INVALID_ARGUMENTS, "INVALID_ARGUMENTS"),
            (ExecutionStatus.EXECUTION_ERROR, "EXECUTION_ERROR"),
            (ExecutionStatus.OK, None),
        ]:
            result = ToolResult(
                tool_name="t",
                execution_status=status,
                business_status=BusinessStatus.NOT_APPLICABLE,
            )
            assert result.error_type == legacy

    def test_plain_handler_return_still_works(self):
        """handler 直接返回 dict（不包 ToolOutcome）仍是 success。"""
        class Args(BaseModel):
            pass

        def plain(**kwargs):
            return {"a": 1}

        reg = ToolRegistry()
        reg.register(Tool(name="p", description="d", args_schema=Args, handler=plain))
        r = ToolExecutor(reg).execute("p", "{}")
        assert r.business_status is BusinessStatus.SUCCESS
        assert r.data == {"a": 1}
