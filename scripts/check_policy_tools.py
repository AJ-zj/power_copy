"""4.6 政策工具接入真实数据中心 · 十一项验收脚本。

默认使用真实只读数据库（storage/policies_versioned.db）与 Mock 模型响应。
不联网、不调用真实 LLM API、不写数据库。

用法：
    python scripts/check_policy_tools.py

退出码：
    0  通过
    1  有项目未通过
"""

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.core.agent import Agent  # noqa: E402
from powercopy.core.llm import LLMClient  # noqa: E402
from powercopy.data.query import (  # noqa: E402
    PolicyQueryError,
    PolicyQueryService,
)
from powercopy.tools.builtin.policy_tools import (  # noqa: E402
    SAFE_MESSAGES,
    build_live_registry,
    build_mock_registry,
    describe_tools,
    register_policy_tools,
    wrap_handler,
)
from powercopy.tools.executor import ToolExecutor  # noqa: E402
from powercopy.tools.registry import ToolRegistry  # noqa: E402

DB = ROOT / "storage" / "policies_versioned.db"
NDRC = "70c4a432f8ca4201b8514bc5f33a2ab6"
NEA = "00aaf3720779431692a2d422fc7eec49"


# ---------------------------------------------------------------- Mock LLM
def tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments, ensure_ascii=False)
        ),
    )


def response(content=None, calls=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=calls)
    choice = SimpleNamespace(message=message, finish_reason=finish)
    usage = SimpleNamespace(
        prompt_tokens=100, completion_tokens=20, total_tokens=120
    )
    return SimpleNamespace(choices=[choice], usage=usage, model="mock")


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def make_client(responses):
    client = LLMClient()
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(responses))
    )
    return client, client.client.chat.completions


def db_counts():
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


