# MVP-04-B 业务审核与工作流集成报告

**日期**：2026-11-27
**分支**：`feat/mvp04-review`
**基线**：`6b29cf1e6a49933e038c5a3e5a8269aba8ee52ce`（1301 passed）
**结果**：**1363 passed**（+62 专项）

---

## 一、TaskBudget 修复（源码审查问题）

### 审核发现的问题（已确认成立）

| 位置 | 问题 |
|---|---|
| `research/agent.py` 690-697 | 外部手动 `reserve_api_attempt()` 后调用 `self.llm.invoke(messages)`，**未传 budget** |
| `copywriting/agent.py` 396-398 | 同上 |
| `scripts/generate_copy.py` | 研究阶段拿满额预算，创作阶段额外再拿一半 |

**后果**：`LLMClient._execute` 内部本身就会 `reserve_api_attempt()` 与
`record_usage()`。外部不传 budget 意味着：

1. **内部每次真实 API 请求（含重试）完全不受预算约束**
2. 外部那次预占与内部计数各记一笔，**账目对不上**
3. 整个业务任务的实际上限是两阶段之和，与用户设定不符

### 修复

**新增 `powercopy/core/shared_budget.py`**

```python
budget = SharedBudget(max_api_attempts=12, max_known_tokens=40_000)
# 四阶段共用同一个实例
```

`SharedBudget` 包装 `TaskBudget`，额外提供：
- `exhausted_by` —— 记录是哪个维度触发
- `to_dict()` —— 供报告与前端展示
- 委托 `stop_reason` / `check` / `reserve_api_attempt` / `record_usage`

**调用侧改为传递 budget**

```python
# research/agent.py
response = self.llm.invoke(messages, budget=budget)

# copywriting/agent.py
response = self.llm.invoke(messages, budget=budget)
```

**移除重复计数**

- `research/agent.py`：删除外部 `reserve_api_attempt()` 与 `record_usage()`
- `copywriting/agent.py`：`_extract_usage()` 只读快照，不再调用 `record_usage()`

**Mock 与真实模型行为一致**

两个 Mock 的 `invoke` 现在都处理 `budget` 参数：

```python
budget = kwargs.get("budget")
if budget is not None:
    budget.reserve_api_attempt()
...
if budget is not None:
    budget.record_usage(0)   # Mock 的 token 消耗为 0，但 attempt 是真实的
```

这样 Mock 模式下也能验证预算耗尽，而不是"只测真实模式"。

**`ResearchAgent` 支持 `shared_budget`**

```python
ResearchAgent(..., shared_budget=budget)   # 复用外部预算，不再另建
```

### Token 上限的性质（不虚假承诺）

| 上限 | 性质 | 说明 |
|---|---|---|
| `max_api_attempts` | **硬约束** | 每次真实 API 请求（含重试）预占一个名额，超出即抛 `BudgetExceeded` |
| `max_known_tokens` | **软预算** | 只在模型返回 `usage` 时可计量；不返回时只能标记 `usage_incomplete=True`，**无法阻止**实际消耗继续增长 |

`budget_snapshot` 中明确写出 `token_limit_is_soft: true` 和说明文字。

---

## 二、Review 模块

### 四维审核能力对照

| 维度 | 程序可判定程度 | 实现方式 |
|---|---|---|
| **引用完整性** | ✅ 完全可判 | 证据 ID 存在性、claim 引用关系、`evidence_refs` 一致性、正文未知 ID |
| **事实支持性** | ⚠️ 部分 | 字面回溯（数字/条款是否在原文）+ 可选 LLM 语义判断 |
| **宣传合规** | ⚠️ 字面匹配 | 复用 `copywriting.guards`，不重复实现 |
| **政策适用性** | ✅ 部分 | 地区一致性、时效状态、免责声明；**现行有效性不可判** |

### 事实支持性：本模块最需要克制的地方

任务书要求「检查标题和完整正文中的事实性陈述，而不仅是模型自行列出的
key_points」。实现分两层：

**程序可做的（可靠）**：字面回溯

对每条事实性陈述，提取其中的数字与条款编号，检查是否在证据原文中
出现。找不到的标 `UNSUPPORTED_LITERAL_ANCHOR`（major）。这是可靠信号
—— 数字凭空出现基本可以确定没有依据。

**程序不能做的（交给 LLM 或人工）**：语义支持性

- 无 LLM：`verdict = pending_review`，明确说明「未做语义审核」
- 有 LLM：模型逐条给出 `supported` / `contradicted` / `insufficient`
  - `contradicted` → blocking
  - `insufficient` → major
  - 全部 supported → `MACHINE_SUPPORTED`，**明确标注「这是机器审核意见，不代表专家确认」**

