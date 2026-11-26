"""把 4.6 的框架中立绑定（PolicyToolBinding）适配到本项目真实 Tool System。

本模块只做三件事：
1. 把 `build_policy_bindings()` 的 binding 转换成项目真实的 `Tool` 数据类；
2. 把 `PolicyQueryError` 收敛成**结构化、无数据库内部信息**的错误负载；
3. 提供两个互斥的注册入口：模拟工具入口、真实数据库工具入口。

设计约束（不修改已验收模块）：
- 不碰 `powercopy/tools/base.py`、`registry.py`、`executor.py`；
- 模型侧参数**不包含**数据库路径、SQL、文件路径、写操作；
- 数据库由程序配置指定（环境变量 `POLICY_DB_PATH`），只读打开。

关于错误映射的说明（重要）
--------------------------
`ToolExecutor.execute` 对 handler 抛出的任何异常统一折叠为
`error_type="EXECUTION_ERROR"`、`error_message="工具执行失败"`，
原始错误码会丢失。由于 `executor.py` 已验收冻结，这里改为
让 handler **返回**结构化错误负载：

    {"ok": False, "error_code": "POLICY_VERSION_NOT_FOUND",
     "error_message": "...", "retryable": False}

这样模型能拿到可行动的失败原因，同时不会看到任何
SQL、路径、堆栈或数据库内部异常原文。成功时返回的负载与
`PolicyQueryService` 的原始契约**完全一致**（不加任何字段）。
"""

from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from powercopy.data.query import (
    PolicyDetailArgs,
    PolicyQueryError,
    PolicyQueryService,
    SearchPolicyArgs,
)
from powercopy.tools.base import Tool
from powercopy.tools.builtin.policy import (
    policy_detail_tool,
    policy_search_tool,
)
from powercopy.tools.builtin.policy_db import (
    PolicyToolBinding,
    build_policy_bindings,
)
from powercopy.tools.registry import ToolRegistry


DEFAULT_DB_PATH = "storage/policies_versioned.db"
DB_PATH_ENV = "POLICY_DB_PATH"

# 面向模型的固定提示文案；不含任何数据库细节。
SAFE_MESSAGES = {
    "POLICY_DB_NOT_FOUND": "本地政策库当前不可用，请改用其它信息或告知用户稍后重试。",
    "POLICY_DB_READ_ERROR": "本地政策库读取失败，请稍后重试或告知用户。",
    "POLICY_VERSION_NOT_FOUND": "没有找到该 policy_id 与 version 对应记录。",
    "BODY_OFFSET_OUT_OF_RANGE": "offset 超出该政策正文长度，请从 0 开始重新读取。",
}
DEFAULT_SAFE_MESSAGE = "政策查询失败，请调整参数后重试。"


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """按「显式参数 → 环境变量 → 默认值」的顺序确定只读数据库路径。

    该路径只来自程序配置，永远不作为模型可控参数暴露。
    """

    if db_path is not None:
        return Path(db_path)
    return Path(environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def _safe_error(code: str) -> dict:
    return {
        "ok": False,
        "error_code": code,
        "error_message": SAFE_MESSAGES.get(code, DEFAULT_SAFE_MESSAGE),
        "retryable": False,
    }


def wrap_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    """把 PolicyQueryError 收敛成结构化负载，其余异常继续向上抛。

    继续向上抛的异常（如程序缺陷）由 ToolExecutor 折叠为
    EXECUTION_ERROR，同样不会把堆栈或 SQL 暴露给模型。
    """

    def wrapped(**kwargs):
        try:
            return handler(**kwargs)
        except PolicyQueryError as error:
            return _safe_error(getattr(error, "code", "") or "")

    wrapped.__name__ = getattr(handler, "__name__", "policy_handler")
    return wrapped


def binding_to_tool(binding: PolicyToolBinding) -> Tool:
    """PolicyToolBinding → 项目真实 Tool。"""

    return Tool(
        name=binding.name,
        description=binding.description,
        args_schema=binding.args_model,
        handler=wrap_handler(binding.handler),
    )


def build_policy_tools(
    service: PolicyQueryService,
) -> tuple[Tool, Tool]:
    """构建两个真实数据库工具（不注册）。"""

    bindings = build_policy_bindings(service)
    tools = tuple(binding_to_tool(binding) for binding in bindings)
    return tools  # type: ignore[return-value]


def register_policy_tools(
    registry: ToolRegistry,
    service: PolicyQueryService,
) -> list[str]:
    """把真实数据库工具注册进给定 Registry。

    同名工具重复注册由 `ToolRegistry.register` 抛 ValueError，
    这里不吞掉该异常，保证「一个 Registry 同名只注册一次」。
    """

    names = []
    for tool in build_policy_tools(service):
        registry.register(tool)
        names.append(tool.name)
    return names


# ---------------------------------------------------------------- 两个入口
def build_mock_registry() -> ToolRegistry:
    """模拟工具入口（保留给既有测试与离线演示）。"""

    registry = ToolRegistry()
    registry.register(policy_search_tool)
    registry.register(policy_detail_tool)
    return registry


def build_live_registry(
    db_path: str | Path | None = None,
) -> ToolRegistry:
    """真实数据库工具入口。

    数据库路径来自程序配置，模型无法指定；
    连接以 SQLite `mode=ro` + `PRAGMA query_only` 打开。
    """

    registry = ToolRegistry()
    register_policy_tools(registry, PolicyQueryService(resolve_db_path(db_path)))
    return registry


def describe_tools(registry: ToolRegistry) -> list[dict]:
    """便于验收输出：工具名、必填参数、可选参数。"""

    rows = []
    for name in registry.list_tools():
        tool = registry.get(name)
        schema = tool.args_schema.model_json_schema()
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        rows.append(
            {
                "name": tool.name,
                "required": sorted(required),
                "optional": sorted(set(properties) - required),
                "additionalProperties": schema.get("additionalProperties"),
            }
        )
    return rows


__all__ = [
    "DEFAULT_DB_PATH",
    "DB_PATH_ENV",
    "SAFE_MESSAGES",
    "PolicyDetailArgs",
    "SearchPolicyArgs",
    "binding_to_tool",
    "build_live_registry",
    "build_mock_registry",
    "build_policy_tools",
    "describe_tools",
    "register_policy_tools",
    "resolve_db_path",
    "wrap_handler",
]
