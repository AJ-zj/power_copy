# MVP-03-B 交接记录（与 MVP-01-F 并行）

**日期**：2026-11-27
**基线提交**：`ea64cb696b40666f56b8acb66e173cea8a44a7a9`
**分支**：`feat/mvp03-copywriting`
**Worktree**：`D:\桌面\昆吾电力项目\PowerCopy-V2-mvp03`

---

## 一、并行开发约定

GPT 6.2 Sol 正在独立负责 **MVP-01-F**（RAG Studio 前端、工作台 API、源码审查）。

本记录用于避免两边同时修改同一文件。

### 本轮（MVP-03-B）修改的文件

```text
新增：
  powercopy/copywriting/{__init__,contracts,guards,agent,mock}.py
  scripts/generate_copy.py
  tests/test_copywriting_agent.py
  docs/copywriting/{mvp-03b-report,mvp-03b-handoff,mvp-03b-contracts}.md

修改（任务书第一部分明确要求的小范围修复）：
  powercopy/research/agent.py       TaskBudget 接入 / 状态重算 / 预算耗尽恢复
  powercopy/research/contracts.py   分项统计 statistics()
```

### 本轮**未触碰**的文件（Sol 可自由修改）

```text
frontend/**                    未修改
workbench/**                   未修改
powercopy/rag/dense.py         未修改
powercopy/rag/embedding.py     未修改
powercopy/rag/hybrid.py        未修改
powercopy/rag/lexical.py       未修改
powercopy/tools/builtin/retrieval.py   未修改
powercopy/tools/executor.py    未修改
powercopy/core/**              未修改
```

---

## 二、需要协调的事项

### 协调项 1：`docs/roadmap-v2.1.md` 的 MVP-03 状态

MVP-01 提交中该文件写的是：

```text
MVP-03  文案生成              ⬜ 未启动
```

本轮完成后应改为：

```text
MVP-03  文案生成              ✅ 已完成（MVP-03-B 后端；前端由 Content Studio 承接）
```

**建议**：由集成方在两分支合并后统一更新。本轮**不修改**该文件。

### 协调项 2：`docs/PROJECT_STATE.md` 与 `docs/CHANGELOG.md`

同理，本轮**不修改**，等集成时统一处理。

### 协调项 3：`powercopy/research/` 的接口变化

本轮对 `EvidencePackage` 做了两处**向后兼容**的调整：

1. `summary()` 移除了 `claims_verified` 字段，改为
   `claims_text_verified` / `claims_pending_semantic_review` / `claims_human_confirmed`
2. 新增 `statistics()` 方法

**影响面**：

- `scripts/run_research.py` 用的是 `summary()` 的其它字段，未受影响
- 若 Sol 的 RAG Studio 或工作台 API 读 `claims_verified`，需同步改为新字段名

**建议**：Sol 侧若有引用，改为：
- 「已核验事实」→ `claims_text_verified`（但需说明这仅表示引文一致）
- 「待审核」→ `claims_pending_semantic_review`

### 协调项 4：Content Studio 需要消费的新契约

Sol 开发 Content Studio 时可直接使用 `ContentDraft`：

```text
GET/序列化：draft.model_dump(mode="json")
```

完整字段说明见 `docs/copywriting/mvp-03b-contracts.md`。

**前端必须注意的三点**：

1. `status = pending_review` 是**正常状态**，不是错误
2. `key_points[].semantic_status` 恒为 `pending_review`，需在 UI 提示
3. `human_confirmed` 恒为 0，不要在 UI 上显示成「0 条已验证事实」

---

## 三、本轮的独立文档

| 文件 | 内容 |
|---|---|
| `docs/copywriting/mvp-03b-report.md` | 完整交付报告 |
| `docs/copywriting/mvp-03b-handoff.md` | 本文件 |
| `docs/copywriting/mvp-03b-contracts.md` | 数据契约（供 Content Studio） |

**不新增 ADR** —— 本轮没有引入新的架构级决策：
- 复用现有 LLMClient / TaskBudget（既有构件）
- 文案生成是单次结构化调用，不涉及新的编排模式
- 证据溯源机制沿用 MVP-02 的 `evidence_id` 体系

---

## 四、依赖

本轮依赖：
- MVP-02-B 的 `powercopy.research`（EvidencePackage / Claim / EvidenceItem）
- MVP-01 的 `storage/rag/{chunks.json,lexical.db,dense.npz}`
- `storage/policies_versioned.db`（只读）

**索引需要是 v2 格式**（MVP-01 已升级）。若本地索引仍是 v1，
需先执行 `python scripts/build_dense_index.py --rebuild`。

---

## 五、合并建议

1. 代码层面：本轮修改了 `powercopy/research/*` 两个文件，
   与 Sol 的前端/工作台改动不重叠，应无冲突
2. 唯一潜在冲突仍是 `docs/roadmap-v2.1.md`（本轮未改）
3. 合并后需重跑全量回归（本分支 1301 passed，其中 63 项为本轮新增）
4. 建议集成方统一更新共享状态文档

---

## 六、本轮未做的事

- 不开发前端
- 不实现自动审核 / 修订 / 审批
- 未调用付费 LLM
- 未修改共享状态文档
