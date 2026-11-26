# 05-A 修复报告

**日期**：2026-11-26
**触发**：项目负责人条件验收，指出三项问题
**测试基线**：971 → 1013 passed

---

## 一、BM25 分数转换方向（真实缺陷，已修复）

### 问题

旧实现：

```python
def _score_from_bm25(bm25: float) -> float:
    return 1.0 / (1.0 + abs(bm25))
```

FTS5 的 `bm25()` 返回负值，**越小（越负）越相关**。上述公式把绝对值当成距离，导致：

| bm25 | 相关性 | 旧公式分数 |
|---|---|---|
| -10.0 | 很相关 | 0.091 |
| -0.1 | 不相关 | 0.909 |

**顺序被完全反转。**

### 实测证据

真实索引，`query = "电力市场"`：

```text
SQL 正确顺序（ORDER BY bm25 ASC）：
  #1 b8359bd47a1f  bm25=-0.00000201  new_score=0.00000201
  #2 c24a935bfdd4  bm25=-0.00000199  new_score=0.00000199

若按旧公式 score 降序重排：
  #1 60780642997f  bm25=-0.00000171  old_score=0.99999829
  #2 535531efef28  bm25=-0.00000172  old_score=0.99999828
```

旧公式把 bm25 最接近 0（最不相关）的排到了最前。

### 修复

采用项目负责人指定公式：

```python
def _score_from_bm25(bm25: float) -> float:
    relevance = -bm25
    if relevance <= 0:
        return 0.0
    return relevance / (1.0 + relevance)
```

性质：
- bm25 越负 → relevance 越大 → score 越接近 1
- bm25 = 0 → score = 0
- 严格单调，不改变排序

### 同时补强

**保留原始分数**：`SearchHit` 新增 `bm25: float | None` 字段，`to_dict()` 输出 `bm25` 原值，便于审计。

**显式排名**：新增 `rank` 字段，从 1 开始。

**稳定排序**：SQL 增加二级排序键 `chunk_id ASC`，同分时结果可复现。

### 原评测是否受影响

**结论：排序指标不受影响。**

| 指标 | 修复前 | 修复后 |
|---|---|---|
| Recall@10 | 0.4231 | 0.4231 |
| MRR | 0.4231 | 0.4231 |
| nDCG@10 | 0.4231 | 0.4231 |
| Hit Rate | 0.4231 | 0.4231 |
| 空结果用例 | 4/4 | 4/4 |

原因：

```text
SQL 层已用 ORDER BY bm25 ASC 排好序
  ↓
Top-K 截断发生在 SQL 层
  ↓
Python 端只做分数映射，不改变顺序
```

所以 `Recall` / `MRR` / `nDCG` 等基于顺序的指标不受影响。

**受影响的是**：
1. `score` 字段的数值（展示、阈值判断、未来与向量分数融合）
2. **若下游按 score 重排，顺序会反转** —— 这才是真正的风险点

### 新增测试

`tests/test_lexical_score_fix.py`（41 项）：

- `test_old_formula_would_invert` — 直接证明旧公式反转
- `test_monotonic_against_relevance` — 相关性越高分数越高
- `test_sql_ordering_matches_score` 组：
  - `test_bm25_ascending` — 返回顺序 bm25 单调递增
  - `test_score_descending` — score 单调递减
  - `test_python_resort_agrees_with_sql` — **按 score 重排不改变顺序**（修复前会失败）
  - `test_top_k_truncation_keeps_best` — Top-K 保留最优

---

## 二、检索模式命名（已修正）

### 问题

原模式名 `fts5_bm25` 容易被理解为「通用 BM25 方案」，但实际是 **FTS5 trigram 字面短语匹配 + BM25 打分**。

trigram 的行为与经典 BM25（词项级倒排）差异很大：

| 特性 | 经典 BM25 | FTS5 trigram |
|---|---|---|
| 分词单位 | 词项 | 3 字符滑窗 |
| 中文支持 | 需分词 | 天然按字切分 |
| 最小查询长度 | 无 | **3 字符** |
| 词干/同义词 | 可配 | 不支持 |

### 修正

```text
fts5_bm25         →  fts5_trigram_phrase
fallback_scan     →  fallback_instr_scan
```

notice 文案明确声明：

```text
字面短语检索（FTS5 trigram + BM25 打分），索引 26 个切片。
这是 trigram 字面匹配，不代表所有 BM25 方案的能力。
未命中不等于政策不存在。
```

### 保留的逻辑

- ✅ 安全转义 `escape_fts_query()`（防语法错误 + 防注入）
- ✅ 短查询回退（< 3 字符走 `instr()` 扫描）

测试：`TestMatchModeNaming` 组，含 7 种特殊字符输入不崩溃验证。

---

## 三、评测指标审计（已补强）

### 问题：三项汇总指标完全相同

项目负责人质疑：Recall / MRR / nDCG 为何都是 0.4231。

### 逐题分析结果

26 条计分用例中：

```text
11 条：完全命中（R=1.0, RR=1.0, nDCG=1.0）
15 条：完全未命中（R=0.0, RR=0.0, nDCG=0.0）
```

**没有任何一条是「部分命中」**，因此三个指标在每条用例上都相等，均值自然相同。

这不是指标实现错误，而是**评测集的分布问题**：命中就是全中，不中就是全无。

### 新增测试证明指标本身正确

`TestMetricAuditability`（10 项）：

