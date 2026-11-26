# ADR-008：混合检索采用等权 RRF

- **状态**：**已采纳并实施**（2026-11-26）
- **相关阶段**：阶段05.5-A
- **前置决策**：[ADR-003 检索方案](ADR-003-retrieval-stage05.md)、[ADR-007 Dense Retrieval](ADR-007-dense-retrieval.md)

---

## 背景

05-A（BM25）与 05-B（Dense）的对照实验结果：

| 类型 | Lexical | Dense | 谁更强 |
|---|---|---|---|
| exact_keyword | **1.000** | 0.833 | 词法 |
| short_word | **1.000** | 0.800 | 词法 |
| semantic | 0.000 | **0.833** | 向量 |
| cross_clause | 0.000 | **0.779** | 向量 |
| policy_relation | 0.000 | **1.000** | 向量 |

**两者覆盖不同的查询类型，互补而非替代。** 需要融合。

---

## 决策一：用 RRF 而不是分数相加

### 为什么不能直接相加

BM25 分数与余弦相似度的量纲完全不同：

| 指标 | 范围 | 分布特征 |
|---|---|---|
| FTS5 bm25 | 负值，随语料变化（实测 -0.000002 到 -4.46） | 越负越相关 |
| 余弦相似度 | -1 到 1（实测 0.43 到 0.68） | 越大越相关 |

直接把两者归一化后加权相加，等于假设「BM25 的 0.8 与余弦的 0.8 语义等价」——这个假设没有依据。而且 BM25 分数分布随语料规模变化，归一化方式本身就不可移植。

### RRF 的做法

只用**排名**，完全避开分数：

```
RRF(d) = Σ_path  weight_path / (rank_constant + rank_path(d))
```

**性质**：
- 只看排名，不看分数 → 免疫量纲问题
- 某路未召回 → 该路贡献为 0
- 标准 `rank_constant=60`（Cormack et al. 2009）

---

## 决策二：标准等权 RRF 作为首个基线

### 配置锁定（实验前声明）

```python
rank_constant = 60
lexical_candidate_k = 10
dense_candidate_k = 10
final_top_k = 10
weights = {"lexical": 1.0, "dense": 1.0}
```

**为什么先做等权**：

1. 等权不需要调参，是最中性的基线
2. 加权需要验证集支撑，当前只有 30 条开发集
3. 先确立基线，后续加权方案才有对照

### 后续候选（本阶段不实现）

| 方案 | 说明 |
|---|---|
| Weighted RRF | 按路的可靠性加权 |
| DBSF（Distribution-Based Score Fusion） | 基于分数分布归一化后融合 |
| Linear Fusion | 线性加权原始分数（需先解决量纲） |

---

## 决策三：稳定排序与同分处理

### 规则

```python
scored.sort(key=lambda x: (-x[1], x[0]))
#                    RRF 降序   chunk_id 升序
```

**为什么用 chunk_id 作二级键**：

1. 保证结果可复现（同输入必同输出）
2. 不依赖 dict 插入顺序（Python 3.7+ 有序但不该依赖）
3. 不依赖检索器返回顺序（那是分数决定的）

实测验证：`电价` 用例中两路各自的第 1 名 RRF 都是 0.016393，按 chunk_id 升序排列，结果稳定。

---

## 决策四：降级必须显式声明

### 问题

单路检索可能失败。若静默降级为单路，调用方会误以为拿到的是混合结果。

### 决策

```python
degraded: str | None   # None = 完整混合
```

取值：

| 值 | 含义 |
|---|---|
| `None` | 完整混合检索 |
| `lexical_failed` | 词法异常，降级为纯向量 |
| `dense_failed` | 向量异常，降级为纯词法 |
| `both_paths_failed` | 两路都失败，无结果 |

**默认 `allow_degrade=False`**，单路异常直接抛 `RetrieverFailure`。只有显式开启降级才返回部分结果，且 `to_dict()` 的 `match_mode` 会变成 `hybrid_rrf_degraded`。

### 区分「正常空结果」与「异常」

```python
if out.returned == 0 and out.degraded is None:
    # 正常空结果：两路都跑了，都没命中
else if out.degraded is not None:
    # 异常降级
```

这两个是不同的状态，不能混淆。

---

## 决策五：索引一致性校验

### 问题

若 Lexical 索引与 Dense 索引来自不同的切片清单，融合的 chunk_id 会对不上。

### 决策

```python
def check_index_consistency(self) -> dict:
    # 比对两路的 manifest_sha256
    if len(set(values)) != 1:
        if not self.allow_degrade:
            raise HybridError("...拒绝融合")
```

默认**拒绝融合**，不猜测、不自动修复。

### 实测

