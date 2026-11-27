# MVP-02-B 政策研究与证据包报告

**日期**：2026-11-27
**分支**：`feat/mvp02-research`
**基线**：`73cdd8b5e2cadd3f8662112704b22526e3eb8c38`（1189 passed）
**结果**：**1238 passed**（+49 专项）

---

## 一、前置调研结论

调研了当前 Research Agent、结构化证据包、Grounded Generation 与
Evidence Verification 的主流工程方案。

### 关键发现

**1. quote-span 校验是业界共识**

主流实现（TREC RAG 2025 的 support-evaluation、IANUA 的 citations 引擎）
都采用「引文 + 字符偏移」的定位方式，并在生成后**重新读取原文校验**：

> "This engine turns retrieval hits into citations that quote the actual
> matching passage (not a document opening), carry an exact character-offset
> locator back into the source, and can be verified: a quote that does not
> appear verbatim at its recorded offset in its named source is rejected."
> — [IANUA/rag/citations.py](https://github.com/IRsoctierDT/IANUA/blob/v1.9.0/rag/citations.py)

这直接印证了本轮的核验设计：`exact_quote` + `start/end` + 重新读库比对。

**2. 引用支持度是分层判定，不是二值**

TREC RAG 的支持度评估把每个答案句子对引文的支持程度分为
full / partial / no support，映射为 1.0 / 0.5 / 0.0：

> "each answer sentence is judged against cited evidence using full support,
> partial support, or no support, typically mapped to weights of 1.0, 0.5, 0.0"
> — [TREC RAG 2025 WING-II](https://trec.nist.gov/pubs/trec34/papers/WING-II.rag.pdf)

因此本轮不把「引文核验通过」等同于「事实成立」，而是标记
`pending_review`，交由人工或后续机制判定。

**3. 不支持的事实率是核心指标**

主流评测框架区分 facts 与 advice，并单独统计
`Unsupported claim rate` / `Citation precision` / `Abstention recall`：

> "Never cite a source as if it directly supports a subjective recommendation
> unless the source itself states that recommendation."
> — [RAG architecture advisor](https://huggingface.co/spaces/arjun10g/rag-architecture-advisor/commit/5ddd798bdd8277e8906a1ef3cde0aa571a22c69c.diff)

这支持了本轮「无证据不产出 claim」的设计。

**4. 复杂多 Agent 编排不是必需品**

Anthropic 的 multi-agent research 采用 orchestrator-worker（主导 + 3~5 子 Agent），
OpenAI deep research 则是单 Agent 端到端训练。两者都能产出带引用的报告。

> "Anthropic Research（2025-06）：采用编排者-执行者模式...研究结束后，由一个
> CitationAgent 给报告标出引用。"

但项目负责人已明确「不要求本轮实现复杂 Supervisor 或多 Agent 框架」。
本轮采用**受限单 Agent + 固定阶段编排**，符合轻量选型要求。

### 技术选型决定

| 决策 | 选择 | 理由 |
|---|---|---|
| Agent 框架 | 复用现有 Runtime 构件，自己编排阶段 | 不引入新框架；阶段间需程序校验 |
| 证据定位 | quote + 字符偏移 + 重读原文校验 | 业界共识，可实现且可验证 |
| 语义支持性 | **不自动判定**，标 `pending_review` | 程序无法可靠判断；避免虚假信心 |
| 结构化输出 | 提示词 + JSON 解析 + 契约校验 | 复用现有 LLMClient，不引入新依赖 |
| 编排复杂度 | 单 Agent 固定阶段 | 符合「不做 Supervisor」约束 |

---

## 二、交付内容

### 新增文件

| 文件 | 行数 | 说明 |
|---|---|---|
| `powercopy/research/__init__.py` | 40 | 包导出 |
| `powercopy/research/contracts.py` | 400 | 输入/输出契约 |
| `powercopy/research/verification.py` | 520 | 证据核验器 + 政策库封装 |
| `powercopy/research/agent.py` | 620 | ResearchAgent 编排 |
| `powercopy/research/mock.py` | 270 | 离线 Mock 规划器/提取器 |
| `scripts/run_research.py` | 270 | 独立验收脚本 |
| `tests/test_research_agent.py` | 760 | 49 项专项测试 |
| `docs/research/mvp-02b-report.md` | — | 本文件 |
| `docs/research/mvp-02b-handoff.md` | — | 并行交接记录 |
| `docs/research/mvp-02b-contracts.md` | — | 数据契约 |

**未修改任何共享模块**（frontend / workbench / rag / core / tools）。

---

## 三、Research Agent 流程

```text
ResearchRequest
    │
    ▼
[1] 拆解研究问题（模型）
    │  · 用户已指定 → 直接用
    │  · 否则调用规划器
    ▼
[2] 逐问题检索（模型决定查询，程序执行）
    │  · 调 search_policy_evidence
    │  · 检查 index_freshness / degraded_status
    ▼
[3] 候选转证据（程序）
    │  · 构造 EvidenceItem
    │  · 截断候选不作为证据
    ▼
[4] 提取候选事实（模型）
    │  · 必须引用给定 evidence_id
    │  · 无效 ID 的 claim 丢弃
    ▼
[5] 证据核验（程序）
    │  · 偏移 / 引文 / 哈希 / 文档版本
    │  · evidence_id 与 canonical 比对
    ▼
[6] claim 引用核验（程序）
    │  · 引用必须全部指向已核验证据
    │  · 语义支持性 → pending_review
    ▼
[7] 时效与地区评估（程序）
    │  · latest_observed，不说「现行有效」
    │  · 地区无法确认 → 问题退回未解决
    ▼
EvidencePackage
```

### 为什么不用现有 `Agent` 的自动 tool loop

`powercopy.core.agent.Agent` 是通用 tool-calling 循环，适合开放式任务。
本研究需要**固定阶段顺序**与**阶段间的程序校验**——例如证据必须先核验
才能被 claim 引用，这在开放式循环里无法保证。

因此本模块**复用** Agent 的构件（LLMClient / ToolRegistry / ToolExecutor /
TaskBudget / Trace），但自己编排阶段。**这不是新 Agent 框架**。

---

## 四、最小证据核验

### 程序能可靠判断的（做）

| 检查 | 方法 |
|---|---|
| `evidence_id` 是否真实 | 与 canonical 哈希比对 |
| 文档与版本是否存在 | 查 `policy_versions` |
| 字符偏移是否合法 | `0 <= start < end <= len(body)` |
| `exact_quote` 是否对应原文 | `body[start:end] == exact_quote` |
| 内容哈希是否一致 | 与 `policy_versions.content_sha256` 比对 |
| 事实是否引用有效证据 | 引用集合 ⊆ 已核验证据集合 |

### 程序无法可靠判断的（不做）

**该引文在语义上是否支持某条事实。**

这一点是本轮设计的核心立场：

> **原文匹配成功 ≠ 事实在语义上成立。**

例如引文确实是政策原文，但可能被断章取义、可能只覆盖结论的一半、
可能与结论方向相反。程序无法发现这些。

因此：

- `EvidenceStatus.VERIFIED` = 「这段文字确实在原文的这个位置」
- `ClaimStatus.PENDING_REVIEW` = 「引文真实，但语义支持需人工确认」

**最终证据包里的 claim 正常状态就是 `pending_review`，不是 `verified`。**

### 实测数据

全部 26 个切片的偏移与引文均通过核验（测试 `test_all_real_chunks_pass`）：

```text
一致 26 / 不一致 0
```

---

## 五、地区适用性（本轮发现并修复的真实缺陷）

### 问题

最初的「证据不足」场景（江西地方交易政策）产出了 10 条 claim，
并把 2 个问题都标记为「已回答」——但库里**没有任何江西内容**。

实测：

```text
policy_documents.region 全部为 None
含"江西"的政策数: 0
manifest chunks 中 region 非空的: 0 / 26
```

这些证据全部来自**国家级政策**，`region=None`（未标注地区）。
「未标注地区」不等于「全国适用」，更不等于「适用于江西」。

**把问题标记为已回答会误导下游文案生成。**

### 修复

新增 `assess_region_applicability()`：

| status | 条件 | 处理 |
|---|---|---|
| `no_region_requested` | 未指定地区 | 不评判 |
| `matched` | 有证据地区匹配 | 正常 |
| `unlabeled` | 证据均未标注地区 | **不适用**，问题退回未解决 |
| `unmatched` | 证据地区不符 | **不适用**，问题退回未解决 |

地区不适用时：
1. 原本 `answered=True` 的问题退回 `False`
2. 问题文本移入 `unanswered_questions`
3. `uncertainties` 中记录说明
4. 若原本是 `completed`，降级为 `partial`

### 修复后行为

```text
research_status : partial
status_reason   : 2 个问题未解决；10 条事实待人工确认语义支持性
[✗] q1  关于「江西省售电公司参与电力现货市场的交易规则是什么？」...
[✗] q2  江西地区江西省售电公司参与电力市场的经营主体准入条件是什么？
! 地区适用性无法确认：没有任何证据的地区标注匹配「江西」：
  7 条未标注地区、0 条地区不符。未标注地区不等于全国适用，
  更不等于适用于「江西」
```

同时**不指定地区**的场景不受影响（`q1`/`q2` 仍为 `✓`）。

---

## 六、政策时效

### 设计立场

**绝不判断「现行有效」**。本地库只有观测记录，没有废止信息。

```python
class PolicyValidityStatus(StrEnum):
    LATEST_OBSERVED = "latest_observed"
    SUPERSEDED_IN_CORPUS = "superseded_in_corpus"
    UNKNOWN = "unknown"
```

**没有 `currently_effective` 这个枚举值**，这是刻意的。

### 输出文案

```text
引用的是本地库中该政策的最新观测版本；这不代表该政策现行有效，
是否废止需以官方公告为准
```

---

## 七、异常处理

| 场景 | 处理 | 实测 |
|---|---|---|
| 检索无结果 | 问题记入 `unanswered`，不编造 claim | ✅ |
| 检索候选不支持问题 | Mock 用字面重叠闸门拦截；真实链路由模型判断 | ✅ |
| 索引过期 | `index_freshness != ok` → 记入 `uncertainties` | ✅ |
| 引用不存在的证据 | claim 标 `REJECTED` | ✅ |
| 引用位置错误 | `OFFSET_OUT_OF_RANGE` / `QUOTE_MISMATCH` | ✅ |
| 文档版本冲突 | `VERSION_NOT_FOUND` / `DOCUMENT_ID_MISMATCH` | ✅ |
| 模型结构化输出错误 | 产出 `FAILED` 包 + 原因，不崩溃 | ✅ |
| 任务预算耗尽 | 剩余问题记入 `unanswered`，产出 `PARTIAL` | ✅ |
| 部分研究问题未解决 | `research_status = partial` + `status_reason` | ✅ |

---

## 八、验收场景实测

### 场景 1：正常研究

```text
topic           : 工商业企业参与电力市场前，需要了解哪些基本规则？
research_status : partial
status_reason   : 全部问题均有证据支撑，但 15 条事实的语义支持性待人工确认
questions       : 3 个，全部 ✓
claims          : 15 条，全部 pending_review
evidence        : 12 条，全部 verified
validity        : latest_observed
```

### 场景 2：证据不足（地方性政策）

```text
topic           : 江西省售电公司参与电力现货市场的交易规则是什么？
region          : 江西
research_status : partial
status_reason   : 2 个问题未解决；10 条事实待人工确认语义支持性
questions       : 2 个，全部 ✗（因地区适用性无法确认而退回）
uncertainties   : 含地区适用性说明
```

### 场景 3：完全无关主题

```text
topic           : zzzz qqqq xxxx vvvv
claims          : 0（相关性闸门拦截）
research_status : partial
```

---

## 九、为什么本轮不新增 ADR

`AGENTS.md` 要求「重要技术决策必须形成 ADR」。

本轮的技术选择**没有产生新的架构级决策**：

| 决策 | 是否新决策 | 依据 |
|---|---|---|
| 复用现有 Runtime 构件 | 否 | 项目负责人明确要求 |
| quote-span 证据核验 | 否 | 业界通行做法，非架构选择 |
| 不判定语义支持性 | 否 | 与 ADR-009「候选≠事实」一致 |
| 不宣称政策现行有效 | 否 | 与既有 `notice` 声明一致 |
| 单 Agent 固定阶段编排 | 否 | 项目负责人明确不做 Supervisor |

因此本轮**不新增 ADR**，而是通过 `docs/research/mvp-02b-contracts.md`
记录数据契约。若后续引入 Reranker 或语义支持性模型，届时再补 ADR。

---

## 十、运行命令

```powershell
# 正常研究场景
python scripts/run_research.py

# 证据不足场景（地方性政策）
python scripts/run_research.py --scenario insufficient

# 指定问题
python scripts/run_research.py --question "注册需要哪些材料" --question "违规如何处罚"

# 输出完整 JSON 证据包
python scripts/run_research.py --json storage/research/out.json --verbose

# 专项测试
python -m pytest tests/test_research_agent.py -q

# 全量回归
python -m pytest tests -q
```

---

## 十一、已知限制

| 限制 | 说明 | 归属 |
|---|---|---|
| **语义支持性未判定** | 程序只能说「引文真实」，不能说「事实成立」 | 需人或后续机制 |
| Mock 用字面重叠做相关性闸门 | 不是语义判断，仅是离线测试的确定性替代 | 真实链路由模型判断 |
| 语料仅 2 份政策 / 26 切片 | 地区覆盖为零，地方政策问题必然「证据不足」 | 需扩语料 |
| 库中 `region` 全为 None | 所有政策都「未标注地区」，地区匹配永远失败 | 需补数据 |
| 未做多轮追问 | 单轮检索 + 提取，不迭代深化 | MVP-02 后续可扩展 |
| 未回查 `get_policy_detail` | 当前用检索片段即足够；长条款可能被截断 | 待实际需要时加 |
| 政策关系未用 | 未做「替代/修订」推理 | 技术升级路线 |
| 未验证前端集成 | 本轮不涉及前端 | MVP-01-F |
| 预算耗尽时不做降级检索 | 直接记入 unanswered | 可接受 |

---

## 十二、测试

```text
专项（test_research_agent.py）：49 passed
全量回归：1238 passed（1189 + 49）
```

覆盖矩阵：

| 组 | 数量 | 内容 |
|---|---|---|
| 输入契约 | 13 | 边界、额外字段拒绝（6 种）、重复问题、预算上下限、严格类型 |
| evidence_id | 3 | 确定性、随输入变化、编造拒绝 |
| 证据核验 | 8 | 有效、引文篡改、偏移越界、哈希不符、版本不存在、chunk 不存在、全量 26 切片 |
| claim 核验 | 4 | 有效引用→pending、无效引用→rejected、契约强制、重复拒绝 |
| 时效与地区 | 4 | 不宣称现行有效、无证据 unknown、未指定地区、未标注地区不适用 |
| 端到端 | 5 | 默认场景、全证据核验、地区退回、时效说明、JSON 往返 |
| 异常处理 | 5 | 无效 JSON、空输出、预算耗尽、无命中、指定问题 |
| Mock 组件 | 3 | 确定性、引文子串、未知证据跳过 |
| 包契约 | 5 | 必需字段、ISO 时间、summary 结构、区间一致性 |

---

## 十三、交付边界

**已完成**：政策研究与证据包后端。

**未做**（按项目负责人要求）：
- 不开发前端（MVP-01-F / Sol 负责）
- 不实现最终文案生成（MVP-03）
- 不引入 GraphRAG / Reranker / Memory
- 不修改共享状态文档（改为交接记录）
- 未推送、未合并

**未执行付费 LLM 调用**：全部使用 Mock 规划器/提取器，完全离线。

---

## 十四、Commit

见下方最终回复中的 SHA。
