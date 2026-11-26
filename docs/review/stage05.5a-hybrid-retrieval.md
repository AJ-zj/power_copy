# 05.5-A 混合检索（等权 RRF）报告

**日期**：2026-11-26
**阶段**：阶段05.5-A Hybrid Retrieval
**测试基线**：1059 → 1122 passed

---

## 一、前置核查结果

按项目负责人要求，先做四项核查。

### 核查 1：policy_relation 的矛盾

**发现的真实缺陷**

报告中「policy_relation Recall@10 = 1.000」与「未解决问题中全 0」确实矛盾。

**根因**：`scripts/eval_retrieval.py` 的 `classify_miss()` 有三处逻辑错误：

```python
# 错误版本
if case.get("type") == "policy_relation":
    return "relation_not_built"        # ← 无条件返回，不管是否命中
if not (relevant & set(retrieved[:top_k])):
    return "retrieval_failure"         # ← 这行永远执行不到
```

问题：
1. `policy_relation` 分支在「是否命中」判断**之前**，导致命中了也被标为 `relation_not_built`
2. `relation_not_built` 根本不该出现在 miss 分类里——它不是「未命中的原因」，而是「需要另一类能力的说明」
3. 因此 `miss_breakdown` 显示 4 条 `relation_not_built`，而分组指标显示 Recall=1.000，自相矛盾

**修复**：

```python
# 先判断是否真的未命中
if relevant & set(retrieved[:top_k]):
    return None
# 再分类
if not anchors: return "annotation_missing"
if not any(anchor in corpus_text for anchor in anchors): return "corpus_missing"
return "retrieval_failure"
```

移除了 `relation_not_built` 这个分类——它属于**能力说明**，不属于**失败原因**。

**修复后**：`miss_breakdown` 只含 `retrieval_failure`，与分组指标一致。

**区分两个不同概念**（项目负责人要求）：

| 概念 | 含义 | 当前状态 |
|---|---|---|
| **文本证据检索** | 能否检索到承载关系表述的**原文片段** | ✅ 已实现，Dense Recall@10 = 1.000 |
| **图关系推理** | 「政策 A 替代政策 B」这类**关系判断** | ❌ 关系数据未建设 |

两者的证据来源不同：
- 前者靠 `chunks` 表（原文切片）
- 后者需要独立的**政策关系表**（policy_id → relation → policy_id + 证据偏移）

当前 `rel-01` 到 `rel-04` 测的是前者（检索发文字号、1+N 体系、上位文件引用等原文片段），**不是**图关系推理。

### 核查 2：Embedding 缓存隔离

**发现不完整**：原缓存只校验 `schema` + `model` + `dim`，缺少 tokenizer 版本、pooling 方式、归一化标志、编码模式。

**修复**：引入 `cache_namespace()`：

```python
{
    "model": "BAAI/bge-small-zh-v1.5",
    "dim": 512,
    "pooling": "cls_l2",
    "normalized": True,
    "encode_mode": "document",
    "tokenizer": "5.24.1",       # transformers 版本
    "max_tokens": 512,
}
```

**逐项比对**，任何一项不符则整个缓存失效：

```python
for key, expected in self._namespace.items():
    if stored.get(key) != expected:
        self._rejected_reason = f"{key}_mismatch"
        return          # 不加载任何旧向量
```

并暴露 `cache.rejected_reason`，便于诊断。

### 核查 3：索引存储实况

| 项 | 实测值 |
|---|---|
| 文件大小 | 51,685 B（50.5 KB） |
| 数组 `vectors` | shape=(26, 512), dtype=**float32**, 53,248 B |
| 数组 `chunk_ids` | shape=(26,), dtype=object |
| 数组 `meta_json` | shape=(), dtype=`<U480` |
| 理论向量内存 | 26×512×4 = **53,248 B** ✓ |
| 范数范围 | [1.000000, 1.000000] |
| 数值有限性 | True |

**存储格式**：`np.savez_compressed`（ZIP + DEFLATE）。压缩后 50.5 KB < 原始 53.2 KB，因为 float32 向量有冗余。

**内存测量口径说明**：
- `bench_dense.py` 用 `tracemalloc`，峰值 **252.6 MB**
- 这个数字**包含** torch 加载模型权重的 Python 堆分配
- 不包含 CUDA 显存（本次纯 CPU）
- 不包含操作系统页缓存中的模型文件

### 核查 4：两索引同源验证

