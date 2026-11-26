# PowerCopy V2 文档中心

售电行业政策研究与文案生产的可追溯 Agent 运行时。

**当前状态：795 项自动化测试全部通过**

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
| 跑测试与验收 | [测试与验收](testing.md) |
| 知道哪些还没做 | [技术边界](limitations.md) |

---

## 项目一句话

把「官方政策采集 → 解析清洗 → 版本化存储 → 切片索引 → Agent 检索生成」串成一条**可审计**的数据链路。每一个事实性输出都能回溯到原始网页字节、采集时间与内容哈希。

---

## 核心数字

| 指标 | 数值 |
|---|---|
| 自动化测试 | 795 项，全部通过 |
| 已验收阶段 | 阶段04（4.1–4.8）、阶段05（5.1） |
| 数据层模块 | 12 个（采集/解析/版本/查询/PDF/切片） |
| 工作台页面 | 10 个，全部对接真实数据 |
| 真实政策样本 | 2 份（NDRC + NEA，含完整哈希链路） |
| 新增第三方依赖 | 6 个（bs4、pypdf、fastapi、uvicorn、openai、pydantic） |

---

## 目录结构

```text
PowerCopy-V2/
├── powercopy/
│   ├── core/          # Agent 核心
│   ├── tools/         # 工具系统
│   ├── data/          # 数据中心
│   ├── rag/           # 文档切片
│   ├── api/           # FastAPI 入口
│   └── {memory,context,skills,agents,harness,evaluation}/   # 骨架
├── workbench/         # 工作台后端
├── frontend/          # 工作台前端（React 19 + Vite）
├── config/            # 解析配置
├── scripts/           # 采集/验收脚本
├── tests/             # 795 项测试
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
# 期望：795 passed

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