**修复的一个真实缺陷**：初版在 Mock 返回空 verdicts 时仍报 `machine_supported`，
把「模型无法判断」伪装成「机器认为支持」。已修正为：`llm_judged == 0` 时
保持 `pending_review`。

### 宣传合规：复用而非重写

`check_compliance` 直接调用 `check_forbidden_content`，
只把 `GuardReport` 的 findings 映射成 `ReviewIssue`。
测试 `test_reuses_guards_not_reimplement` 用 `inspect.getsource` 断言
模块内确实引用了该函数。

### 政策适用性

| 检查 | 严重度 |
|---|---|
| 证据包限定地区但文案未声明 | major |
| 文案声称适用范围但证据包无地区 | major |
| 时效状态不是 `latest_observed` | major |
| 文案声称「现行有效」 | **blocking** |
| 缺少时效免责声明 | minor |

**修复的一个真实缺陷**：初版把「提到地区名」等同于「声称适用范围」，
导致引文里的「全国统一大市场」被误判。已改为只在出现
「适用于 / 适用范围 / 本政策适用」等限定语时才构成地区主张。

**修复的另一个缺陷**：否定式表述「不代表该政策现行有效」被误判为
声称现行有效。已加入上下文否定词检测（`不代表` / `不表示` / `需以` /
`以官方公告为准` 等）。

---

## 三、有限自动修订

### 停止条件（关键设计）

判定标准不是严重度，而是**「这个问题能不能靠改写文案修好」**：

| 情况 | 能否改写修复 | 处理 |
|---|---|---|
| 引用不存在的证据 | ❌ 根因在上游 | 立即停止 |
| 引用被拒 claim 的证据 | ❌ 同上 | 立即停止 |
| 宣传违规（节省承诺等） | ✅ 删除该句 | 尝试修订 |
| 绝对化措辞 / 缺免责 | ✅ 改写 | 尝试修订 |
| 事实与证据矛盾 | ✅ 删改该陈述 | 尝试修订 |

实现上通过 `ReviewIssue.auto_fixable` 区分：citation 维度的引用类问题
全部标 `auto_fixable=False`（根因在上游），其余问题标 `True`。

### 硬性约束

- **默认最多 1 次**（`max_revisions=1`）
- 契约层 `RevisionResult.attempt` 限制 `le=3`
- 构造函数校验 `max_revisions <= 3`
- 修订后**必须重新审核**（`post_report = self.review(new_draft)`）
- 修订后出现 blocking 或无可修问题 → 停止

**禁止无限循环**：`for attempt in range(1, self.max_revisions + 1)`，
测试 `test_no_infinite_loop` 用「不改的修订器」验证循环会终止。

### 记录内容

`RevisionResult` 保留：
- `before_title` / `before_body` vs `after_title` / `after_body`
- `addressed_issue_ids` / `remaining_issue_ids`
- `post_review_status` / `post_review_blocking`
- `changed` 标志

---

## 四、统一业务入口

### `powercopy/workflows/content.py`

```python
service = ContentWorkflowService(corpus, registry, model_name="mock")
result = service.run(ContentWorkflowRequest(...))
```

四阶段串联，**共享一个 `SharedBudget`**。

### 终态设计（没有任何值表示可发布）

| 值 | 含义 |
|---|---|
| `draft_ready` | 已生成草稿，未审核（`enable_review=false`） |
| `pending_review` | 已审核，需人工确认 —— **正常终态** |
| `needs_manual_review` | 阻断级问题或修订后仍不通过 |
| `aborted` | 证据不足，主动停止 |
| `budget_exhausted` | 预算耗尽 |
| `failed` | 程序错误 |

### 契约已提前交给 Sol

`docs/contracts/content-workflow.md` —— 含完整字段表、状态语义、
前端集成要点、禁止的 UI 做法、最小接入示例。

**关键提醒给前端**：
- `pending_review` 不得渲染成「成功/已发布」
- `fact_support_llm_used=True` 时必须标注「机器意见」
- 四维审核不要合并成单一通过率

---

## 五、验收结果

用真实政策库（2 份政策 / 26 切片）+ Mock，实测：

```text
task_id       : task-4db8d54bf6c741b6
最终状态      : pending_review（待人工审核）
需人工处理    : True

阶段一 研究：问题 5 / 证据 14 / 原文核验通过 25 / 人工确认 0
阶段二 文案：767/800 字 / 5 条要点全部绑定证据
阶段三 审核：citation=passed  fact_support=pending_review
             compliance=passed  applicability=passed
阶段四 修订：未触发（0 次）

预算：12 次上限，已用 6 次，剩余 6 次
阶段耗时：research 10052ms / draft 2.7ms / review 2.9ms
```

### 7 项验收场景（均有测试）

