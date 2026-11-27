# MVP-01 检索集成报告

**日期**：2026-11-26
**阶段**：V2.1 MVP-01 Retrieval Integration
**基线**：`f26d8bd`，1122 passed
**结果**：**1189 passed**（+55 专项 +12 其他调整）

---

## 一、交付目标

让现有 Agent 真正能够**安全、稳定**地使用已实现的混合检索系统。

本阶段不重建 RAG、不实现 GraphRAG、不引入 Reranker、不重构 Agent Runtime。

---

## 二、源码评审问题修复

### 2.1 Hybrid 索引一致性（原 302-349 / 393-442 行）

**问题**：旧实现用 `[v for v in fingerprints.values() if v]` 过滤掉空值，
导致「拿不到指纹」被当成「一致」；且版本冲突可以用 `allow_degrade=True` 绕过。

**修复**：引入四状态判定。

| status | 触发条件 | 行为 |
|---|---|---|
| `ok` | 两路指纹非空且相等 | 完整融合 |
| `single_path_unconfigured` | 只配置一路 | 降级为单路，显式标注 |
| `fingerprint_missing` | 任一路指纹为空 | **不判为一致**；不降级则抛错，降级则返回空 |
| `version_conflict` | 指纹不相等 | **即使 allow_degrade=True 也抛 HybridError** |

**降级时如何确定可信单路**：

1. 该路自身必须被配置且指纹非空
2. 该路检索必须成功

任一不满足 → `_empty_outcome` 返回空结果 + `single_path_failed`，不返回任何候选。

**区分四种状态**（项目负责人要求）：

- 单路未配置 → `single_path_unconfigured`
- 单路异常 → `lexical_failed` / `dense_failed` / `single_path_failed`
- 索引指纹缺失 → `fingerprint_missing`
- 版本冲突 → 抛 `HybridError`，`consistency_status = version_conflict`

### 2.2 Dense 模型一致性

**问题**：

- 命名空间只记录 `transformers` 库版本号当 tokenizer 指纹（库版本相同 ≠ 词表相同）
- 索引侧完全不校验模型身份，只靠 `dim == 512`
- 缓存命名空间缺 commit / tokenizer 时仍会加载旧向量

**修复**：

`CacheNamespace` 绑定七项：

```text
model / commit / tokenizer_fingerprint / dim / pooling / normalized / encode_mode
```

- 命名空间缺 `model` / `commit` / `tokenizer_fingerprint` → **拒绝加载缓存**
- 索引必须记录 `model_commit` 与 `tokenizer_fingerprint`，缺失 → 拒绝加载索引
- 加载后与**每次 search()** 都校验模型一致性 → 任一不符抛 `DenseSearchError`

**commit 获取方式**：新版 transformers 的 `config._commit_hash` 为 `None`（实测），
改用 `huggingface_hub.snapshot_download(local_files_only=True)` 解析本地快照目录名。

**chunk_id 完整性**：

- 索引内唯一性检查
- 与清单双向对应：索引里的必须在清单里，清单里的必须都被索引

### 2.3 安全与离线运行

| 项 | 修复前 | 修复后 |
|---|---|---|
| NPZ chunk_ids | `dtype=object`，需 `allow_pickle=True` | 定长 Unicode，`allow_pickle=False` |
| 旧索引 | 自动兼容 | **拒绝并指引重建** |
| 模型加载 | 缺模型可能联网 | `local_files_only=True` 默认，缺模型报错 |
| 路径来源 | 可传入 | 只来自程序配置，模型参数 `extra="forbid"` |

**旧索引重建指引**（错误信息里直接给出）：

```text
索引格式不受支持（可能由旧版本生成）。出于安全考虑不允许反序列化 pickle
内容，请重建索引：python scripts/build_dense_index.py --rebuild
```

新增脚本 `scripts/build_dense_index.py`：

- 默认离线
- 需显式 `--rebuild` 才覆盖
- 构建后自动自检（能否 `allow_pickle=False` 加载 + 模型一致性）

---

## 三、Retrieval Tool

### 3.1 工具设计

**名称**：`search_policy_evidence`
**文件**：`powercopy/tools/builtin/retrieval.py`

**为什么不改造 `search_policy`**：

`search_policy` 是字面关键词查询（按省份+关键词粗查清单）；
新工具是语义召回（找相关原文片段作证据）。
两者输入、输出、失败模式都不同，混在一起会让模型无法判断该用哪个。

**保留原有工具**：`search_policy`、`get_policy_detail` 行为完全不变。

### 3.2 输入

| 字段 | 约束 |
|---|---|
| `query` | 必填，1-200 字符 |
| `top_k` | 可选，1-20，默认 5 |

