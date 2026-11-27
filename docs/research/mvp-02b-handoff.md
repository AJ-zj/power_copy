# MVP-02-B 交接记录（与 MVP-01-F 并行）

**日期**：2026-11-27
**基线提交**：`73cdd8b5e2cadd3f8662112704b22526e3eb8c38`
**分支**：`feat/mvp02-research`
**Worktree**：`D:\桌面\昆吾电力项目\PowerCopy-V2-mvp02`

---

## 一、并行开发约定

GPT 6.2 Sol 正在独立负责 **MVP-01-F**（RAG Studio 前端、工作台 API、源码审查）。

本记录用于避免两边同时修改同一文件。

### 本轮开发（MVP-02-B）涉及的目录

```text
powercopy/research/            ← 全新，无冲突
scripts/run_research.py        ← 新增文件
tests/test_research_agent.py   ← 新增文件
docs/roadmap-v2.1.md           ← 仅追加 MVP-02 状态（见下方协调说明）
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

**结论：本轮为纯新增模块，不修改任何共享模块。**

---

## 二、需要协调的事项

### 协调项 1：`docs/roadmap-v2.1.md` 的 MVP-02 状态

MVP-01 提交中该文件的 MVP-02 行是：

```text
MVP-02  政策研究与证据包       ⬜ 未启动
```

本轮完成后应改为：

```text
MVP-02  政策研究与证据包       ✅ 已完成（MVP-02-B 后端；前端由 MVP-01-F 负责）
```

**建议**：由集成方在两分支合并后统一更新，避免同时改同一行产生冲突。
本轮**不修改** `docs/roadmap-v2.1.md`。

### 协调项 2：`docs/PROJECT_STATE.md` 与 `docs/CHANGELOG.md`

同理，这两份共享状态文档本轮**不修改**，等集成时统一处理。

### 协调项 3：证据包与 RAG Studio 前端的接口对齐

MVP-01-F 的 RAG Studio 前端如果需要展示研究结果，可直接消费
`EvidencePackage.model_dump(mode="json")` 的结构。

关键字段：

```text
research_id / topic / research_status / status_reason
research_questions[]      question_id / text / answered
claims[]                  claim_id / statement / evidence_ids / status
evidence_items[]          evidence_id / policy_id / version / start / end /
                          exact_quote / content_sha256 / source_url / status
unanswered_questions[]
uncertainties[]
policy_validity_status / policy_validity_note
verification_issues[]
```

**注意**：`claims[].status` 的正常值就是 `pending_review`（语义支持性待人工确认），
前端不应把它渲染成错误或失败状态。

---

## 三、本轮的独立文档

为不干扰共享文档，本轮新增以下**专属**文档：

| 文件 | 内容 |
|---|---|
| `docs/research/mvp-02b-report.md` | 本轮完整交付报告 |
| `docs/research/mvp-02b-handoff.md` | 本文件 |
| `docs/research/mvp-02b-contracts.md` | 数据契约说明 |

不新增 ADR —— 本轮没有引入新的架构级决策（见报告第 9 节说明）。

---

## 四、索引与缓存依赖

本轮依赖 MVP-01 交付的：
- `storage/rag/chunks.json`（切片清单）
- `storage/rag/lexical.db`（词法索引）
- `storage/rag/dense.npz`（向量索引，schema v2）

**注意**：MVP-01 已将索引升级为 `policy-dense-index-v2` 且要求
`allow_pickle=False` 加载。若 Sol 的 RAG Studio 也在读索引，
必须同样使用新的加载路径（`DenseSearch` 已封装好）。

---

## 五、合并建议

1. 本轮为纯新增，合并时不应产生代码冲突
2. 唯一潜在冲突是 `docs/roadmap-v2.1.md` 的状态行（本轮未改）
3. 合并后需重新跑全量回归（本分支 1238 passed，其中 49 项为本轮新增）
4. 合并后建议由集成方统一更新共享状态文档

---

## 六、本轮未做的事

- 不开发前端（由 MVP-01-F 负责）
- 不实现最终文案生成（MVP-03）
- 不引入 GraphRAG / Reranker / Memory
- 不修改共享状态文档
- 未推送、未合并
