# CHANGELOG

本文件记录每个阶段的交付内容与测试基线变化。

---

## [05.5-A] - 2026-11-26

### Hybrid Retrieval（等权 RRF 融合）

**类型**：新功能（阶段05.5 混合检索）

**背景**

05-A/05-B 对照显示词法与向量互补：精确词法 1.00 / 向量 0.83，语义词法 0.00 / 向量 0.83。
需要融合。

**前置核查发现并修复的两个缺陷**

1. `classify_miss()` 逻辑错误：`policy_relation` 分支在「是否命中」判断之前，
   导致分组指标 Recall=1.000 与 miss_breakdown 显示 4 条未命中自相矛盾。
   已移除 `relation_not_built` 分类（它属于能力说明，不属于失败原因）。
2. Embedding 缓存隔离不完整：只校验 schema+model+dim，缺少
   tokenizer 版本、pooling、归一化、编码模式。已引入 `cache_namespace()` 逐项比对。

**新增**

```text
powercopy/rag/hybrid.py                     RRF 融合模块（610 行）
tests/test_hybrid_retrieval.py              63 项测试
scripts/compare_hybrid.py                   三路对照实验
docs/adr/ADR-008-hybrid-rrf.md              决策记录
docs/review/stage05.5a-hybrid-retrieval.md  报告
```

**基线配置（实验前锁定）**

```text
rank_constant        = 60
lexical_candidate_k  = 10
dense_candidate_k    = 10
final_top_k          = 10
weights              = {lexical: 1.0, dense: 1.0}
```

**实验结果**

| 指标 | Lexical | Dense | Hybrid |
|---|---|---|---|
| Recall@1 | 0.3205 | 0.4009 | **0.4138** |
| Recall@5 | 0.4231 | 0.7931 | **0.8700** |
| Recall@10 | 0.4231 | 0.8421 | **0.9190** |
| MRR | 0.4231 | 0.7234 | **0.7811** |
| nDCG@10 | 0.4231 | 0.7315 | **0.7933** |
| Hit Rate | 0.4231 | 0.8846 | **0.9615** |
| Complete Evidence Rate | 0.4231 | 0.7692 | **0.8462** |
| 证据不足用例 | **4/4** | 0/4 | 0/4 |

**H3 成立（5/5 主指标），但不构成「显著提升」**

- 分类型指标全部「持平」——Hybrid 只取两路各自的上限，不产生新召回
- 未做统计显著性检验（30 条用例不足）
- 证据不足仍 0/4（RRF 不判断证据充分性）

**耗时**

```text
Lexical : 2.11 ms
Dense   : 14.16 ms
Hybrid  : 17.20 ms（+21%）
```

**测试基线**：1059 → 1122 passed

**未实现**：Reranker、GraphRAG、Adaptive Router、Weighted RRF、DBSF、Linear Fusion

---

## [05-B] - 2026-11-26

### Dense Retrieval（向量检索）

**类型**：新功能（阶段05 向量检索）

**背景**

05-A BM25 基线显示语义与跨条款查询 Recall@10 为 0，需要向量检索补齐。

**环境核实（实测）**

任务书要求 FastEmbed + ONNX Runtime，实际核实：**两者均未安装**。
已装的是 torch 2.15.0+cpu + transformers 5.24.1。经项目负责人确认，
继续用 torch 路径，不装 ONNX。

**新增**

```text
powercopy/rag/dense.py              向量索引 + 检索 + 缓存（520 行）
tests/test_dense_retrieval.py       46 项测试
scripts/bench_dense.py              性能测量
scripts/compare_retrieval.py        对照实验
docs/adr/ADR-007-dense-retrieval.md 决策记录
docs/review/stage05b-dense-retrieval.md  报告
```

**embedding.py 增强**

- `token_lengths()` / `check_lengths()` 长度查询
- `strict_length=True` 默认禁止静默截断
- `model_fingerprint` 模型指纹

**性能实测**

