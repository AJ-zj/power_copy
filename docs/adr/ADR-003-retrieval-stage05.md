# ADR-003：阶段05 检索方案——先 BM25 基线，向量延后

- **状态**：已采纳（05-A 部分）；05-B 向量方案已定（见 ADR-006）
- **日期**：2026-11-26
- **决策者**：项目负责人
- **相关阶段**：阶段05 RAG
- **关联问题**：P0-3

> **2026-11-26 更新**：Embedding 来源已确定 —— 本地 BGE-small-zh-v1.5（512 维），详见 [ADR-006](ADR-006-local-embedding.md)。
> 平台侧核实结论：`/v1/embeddings` 端点存在，但当前 API Key 分组无 embedding 通道。

---

## 背景

阶段05 的目标架构是「Adaptive Graph-enhanced RAG」，包含混合检索、知识图谱、自适应路由、Agentic RAG、增量索引。

复审时发现一个**决定性的技术事实**：

**平台不提供 embedding 模型。**

只读探测 `GET /v1/models` 返回 26 个模型，全部是对话模型：

```text
deepseek-v4-flash/pro, glm-5.1/5.2/5.3/flash, hy3/hy4-preview,
kimi-k2.7-code/k3/k3.1-pro, mimo-v2.5-pro/v2.6-flash/v2.6-pro,
MiniMax-M2.7/M3, minimax-m3.1-flash, qwen3.7-max/qwen3.8-flash/qwen3.8-max
```

`embedding / bge / gte / text-embedding` 关键词匹配结果为 **0**。

这意味着"向量检索"这条路在当前基础设施下**无法直接实现**。

---

## 备选方案

### 方案 A：引入本地 embedding 模型

例如 BGE-m3（约 2GB）、text2vec-base-chinese。

**优点**
- 完全自主可控，无 API 成本
- 中文支持好

**缺点**
- 新增 ~2GB 依赖 + 推理开销
- Windows 环境下 torch 安装体积大
- 与"新增第三方依赖 0"的既有约束冲突
- 需 GPU 或较长 CPU 推理时间

### 方案 B：接入第三方 embedding API

例如 OpenAI text-embedding-3、智谱 embedding-3、阿里 text-embedding-v3。

**优点**
- 无本地依赖
- 质量有保证

**缺点**
- 新增外部依赖与成本
- 需再申请一个 API Key
- 数据出本地，对政策文档是合规考量
- 当前平台不提供，需另寻供应商

### 方案 C：先用 SQLite FTS5 做 BM25 基线（推荐）

SQLite 内置 FTS5 全文索引模块，支持 BM25 排序。

**优点**
- **零新依赖**（标准库 `sqlite3` 自带）
- 立即可以建基线，产出可量化指标
- 中文需要分词，但可用 FTS5 的 trigram tokenizer 或配合 `jieba` 预处理
- 为后续向量方案提供**对照基线**

**缺点**
- 纯词汇匹配，无同义扩展
- 中文分词质量影响效果

### 方案 D：跳过检索，直接做 GraphRAG

**优点**
- 一步到位

**缺点**
- 无基线无法证明 GraphRAG 更好
- 无 embedding 时图检索也只能靠实体名精确匹配
- 语料仅 2 份政策，规模完全不匹配
- 成本极高（原版 GraphRAG 需对每 chunk 做 LLM 实体抽取）

---

## 决策建议

**推荐方案 C：先做 SQLite FTS5 的 BM25 基线，向量延后到条件成熟。**

> **2026-11-26 更新**：向量来源已确定为本地 BGE-small-zh-v1.5（见 ADR-006）。
> 05-A（BM25 基线）仍是第一步，05-B 现在具备落地条件。

理由：

1. **无依赖是硬约束**。项目从开始就坚持"新增第三方依赖 0"（4.8 节、5.1 节均明确记录）。FTS5 是 SQLite 内置模块，符合这一原则。

2. **基线是评测的前提**。目标架构包含"自适应检索路由"和"对照评测能力"。没有基线，就无法判断"路由到图检索比路由到 BM25 更好"。这是方法论问题，不是技术选择问题。

3. **语料规模不支持复杂方案**。当前 2 份政策、26 个切片。任何复杂检索方案在这个规模下都无法体现优势，反而增加调试成本。

4. **向量化来源可以后补**。`chunking.py` 产出的清单已经包含 `text` 和 `text_sha256`，一旦有了 embedding 来源，可以对现有切片批量补算向量，不影响已有结构。

### 分阶段路线