```text
manifest_sha256      : ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
  lexical 记录       : ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
  dense 记录         : ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2

三者 sha 一致        : True
manifest chunk 数    : 26
lexical chunk 数     : 26
dense chunk 数       : 26

lexical == manifest  : True
dense == manifest    : True

(policy,version) 集合: [('00aaf372...', 1), ('70c4a432...', 1)]
版本集合一致          : True
```

**结论**：两路索引同源，chunk_id 集合与版本集合完全一致，可安全融合。

---

## 二、技术选型

采用**标准等权 RRF**，参数预先锁定：

```python
rank_constant = 60
lexical_candidate_k = 10
dense_candidate_k = 10
final_top_k = 10
weights = {"lexical": 1.0, "dense": 1.0}
```

### 为什么不用分数相加

| 指标 | 范围 | 分布 |
|---|---|---|
| FTS5 bm25 | 负值（实测 -0.000002 到 -4.46） | 越负越相关，随语料变化 |
| 余弦相似度 | -1 到 1（实测 0.43 到 0.68） | 越大越相关 |

两者量纲不可比。归一化后相加等于假设「BM25 的 0.8 与余弦的 0.8 等价」，无依据。

### 为什么先做等权

1. 等权不需调参，是最中性基线
2. 加权需验证集，当前只有 30 条开发集
3. 先确立基线，后续 Weighted RRF / DBSF / Linear Fusion 才有对照

---

## 三、实现

### 文件

| 文件 | 行数 | 说明 |
|---|---|---|
| `powercopy/rag/hybrid.py` | 610 | RRF 融合模块 |
| `tests/test_hybrid_retrieval.py` | 640 | 63 项测试 |
| `scripts/compare_hybrid.py` | 500 | 三路对照实验 |
| `docs/adr/ADR-008-hybrid-rrf.md` | — | 决策记录 |

### 数据契约

```python
HybridOutcome:
    query                 : str
    hits                  : list[HybridHit]
    rank_constant         : int
    lexical_candidate_k   : int
    dense_candidate_k     : int
    final_top_k           : int
    weights               : dict[str, float]
    lexical_mode          : str | None
    dense_mode            : str | None
    lexical_returned      : int
    dense_returned        : int
    degraded              : str | None      # None = 完整混合
    degraded_detail       : str | None
    notice                : str
    index_fingerprints    : dict[str, str]

HybridHit:
    chunk_id, rrf_score, rank
    policy_id, document_id, version, page, start, end
    title, source_key, source_url, content_sha256, text
    contributions : list[PathContribution]

PathContribution:
    path        : "lexical" | "dense"
    rank        : int | None      # 未召回为 None
    score       : float | None    # 该路原始分数
    match_mode  : str | None
    rrf         : float           # 该路贡献，未召回为 0.0
```

### 架构流程

```text
query
  │
  ├─→ 索引一致性检查（manifest_sha256 比对）
  │     不一致且不允许降级 → 抛 HybridError
  │
  ├─→ LexicalSearch.search(query, top_k=lex_k)
  │     → hits + match_mode
  │     → 异常：允许降级则标记 lexical_failed
  │
  ├─→ DenseSearch.search(query, top_k=dense_k)
  │     → hits + match_mode
  │     → 异常：允许降级则标记 dense_failed
  │
  ├─→ RRF 融合
  │     contribution = weight / (rank_constant + rank)
  │     按 chunk_id 聚合求和
  │     排序：(-rrf_score, chunk_id)
  │     截断 final_top_k
  │
  └─→ HybridOutcome（含 contributions、降级状态、notice）
```

### 关键实现点

**1. RRF 计算**

```python
def rrf_contribution(rank, rank_constant, weight):
    if rank is None:
        return 0.0
    if rank < 1:
        raise HybridError("rank 必须从 1 开始")
    return weight / (rank_constant + rank)
```

实测：`rank=1, k=60, w=1.0 → 0.01639344 = 1/61` ✓

**2. 稳定排序**

```python
scored.sort(key=lambda x: (-x[1], x[0]))   # RRF 降序, chunk_id 升序
```

**3. 异常捕获范围**

不只捕获 `LexicalSearchError` / `DenseSearchError`，捕获 `Exception`：

```python
except Exception as exc:  # noqa: BLE001
    if not self.allow_degrade:
        raise RetrieverFailure(f"词法检索失败（{type(exc).__name__}）：{exc}")
    failed_paths.append(("lexical", f"{type(exc).__name__}: {exc}"))
```

理由：真实故障可能是 `MemoryError`、编码错误、文件损坏等任意异常。只捕特定类型会让降级机制在真实故障时失效。异常类型名记录在 `degraded_detail` 中，不吞信息。

**4. 降级状态显式化**

