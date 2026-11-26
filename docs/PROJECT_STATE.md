# PROJECT_STATE

**最后更新**：2026-11-26（05.5-A 混合检索完成后）
**当前阶段**：阶段04 完成，阶段05 进行中（05.1 切片 + Embedding + 05-A 词法 + 05-B 向量 + 05.5-A 混合检索）
**代码基线**：1122 项测试通过

---

## 一、已完成阶段

| 阶段 | 内容 | 状态 | 测试 |
|---|---|---|---|
| 01 | LLM Client | ✅ 已验收 | — |
| 02 | Tool System | ✅ 已验收 | — |
| 03 | Agent Runtime | ✅ 已验收 | — |
| 04.1 | 政策文档数据模型与持久化 | ✅ 已验收 | 46 |
| 04.2 | 数据源管理与真实采集 | ✅ 已验收 | 58 |
| 04.3 | 解析与清洗 | ✅ 已验收 | 60 |
| 04.4 | 去重与版本管理 | ✅ 已验收 | 44 |
| 04.5 | 增量采集工作流 | ✅ 已验收 | 45 |
| 04.6 | 政策工具接入真实数据 | ✅ 已验收 | 65 |
| 04.8 | PDF 文档摄取 | ✅ 已验收 | 64 |
| 04.9 | Tool Result Semantics | ✅ **已验收** | 41 |
| 05.1 | Document Chunking | ✅ 已验收 | 65 |
| 05.1-B | 本地 Embedding 能力 | ✅ 已验收 | 28 |
| 05-A | BM25 词法检索基线 | ✅ 已验收 | 81 |
| 05-B | Dense 向量检索 | ✅ 已验收 | 46 |
| 05.5-A | Hybrid 混合检索（RRF） | ✅ **本次完成** | 63 |

**测试总数**：1122 passed

---

## 二、05.1-B 交付摘要（Embedding）

### 决策链

1. 复审发现 `/v1/models` 无 embedding 模型
2. 只读探测确认 `/v1/embeddings` **端点存在**，但当前 API Key 分组无通道
3. 项目负责人决策：**允许安装 CPU 版 PyTorch**，**不使用平台通道**，**512 维（暂不考虑 1024）**
4. 实施：本地 BGE-small-zh-v1.5

### 模型

```text
BAAI/bge-small-zh-v1.5
参数 24.0M / 512 维 / 91.4 MB / MIT
单条编码 7.4 ms（CPU）
```

### 新增文件

```text
powercopy/rag/embedding.py                 编码器 + 相似度工具
tests/test_embedding.py                    28 项
requirements-embedding.txt                 隔离的重型依赖
docs/adr/ADR-006-local-embedding.md        决策记录
docs/review/embedding-source-assessment.md 核实报告
```

### 依赖影响

| 项 | 变化 |
|---|---|
| venv 大小 | 67 MB → **876 MB** |
| 模型文件 | 91 MB（`storage/models/`，已 gitignore） |
| 主 requirements.txt | **未变**（依赖隔离到 `requirements-embedding.txt`） |

### 能力边界

- ✅ 文本 → 512 维归一化向量
- ✅ 查询/文档前缀分离（BGE 官方用法）
- ✅ 余弦相似度与排序
- ❌ 无向量索引
- ❌ 无检索接口
- ❌ 未与 BM25 混合

---

## 二-A、04.9 交付摘要（Tool Result Semantics）

### 解决的 P0 问题

**P0-1 ToolResult 状态语义混淆** —— 已修复。

旧实现把业务失败与执行崩溃都折叠为 `EXECUTION_ERROR`，模型拿不到可行动的失败原因。

### 三层状态分离

| 层 | 拥有者 | 取值 |
|---|---|---|
| Execution Status | `ToolExecutor` | `ok` / `tool_not_found` / `invalid_json` / `invalid_arguments` / `execution_error` |
| Business Status | handler | `success` / `empty` / `failed` / `not_applicable` |
| Task Status | `Agent` | 不属于 `ToolResult` |

