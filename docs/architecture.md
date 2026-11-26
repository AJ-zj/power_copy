# 系统架构

## 分层总览

```text
┌─────────────────────────────────────────────────────────────┐
│  工作台前端  React 19 + Vite + TypeScript                    │
│  Overview / Runtime / Policies / Versions / Sources / ...   │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP /api/workbench
┌──────────────────────────▼──────────────────────────────────┐
│  工作台后端  workbench/                                      │
│  data_api.py（数据中心视图）  runtime_api.py（Agent 运行时）  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Agent 运行时  powercopy/core/                               │
│  LLMClient → RetryPolicy → TaskBudget → Agent Loop           │
│  ExecutionTrace（ContextVar 全程追踪）                        │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  工具系统  powercopy/tools/                                  │
│  Tool 契约 → ToolRegistry → ToolExecutor → builtin/          │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  数据中心  powercopy/data/ + powercopy/rag/                  │
│  Sources → Fetcher → Parser/PDF → Store → Query → Chunking  │
│        → Lexical / Dense → Hybrid（RRF）→ 证据溯源            │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  存储层  SQLite（标准库 sqlite3）                             │
│  policy_identities / policy_versions / policy_fetches        │
│  policy_documents                                            │
└─────────────────────────────────────────────────────────────┘
```

## Agent 运行时

### 三层预算设计

```text
RetryPolicy（本地重试）
   ↓ 每次失败按指数退避
TaskBudget（任务级全局预算）
   ↓ 累计 API 尝试次数与 Token
Agent Loop（Agent 循环）
   ↓ 每轮检查预算余量
```

**为什么分三层**：

- `RetryPolicy` 只管「这一次请求失败了要不要重试」，不关心任务整体
- `TaskBudget` 管「这个任务总共能花多少次 API 调用」，防止无界循环
- `Agent Loop` 每轮开始前检查预算，避免执行了工具却无法反馈给模型

### Agent Loop 时序

```text
1. 记录 round_started
2. LLM 决策（带 tools schema）
3. 检查响应完整性（finish_reason）
4. 有 tool_calls？
   ├─ 否 → 检查 finish_reason=stop 且 content 非空 → 完成
   └─ 是 → 检查工具协议 + 工具预算
           → 保存 assistant tool_calls 消息
           → 逐个执行工具
           → 回填 tool 消息（匹配 tool_call_id）
           → 下一轮
```

### ExecutionTrace

用 `ContextVar` 实现，无需显式传参：

```python
@trace_task
def run(self, task):
    ...

@trace_model_attempt
def _invoke_once(self, messages, ...):
    ...

@trace_tool
def execute(self, name, arguments):
    ...
```

追踪事件类型：

| 事件 | 触发点 |
|---|---|
| `task_started` | Agent.run 开始 |
| `round_started` | 每轮开始 |
| `model_attempt` | 每次实际模型请求 |
| `tool_execution` | 每次工具执行（含失败） |
| `budget_exceeded` | 预算耗尽 |
| `task_completed` | 正常结束 |
| `task_finished` | 收尾（含状态与预算快照） |

## 工具系统

### 三层分离

```python
# 1. 契约
@dataclass
class Tool:
    name: str
    description: str
    args_schema: type[BaseModel]
    handler: Callable

# 2. 注册表
class ToolRegistry:
    def register(self, tool): ...      # 同名重复注册抛 ValueError
    def get(self, name): ...           # 不存在抛 KeyError
    def get_schemas(self): ...         # 生成 OpenAI Function Schema

# 3. 执行器
class ToolExecutor:
    @trace_tool
    def execute(self, name, arguments):
        # 优先级：TOOL_NOT_FOUND > INVALID_JSON > INVALID_ARGUMENTS > EXECUTION_ERROR
```

### 政策工具适配

`policy_db.py` 提供框架中立绑定，`policy_tools.py` 适配到项目 `Tool`：

```python
PolicyToolBinding(name, description, args_model, handler)
    ↓ binding_to_tool()
Tool(name, description, args_schema, handler)
```

两个互斥入口：