| 测试 | 验证内容 |
|---|---|
| `test_recall_multi_evidence_partial` | 4 条证据召回 1 条 = 0.25 |
| `test_rr_varies_with_rank` | 第 1/2/3 名命中 → 1.0 / 0.5 / 0.333 |
| `test_ndcg_penalizes_late_hits` | 命中越靠后 nDCG 越低 |
| `test_three_metrics_differ_with_multiple_evidence` | **多证据部分召回时三者分离** |
| `test_single_evidence_first_hit_all_one` | **解释三者一致的原因** |

### 新增完整证据召回率

```python
def complete_evidence_rate(relevant, retrieved, k) -> float:
    """全部命中得 1，否则 0（按用例而非按条数）"""
    return 1.0 if relevant <= set(retrieved[:k]) else 0.0
```

与 Recall 的区别：

| 场景 | Recall@10 | Complete Evidence Rate |
|---|---|---|
| 3 条证据召回 2 条 | 0.667 | **0** |
| 3 条证据召回 3 条 | 1.0 | **1** |

对跨条款查询，「能否一次召回全部所需条款」比「召回了多少条」更贴近「能否完整回答」。

### 基线新增指标

```text
complete_evidence_rate          : 0.4231
multi_evidence_cases            : 13
multi_evidence_complete_rate    : 0.3846
```

多证据用例的完整召回率（0.3846）低于整体 Recall（0.4231），说明跨条款场景确实更难。

---

## 四、评测集版本与证据定位（已补强）

### v1 处理

**v1 已删除**，只保留 v2。

理由：v1 的标注有实质缺陷（宽泛关键词导致相关集覆盖全部语料，Recall@10 数学上限仅 0.385）。保留会误导后续对比。

`docs/review/stage05a-bm25-baseline.md` 第 4 节完整记录了 v1 → v2 的修正原因。

### v2 证据定位字段

每条用例的相关证据现输出完整溯源：

```json
{
  "chunk_id": "23d472d7eb6e...",
  "policy_id": "00aaf3720779431692a2d422fc7eec49",
  "document_id": "a459ff64-c41c-4b51-9cfe-a84e3892e90e",
  "version": 1,
  "page": null,
  "start": 737,
  "end": 1144,
  "retrieved_rank": 1,
  "retrieved": true
}
```

包含项目负责人要求的全部字段：`document_id` / `policy_id` / `version` / 原文字符偏移（`start`/`end`）。

### 未命中原因分类

新增 `miss_reason` 字段，区分四种情况：

| 分类 | 含义 |
|---|---|
| `corpus_missing` | 语料里根本没有这条政策 |
| `relation_not_built` | 政策关系数据尚未建设 |
| `retrieval_failure` | 证据在语料里，但检索没召回 |
| `no_answer` | 真实无答案（预期为空且确实为空） |

当前基线分布：

```text
retrieval_failure    : 11  (semantic 6 + cross_clause 5)
relation_not_built   :  4  (policy_relation 全部)
```

**重要区分**：`policy_relation` 的 4 条不是「检索效果差」，而是**关系数据未建设**。这两类问题需要不同的解决方案，不能混为一谈。

### 评测集状态

```json
{
  "schema": "policy-retrieval-eval-v1",
  "version": 2,
  "frozen": true,
  "revision_note": "v2 修正 v1 的标注缺陷..."
}
```

**标记为开发评测集**：`docs/review/stage05a-bm25-baseline.md` 第 9 节明确记录「评测集由开发方标注，未经业务人员审核」。

**未根据检索结果修改标准答案**：本次所有标注调整都是因为发现标注本身的技术缺陷（相关集过宽、锚点与 query 不对应），不是因为「模型没检索到所以改标准」。三处修正均记录在案：

1. `kw-06` query 与锚点不一致（`一地注册各方共享` vs `一地注册、各方共享`）
2. `sw-01` query 与锚点不对应（`电价` 却标 `哄抬价格`）
3. v1 宽泛关键词 → v2 条款级锚点

---

## 五、修复前后报告

| 文件 | 内容 |
|---|---|
| `storage/rag/eval-baseline-v2.json` | 修复前 |
| `storage/rag/eval-baseline-after-fix.json` | 修复后（含溯源与 miss_reason） |

两者排序指标完全一致，证明 SQL 层排序一直正确，缺陷只在 score 数值方向。

---

## 六、测试结果

```text
全量回归：1013 passed（971 基线 + 42 新增）
```

新增测试文件：

```text
tests/test_lexical_score_fix.py    41 项
```

更新测试：

```text
tests/test_lexical_search.py       模式名更新（18 处断言）
```

---

## 七、遗留问题

| 项 | 说明 |
|---|---|
| 三项指标仍相同 | 因为评测集无「部分命中」用例。已在测试中证明指标本身正确 |
| `policy_relation` 全 0 | 关系数据未建设，不是检索问题 |
| 评测集未经业务审核 | 当前为开发评测集 |
| 无中文分词 | trigram 按 3 字切分 |

---

## 八、复现命令

```powershell
# 重建索引并评测
python scripts/eval_retrieval.py --rebuild --show-cases

# 输出 JSON（含溯源与 miss_reason）
python scripts/eval_retrieval.py --json storage/rag/eval-baseline-after-fix.json

# 专项测试
python -m pytest tests/test_lexical_score_fix.py -q
python -m pytest tests -q
```
