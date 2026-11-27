# Token 计费与成本控制契约

**版本**：`cost-accounting-v1`
**日期**：2026-11-27
**用途**：供 Sol 开发费用看板（每日进展 / 业务产出 / Token 消耗 / API 费用）

---

## 一、模块结构

```text
powercopy/cost/
├── contracts.py   数据契约（含 Usage / PriceRule / DailyReport）
├── pricing.py     单价表与费用计算（Decimal）
├── store.py       SQLite 台账
├── daily.py       每日预算守卫 + 日报 + 导出
├── recorder.py    把 LLMClient 请求写入台账
├── reconcile.py   账单 CSV 导入 / 人工录入对账
└── ledger.py      门面 CostAccounting
```

**与既有模块的关系**：

```text
LLMClient.invoke(..., cost_hook=recorder, daily_guard=guard)
                              │              │
                              ▼              ▼
                     CallRecorder      DailyBudgetGuard
                              │              │
                              └──────┬───────┘
                                     ▼
                                 CostStore (SQLite)
                                     │
                                     ▼
                              build_daily_report()
```

**`powercopy/core` 保持零依赖**：`cost_hook` / `daily_guard` 都是可选回调，
不传就完全不记录。这样测试和离线脚本不必建库。

---

## 二、数据契约

### 2.1 `Usage`（用量）

```python
{
  "input_tokens": 1000,      # int | null
  "output_tokens": 500,      # int | null
  "cached_tokens": null,     # int | null
  "total_tokens": 1500,      # int | null
  "complete": true           # bool
}
```

**关键语义**：

| 情况 | `total_tokens` | `complete` | 含义 |
|---|---|---|---|
| 服务商返回完整用量 | 1500 | `true` | 可精确计价 |
| 请求失败，用量未知 | **`null`** | `false` | **不知道消耗了多少** |
| 明确知道消耗为 0 | `0` | `true` | 真的是 0 |

⚠️ **`null` 与 `0` 绝不可混淆。** 前端必须区分显示：
- `total_tokens === null` → 显示「用量未知」，不要显示 0
- `total_tokens === 0` → 显示 0

### 2.2 `ModelCallRecord`（调用明细）

一行 = 一次**实际 API 请求尝试**。一次 `invoke()` 内部重试 3 次就产生 3 行。

| 字段 | 类型 | 说明 |
|---|---|---|
| `request_id` | str | `req-<20hex>`，唯一 |
| `task_id` | str | 业务任务 ID |
| `call_index` | int | 同一 task 内第几次请求（从 1 起） |
| `occurred_at` | str | ISO 8601 **UTC** |
| `stage` | str | research / copywriting / review / revision |
| `provider` / `model` | str | 实际服务商与实际路由到的模型 |
| `usage` | Usage | 见上 |
| `outcome` | enum | success / api_error / rate_limited / quota_exceeded / output_invalid / budget_blocked |
| `retry_index` | int | 本次 invoke 内第几次重试（0 起） |
| `latency_ms` | float | 耗时 |
| `price_version` | str | 单价版本（价格调整后可追溯） |
| `input_price_per_million` / `output_price_per_million` | Decimal | 当时使用的单价 |
| `currency` | str | 默认 CNY |
| `estimated_cost` | Decimal \| null | 按单价表估算 |
| `billed_cost` | Decimal \| null | 服务商账单金额 |
| `cost_status` | enum | 见下 |
| `mode` | str | mock / live |
| `billed_ref` | str | 对账批次号 |
| `prompt_chars` / `prompt_sha256` | int / str | **只存摘要，不存原文** |

### 2.3 `CostStatus`（费用状态）

| 值 | 含义 | 前端展示 |
|---|---|---|
| `estimated` | Mock 或已估算 | — |
| `unreconciled` | 有估算，尚无账单 | 待对账 |
| `billed` | 已对账（有账单金额） | 已对账 |
| `unknown` | 无法计算（缺单价或缺用量） | **费用未知** |

**`unknown` 不是错误**，是「数据不足」的如实表达。

### 2.4 `DailyBudget`（每日预算）

```python
{
  "max_requests_per_day": 200,
  "max_tokens_per_day": 2000000,
  "warn_at_requests": 0.8,
  "warn_at_tokens": 0.8,
  "daily_cost_ceiling": null     # Decimal | null
}
```

### 2.5 `DailyReport`（日报）

