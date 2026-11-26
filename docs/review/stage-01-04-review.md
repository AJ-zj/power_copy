# PowerCopy V2 前四阶段技术基线复审

**复审日期**：2026-11-26
**复审范围**：阶段01 LLM Client、阶段02 Tool System、阶段03 Agent Runtime、阶段04 Data Center
**代码基线**：commit `4d20cb6`，820 项测试通过
**复审性质**：技术调查与决策记录，不含重构

---

## 摘要

本次复审对照真实源码，逐项检查四个已交付阶段，并与当前主流方案做了对照。核心结论：

**架构方向正确，但有 5 个 P0 级事实性缺口需要优先处理**：

1. **平台无 Embedding 模型** —— 26 个模型全部是对话模型，无一个 embedding 端点。这直接改变了阶段05 的技术选型前提。
2. **ToolExecutor 无法区分"业务失败"与"执行失败"** —— 所有 handler 异常都被折叠为 `EXECUTION_ERROR`，业务错误码只能靠 handler 返回负载绕过。这是设计层面的语义缺陷。
3. **Trace 无持久化** —— 前端可观测中心只显示"当前会话最近一次任务"，刷新即丢失，无法审计历史。
4. **SQLite 未启用 WAL** —— 读写互斥，与已有只读查询/写事务混合使用的场景存在锁冲突风险。
5. **政策生命周期未建模** —— `PolicyDocument` 没有生效日期、失效日期、替代关系字段，无法回答"这条政策现在还有效吗"。

**不建议引入的**：Responses API（第三方兼容性未验证）、完整 GraphRAG（成本与复杂度不匹配当前规模）、重型图数据库。

---

## 一、当前真实代码与最新技术方案对照表

### 1.1 阶段01 LLM Client

| 维度 | 当前实现 | 最新方案 | 差距判断 |
|---|---|---|---|
| 接口协议 | `chat.completions.create` | [Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)（新项目推荐） | **保持现状**。第三方兼容平台对 Responses 支持未验证，迁移风险高 |
| 结构化输出 | 提示词 + `parse_json_content` + 1 次格式重试 | [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) `response_format: json_schema, strict: true` | **待验证后升级**。当前是"提示词约束"，非模型级强制 |
| 供应商耦合 | `openai` SDK 直连，配置来自 `.env` | 多provider 抽象层 | 轻度耦合。`OpenAI(base_url=...)` 是通用做法，但模型名硬编码在环境变量 |
| Streaming | **未实现** | SSE / Responses 事件流 | 缺失。工作台当前一次性收全量结果 |
| Embedding | **未实现** | `/v1/embeddings` | **平台不提供**（见 §4） |
| Tool Calling | ✅ 已实现，`tools=` 传参 | 同左 | 符合标准 |
| 重试 | 自研 `RetryPolicy`，SDK `max_retries=0` | 同左 | 设计正确，避免双重重试 |
| 超时 | 单层 `timeout=60`（SDK 级） | 分层：连接/读取/任务级 | 缺任务级硬截止 |
| Token 统计 | `usage.total_tokens`，缺失时标 `usage_incomplete` | OTel `gen_ai.usage.*` | 语义正确，缺标准字段名 |
| 错误分类 | `APIConnectionError` / `APITimeoutError` / `APIStatusError` 三类 | 更细的 429/5xx/配额区分 | 已区分配额错误，够用 |

### 1.2 阶段02 Tool System

| 维度 | 当前实现 | 最新方案 | 差距判断 |
|---|---|---|---|
| 工具契约 | `@dataclass Tool(name, description, args_schema, handler)` | MCP Tool 定义 | 自研，但与 OpenAI Function Schema 对齐 |
| Schema 生成 | `args_schema.model_json_schema()` | 同左 | 符合标准 |
| 执行成功语义 | `ToolResult.success: bool` | 三段式：传输成功 / 业务成功 / 任务成功 | **语义混淆**（详见 §2.2） |
| 错误分类 | `TOOL_NOT_FOUND` → `INVALID_JSON` → `INVALID_ARGUMENTS` → `EXECUTION_ERROR` | 同左 + 业务错误码 | 优先级正确，但业务错误无法透传 |
| 超时控制 | **无** | 每工具超时 | 缺失 |
| 权限控制 | **无** | 工具级权限/白名单 | 缺失 |
| MCP 扩展 | 未实现 | [MCP 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/index) | 自研协议，未走标准 |
| Progressive Disclosure | 未实现 | 按需加载工具定义 | 当前 2 个工具，无必要 |
| Programmatic Tool Calling | 未实现 | 模型写代码调用工具 | 当前不适用 |