| 入口 | 用途 |
|---|---|
| `build_mock_registry()` | 旧测试与离线演示（返回 mock 数据） |
| `build_live_registry(db_path)` | 真实数据库（只读查询） |

### 错误映射的特殊处理

`ToolExecutor` 会把 handler 的所有异常折叠成 `EXECUTION_ERROR`，原始错误码丢失。因为 executor 已验收冻结，`policy_tools.py` 改为让 handler **返回**结构化错误：

```json
{
  "ok": false,
  "error_code": "POLICY_VERSION_NOT_FOUND",
  "error_message": "没有找到该 policy_id 与 version 对应记录。",
  "retryable": false
}
```

这样模型能拿到可行动的失败原因，同时零数据库内部信息泄漏。

## 数据中心

### 数据流

```text
官方网页
   │ SourceRegistry（精确域名白名单）
   ▼
HttpFetcher ─-─► Raw JSON（Base64 + SHA-256 + 元数据）
                   │
                   ▼
              Parser / PDF Ingest
                   │
                   ▼
              PolicyDocument（严格 Pydantic 契约）
                   │
                   ▼
          VersionedPolicyStore（三元组写入）
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
  PolicyQueryService      Chunking
  （只读查询）              （切片索引）
                              │
                 ┌────────────┼────────────┐
                 ▼            ▼            ▼
            Lexical        Dense        （关系检索）
          FTS5 trigram   BGE-small      待建设
            + BM25       512 维
                 │            │
                 └─────┬──────┘
                       ▼
                  Hybrid（等权 RRF）
                       │
                       ▼
                  排序后的切片 + 证据溯源
```

### 检索层设计

三路检索共用同一份切片清单（`manifest_sha256` 校验一致后才允许融合）。

| 层 | 模块 | 依赖 | 特点 |
|---|---|---|---|
| 词法 | `rag/lexical.py` | 标准库 `sqlite3` | FTS5 trigram 字面短语 + BM25 打分；< 3 字符查询走 `instr` 回退 |
| 向量 | `rag/embedding.py` + `rag/dense.py` | 可选 torch + transformers | BGE-small-zh-v1.5，512 维，NumPy 精确余弦；索引指纹校验 |
| 混合 | `rag/hybrid.py` | 无新增 | 等权 RRF，只融合排名不融合分数；降级状态显式 |

**为什么不用向量数据库**：26 个切片 × 512 维 = 52 KB，NumPy 矩阵乘法实测 5.7 μs，引入外部服务是过度设计。

**为什么 RRF 不做分数相加**：BM25 分值为负且随语料变化，余弦相似度在 -1..1，两者量纲不可比。RRF 只用排名，避开该问题。

**已知边界**：向量检索必然返回 Top-K，相似度高低**不代表**证据充分。当前未设阈值，`notice` 字段每次声明这一点。


### 存储三元组

```text
policy_identities          政策身份
  policy_id (PK)
  source_key + source_url (UNIQUE)
       │
       ├──► policy_versions      文档版本
       │      (policy_id, version) PK
       │      document_id (UNIQUE)
       │      content_sha256
       │           │
       │           └──► policy_documents   文档快照
       │                  document_id (PK)
       │                  title, body_text, ...
       │
       └──► policy_fetches       采集观测
              fetch_id (PK)
              (policy_id, version) FK
              raw_sha256, input_sha256
              retrieved_at
```

**关键设计**：30 天采集同一政策、第 15 天内容变了

```text
1 个 policy_id
2 个 version（V1, V2）
30 条 fetch 记录
2 份 document 快照
```

历史版本永不被覆盖——`repository.add()` 只用 INSERT，不用 UPSERT。

### 版本写入的原子性

```python
def _ingest_prepared(self, doc, *, fetch_id, target_key,
                     stamp, raw_sha, digest, fingerprint):
    with closing(self._connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')   # 先拿写锁
        self._check_untracked(conn)
        # 幂等检查 → 身份查找 → 版本计算 → 原子写入
```

**为什么不调 `repo.add()`**：那会在另一个连接上 commit，破坏原子性。

## 数据契约