当前两路索引的 `manifest_sha256` 都是 `ea194999...`，chunk 数与 policy/version 集合完全一致。

---

## 实施结果

### 实验（开发集 30 条，配置预先锁定）

| 指标 | Lexical | Dense | **Hybrid** |
|---|---|---|---|
| Recall@1 | 0.3205 | 0.4009 | **0.4138** |
| Recall@5 | 0.4231 | 0.7931 | **0.8700** |
| Recall@10 | 0.4231 | 0.8421 | **0.9190** |
| MRR | 0.4231 | 0.7234 | **0.7811** |
| nDCG@10 | 0.4231 | 0.7315 | **0.7933** |
| Hit Rate | 0.4231 | 0.8846 | **0.9615** |
| CER | 0.4231 | 0.7692 | **0.8462** |

**H3 在 5/5 主指标上成立。**

### 但必须说明的三点

**1. 分类型指标全部「持平」，不是「提升」**

| 类型 | Lex | Dense | Hybrid | 判断 |
|---|---|---|---|---|
| exact_keyword | 1.000 | 0.833 | 1.000 | 持平（取词法上限） |
| short_word | 1.000 | 0.800 | 1.000 | 持平 |
| semantic | 0.000 | 0.833 | 0.833 | 持平（取向量上限） |
| cross_clause | 0.000 | 0.779 | 0.779 | 持平 |
| policy_relation | 0.000 | 1.000 | 1.000 | 持平 |

**Hybrid 没有让任何类型的 Recall@10 超过两路的最好水平**，它的贡献是「同时拿到两路各自的上限」，体现在**总体**指标（因为总体是混合了各类型）。

这是 RRF 的典型特征：**它不产生新的召回能力，只做排名融合**。

**2. 未做统计显著性检验**

30 条用例太小。`+0.0769` 的 Recall@10 提升在统计上是否稳定，需要更多数据。**报告中使用「在开发集上观测到」而非「显著提升」。**

**3. 证据不足用例仍 0/4**

RRF 只融合排名，不判断证据充分性。`empty-01` 到 `empty-04` 仍必然返回 Top-K。

### 敏感性（开发集调参，非最终配置）

| 配置 | Recall@10 | MRR | CER |
|---|---|---|---|
| rank_constant=10 | 0.9190 | 0.7811 | 0.8462 |
| rank_constant=30 | 0.9190 | 0.7811 | 0.8462 |
| **rank_constant=60（基线）** | 0.9190 | 0.7811 | 0.8462 |
| rank_constant=100 | 0.9190 | 0.7811 | 0.8462 |
| lex_k=5 | 0.9190 | 0.7811 | 0.8462 |
| lex_k=20 | 0.9190 | 0.7811 | 0.8462 |
| dense_k=5 | 0.8700 | 0.7756 | 0.8077 |
| dense_k=20 | 0.9190 | **0.8196** | 0.8462 |

**观察**：
- `rank_constant` 在 10-100 范围内对结果**完全无影响**（因为语料只有 26 切片，排名差异小，常数影响被稀释）
- `lexical_candidate_k` 在 5-20 范围内无影响（词法召回本身就少）
- `dense_candidate_k` 有影响：5 时 Recall 降 0.05，20 时 MRR 提升

**这说明当前语料规模下 RRF 参数不敏感**，也说明这套参数不太可能过拟合。但也意味着 30 条用例无法有效区分参数优劣。

---

## 后果

### 正面

- 总体指标全面优于任一单路
- 零新增依赖
- 索引一致性强制校验
- 降级状态显式，不伪装
- 保留两路原始分数与排名，可审计

### 负面

- **分类型无提升**：Hybrid 只是拿到两路各自的上限
- 耗时增加：17.2ms vs Dense 14.2ms（约 +21%）
- 证据不足判断仍缺失

### 风险

- 30 条用例的结论泛化能力有限
- `rank_constant` 不敏感可能是语料太小的假象

---

## 重新评估触发条件

1. **语料扩到 500+ 切片** — `rank_constant` 才开始有区分度，需重新调参
2. **有验证集**（非开发集）— 可尝试 Weighted RRF / DBSF
3. **出现某类型查询占比极高** — 考虑按类型路由而非统一融合
4. **需要证据充分性判断** — 引入 Reranker 或阈值机制
5. **延迟要求 < 10ms** — 考虑并行化两路检索

---

## 参考

- [Elasticsearch RRF](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion)
- [Qdrant Hybrid Queries](https://qdrant.tech/documentation/hybrid-queries/)
- [OpenSearch RRF](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/)
- Cormack, Clarke, Buettcher (2009). *Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods*. SIGIR.
- 实验数据：`storage/rag/compare-hybrid.json`
- 报告：`docs/review/stage05.5a-hybrid-retrieval.md`