### 1.3 阶段03 Agent Runtime

| 维度 | 当前实现 | 最新方案 | 差距判断 |
|---|---|---|---|
| Loop 终止条件 | 6 种：`completed` / `max_rounds` / `tool_budget_exhausted` / `api_budget_exhausted` / `token_budget_exhausted` / `incomplete_response` / `invalid_response` | 同左 + 显式中止信号 | 覆盖完整 |
| Budget 边界 | `TaskBudget` 在 `run()` 内创建，每任务独立 | 同左 | 设计正确（注释明确说明为何不放在 `__init__`） |
| 并发安全 | `api_attempts += 1` 非原子 | 需要锁或原子操作 | **潜在竞态**（单线程当前无问题） |
| Trace 字段 | `task_id` / `status` / `stop_reason` / `started_at` / `duration_ms` / `budget` / `events[]` | [OTel GenAI semconv](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/) | 字段合理，命名不符标准 |
| Trace 生命周期 | ContextVar，随 `run()` 结束销毁 | 应可导出/持久化 | **无持久化**（详见 §3.2） |
| Checkpoint | **无** | Human-in-the-loop / 恢复 | 缺失 |
| 暂停恢复 | **无** | 同左 | 缺失 |
| 任务持久化 | **无** | 任务表 + 状态机 | 缺失 |
| 可观测数据来源 | 前端 `POST /runs` 的响应 `result.trace` | 查询历史 Trace API | 只显示当前会话最近一次 |

### 1.4 阶段04 Data Center

