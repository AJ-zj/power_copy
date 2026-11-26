"""真实政策查询工具：把 4.6 的绑定适配到项目 Tool System。

04.9 更新
=========
之前本模块用「handler 返回错误负载」的方式绕过 `ToolExecutor` 把异常
统一折叠为 `EXECUTION_ERROR` 的限制。现在 `ToolExecutor` 支持
`ToolBusinessError` 与 `ToolOutcome`，可以按语义正确分类：

| 情况 | execution_status | business_status | 模型看到 |
|---|---|---|---|
| 查到政策 | ok | success | 数据 |
| 搜索无命中（正常空结果） | ok | empty | 空列表 + 说明 |
| 政策不存在 / 版本不存在 | ok | failed | 业务错误码 |
| 数据库不可读 | ok | failed | 业务错误码 |
| 参数不合法 | invalid_arguments | not_applicable | Schema 错误 |
| handler 崩溃 | execution_error | not_applicable | 通用错误 |

`empty` 与 `failed` 的区分是 04.9 的核心：
搜索没命中不是错误，模型不该因此重试或道歉。
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
from powercopy.tools.result import (
    BusinessStatus,
    ToolBusinessError,
    ToolOutcome,
)


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


def _business_error(code: str) -> ToolBusinessError:
    """把内部错误码转成对模型安全的业务异常。"""

    return ToolBusinessError(
        code,
        SAFE_MESSAGES.get(code, DEFAULT_SAFE_MESSAGE),
        retryable=False,
    )


def wrap_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    """把 PolicyQueryError 翻译成 ToolBusinessError。

    其余异常（程序缺陷）继续向上抛，由 ToolExecutor 归为
    `execution_error`，同样不会把堆栈或 SQL 暴露给模型。

    同时把「搜索无结果」从 `success` 修正为 `empty`：
    查不到政策是正常业务结果，不是失败。
    """

    def wrapped(**kwargs):
        try:
            result = handler(**kwargs)
        except PolicyQueryError as error:
            raise _business_error(getattr(error, "code", "") or "") from error

        # 搜索结果为空 → business_status = empty
        if isinstance(result, dict) and result.get("search_mode") is not None:
            if not result.get("items"):
                return ToolOutcome(
                    data=result,
                    business_status=BusinessStatus.EMPTY,
                    business_code="NO_MATCH",
                )

        return result

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
