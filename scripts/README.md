"""脚本说明

阶段一 · LLM Client

## check_llm.py

LLM Client 验收脚本，对应四项验收标准：

1. 成功读取 .env 配置
2. 成功调用易拓AI模型
3. 成功获取模型回复
4. 成功获取 Token 用量或确认平台未提供

```powershell
python scripts/check_llm.py --check   # 只检查配置，不联网、不花钱
python scripts/check_llm.py           # 完整验收，会发一次真实请求
```

退出码：`0` 全部通过 / `1` 有验收项未通过 / `2` 配置不完整（未发送请求）。

## check_messages.py

Messages 模块验收，四项：

1. 能够正确添加 system、user、assistant 消息
2. 第二轮对话能够利用第一轮提供的信息
3. get_messages() 能返回完整消息历史的副本
4. clear() 能清除历史并保留初始系统提示词

```powershell
python scripts/check_messages.py --check   # 离线检查
python scripts/check_messages.py           # 含真实多轮对话
```

## check_structured.py

Structured Output 异常验收，六项：标准 JSON、Markdown JSON、非法 JSON、空内容、缺失字段、错误类型。

```powershell
python scripts/check_structured.py          # 六项离线验收
python scripts/check_structured.py --live   # 额外跑一次真实模型
```

## check_retry.py

Retry 接入验收，七项。全部使用 Mock 模拟异常，不发送真实失败请求。

```powershell
python scripts/check_retry.py
```

## check_execute.py

统一执行器 `_execute()` 验收，十项：正常文本、正常结构化、JSON 错误后成功、Schema 失败后成功、混合异常、总预算、格式预算、Token 累计、耗时统计、消息隔离。

```powershell
python scripts/check_execute.py
```

阶段二 · Tool System

## check_tools.py

工具机制验收，用模拟的模型 tool_call 走完整链路：

1. 工具导出 OpenAI schema
2. 模拟模型返回 tool_call，解析参数
3. 用 args_schema 校验入参
4. 执行 handler
5. 组装 tool 消息

```powershell
python scripts/check_tools.py
```

## check_tool_definition.py

Tool Definition 验收，六项：统一 Tool 类、Function Calling Schema、无 handler 泄漏、Pydantic 校验、handler 执行、全量回归。

```powershell
python scripts/check_tool_definition.py
```

## check_registry.py

Tool Registry 验收，六项：注册 Tool、按名称获取、拒绝重复注册、不存在时抛异常、多工具批量生成 Schema、全量回归。

```powershell
python scripts/check_registry.py
```

## check_executor.py

Tool Executor 验收，八项：合法参数执行（字典/JSON 字符串）、TOOL_NOT_FOUND、INVALID_JSON、INVALID_ARGUMENTS、校验失败不执行 handler、EXECUTION_ERROR 捕获、ToolResult 与耗时、全量回归。

```powershell
python scripts/check_executor.py
```

阶段三 · Function Calling

## check_tool_calling.py

真实 Function Calling 全链路：tools 传参 → 模型发起 tool_calls → 执行 → 回填 tool 消息 → 第二次调用得到最终回答。

```powershell
python -m scripts.check_tool_calling
```

**会真实调用模型（2 次）**，消耗少量 token。脚本会打印完整消息历史，可用来核对 tool_call_id 匹配。

阶段四 · Agent Loop

## check_agent.py

Agent Loop 真实调用：模型自主决定是否使用工具、多轮循环、预算控制。

```powershell
python -m scripts.check_agent
```

**会真实调用模型（通常 2 轮）**。脚本检查三件事：状态是否为 `completed`、是否自主发起工具调用、最终回答是否明确标注模拟数据。

Agent 的 `run()` 返回结构：

```python
{
    "status": "completed" | "max_rounds" | "tool_budget_exhausted"
              | "invalid_response" | "incomplete_response",
    "content": "最终回答或 None",
    "rounds": 2,           # 实际执行轮数
    "tool_requests": 1,    # 实际工具调用次数
}
```

## check_multi_tool.py

多工具与错误反馈：两个工具（检索 + 详情）、顺序依赖调用、批量工具、失败结果回填。

```powershell
python -m scripts.check_multi_tool
```

**会真实调用模型（通常 3 轮）**。验证模型能否先 `search_policy` 拿到 `mock-001`，再用该 ID 调 `get_policy_detail`，最后给出可靠性说明。

阶段五 · Task Budget

## check_budget.py

任务级全局预算验收，九项：API 硬限制、局部/全局预算、跨轮共享、Token 累计、Token 阈值、未知用量、安全退出、任务隔离、全量回归。

```powershell
python scripts/check_budget.py
```

**全程 Mock**，不联网。重点验证「全局预算优先于局部重试」：局部允许 3 次但全局只剩 2 次时，实际只请求 2 次。

阶段六 · Execution Trace

## check_trace.py

运行轨迹验收，十一项：task_id、模型事件、工具事件、结构化失败识别、异常重试事件、状态与预算快照、耗时、任务隔离、异常清理、Runtime 接线、全量回归。

```powershell
python scripts/check_trace.py
```

**全程 Mock**。Agent 返回结果中会多出 `task_id` 和 `trace` 字段，trace 包含完整事件列表，可直接 JSON 序列化后存档或送入评测中心。

Agent 返回结构现在是：

```python
{
    "status": "...",
    "content": "...",
    "rounds": 2,
    "tool_requests": 1,
    "budget": {...},
    "task_id": "32位十六进制",
    "trace": {
        "task_id": "...",
        "status": "success" | "stopped" | "failed",
        "stop_reason": "...",
        "started_at": "ISO8601",
        "duration_ms": 123.4,
        "budget": {...},
        "events": [
            {"event_id": 1, "event_type": "task_started", "offset_ms": 0.0},
            {"event_type": "round_started", "round": 1},
            {"event_type": "model_attempt", "status": "success", "duration_ms": 1.2},
            {"event_type": "tool_execution", "tool_name": "...", "status": "success"},
            {"event_type": "task_completed", ...},
            {"event_type": "task_finished", ...},
        ],
    },
}
```

阶段七 · 政策文档数据模型与持久化

## check_policy_repository.py

PolicyDocument 数据契约与 SQLite 持久化验收，九项：数据契约、非法数据拒绝、URL/时区校验、写入读取、持久化、重复主键、未知文档与快照、参数化 SQL、全量回归。

```powershell
python scripts/check_policy_repository.py
```

**全部本地运行**，不联网、不调用模型、不花钱。测试用临时数据库，不会污染 `storage/policies.db`。

阶段八 · 数据源管理与真实采集

## check_fetcher.py

Source Registry 与 Fetcher 验收，十一项：注册与查询、未知/禁用源不发请求、URL 与域名检查、响应元数据、错误分类、大小限制、禁重定向、原始字节、Base64 往返、真实采集、全量回归。

```powershell
python scripts/check_fetcher.py           # 9 项 Mock 验收，不联网
python scripts/check_fetcher.py --live    # 额外做 3 次真实官方网页采集
```

## fetch_policy.py

按注册中心采集单个目标页面，落到 `storage/raw/<fetch_id>.json`。

```powershell
python -m scripts.fetch_policy ndrc market_rules
python -m scripts.fetch_policy nea registration_rules
python -m scripts.fetch_policy nea_east anhui_market_news
python -m scripts.fetch_policy nea_east policy_index
```

只发一次 GET，不重试、不跟随重定向。原始 HTML 以 Base64 存在 JSON 里，附带状态码、content_type、charset、耗时、采集时间。

阶段九 · Parser & Normalizer

## check_parser.py

Parser 与 Normalizer 验收，十一项：原始校验、编码、选择器、正文提取、条款保留、日期、安全拒绝、新闻/列表边界、入库回读、真实数据、全量回归。

```powershell
python scripts/check_parser.py           # 9 项 Mock 验收
python scripts/check_parser.py --real    # 额外验证 storage/raw/ 真实数据
```

## parse_policy.py

离线解析采集文件，不重新请求网站。

```powershell
# 只看候选选择器，用于校准配置
python -m scripts.parse_policy inspect storage/raw/<fetch_id>.json

# 解析预览，不写库
python -m scripts.parse_policy parse storage/raw/<fetch_id>.json --show-body

# 解析并入库
python -m scripts.parse_policy parse storage/raw/<fetch_id>.json --commit --db storage/policies.db
```

解析配置在 `config/parser_profiles.json`，按 `source_key/target_key` 索引。

阶段十 · 去重与版本管理

## check_versioning.py

版本管理验收，十一项：身份映射、V1、去重、幂等与冲突、新版本、A→B→A、哈希血缘、事务/外键/并发、迟到与非法输入、真实数据、全量回归。

```powershell
python scripts/check_versioning.py           # 9 项构造数据验收
python scripts/check_versioning.py --real    # 额外验证真实政策
```

## ingest_versioned.py

带版本管理的离线入库，不联网。

```powershell
python -m scripts.ingest_versioned --db storage/policies_versioned.db ingest storage/raw/<fetch_id>.json
python -m scripts.ingest_versioned --db storage/policies_versioned.db counts
python -m scripts.ingest_versioned --db storage/policies_versioned.db history <policy_id>
```

返回 `action` 为 `created` / `unchanged` / `updated` / `replayed` 之一，并给出 `policy_id`、`version`、`document_id`、`content_sha256`。

阶段十一 · Ingestion Pipeline

## check_pipeline.py

增量采集 Pipeline 验收，十一项：完整串联、真实入库、四种动作、原始保存优先、重试退避、预算限制、403/429 暂停与批准、列表页选择器、候选过滤与隔离、运行报告与失败隔离、全量回归。

```powershell
python scripts/check_pipeline.py           # 8 项 Mock 验收
python scripts/check_pipeline.py --real    # 额外验证真实数据与列表页
```

## run_ingestion.py

```powershell
# 离线回放（默认，不发网络请求）
python -m scripts.run_ingestion --db storage/pipeline_acceptance.db replay storage/raw/<id1>.json storage/raw/<id2>.json

# 列表页候选发现（需要 index selectors）
python -m scripts.run_ingestion --db storage/pipeline_acceptance.db --index-selectors config/index_selectors.json replay storage/raw/<index_id>.json

# 实时采集（需要 operator approval）
python -m scripts.run_ingestion live --approval config/ingestion_approval.json ndrc/market_rules
```

运行报告写入 `storage/ingestion_runs/<run_id>.json`。

阶段十二 · 政策工具接入真实数据中心

## check_policy_tools.py

政策工具接入真实数据中心验收，十一项：真实搜索、版本选择、地区筛选、数据契约、来源哈希、只读与注入防护、分段读取、Registry 与 Schema、Agent Runtime 链路、异常与预算 Trace、核心模块不变。

```powershell
python scripts/check_policy_tools.py
```

只读 `storage/policies_versioned.db`，Mock 模型响应，不联网、不调用真实 LLM API、不写库。

## query_policy.py

命令行查询真实政策库。

```powershell
python -m scripts.query_policy --db storage/policies_versioned.db search 电力 --limit 3
python -m scripts.query_policy --db storage/policies_versioned.db search 电力 --region 安徽
python -m scripts.query_policy --db storage/policies_versioned.db detail <policy_id> --version 1
```

阶段十三 · PDF 文档摄取

## check_pdf_ingest.py

PDF 摄取验收，十一项：导入与哈希、中文逐页、页码映射、异常拒绝、来源契约、审核指纹、版本与幂等、旧库兼容、查询可读、真实覆盖说明、回归范围。

```powershell
python scripts/check_pdf_ingest.py
```

全程离线，使用 `tests/fixtures/synthetic_policy_zh.pdf`（中文双页合成样本，非真实政策）。

## ingest_pdf.py

```powershell
# 1. 导入（生成一次性事件文件，不写库）
python -m scripts.ingest_pdf import approved.pdf --metadata config/pdf_metadata.json

# 2. 预览（不写库），人工逐页核对后记录 review_sha256
python -m scripts.ingest_pdf preview storage/raw_pdf/<fetch_id>.json --show-body

# 3. 提交（需人工审核指纹）
python -m scripts.ingest_pdf commit storage/raw_pdf/<fetch_id>.json `
    --approve-sha256 <review_sha256> --db storage/pdf_acceptance.db
```

页码与字符映射：正文用 `\n\f\n` 分隔页，`pages[].number` 为 1 起始物理页码，`[start, end)` 为字符区间。

阶段十四 · 文档切片

## check_chunking.py

文档切片验收，十项：真实政策只读切片、参数约束、自然边界、字符精确还原、重复构建确定性、版本与配置身份更新、元数据关联、PDF 页码映射、专项测试、回归与模块不变。

```powershell
python scripts/check_chunking.py
```

全程离线只读，不写数据库、不调用模型。

## build_chunks.py

```powershell
python -m scripts.build_chunks --db storage/policies_versioned.db --out storage/rag/chunks.json
python -m scripts.build_chunks --max-chars 300 --overlap-chars 60
```

输出 `policy-chunk-manifest-v1` 清单：配置、文档列表、chunks、`manifest_sha256`。

字符区间为 Python Unicode 字符位置 `[start, end)`，不是字节坐标。PDF 正文用 `\n\f\n` 分页，`page` 为物理页码，切片不跨页。

## check_tool_result_semantics.py

```powershell
python scripts/check_tool_result_semantics.py
```

工具结果三层状态语义验收（11 项）：执行状态、业务状态、任务状态分离，
搜索无命中归为 `empty`，业务失败保留 `business_code`，执行崩溃不透传内部信息。

## eval_retrieval.py

```powershell
python scripts/eval_retrieval.py --rebuild
python scripts/eval_retrieval.py --json storage/rag/eval-baseline.json --show-cases
```

构建 FTS5 trigram 词法索引并跑评测（Recall@K / MRR / nDCG@K / Complete Evidence Rate）。
输出逐题结果、证据溯源（policy_id / document_id / version / 字符偏移）与未命中原因分类。

## compare_retrieval.py

```powershell
python scripts/compare_retrieval.py --json storage/rag/compare-v1.json
```

词法 vs 向量两路对照实验（05-B）。

## compare_hybrid.py

```powershell
python scripts/compare_hybrid.py --json storage/rag/compare-hybrid.json
python scripts/compare_hybrid.py --show-cases --sweep
```

三路对照实验（Lexical / Dense / Hybrid）。含 H3 假设检验、分类型指标、Bad Case、耗时。
`--sweep` 输出候选深度与 rank_constant 敏感性（开发集调参结果，非最终配置）。

## bench_dense.py

```powershell
python scripts/bench_dense.py
```

向量检索性能测量：模型冷启动、批量向量化、单条 query、相似度计算、内存峰值、索引占用。

## sync_wiki.py

```powershell
python scripts/sync_wiki.py
```

把 `docs/*.md` 转成 GitHub Wiki 格式（链接改写、图片路径映射）。`docs/` 是唯一真相源。

## build_dense_index.py

```powershell
python scripts/build_dense_index.py --rebuild
python scripts/build_dense_index.py --rebuild --allow-network
```

构建向量索引（离线）。默认 `local_files_only=True`，不会联网下载模型；
缺少模型时明确报错。旧格式索引（`policy-dense-index-v1`，object 数组）
必须用 `--rebuild` 重建，脚本不兼容旧格式。

构建后自动自检：能否以 `allow_pickle=False` 加载 + 模型一致性校验是否通过。

## 通用约定

- 验收脚本不写数据库、不改 `.env`。
- 默认不联网；需要真实请求的脚本会明确标注，并提供 `--check` 或离线模式。
- 退出码统一：`0` 通过，`1` 未通过，`2` 配置或环境问题。
- 任何时候都可以用 `python -m pytest tests -q` 跑全量回归。