```text
05-A  BM25 基线（方案 C）
  ├─ SQLite FTS5 虚表 + 触发器同步
  ├─ 中文分词：优先试 FTS5 trigram，不行再用 jieba
  ├─ 检索评测集：20-30 条 query，人工标注相关切片
  ├─ 指标：Recall@10 / MRR / nDCG
  └─ 产出：基线数字

05-B  向量检索（待 embedding 来源确定）
  ├─ 前置决策：本地模型 or 第三方 API（见下方重评条件）
  ├─ 混合检索：BM25 分数 + 向量相似度加权
  └─ 产出：与 05-A 基线的对照表

05-C  关系增强（不引入图数据库）
  ├─ 前置：P0-4 政策生命周期建模
  ├─ 在 SQLite 里建 policy_relations 表
  │   (source_policy_id, relation_type, target_policy_id)
  │   relation_type: supersedes / amends / references
  ├─ 检索时先查关系，再扩展召回
  └─ 产出：关系查询能力，验证是否需要图数据库

05-D  Adaptive Routing + Agentic RAG
  ├─ 前置：05-A/B/C 的评测数据
  ├─ 按 query 特征路由（精确条款号 / 概念查询 / 关系查询）
  └─ 产出：自适应检索，Agent 自主决策
```

---

## 关于 GraphRAG 的具体判断

**不引入原版 GraphRAG。**

原版 Microsoft GraphRAG 的索引流程：每个 chunk 过 LLM 抽取实体 → 三元组合并 → Leiden 社区检测 → 每个社区 LLM 写摘要 → 层级再摘要。公开案例显示，索引一个中等数据集可能消耗数万美元 LLM 调用。

当前 2 份政策，用这个流程是严重的过度设计。

**LazyGraphRAG 也不引入。**

LazyGraphRAG 把摘要推迟到查询期，索引成本降到原版 0.1%，查询成本低 700 倍。但：

- 它主要集成在 Azure 产品中，开源库支持有限
- 即使成本降到 0.1%，当前语料规模下收益仍然是负的（2 份政策做实体抽取的收益 < 实现成本）

**真正有价值的"图"是政策关系，不是实体共现。**

售电业务的核心问题不是"哪些实体经常一起出现"，而是：

- 政策 A 废止了政策 B 吗？
- 这条规则被哪条新规替代了？
- 引用链是什么？

这些用 SQLite 的关系表 + 外键就能表达，不需要图数据库，不需要 Leiden 社区检测。

**引入图数据库的触发条件**（明确记录下来）：

1. 政策库 > 500 份
2. 需要 3 跳以上的路径查询
3. 图查询占总查询量 > 30%
4. 关系表查询性能成为瓶颈（实测）

---

## 后果

### 正面

- 阶段05 可以立即启动，不被 embedding 阻塞
- 零新依赖，符合项目一贯原则
- 建立可量化基线，为后续所有方案提供对照
- 关系建模用 SQLite 完成，无需图数据库

### 负面

- 纯词汇匹配，对同义词、改写不敏感
- 中文分词需要额外处理

### 风险

- FTS5 中文支持需要实测。如果 trigram tokenizer 效果差，可能需要引入 `jieba`（约 20MB），这会打破"零依赖"原则，需要单独决策

---

## 重新评估触发条件

### 向量方案启动条件（满足任一）

1. 平台新增 embedding 端点
2. 项目决定引入本地模型（需评估 2GB 依赖与推理开销）
3. BM25 基线评测显示 Recall@10 低于业务可接受阈值（如 < 60%）
4. 出现大量语义改写型查询（如"电价怎么定"查"电价形成机制"）

### GraphRAG 启动条件（必须全部满足）

1. 政策库 > 500 份
2. BM25 + 向量混合检索已建立且评测完成
3. 出现明确的跨文档关系查询需求
4. 关系表方案被验证为性能瓶颈

### 图数据库启动条件（必须全部满足）

1. GraphRAG 已用关系表实现并验证价值
2. 关系表方案遇到明确的性能或表达力瓶颈
3. 团队具备图数据库运维能力

---

## 参考

- [Microsoft GraphRAG 论文](https://arxiv.org/abs/2404.16130)（访问 2026-11-26）
- [LazyGraphRAG 官方博客](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)（访问 2026-11-26）
- [GraphRAG 增量索引实践](https://www.falkordb.com/reference/graphrag-incremental-knowledge-graph-indexing/)（访问 2026-11-26）
- 项目内证据：`docs/review/stage-01-04-review.md` §4
