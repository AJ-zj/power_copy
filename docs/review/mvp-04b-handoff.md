# MVP-04-B 交接记录（与 MVP-01-F / Sol 并行）

**日期**：2026-11-27
**基线提交**：`6b29cf1e6a49933e038c5a3e5a8269aba8ee52ce`
**分支**：`feat/mvp04-review`
**Worktree**：`D:\桌面\昆吾电力项目\PowerCopy-V2-mvp04`

---

## 一、并行开发约定

Sol 负责前端与 workbench API。本轮**未修改**：

```text
frontend/**          未修改
workbench/**         未修改
powercopy/rag/**     未修改
powercopy/tools/**   未修改
```

### 本轮修改的文件

```text
新增：
  powercopy/core/shared_budget.py
  powercopy/review/{__init__,contracts,verifier,agent,mock}.py
  powercopy/workflows/{__init__,content}.py
  scripts/run_content_workflow.py
  tests/test_review_workflow.py
  docs/contracts/content-workflow.md
  docs/review/mvp-04b-{report,handoff}.md

修改（TaskBudget 修复，任务书第二部分明确要求）：
  powercopy/research/agent.py     传 budget / 移除重复计数 / shared_budget
  powercopy/research/mock.py      invoke 支持 budget
  powercopy/copywriting/agent.py  传 budget / 移除重复计数
  powercopy/copywriting/mock.py   invoke 支持 budget
```

---

## 二、给 Sol 的接入契约（最高优先级）

**`docs/contracts/content-workflow.md`** 已写好，含：
- 服务签名与构造参数
- `ContentWorkflowRequest` / `ContentWorkflowResult` 完整字段
- 六种 `final_status` 语义
- 预算快照结构
- 四维审核状态
- **前端必须做与禁止做的清单**

### 最小接入代码

```python
from powercopy.workflows import (
    ContentWorkflowRequest,
    ContentWorkflowService,
)
from powercopy.research.verification import PolicyCorpus
from powercopy.tools.builtin.policy_tools import build_live_registry

def run_content_workflow(payload: dict, db_path, manifest_path):
    registry = build_live_registry(db_path)
    corpus = PolicyCorpus(db_path, manifest_path)
    service = ContentWorkflowService(corpus, registry, model_name="mock")

    request = ContentWorkflowRequest.model_validate(payload)
    result = service.run(request)
    return result.model_dump(mode="json")
```

### 三个必须提醒前端的关键点

1. **`final_status = "pending_review"` 是正常终态**，不是错误，不得渲染成「成功/已发布」
2. **`review.fact_support_llm_used = true` 时必须标注「机器意见，不代表专家确认」**
3. **四维审核不要合并成单一通过率**，能力差异太大

---

## 三、接口变化（可能影响 Sol）

### 3.1 `EvidencePackage.summary()` 字段变化（MVP-03 已改，此处再次申明）

移除了 `claims_verified`，改为：
- `claims_text_verified`
- `claims_pending_semantic_review`
- `claims_human_confirmed`（恒为 0）

### 3.2 `ResearchAgent.__init__` 新增 `shared_budget` 参数

向后兼容（默认 None）。若 Sol 侧有直接实例化 `ResearchAgent` 的代码，无需改动。

### 3.3 `LLMClient.invoke` 现在应传 `budget`

```python
response = client.invoke(messages, budget=budget)
```

不传也能工作（默认 None），但那样预算不受控。**建议 Sol 若在自己的代码里
调用 LLMClient，也按此传参。**

### 3.4 Mock 的 `invoke` 现在会消耗预算

`MockResearchLLM` / `MockCopywriterLLM` / `MockReviewerLLM` / `MockReviserLLM`
收到 `budget` 时会 `reserve_api_attempt()`。若 Sol 的测试复用了这些 Mock 并
传入共享预算，需注意 attempt 计数会增长。

---

## 四、需要协调的事项

### 协调项 1：`docs/roadmap-v2.1.md` 的 MVP-04 状态

本行应更新为：

```text
MVP-04  审核及修订            ✅ 已完成（MVP-04-B 后端；审批 UI 由 MVP-05 承接）
```

**本轮不修改该文件**，等集成时统一处理。

### 协调项 2：共享状态文档

`docs/PROJECT_STATE.md`、`docs/CHANGELOG.md`、`README.md` 同理，本轮不改。

### 协调项 3：Sol 的 RAG Studio 是否要展示审核结果

若 RAG Studio 需要展示 `ReviewReport`，直接消费
`result.review.model_dump(mode="json")` 即可。

---

## 五、本轮的独立文档

| 文件 | 内容 |
|---|---|
| `docs/contracts/content-workflow.md` | **统一业务契约，供 Sol 接入** |
| `docs/review/mvp-04b-report.md` | 完整交付报告 |
| `docs/review/mvp-04b-handoff.md` | 本文件 |

---

## 六、依赖

- `storage/policies_versioned.db`（只读）
- `storage/rag/{chunks.json,lexical.db,dense.npz}`（需 v2 索引格式）

---

## 七、合并建议

1. 本轮修改了 `powercopy/research/*` 与 `powercopy/copywriting/*` 各两个文件
   （TaskBudget 修复），与 Sol 的前端/工作台改动不重叠
2. 唯一潜在冲突是 `docs/roadmap-v2.1.md`（本轮未改）
3. 合并后需重跑全量回归（本分支 1363 passed）
4. 合并后由集成方统一更新共享状态文档

---

## 八、本轮未做的事

- 不开发前端
- 不修改 workbench
- 不建设 GraphRAG / Memory / Skills / 多 Agent Supervisor
- 未调用付费 LLM
- 未修改共享状态文档
