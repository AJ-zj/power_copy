# PowerCopy V2

> 面向售电行业政策研究与文案生产的可追溯 Agent 运行时。

PowerCopy V2 把「官方政策采集 → 解析清洗 → 版本化存储 → 切片索引 → Agent 检索生成」串成一条可审计的数据链路。每一个事实性输出都能回溯到原始网页字节、采集时间与内容哈希。

当前状态：**1122 项自动化测试全部通过**，前端运营工作台已接入真实数据中心，检索链路（词法 / 向量 / 混合）已就绪并有可复现的对照实验。

---

## 目录

```text
PowerCopy-V2/
├── powercopy/
│   ├── core/          # Agent 核心：配置、消息、LLM、重试、预算、Trace、Agent Loop
│   ├── tools/         # 工具契约、注册表、执行器、内置政策工具
│   ├── data/          # 数据中心：数据源、采集、解析、清洗、版本化存储、PDF 摄取、查询
│   ├── rag/           # RAG：切片、Embedding、词法检索、向量检索、混合融合
│   ├── api/           # FastAPI 入口
│   ├── memory/        # 短期与长期记忆（骨架）
│   ├── context/       # 上下文工程（骨架）
│   ├── skills/        # 业务技能（骨架）
│   ├── agents/        # 业务 Agent（骨架）
│   ├── harness/       # 运行保障（骨架）
│   └── evaluation/    # 评测中心（骨架）
├── workbench/         # 运营工作台后端：数据中心 API + Agent 运行时 API
├── frontend/          # 运营工作台前端：React 19 + Vite + TypeScript
├── config/            # 解析配置、列表页选择器、审批模板
├── scripts/           # 采集、解析、入库、切片、验收脚本
├── tests/             # 自动化测试（1122 项）
├── docs/              # 交付文档与页面截图
├── prompts/           # Prompt 版本管理
├── datasets/          # 评测数据集
├── storage/           # 运行时数据（不入库）
├── .env.example
├── pyproject.toml
└── README.md
```

---

## 快速开始

### 后端

```powershell
cd PowerCopy-V2
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]" -e ".[api]"
pip install -r requirements.txt
copy .env.example .env    # 填入自己的配置
python -m pytest tests -q
uvicorn powercopy.api.main:app --reload --port 8000
```

### 工作台（前端 + 后端）

```powershell
# 后端：挂载工作台 API
uvicorn workbench.main:app --reload --port 8000

# 前端：另开一个终端
cd frontend
pnpm install
pnpm dev        # http://127.0.0.1:5173
```

打开 http://127.0.0.1:8000/health 与 http://127.0.0.1:8000/docs。

> `workbench/main.py` 启动时会校验 `POWERCOPY_PROJECT_ROOT` 指向的项目是否含 `powercopy/core/trace.py`。默认指向自身目录，无需额外配置。

---

## 数据中心链路

```text
官方数据源
   │  Source Registry（精确域名白名单）
   ▼
Fetcher ─────► Raw JSON（原始字节 + Base64 + SHA-256）
   │
   ▼
Parser / PDF Ingest ──► PolicyDocument（严格数据契约）
   │
   ▼
VersionedPolicyStore（身份 / 版本 / 观测三元组）
   │
   ├──► PolicyQueryService（只读查询，供 Agent 工具调用）
   └──► Chunking（可追溯切片）
          │
          ├──► Lexical（FTS5 trigram + BM25 打分）
          ├──► Dense（BGE-small-zh-v1.5，512 维，NumPy 精确检索）
          └──► Hybrid（等权 RRF，rank_constant=60）
```

### 阶段04 · Data Center（1–8 节，已验收）

