# PowerCopy V2 文档中心

售电行业政策研究与文案生产的可追溯 Agent 运行时。

**当前状态：1122 项自动化测试全部通过**

---

## 按角色阅读

| 你想做什么 | 从这里开始 |
|---|---|
| 第一次接触这个项目 | [项目概览](overview.md) |
| 搭建本地环境跑起来 | [快速开始](getting-started.md) |
| 理解整体架构 | [系统架构](architecture.md) |
| 看数据中心怎么工作 | [数据中心详解](data-center.md) |
| 用运营工作台 | [工作台使用](workbench.md) |
| 了解 RAG 切片 | [文档切片](chunking.md) |
| 了解检索与实验 | [检索报告](review/stage05.5a-hybrid-retrieval.md)、[ADR 索引](adr/) |
| 跑测试与验收 | [测试与验收](testing.md) |
| 知道哪些还没做 | [技术边界](limitations.md) |

---

## 项目一句话

把「官方政策采集 → 解析清洗 → 版本化存储 → 切片索引 → Agent 检索生成」串成一条**可审计**的数据链路。每一个事实性输出都能回溯到原始网页字节、采集时间与内容哈希。

---

## 核心数字

| 指标 | 数值 |
|---|---|
| 自动化测试 | 1122 项，全部通过 |
| 已验收阶段 | 阶段04（4.1–4.8、4.9）、阶段05（5.1、5.1-B、5-A、5-B、5.5-A） |
| 数据层模块 | 17 个（采集/解析/版本/查询/PDF/切片/Embedding/词法/向量/混合） |
| 工作台页面 | 10 个，全部对接真实数据 |
| 真实政策样本 | 2 份（NDRC + NEA，含完整哈希链路） |
| 新增第三方依赖 | 主依赖 6 个（bs4、pypdf、fastapi、uvicorn、openai、pydantic）；向量检索为可选依赖（torch+transformers，见 requirements-embedding.txt） |

---

## 目录结构

```text
PowerCopy-V2/
├── powercopy/
│   ├── core/          # Agent 核心
│   ├── tools/         # 工具系统
│   ├── data/          # 数据中心
│   ├── rag/           # 切片 / Embedding / 词法 / 向量 / 混合检索
│   ├── api/           # FastAPI 入口
│   └── {memory,context,skills,agents,harness,evaluation}/   # 骨架
├── workbench/         # 工作台后端
├── frontend/          # 工作台前端（React 19 + Vite）
├── config/            # 解析配置
├── scripts/           # 采集/验收脚本
├── tests/             # 1122 项测试
└── docs/              # 本文档中心
```

---

## 三条设计红线

### 1. 事实必须可追溯

每份入库政策绑定双重哈希：

- `raw_sha256` — 原始网页/PDF 字节哈希
- `content_sha256` — 标准化正文业务哈希

采集事件与文档版本分离，因此「采集 30 次、内容改 2 次」= **1 个身份 + 2 个版本 + 30 条观测**。

### 2. 不确定就拒绝（Fail Closed）

| 场景 | 处理 |
|---|---|
| CSS 选择器零匹配或多匹配 | 拒绝解析，不猜测相似节点 |
| 编码声明冲突 | 拒绝解码，不静默丢字节 |
| PDF 加密 / 无文本页 | 拒绝摄取 |
| 审核指纹不匹配 | 拒绝入库 |

### 3. 不编造业务结论

查询结果每次都会附带 `notice` 声明：

- 未标注地区 **不等于** 全国适用
- 最新观测版本 **不等于** 当前法律有效
- 无结果 **不等于** 政策不存在

---

## 快速验证

```powershell
# 安装
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]" -e ".[api]" -r requirements.txt

# 跑测试
python -m pytest tests -q
# 期望：1122 passed

# 起服务
uvicorn workbench.main:app --port 8000
```

---

## 文档索引

- [项目概览](overview.md) — 项目定位与能力清单
- [快速开始](getting-started.md) — 环境搭建与运行
- [系统架构](architecture.md) — 分层设计与数据流
- [数据中心详解](data-center.md) — 采集到入库全链路
- [工作台使用](workbench.md) — 10 个页面与 API 契约
- [文档切片](chunking.md) — RAG 切片与可追溯性
- [测试与验收](testing.md) — 测试矩阵与验收脚本
- [技术边界](limitations.md) — 明确未实现的范围

## 阶段复审与决策

- [项目状态](PROJECT_STATE.md) — 当前进度、已完成阶段、待决策事项
- [变更日志](CHANGELOG.md) — 每个阶段交付内容与测试基线变化
- [前四阶段技术基线复审](review/stage-01-04-review.md) — 阶段01-04 源码检查、技术对照、P0-P3 优先级
- [Embedding 来源核实](review/embedding-source-assessment.md) — 平台端点探测与本机硬件核实
- [05-A BM25 基线报告](review/stage05a-bm25-baseline.md) — 词法检索能力边界与基线指标
- [05-A 修复报告](review/stage05a-fixes.md) — BM25 分数方向修正与指标审计
- [05-B Dense Retrieval 报告](review/stage05b-dense-retrieval.md) — 向量检索实现、性能与对照实验
- [05.5-A Hybrid Retrieval 报告](review/stage05.5a-hybrid-retrieval.md) — 等权 RRF 融合、三路对照与 H3 检验
- [已知安全问题与技术债务](review/security-and-tech-debt.md) — **必读**：历史遗留、证据充分性缺失、语料规模限制
- [阶段05 路线图](roadmap-stage05.md) — Adaptive Graph-enhanced RAG 分阶段计划

### ADR（架构决策记录）

- [ADR-001 LLM 接口协议](adr/ADR-001-llm-protocol.md) — 保持 Chat Completions
- [ADR-002 ToolResult 语义](adr/ADR-002-tool-result-semantics.md) — **三段式状态拆分（已采纳并实施 04.9）**
- [ADR-003 阶段05 检索方案](adr/ADR-003-retrieval-stage05.md) — 先 BM25 基线，向量延后（已采纳，向量来源见 ADR-006）
- [ADR-004 SQLite WAL](adr/ADR-004-sqlite-wal.md) — 启用 WAL 提升并发读（提议，待核实版本）
- [ADR-005 PDF 解析](adr/ADR-005-pdf-parsing.md) — 保持 pypdf，高级解析延后
- [ADR-006 本地 Embedding](adr/ADR-006-local-embedding.md) — **BGE-small-zh-v1.5，512 维，CPU（已采纳并实施）**
- [ADR-007 Dense Retrieval](adr/ADR-007-dense-retrieval.md) — **NumPy 精确检索，不引入向量数据库（已采纳并实施）**
- [ADR-008 Hybrid RRF](adr/ADR-008-hybrid-rrf.md) — **等权 RRF 融合，rank_constant=60（已采纳并实施）**

> ADR 的生命周期：**提议** → **已采纳** / **已废弃**。
> 每条 ADR 都写明「重新评估触发条件」，满足条件时应重新审视该决策。
