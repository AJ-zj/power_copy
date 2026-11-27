# 首篇真实文案实验方案

**日期**：2026-11-27
**基线**：`feat/mvp04-review` @ `54ad1c9`
**状态**：✅ 前置核实完成，等待执行授权

---

## 一、三项核实结果

### 1.1 共享 TaskBudget 贯穿性 —— 通过

用 AST 精确分析（不受注释与文档字符串干扰）：

| 文件 | llm.invoke | budget 传参 | 外部 reserve | 外部 record_usage |
|---|---|---|---|---|
| `research/agent.py:705` | 1 处 | ✅ 有 | 无 | 无 |
| `copywriting/agent.py:413` | 1 处 | ✅ 有 | 无 | 无 |
| `review/agent.py:328` | 1 处 | ✅ 有 | 无 | 无 |
| `workflows/content.py` | 0 处 | — | 无 | 无 |

**`LLMClient` 内部统一负责**（`llm.py`）：

```text
L183: budget.reserve_api_attempt()   ← 每次真实请求（含重试）都预占
L208: budget.record_usage(tokens)    ← 成功时累计
L273: budget.record_usage(None)      ← API 失败时标记 usage 不完整
```

**`SharedBudget` 在 `content.py` 只创建 1 次**，通过 `shared_budget=budget`
传给 `ResearchAgent`、通过 `budget=budget` 传给 `ReviewAgent`。

**单次 `invoke` 最多消耗 3 次 attempt**（`RetryPolicy.max_attempts=3`）。

### 1.2 旧脚本 `generate_copy.py` —— 发现并修复

**问题确认**：

```python
# 修复前
budget=CopywritingBudget(
    max_api_attempts=max(1, args.max_api_attempts // 2),      # 文案另拿一半
    max_known_tokens=max(500, args.max_known_tokens // 2),
)
research_agent = ResearchAgent(..., budget=research_request.task_budget)
# ← 没有 shared_budget，两阶段各自独立预算
```

后果：整个任务的实际上限是「研究满额 + 文案一半」，与用户设定的单次预算不符。

**修复**：

- 新增 `SharedBudget`，两阶段共用
- `ResearchAgent(..., shared_budget=shared_budget)`
- 文案预算改为按契约上限封顶（`min(..., 20)` / `min(..., 100_000)`），不再减半

**验证**：Mock 模式下预算耗尽能正确停止；新增 5 项测试覆盖。

### 1.3 真实 LLM 接口与模型配置 —— 兼容

| 项 | 值 |
|---|---|
| 模型 | `glm-5.3-flash` |
| base_url | `https://www.yituohub.si/v1/` |
| timeout | 60s（可用 `LLM_TIMEOUT` 覆盖） |
| `invoke` 签名 | 支持 `budget` 关键字 ✅ |
| `invoke_structured` | 支持 `budget` ✅ |
| `SharedBudget` 接口 | 满足 `_execute` 所需的 `reserve_api_attempt` / `record_usage` / `check` ✅ |
| 返回字段 | `content` / `model` / `input_tokens` / `output_tokens` / `total_tokens` / `retry_count` / `usage_incomplete` ✅ |

**未发起任何付费请求**（本节仅核实接口与配置）。

---

## 二、实验配置

| 项 | 值 |
|---|---|
| 主题 | 《工商业企业参与电力市场前，需要了解哪些基本规则？》 |
| 目标客户 | 工商业用电企业 |
| 平台 | 微信公众号（`wechat_mp`） |
| 字数 | 800（验收区间 600–800） |
| 风格 | 专业、通俗、可信 |
| 地区 | 不指定（避免触发地方政策缺口） |
| 自动修订 | 最多 1 次 |

---

## 三、实验命令

```powershell
# ---- 必做：显式授权 ----
$env:POWERCOPY_ALLOW_LIVE = "1"
$env:LLM_TIMEOUT = "180"          # 实测 60s 偏短，网络波动时会超时

# ---- 执行 ----
cd D:\桌面\昆吾电力项目\PowerCopy-V2-mvp04

python scripts/run_content_workflow.py --live --confirm-live `
  --topic "工商业企业参与电力市场前，需要了解哪些基本规则？" `
  --audience "工商业用电企业" `
  --region "" `
  --platform wechat_mp `
  --target-chars 800 `
  --style "专业、通俗、可信" `
  --purpose "为工商业用电企业提供政策科普" `
  --max-questions 2 `
  --max-tool-calls 3 `
  --max-api-attempts 12 `
  --max-known-tokens 60000 `
  --max-revisions 1 `
  --json storage/workflow/first-article.json
