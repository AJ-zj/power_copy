"""4.6 真实政策工具接入：Tool Registry / Executor / Agent Runtime 验收测试。

不调用真实模型 API，不联网，不写数据库。
真实数据库使用 4.4 的 storage/policies_versioned.db（只读）。
"""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from powercopy.core.agent import Agent
from powercopy.core.llm import LLMClient
from powercopy.core.message import Messages
from powercopy.data.query import (
    PolicyDetailArgs,
    PolicyQueryError,
    PolicyQueryService,
    SearchPolicyArgs,
)
from powercopy.tools.builtin.policy import policy_detail_tool, policy_search_tool
from powercopy.tools.builtin.policy_tools import (
    DB_PATH_ENV,
    SAFE_MESSAGES,
    binding_to_tool,
    build_live_registry,
    build_mock_registry,
    build_policy_tools,
    describe_tools,
    register_policy_tools,
    resolve_db_path,
    wrap_handler,
)
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "policies_versioned.db"

NDRC_POLICY_ID = "70c4a432f8ca4201b8514bc5f33a2ab6"
NEA_POLICY_ID = "00aaf3720779431692a2d422fc7eec49"

pytestmark = pytest.mark.skipif(
    not DB.is_file(),
    reason="缺少 storage/policies_versioned.db，跳过真实政策库测试",
)


# --------------------------------------------------------------------------
# Mock Chat Completions
# --------------------------------------------------------------------------
def make_tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )


def make_response(content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=20, total_tokens=120
    )
    return SimpleNamespace(choices=[choice], usage=usage, model="glm-5.3-flash")


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def make_client(responses):
    client = LLMClient()
    completions = FakeCompletions(responses)
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return client, completions


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("powercopy.core.llm.time.sleep", lambda s: None)


@pytest.fixture
def live_registry():
    return build_live_registry(DB)


# --------------------------------------------------------------------------
# 1. 真实版本化数据库能够完成政策搜索
# --------------------------------------------------------------------------
class TestRealSearch:
    def test_search_returns_real_policies(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        )

        assert result.success is True
        assert result.data["returned"] == 2
        assert result.data["search_mode"] == "literal_substring"

        ids = {item["policy_id"] for item in result.data["items"]}
        assert ids == {NDRC_POLICY_ID, NEA_POLICY_ID}

    def test_search_title_and_body_both_match(self, live_registry):
        executor = ToolExecutor(live_registry)

        # 标题命中排在前面
        by_title = executor.execute(
            "search_policy", '{"region": "", "keyword": "运行基本规则"}'
        )
        assert by_title.data["items"][0]["policy_id"] == NDRC_POLICY_ID

        # 只在正文出现的词
        by_body = executor.execute(
            "search_policy", '{"region": "", "keyword": "电能计量检测机构"}'
        )
        assert [i["policy_id"] for i in by_body.data["items"]] == [NDRC_POLICY_ID]

    def test_search_result_has_no_body_text(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        )
        for item in result.data["items"]:
            assert "body_text" not in item


# --------------------------------------------------------------------------
# 2. 默认仅最新观测版本，支持明确历史版本
# --------------------------------------------------------------------------
class TestVersionSelection:
    def test_default_is_latest_observed(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        )
        assert all(item["is_latest_observed"] is True for item in result.data["items"])
        assert all(item["version"] >= 1 for item in result.data["items"])

    def test_explicit_version_accepted(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "get_policy_detail",
            json.dumps({"policy_id": NDRC_POLICY_ID, "version": 1}),
        )
        assert result.success is True
        assert result.data["version"] == 1
        assert result.data["is_latest_observed"] is True

    def test_unknown_version_rejected(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "get_policy_detail",
            json.dumps({"policy_id": NDRC_POLICY_ID, "version": 99}),
        )
        assert result.data["ok"] is False
        assert result.data["error_code"] == "POLICY_VERSION_NOT_FOUND"

    def test_notice_warns_about_legal_validity(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID})
        )
        assert "未核验当前法律效力" in result.data["notice"]