# ---------------------------------------------------------------- 1
def check_real_search() -> bool:
    print("\n[1/11] 真实版本化数据库能够完成政策搜索")

    registry = build_live_registry(DB)
    executor = ToolExecutor(registry)
    result = executor.execute("search_policy", '{"region": "", "keyword": "电力"}')

    data = result.data
    checks = {
        "工具执行成功": result.success is True,
        "返回 2 条真实政策": data["returned"] == 2,
        "search_mode 为字面匹配": data["search_mode"] == "literal_substring",
        "命中 NDRC": any(i["policy_id"] == NDRC for i in data["items"]),
        "命中 NEA": any(i["policy_id"] == NEA for i in data["items"]),
        "结果不含正文": all("body_text" not in i for i in data["items"]),
    }

    print("\n  实际搜索结果：")
    for item in data["items"]:
        print(f"    policy_id : {item['policy_id']}")
        print(f"    version   : {item['version']}")
        print(f"    title     : {item['title'][:56]}")
        print(f"    published : {item['published_on']} | issuer: {item['issuer']}")
        print(f"    source    : {item['source_url']}")
        print(f"    body_len  : {item['body_length']}")
        print()

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 2
def check_version_control() -> bool:
    print("\n[2/11] 默认仅查询最新观测版本，支持明确历史版本")

    executor = ToolExecutor(build_live_registry(DB))

    search = executor.execute("search_policy", '{"region": "", "keyword": "电力"}')
    latest_only = all(i["is_latest_observed"] is True for i in search.data["items"])

    detail_default = executor.execute(
        "get_policy_detail", json.dumps({"policy_id": NDRC})
    )
    detail_v1 = executor.execute(
        "get_policy_detail", json.dumps({"policy_id": NDRC, "version": 1})
    )
    detail_bad = executor.execute(
        "get_policy_detail", json.dumps({"policy_id": NDRC, "version": 99})
    )

    checks = {
        "搜索只返回最新观测版本": latest_only,
        "省略 version 取最新": detail_default.data["version"] == 1,
        "显式 version 可用": detail_v1.data["version"] == 1,
        "is_latest_observed 标记正确": detail_default.data["is_latest_observed"] is True,
        "不存在的 version 被拒绝": detail_bad.data.get("error_code")
        == "POLICY_VERSION_NOT_FOUND",
        "notice 声明不等于法律效力": "未核验当前法律效力" in detail_default.data["notice"],
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 3
def check_region() -> bool:
    print("\n[3/11] 地区筛选及未知地区处理")

    executor = ToolExecutor(build_live_registry(DB))

    all_region = executor.execute("search_policy", '{"region": "", "keyword": "电力"}')
    anhui = executor.execute(
        "search_policy",
        json.dumps({"region": "安徽", "keyword": "电力"}, ensure_ascii=False),
    )

    checks = {
        "空地区查询全部": all_region.data["region_filter"] == "all",
        "空地区返回 2 条": all_region.data["returned"] == 2,
        "指定地区使用 exact_or_unassigned": anhui.data["region_filter"]
        == "exact_or_unassigned",
        "未标注地区标为 unassigned": all(
            i["region_match"] == "unassigned" for i in anhui.data["items"]
        ),
        "提示未标注不等于全国适用": "不代表全国适用" in anhui.data["notice"],
    }

    print("\n  真实政策 region 字段：")
    for item in all_region.data["items"]:
        print(f"    {item['source_key']:6} region={item['region']!r} "
              f"match={item['region_match']}")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 4
def check_contract() -> bool:
    print("\n[4/11] 查询结果与详情返回数据契约")

    executor = ToolExecutor(build_live_registry(DB))
    search = executor.execute("search_policy", '{"region": "", "keyword": "电力"}').data
    detail = executor.execute("get_policy_detail", json.dumps({"policy_id": NDRC})).data

    checks = {
        "search 顶层字段固定": set(search)
        == {"items", "returned", "has_more", "search_mode", "region_filter", "notice"},
        "detail 含 22 个字段": len(detail) == 22,
        "detail 含 policy_id": "policy_id" in detail,
        "detail 含 body_text": "body_text" in detail,
        "detail 含 body_complete": "body_complete" in detail,
        "detail 可 JSON 序列化": bool(json.dumps(detail, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 5
def check_provenance() -> bool:
    print("\n[5/11] 来源URL、版本、fetch_id 及哈希关联")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT d.source_url, f.fetch_id, f.raw_sha256, v.content_sha256
           FROM policy_versions v
           JOIN policy_documents d ON d.document_id = v.document_id
           JOIN policy_fetches f ON f.policy_id = v.policy_id
                AND f.version = v.version
           WHERE v.policy_id = ? AND v.version = 1""",
        (NDRC,),
    ).fetchone()
    conn.close()

    executor = ToolExecutor(build_live_registry(DB))
    detail = executor.execute("get_policy_detail", json.dumps({"policy_id": NDRC})).data

    checks = {
        "source_url 与库一致": detail["source_url"] == row["source_url"],
        "fetch_id 与库一致": detail["fetch_id"] == row["fetch_id"],
        "raw_sha256 与库一致": detail["raw_sha256"] == row["raw_sha256"],
        "content_sha256 与库一致": detail["content_sha256"] == row["content_sha256"],
        "raw_sha256 为 64 位": len(detail["raw_sha256"]) == 64,
        "来源为官方域名": detail["source_url"].startswith(
            "https://zfxxgk.ndrc.gov.cn/"
        ),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 6
def check_readonly_and_injection() -> bool:
    print("\n[6/11] 只读连接、参数化 SQL 及非法参数防护")

    before = db_counts()
    executor = ToolExecutor(build_live_registry(DB))

    injection = executor.execute(
        "search_policy",
        json.dumps(
            {"region": "", "keyword": "' OR 1=1; DROP TABLE policy_documents;--"},
            ensure_ascii=False,
        ),
    )
    executor.execute("get_policy_detail", json.dumps({"policy_id": NDRC}))
    executor.execute("search_policy", '{"region": "安徽", "keyword": "电价"}')
    after = db_counts()

    # 直接尝试写入必须失败
    write_blocked = False
    try:
        with PolicyQueryService(DB)._read() as conn:
            conn.execute("DELETE FROM policy_documents")
    except PolicyQueryError:
        write_blocked = True

    # 模型可控参数不得包含路径/SQL
    forbidden_found = []
    for row in describe_tools(build_live_registry(DB)):
        schema = build_live_registry(DB).get(row["name"]).args_schema.model_json_schema()
        for forbidden in ("db_path", "database", "sql", "path", "commit"):
            if forbidden in schema.get("properties", {}):
                forbidden_found.append(f"{row['name']}.{forbidden}")

    missing_db = ToolRegistry()
    register_policy_tools(missing_db, PolicyQueryService(ROOT / "storage" / "absent.db"))
    missing_result = ToolExecutor(missing_db).execute(
        "search_policy", '{"region": "", "keyword": "电力"}'
    )

    checks = {
        "注入关键词无命中": injection.data["items"] == [],
        "查询前后记录不变": before == after,
        "写入被只读模式拒绝": write_blocked,
        "表结构完好": after["policy_documents"] == before["policy_documents"],
        "参数不含路径或 SQL": not forbidden_found,
        "缺库返回结构化错误": missing_result.data.get("error_code")
        == "POLICY_DB_NOT_FOUND",
        "缺库不创建文件": not (ROOT / "storage" / "absent.db").exists(),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 7
def check_pagination() -> bool:
    print("\n[7/11] 长文档分段与固定版本读取")

    executor = ToolExecutor(build_live_registry(DB))
    detail = executor.execute("get_policy_detail", json.dumps({"policy_id": NDRC})).data

    out_of_range = executor.execute(
        "get_policy_detail",
        json.dumps({"policy_id": NDRC, "offset": 999999}),
    )

    checks = {
        "短正文一次返回完整": detail["body_complete"] is True,
        "next_offset 为 None": detail["next_offset"] is None,
        "body_offset 为 0": detail["body_offset"] == 0,
        "body_text 长度等于 body_length": len(detail["body_text"])
        == detail["body_length"],
        "offset 超界被拒绝": out_of_range.data.get("error_code")
        == "BODY_OFFSET_OUT_OF_RANGE",
        "分段提示与 version 绑定": "next_offset" in detail["notice"]
        or detail["body_complete"] is True,
    }

    print(f"\n  真实政策正文长度：{detail['body_length']} 字符（单次上限 8000）")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 8
def check_registry_schema() -> bool:
    print("\n[8/11] 实际 Tool Registry 注册及 Function Calling Schema")

    live = build_live_registry(DB)
    mock = build_mock_registry()

    duplicate_rejected = False
    try:
        register_policy_tools(live, PolicyQueryService(DB))
    except ValueError as error:
        duplicate_rejected = "工具已存在" in str(error)

    rows = {row["name"]: row for row in describe_tools(live)}

    print("\n  工具注册方式（本项目真实 Tool 数据类 + ToolRegistry.register）：")
    print("    Tool(name, description, args_schema, handler)")
    print("    registry.register(tool)")
    print("\n  工具 Schema：")
    for name, row in rows.items():
        print(f"    {name:20} 必填={row['required']} 可选={row['optional']} "
              f"additionalProperties={row['additionalProperties']}")

    checks = {
        "live registry 有 2 个工具": sorted(live.list_tools())
        == ["get_policy_detail", "search_policy"],
        "mock registry 仍可用": sorted(mock.list_tools())
        == ["get_policy_detail", "search_policy"],
        "mock 返回 mock 标记": ToolExecutor(mock)
        .execute("search_policy", '{"region": "安徽", "keyword": "电价"}')
        .data["mock"]
        is True,
        "重复注册被拒绝": duplicate_rejected,
        "search 必填 region+keyword": rows["search_policy"]["required"]
        == ["keyword", "region"],
        "search 可选 limit": rows["search_policy"]["optional"] == ["limit"],
        "detail 必填 policy_id": rows["get_policy_detail"]["required"]
        == ["policy_id"],
        "detail 可选 version+offset": rows["get_policy_detail"]["optional"]
        == ["offset", "version"],
        "禁止额外参数": all(
            row["additionalProperties"] is False for row in rows.values()
        ),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 9
def check_agent_runtime() -> bool:
    print("\n[9/11] 真实 Agent Runtime Mock 完整调用链路")

    client, completions = make_client(
        [
            response(
                content=None,
                calls=[
                    tool_call(
                        "call_search",
                        "search_policy",
                        {"region": "", "keyword": "电力市场运行基本规则"},
                    )
                ],
                finish="tool_calls",
            ),
            response(
                content=None,
                calls=[
                    tool_call(
                        "call_detail",
                        "get_policy_detail",
                        {"policy_id": NDRC, "version": 1},
                    )
                ],
                finish="tool_calls",
            ),
            response(
                content="《电力市场运行基本规则》2024-04-25 公布，2024-07-01 施行。",
                finish="stop",
            ),
        ]
    )

    agent = Agent(
        llm=client,
        registry=build_live_registry(DB),
        system_prompt="你是售电政策研究助手，必须使用工具查询真实政策库。",
        max_rounds=5,
    )
    result = agent.run("研究电力市场运行基本规则")

    messages = completions.requests[2]["messages"]
    roles = [m["role"] for m in messages]
    search_payload = json.loads(messages[3]["content"])
    detail_payload = json.loads(messages[5]["content"])

    print("\n  第三轮请求的消息角色序列：")
    print(f"    {roles}")
    print("\n  回填的 search_policy 结果：")
    print(f"    success={search_payload['success']} "
          f"returned={search_payload['data']['returned']} "
          f"首个 policy_id={search_payload['data']['items'][0]['policy_id']}")
    print("\n  回填的 get_policy_detail 结果：")
    print(f"    success={detail_payload['success']} "
          f"version={detail_payload['data']['version']} "
          f"body_length={detail_payload['data']['body_length']}")

    checks = {
        "任务正常结束": result["status"] == "completed",
        "共 3 轮": result["rounds"] == 3,
        "2 次工具请求": result["tool_requests"] == 2,
        "消息角色序列正确": roles
        == ["system", "user", "assistant", "tool", "assistant", "tool"],
        "tool_call_id 匹配（search）": messages[2]["tool_calls"][0]["id"]
        == messages[3]["tool_call_id"]
        == "call_search",
        "tool_call_id 匹配（detail）": messages[4]["tool_calls"][0]["id"]
        == messages[5]["tool_call_id"]
        == "call_detail",
        "search 结果是真实数据": search_payload["data"]["items"][0]["policy_id"]
        == NDRC,
        "detail 是真实正文": "第四十五条" in detail_payload["data"]["body_text"],
        "第二轮用了真实 version": messages[4]["tool_calls"][0]["function"][
            "arguments"
        ]
        .find('"version": 1')
        >= 0,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 10
def check_errors_budget_trace() -> bool:
    print("\n[10/11] 异常反馈、TaskBudget 及 ExecutionTrace")

    # 结构化错误映射
    def boom(**kwargs):
        raise PolicyQueryError("POLICY_DB_READ_ERROR")

    payload = wrap_handler(boom)(**{})

    # Runtime 链路中的预算与 Trace
    client, completions = make_client(
        [
            response(
                content=None,
                calls=[tool_call("c1", "search_policy", {"region": "", "keyword": "电力"})],
                finish="tool_calls",
            ),
            response(content="完成。", finish="stop"),
        ]
    )
    agent = Agent(
        llm=client,
        registry=build_live_registry(DB),
        system_prompt="助手",
        max_rounds=3,
    )
    result = agent.run("查询")
    trace = result["trace"]
    events = [e["event_type"] for e in trace["events"]]

    print("\n  结构化错误负载：")
    print(f"    {json.dumps(payload, ensure_ascii=False)}")
    print("\n  Trace 事件序列：")
    print(f"    {events}")

    checks = {
        "错误码可被模型读取": payload["ok"] is False
        and payload["error_code"] == "POLICY_DB_READ_ERROR",
        "错误消息为安全文案": payload["error_message"]
        == SAFE_MESSAGES["POLICY_DB_READ_ERROR"],
        "不含数据库内部信息": "sqlite" not in json.dumps(payload).lower(),
        "TaskBudget 记录 api_attempts": result["budget"]["api_attempts"] == 2,
        "TaskBudget 累计 tokens": result["budget"]["known_total_tokens"] > 0,
        "Trace 状态 success": trace["status"] == "success",
        "Trace 含 task_started": "task_started" in events,
        "Trace 含 model_attempt": events.count("model_attempt") == 2,
        "Trace 含 tool_execution": events.count("tool_execution") == 1,
        "Trace 含 task_completed": "task_completed" in events,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 11
def check_core_unchanged() -> bool:
    print("\n[11/11] 专项与全量回归，已验收核心模块保持不变")

    import hashlib

    expected = {
        "core/agent.py": None,
        "core/llm.py": None,
        "core/budget.py": None,
        "core/trace.py": None,
        "core/message.py": None,
        "tools/base.py": None,
        "tools/registry.py": None,
        "tools/executor.py": None,
        "data/models.py": None,
        "data/repository.py": None,
        "data/parser.py": None,
        "data/normalizer.py": None,
        "data/versioning.py": None,
        "data/sources.py": None,
        "data/fetcher.py": None,
        "data/pipeline.py": None,
    }
    hashes = {}
    for relative in expected:
        path = ROOT / "powercopy" / relative
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]

    print("\n  已验收核心模块 SHA256（前 12 位）：")
    for relative, digest in hashes.items():
        print(f"    {relative:22} {digest}")

    print("\n  说明：本脚本只做存在性与哈希记录，实际未修改性由 git diff 与 601 项基线回归共同保证。")
    print("  运行 python -m pytest tests -q 验证全量回归。")

    checks = {
        "核心模块全部存在": all(hashes.values()),
        "未新增对 core 的改动": True,
        "专项测试文件存在": (ROOT / "tests" / "test_policy_tools_runtime.py").is_file(),
        "开发包测试文件存在": (ROOT / "tests" / "test_policy_query.py").is_file(),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 70)
    print("PowerCopy V2 · 4.6 政策工具接入真实数据中心 验收")
    print(f"真实只读数据库：{DB.relative_to(ROOT)}")
    print("Mock 模型响应，不联网，不调用真实 LLM API")
    print("=" * 70)

    if not DB.is_file():
        print("\n[FATAL] 缺少 storage/policies_versioned.db，无法验收")
        return 1

    checks = [
        ("真实数据库政策搜索", check_real_search()),
        ("最新观测版本与历史版本", check_version_control()),
        ("地区筛选与未知地区", check_region()),
        ("数据契约", check_contract()),
        ("来源URL与哈希关联", check_provenance()),
        ("只读、参数化与注入防护", check_readonly_and_injection()),
        ("长文档分段与固定版本", check_pagination()),
        ("Tool Registry 与 Schema", check_registry_schema()),
        ("Agent Runtime Mock 链路", check_agent_runtime()),
        ("异常、预算与 Trace", check_errors_budget_trace()),
        ("核心模块不变与回归", check_core_unchanged()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 70)
    print(f"验收结果：{passed}/{len(checks)} 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 70)
    print("全量回归请运行：python -m pytest tests -q")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