### PolicyDocument

```python
class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID              # 自动生成
    source_key: str                # 来源标识
    source_url: AnyHttpUrl         # 必须是 HTTP(S)
    title: str                     # 非空白
    body_text: str                 # 非空白
    region: str | None
    issuer: str | None
    published_on: date | None
    retrieved_at: AwareDatetime    # 必须带时区
```

`frozen=True` 保证不可变快照；`extra="forbid"` 拒绝未知字段。

### 内容哈希

```python
def content_hash(document: PolicyDocument) -> str:
    data = document.model_dump(mode='json', include={
        'title', 'body_text', 'issuer', 'region', 'published_on'})
    return _json_hash({'schema': 'policy-content-v1', **data})
```

**只算业务字段**，不含 `document_id`、采集时间、HTML 布局。所以页脚变化不产生新版本。

## 设计原则

### 1. 关注点分离

| 模块 | 只做一件事 |
|---|---|
| `Fetcher` | 取原始字节，不解码 |
| `Parser` | 解码 + 提取，不入库 |
| `Normalizer` | 清洗格式，不改语义 |
| `VersionedPolicyStore` | 事务写入，不解析 |
| `PolicyQueryService` | 只读查询，不改数据 |

**好处**：Parser 出错不需要重访政府网站，直接用已存原始字节测试。

### 2. 配置驱动

不同政府网站的 HTML 结构完全不同，但 Parser 是统一的：

```json
{
  "ndrc/market_rules": {
    "title_selector": "h1",
    "body_selector": ".zhengwen",
    "published_selector": "meta[name=\"PubDate\"]",
    "issuer_selector": "meta[name=\"SiteName\"]",
    "exclude_selectors": []
  }
}
```

新增结构相似的数据源，只加配置不改代码。

### 3. 依赖注入

```python
class HttpFetcher:
    def __init__(self, registry, *, timeout=10.0, max_bytes=2*1024*1024, opener=None):
        self._opener = opener or build_opener(ProxyHandler({}), NoRedirect())
```

测试传 mock opener，生产用真实 opener。业务逻辑仍然是真的，只有网络被替换。

### 4. Fail Closed

数据不符合要求时，宁可报错也不猜：

- 选择器匹配数 ≠ 1 → `SELECTOR_MATCH_COUNT`
- 编码声明冲突 → `CHARSET_CONFLICT`
- PDF 无文本页 → `PAGE_WITHOUT_TEXT`
- 审核指纹不匹配 → `REVIEW_REQUIRED_OR_STALE`

## 目录职责

| 目录 | 职责 | 状态 |
|---|---|---|
| `core/` | Agent 运行时 | ✅ 完整 |
| `tools/` | 工具系统 | ✅ 完整 |
| `data/` | 数据中心 | ✅ 完整 |
| `rag/chunking.py` | 切片 | ✅ 5.1 完成 |
| `rag/embedding.py` | 本地 Embedding | ✅ 5.1-B 完成 |
| `rag/lexical.py` | 词法检索 | ✅ 5-A 完成 |
| `rag/dense.py` | 向量检索 | ✅ 5-B 完成 |
| `rag/hybrid.py` | 混合融合 | ✅ 5.5-A 完成 |
| `api/` | FastAPI 入口 | ✅ 基础 |
| `memory/` | 记忆 | 🚧 骨架 |
| `context/` | 上下文工程 | 🚧 骨架 |
| `skills/` | 业务技能 | 🚧 骨架 |
| `agents/` | 业务 Agent | 🚧 骨架 |
| `harness/` | 运行保障 | 🚧 骨架 |
| `evaluation/` | 评测 | 🚧 骨架 |

## 下一步阅读

- [数据中心详解](data-center.md) — 深入每个数据模块
- [文档切片](chunking.md) — RAG 切片设计
- [ADR 索引](adr/) — 架构决策记录（ADR-001 ~ ADR-008）
- [检索实验报告](review/stage05.5a-hybrid-retrieval.md) — 词法/向量/混合三路对照
- [测试与验收](testing.md) — 1122 项测试怎么组织的
