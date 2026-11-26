# 05-A BM25 检索基线报告

**日期**：2026-11-26
**阶段**：阶段05-A 词法检索基线
**测试基线**：971 passed

---

## 一、做了什么

用 SQLite 内置 FTS5 的 `trigram` tokenizer + `bm25()` 排序，为 26 个切片建立词法检索索引，并跑出一份可量化的基线指标。

**零新增依赖**（`trigram` 是 SQLite 3.53.1 内置能力）。

---

## 二、核心发现：trigram 的两字符限制

实测确认，这不是 bug 而是 tokenizer 的硬约束：

| 查询 | 长度 | 结果 |
|---|---|---|
| `电` | 1 | 0 条 |
| `电价` | 2 | **0 条** |
| `电力市` | 3 | 命中 |
| `电力市场` | 4 | 命中 |

`trigram` 按 3 字符滑窗建立索引，因此**查询本身也必须 >= 3 字符**才能匹配。

项目负责人要求「必须设计少于 3 个字符的查询回退机制」——这个要求是必要的，不是可选项。

### 回退策略

```python
if len(query) >= 3:
    FTS5 MATCH + bm25() 排序   # match_mode = "fts5_bm25"
else:
    instr() 全表扫描 + 命中次数排序  # match_mode = "fallback_scan"
```

对 26 个切片，全表扫描耗时 < 1ms，完全够用。

---

## 三、基线指标

### 总体

```text
Recall@10     : 0.4231
MRR           : 0.4231
nDCG@10       : 0.4231
命中率         : 0.4231  (11 / 26)
证据不足用例   : 4 / 4 正确返回空
```

### 分类

| 类型 | 用例数 | Recall@10 | 命中率 | 解读 |
|---|---|---|---|---|
| exact_keyword | 6 | **1.000** | **1.000** | 精确词组，词法检索的最强项 |
| short_word | 5 | **1.000** | **1.000** | 回退扫描生效，2 字符查询全部命中 |
| semantic | 6 | **0.000** | **0.000** | 语义改写，词法完全覆盖不到 |
| cross_clause | 5 | **0.000** | **0.000** | 跨条款综合，需多切片拼接 |
| policy_relation | 4 | **0.000** | **0.000** | 关系型查询，需政策关系数据 |
| insufficient_evidence | 4 | — | 4/4 正确返回空 | 无幻觉 |

**这组数字非常干净地划出了 BM25 的能力边界**：

- 词法层（精确词组、短词）**100% 覆盖**
- 语义层（改写、跨条款、关系）**0% 覆盖**

这正是引入向量检索的理由，也是对照基线的价值所在。

---

## 四、评测集设计

`evaluations/retrieval-eval-v2.json`，30 条用例，已 `frozen: true`。

### 六类覆盖

| 类型 | 数量 | 示例 |
|---|---|---|
| exact_keyword | 6 | "电能计量点"、"市场管理委员会" |
| short_word | 5 | "电价"、"注销"、"监管"（2 字符） |
| semantic | 6 | "失信了会有什么后果" → 第四十九条 |
| cross_clause | 5 | "注销后还能不能重新入市" |
| policy_relation | 4 | "国家发改委通过了哪部电力市场规章" |
| insufficient_evidence | 4 | "安徽电力现货市场交易规则" |

### 标注方式：锚点而非 Chunk ID

按项目负责人要求「保留原文证据位置，不仅依赖 Chunk ID」，评测集用**条款级锚点字符串**标注：

```json
{
  "id": "sem-03",
  "query": "失信了会有什么后果",
  "anchors": ["纳入失信管理"],
  "evidence": "NEA 第四十九条：失信管理措施"
}
```

运行时把锚点与切片原文做字面匹配确定相关集。

**好处**：重新切片后 chunk_id 会变，但「纳入失信管理」这个原文片段不受影响，评测集不需要重annotate。

### v1 → v2 的修正

第一版（v1）用宽泛关键词标注，导致相关集过宽：

| 用例 | v1 锚点 | 相关集 | Recall@10 数学上限 |
|---|---|---|---|
| sw-05 | `电` | 26/26 | 0.385 |
| sem-02 | `注册,经营主体` | 26/26 | 0.385 |
| xr-01 | `经营主体,权利,义务` | 26/26 | 0.385 |

相关集覆盖全部语料时，Recall@10 在数学上不可能超过 10/26=0.385，指标失真。

**v2 改用条款级精确锚点**，相关集控制在 1-11 条，全部用例的 Recall@10 上限 ≥ 0.909。