`extra="forbid"`：模型传 `index_path` 之类字段会被直接拒绝。

### 3.3 输出

```json
{
  "query": "...",
  "retrieval_mode": "hybrid_rrf",
  "index_fingerprint": "...",
  "degraded_status": null,
  "consistency_status": "ok",
  "index_freshness": "ok",
  "candidate_count": 5,
  "evidence_candidates": [
    {
      "evidence_id": "23d472d7eb6ec5c4",
      "chunk_id": "...", "policy_id": "...", "document_id": "...",
      "version": 1, "title": "...", "source_url": "...",
      "start": 737, "end": 1144, "page": null,
      "content_sha256": "...", "text": "...", "text_truncated": false,
      "retrieval_rank": 1, "retrieval_score": 0.032787,
      "path_contributions": [...]
    }
  ],
  "candidates_are_not_facts": true,
  "notice": "..."
}
```

### 3.4 「候选 ≠ 事实」的三处声明

1. `candidates_are_not_facts: true` 字段
2. `notice` 首句：「以下内容是**检索候选**，不是已核实的事实依据」
3. 工具 `description` 中写明（模型选工具时就能看到）

### 3.5 索引与政策库版本一致性

`check_index_freshness()` 从政策库读出所有 `(policy_id, version, content_sha256)`，
与切片清单比对：

| 情况 | 判定 |
|---|---|
| 索引有、库里没有 | `stale` |
| 库里有更高版本、索引没有 | `stale` |
| 库不可读 / 清单不可读 | `fresh=False`，`status` 标注具体原因 |
| 完全对应 | `ok` |

**不允许把陈旧索引描述为最新政策**：
不新鲜时 `business_code = INDEX_MAY_BE_STALE`，`notice` 明确提示「结果可能不是最新政策」。

实测（当前库）：`index_freshness = ok`，26 切片与库中 2 份政策完全对应。

### 3.6 资源限制

| 项 | 上限 |
|---|---|
| query | 200 字符 |
| top_k | 20 |
| 单条候选正文 | 800 字符（超出截断 + `text_truncated=true`） |
| 候选总数 | 受 top_k 约束 |

### 3.7 三层状态

| 情况 | execution_status | business_status | business_code |
|---|---|---|---|
| 正常召回 + 新鲜 | ok | success | — |
| 正常召回 + 可能过期 | ok | success | `INDEX_MAY_BE_STALE` |
| 无候选 | ok | empty | `NO_EVIDENCE_CANDIDATE` |
| 索引缺失 | ok | failed | `RAG_INDEX_UNAVAILABLE` |
| 检索异常 | ok | failed | `RAG_RETRIEVAL_FAILED` |
| 参数非法 | invalid_arguments | not_applicable | — |

---

## 四、运行时集成

### 4.1 默认行为不变

`RunRequest` 新增 `use_retrieval: bool = False`：

- 默认 `False`：走原有 `search_policy` → `get_policy_detail`
- `True`：走 `search_policy_evidence`

这样原有测试、演示流程、前端默认行为**零影响**。

### 4.2 端到端验证（Mock LLM）

实际运行结果：

```text
status  : completed
rounds  : 2
content : 本地混合检索（hybrid_rrf）返回 5 个候选片段。
          首条来自 policy_id=00aaf372... 的 v1，字符区间 [385, 817)。
          这些是检索候选而非已核实事实，需核对原文后引用。
tool    : search_policy_evidence  exec=ok biz=success code=None
```

完整链路：用户任务 → Agent Loop → Hybrid Retrieval Tool → ToolResult → Observation → 最终输出。

### 4.3 编码器复用

`PolicyEvidenceService` 持有 `_embedder` 与 `_hybrid` 单例：

| 场景 | 耗时 |
|---|---|
| 首次检索（含模型加载） | 约 3-5 秒 |
| 后续检索（复用） | 约 15 ms |

测试验证：两次检索返回同一个对象 `id()`。

### 4.4 默认本地，不自动开付费模型

`mode` 仍为 `Literal["local", "live"]`，默认 `local`。
`live` 需要 `POWERCOPY_WORKBENCH_ALLOW_LIVE=1` + `.env` 配齐 + 本次任务 `confirm_live=True`。

### 4.5 前端工具清单

`GET /api/workbench/tools` 实际返回 3 个工具，包含新工具的 5 条限制说明。
前端 `ToolsPage` 直接消费该端点，无需改动。

测试验证：端点返回的工具名与 `build_live_registry().list_tools()` **完全一致**。

---

## 五、验收测试

```text
专项（test_retrieval_integration.py）：55 passed
全量回归：1189 passed
```

### 覆盖矩阵