| 维度 | 当前实现 | 最新方案 | 差距判断 |
|---|---|---|---|
| 采集权限 | 精确域名白名单（注册期 + 请求期双重校验） | 同左 + robots.txt | 边界正确，robots 人工确认 |
| 原始存储 | Base64 存入 JSON + SHA-256 | 同左 | 可追溯 |
| 证据溯源 | 双哈希（raw + content）+ fetch_id | 同左 | 完整 |
| HTML 解析 | BeautifulSoup + CSS Selector 配置 | 同左 | 够用 |
| PDF 解析 | `pypdf` 纯文本提取 | [Docling](https://www.ibm.com/think/news/doclings-rise-llm-ready-data) / [MinerU](https://pypi.org/project/mineru/2.7.0/) | **能力差距大**（见 §5） |
| 表格支持 | **无** | Docling TableFormer | 缺失 |
| OCR | **无** | MinerU 支持 109 种语言 | 缺失 |
| SQLite 使用 | 单文件，`BEGIN IMMEDIATE`，未开 WAL | [WAL 模式](https://www.sqlite.org/forum/forumpost/ab2f360c5010c025?raw) | 见 §6.1 |
| 版本管理 | 身份/版本/观测三元组，A→B→A 完整 | 同左 | 设计优秀 |
| 内容哈希 | 业务字段哈希（不含时间/布局） | 同左 | 正确 |
| 事件幂等 | `fetch_id` 唯一 + 输入指纹 | 同左 | 正确 |
| 政策生命周期 | **未建模** | `effective_from` / `effective_to` / `supersedes` | 缺失（见 §7） |
| 增量索引 | **未实现** | 文档变更事件 | 缺失 |

---

## 二、需要保留 / 需要升级 / 不建议引入

### 2.1 需要保留（架构正确，不要动）

| 组件 | 理由 |
|---|---|
| **三层预算设计**（RetryPolicy → TaskBudget → Agent Loop） | 职责清晰。`TaskBudget` 在 `run()` 内创建的设计有明确注释，防止跨任务复用 |
| **双哈希证据链**（`raw_sha256` + `content_sha256`） | 页脚变化不产生新版本，这个设计让"内容真的变了"和"HTML 变了"可区分 |
| **三元组存储**（identity / version / fetch） | 30 天采集 1 政策 = 1 身份 + 2 版本 + 30 观测，语义精确 |
| **配置驱动 Parser** | 新增数据源只加 JSON 配置，不改代码 |
| **Fail Closed 策略** | 选择器零/多匹配拒绝、编码冲突拒绝、PDF 无文本页拒绝 —— 全部正确 |
| **只读查询双保险**（`mode=ro` + `PRAGMA query_only`） | 模型无法通过工具写库 |
| **`_ingest_prepared` 事务边界** | HTML 与 PDF 共用同一写入逻辑，不调 `repo.add()` 避免跨连接 commit |
| **精确域名白名单** | 注册期 + 请求期 + 响应后三重校验，防子域名伪装 |
| **不编造数据的声明机制** | `notice` 字段每次声明"最新观测 ≠ 法律有效" |

### 2.2 需要升级（有明确收益）

| 项 | 当前 | 目标 | 优先级 |
|---|---|---|---|
| **ToolResult 语义** | `success: bool` 混用传输/业务/任务 | 拆为 `transport_ok` / `business_ok` / 错误分类 | **P0** |
| **Trace 持久化** | 内存，刷新即丢 | 写入 SQLite `agent_runs` 表 | **P0** |
| **SQLite WAL** | 未启用 | `PRAGMA journal_mode=WAL` | **P1** |
| **Structured Outputs** | 提示词约束 | `response_format` + `strict`（需先验证平台） | **P1** |
| **工具超时** | 无 | `ToolExecutor` 加 per-tool timeout | **P1** |
| **OTel 语义对齐** | 自研字段名 | 增加 `gen_ai.*` 映射层 | **P2** |
| **Checkpoint / 恢复** | 无 | 任务状态机 + 断点 | **P2** |
| **Streaming** | 无 | SSE 推送 | **P2** |
| **robots.txt 检查** | 人工 | 自动检查 | **P2** |
| **PDF 表格 / OCR** | 无 | Docling 或 MinerU | **P2** |
| **政策生命周期** | 无 | `effective_from/to` + `supersedes` | **P1** |
| **增量索引事件** | 无 | 文档变更事件表 | **P2** |

### 2.3 不建议引入（收益不足或风险过高）

| 技术 | 不建议理由 |
|---|---|
| **Responses API** | 第三方兼容平台支持未验证。当前 Chat Completions 工作正常，迁移需重写消息与会话管理，收益仅在"官方推荐"。**触发重评条件**：平台 `/models` 返回支持 `responses` 端点，或项目迁移到 OpenAI 直连 |
| **完整 GraphRAG**（Microsoft 原版） | 索引需对每个 chunk 做 LLM 实体抽取 + 社区摘要，成本极高（原版被引用的 $33,000 案例）。当前语料仅 2 份政策，规模完全不匹配。**触发重评条件**：政策库 > 500 份且出现跨文档关系查询需求 |
| **重型图数据库**（Neo4j / NebulaGraph） | 当前数据量用 SQLite 关系表即可表达"政策 A 替代政策 B"。引入图库需要额外运维成本。**触发重评条件**：图查询占比超过总查询 30%，或需要多跳路径分析 |
| **MCP 协议** | 当前 2 个自研工具，引入 MCP 会增加一层协议适配。**触发重评条件**：需要接入第三方工具生态，或工具数 > 10 |
| **向量数据库**（Milvus / Qdrant / Chroma） | **平台无 embedding 模型**（已验证），无法本地生成向量。**触发重评条件**：接入 embedding 服务，或改用本地 embedding 模型（如 BGE-m3） |
| **BM25 库**（rank_bm25 等） | 可先用 SQLite FTS5 实现，无需额外依赖。**触发重评条件**：FTS5 无法满足中文分词需求 |
| **Playwright** | 当前官方页面为静态 HTML，无 JS 渲染需求 |
| **Scrapy** | 少量审核过的目标，无需爬虫框架 |
| **换框架**（LangGraph 等） | 自研 Runtime 仅 239 行，完全可控，无框架锁定的问题。换框架收益不明确 |

---

## 三、优先级与判断依据

### P0 —— 事实性缺口，影响后续设计前提

| 编号 | 问题 | 依据 | 影响 |
|---|---|---|---|
| **P0-1** | ToolExecutor 状态语义混淆 | handler 抛业务异常 → 折叠为 `EXECUTION_ERROR`，错误码丢失 | 阶段05 Agentic RAG 需要工具返回可行动失败原因，当前只能靠 `policy_tools.py` 的返回负载绕过 |
| **P0-2** | Trace 无持久化 | `main.tsx` 的 `result` 是 React state，刷新丢失 | 无法审计、无法评测、无法做增量索引的变更依据 |
| **P0-3** | 平台无 Embedding | `/models` 返回 26 个模型，0 个 embedding | **直接否定"向量检索"路径**，阶段05 必须先解决向量化来源 |
| **P0-4** | 政策生命周期未建模 | `PolicyDocument` 无 `effective_from/to/supersedes` | 无法回答合规问题"这条政策现在有效吗"，是售电业务的核心需求 |
| **P0-5** | SQLite 未启用 WAL | 未发现 `journal_mode` 设置 | 读写互斥，工作台查询与采集写入并发时可能锁超时 |

### P1 —— 有明确收益，成本可控

| 编号 | 问题 | 依据 |
|---|---|---|
| P1-1 | 工具超时控制缺失 | 一个卡死的工具会挂住整个 Agent Loop |
| P1-2 | Structured Outputs 未验证 | 需实测平台是否支持 `response_format`，决定是否放弃"提示词+重试"方案 |
| P1-3 | 任务级硬截止时间缺失 | `timeout=60` 是单次请求超时，不是任务级 |
| P1-4 | Budget 并发安全 | `api_attempts += 1` 非原子，多线程下会少计 |
| P1-5 | robots.txt 人工确认 | 合规风险，上生产前必须自动化 |

### P2 —— 有收益但非阻塞

| 编号 | 问题 |
|---|---|
| P2-1 | OTel GenAI 语义对齐 |
| P2-2 | Checkpoint / 暂停恢复 |
| P2-3 | Streaming 输出 |
| P2-4 | PDF 表格与 OCR（Docling/MinerU） |
| P2-5 | 文档变更事件表（为增量索引准备） |
| P2-6 | 前端可观测页显示历史 Trace |

### P3 —— 长期观察

| 编号 | 问题 |
|---|---|
| P3-1 | MCP 协议适配 |
| P3-2 | GraphRAG / LazyGraphRAG |
| P3-3 | Programmatic Tool Calling |
| P3-4 | 图数据库 |

---

## 四、平台能力核实结果

**探测方式**：只读请求 `GET /v1/models`，不产生付费调用。

```text
base_url: https://www.yituohub.si/v1
HTTP 200 | 模型数: 26
```

**模型清单**（全部为对话模型）：

```text
deepseek-v4-flash / deepseek-v4-flash-0731 / deepseek-v4-pro / deepseek-v4-pro-0813
deepseek-v4.1-flash / deepSeek-v4.1-flash-1017
glm-5.1 / glm-5.2 / glm-5.3 / glm-5.3-flash / glm-5.3-flashx
hy3 / hy4-preview
kimi-k2.7-code / kimi-k3 / kimi-k3.1-pro
mimo-v2.5-pro / mimo-v2.6-flash / mimo-v2.6-pro
MiniMax-M2.7 / MiniMax-M2.7-highspeed / MiniMax-M3 / minimax-m3.1-flash
qwen3.7-max / qwen3.8-flash / qwen3.8-max
```

**单条记录结构**：

```json
{
  "id": "deepseek-v4-flash",
  "object": "model",
  "created": 1626777600,
  "owned_by": "openai",
  "supported_endpoint_types": ["openai"]
}
```

**关键结论**：

1. **无 embedding 模型** —— `embedding/bge/gte/text-embedding` 关键词匹配结果为 0
2. **`supported_endpoint_types: ["openai"]`** —— 平台自称兼容 OpenAI 协议，但**未声明**支持 `responses` 端点
3. **无模型能力元数据** —— 不返回 context window、是否支持 structured outputs、是否支持 vision
4. **`owned_by: "openai"` 是平台填充的占位值** —— 真实供应商并非 OpenAI

**未验证项**（明确标注）：

- ❌ 平台是否支持 `response_format: {"type": "json_schema", "strict": true}`
- ❌ 平台是否支持 `stream: true`
- ❌ 平台是否支持 `/v1/responses` 端点
- ❌ 各模型的实际 context window
- ❌ 各模型对 `tool_choice` 的支持情况

上述均需实际调用验证，本次复审按约束**未发起付费调用**。

---

## 五、PDF 解析能力差距

当前用 `pypdf` 做纯文本提取。对照两个主流方案：

| 能力 | 当前 pypdf | Docling | MinerU |
|---|---|---|---|
| 纯文本提取 | ✅ | ✅ | ✅ |
| 表格结构 | ❌ 展平 | ✅ TableFormer | ✅ |
| 扫描件 OCR | ❌ | 需另配 | ✅ 109 语言 |
| 版面分析 | ❌ | ✅ Layout Model | ✅ |
| 阅读顺序 | ❌ 按内部顺序 | ✅ | ✅ |
| 公式识别 | ❌ | ✅ | ✅ 可开关 |
| 依赖体积 | 极轻 | 重（含模型） | 重（含 torch） |
| 安装复杂度 | `pip install pypdf` | 需模型下载 | 需 CUDA/CPU 配置 |

**判断**：

- 当前 2 份真实政策均为 HTML，PDF 只处理过 1 份合成样本
- 4.5 发现的 15 条候选链接**全部指向 PDF**，一旦启用采集，表格能力会立即成为瓶颈
- 但引入 Docling/MinerU 会带来数百 MB 依赖与显著启动开销

**建议**：P2 阶段引入，优先 Docling（API 更简洁，无 torch 强依赖可配 CPU 模式）。引入前先做**单份真实政策 PDF 的 A/B 对比**，验证表格提取确实带来业务价值。

---

## 六、GraphRAG 目标架构评估

### 6.1 现状与目标差距

当前阶段05 只有 `chunking.py`（164 行），产出确定性切片清单。**没有检索层**。

目标架构「Adaptive Graph-enhanced RAG」包含 6 个组件：

| 组件 | 当前 | 前置依赖 |
|---|---|---|
| 传统混合检索（BM25 + 向量） | ❌ | **需要 embedding 来源** |
| 领域知识图谱 | ❌ | 需要实体抽取（LLM） |
| 图检索 | ❌ | 需要图存储 |
| 自适应检索路由 | ❌ | 需要检索基线对照 |
| Agentic RAG | ❌ | 需要工具化的检索接口 |
| 增量索引 | ❌ | 需要文档变更事件 |

### 6.2 关键阻塞点

**平台无 embedding 模型** 是最大的架构约束。这意味着：

- 无法用平台 API 生成向量
- 只有三条路：① 接入本地 embedding 模型（如 BGE-m3，需下载 ~2GB）；② 接入第三方 embedding API（额外成本与依赖）；③ 先只做 BM25（SQLite FTS5），向量留待后续

**建议路线**：阶段05 先用 **SQLite FTS5 做 BM25 基线**，不引入任何新依赖，建立评测集后再决定是否引入向量。

### 6.3 GraphRAG 的成本现实

参考公开数据：

- 微软原版 GraphRAG：索引阶段每个 chunk 都要 LLM 抽取实体，再对每个社区做摘要，成本极高
- [LazyGraphRAG](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)：把摘要推迟到查询期，索引成本降到原版 0.1%，查询成本低 700 倍
- 但 LazyGraphRAG 主要集成在 Azure 产品中，开源库支持有限

**对当前项目的判断**：语料 2 份政策，GraphRAG 的任何变体都是过度设计。**真正的业务价值在于「政策替代关系」**（政策 A 废止政策 B），这用 SQLite 关系表加一条 `supersedes` 外键就能表达，不需要图数据库。

### 6.4 增量索引的前提

图数据库厂商 FalkorDB 的工程文章指出，增量索引需要两个基础设施：

1. 每个源文档有内容哈希或版本指纹
2. 图上每个节点和边带来源溯源，能回指到产生它的文档

**PowerCopy V2 已具备第 1 项**（`content_sha256`），**缺第 2 项**（没有变更事件表）。

这是一个好消息：增量索引的地基已经打好一半。补上"文档变更事件表"是 P2 级工作，不需要推翻现有设计。

---

## 七、前端影响分析

### 7.1 当前前端架构

```text
React 19 + Vite + TypeScript，hash 路由，无状态管理库
10 个页面，数据全部来自 /api/workbench
```

### 7.2 各 P0/P1 项对前端的影响

| 改进项 | 前端影响 | 工作量 |
|---|---|---|
| **Trace 持久化** | 可观测页需增加"历史任务列表"和"按 task_id 查询" | 中（新增 1 个页面区块 + 2 个 API 调用） |
| **ToolResult 语义拆分** | 前端展示工具结果时需区分 `transport_ok`/`business_ok` | 小（`types/workbench.ts` 加字段，展示层调整） |
| **工具超时** | 无界面变化 | 无 |
| **WAL 模式** | 无界面变化 | 无 |
| **政策生命周期字段** | 政策详情页需增加"生效日期/失效日期/被替代关系"区块 | 中（`PoliciesPage.tsx` 扩展） |
| **Structured Outputs** | 无界面变化 | 无 |
| **BM25 检索** | 政策库搜索可增加"检索模式"切换 | 小 |

### 7.3 结论

**所有 P0/P1 改进都不需要重构前端**。当前架构的优势：

- `api/client.ts` 统一封装，新增 API 只需加一个函数
- `types/workbench.ts` 集中类型定义，后端字段变化改一处
- hash 路由，新增页面只需加一个 `else if`

**风险点**：`Observability` 组件通过 props 接收 `result`（React state），改成从 API 拉取历史 Trace 会改变数据流。这是**唯一需要小范围调整数据流向**的地方。

---

## 八、研发路线调整

### 8.1 原定第五阶段的问题

原计划直接做「Adaptive Graph-enhanced RAG」，但复审发现三个前提不成立：

1. 无 embedding 来源
2. 无检索基线（无法评测 GraphRAG 是否真的更好）
3. 无历史 Trace（无法做评测记录）

### 8.2 调整后的路线

```text
阶段05-A  检索基线（必须先做）
  ├─ SQLite FTS5 全文索引（零新依赖）
  ├─ BM25 排序
  ├─ 检索评测集（人工标注 query→relevant chunks）
  └─ 产出：可量化的基线指标

阶段05-B  向量检索（有条件后做）
  ├─ 前置：解决 embedding 来源（本地模型 or 第三方 API）
  ├─ 混合检索（BM25 + 向量加权）
  └─ 产出：与基线的对照数据

阶段05-C  领域知识图谱（规模够大后做）
  ├─ 前置：政策库 > 500 份
  ├─ 实体抽取：政策、发文机关、地区、政策类型
  ├─ 关系建模：supersedes / amends / references
  └─ 产出：关系查询能力（未必需要图数据库）

阶段05-D  自适应路由 + Agentic RAG
  ├─ 前置：05-A/B/C 的评测数据
  ├─ 按 query 类型路由到不同检索器
  └─ Agent 自主决定检索策略
```

### 8.3 与 P0/P1 的依赖关系

```text
P0-3 (embedding) ─────► 05-B
P0-4 (生命周期) ─────► 05-C 的关系建模
P0-2 (Trace 持久化) ─► 评测数据记录
P0-1 (Tool 语义) ────► 05-D Agentic RAG 的工具接口
P1-2 (Struct Outputs) ► 实体抽取的结构化保证
```

**结论**：P0/P1 不是"技术债清理"，而是**阶段05 的前置条件**。

---

## 九、参考资料

| 主题 | 链接 | 访问日期 |
|---|---|---|
| OpenAI Responses API 迁移 | https://developers.openai.com/api/docs/guides/migrate-to-responses | 2026-11-26 |
| OpenAI Structured Outputs | https://developers.openai.com/api/docs/guides/structured-outputs | 2026-11-26 |
| OpenTelemetry GenAI 语义约定 | https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/ | 2026-11-26 |
| OTel GenAI Agent Spans | https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md | 2026-11-26 |
| MCP 规范 2025-11-25 | https://modelcontextprotocol.io/specification/2025-11-25/index | 2026-11-26 |
| Docling 项目介绍 | https://www.ibm.com/think/news/doclings-rise-llm-ready-data | 2026-11-26 |
| Docling TableFormer 模型 | https://pypi.org/project/docling-ibm-models/ | 2026-11-26 |
| MinerU 更新日志 | https://pypi.org/project/mineru/2.7.0/ | 2026-11-26 |
| Microsoft GraphRAG 论文 | https://arxiv.org/abs/2404.16130 | 2026-11-26 |
| LazyGraphRAG 官方博客 | https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/ | 2026-11-26 |
| GraphRAG 增量索引工程实践 | https://www.falkordb.com/reference/graphrag-incremental-knowledge-graph-indexing/ | 2026-11-26 |
| SQLite WAL 并发机制 | https://www.sqlite.org/forum/forumpost/ab2f360c5010c025?raw | 2026-11-26 |
| SQLite WAL 实测数据 | https://bbs.huaweicloud.com/blogs/508031 | 2026-11-26 |

---

## 十、未验证的假设与实现状态

### 10.1 模型接口未验证

| 项 | 状态 | 验证方式 |
|---|---|---|
| `response_format: json_schema` | **未验证** | 需一次真实调用，检查是否返回 400 |
| `strict: true` 模式 | **未验证** | 同上 |
| `/v1/responses` 端点 | **未验证** | `GET /v1/responses` 探测 |
| `stream: true` | **未验证** | 需一次流式调用 |
| 各模型 context window | **未知** | 平台未提供元数据 |
| `tool_choice` 强制调用 | **未验证** | 需实测 |
| Embedding 端点 | **已确认不存在** | `/models` 列表无 embedding 模型 |

### 10.2 技术假设

| 假设 | 依据 | 风险 |
|---|---|---|
| 平台兼容 OpenAI 协议 | `supported_endpoint_types: ["openai"]` | 中。字段是平台自述，非强制保证 |
| `glm-5.3-flash` 支持 tool calling | 4.6 节实测通过 | 已验证 |
| SQLite 足够支撑到 1000 份政策 | 未实测 | 低。当前 2 份，量级差距大但 SQLite 能力远超此规模 |
| `pypdf` 能处理所有文本型 PDF | 仅测试 1 份合成样本 | **高**。真实政府 PDF 可能有复杂版面 |

### 10.3 实现状态

| 章节 | 状态 |
|---|---|
| 阶段01-04 源码检查 | ✅ 全部读过 |
| 平台模型清单 | ✅ 只读探测完成 |
| embedding 缺失 | ✅ 已确认 |
| 模型细粒度能力 | ⚠️ 未验证（受"不调用付费 API"约束） |
| 真实 PDF 解析质量 | ⚠️ 未验证（无授权样本） |
| GraphRAG 成本估算 | ⚠️ 引用公开数据，未在本地实测 |

---

## 十一、本次复审未做的事

明确记录边界：

- ❌ 未修改任何业务代码
- ❌ 未修改数据库结构
- ❌ 未引入任何新依赖
- ❌ 未调用任何付费 API
- ❌ 未验证模型细粒度能力（受约束）
- ❌ 未做 PDF 解析 A/B 测试（无授权样本）
- ❌ 未重构 ToolExecutor / Trace / Budget

**所有建议均待项目负责人确认后再执行。**