```text
模型冷启动加载 : 31,320 ms
26 chunk 批量  : 3,240 ms（每条 124.6 ms）
单条 query     : 15.02 ms
相似度计算      : 5.7 μs（26×512 矩阵乘法）
热启动完整检索  : 14.95 ms
模型峰值内存    : 252.6 MB
索引磁盘占用    : 50.5 KB
```

**对照实验结果**

| 指标 | Lexical | Dense | 差值 |
|---|---|---|---|
| Recall@1 | 0.3205 | 0.4009 | +0.0804 |
| Recall@5 | 0.4231 | 0.7931 | +0.3700 |
| Recall@10 | 0.4231 | 0.8421 | +0.4190 |
| MRR | 0.4231 | 0.7234 | +0.3003 |
| nDCG@10 | 0.4231 | 0.7315 | +0.3084 |
| Hit Rate | 0.4231 | 0.8846 | +0.4615 |
| Complete Evidence Rate | 0.4231 | 0.7692 | +0.3461 |
| 证据不足用例 | 4/4 | **0/4** | -4 |

**假设检验**

- H1（semantic+cross_clause > 0.5）: Dense 0.8085 ✅ 成立
- H2（exact_keyword+short_word ≥ 0.8）: Dense 0.8182 ✅ 成立
- H3（混合检索）: 留待下一阶段

**关键发现：两者互补**

| 类型 | Lexical | Dense |
|---|---|---|
| exact_keyword | 1.000 | 0.833 |
| short_word | 1.000 | 0.800 |
| semantic | 0.000 | 0.833 |
| cross_clause | 0.000 | 0.779 |

**已知局限**

- ❌ Dense 对无证据查询必然返回 Top-K（0/4），需独立证据充分性机制
- ❌ 冷启动 31 秒
- ❌ 未做混合检索

**测试基线**：1013 → 1059 passed

---

## [05-A] - 2026-11-26

### BM25 词法检索基线

**类型**：新功能（阶段05 检索基线）

**内容**

用 SQLite 内置 FTS5 trigram + bm25() 建立词法检索索引，产出可量化基线。

**新增**

```text
powercopy/rag/lexical.py                 索引构建 + 检索 + 评测指标（434 行）
tests/test_lexical_search.py             59 项测试
tests/test_retrieval_eval.py             22 项测试
scripts/eval_retrieval.py                评测脚本
evaluations/retrieval-eval-v2.json       30 条评测集（已冻结）
docs/review/stage05a-bm25-baseline.md    基线报告
```

**核心发现：trigram 的两字符限制**

| 查询 | 长度 | 结果 |
|---|---|---|
| 电 | 1 | 0 条 |
| 电价 | 2 | **0 条** |
| 电力市 | 3 | 命中 |

因此必须实现短查询回退（`instr()` 全表扫描）。

**基线指标**

```text
Recall@10      : 0.4231
MRR            : 0.4231
nDCG@10        : 0.4231
命中率          : 0.4231  (11/26)
证据不足用例     : 4/4 正确返回空
```

**分类表现（能力边界）**

| 类型 | Recall@10 | 解读 |
|---|---|---|
| exact_keyword | 1.000 | 词法最强项 |
| short_word | 1.000 | 回退扫描生效 |
| semantic | 0.000 | 语义改写无法覆盖 |
| cross_clause | 0.000 | 跨条款无法综合 |
| policy_relation | 0.000 | 无关系数据 |

**测试基线**：890 → 971 passed

**未做**

- ❌ 向量检索（05-B）
- ❌ 混合检索
- ❌ 中文分词（不引入 jieba）
- ❌ 重排

---

## [05.1-B] - 2026-11-26

### 本地 Embedding 能力

**类型**：新功能（阶段05 前置能力）

**背景**

阶段05 检索需要向量化。平台 `/v1/embeddings` 端点存在但当前 API Key 分组无 embedding 通道。项目负责人决策：本地部署，512 维，不用平台通道。

**新增**

```text
powercopy/rag/embedding.py                 BGEEmbedder + 相似度工具
tests/test_embedding.py                    28 项测试
requirements-embedding.txt                 可选依赖清单
docs/adr/ADR-006-local-embedding.md        决策记录
docs/review/embedding-source-assessment.md 来源核实报告
```

