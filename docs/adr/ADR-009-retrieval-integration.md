# ADR-009：检索工具接入与索引一致性收紧

- **状态**：**已采纳并实施**（2026-11-26）
- **相关阶段**：V2.1 MVP-01 Retrieval Integration
- **前置决策**：ADR-006（本地 Embedding）、ADR-007（NumPy 精确检索）、ADR-008（等权 RRF）

---

## 背景

05.5-A 交付了混合检索与三路对照实验，但存在三个阻碍「接入 Agent」的问题：

1. **索引一致性判定过宽**：任一路指纹缺失时，`check_index_consistency` 会跳过比较，
   把「拿不到指纹」当成「一致」。
2. **版本冲突可被降级绕过**：`allow_degrade=True` 时即使两路索引版本不同也继续融合，
   跨版本的 chunk_id 可能指向不同内容，融合结果不可追溯。
3. **索引格式不安全**：`chunk_ids` 用 `dtype=object` 存储，`np.load` 必须
   `allow_pickle=True`，存在反序列化执行风险。

此外，混合检索能力**没有接入 Agent Runtime**，模型无法使用。

---

## 决策一：索引一致性判定改为严格模式

### 旧逻辑的缺陷

```python
values = [v for v in fingerprints.values() if v]   # ← 过滤掉空值
if len(values) < 2:
    return {"consistent": True, ...}                # ← 缺指纹判为一致
```

后果：索引损坏或旧版索引不记录 `manifest_sha256` 时，两路会被判定「一致」并融合。

### 新逻辑

四种状态显式区分：

| status | 含义 | 行为 |
|---|---|---|
| `ok` | 两路同源 | 完整融合 |
| `single_path_unconfigured` | 只配置一路 | 降级为单路，显式标注 |
| `fingerprint_missing` | 任一指纹为空 | **不得判定一致**；不允许降级则抛错，允许降级则返回空结果 |
| `version_conflict` | 指纹不相等 | **即使 allow_degrade=True 也抛 HybridError** |

### 为什么版本冲突不可降级

假设 lexical 索引来自 manifest A、dense 索引来自 manifest B。
两路返回的同一个 `chunk_id` 可能指向**不同内容**（切片边界变了）。

此时融合产生的「两路都命中此 chunk」是虚假信号，且无法追溯到底命中哪份内容。
因此不是「降级可用」，而是「结果不可信」——必须拒绝。

### 降级时如何确定可信单路

`single_path_unconfigured` 场景下，被使用的那一路必须：
1. 自身指纹非空（可确认版本）
2. 检索成功（未抛异常）

不满足任一条件时，`_empty_outcome` 返回空结果 + `single_path_failed` 状态，
不返回任何候选。

---

## 决策二：模型一致性绑定实际 commit 与 tokenizer 指纹

### 旧逻辑的缺陷

缓存命名空间只记录 `transformers` 库版本号作为 `tokenizer` 字段：

```python
"tokenizer": getattr(transformers, "__version__", "unknown")
```

**这是错的**：库版本相同但词表可能不同（换了模型、换了 tokenizer 配置）。
更严重的是，索引侧完全没校验模型身份，只靠 `dim == 512` 就允许比较向量。

### 新逻辑

**`CacheNamespace` 绑定七项**：

```python
model                  模型名
commit                 实际 commit hash
tokenizer_fingerprint  词表大小 + cls/sep/pad/unk id + model_max_length 的 sha256
dim                    向量维度
pooling                池化方式
normalized             是否归一化
encode_mode            document / query
```

**命名空间不完整则拒绝加载缓存**：

```python
missing = self._namespace.missing_fields()   # model / commit / tokenizer_fingerprint
if missing:
    self._rejected_reason = "namespace_incomplete:" + ",".join(missing)
    return   # 不加载任何旧向量
```

**索引记录模型身份**，加载与每次检索时都校验：

```python
self._require_model_identity()                    # 加载时
embedder._ensure_loaded()
self.check_model_consistency(embedder)            # 每次 search()
```

比对项：`model` / `model_commit` / `tokenizer_fingerprint` / `dim` / `pooling` / `normalized`。
任一不符或无法确认 → `DenseSearchError`。