```python
degraded = None                     # 完整混合
degraded = "lexical_failed"         # 降级为纯向量
degraded = "dense_failed"           # 降级为纯词法
degraded = "both_paths_failed"      # 无结果
```

`to_dict()["match_mode"]` 在降级时变为 `hybrid_rrf_degraded`，`notice` 含 `⚠ 降级模式`。

---

## 四、实验指标

**配置预先锁定，未根据结果回调。**

### 总体指标

| 指标 | Lexical | Dense | **Hybrid** | Hybrid vs 最佳单路 |
|---|---|---|---|---|
| Recall@1 | 0.3205 | 0.4009 | **0.4138** | +0.0129 |
| Recall@5 | 0.4231 | 0.7931 | **0.8700** | +0.0769 |
| Recall@10 | 0.4231 | 0.8421 | **0.9190** | +0.0769 |
| MRR | 0.4231 | 0.7234 | **0.7811** | +0.0577 |
| nDCG@10 | 0.4231 | 0.7315 | **0.7933** | +0.0618 |
| Hit Rate | 0.4231 | 0.8846 | **0.9615** | +0.0769 |
| Complete Evidence Rate | 0.4231 | 0.7692 | **0.8462** | +0.0770 |
| 证据不足用例通过 | **4/4** | 0/4 | 0/4 | -4 |

### 分类型指标（Recall@10）

| 类型 | n | Lexical | Dense | Hybrid | 判断 |
|---|---|---|---|---|---|
| cross_clause | 5 | 0.0000 | 0.7788 | 0.7788 | **持平**（取向量上限） |
| exact_keyword | 6 | 1.0000 | 0.8333 | 1.0000 | **持平**（取词法上限） |
| policy_relation | 4 | 0.0000 | 1.0000 | 1.0000 | **持平** |
| semantic | 6 | 0.0000 | 0.8333 | 0.8333 | **持平** |
| short_word | 5 | 1.0000 | 0.8000 | 1.0000 | **持平**（取词法上限） |

### H3 检验

**声明**：混合检索优于单路检索（至少一个主指标高于 Lexical 和 Dense）

**结果**：Hybrid 在 **5/5** 主指标上优于最佳单路。

**但必须说明**：

1. **分类型指标全部「持平」**。Hybrid 没有让任何类型的 Recall@10 超过两路的最好水平，它的贡献是「同时拿到两路各自的上限」。这是 RRF 的典型特征——**它不产生新的召回能力，只做排名融合**。

2. **未做统计显著性检验**。30 条用例太小。`+0.0769` 在统计上是否稳定需要更多数据。本报告使用「在开发集上观测到」，不使用「显著提升」。

3. **证据不足用例仍 0/4**。RRF 只融合排名，不判断证据充分性。

### 敏感性实验（开发集调参结果）

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
- `rank_constant` 在 10-100 完全无影响（语料只有 26 切片，常数被稀释）
- `lexical_candidate_k` 5-20 无影响（词法召回本身少）
- `dense_candidate_k` 有影响：5 时 Recall 降 0.05，20 时 MRR 升 0.038

**⚠ 以上为开发集调参结果，不代表泛化性能，不作为最终配置依据。**

---

## 五、逐题结果

见 `storage/rag/compare-hybrid.json` 的 `lexical.cases` / `dense.cases` / `hybrid.cases`。

完整可用以下命令重现：

```powershell
python scripts/compare_hybrid.py --show-cases
```

---

## 六、Bad Case

### Hybrid 相对单路下降的用例

**无**——Hybrid 未在任何用例上低于最佳单路。

这是 RRF 的性质决定的：融合后排序是两路排名的加权组合，理论上不会让某条用例的相关切片排到比它在最强单路中更靠后的位置（除非候选深度不足）。

### Dense 相对 Lexical 下降的用例（05-B 遗留）

| 用例 | 类型 | 查询 | Lex | Dense | Hybrid |
|---|---|---|---|---|---|
| kw-04 | exact_keyword | 市场管理委员会 | 1.00 | 0.00 | 1.00 |
| sw-01 | short_word | 电价 | 1.00 | 0.00 | 1.00 |
| sem-01 | semantic | 企业参与交易不能串通抬价 | 0.00 | 0.00 | 0.00 |

前两条被 Hybrid 救回（词法路径兜底），第三条两路都失败。

**sem-01 分析**：「企业参与交易不能串通抬价」→ 原文是「不得实行串通报价、哄抬价格」。
- 词法：查询与原文无 3 字以上连续匹配 → 0 命中
- 向量：语义跨度大（抬价 vs 哄抬价格），未进入 Top-10

这是真正的难点，需 Reranker 或查询改写才能解决。