```

### 参数说明

| 参数 | 值 | 理由 |
|---|---|---|
| `--max-questions` | 2 | 单篇 600–800 字文案，2 个研究问题足够；问题越多耗时越长 |
| `--max-tool-calls` | 3 | 每个问题 1 次检索 + 余量 |
| `--max-api-attempts` | 12 | 见下方预算测算 |
| `--max-known-tokens` | 60000 | 软预算，见下 |
| `--max-revisions` | 1 | 任务书要求 |

---

## 四、预算与停止条件

### 4.1 请求次数（硬约束）

**实测单次完整流程的 invoke 次数**（上一轮真实运行）：

| 阶段 | invoke 次数 | 说明 |
|---|---|---|
| 研究（规划） | 1 | 拆解问题 |
| 研究（提取） | 1–2 | 每问题 1 次 |
| 文案生成 | 1 | 单次结构化调用 |
| 审核（语义判断） | 1 | 逐条判断事实支持性 |
| 修订（如触发） | 1 | 最多 1 次 |
| **合计** | **5–7** | |

**注意**：单次 `invoke` 内部最多重试 3 次（`RetryPolicy.max_attempts=3`）。
若网络不稳定，一次 invoke 可能消耗 2–3 次 attempt。

因此设定 **`--max-api-attempts 12`**：

- 正常情况：消耗 5–7 次，余量 5–7 次
- 网络抖动：可容忍 2–3 次 invoke 各自重试一次
- 超出即停止，不会无限消耗

### 4.2 Token（软预算）

**实测 token 消耗**（上一轮）：

| 项 | 值 |
|---|---|
| 研究阶段 | 约 2,300–2,600 |
| 文案阶段 | 约 2,500–2,800 |
| 审核阶段 | 约 500–1,500 |
| **合计** | **约 5,000–7,000** |

设定 **`--max-known-tokens 60000`** 约为实测值的 8–10 倍，留有充足余量。

**重要：Token 是软预算**。只有当模型返回 `usage` 时才能累计。
若出现 API 失败，该次用量未知，`usage_incomplete` 会被置为 `True`，
此时**无法阻止**实际消耗继续增长。这是已如实声明的限制，不作「绝对不会超」的承诺。

### 4.3 预估费用

`glm-5.3-flash` 的实际单价需以平台计费页为准（本环境未核实到确切定价），
因此**不给出具体金额**，只给量级：

```text
单次完整实验：约 5,000–7,000 tokens
即使按较高的 0.01 元/千 token 估算，单次成本也在 0.1 元以内
```

如需精确金额，需项目负责人提供该模型的计费单价。

### 4.4 停止条件

流程会在以下任一情况自动停止并产出明确状态：

| 触发条件 | final_status | 说明 |
|---|---|---|
| API attempt 达到 12 | `budget_exhausted` | 硬约束，立即停止 |
| 已知 token 达到 60000 | `budget_exhausted` | 软预算，仅在能计量时生效 |
| 证据不足（无可用 claim） | `aborted` | 不编造 |
| 模型声明证据不足 | `aborted` | 尊重模型判断 |
| 引用校验失败（blocking） | `needs_manual_review` | 根因在上游，不尝试改写掩盖 |
| 修订后仍有 blocking | `needs_manual_review` | 已达修订上限 |
| 模型调用异常 | `failed` | 优雅失败，不崩溃 |
| 正常完成 | `pending_review` | **仍需人工审核** |

**没有任何一个状态表示「可自动发布」。**

### 4.5 额外的人工中止手段

若实验过程中发现异常消耗：

```powershell
# 方式一：Ctrl+C 直接中断
# 方式二：把 --max-api-attempts 改小后重跑
# 方式三：不设置 POWERCOPY_ALLOW_LIVE，脚本立即拒绝执行
```

---

## 五、执行前检查清单

- [x] 共享预算贯穿四阶段（AST 核实）
- [x] 旧脚本独立预算已修复
- [x] 真实接口与模型配置兼容
- [x] 全量回归 1389 passed
- [x] 敏感信息扫描通过
- [ ] **项目负责人确认执行**（本轮任务书第 7 条）
- [ ] 授权后执行并记录实际消耗

---

## 六、预期产出

执行成功后会得到：

1. `storage/workflow/first-article.json` —— 完整结构化结果，含：
   - `research`：EvidencePackage（问题、claims、evidence）
   - `draft`：ContentDraft（标题、正文、要点、证据映射、待审核事项）
   - `review`：ReviewReport（四维审核结果）
   - `revision`：RevisionResult（如触发）
   - `budget_snapshot`：实际消耗
2. 终端输出的可读报告
3. 每篇文案的事实要点与证据 ID 逐一对应，可追溯

**首篇文案必定带有 `pending_review` 状态，必须经过人工验收表才能使用。**
