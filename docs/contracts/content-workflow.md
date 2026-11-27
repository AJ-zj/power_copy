# 内容工作流统一契约

**版本**：`content-workflow-v1`
**日期**：2026-11-27
**用途**：供 workbench API（Sol）接入的统一业务服务
**状态**：research → draft → review → revision 四阶段均已实现

---

## 一、服务签名

```python
from powercopy.workflows import ContentWorkflowService, ContentWorkflowRequest
from powercopy.research.verification import PolicyCorpus
from powercopy.tools.builtin.policy_tools import build_live_registry

registry = build_live_registry(db_path)
corpus = PolicyCorpus(db_path, manifest_path)

service = ContentWorkflowService(
    corpus,
    registry,
    research_llm=None,      # None → 按 mode 用 Mock
    copywriting_llm=None,
    reviewer_llm=None,
    reviser_llm=None,
    model_name="mock",
)

result = service.run(request)   # ContentWorkflowResult
```

### 构造参数

| 参数 | 说明 |
|---|---|
| `corpus` | `PolicyCorpus`，封装只读政策库与切片清单 |
| `registry` | `ToolRegistry`，需含 `search_policy_evidence` |
| `research_llm` | 研究阶段模型。None → Mock |
| `copywriting_llm` | 文案阶段模型。None → Mock |
| `reviewer_llm` | 事实支持性判断模型。None → Mock（不做语义判断） |
| `reviser_llm` | 自动修订模型。None → Mock |
| `model_name` | 写入结果，用于溯源 |

**workbench 接入建议**：`mode=mock` 时全部传 None；`mode=live` 时传同一个
`LLMClient` 实例给四个参数（复用连接）。

---

## 二、输入契约 `ContentWorkflowRequest`

```python
{
  "topic": "工商业企业参与电力市场前，需要了解哪些基本规则？",
  "target_audience": "工商业用电企业",
  "region": "",
  "platform": "wechat_mp",
  "target_chars": 800,
  "style": "专业、通俗、可信",
  "purpose": "为工商业用电企业提供政策科普",

  "mode": "mock",
  "enable_review": true,
  "enable_revision": true,
  "max_revisions": 1,

  "max_api_attempts": 12,
  "max_known_tokens": 40000,
  "max_questions": 5,
  "max_tool_calls": 8
}
```

### 字段约束

| 字段 | 类型 | 默认 | 范围 |
|---|---|---|---|
| `topic` | str | — | 1-200 字符，必填 |
| `target_audience` | str | `""` | ≤100 |
| `region` | str | `""` | ≤40 |
| `platform` | enum | `wechat_mp` | wechat_mp / website / linkedin / general |
| `target_chars` | int | 800 | 100-5000 |
| `style` | str | 专业、通俗、可信 | ≤200 |
| `purpose` | str | `""` | ≤500 |
| `mode` | enum | `mock` | mock / live |
| `enable_review` | bool | true | — |
| `enable_revision` | bool | true | — |
| `max_revisions` | int | 1 | 0-3 |
| `max_api_attempts` | int | 12 | 1-60 |
| `max_known_tokens` | int | 40000 | 1000-400000 |
| `max_questions` | int | 5 | 1-12 |
| `max_tool_calls` | int | 8 | 1-30 |

### 安全约束

`extra="forbid"` —— 拒绝任何未声明字段。因此无法通过请求传入
`db_path` / `index_path` / `url` / `llm_api_key` 等。

**注意**：本契约**未使用** `strict=True`。原因是它是 HTTP API 边界，
JSON 里 `platform` 必然是字符串 `"wechat_mp"` 而非枚举实例。
安全边界由 `extra="forbid"` 保证。

### 预算语义（重要）

| 上限 | 性质 |
|---|---|
| `max_api_attempts` | **硬约束**。每次真实 API 请求（含重试）都预占一个名额 |
| `max_known_tokens` | **软预算**。只在模型返回 `usage` 时可计量；不返回时只能标记 `usage_incomplete` |

**四个阶段共享同一份预算**，依次消耗剩余额度。
不存在「研究拿满额、创作另给一半」的拆分。

---

## 三、输出契约 `ContentWorkflowResult`

```python
{
  "task_id": "task-<16hex>",
  "topic": "...",
  "mode": "mock",

  "final_status": "pending_review",
  "status_reason": "流程完成，产物待人工审核...",

  "research": {...},      # EvidencePackage | null
  "draft": {...},         # ContentDraft | null
  "review": {...},        # ReviewReport | null
  "revision": {...},      # RevisionResult | null

  "budget_snapshot": {...},
  "stage_timings_ms": {"research_ms": 1200, "draft_ms": 30, "review_ms": 5},
  "model_name": "mock",
  "created_at": "2026-11-27T02:14:03+00:00",
  "schema_version": "content-workflow-v1"
}
```

### `final_status` 取值