```python
{
  "day": "2026-11-27",
  "tasks_started": 3,
  "tasks_succeeded": 2,
  "tasks_failed": 1,
  "tasks_partial": 0,
  "drafts_generated": 2,
  "requests_total": 18,
  "requests_success": 16,
  "requests_failed": 2,
  "tokens_known_total": 45230,
  "tokens_unknown_requests": 2,
  "tokens_by_stage": {"research": 20100, "copywriting": 25130},
  "estimated_cost": "0.42",
  "billed_cost": "0.00",
  "unreconciled_cost": "0.42",
  "unknown_cost_requests": 2,
  "avg_cost_per_draft": "0.210000",
  "currency": "CNY",
  "generated_at": "2026-11-27T05:00:00+00:00",
  "warnings": ["2 次请求未返回用量..."]
}
```

---

## 三、统计口径（必须遵守）

### 3.1 已知 Token vs 未知用量

```text
tokens_known_total      只累加 usage.complete === true 的请求
tokens_unknown_requests 只计数，不参与 token 汇总
```

**绝不把未知用量按 0 计入 `tokens_known_total`。**
否则日报会系统性低估消耗。

### 3.2 三档费用

```text
estimated_cost  按内部单价表估算的总额
billed_cost     已导入账单的总额
unreconciled     有估算但无账单的部分
```

三者**不相互替代**。`estimated` 与 `billed` 的差额即 `variance`。

### 3.3 按北京时间切日

所有时间以 **UTC** 存储，按 **UTC+8** 切日。
`to_beijing_day("2026-11-27T20:00:00+00:00")` → `"2026-11-28"`。

> 实现上用固定 `+08:00` 偏移而非 `zoneinfo`：Windows 默认无 IANA 时区库，
> 且中国自 1991 年起不实行夏令时，固定偏移既正确又无依赖。

### 3.4 平均单篇成本

```text
avg_cost_per_draft = 已确认费用 / 当日生成文案数
```

无文案时为 `null`（不是 0）。

---

## 四、预算语义（不可虚假承诺）

| 预算 | 性质 | 保证 |
|---|---|---|
| 单任务 `max_api_attempts` | **硬约束** | 每次请求（含重试）预占，超出抛异常 |
| 单任务 `max_known_tokens` | 软预算 | 仅能计量已知部分 |
| 每日 `max_requests_per_day` | **硬约束** | 请求前检查，超限拒绝 |
| 每日 `max_tokens_per_day` | 软预算 | 仅能计量已知部分 |

**Token 为什么是软预算：**

1. 只有服务商返回 `usage` 时才能累计
2. 请求失败那次的消耗是**未知的**，无法参与超支判定
3. 因此「已知 Token」永远可能低于真实消耗

前端**不得**展示「Token 一定不会超出」这类表述。
日报的 `warnings` 会在有未知用量时明确提示。

### 并发与重试

- 重试**计入**每日请求数（服务商也按请求计费）
- 每日检查是**乐观检查**：先读当日计数，超限则拒绝，请求后写记录。
  并发窗口内可能多发 1-2 个请求，由单任务硬上限兜底。
  **不声称这是严格的事务级约束。**

---

## 五、接入方式

### 5.1 在业务流程中启用

```python
from powercopy.cost import CostAccounting, DailyBudget, load_price_table

accounting = CostAccounting(
    "storage/cost/cost.db",
    price_table=load_price_table("config/pricing.json"),
    daily_budget=DailyBudget(max_requests_per_day=200),
    mode="live",
    provider="your-provider",
    model="your-model",
)

# 每个任务一个记录器，按阶段派生
recorder = accounting.recorder_for(task_id, "research")

response = client.invoke(
    messages,
    budget=shared_budget,
    cost_hook=recorder,
    daily_guard=accounting.guard,
)

# 任务结束时登记结果
accounting.task_outcome(task_id, final_status, drafts=1)
```

### 5.2 不传参数 = 完全不记录

```python
client.invoke(messages)   # 不写台账，不检查每日预算
```

---

## 六、单价配置

`config/pricing.json`（**不提交真实价格到公开仓库**）：

```json
{
  "currency": "CNY",
  "rules": [
    {
      "provider": "服务商标识",
      "model": "模型标识",
      "input_per_million": "1.00",
      "output_per_million": "2.00",
      "cached_input_per_million": "0.50",
      "price_version": "v1",
      "effective_from": "2026-11-01T00:00:00+00:00",
      "source": "服务商官网定价页 URL + 核实日期"
    }
  ]
}
```