v1 文件已删除，只保留 v2。

---

## 五、实现

### 新增文件

| 文件 | 行数 | 说明 |
|---|---|---|
| `powercopy/rag/lexical.py` | 434 | 索引构建 + 检索 + 评测指标 |
| `tests/test_lexical_search.py` | 460 | 59 项测试 |
| `tests/test_retrieval_eval.py` | 320 | 22 项测试 |
| `scripts/eval_retrieval.py` | 290 | 评测脚本 |
| `evaluations/retrieval-eval-v2.json` | — | 30 条评测集 |

### 关键设计

**1. 查询转义（防注入 + 防语法错误）**

```python
def escape_fts_query(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'
```

包成双引号短语。这样：

- `AND` / `OR` / `NOT` / `NEAR` 按字面处理，不触发布尔语义
- `*` / `-` / `(` / `)` 不会导致语法错误
- 内部双引号加倍转义，不会破坏短语

实测：`AND`、`*`、`"`、`电力 OR 注册` 等 10 种特殊输入均不崩溃。

**2. 分数归一化**

FTS5 的 `bm25()` 返回负值、越小越相关。为便于未来与向量分数融合，统一转成 0-1、越大越相关：

```python
# FTS 模式
score = 1 / (1 + abs(bm25))
# 回退模式
score = 命中次数 / (命中次数 + 1)
```

两者都是单调映射，保持原始排序不变。

**3. 只读保证**

```python
sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
conn.execute("PRAGMA query_only=ON")
```

测试验证：检索前后索引文件字节一致，`DELETE` 被拒绝。

**4. 索引与业务库分离**

```text
storage/policies_versioned.db   业务库（政策原文）
storage/rag/lexical.db          检索索引（从切片重建）
```

索引是**派生产物**，可随时从 `chunks.json` 重建，不入 git。

---

## 六、如何复现

```powershell
# 重建索引并评测
python scripts/eval_retrieval.py --rebuild

# 输出 JSON 报告
python scripts/eval_retrieval.py --json storage/rag/eval-baseline.json

# 查看每条用例明细
python scripts/eval_retrieval.py --show-cases
```

---

## 七、这份基线的意义

**它给出的不是「检索效果好不好」，而是「词法检索的能力边界在哪」**：

```text
能做的：精确词组、短词片段 → 100%
做不到的：语义改写、跨条款综合、关系推理 → 0%
```

这正是 05-B 向量检索要验证的假设：**向量能否补上语义与跨条款的缺口**。

如果 05-B 上线后 semantic 和 cross_clause 的 Recall 没有显著提升，那就说明在当前语料规模下向量检索价值有限，应如实记录而非强行保留。

---

## 八、下一步

**05-B 向量检索**（前置条件已满足）：

- 本地 BGE-small-zh-v1.5 已就绪（ADR-006）
- 索引能力已有（`BGEEmbedder`）
- 基线数字已确定（本报告）

**待验证的假设**：

```text
H1: 向量检索能在 semantic 类上把 Recall@10 从 0 提升到 > 0.5
H2: 向量检索在 exact_keyword 上不应比 BM25 差太多（< 0.2 降幅）
H3: 混合检索（BM25 + 向量加权）应显著优于任一单路
```

**05-B 的验收标准**（预先声明，避免事后调整）：

- semantic + cross_clause 的 Recall@10 相比基线提升 ≥ 0.3
- exact_keyword + short_word 的 Recall@10 不低于 0.8
- 若 H1 不成立，如实记录「当前语料规模下向量检索无显著收益」，不强行引入

---

## 九、已知局限

| 项 | 说明 |
|---|---|
| 语料仅 2 份政策 | 26 个切片，任何方案的收益都受限 |
| 未做中文分词 | trigram 按 3 字切分，短词靠回退扫描 |
| 无同义词扩展 | "电价" 无法匹配 "电费" |
| 无重排 | 单一 BM25 打分 |
| policy_relation 类为 0 | 库里没有政策关系数据，这类指标当前无意义 |
| 评测集由开发方标注 | 未经业务人员审核，专业政策解释可能有偏差 |

---

## 十、参考

- [SQLite FTS5 文档](https://www.sqlite.org/fts5.html)（trigram tokenizer 与 bm25 函数）
- 项目内实现：`powercopy/rag/lexical.py`
- 评测集：`evaluations/retrieval-eval-v2.json`
- ADR-003：阶段05 检索方案
- ADR-006：本地 Embedding