### commit 如何获取

新版 transformers 不再在 config 上设置 `_commit_hash`（实测为 `None`）。
改用 `huggingface_hub.snapshot_download(local_files_only=True)` 解析本地快照目录名，
目录名即 commit hash。该调用只读本地缓存，不联网。

---

## 决策三：NPZ 改用非 pickle 格式

### 旧格式风险

```python
np.savez_compressed(..., chunk_ids=np.asarray(ids, dtype=object))
```

`dtype=object` 的数组在 `np.load` 时必须 `allow_pickle=True`，
这会执行任意 pickle 反序列化逻辑。索引文件一旦被替换，等同于代码执行。

### 新格式

```python
np.savez_compressed(..., chunk_ids=np.asarray(chunk_ids, dtype=str))
```

定长 Unicode 数组（`<U64`），`allow_pickle=False` 即可加载。

### 加载策略

```python
with np.load(self.index_path, allow_pickle=False) as data:
    ...
except ValueError as exc:
    raise DenseSearchError(
        "索引格式不受支持（可能由旧版本生成）。"
        "出于安全考虑不允许反序列化 pickle 内容，请重建索引。"
    )
```

**不自动兼容旧格式**。旧索引必须显式重建：

```powershell
python scripts/build_dense_index.py --rebuild
```

### schema 版本

- 索引：`policy-dense-index-v1` → **`v2`**
- 缓存：`policy-embedding-cache-v1` → **`v2`**

---

## 决策四：完全离线，禁止自动下载

```python
def __init__(self, ..., local_files_only: bool = True):
```

**默认 True**。缺少模型时：

```python
except OSError as exc:
    raise EmbeddingError(
        f"本地模型不可用（local_files_only=True，不会自动下载）：{model_name}。"
        f"请先在有网环境准备缓存。原始错误：{exc}"
    )
```

不静默联网，不降级为「空向量」。

`scripts/build_dense_index.py` 需显式 `--allow-network` 才允许联网。

---

## 决策五：路径不可由模型控制

`RetrievalPaths` 只从程序配置解析：

```python
base = Path(rag_dir or environ.get(INDEX_DIR_ENV) or DEFAULT_INDEX_DIR)
```

工具的入参模型：

```python
model_config = ConfigDict(strict=True, extra="forbid")
query: str
top_k: int
```

`extra="forbid"` 意味着模型传 `index_path` 之类字段会被直接拒绝。
政策库路径同理，来自 `POLICY_DB_PATH` 环境变量或默认值。

---

## 决策六：新增 search_policy_evidence 工具

### 为什么不改造 search_policy

`search_policy` 是**字面关键词**查询，语义是「按省份+关键词粗查政策清单」。
混合检索是**语义召回**，语义是「找到相关原文片段作证据」。

两者输入、输出、失败模式都不同。混在一起会让模型无法判断该用哪个。
因此保留原工具，新增独立工具。

### 输出契约

```json
{
  "query": "...",
  "retrieval_mode": "hybrid_rrf",
  "index_fingerprint": "...",
  "degraded_status": null,
  "consistency_status": "ok",
  "index_freshness": "ok",
  "candidate_count": 5,
  "evidence_candidates": [
    {
      "evidence_id": "23d472d7eb6ec5c4",
      "chunk_id": "23d472d7eb6ec5c4...",
      "policy_id": "00aaf372...",
      "document_id": "a459ff64-...",
      "version": 1,
      "title": "...",
      "source_url": "https://...",
      "start": 737,
      "end": 1144,
      "page": null,
      "content_sha256": "...",
      "text": "...",
      "text_truncated": false,
      "retrieval_rank": 1,
      "retrieval_score": 0.032787,
      "path_contributions": [...]
    }
  ],
  "candidates_are_not_facts": true,
  "notice": "..."
}
```

### 明确「候选 ≠ 事实」

三处声明：

1. `candidates_are_not_facts: true` 字段
2. `notice` 首句：「以下内容是**检索候选**，不是已核实的事实依据」
3. 工具 `description` 里写明，模型在选择工具时就能看到

### 三层状态