| 值 | 含义 | 前端应如何展示 |
|---|---|---|
| `draft_ready` | 已生成草稿，未审核（`enable_review=false`） | 需审核 |
| `pending_review` | 已审核，需人工确认 —— **正常终态** | 待审核（非错误） |
| `needs_manual_review` | 阻断级问题或修订后仍不通过 | 需人工处理 |
| `aborted` | 证据不足，主动停止 | 证据不足 |
| `budget_exhausted` | 预算耗尽，流程中断 | 预算耗尽 |
| `failed` | 程序错误 | 失败 |

**没有任何一个值表示「可以自动发布」。**

### `budget_snapshot`

```json
{
  "max_api_attempts": 12,
  "used_api_attempts": 6,
  "remaining_api_attempts": 6,
  "max_known_tokens": 40000,
  "known_total_tokens": 0,
  "remaining_known_tokens": 40000,
  "usage_incomplete": false,
  "exhausted_by": null,
  "token_limit_is_soft": true,
  "note": "api_attempts 是硬约束；known_tokens 只在模型返回 usage 时可计量..."
}
```

---

## 四、审核状态

### `ReviewReport.status`

| 值 | 含义 |
|---|---|
| `passed` | 程序未发现已知硬性问题。**不代表可发布** |
| `needs_revision` | 存在可修复的 major 问题 |
| `failed` | 存在 blocking 问题，需人工处理 |
| `pending_review` | 存在无法程序判定的部分 |

### 四维审核

| 维度 | 判定能力 | verdict 可能值 |
|---|---|---|
| `citation` | **程序完全可判** | passed / failed |
| `fact_support` | 需语义判断，程序不可靠 | pending_review / machine_supported / failed |
| `compliance` | 字面匹配，有漏报 | passed / pending_review / failed |
| `applicability` | 时效地区部分可判 | passed / pending_review / failed |

### 关键警示字段

```python
report.fact_support_llm_used      # True → 结论仅是机器意见
report.human_confirmation_available  # 本阶段恒为 False
report.requires_human()           # 本阶段几乎恒为 True
```

**`fact_support_llm_used=True` 时，前端必须标注「机器审核，不代表专家确认」。**

### `RevisionResult`

```python
{
  "revision_id": "revn-<16hex>",
  "attempt": 1,                    # 0 表示未执行
  "status": "revised",             # skipped/revised/unchanged/failed/budget_exhausted
  "addressed_issue_ids": ["i1"],
  "remaining_issue_ids": ["i2"],
  "changed": true,
  "before_title": "...", "before_body": "...",
  "after_title": "...", "after_body": "...",
  "post_review_status": "pending_review",
  "post_review_blocking": 0
}
```

**自动修订默认最多 1 次**（`max_revisions` 上限 3）。
修订后自动重新审核；仍不通过则停止并请求人工。

---

## 五、前端集成要点

### 必须展示

1. **`final_status`** 徽章，`pending_review` 不得渲染成「成功/已发布」
2. **`requires_human`** 提示
3. **`review.fact_support_llm_used`** 为 True 时标注机器意见
4. **`review.dimension_results`** 四维分开显示，不要合并成单一分数
5. **`draft.key_points[].evidence_ids`** 与原文的对应关系
6. **`budget_snapshot`**（尤其 `usage_incomplete`）
7. **`revision.changed`** 与前后对比

### 禁止的 UI 做法

| 做法 | 问题 |
|---|---|
| `pending_review` 显示为「成功」 | 用户误以为可发布 |
| 隐藏 `policy_validity_note` | 忽略时效风险 |
| 把四维合并成「审核通过率 85%」 | 掩盖能力差异 |
| 显示 `human_confirmed=0` 为「0 条已验证」 | 误导为「无事实」 |
| 提供「一键发布」按钮 | 违反本阶段约束 |

---

## 六、最小接入示例（workbench）

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

`ContentWorkflowResult` 支持 `model_dump(mode="json")` 直接序列化。

---

## 七、只跑 research → draft

按任务书「允许先完成 research → draft 入口，再增量接入 review → revision」：

```python
request = ContentWorkflowRequest(
    topic="...",
    enable_review=False,     # 跳过审核
)
result = service.run(request)
# result.final_status == "draft_ready"
# result.review is None, result.revision is None
```

---

## 八、与既有契约的关系

```text
ContentWorkflowRequest
    │
    ├─► ResearchRequest ──► ResearchAgent ──► EvidencePackage
    │                                          │
    ├─► CopywritingRequest ◄───────────────────┘
    │       └─► CopywritingAgent ──► ContentDraft
    │                                  │
    └─► ReviewAgent ◄──────────────────┘
            └─► ReviewReport + RevisionResult
```

`ContentWorkflowResult` 内嵌全部四个产物，可直接追溯：

```text
文案句子 → key_point → claim_id → evidence_id → chunk_id
        → policy_id + version + start/end → 政策原文
```