| # | 场景 | 测试 | 结果 |
|---|---|---|---|
| 1 | 正常生成 | `test_scenario_1_normal` | ✅ |
| 2 | 正文含无依据事实 | `test_scenario_2_body_with_unsupported_fact` | ✅ 字面回溯捕获 |
| 3 | 真实引文却不支持结论 | `test_scenario_3_quote_not_supporting_conclusion` | ✅ LLM 判 contradicted → blocking |
| 4 | 地区或时效不明确 | `test_scenario_4_region_or_validity_unclear` | ✅ |
| 5 | 禁用宣传内容 | `test_scenario_5_forbidden_promotion` | ✅ blocking |
| 6 | 修订后仍未通过 | `test_scenario_6_revision_still_fails` | ✅ 记录完整 |
| 7 | 全流程预算耗尽 | `test_scenario_7_budget_exhausted` | ✅ |

**未调用真实付费模型**。

---

## 六、测试

```text
专项（test_review_workflow.py）：62 passed
全量回归：1363 passed（1301 + 62）
```

| 组 | 数量 | 内容 |
|---|---|---|
| TaskBudget | 5 | 防重复计数、耗尽记录、软硬约束声明、上下文管理 |
| 引用完整性 | 5 | 有效、未知证据、被拒 claim、refs 不一致、正文未知 ID |
| 事实支持性 | 6 | 无 LLM 保持 pending、空 verdict 保持 pending、supported、contradicted、字面回溯、检查正文 |
| 宣传合规 | 7 | 干净内容、5 类违规、复用 guards 断言 |
| 政策适用性 | 6 | 一致通过、声称有效 blocking、地区主张、仅提及不算主张、时效非最新、缺免责 |
| 报告契约 | 4 | passed≠可发布、分维度统计、人工确认恒 False、LLM 标志 |
| 自动修订 | 7 | 无模型跳过、修订并重审、上限校验、blocking 跳过、无死循环、前后记录 |
| 统一工作流 | 7 | 正常、无发布状态、跳过审核、预算共享、耗尽停止、JSON 往返、分层统计 |
| 请求契约 | 5 | 路径字段拒绝、空主题、字符串 platform、修订上限 |
| 验收场景 | 7 | 见上表 |

---

## 七、实际修改文件

### 新增

```text
powercopy/core/shared_budget.py            共享预算
powercopy/review/{__init__,contracts,verifier,agent,mock}.py
powercopy/workflows/{__init__,content}.py  统一入口
scripts/run_content_workflow.py            统一 CLI
tests/test_review_workflow.py              62 项
docs/contracts/content-workflow.md         供 Sol 接入的契约
docs/review/mvp-04b-report.md              本文件
docs/review/mvp-04b-handoff.md             交接记录
```

### 修改

```text
powercopy/research/agent.py       传 budget / 移除重复计数 / shared_budget 参数
powercopy/research/mock.py        invoke 支持 budget
powercopy/copywriting/agent.py    传 budget / 移除重复计数
powercopy/copywriting/mock.py     invoke 支持 budget
.gitignore                        + storage/workflow/
```

---

## 八、运行命令

```powershell
# 完整四阶段（Mock，无费用）
python scripts/run_content_workflow.py

# 只跑 research → draft
python scripts/run_content_workflow.py --no-review

# 输出完整 JSON
python scripts/run_content_workflow.py --json storage/workflow/out.json

# 真实 LLM（三重门禁）
$env:POWERCOPY_ALLOW_LIVE="1"
python scripts/run_content_workflow.py --live --confirm-live

# 专项测试
python -m pytest tests/test_review_workflow.py -q

# 全量回归
python -m pytest tests -q
```

---

## 九、已知限制

| 限制 | 说明 |
|---|---|
| **事实支持性程序无法判定** | 只能字面回溯 + 可选 LLM；LLM 结论是机器意见 |
| **人工确认通道未实现** | `human_confirmation_available` 恒为 False |
| 宣传合规是字面匹配 | 无法识别语义等价改写、断章取义、暗示性承诺 |
| **现行有效性不可判** | 一律标记待人工确认官方公告 |
| 自动修订只改表述 | 不动引用映射，不引入新事实；根因在上游的问题不修 |
| 修订次数上限 | 默认 1 次，硬上限 3 次 |
| Token 软预算 | 模型不返回 usage 时无法阻止实际消耗 |
| 语料仅 2 政策 / 26 切片 | 主题覆盖有限 |
| **真实 LLM 路径未实际执行** | 未获付费授权 |

---

## 十、交付边界

**已完成**：四维审核 + 有限自动修订 + 统一业务入口 + CLI + 契约。

**未做**：
- 不开发前端
- 不修改 `workbench/`
- 不建设 GraphRAG / Memory / Skills / 多 Agent Supervisor
- 未调用付费 LLM