### 核心语义

- **搜索无命中 = `empty`**（正常结果），不是 `failed`
- **业务失败 = `ok` + `failed` + 保留 `business_code`**
- **执行崩溃 = `execution_error` + `not_applicable`**

### 改动文件

```text
powercopy/tools/result.py                重写
powercopy/tools/executor.py              重写
powercopy/tools/builtin/policy_tools.py  wrap_handler 改造
powercopy/core/trace.py                  trace_tool 增加两层状态
workbench/runtime_api.py                 读状态字段而非反推
frontend/src/types/workbench.ts          增加状态类型
frontend/src/pages/Runtime.tsx           状态展示与配色
tests/test_tool_result_semantics.py      新增 41 项
scripts/check_tool_result_semantics.py   新增 11 项验收
```

---

## 三、阶段05 状态

### 已验收

**05.1 Document Chunking** —— 65 项测试通过。

产出 `policy-chunk-manifest-v1` 清单：

```text
真实 HTML 政策：2 份
切片总数：26
manifest_sha256: ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
```

能力：
- ✅ 精确字符区间 `[start, end)`
- ✅ 自然边界优先（换行 > 句末标点 > 硬切）
- ✅ 非空白字符全覆盖
- ✅ 重复构建确定性
- ✅ PDF 物理页码映射
- ✅ 配置/版本变化时 chunk_id 更新

**05.1-B 本地 Embedding** —— 28 项测试通过。

```text
BAAI/bge-small-zh-v1.5，24M 参数，512 维，MIT
单条编码 7.4 ms（CPU）
```

能力：
- ✅ 文本 → 512 维归一化向量
- ✅ 查询/文档前缀分离
- ✅ 余弦相似度与排序
- ❌ 无向量索引、无检索接口

**05-A BM25 词法检索基线** —— 81 项测试通过（59 + 22）。

用 FTS5 trigram + bm25()，零新增依赖。

```text
语料       : 26 切片 / 2 份政策
评测集     : 30 条（evaluations/retrieval-eval-v2.json，已冻结）
Recall@10  : 0.4231
MRR        : 0.4231
nDCG@10    : 0.4231
命中率      : 0.4231 (11/26)
空结果用例  : 4/4 正确返回空
```

**关键发现**：trigram 需 >= 3 字符，2 字符查询命中 0 条，因此实现了 `instr()` 回退扫描。

**能力边界**：

| 类型 | Recall@10 | 解读 |
|---|---|---|
| exact_keyword | 1.000 | 词法最强项 |
| short_word | 1.000 | 回退扫描生效 |
| semantic | 0.000 | 语义改写无法覆盖 |
| cross_clause | 0.000 | 跨条款无法综合 |
| policy_relation | 0.000 | 无关系数据 |

这组数字清晰划出了 BM25 的边界：**词法 100%，语义 0%**。

限制：
- ❌ 无中文分词（不引入 jieba）
- ❌ 无同义词扩展
- ❌ 无重排

### 未启动

- 05-B 向量检索
- 05-C 政策关系增强
- 05-D 自适应路由 / Agentic RAG
- 05-E 增量索引

详见 `docs/roadmap-stage05.md`。

---

## 四、待决策事项（P0 之外的调整）

根据项目负责人 2026-11-26 的确认：

| 项 | 原优先级 | 调整后 | 说明 |
|---|---|---|---|
| P0-1 Tool 语义 | P0 | **已实施** | 04.9 完成 |
| P0-2 Trace 持久化 | P0 | **P1** | 暂不阻塞 RAG 开发 |
| P0-3 Embedding | P0 | **已实施** | ADR-006：本地 BGE-small-zh-v1.5，512 维 |
| P0-4 政策生命周期 | P0 | **纳入 05-C 前置设计** | 暂不改 PolicyDocument 和数据库 |
| P0-5 SQLite WAL | P0 | **P1** | 先查版本与 WAL-Reset Bug 状态 |