**模型**

```text
BAAI/bge-small-zh-v1.5
参数 24.0M / 512 维 / 91.4 MB / MIT 许可证
```

**实测性能**

```text
单条编码     : 7.4 ms（CPU，i5-8265U）
26 切片总量  : 约 0.2 秒
模型首次加载 : 52 秒（启动成本，之后常驻）
```

**语义抽查**

```text
"电价形成机制" vs "电价是怎么确定的"  → 0.758
"电价形成机制" vs "电力市场注册流程"  → 0.498
近义显著高于无关
```

**新增依赖**

```text
torch 2.15.0+cpu     ~800 MB
transformers 5.24.1  ~100 MB
模型文件              91 MB
```

venv 从 67 MB 增至 876 MB。依赖已隔离到 `requirements-embedding.txt`，未安装时相关测试自动 skip。

**测试基线**：862 → 890 passed

**未做**

- ❌ 向量索引（只有编码能力）
- ❌ 检索接口
- ❌ 与 BM25 的混合
- ❌ 真实召回率评测（需先建评测集）

---

## [04.9] - 2026-11-26

### Tool Result Semantics

**类型**：P0 缺陷修复（架构语义）

**问题**

`ToolResult` 只有一个 `success: bool`，`ToolExecutor` 把所有 handler 异常折叠为 `EXECUTION_ERROR`。业务失败（政策不存在）与执行崩溃（代码缺陷）无法区分，模型拿不到可行动的错误码。4.6 节只能在 handler 里返回错误负载绕过，造成 `success=True` 但业务失败的矛盾。

**解决方案**

三层状态分离：

| 层 | 拥有者 | 取值 |
|---|---|---|
| Execution Status | `ToolExecutor` | `ok` / `tool_not_found` / `invalid_json` / `invalid_arguments` / `execution_error` |
| Business Status | handler | `success` / `empty` / `failed` / `not_applicable` |
| Task Status | `Agent` | 不属于 ToolResult |

核心语义：
- 搜索无命中 = `empty`（正常结果），不是错误
- 业务失败 = `ok` + `failed` + 保留 `business_code`
- 执行崩溃 = `execution_error` + `not_applicable`

### 变更文件

**核心**

```text
powercopy/tools/result.py       重写（20 行 → 176 行）
powercopy/tools/executor.py     重写（117 行 → 170 行）
powercopy/tools/builtin/policy_tools.py  wrap_handler 改造 + 空结果识别
powercopy/core/trace.py         trace_tool 记录两层状态
```

**工作台**

```text
workbench/runtime_api.py        状态直接来自 ToolResult，不再反推
```

**前端**

```text
frontend/src/types/workbench.ts 新增 ExecutionStatus / BusinessStatus 类型
frontend/src/pages/Runtime.tsx  状态文案与配色分层，展示 business_code
```

**测试与验收**

```text
tests/test_tool_result_semantics.py      新增 41 项
tests/test_tool_executor.py              to_dict 断言更新（契约变更）
tests/test_policy_tools_runtime.py       6 处断言更新为新状态字段
scripts/check_tool_result_semantics.py   新增 11 项验收
```

**文档**

```text
docs/adr/ADR-002-tool-result-semantics.md  更新为已采纳并实施
docs/PROJECT_STATE.md                     新增
docs/CHANGELOG.md                         本文件
docs/README.md                            索引更新
```

### 数据契约变更

`ToolResult.to_dict()` 新增 4 个字段：

```json
{
  "execution_status": "ok",
  "business_status": "failed",
  "business_code": "POLICY_VERSION_NOT_FOUND",
  "retryable": false,

  "tool_name": "get_policy_detail",
  "success": true,
  "error_type": null,
  "error_message": "没有找到该 policy_id 与 version 对应记录。",
  "data": null,
  "latency_ms": 1.23
}
```

旧字段全部保留，`success` / `error_type` 为派生 property。

### 测试基线