| 节 | 内容 | 关键产出 |
|---|---|---|
| 4.1 | 政策文档数据模型与持久化 | `PolicyDocument`、`PolicyRepository`、SQLite |
| 4.2 | 数据源管理与真实采集 | `SourceRegistry`、`HttpFetcher`、禁止重定向与代理 |
| 4.3 | 解析与清洗 | `ParseProfile`、`parse_article`、配置驱动 CSS Selector |
| 4.4 | 去重与版本管理 | `VersionedPolicyStore`、内容哈希、A→B→A 历史保留 |
| 4.5 | 增量采集工作流 | `IngestionPipeline`、有限重试、请求预算、403/429 暂停 |
| 4.6 | 政策工具接入真实数据 | `PolicyQueryService`、`search_policy` / `get_policy_detail` |
| 4.8 | PDF 文档摄取 | `pdf_ingest`、逐页提取、物理页码映射、审核指纹 |

### 阶段05 · RAG（已验收）

| 节 | 内容 | 关键产出 |
|---|---|---|
| 5.1 | Document Chunking | `build_manifest`、精确字符区间、`manifest_sha256`、PDF 不跨页 |
| 5.1-B | 本地 Embedding | `BGEEmbedder`、BGE-small-zh-v1.5、512 维、CLS 池化 + L2、禁止静默截断 |
| 5-A | 词法检索基线 | `LexicalSearch`、FTS5 trigram + BM25、短查询 `instr` 回退、Recall@10 基线 |
| 5-B | 向量检索 | `DenseSearch`、NumPy 精确余弦、索引指纹校验、向量缓存 |
| 5.5-A | 混合检索 | `HybridRetriever`、等权 RRF、双路贡献可审计、降级状态显式 |

检索效果（开发集 30 条，未做统计显著性检验）：

| 指标 | Lexical | Dense | Hybrid |
|---|---|---|---|
| Recall@10 | 0.4231 | 0.8421 | **0.9190** |
| MRR | 0.4231 | 0.7234 | **0.7811** |
| Complete Evidence Rate | 0.4231 | 0.7692 | **0.8462** |
| 证据不足用例正确返回空 | **4/4** | 0/4 | 0/4 |

> 词法与向量**互补**：精确关键词词法 1.00 / 向量 0.83，语义改写词法 0.00 / 向量 0.83。
> 混合检索取两路各自上限，不产生新的召回能力；**RRF 不判断证据充分性**。

决策记录见 `docs/adr/`，实验报告见 `docs/review/`。

---

## 核心设计

### 事实可追溯

每个入库政策都绑定双重哈希：

- `raw_sha256` —— 原始网页/PDF 字节的哈希
- `content_sha256` —— 标准化正文字段的业务哈希

采集事件（`policy_fetches`）与文档版本（`policy_versions`）分离，因此「采集 30 次、内容改 2 次」会记录为 **1 个身份 + 2 个版本 + 30 条观测**。

### 只读查询

`PolicyQueryService` 用 `mode=ro` + `PRAGMA query_only=ON` 打开数据库，参数化 SQL，模型无法传入路径或 SQL 片段。

### 失败关闭

- 选择器零匹配或多匹配 → 拒绝解析，不猜
- 编码声明冲突 → 拒绝解码，不静默丢弃字节
- PDF 加密或无文本页 → 拒绝摄取
- 审核指纹不匹配 → 拒绝入库

### 不编造数据

- 未标注地区的政策不等于「全国适用」
- 「最新观测版本」不等于「当前法律有效」
- 无结果不等于政策不存在

以上三点在查询结果的 `notice` 字段中每次都会声明。

---

## 脚本

### 采集与解析

```powershell
python -m scripts.fetch_policy ndrc market_rules
python -m scripts.parse_policy inspect storage/raw/<id>.json
python -m scripts.parse_policy parse storage/raw/<id>.json --show-body
python -m scripts.parse_policy parse storage/raw/<id>.json --commit --db <db>
```

### 版本化入库

```powershell
python -m scripts.ingest_versioned --db <db> ingest storage/raw/<id>.json
python -m scripts.ingest_versioned --db <db> counts
python -m scripts.ingest_versioned --db <db> history <policy_id>
```

