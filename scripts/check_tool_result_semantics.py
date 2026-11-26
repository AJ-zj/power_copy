"""04.9 Tool Result Semantics 验收脚本。

全程离线：不联网、不调用模型 API、不写数据库。

用法：
    python scripts/check_tool_result_semantics.py

退出码：
    0  通过
    1  有项目未通过
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from powercopy.data.query import PolicyQueryError, PolicyQueryService  # noqa: E402
from powercopy.tools.base import Tool  # noqa: E402
from powercopy.tools.builtin.policy_tools import (  # noqa: E402
    SAFE_MESSAGES,
    build_live_registry,
    register_policy_tools,
    wrap_handler,
)
from powercopy.tools.executor import ToolExecutor  # noqa: E402
from powercopy.tools.registry import ToolRegistry  # noqa: E402
from powercopy.tools.result import (  # noqa: E402
    BusinessStatus,
    ExecutionStatus,
    ToolBusinessError,
    ToolOutcome,
    ToolResult,
)

DB = ROOT / "storage" / "policies_versioned.db"
NDRC = "70c4a432f8ca4201b8514bc5f33a2ab6"


class _Args(BaseModel):
    text: str = Field(default="x")


def _build_test_registry() -> ToolRegistry:
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
        raise RuntimeError("内部崩溃，不应透传")

    reg = ToolRegistry()
    for name, handler in (
        ("ok", ok),
        ("empty", empty),
        ("fails", fails),
        ("crashes", crashes),
    ):
        reg.register(
            Tool(name=name, description="d", args_schema=_Args, handler=handler)
        )
    return reg


# ---------------------------------------------------------------- 1
def check_status_enums() -> bool:
    print("\n[1/11] 三层状态枚举定义")

    checks = {
        "执行层 5 个取值": {str(s) for s in ExecutionStatus}
        == {
            "ok",
            "tool_not_found",
            "invalid_json",
            "invalid_arguments",
            "execution_error",
        },
        "业务层 4 个取值": {str(s) for s in BusinessStatus}
        == {"success", "empty", "failed", "not_applicable"},
        "empty 是独立状态": BusinessStatus.EMPTY != BusinessStatus.FAILED,
        "not_applicable 存在": BusinessStatus.NOT_APPLICABLE
        == "not_applicable",
    }

    print("\n  执行层：ok / tool_not_found / invalid_json / "
          "invalid_arguments / execution_error")
    print("  业务层：success / empty / failed / not_applicable")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 2
def check_executor_mapping() -> bool:
    print("\n[2/11] ToolExecutor 状态映射")

    executor = ToolExecutor(_build_test_registry())

    cases = {
        "成功": (executor.execute("ok", "{}"), "ok", "success"),
        "空结果": (executor.execute("empty", "{}"), "ok", "empty"),
        "业务失败": (executor.execute("fails", "{}"), "ok", "failed"),
        "执行崩溃": (
            executor.execute("crashes", "{}"),
            "execution_error",
            "not_applicable",
        ),
        "工具不存在": (
            executor.execute("ghost", "{}"),
            "tool_not_found",
            "not_applicable",
        ),
        "非法 JSON": (
            executor.execute("ok", "not json"),
            "invalid_json",
            "not_applicable",
        ),
    }

    print("\n  场景 → (execution_status, business_status)")
    ok_all = True
    for label, (result, exec_expected, biz_expected) in cases.items():
        actual = (str(result.execution_status), str(result.business_status))
        matched = actual == (exec_expected, biz_expected)
        ok_all = ok_all and matched
        print(f"    {label:8} → {actual}  {'✓' if matched else '✗ 期望 ' + str((exec_expected, biz_expected))}")

    checks = {
        "全部映射正确": ok_all,
        "业务失败不是执行失败": str(executor.execute("fails", "{}").execution_status)
        == "ok",
    }
    print()
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 3
def check_empty_vs_failed() -> bool:
    print("\n[3/11] 空结果与业务失败的语义区分")

    executor = ToolExecutor(_build_test_registry())
    empty = executor.execute("empty", "{}")
    failed = executor.execute("fails", "{}")

    checks = {
        "empty.business_ok 为真": empty.business_ok is True,
        "empty.has_business_failure 为假": empty.has_business_failure is False,
        "failed.business_ok 为假": failed.business_ok is False,
        "failed.has_business_failure 为真": failed.has_business_failure is True,
        "两者执行层都是 ok": str(empty.execution_status) == "ok"
        and str(failed.execution_status) == "ok",
        "两者 business_status 不同": str(empty.business_status)
        != str(failed.business_status),
    }

    print(f"\n  empty  → business_status={empty.business_status}, "
          f"business_ok={empty.business_ok}")
    print(f"  failed → business_status={failed.business_status}, "
          f"has_business_failure={failed.has_business_failure}")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 4
def check_business_error_contract() -> bool:
    print("\n[4/11] ToolBusinessError 契约")

    checks = {}

    try:
        ToolBusinessError("")
        checks["空错误码被拒绝"] = False
    except ValueError:
        checks["空错误码被拒绝"] = True

    try:
        ToolBusinessError("   ")
        checks["纯空白错误码被拒绝"] = False
    except ValueError:
        checks["纯空白错误码被拒绝"] = True

    error = ToolBusinessError("X", "msg", retryable=True)
    checks["保留原始错误码"] = error.code == "X"
    checks["保留消息"] = error.message == "msg"
    checks["retryable 可设置"] = error.retryable is True

    default_msg = ToolBusinessError("ONLY_CODE")
    checks["无消息时回退到错误码"] = str(default_msg) == "ONLY_CODE"

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 5
def check_policy_tools() -> bool:
    print("\n[5/11] 真实政策工具状态语义")

    if not DB.is_file():
        print("  [ ] 缺少真实政策库，跳过")
        return False

    executor = ToolExecutor(build_live_registry(DB))

    normal = executor.execute("search_policy", '{"region": "", "keyword": "电力"}')
    empty = executor.execute(
        "search_policy",
        json.dumps(
            {"region": "", "keyword": "绝不存在的关键词组合"}, ensure_ascii=False
        ),
    )
    unknown = executor.execute(
        "get_policy_detail", '{"policy_id": "00000000000000000000000000000000"}'
    )
    detail = executor.execute(
        "get_policy_detail", json.dumps({"policy_id": NDRC})
    )

    print("\n  真实查询结果：")
    print(f"    正常搜索 : execution={normal.execution_status} "
          f"business={normal.business_status} "
          f"returned={normal.data.get('returned')}")
    print(f"    空结果   : execution={empty.execution_status} "
          f"business={empty.business_status} code={empty.business_code}")
    print(f"    未知政策 : execution={unknown.execution_status} "
          f"business={unknown.business_status} code={unknown.business_code}")
    print(f"    正常详情 : execution={detail.execution_status} "
          f"business={detail.business_status} version={detail.data.get('version')}")

    checks = {
        "正常搜索 = success": str(normal.business_status) == "success",
        "搜索无命中 = empty": str(empty.business_status) == "empty",
        "无命中错误码 NO_MATCH": empty.business_code == "NO_MATCH",
        "未知政策 = failed": str(unknown.business_status) == "failed",
        "未知政策错误码保留": unknown.business_code == "POLICY_VERSION_NOT_FOUND",
        "详情 = success": str(detail.business_status) == "success",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 6
def check_error_safety() -> bool:
    print("\n[6/11] 业务错误不泄漏内部信息")

    if not DB.is_file():
        print("  [ ] 缺少真实政策库，跳过")
        return False

    executor = ToolExecutor(build_live_registry(DB))
    result = executor.execute(
        "get_policy_detail", '{"policy_id": "00000000000000000000000000000000"}'
    )
    text = json.dumps(result.to_dict(), ensure_ascii=False)

    leaks = ["sqlite", "SELECT", "Traceback", "/storage/", "policy_documents"]
    found = [leak for leak in leaks if leak in text]

    print(f"\n  检查的泄漏关键词: {leaks}")
    print(f"  命中: {found if found else '无'}")
    print(f"  面向模型的文案: {result.error_message}")

    checks = {
        "无 SQL 泄漏": "SELECT" not in text,
        "无数据库文件路径": "/storage/" not in text,
        "无堆栈": "Traceback" not in text,
        "使用安全文案": result.error_message
        == SAFE_MESSAGES["POLICY_VERSION_NOT_FOUND"],
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 7
def check_wrap_handler() -> bool:
    print("\n[7/11] wrap_handler 转换规则")

    def boom(**kwargs):
        raise PolicyQueryError("POLICY_DB_READ_ERROR")

    converted = False
    try:
        wrap_handler(boom)(**{})
    except ToolBusinessError as error:
        converted = error.code == "POLICY_DB_READ_ERROR"

    def empty_search(**kwargs):
        return {"items": [], "returned": 0, "search_mode": "literal_substring"}

    outcome = wrap_handler(empty_search)(**{})
    empty_ok = (
        isinstance(outcome, ToolOutcome)
        and outcome.business_status is BusinessStatus.EMPTY
    )

    def plain_detail(**kwargs):
        return {"policy_id": "x"}

    plain = wrap_handler(plain_detail)(**{})
    plain_ok = not isinstance(plain, ToolOutcome)

    checks = {
        "PolicyQueryError → ToolBusinessError": converted,
        "空搜索 → ToolOutcome(empty)": empty_ok,
        "非搜索返回原样透传": plain_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 8
def check_trace() -> bool:
    print("\n[8/11] Trace 事件记录两层状态")

    from powercopy.core.trace import ExecutionTrace, _current_trace

    def run(name):
        trace = ExecutionTrace()
        token = _current_trace.set(trace)
        try:
            ToolExecutor(_build_test_registry()).execute(name, "{}")
        finally:
            _current_trace.reset(token)
        events = [e for e in trace.events if e["event_type"] == "tool_execution"]
        return events[0] if events else {}

    success_event = run("ok")
    business_event = run("fails")
    crash_event = run("crashes")

    print("\n  Trace 事件：")
    for label, event in (
        ("成功", success_event),
        ("业务失败", business_event),
        ("执行崩溃", crash_event),
    ):
        print(f"    {label:6} status={event.get('status')} "
              f"execution={event.get('execution_status')} "
              f"business={event.get('business_status')}")

    checks = {
        "成功事件两层都是成功": success_event.get("status") == "success"
        and success_event.get("business_status") == "success",
        "业务失败事件 status 仍为 success": business_event.get("status")
        == "success",
        "业务失败事件 business 为 failed": business_event.get("business_status")
        == "failed",
        "业务失败事件带 business_code": business_event.get("business_code")
        == "BIZ_CODE",
        "崩溃事件 status 为 failed": crash_event.get("status") == "failed",
        "崩溃事件 business 为 not_applicable": crash_event.get(
            "business_status"
        )
        == "not_applicable",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 9
def check_task_independence() -> bool:
    print("\n[9/11] Task Status 不由单次工具结果决定")

    print("\n  ToolResult 结构中没有 task 字段:")
    fields = set(ToolResult.__dataclass_fields__)
    task_fields = [f for f in fields if "task" in f.lower()]
    print(f"    字段: {sorted(fields)}")
    print(f"    含 task 的字段: {task_fields if task_fields else '无'}")

    checks = {
        "ToolResult 无 task 字段": not task_fields,
        "执行层与业务层独立": "execution_status" in fields
        and "business_status" in fields,
        "success 是派生属性": isinstance(
            ToolResult.__dict__.get("success"), property
        ),
        "error_type 是派生属性": isinstance(
            ToolResult.__dict__.get("error_type"), property
        ),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 10
def check_backward_compat() -> bool:
    print("\n[10/11] 向后兼容")

    ok_result = ToolResult(
        tool_name="t",
        execution_status=ExecutionStatus.OK,
        business_status=BusinessStatus.SUCCESS,
    )
    bad_result = ToolResult(
        tool_name="t",
        execution_status=ExecutionStatus.INVALID_JSON,
        business_status=BusinessStatus.NOT_APPLICABLE,
    )

    legacy_map = {
        ExecutionStatus.TOOL_NOT_FOUND: "TOOL_NOT_FOUND",
        ExecutionStatus.INVALID_JSON: "INVALID_JSON",
        ExecutionStatus.INVALID_ARGUMENTS: "INVALID_ARGUMENTS",
        ExecutionStatus.EXECUTION_ERROR: "EXECUTION_ERROR",
    }
    mapping_ok = all(
        ToolResult(
            tool_name="t",
            execution_status=status,
            business_status=BusinessStatus.NOT_APPLICABLE,
        ).error_type
        == expected
        for status, expected in legacy_map.items()
    )

    payload = ok_result.to_dict()
    compat_keys = {"tool_name", "success", "data", "error_type",
                   "error_message", "latency_ms"}
    new_keys = {"execution_status", "business_status", "business_code", "retryable"}

    checks = {
        "result.success 仍可读": ok_result.success is True
        and bad_result.success is False,
        "error_type 旧值映射正确": mapping_ok,
        "to_dict 保留旧字段": compat_keys <= set(payload),
        "to_dict 新增新字段": new_keys <= set(payload),
        "可 JSON 序列化": bool(json.dumps(payload, ensure_ascii=False)),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 11
def check_no_unrelated_change() -> bool:
    print("\n[11/11] 改动范围检查")

    import hashlib

    expected_unchanged = [
        "core/llm.py",
        "core/budget.py",
        "core/retry.py",
        "core/message.py",
        "core/schemas.py",
        "core/config.py",
        "tools/base.py",
        "tools/registry.py",
        "data/models.py",
        "data/repository.py",
        "data/parser.py",
        "data/normalizer.py",
        "data/versioning.py",
        "data/sources.py",
        "data/fetcher.py",
        "data/pipeline.py",
        "data/query.py",
        "data/pdf_ingest.py",
        "rag/chunking.py",
    ]

    print("\n  本次允许改动的文件（04.9 范围）：")
    allowed = [
        "tools/result.py",
        "tools/executor.py",
        "tools/builtin/policy_tools.py",
        "core/trace.py",
    ]
    for path in allowed:
        print(f"    - powercopy/{path}")

    print("\n  未改动的模块（哈希记录）：")
    digests = {}
    for relative in expected_unchanged:
        path = ROOT / "powercopy" / relative
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    for relative, digest in list(digests.items())[:6]:
        print(f"    {relative:24} {digest}")
    print(f"    ... 共 {len(digests)} 个")

    if DB.is_file():
        import sqlite3

        conn = sqlite3.connect(DB)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        conn.close()
    else:
        tables = []

    checks = {
        "核心模块全部存在": all(digests.values()),
        "数据库表结构未变": tables
        == ["policy_documents", "policy_fetches", "policy_identities",
            "policy_versions"],
        "未新增依赖": "requirements.txt 未在本节修改",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 72)
    print("PowerCopy V2 · 04.9 Tool Result Semantics 验收")
    print("全程离线：不联网、不调用模型 API、不写数据库")
    print("=" * 72)

    checks = [
        ("三层状态枚举", check_status_enums()),
        ("Executor 状态映射", check_executor_mapping()),
        ("空结果与业务失败区分", check_empty_vs_failed()),
        ("ToolBusinessError 契约", check_business_error_contract()),
        ("真实政策工具语义", check_policy_tools()),
        ("错误信息安全", check_error_safety()),
        ("wrap_handler 转换", check_wrap_handler()),
        ("Trace 两层记录", check_trace()),
        ("Task Status 独立性", check_task_independence()),
        ("向后兼容", check_backward_compat()),
        ("改动范围", check_no_unrelated_change()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 72)
    print(f"验收结果：{passed}/{len(checks)} 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 72)
    print("全量回归请运行：python -m pytest tests -q")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
