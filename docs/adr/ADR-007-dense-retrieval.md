# ADR-007：05-B Dense Retrieval 采用 NumPy 精确检索

- **状态**：**已采纳并实施**（2026-11-26）
- **相关阶段**：阶段05-B
- **前置决策**：[ADR-006 本地 Embedding](ADR-006-local-embedding.md)

---

## 背景

05-A 建立了 BM25 词法基线，指标显示：

```text
exact_keyword  Recall@10 = 1.000
short_word     Recall@10 = 1.000
semantic       Recall@10 = 0.000
cross_clause   Recall@10 = 0.000
```

词法检索对语义改写和跨条款查询完全无覆盖。需要向量检索补齐。

ADR-006 已确定 Embedding 来源：本地 BGE-small-zh-v1.5，512 维。

---

## 决策一：不用向量数据库，用 NumPy 精确检索

### 计算

```text
26 个切片 × 512 维 = 13,312 个 float32 = 52 KB
单次查询：26×512 矩阵乘法实测 5.7 μs
```

### 结论

引入 Milvus / Qdrant / Chroma 是**过度设计**。

| 方案 | 依赖体积 | 查询延迟 | 运维成本 |
|---|---|---|---|
| **NumPy 精确检索** | 0（已有） | 5.7 μs | 0 |
| Milvus | ~500 MB | ~1 ms | 需独立服务 |
| Qdrant | ~100 MB | ~1 ms | 需独立服务 |
| Chroma | ~50 MB | ~2 ms | 嵌入式 |

NumPy 不仅零依赖，**实际比向量数据库更快**（因为数据量小到完全在 L1 缓存里）。

### 触发引入向量数据库的条件

1. 切片数 > 10 万（此时暴力检索 > 50ms）
2. 需要 ANN 近似检索的召回-延迟权衡
3. 需要增量更新与并发写

---

## 决策二：FastEmbed + ONNX 改为 torch + transformers

### 背景

任务书要求「优先使用此前批准的 FastEmbed + ONNX Runtime CPU 方案」。

**实际核实结果**：

```text
fastembed     未安装
onnxruntime   未安装
torch         2.15.0+cpu  已安装
transformers  5.24.1      已安装
```

已安装的是 torch 路径，且已实测可用（单条 7.4ms）。

### 项目负责人决策

**继续用 torch + transformers，不装 ONNX。**

理由：

1. **已实测可用**，零新增依赖
2. ONNX 需重新下载模型（转换格式），增加 150-300 MB
3. 两个后端共存会让依赖管理复杂化
4. 当前 CPU 推理 15ms 已满足需求

### 后果

- ✅ 零新增依赖，venv 保持 876 MB
- ⚠️ 与任务书原始方案不一致，已在报告中明确记录
- ⚠️ torch 占 513 MB，是 venv 最大的单一依赖

### 重新评估触发条件

1. 需要更快推理（ONNX 通常比 torch CPU 快 1.5-2 倍）
2. 需要减少部署体积（ONNX Runtime 约 50 MB vs torch 513 MB）
3. 需要边缘设备部署

---

## 决策三：禁止静默截断

### 问题

HuggingFace tokenizer 默认 `truncation=True` 会把超长文本**静默切掉**：

```python
batch = tokenizer(texts, truncation=True, max_length=512)
```

如果政策条款在 512 token 之后被截掉，向量就不代表完整内容，检索会漏掉关键证据。

### 决策

```python
def encode(self, texts, *, strict_length: bool = True):
    if strict_length:
        over = self.check_lengths(texts)
        if over:
            raise EmbeddingError("...拒绝静默截断...")
```

**默认严格模式**，超长直接报错。调用方必须显式传 `strict_length=False` 才能截断。

### 提供独立的长度校验 API

```python
embedder.token_lengths(texts)   # 返回每条的 token 数
embedder.check_lengths(texts)   # 返回超限明细，不抛异常
```

### 实测

真实 26 个切片全部在 512 token 内（最长 499 字符），无需分片。

---

## 决策四：向量缓存与索引指纹

### 缓存

按 `text_sha256` 缓存向量，避免切片重算时重复编码：

```text
首次编码 26 条：3240 ms
命中缓存：< 10 ms
```

缓存文件是 JSON，key 是 sha256（不存原文，避免泄露政策正文）。

### 索引指纹

索引必须与以下三者同时匹配，否则拒绝使用：

```python
meta = {
    "schema": "policy-dense-index-v1",
    "manifest_sha256": "...",   # 切片清单指纹
    "model": "BAAI/bge-small-zh-v1.5",
    "model_commit": "...",
    "dim": 512,
    "pooling": "cls_l2",
    "normalized": True,
}
meta["fingerprint"] = sha256(sorted_json(meta))
```