### PDF 摄取

```powershell
python -m scripts.ingest_pdf import approved.pdf --metadata config/pdf_metadata.json
python -m scripts.ingest_pdf preview storage/raw_pdf/<fetch_id>.json --show-body
python -m scripts.ingest_pdf commit storage/raw_pdf/<fetch_id>.json --approve-sha256 <hash> --db <db>
```

### 查询与切片

```powershell
python -m scripts.query_policy --db <db> search 电力 --limit 3
python -m scripts.query_policy --db <db> detail <policy_id> --version 1
python -m scripts.build_chunks --db <db> --out storage/rag/chunks.json
```

### 检索索引与评测

```powershell
# 构建词法索引（FTS5 trigram，零依赖）
python scripts/eval_retrieval.py --rebuild

# 构建向量索引（需要 requirements-embedding.txt）
python -c "from powercopy.rag.dense import build_dense_index; print(build_dense_index('storage/rag/chunks.json','storage/rag/dense.npz',cache_path='storage/rag/embedding-cache.json'))"

# 三路对照实验
python scripts/compare_hybrid.py --json storage/rag/compare-hybrid.json
python scripts/compare_hybrid.py --show-cases --sweep

# 性能测量
python scripts/bench_dense.py
```

> 向量检索依赖可选包：`pip install torch --index-url https://download.pytorch.org/whl/cpu` 后
> `pip install -r requirements-embedding.txt`。未安装时相关测试自动 skip，其余功能不受影响。
> 模型 `BAAI/bge-small-zh-v1.5`（91 MB，MIT）首次运行自动下载到 `storage/models/`（不入库）。

### 验收脚本

每个阶段都有对应的离线验收脚本，全部不联网：

```powershell
python scripts/check_policy_repository.py   # 4.1
python scripts/check_fetcher.py --live      # 4.2
python scripts/check_parser.py --real       # 4.3
python scripts/check_versioning.py --real   # 4.4
python scripts/check_pipeline.py --real     # 4.5
python scripts/check_policy_tools.py        # 4.6
python scripts/check_pdf_ingest.py          # 4.8
python scripts/check_chunking.py            # 5.1
python scripts/check_tool_result_semantics.py  # 4.9
```

完整索引见 `scripts/README.md`。

---

## 运营工作台

前端提供 10 个页面，全部对接真实后端数据：

| 页面 | 功能 |
|---|---|
| Overview | 数据总览 |
| Runtime | Agent 运行时与工具调用实验台 |
| Policies | 政策库检索与详情 |
| Versions | 版本历史与差异对比 |
| Sources | 数据源注册表 |
| Fetch Records | 采集记录归档 |
| Raw Archive | 原始采集文件浏览 |
| Ingestion | 采集流水线运行与报告 |
| Tool Registry | 工具清单与 Schema |
| Observability | 执行轨迹与预算观测 |

页面截图见 `docs/screenshots/`，交付说明见 `docs/workbench.md`。

---

## 环境变量

复制 `.env.example` 为 `.env`，`.env` 永远不进版本库。

```ini
# 旧版配置（core/llm.py 直接读取）
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL=
LLM_TIMEOUT=60

# 新版配置（core/config.py 读取，供 API/工作台使用）
POWERCOPY_ENV=development
POWERCOPY_LLM_BASE_URL=
POWERCOPY_LLM_MODEL=
POWERCOPY_LLM_API_KEY=
POWERCOPY_LLM_TIMEOUT=90
POWERCOPY_TAVILY_API_KEY=
POWERCOPY_DATA_DIR=workspace/data

# 政策库路径（只读工具使用）
POLICY_DB_PATH=storage/policies_versioned.db
```

> **注意**：`core/llm.py` 读 `LLM_*`，`core/config.py` 读 `POWERCOPY_*`。两套并存，接入时按模块区分。