### 证据不足用例

| 用例 | 查询 | Hybrid returned |
|---|---|---|
| empty-01 | 安徽电力现货市场交易规则 | 10 |
| empty-02 | 广东省售电公司管理办法 | 10 |
| empty-03 | 碳排放配额交易规则 | 10 |
| empty-04 | 2026年居民阶梯电价补贴标准 | 10 |

**RRF 不判断证据充分性**，仅记录候选返回情况，不宣称有效证据。

---

## 七、耗时

10 条查询平均（热启动）：

| 路径 | 平均 | 范围 |
|---|---|---|
| Lexical | 2.11 ms | 1.9 - 2.4 ms |
| Dense | 14.16 ms | 12.4 - 16.5 ms |
| **Hybrid** | **17.20 ms** | 13.9 - 19.8 ms |

Hybrid 比 Dense 多 3.04 ms（+21%），主要是两路串行执行。可优化为并行，但当前规模不值得。

---

## 八、测试

```text
专项（hybrid）：63 passed
全量回归      ：1122 passed（1059 + 63）
```

覆盖矩阵：

| 组 | 数量 | 内容 |
|---|---|---|
| RRF 公式 | 11 | rank=1/2/10、None、权重、单调性、非法参数 |
| 双路累加 | 3 | 贡献求和、分别记录、两路优先 |
| 单路命中 | 3 | 各自贡献、未召回为 0 |
| 去重 | 2 | 同 chunk 合并、无重复 ID |
| 稳定排序 | 4 | 同分按 ID、确定性、rank 连续、分数降序 |
| 空处理 | 4 | 双空、空查询、非法 K、K 超候选 |
| 异常降级 | 6 | 抛错、两路降级、双失败、notice 显式、空≠失败 |
| 非法参数 | 9 | rank_constant、weights、candidate_k、双 None |
| 候选深度 | 4 | 各自深度、深候选更多结果、final_k 独立 |
| 索引一致性 | 3 | 同哈希、不一致拒绝、降级允许 |
| 证据元信息 | 3 | 全字段、可序列化、贡献结构 |
| 权重 | 3 | 默认等权、自定义、零权重 |
| 真实集成 | 5 | 一致性、真实查询、两路覆盖、元信息、notice |

### 未回归验证

原有 `tests/test_lexical_search.py`（59）、`test_lexical_score_fix.py`（41）、`test_dense_retrieval.py`（46）、`test_retrieval_eval.py`（22）全部通过，检索能力无回归。

---

## 九、复现命令

```powershell
# 三路对照实验
python scripts/compare_hybrid.py --json storage/rag/compare-hybrid.json

# 含逐题与敏感性
python scripts/compare_hybrid.py --show-cases --sweep

# 专项测试
python -m pytest tests/test_hybrid_retrieval.py -q

# 全量回归
python -m pytest tests -q
```

---

## 十、未解决问题

| 问题 | 说明 | 归属 |
|---|---|---|
| 证据充分性判断 | RRF 对无证据查询必然返回 Top-K（0/4） | 后续阶段 |
| 分类型无提升 | Hybrid 只取两路上限，不产生新召回 | RRF 固有性质 |
| 未做显著性检验 | 30 条用例不足以支撑统计结论 | 需扩评测集 |
| 语料仅 26 切片 | `rank_constant` 不敏感可能是假象 | 需扩语料 |
| 图关系推理缺失 | 政策关系表未建设 | 05-C |
| sem-01 类语义跨越 | 两路均失败 | 需 Reranker |
| 评测集未经业务审核 | 开发评测集 | 需业务核实 |

---

## 十一、本轮修复的两个真实缺陷

1. **`classify_miss` 逻辑错误** —— 导致 `policy_relation` 分组指标与 `miss_breakdown` 自相矛盾
2. **缓存隔离不完整** —— 缺 tokenizer / pooling / 归一化 / 编码模式校验

两者都是项目负责人核查时发现的，已修复并有测试覆盖。

---

## 十二、结论

**H3 在开发集上成立（5/5 主指标），但不构成「显著提升」。**

按重要性排序：

1. **Hybrid 的价值是「同时拿到两路各自的上限」**，不是产生新召回
2. **词法与向量的互补性已被数据证实**，融合是合理架构
3. **证据充分性判断仍是空缺**，这是比融合更需要解决的问题
4. **语料规模太小（26 切片）**，所有参数的敏感性和指标的稳定性都受限

本轮仅实现 Hybrid Retrieval 与 RRF 融合。未实现 Reranker、GraphRAG、Adaptive Router。未执行网络采集、付费 API 调用或远程推送。