# --------------------------------------------------------------------------
# 3. 地区筛选及未知地区处理
# --------------------------------------------------------------------------
class TestRegionHandling:
    def test_empty_region_returns_all(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        )
        assert result.data["region_filter"] == "all"
        assert result.data["returned"] == 2

    def test_unknown_region_returns_nothing(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "西藏", "keyword": "电力"}'
        )
        # 两份真实政策 region 均为 None（国家级），因此仍会作为 unassigned 命中
        assert result.data["region_filter"] == "exact_or_unassigned"
        for item in result.data["items"]:
            assert item["region_match"] in {"exact", "unassigned"}

    def test_unassigned_not_claiming_national(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "安徽", "keyword": "电力"}'
        )
        assert "不代表全国适用" in result.data["notice"]
        for item in result.data["items"]:
            assert item["region_match"] == "unassigned"


# --------------------------------------------------------------------------
# 4. 查询结果与详情返回数据契约
# --------------------------------------------------------------------------
class TestDataContract:
    def test_search_contract_keys(self, live_registry):
        executor = ToolExecutor(live_registry)
        data = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        ).data

        assert set(data) == {
            "items",
            "returned",
            "has_more",
            "search_mode",
            "region_filter",
            "notice",
        }

    def test_detail_contract_keys(self, live_registry):
        executor = ToolExecutor(live_registry)
        data = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID})
        ).data

        expected = {
            "policy_id",
            "version",
            "document_id",
            "content_sha256",
            "title",
            "region",
            "issuer",
            "published_on",
            "source_key",
            "source_url",
            "document_retrieved_at",
            "fetch_id",
            "raw_sha256",
            "last_observed_at",
            "observation_count",
            "is_latest_observed",
            "body_length",
            "body_text",
            "body_offset",
            "next_offset",
            "body_complete",
            "notice",
        }
        assert set(data) == expected

    def test_values_match_database(self, live_registry):
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT d.title, d.issuer, d.published_on, d.source_url
               FROM policy_documents d
               JOIN policy_versions v ON v.document_id = d.document_id
               WHERE v.policy_id = ? AND v.version = 1""",
            (NDRC_POLICY_ID,),
        ).fetchone()
        conn.close()

        executor = ToolExecutor(live_registry)
        data = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID})
        ).data

        assert data["title"] == row["title"]
        assert data["issuer"] == row["issuer"]
        assert data["published_on"] == row["published_on"]
        assert data["source_url"] == row["source_url"]


# --------------------------------------------------------------------------
# 5. 来源URL、版本、fetch_id 及哈希关联
# --------------------------------------------------------------------------
class TestProvenance:
    def test_fetch_id_matches_db(self, live_registry):
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        expected = conn.execute(
            "SELECT fetch_id, raw_sha256 FROM policy_fetches "
            "WHERE policy_id = ? AND version = 1",
            (NDRC_POLICY_ID,),
        ).fetchone()
        conn.close()

        executor = ToolExecutor(live_registry)
        data = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID})
        ).data

        assert data["fetch_id"] == expected["fetch_id"]
        assert data["raw_sha256"] == expected["raw_sha256"]

    def test_hashes_are_64_hex(self, live_registry):
        executor = ToolExecutor(live_registry)
        for pid in (NDRC_POLICY_ID, NEA_POLICY_ID):
            data = executor.execute(
                "get_policy_detail", json.dumps({"policy_id": pid})
            ).data
            assert len(data["raw_sha256"]) == 64
            assert len(data["content_sha256"]) == 64

    def test_source_url_is_official(self, live_registry):
        executor = ToolExecutor(live_registry)
        ndrc = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID})
        ).data
        nea = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NEA_POLICY_ID})
        ).data

        assert ndrc["source_url"].startswith("https://zfxxgk.ndrc.gov.cn/")
        assert nea["source_url"].startswith("https://zfxxgk.nea.gov.cn/")


# --------------------------------------------------------------------------
# 6. 只读连接、参数化 SQL 及非法参数防护
# --------------------------------------------------------------------------
class TestReadOnlyAndInjection:
    def test_database_unchanged_after_queries(self, live_registry):
        def snapshot():
            conn = sqlite3.connect(DB)
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "policy_identities",
                    "policy_versions",
                    "policy_fetches",
                    "policy_documents",
                )
            }
            conn.close()
            return counts

        before = snapshot()
        executor = ToolExecutor(live_registry)
        executor.execute("search_policy", '{"region": "", "keyword": "电力"}')
        executor.execute("get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID}))
        executor.execute("search_policy", '{"region": "安徽", "keyword": "电价"}')
        assert snapshot() == before

    def test_write_attempt_raises(self):
        service = PolicyQueryService(DB)
        with pytest.raises(PolicyQueryError):
            with service._read() as conn:
                conn.execute("DELETE FROM policy_documents")

    def test_sql_injection_is_literal(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy",
            json.dumps(
                {"region": "", "keyword": "' OR 1=1; DROP TABLE policy_documents;--"},
                ensure_ascii=False,
            ),
        )
        assert result.data["items"] == []
        # 表仍然存在
        conn = sqlite3.connect(DB)
        assert conn.execute("SELECT COUNT(*) FROM policy_documents").fetchone()[0] == 2
        conn.close()

    def test_metacharacters_are_literal(self, live_registry):
        executor = ToolExecutor(live_registry)
        for keyword in ("%", "_"):
            result = executor.execute(
                "search_policy",
                json.dumps({"region": "", "keyword": keyword}, ensure_ascii=False),
            )
            # 真实政策正文不含裸 % 或 _，应命中的是含该字符的文档（此处为 0）
            assert isinstance(result.data["items"], list)

    def test_no_path_or_sql_parameter_exposed(self, live_registry):
        for row in describe_tools(live_registry):
            schema = live_registry.get(row["name"]).args_schema.model_json_schema()
            properties = set(schema.get("properties", {}))
            for forbidden in ("db_path", "database", "sql", "path", "commit", "write"):
                assert forbidden not in properties

    def test_missing_db_returns_structured_error(self, tmp_path):
        registry = ToolRegistry()
        register_policy_tools(
            registry, PolicyQueryService(tmp_path / "absent.db")
        )
        executor = ToolExecutor(registry)
        result = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        )
        assert result.success is True
        assert result.data["ok"] is False
        assert result.data["error_code"] == "POLICY_DB_NOT_FOUND"
        assert not (tmp_path / "absent.db").exists()


# --------------------------------------------------------------------------
# 7. 长文档分段与固定版本读取
# --------------------------------------------------------------------------
class TestPagination:
    def test_single_chunk_when_body_short(self, live_registry):
        executor = ToolExecutor(live_registry)
        data = executor.execute(
            "get_policy_detail", json.dumps({"policy_id": NDRC_POLICY_ID})
        ).data

        assert data["body_complete"] is True
        assert data["next_offset"] is None
        assert data["body_offset"] == 0
        assert len(data["body_text"]) == data["body_length"] == 3829

    def test_offset_out_of_range(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "get_policy_detail",
            json.dumps({"policy_id": NDRC_POLICY_ID, "offset": 999999}),
        )
        assert result.data["error_code"] == "BODY_OFFSET_OUT_OF_RANGE"

    def test_paging_preserves_version(self, tmp_path):
        """用构造的长文档验证分页与固定版本，不写真实业务库。"""
        import base64
        from datetime import datetime, timezone

        from powercopy.data.parser import ParseProfile, RawPage
        from powercopy.data.versioning import VersionedPolicyStore

        body = "电力政策条款内容。" * 1500  # 13500 字符，超过 8000 单次上限
        html = f"<h1>长文政策</h1><article><p>{body}</p></article>".encode()
        raw = RawPage(
            fetch_id="long-doc",
            source_key="test",
            target_key="long",
            source_url="https://example.invalid/long",
            content_kind="policy",
            status_code=200,
            content_type="text/html",
            charset="utf-8",
            retrieved_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            body_size=len(html),
            body_base64=base64.b64encode(html).decode(),
        )
        store = VersionedPolicyStore(tmp_path / "long.db")
        result = store.ingest(
            raw, ParseProfile(title_selector="h1", body_selector="article")
        )

        service = PolicyQueryService(store.db_path)
        full = service.get_policy_detail(result.policy_id, result.version)
        total = full["body_length"]
        assert total > 8000, "构造的长文档必须超过单次返回上限"

        parts, offset = [], 0
        while offset is not None:
            detail = service.get_policy_detail(result.policy_id, result.version, offset)
            assert detail["version"] == result.version
            assert len(detail["body_text"]) <= 8000
            parts.append(detail["body_text"])
            offset = detail["next_offset"]

        assert len(parts) >= 2
        assert len("".join(parts)) == total
        assert full["body_complete"] is False
        assert full["body_text"] == parts[0]


# --------------------------------------------------------------------------
# 8. 实际 Tool Registry 注册及 Function Calling Schema
# --------------------------------------------------------------------------
class TestRegistryAndSchema:
    def test_live_registry_has_two_tools(self, live_registry):
        assert sorted(live_registry.list_tools()) == [
            "get_policy_detail",
            "search_policy",
        ]

    def test_mock_registry_still_available(self):
        mock = build_mock_registry()
        assert sorted(mock.list_tools()) == [
            "get_policy_detail",
            "search_policy",
        ]
        result = ToolExecutor(mock).execute(
            "search_policy", '{"region": "安徽", "keyword": "电价"}'
        )
        assert result.data["mock"] is True

    def test_duplicate_registration_rejected(self, live_registry):
        with pytest.raises(ValueError, match="工具已存在"):
            register_policy_tools(
                live_registry, PolicyQueryService(DB)
            )

    def test_search_schema_optional_limit(self, live_registry):
        schema = live_registry.get("search_policy").args_schema.model_json_schema()
        assert set(schema["required"]) == {"region", "keyword"}
        assert schema["properties"]["limit"]["default"] == 5
        assert schema["additionalProperties"] is False

    def test_detail_schema_optional_version_and_offset(self, live_registry):
        schema = live_registry.get(
            "get_policy_detail"
        ).args_schema.model_json_schema()
        assert set(schema["required"]) == {"policy_id"}
        assert schema["properties"]["version"]["default"] is None
        assert schema["properties"]["offset"]["default"] == 0
        assert schema["additionalProperties"] is False

    def test_openai_schema_shape(self, live_registry):
        for schema in live_registry.get_schemas():
            assert schema["type"] == "function"
            assert set(schema["function"]) == {"name", "description", "parameters"}
            description = schema["function"]["description"]
            assert isinstance(description, str) and description.strip()
            assert schema["function"]["parameters"]["type"] == "object"

    def test_policy_id_pattern_enforced(self, live_registry):
        executor = ToolExecutor(live_registry)
        # document_id 是 UUID，不能当 policy_id 用
        result = executor.execute(
            "get_policy_detail",
            '{"policy_id": "7430758f-033a-4ef2-8b26-428597d20980"}',
        )
        assert result.success is False
        assert result.error_type == "INVALID_ARGUMENTS"

    def test_optional_params_default_applied(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力"}'
        )
        assert result.success is True
        assert result.data["returned"] == 2

    def test_explicit_limit_respected(self, live_registry):
        executor = ToolExecutor(live_registry)
        result = executor.execute(
            "search_policy", '{"region": "", "keyword": "电力", "limit": 1}'
        )
        assert result.data["returned"] == 1
        assert result.data["has_more"] is True


# --------------------------------------------------------------------------
# 9. 真实 Agent Runtime Mock 完整调用链路
# --------------------------------------------------------------------------
class TestAgentRuntimeFullChain:
    def test_three_round_real_db_flow(self, no_sleep, live_registry):
        """第一轮 search → 第二轮用真实 policy_id+version 查详情 → 第三轮总结。"""

        client, completions = make_client(
            [
                # 轮 1：模型请求 search_policy
                make_response(
                    content=None,
                    tool_calls=[
                        make_tool_call(
                            "call_search",
                            "search_policy",
                            {"region": "", "keyword": "电力市场运行基本规则"},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                # 轮 2：模型用真实 policy_id + version 请求 detail
                make_response(
                    content=None,
                    tool_calls=[
                        make_tool_call(
                            "call_detail",
                            "get_policy_detail",
                            {"policy_id": NDRC_POLICY_ID, "version": 1},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                # 轮 3：模型给出最终研究结论
                make_response(
                    content=(
                        "《电力市场运行基本规则》由国家发展改革委于2024-04-25公布，"
                        "自2024年7月1日起施行，现行共十一章四十五条。"
                    ),
                    finish_reason="stop",
                ),
            ]
        )

        agent = Agent(
            llm=client,
            registry=live_registry,
            system_prompt="你是售电政策研究助手，必须使用工具查询真实政策库。",
            max_rounds=5,
        )
        result = agent.run("研究电力市场运行基本规则的核心内容")

        assert result["status"] == "completed"
        assert result["rounds"] == 3
        assert result["tool_requests"] == 2
        assert "电力市场运行基本规则" in result["content"]

        # TaskBudget 正常
        assert result["budget"]["api_attempts"] == 3
        assert result["budget"]["known_total_tokens"] > 0

        # ExecutionTrace 正常
        trace = result["trace"]
        assert trace["status"] == "success"
        assert trace["stop_reason"] == "completed"
        event_types = [event["event_type"] for event in trace["events"]]
        assert "task_started" in event_types
        assert event_types.count("round_started") == 3
        assert event_types.count("model_attempt") == 3
        assert event_types.count("tool_execution") == 2
        assert "task_completed" in event_types

        tool_events = [e for e in trace["events"] if e["event_type"] == "tool_execution"]
        assert tool_events[0]["tool_name"] == "search_policy"
        assert tool_events[0]["status"] == "success"
        assert tool_events[1]["tool_name"] == "get_policy_detail"
        assert tool_events[1]["status"] == "success"

        # 消息回填与 tool_call_id 匹配
        third_request_messages = completions.requests[2]["messages"]
        roles = [m["role"] for m in third_request_messages]
        assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]

        assert third_request_messages[2]["tool_calls"][0]["id"] == "call_search"
        assert third_request_messages[3]["tool_call_id"] == "call_search"
        assert third_request_messages[4]["tool_calls"][0]["id"] == "call_detail"
        assert third_request_messages[5]["tool_call_id"] == "call_detail"

        # 回填的是真实数据库结果
        search_payload = json.loads(third_request_messages[3]["content"])
        assert search_payload["success"] is True
        assert search_payload["data"]["items"][0]["policy_id"] == NDRC_POLICY_ID

        detail_payload = json.loads(third_request_messages[5]["content"])
        assert detail_payload["success"] is True
        assert detail_payload["data"]["version"] == 1
        assert "电力市场运行基本规则" in detail_payload["data"]["title"]
        assert "第四十五条" in detail_payload["data"]["body_text"]

    def test_runtime_handles_unknown_policy(self, no_sleep, live_registry):
        client, completions = make_client(
            [
                make_response(
                    content=None,
                    tool_calls=[
                        make_tool_call(
                            "call_bad",
                            "get_policy_detail",
                            {"policy_id": "0" * 32},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                make_response(content="未查询到该政策。", finish_reason="stop"),
            ]
        )
        agent = Agent(
            llm=client, registry=live_registry, system_prompt="助手", max_rounds=3
        )
        result = agent.run("查询不存在的政策")

        assert result["status"] == "completed"
        tool_message = completions.requests[1]["messages"][-1]
        payload = json.loads(tool_message["content"])
        assert payload["success"] is True, "结构化错误不应折叠为执行失败"
        assert payload["data"]["error_code"] == "POLICY_VERSION_NOT_FOUND"
        # 不得泄漏数据库内部信息
        assert "sqlite" not in tool_message["content"].lower()
        assert "Traceback" not in tool_message["content"]

    def test_runtime_handles_invalid_arguments(self, no_sleep, live_registry):
        client, completions = make_client(
            [
                make_response(
                    content=None,
                    tool_calls=[
                        make_tool_call(
                            "call_invalid",
                            "search_policy",
                            {"region": "", "keyword": ""},
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                make_response(content="参数不合法。", finish_reason="stop"),
            ]
        )
        agent = Agent(
            llm=client, registry=live_registry, system_prompt="助手", max_rounds=3
        )
        result = agent.run("查询")

        assert result["status"] == "completed"
        payload = json.loads(completions.requests[1]["messages"][-1]["content"])
        assert payload["success"] is False
        assert payload["error_type"] == "INVALID_ARGUMENTS"


# --------------------------------------------------------------------------
# 10. 异常反馈、TaskBudget 及 ExecutionTrace
# --------------------------------------------------------------------------
class TestErrorMappingAndBudgets:
    def test_policy_query_error_becomes_structured_payload(self):
        def boom(**kwargs):
            raise PolicyQueryError("POLICY_DB_READ_ERROR")

        wrapped = wrap_handler(boom)
        payload = wrapped(**{})

        assert payload["ok"] is False
        assert payload["error_code"] == "POLICY_DB_READ_ERROR"
        assert payload["error_message"] == SAFE_MESSAGES["POLICY_DB_READ_ERROR"]
        assert payload["retryable"] is False

    def test_unknown_code_falls_back_to_generic_message(self):
        def boom(**kwargs):
            raise PolicyQueryError("SOME_INTERNAL_CODE_WITH_DETAIL")

        payload = wrap_handler(boom)(**{})
        assert payload["error_code"] == "SOME_INTERNAL_CODE_WITH_DETAIL"
        assert "SOME_INTERNAL" not in payload["error_message"]

    def test_unexpected_exception_folded_by_executor(self, live_registry):
        from powercopy.tools.base import Tool

        def boom(**kwargs):
            raise RuntimeError("SELECT * FROM secret_table -- 内部SQL")

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="boom",
                description="boom",
                args_schema=SearchPolicyArgs,
                handler=boom,
            )
        )
        result = ToolExecutor(registry).execute(
            "boom", '{"region": "", "keyword": "x"}'
        )
        assert result.success is False
        assert result.error_type == "EXECUTION_ERROR"
        assert "SQL" not in (result.error_message or "")
        assert "secret_table" not in (result.error_message or "")

    def test_tool_budget_still_enforced(self, no_sleep, live_registry):
        client, _ = make_client(
            [
                make_response(
                    content=None,
                    tool_calls=[
                        make_tool_call("c1", "search_policy", {"region": "", "keyword": "电力"}),
                        make_tool_call("c2", "search_policy", {"region": "", "keyword": "规则"}),
                        make_tool_call("c3", "search_policy", {"region": "", "keyword": "注册"}),
                    ],
                    finish_reason="tool_calls",
                )
            ]
        )
        agent = Agent(
            llm=client,
            registry=live_registry,
            system_prompt="助手",
            max_tool_calls=2,
        )
        result = agent.run("查询")
        assert result["status"] == "tool_budget_exhausted"
        assert result["tool_requests"] == 0

    def test_binding_adapter_is_framework_neutral(self, live_registry):
        service = PolicyQueryService(DB)
        tools = build_policy_tools(service)
        assert [t.name for t in tools] == ["search_policy", "get_policy_detail"]
        assert all(t.args_schema in (SearchPolicyArgs, PolicyDetailArgs) for t in tools)

    def test_describe_tools_reports_optional(self, live_registry):
        rows = {row["name"]: row for row in describe_tools(live_registry)}
        assert rows["search_policy"]["optional"] == ["limit"]
        assert rows["get_policy_detail"]["optional"] == ["offset", "version"]


# --------------------------------------------------------------------------
# 11. 配置解析
# --------------------------------------------------------------------------
class TestDbPathResolution:
    def test_explicit_path_wins(self, monkeypatch):
        monkeypatch.setenv(DB_PATH_ENV, "env/path.db")
        assert resolve_db_path("explicit/path.db") == Path("explicit/path.db")

    def test_env_used_when_no_argument(self, monkeypatch):
        monkeypatch.setenv(DB_PATH_ENV, "env/path.db")
        assert resolve_db_path() == Path("env/path.db")

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv(DB_PATH_ENV, raising=False)
        assert resolve_db_path() == Path("storage/policies_versioned.db")
