# MVP-05-B 成本核算交接记录

**日期**：2026-11-27
**分支**：`feat/mvp05-cost`
**Commit**：`896d9f178eaafa0b16d1ea152916b93f419d9188`
**基线**：`239f66a`（1396 passed）
**结果**：**1471 passed**（+75 专项）

---

## 一、前提澄清

任务书写「在完成当前持久化和审批任务的基础上」，但核实后确认：
**仓库中不存在持久化与审批任务的任何提交**（`git log --all` 搜索
`persistent` / `approval` / `审批` 均为空）。

MVP-05 的「人工审批 + 审批记录留存」尚未开发，属 Sol 的前端范围。

经确认，本轮范围收敛为：**只做 cost accounting**。
成本台账独立自洽，不依赖审批模块。

---

## 二、给 Sol 的接入契约

**`docs/contracts/cost-accounting.md`** —— 含字段表、统计口径、
预算语义、前端必须做与禁止做的清单、已知限制。

### 接入代码

```python
from powercopy.cost import CostAccounting, DailyBudget, load_price_table

accounting = CostAccounting(
    "storage/cost/cost.db",
    price_table=load_price_table("config/pricing.json"),
    daily_budget=DailyBudget(max_requests_per_day=200),
    mode="live", provider="...", model="...",
)

recorder = accounting.recorder_for(task_id, "research")

response = client.invoke(
    messages,
    budget=shared_budget,
    cost_hook=recorder,
    daily_guard=accounting.guard,
)

accounting.task_outcome(task_id, final_status, drafts=1)
report = accounting.daily_report()      # DailyReport
markdown = accounting.export_markdown()  # str
```

### 三个必须提醒前端的点

1. **`total_tokens === null` 不能显示为 0** —— 那是「用量未知」，
   与「消耗 0」完全不同
2. **三档费用不可合并** —— estimated / billed / unreconciled 各有含义
3. **`avg_cost_per_draft` 为 null 时显示「—」**，不要显示 0

---

## 三、本轮修改范围

### 新增

```text
powercopy/cost/{__init__,contracts,pricing,store,daily,recorder,reconcile,ledger}.py
scripts/cost_report.py
tests/test_cost_accounting.py
docs/contracts/cost-accounting.md
docs/cost/mvp-05b-handoff.md（本文件）
config/pricing.example.json
```

### 修改

```text
powercopy/core/llm.py      invoke/invoke_structured 新增 cost_hook / daily_guard
.gitignore                 + storage/cost/、config/pricing.json
```

### 未触碰

```text
frontend/**            未修改
workbench/**           未修改
powercopy/rag/**       未修改
powercopy/tools/**     未修改
powercopy/review/**    未修改
powercopy/workflows/** 未修改
```

---

## 四、对 `LLMClient` 的改动性质

**纯增量，向后兼容**：

```python
# 旧调用方式完全不变
client.invoke(messages, budget=budget)

# 新增可选参数
client.invoke(messages, budget=budget, cost_hook=recorder, daily_guard=guard)
```

不传新参数 → 行为与之前完全一致，不写台账，不检查每日预算。
**`powercopy/core` 保持零依赖**：不在 core 层 import cost 模块。

已有 1396 项测试全部通过，证明兼容性。

---

## 五、需要协调的事项

### 协调项 1：单价配置

`config/pricing.json` **已在 .gitignore 中**（真实价格不入公开仓库）。

需要运维从服务商官网核实后本地填写。格式见
`config/pricing.example.json`（值全部为 `0.00` 并标注未核实）。

**未配置单价时，所有费用记为 `unknown`，不填零、不套用其他平台价格。**
这是刻意设计，不是 bug。

### 协调项 2：研发 vs 业务用量

本轮的研发过程本身如果调用了模型，会被 `CallRecorder` 记录。

**当前实现未自动区分**。若要看板分开，需靠：
- `task_id` 前缀约定（如 `dev-*`）
- 或 `stage` 标记
- 或在账单 CSV 中单独一批 `billed_ref`（如 `bill-dev-*`）

已在契约文档「第十节」注明。

### 协调项 3：workbench 是否需要暴露成本 API

若需要，建议新增只读端点：

```text
GET /api/workbench/cost/report?day=YYYY-MM-DD   → DailyReport.to_dict()
GET /api/workbench/cost/calls?day=YYYY-MM-DD    → 调用明细
GET /api/workbench/cost/reconciliation          → 对账总览
```

**本轮未实现**（属 workbench 范围，Sol 负责）。

---

## 六、数据库

```text
storage/cost/cost.db     ← 已 gitignore
```

表：

```sql
model_calls        -- 请求明细，主键 request_id（幂等）
reconciliations    -- 对账批次
task_outcomes      -- 任务结果
schema_meta        -- 版本
```

金额全存 TEXT（Decimal 字符串），避免 SQLite REAL 浮点误差。

---

## 七、合并建议

1. `powercopy/core/llm.py` 的改动是**纯增量**，与 Sol 的工作台改动无冲突
2. 唯一潜在冲突是 `.gitignore`（本轮新增两行）
3. 合并后重跑全量回归（本分支 1471 passed）
4. 合并后由集成方统一更新共享状态文档

---

## 八、本轮未做的事

- 不引入监控框架（Prometheus / Grafana 等）
- 不调用付费模型验证
- 不开发前端费用看板
- 不修改 workbench
- 不实现研发/业务用量自动分离
- 未修改共享状态文档（PROJECT_STATE / CHANGELOG / roadmap / README）