| 验收项 | 测试类 | 数量 |
|---|---|---|
| 双索引正常融合 | `TestHybridIndexConsistency` | 1 |
| 指纹缺失 | 同上 | 3 |
| 版本不一致 | 同上 | 2 |
| 单路未配置 / 异常 | 同上 | 3 |
| 四状态可区分 | 同上 | 1 |
| 模型 commit 不一致 | `TestDenseModelConsistency` | 6 |
| 缓存失效 | `TestCacheInvalidation` | 7 |
| 旧索引格式拒绝 | `TestNpzSafety` | 3 |
| 完全离线模型加载 | `TestOfflineLoading` | 4 |
| 原政策数据库只读 | `TestEvidenceToolLive` | 1 |
| 工具成功 / 空 / 异常 / 降级 | `TestEvidenceTool` + `TestEvidenceToolStates` | 12 |
| Agent Runtime 完整链路 | `TestAgentRuntimeIntegration` | 4 |
| 原有政策查询无回归 | `TestEvidenceToolStates` | 1 |
| 前端工具清单 | `TestToolsEndpoint` | 4 |
| 参数边界 | `TestEvidenceTool` | 5 |

### Node 环境

**本机无 Node / pnpm**，无法执行前端类型检查与生产构建。

已验证的替代手段：

- `/api/workbench/tools` 端点实际返回正确（3 工具 + 完整 schema + 限制说明）
- 端点返回与 registry 一致（测试断言）
- 前端 `ToolsPage` 是动态渲染（`useApi<{items}>('/tools')`），无硬编码工具名

**未验证**：`tsc --noEmit`、`pnpm build`。需在有 Node 的机器上补跑。

---

## 六、实际修改文件

### 新增

```text
powercopy/tools/builtin/retrieval.py     检索证据工具（约 560 行）
scripts/build_dense_index.py             离线索引构建脚本
tests/test_retrieval_integration.py      55 项专项测试
docs/adr/ADR-009-retrieval-integration.md
docs/roadmap-v2.1.md
docs/review/mvp-01-retrieval-integration.md
```

### 修改

```text
powercopy/rag/hybrid.py         四状态一致性判定 + 单路降级 + consistency_status
powercopy/rag/dense.py          CacheNamespace / 模型身份校验 / NPZ 安全 / chunk_id 完整性
powercopy/rag/embedding.py      local_files_only / commit 解析 / tokenizer 指纹
powercopy/tools/builtin/policy_tools.py  build_live_registry 支持 with_retrieval
workbench/runtime_api.py        use_retrieval 开关 + _respond_retrieval + 工具限制
tests/test_hybrid_retrieval.py  一致性测试改为新语义（+7）
tests/test_dense_retrieval.py   缓存测试改为新语义（+7）
tests/test_policy_tools_runtime.py  工具数量 2 → 3（+1）
docs/roadmap-stage05.md         RRF 表述澄清 + H3 状态更新
docs/README.md / CHANGELOG / PROJECT_STATE / AGENTS.md
```

---

## 七、运行命令

```powershell
# 重建向量索引（旧格式必须重建）
python scripts/build_dense_index.py --rebuild

# 重建词法索引
python scripts/eval_retrieval.py --rebuild

# 专项测试
python -m pytest tests/test_retrieval_integration.py -q

# 全量回归
python -m pytest tests -q
```

---

## 八、未解决问题

| 问题 | 说明 | 归属 |
|---|---|---|
| **证据充分性判断** | 候选 ≠ 有证据，向量必然返回 Top-K | MVP-02 |
| RRF 不保证所有查询更优 | 分类型指标全部持平 | 已在文档澄清 |
| 语料仅 26 切片 | 指标泛化能力受限 | 需扩语料 |
| 冷启动约 3-5 秒 | 模型加载成本 | 可预热 / ONNX |
| 前端未做类型检查 | 本机无 Node | 需补跑 |
| 政策关系推理未建设 | 与文本检索是两件事 | 技术升级路线 |
| 评测集未业务审核 | 开发方标注 | MVP-06 |

---

## 九、结论

**达成目标**：现有 Agent 现在可以安全、稳定地使用混合检索。

关键改进：

1. **索引一致性不再有「拿不到指纹就算一致」的漏洞**
2. **版本冲突硬拒绝，跨版本融合不可能发生**
3. **NPZ 加载不再需要 pickle**
4. **模型身份绑定到 commit + tokenizer 指纹**
5. **完全离线，缺模型明确报错**
6. **输出明确区分「候选」与「事实」**

诚实说明的局限：

- 证据充分性判断仍是空缺，这是比检索接入更本质的问题
- RRF 不保证每个查询更优，只在总体指标上更好
- 前端类型检查未执行