### 中文检索方案（已批准）

- 采用 **SQLite FTS5 trigram** 作为初始词法检索基线
- **必须设计少于 3 字符查询的回退机制**
- 暂不引入 jieba
- 后续由真实评测决定是否加分词与行业词典

### 向量化来源（已批准方向）

- 优先**本地 Embedding 模型**
- 候选：BGE-small-zh-v1.5、BGE-M3、Qwen3-Embedding-0.6B
- **先核实 CPU/GPU/内存/磁盘/依赖/许可证**，未确认前不下载大型模型
- 小规模数据优先 NumPy 精确检索，不强制向量数据库

### 05-B 实际采用（已实施）

| 项 | 采用方案 |
|---|---|
| 推理后端 | torch 2.15.0+cpu + transformers 5.24.1（**不是** FastEmbed+ONNX） |
| 模型 | BAAI/bge-small-zh-v1.5，commit `7999e1d`，512 维，MIT |
| 检索方式 | NumPy 精确余弦相似度（26×512 矩阵乘法 5.7 μs） |
| 向量数据库 | **未引入** |
| 证据充分性阈值 | **未设置**（4 条空用例不足以标定） |

**与任务书不一致之处**：任务书要求 FastEmbed + ONNX Runtime，实际环境两者均未安装，
经项目负责人确认改用已装的 torch 路径。详见 ADR-007。

### 政策关系抽取（已批准方向）

- 规则抽取 + LLM 辅助 + 人工验证
- 候选关系必须绑定**原始政策版本和证据位置**
- 未验证的高风险关系**不得进入正式政策推理链路**
- 先用 SQLite 关系表，不引入图数据库

### 评测集（已批准）

- 首批 30 条测试问题
- 覆盖：精确关键词、短词、语义、跨条款、政策关系、证据不足
- Codex 辅助生成候选，项目负责人核实，专业解释由业务人员审核
- **正式评测前冻结版本**
- 保留原文证据位置，不仅依赖 Chunk ID

---

## 五、技术边界（当前仍存在的限制）

### 数据采集

- ❌ robots.txt 自动检查（人工确认）
- ❌ 持续自动化采集授权
- ❌ 完整 SSRF 防护
- ❌ 定时增量调度

### 文档处理

- ❌ OCR（扫描件、图片型 PDF）
- ❌ 复杂表格结构
- ❌ 多栏阅读顺序
- ✅ 文本型 PDF 逐页提取

### 检索

- ❌ 向量数据库
- ❌ BM25 / 混合检索
- ❌ 增量索引
- ❌ 页码级查询接口

### 系统

- ❌ Trace 持久化
- ❌ Checkpoint / 暂停恢复
- ❌ 任务级硬截止时间
- ❌ 工具超时控制
- ❌ API 认证鉴权

---

## 六、验证状态

| 项 | 状态 | 方式 |
|---|---|---|
| 全量回归 | ✅ 1122 passed | `python -m pytest tests -q` |
| 04.9 验收 | ✅ 11/11 | `python scripts/check_tool_result_semantics.py` |
| 05.1 验收 | ✅ 10/10 | `python scripts/check_chunking.py` |
| 源码未越界改动 | ✅ | 19 个核心模块哈希记录 |
| 数据库结构未变 | ✅ | 4 张表结构一致 |
| 新增依赖 | ✅ 0 | requirements.txt 未改 |

### 未验证

- ❌ 模型 `response_format` / `strict` / `/v1/responses` / `stream` 支持
- ❌ 真实政策 PDF 解析质量（无授权样本）
- ❌ 前端 TypeScript 类型检查（环境无 node/pnpm）

---

**下一步**：等待项目负责人验收 04.9，确认后进入阶段05 下一项任务。
