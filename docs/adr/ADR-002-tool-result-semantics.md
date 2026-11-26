# ADR-002：ToolResult 三段式状态语义

- **状态**：**已采纳并实施**（04.9，2026-11-26）
- **原提议日期**：2026-11-26
- **实施日期**：2026-11-26
- **相关阶段**：阶段04.9 Tool Result Semantics
- **关联问题**：P0-1

---

## 背景

旧 `ToolResult` 只有一个 `success: bool`，`ToolExecutor` 对 handler 抛出的**任何**异常统一折叠为 `execution_error`：

```python
except Exception as error:
    return finish(success=False, error_type="EXECUTION_ERROR",
                  error_message="工具执行失败")
```

导致业务失败与执行崩溃无法区分。4.6 节接入真实政策查询时只能在 handler 里返回错误负载绕过，造成 `success=True` 但业务失败的语义矛盾。

---

## 决策

**采纳三段式状态分离，已实施。**

### 三层归属

| 层 | 拥有者 | 取值 |
|---|---|---|
| **Execution Status** | `ToolExecutor` 独占 | `ok` / `tool_not_found` / `invalid_json` / `invalid_arguments` / `execution_error` |
| **Business Status** | handler（返回值或异常） | `success` / `empty` / `failed` / `not_applicable` |
| **Task Status** | `Agent` | 不属于 `ToolResult` |

### 核心语义

**`empty` 与 `failed` 必须区分：**

- `empty` = 业务执行正常，没命中任何结果 → **正常结果，不是错误**
- `failed` = 该做的事做不了（数据库不存在、版本不存在） → **业务失败，保留错误码**

搜索无命中若标为 `failed`，模型会误以为查询出错而重试或道歉。

### 实现方式

1. `ToolBusinessError(code, message, retryable)` — handler 用它声明业务失败
2. `ToolOutcome(data, business_status, business_code)` — handler 用它显式声明业务状态
3. handler 直接返回其他值 → 视为业务成功（向后兼容）

`ToolExecutor` 的映射：

```python
except ToolBusinessError as error:
    # 执行层 OK，业务层 FAILED
    return finish(ExecutionStatus.OK, BusinessStatus.FAILED,
                  business_code=error.code, ...)

except Exception:
    # 执行层失败
    return finish(ExecutionStatus.EXECUTION_ERROR,
                  BusinessStatus.NOT_APPLICABLE, ...)
```

### 向后兼容

`success` 与 `error_type` 保留为**派生 property**：

```python
@property
def success(self) -> bool:
    return self.execution_status is ExecutionStatus.OK

@property
def error_type(self) -> str | None:
    return _LEGACY_ERROR_TYPE[self.execution_status]
```

旧代码读 `result.success` 语义不变（执行层未失败）。

---

## 实施范围（04.9）

| 文件 | 改动 |
|---|---|
| `powercopy/tools/result.py` | 重写：状态枚举 + ToolBusinessError + ToolOutcome + 派生属性 |
| `powercopy/tools/executor.py` | 重写：两层状态映射，区分业务异常与崩溃 |
| `powercopy/tools/builtin/policy_tools.py` | `wrap_handler` 改为抛 `ToolBusinessError`；空搜索转 `ToolOutcome(empty)` |
| `powercopy/core/trace.py` | `trace_tool` 事件增加 `execution_status` / `business_status` / `business_code` |
| `workbench/runtime_api.py` | 直接读状态字段，不再从 payload 反推 |
| `frontend/src/types/workbench.ts` | 增加 `ExecutionStatus` / `BusinessStatus` 类型 |
| `frontend/src/pages/Runtime.tsx` | 状态文案与配色区分两层；展示 `business_code` |
| `tests/test_tool_result_semantics.py` | 新增 41 项专项测试 |
| `scripts/check_tool_result_semantics.py` | 新增 11 项验收脚本 |

**未改动**：`tools/base.py`、`tools/registry.py`、`data/*`、`core/agent.py`、`core/llm.py`、`core/budget.py`。

---

## 后果

### 正面

- 模型能收到可行动的业务错误码（`POLICY_VERSION_NOT_FOUND` 而非"工具执行失败"）
- 搜索无命中不再被当作错误
- Trace 可区分"工具跑通了但业务失败"与"工具崩溃"
- `policy_tools.py` 的 `_safe_error()` 返回负载 hack 已删除

### 负面

- `ToolResult.to_dict()` 字段增加，属于契约变更
- 前端需同步类型定义

### 风险

- 已评估：`success`/`error_type` 派生属性保证旧代码与旧测试不破坏
- 全量回归 862 项通过验证

---

## 验证结果

```text
专项测试：41 passed
全量回归：862 passed（820 基线 + 41 新增 + 1 处断言更新）
验收脚本：11/11 通过
```

关键用例：

| 场景 | execution_status | business_status | business_code |
|---|---|---|---|
| 正常搜索 | ok | success | — |
| 搜索无命中 | ok | empty | NO_MATCH |
| 未知政策 | ok | failed | POLICY_VERSION_NOT_FOUND |
| offset 超界 | ok | failed | BODY_OFFSET_OUT_OF_RANGE |
| 数据库不可用 | ok | failed | POLICY_DB_NOT_FOUND |
| 参数不符 | invalid_arguments | not_applicable | — |
| handler 崩溃 | execution_error | not_applicable | — |

---

## 重新评估触发条件

1. **引入 MCP 协议** — 应改为遵循 MCP 的 `isError` 语义，而非自定义状态枚举
2. **工具数 > 20 且业务状态复杂** — 考虑把 `business_code` 收敛为受控枚举
3. **Agent 需要基于业务状态做路由** — 可能需要在 `ToolResult` 增加 `suggested_next_action` 字段
4. **多模态工具**（返回图片/文件）— `data` 类型需要更严格的约定

---

## 参考

- 复审报告：`docs/review/stage-01-04-review.md`
- 验收脚本：`scripts/check_tool_result_semantics.py`
- 专项测试：`tests/test_tool_result_semantics.py`
- [MCP 规范 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/index)（工具错误语义参考）