| 情况 | execution_status | business_status | business_code |
|---|---|---|---|
| 正常召回 + 索引新鲜 | ok | success | — |
| 正常召回 + 索引可能过期 | ok | success | `INDEX_MAY_BE_STALE` |
| 无候选 | ok | empty | `NO_EVIDENCE_CANDIDATE` |
| 索引缺失/不可读 | ok | failed | `RAG_INDEX_UNAVAILABLE` |
| 检索异常 | ok | failed | `RAG_RETRIEVAL_FAILED` |
| 参数非法 | invalid_arguments | not_applicable | — |

### 索引与数据库版本一致性

`check_index_freshness()` 从政策库读出每个 policy 的所有 `(version, content_sha256)`，
与清单中的比对：

- 索引有、库里没有 → `stale`
- 库里有更高版本、索引没有 → `stale`
- 无法读取（库不可用 / 清单不可读）→ `fresh=False`，**宁可标注不可信**

**不允许把陈旧索引描述为最新政策**：不新鲜时 `business_code = INDEX_MAY_BE_STALE`，
`notice` 明确提示「结果可能不是最新政策」。

### 资源限制

| 项 | 上限 |
|---|---|
| query 长度 | 200 字符 |
| top_k | 20（默认 5） |
| 单条候选正文 | 800 字符（超出截断 + `text_truncated=true`） |

### 编码器复用

`PolicyEvidenceService` 持有 `_embedder` 与 `_hybrid` 两个单例：

```python
service.search(...)   # 首次加载模型（约 30 秒）
service.search(...)   # 复用，约 15 ms
```

测试验证：两次检索返回同一个对象 id。

---

## 决策七：运行时集成不改变默认行为

`RunRequest` 新增 `use_retrieval: bool = False`：

- `False`（默认）：仍走 `search_policy` → `get_policy_detail`，行为与之前完全一致
- `True`：走 `search_policy_evidence`

这样原有测试与演示流程零影响，新能力按需开启。

`build_live_registry` 新增 `with_retrieval: bool = True` 与 `rag_dir` 参数，
索引缺失时工具仍注册，首次调用返回 `RAG_INDEX_UNAVAILABLE`，
不影响 `search_policy` / `get_policy_detail`。

---

## 后果

### 正面

- 索引一致性判定不再有「拿不到指纹就算一致」的漏洞
- 版本冲突硬拒绝，跨版本融合不可能发生
- NPZ 加载不再需要 pickle
- 模型身份绑定到 commit + tokenizer 指纹，换模型会被立即发现
- 完全离线，缺模型明确报错
- 模型可用混合检索，且输出明确区分「候选」与「事实」

### 负面

- **旧索引必须重建**（v1 → v2，且 object 数组不再支持）
- 缓存全部失效（命名空间字段变化）
- 每次 `search()` 都做一次模型一致性校验（实测开销可忽略，均为内存比对）
- `DenseSearch` 构造时必须能解析 commit，非 HF 布局的本地模型目录会失败

### 风险

- 若有外部脚本硬编码旧 schema，会直接报错（这是有意的失败关闭）
- `snapshot_download` 依赖 huggingface_hub 的缓存布局

---

## 重新评估触发条件

1. **需要支持非 HF 布局的本地模型目录** —— 需扩展 `_resolve_identity`
2. **模型一致性校验成为性能瓶颈** —— 改为构造时校验一次 + 索引指纹缓存
3. **需要支持增量索引** —— 当前 `_require_model_identity` 假设单次构建
4. **证据充分性判断上线** —— 届时 `candidates_are_not_facts` 的措辞需重新评估
5. **多租户 / 多索引** —— 当前路径为单例配置，需改为可注入

---

## 参考

- 前序决策：ADR-006 / ADR-007 / ADR-008
- 实现：`powercopy/rag/dense.py`、`powercopy/rag/hybrid.py`、`powercopy/rag/embedding.py`
- 工具：`powercopy/tools/builtin/retrieval.py`
- 测试：`tests/test_retrieval_integration.py`
- 报告：`docs/review/mvp-01-retrieval-integration.md`
- NumPy 安全加载：[numpy.load allow_pickle](https://numpy.org/doc/stable/reference/generated/numpy.load.html)
