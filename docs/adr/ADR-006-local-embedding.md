# ADR-006：本地 Embedding 采用 BGE-small-zh-v1.5

- **状态**：**已采纳并实施**（2026-11-26）
- **相关阶段**：阶段05 RAG
- **关联问题**：P0-3（Embedding 来源）

---

## 背景

阶段05 的检索需要向量化能力。复审时发现平台 `/v1/models` 列表无 embedding 模型。

后续只读探测确认：

```text
POST /v1/embeddings  {}                                    → 400 "input is empty"
POST /v1/embeddings  {"model":"glm-5.3-flash"}             → 400 "Embeddings are not supported on this channel"
POST /v1/embeddings  {"model":"bge-m3"}                    → 503 "No available channel ... under group yituohub-火山-国模全家桶"
POST /v1/embeddings  {"model":"text-embedding-3-small"}    → 503 同上
```

**结论**：端点存在，但当前 API Key 所属分组无 embedding 通道。

---

## 备选方案

### 方案 A：向平台申请 embedding 通道

**优点**：零本地依赖，调用方式与现有 LLM 统一。

**缺点**：需平台开通（不受控）、政策正文出本地、按 Token 计费。

**项目负责人决策**：**否**。

### 方案 B：本地部署 BGE-small-zh-v1.5（采纳）

**优点**
- 模型 91 MB，24M 参数，CPU 可跑
- MIT 许可证，商用无限制
- 数据不出本地
- 4GB 显存轻松，实际上不需要 GPU

**缺点**
- 引入 torch + transformers，约 900 MB 依赖

### 方案 C：暂不做向量，只用 BM25

**优点**：零依赖，立即可做。

**缺点**：无同义扩展，中文按 3 字切分。

**项目负责人决策**：先做 BM25 基线，同时准备向量（即 A+B 并行，落地时选 B）。

---

## 决策

**采纳方案 B：本地部署 BAAI/bge-small-zh-v1.5。**

参数：
- 向量维度 **512**（项目负责人明确：暂不考虑 1024 维的 BGE-M3）
- 设备 **CPU**
- 池化方式 **CLS pooling**（BGE 官方用法，非 mean pooling）
- 归一化 **L2**，检索用余弦相似度（等价于内积）

---

## 本机实测数据

### 硬件

| 项 | 实测 |
|---|---|
| CPU | Intel i5-8265U，4 核 8 线程，1.6 GHz |
| 内存 | 15.55 GB 总 / 7.4 GB 可用 |
| GPU | NVIDIA GTX 1050 Max-Q，4 GB（**未使用**） |
| 磁盘 | D 盘可用 202.9 GB |

### 模型性能

```text
参数量         : 24.0M
hidden size    : 512
最大序列长度   : 512
模型文件       : 91.4 MB
tokenizer 加载 : 4250 ms（首次，含磁盘 IO）
model 加载     : 51978 ms（首次，含权重加载）
```

### 编码速度

```text
单批 6 条 x 10 轮  : 44.2 ms/轮
单条平均           : 7.4 ms
单条首次           : 9.3 ms（含调度开销）
```

**26 个切片全部编码约 0.2 秒**，完全满足需求。

### 语义质量抽查

```text
句子：
  [0] 电价形成机制
  [1] 电价是怎么确定的
  [2] 电力市场注册流程

余弦相似度：
       0      1      2
  0  1.000  0.758  0.498
  1  0.758  1.000  0.542
  2  0.498  0.542  1.000

近义对 (0,1) = 0.758
无关对 (0,2) = 0.498
```

近义显著高于无关（0.758 vs 0.498），语义区分能力达标。

---

## 实现

### 新增文件

| 文件 | 说明 |
|---|---|
| `powercopy/rag/embedding.py` | `BGEEmbedder` 编码器 + 相似度工具 |
| `tests/test_embedding.py` | 28 项测试 |
| `requirements-embedding.txt` | 可选依赖清单 |
| `docs/review/embedding-source-assessment.md` | 核实报告 |

### 关键设计

**1. 惰性加载**

```python
def _ensure_loaded(self):
    if self._model is not None:
        return
    # 只在第一次 encode 时加载
```

仅 `import` 不占 90 MB 内存。

**2. 查询 / 文档分离**

BGE 官方建议查询加前缀，文档不加：

```python
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

def encode_query(self, text):      # 加前缀
def encode_documents(self, texts): # 不加前缀
```

混用会显著降低召回质量，因此接口层面强制区分。

**3. 依赖隔离**

torch 不在主 `requirements.txt` 中：

```text
requirements.txt             主依赖（轻量）
requirements-embedding.txt   可选（torch + transformers）
pyproject.toml               [project.optional-dependencies].embedding
```

未安装 torch 时，`BGEEmbedder` 抛 `EmbeddingError` 并提示安装命令；测试自动 skip，不影响其他 862 项。

**4. 模型缓存位置**

```python
_DEFAULT_CACHE = Path(__file__).resolve().parents[3] / "storage" / "models"
os.environ.setdefault("HF_HOME", str(_DEFAULT_CACHE))
```

模型存在项目内 `storage/models/`，已加入 `.gitignore`，不污染用户目录。

---

## 后果

### 正面

- 向量检索能力就绪，不被平台限制
- 数据不出本地
- MIT 许可证，商用无风险
- 单条 7.4 ms，足够实时检索

### 负面

- **依赖体积 876 MB**（venv 从 67 MB 增至 876 MB）
- **模型加载慢**：首次 52 秒（启动成本）
- **测试时间翻倍**：19s → 42s（模型加载）

### 风险控制

- 模型加载后常驻，多次调用无重复开销
- 测试用 `module` scope fixture 复用同一个 embedder
- 生产环境应在服务启动时预热

---

## 重新评估触发条件

1. **平台开通 embedding 通道** —— 可对比本地 vs 平台的质量与成本
2. **法务要求数据不出内网** —— 已是本地，无变化
3. **512 维召回质量不足** —— 评测 Recall@10 低于阈值时考虑 BGE-M3（1024 维，2.1 GB）
4. **切片数 > 10 万** —— 编码耗时超过可接受范围，考虑批量离线编码
5. **需要多语言** —— BGE-small-zh 仅中文优化，多语言场景需换 BGE-M3
6. **许可证变更** —— 当前 MIT，若上游变更需重新评估

---

## 未验证项

- ❌ 真实检索召回率（需先建评测集与 BM25 基线）
- ❌ GPU 加速效果（当前用 CPU，未测 CUDA）
- ❌ 长文本（> 512 token）截断对召回的影响
- ❌ 与 BM25 混合后的最优权重

---

## 参考

- [BGE-small-zh-v1.5 MTEB 模型卡](https://mteb-leaderboard.hf.space/models/BAAI/bge-small-zh-v1.5)（访问 2026-11-26）
- [BGE-M3 模型卡](https://leaderboard.mteb.org/models/dragonkue/BGE-m3-ko)（访问 2026-11-26）
- [Qwen3-Embedding-0.6B](https://www.siliconflow.com/models/qwen3-embedding-0-6b)（访问 2026-11-26）
- 核实报告：`docs/review/embedding-source-assessment.md`
- 实现：`powercopy/rag/embedding.py`