`DenseSearch.__init__` 校验：

```python
if self._meta["manifest_sha256"] != manifest["manifest_sha256"]:
    raise DenseSearchError("索引与切片清单不匹配")
```

**不猜测、不自动修复**，直接拒绝。

---

## 决策五：证据不足不设相似度阈值

### 问题

向量检索**必然返回 Top-K**，即使语料里完全没有相关内容。

实测：

```text
query = "安徽电力现货市场交易规则"（库里无安徽政策）
→ 返回 10 条，最高分 0.53
```

这是向量检索的本质，不是 bug。

### 决策

**本阶段不设相似度阈值。**

理由：

1. 阈值需要评测数据支撑，当前 4 条空用例不足以确定阈值
2. 不同查询类型的分数分布差异大（实测 0.43-0.68）
3. 随意设阈值会造成两种错误：把有证据判为无证据，把无证据判为有证据

### 当前处理

- `notice` 字段每次声明「相似度高低不代表证据充分」
- 评测中**分别记录**「候选召回」与「证据充分性」
- `empty` 用例的 `empty_ok` 在 Dense 下必然为 False，如实记录

### 后续

证据充分性判断需要独立机制（重排模型 / 二分类器 / 阈值标定），留待后续阶段。

---

## 实施结果

### 文件

```text
powercopy/rag/dense.py              向量索引 + 检索 + 缓存（约 520 行）
tests/test_dense_retrieval.py       46 项测试
scripts/bench_dense.py              性能测量
scripts/compare_retrieval.py        对照实验
```

### 对照实验结果

| 指标 | Lexical | Dense | 差值 |
|---|---|---|---|
| Recall@1 | 0.3205 | 0.4009 | +0.0804 |
| Recall@5 | 0.4231 | **0.7931** | **+0.3700** |
| Recall@10 | 0.4231 | **0.8421** | **+0.4190** |
| MRR | 0.4231 | **0.7234** | +0.3003 |
| nDCG@10 | 0.4231 | **0.7315** | +0.3084 |
| Hit Rate | 0.4231 | **0.8846** | +0.4615 |
| Complete Evidence Rate | 0.4231 | **0.7692** | +0.3461 |

### 假设检验

| 假设 | 目标 | 实测 | 结论 |
|---|---|---|---|
| H1 | semantic + cross_clause Recall@10 > 0.5 | **0.8085** | ✅ 成立 |
| H2 | exact_keyword + short_word Recall@10 ≥ 0.8 | **0.8182** | ✅ 成立 |
| H3 | 混合检索优于单路 | — | 留待下一阶段 |

### 关键发现：词法与向量互补

| 类型 | Lexical | Dense | 谁更强 |
|---|---|---|---|
| exact_keyword | **1.000** | 0.833 | 词法 |
| short_word | **1.000** | 0.800 | 词法 |
| semantic | 0.000 | **0.833** | 向量 |
| cross_clause | 0.000 | **0.779** | 向量 |
| policy_relation | 0.000 | **1.000** | 向量 |

**这不是「向量比词法好」，而是两者覆盖不同的查询类型。** 这正是 H3（混合检索）的价值所在。

---

## 后果

### 正面

- 语义与跨条款查询从 0 提升到 0.78-0.83
- 零新增向量数据库依赖
- 索引指纹与缓存机制保证可复现
- 禁止静默截断，避免隐性数据损失

### 负面

- **Dense 在 empty 用例上 0/4 通过**（必然返回 Top-K）
- 冷启动 31 秒（模型加载），首次检索体验差
- 批量编码 26 条需 3.2 秒
- 词法优势类型（精确关键词）Dense 反而下降 0.17

### 风险

- 无证据充分性判断，可能误导下游
- 语料仅 26 切片，指标代表性有限

---

## 重新评估触发条件

1. **切片数 > 10 万** — 考虑 ANN 索引（FAISS / Milvus）
2. **需要证据充分性判断** — 引入重排模型或阈值标定
3. **冷启动时间不可接受** — 考虑服务常驻或 ONNX 加速
4. **语料扩展到千份以上** — 重新评估 512 维是否够用
5. **多语言需求** — 换 BGE-M3

---

## 参考

- [BGE-small-zh-v1.5 模型卡](https://mteb-leaderboard.hf.space/models/BAAI/bge-small-zh-v1.5)
- ADR-003：阶段05 检索方案
- ADR-006：本地 Embedding
- 对照实验：`storage/rag/compare-v1.json`
- 报告：`docs/review/stage05b-dense-retrieval.md`