**单位：元 / 百万 Token。**

**未知价格的处理**：`PriceTable.lookup()` 返回 `None` →
`cost_status = unknown` → 日报里单独计 `unknown_cost_requests`。
**绝不回退到其他平台或同厂商其他模型的单价。**

`config/pricing.example.json` 只有格式占位，值为 `0.00` 并标注未核实。

---

## 七、对账

### 7.1 CSV 格式

最小必需列：

```csv
request_id,amount
req-abc123,0.0123
```

- 金额列名可用：`amount` / `cost` / `billed_cost` / `金额` / `费用`
- 请求 ID 列名可用：`request_id` / `id` / `请求ID`
- 可选：`currency`、`note`
- 负金额拒绝（退款需单独记录）

### 7.2 对账命令

```powershell
python scripts/cost_report.py --import-bill bill-2026-11.csv
python scripts/cost_report.py --manual-bill 12.34 --note "服务商后台核对"
python scripts/cost_report.py --reconciliation
```

**对账不会覆盖 `estimated_cost`**，只写入 `billed_cost` 并改状态为 `billed`。
这样才能计算 `variance`。

---

## 八、日报导出

```powershell
python scripts/cost_report.py                              # 今日 Markdown
python scripts/cost_report.py --day 2026-11-27 --format csv
python scripts/cost_report.py --day 2026-11-27 --detail    # 调用明细 CSV
python scripts/cost_report.py --list-days
```

**导出内容不含提示词原文与任何密钥。** 明细 CSV 只有 `prompt_chars`
与 `prompt_sha256`。

---

## 九、前端集成要点

### 必须展示

1. **`tokens_unknown_requests`** —— 与 `tokens_known_total` 并列，
   不得只显示后者就下结论
2. **三档费用分开** —— 预计 / 已对账 / 未对账
3. **`unknown_cost_requests`** —— 说明有多少次请求无法计价
4. **`warnings` 数组** —— 原样展示
5. **`avg_cost_per_draft` 为 `null` 时显示「—」**，不要显示 0

### 禁止的 UI 做法

| 做法 | 问题 |
|---|---|
| 把 `total_tokens === null` 显示为 0 | 掩盖未知消耗 |
| 把三档费用合并成「总费用」 | 掩盖估算与账单的差异 |
| 展示「Token 不会超支」 | 与软预算事实不符 |
| 把 `cost_status === 'unknown'` 显示为 0 元 | 误导为免费 |
| 无单价时自动套用其他平台价格 | 编造成本数据 |
| 在页面暴露提示词原文 | 可能含业务信息 |

---

## 十、研发模型消耗与业务模型分离

**研发阶段自己调用模型（如本轮的代码生成、测试）不算业务用量。**

处理方式：
- 研发消耗通过独立的 `--manual-bill` 录入，`source` 标记为 `dev`
- 或在账单 CSV 中单独一批 `billed_ref`（如 `bill-dev-*`）
- 日报可通过 `source` 过滤

**当前实现未自动区分**：`CallRecorder` 记录所有经过 `LLMClient` 的请求。
若需区分，靠 `task_id` 前缀或 `stage` 标记，由调用方约定。

---

## 十一、已知限制

| 限制 | 说明 |
|---|---|
| Token 是软预算 | 无法保证绝对不超支 |
| 每日检查是乐观的 | 并发窗口内可能多发 1-2 个请求 |
| 未内置任何真实单价 | 必须由运维从官网核实后配置 |
| 不依赖第三方账单 API | 只支持 CSV 与手工录入 |
| 研发/业务用量未自动区分 | 靠 `task_id` / `stage` 约定 |
| 账单无法分摊到单次调用时 | 只记批次总额（`line_count` 仍记录条数） |
| 未做跨月/跨年归档 | 当前按日查询，数据不清理 |
| 未验证前端集成 | 本轮不涉及前端 |

---

## 十二、数据库表

```sql
model_calls        -- 请求明细（主键 request_id，幂等写入）
reconciliations    -- 对账批次
task_outcomes      -- 任务结果（用于日报任务统计）
schema_meta        -- schema 版本
```

**存放位置约定**：`storage/cost/cost.db`（已在 `.gitignore` 中）。

**金额全存 TEXT**（Decimal 字符串），避免 SQLite REAL 的浮点误差。