```text
变更前：820 passed
变更后：862 passed（+41 新增 +1 更新）
回归：全绿
```

### 兼容性

| 维度 | 影响 |
|---|---|
| 旧代码读 `result.success` | ✅ 语义不变 |
| 旧代码读 `result.error_type` | ✅ 旧值映射正确 |
| 前端 `ToolCall` 类型 | ⚠️ 字段类型收窄为联合类型 |
| 数据库 | ✅ 无变化 |
| API 响应结构 | ⚠️ 新增 2 个字段，旧字段保留 |

### 未做

- ❌ Trace 持久化（P1）
- ❌ SQLite WAL（P1）
- ❌ 工具超时（P1）
- ❌ 政策生命周期建模（纳入 05-C 前置）

---

## [05.1] - 2026-11-25

### Document Chunking

**类型**：新功能

**内容**

- 确定性切片，精确字符区间 `[start, end)`
- 自然边界优先（换行 > 句末标点 > 硬切）
- `manifest_sha256` 保证可重复
- PDF 物理页码映射，不跨页

**新增**

```text
powercopy/rag/__init__.py
powercopy/rag/chunking.py
scripts/build_chunks.py
tests/test_chunking.py            21 项
tests/test_chunking_extra.py      44 项
scripts/check_chunking.py         10 项验收
```

**真实数据结果**

```text
NEA 电力市场注册基本规则：6010 字 → 16 切片
NDRC 电力市场运行基本规则：3829 字 → 10 切片
manifest_sha256: ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
```

**测试基线**：730 → 795

---

## [04.8] - 2026-11-25

### PDF 文档摄取

**新增**：`powercopy/data/pdf_ingest.py`、`scripts/ingest_pdf.py`

**能力**：文本型 PDF 逐页提取、物理页码映射、审核指纹、加密/损坏/无文本页拒绝

**重构**：`versioning.py` 抽出 `_ingest_prepared()` 公共事务入口

**测试基线**：666 → 730

---

## [04.6] - 2026-11-25

### 政策工具接入真实数据

**新增**：`powercopy/data/query.py`、`powercopy/tools/builtin/policy_db.py`、`policy_tools.py`

**能力**：真实 SQLite 只读查询、`search_policy` / `get_policy_detail` 工具、Agent 全链路

**测试基线**：601 → 666

---

## [04.5] - 2026-11-25

### 增量采集工作流

**新增**：`powercopy/data/pipeline.py`、`scripts/run_ingestion.py`

**能力**：Pipeline 串联、有限重试、请求预算、403/429 暂停、运行报告

**测试基线**：556 → 601

---

## [04.4] - 2026-11-25

### 去重与版本管理

**新增**：`powercopy/data/versioning.py`、`scripts/ingest_versioned.py`

**能力**：身份/版本/观测三元组、A→B→A 历史、fetch_id 幂等、事务原子性

**测试基线**：512 → 556

---

## [04.3] - 2026-11-25

### 解析与清洗

**新增**：`powercopy/data/parser.py`、`normalizer.py`、`config/parser_profiles.json`

**能力**：编码识别、CSS Selector 配置、正文清洗、日期标准化

**测试基线**：452 → 512

---

## [04.2] - 2026-11-25

### 数据源管理与真实采集

**新增**：`powercopy/data/sources.py`、`fetcher.py`

**能力**：数据源注册、精确域名白名单、禁重定向、错误分类

**测试基线**：394 → 452

---

## [04.1] - 2026-11-25

### 政策文档数据模型与持久化

**新增**：`powercopy/data/models.py`、`repository.py`

**能力**：`PolicyDocument` 严格契约、SQLite 持久化、参数化 SQL

**测试基线**：348 → 394

---

## [01-03] - 2026-11-25

### Agent Runtime 基础

- **01 LLM Client**：OpenAI 兼容客户端、重试、Token 统计、结构化输出
- **02 Tool System**：工具契约、注册表、执行器、Function Calling
- **03 Agent Runtime**：Agent Loop、TaskBudget、ExecutionTrace

**测试基线**：0 → 348