---

## 测试

```powershell
python -m pytest tests -q
```

```text
1122 passed
```

| 测试组 | 覆盖 |
|---|---|
| core | LLM 客户端、重试、预算、Trace、Agent Loop、消息管理 |
| tools | 工具定义、注册表、执行器、Function Calling、三层状态语义 |
| data（4.1） | 数据契约、SQLite 读写、参数化 SQL |
| data（4.2） | 数据源校验、域名白名单、错误分类、禁重定向 |
| data（4.3） | 编码识别、CSS Selector、正文清洗、真实 HTML |
| data（4.4） | 身份/版本/观测三元组、事务回滚、并发 |
| data（4.5） | Pipeline 串联、重试退避、预算、暂停策略 |
| data（4.6） | 真实库查询、只读、注入防护、Agent 全链路 |
| data（4.8） | PDF 逐页提取、页码映射、审核指纹 |
| rag（5.1） | 切片精确性、覆盖完整性、确定性与身份更新 |
| rag（5.1-B） | 向量维度、L2 归一化、查询/文档前缀分离、Token 长度拒绝静默截断 |
| rag（5-A） | 索引构建、BM25 排序方向、短查询回退、查询转义、只读保证、评测指标 |
| rag（5-B） | 向量索引、数值校验、缓存命名空间隔离、索引指纹一致性 |
| rag（5.5-A） | RRF 公式、双路累加、去重、同分稳定、异常降级、非法参数 |

> 真实数据测试会自动跳过缺失的文件，因此首次 clone 后部分测试会 skip。要跑完整测试，需先执行一次采集或使用仓库自带的合成 PDF 样本。

---

## 技术边界

本节记录**明确的未实现范围**，避免把工程骨架误认为完整产品。

### 数据采集

- ❌ robots.txt 自动检查（当前靠人工确认）
- ❌ 持续自动化采集授权（仅为工程技术验证）
- ❌ 完整 SSRF 防护（属 Harness 阶段）
- ❌ 定时增量调度
- ✅ 有限重试、退避、请求预算、403/429 暂停

### 文档处理

- ❌ OCR（扫描件、图片型 PDF 无法处理）
- ❌ 复杂表格结构保留
- ❌ 多栏排版阅读顺序推断
- ❌ PDF 附件批量采集解析
- ✅ 文本型 PDF 逐页提取与物理页码映射

### 检索与生成

- ❌ 向量数据库（当前 26 切片，NumPy 精确检索 5.7 μs，无需引入）
- ❌ 重排模型 / Reranker
- ❌ 政策关系推理（关系表未建设；当前只能检索承载关系表述的原文片段）
- ❌ 证据充分性判断（向量检索必然返回 Top-K，相似度高低不等于有证据）
- ❌ 增量索引（当前全量重建）
- ❌ 页码级查询接口
- ❌ 中文分词（不引入 jieba；trigram 按 3 字切分，短查询走 `instr` 回退）
- ❌ 混合检索的加权 RRF / DBSF / Linear Fusion
- ✅ 精确字符区间切片与 manifest 哈希
- ✅ FTS5 trigram 字面短语检索与短查询回退
- ✅ 本地 BGE-small 向量检索与索引指纹校验
- ✅ 等权 RRF 混合融合与降级状态显式化

### 系统级

- ❌ 跨进程采集协调
- ❌ 硬任务级截止时间
- ❌ 断电持久性保证
- ❌ 分布式锁

---

## 设计约束

1. 业务规则放 Skill / Agent，不放 core。
2. 所有对外部世界的访问都走 Tool，方便审计和替换。
3. 任何事实性输出都必须能回到证据，或明确标注「待核实」。
4. `.env` 不进版本库；密钥只在本机或服务器环境变量中。
5. 原始采集文件、验收数据库、运行报告默认 gitignore。

---

## 许可

内部项目，暂未开源授权。
