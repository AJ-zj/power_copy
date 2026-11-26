# Embedding 来源核实报告

**核实日期**：2026-11-26
**核实方式**：只读探测，不产生任何 Token 计费
**结论**：平台 `/v1/embeddings` **端点存在**，但**当前 API Key 所在分组无 embedding 通道**

---

## 一、平台侧核实

### 1.1 `/models` 列表

```text
GET /v1/models → HTTP 200，26 个模型
```

全部是对话模型，**无一个 embedding 模型**。

### 1.2 `/embeddings` 端点探测

用「故意非法」的请求探测，只在参数校验阶段被拒，不产生计费：

| 请求 | 状态码 | 响应 |
|---|---|---|
| `GET /v1/embeddings` | 404 | `Invalid URL (GET /v1/embeddings)` |
| `POST /v1/embeddings` `{}` | 400 | `input is empty` |
| `POST` `{"model":"glm-5.3-flash","input":[]}` | 400 | **`Embeddings are not supported on this channel.`** |
| `POST` `{"model":"bge-m3","input":[]}` | 503 | `No available channel for model bge-m3 under group yituohub-火山-国模全家桶` |
| `POST` `{"model":"text-embedding-3-small","input":[]}` | 503 | 同上，无可用通道 |
| `POST` `{"model":"不存在的模型","input":[]}` | 503 | 同上 |

### 1.3 关键判断

**端点本身存在**。证据：`POST /v1/embeddings` 返回 `input is empty`，说明网关认识这个路径并做了参数校验。

**但当前凭证的分组不支持**：

- `glm-5.3-flash` 报 `Embeddings are not supported on this channel`（模型存在，但该通道不支持 embeddings）
- `bge-m3` / `text-embedding-3-small` 报 `No available channel ... under group yituohub-火山-国模全家桶`（该模型不在当前分组里）

**因此**：不是平台不提供服务，而是**当前 API Key 所属分组（`yituohub-火山-国模全家桶`）没有 embedding 通道**。

这与项目负责人的判断一致：「当前模型列表未发现 Embedding，不等于证明平台不存在该服务。」

---

## 二、本机硬件核实

### 2.1 CPU

```text
Intel Core i5-8265U @ 1.60GHz
4 物理核心 / 8 逻辑核心
```

### 2.2 内存

```text
总内存：15.55 GB
当前可用：7.4 GB
```

### 2.3 GPU

```text
NVIDIA GeForce GTX 1050 with Max-Q Design
显存：4096 MiB（4 GB）
驱动：560.94
CUDA：可用（nvidia-smi 正常）
Intel UHD Graphics 620（集显，1024 MB，不用）
```

### 2.4 磁盘

```text
C: 可用 37.4 GB / 99.7 GB
D: 可用 202.9 GB / 376 GB
```

项目在 D 盘，空间充足。

### 2.5 已安装的 ML 依赖

```text
torch                MISS
numpy                MISS
onnxruntime          MISS
transformers         MISS
sentence_transformers MISS
sentencepiece        MISS
tokenizers           MISS
scipy                MISS
```

**venv 总计 67.2 MB**，纯业务依赖，无任何 ML 包。

### 2.6 SQLite 能力

```text
SQLite 版本：3.53.1
FTS5：可用
FTS5 trigram：可用
```

**结论**：FTS5 trigram 基线可以立即开始，零依赖。

---

## 三、候选模型规格对照

| 模型 | 参数量 | 向量维度 | 模型大小 | 许可证 | 4GB 显存 | 本机可行性 |
|---|---|---|---|---|---|---|
| **BGE-small-zh-v1.5** | 33M | 512 | **91 MB** | MIT | ✅ 轻松 | **最优** |
| BGE-M3 | 568M | 1024 | 2.1 GB | MIT | ⚠️ 勉强 | 可行但重 |
| Qwen3-Embedding-0.6B | 0.6B | 32–1024 | ~1.2 GB | Apache-2.0 | ⚠️ 紧张 | CPU 为主 |

数据来源：
- [BGE-small-zh-v1.5 MTEB](https://mteb-leaderboard.hf.space/models/BAAI/bge-small-zh-v1.5) — 33M 参数，512 维，91 MB，MIT
- [BGE-M3 MTEB](https://leaderboard.mteb.org/models/dragonkue/BGE-m3-ko) — 568M 参数，1024 维，2.1 GB
- [Qwen3-Embedding-0.6B](https://www.siliconflow.com/models/qwen3-embedding-0-6b) — 0.6B，32–1024 维，Apache-2.0

### 依赖体积估算

| 方案 | 需要安装 | 体积 |
|---|---|---|
| BGE-small-zh-v1.5 | torch + transformers + sentence-transformers | **~2.5 GB** |
| BGE-M3 | 同上（模型更大） | ~4.5 GB |
| Qwen3-Embedding-0.6B | 同上 | ~3.7 GB |

**注意**：三个方案的主要体积都是 PyTorch 本身（CPU 版约 200MB，CUDA 版约 2.5GB），不是模型。

如果只装 CPU 版 torch，依赖可以压到约 300MB。

---

## 四、三条路径的判断

### 路径 A：向平台申请 embedding 通道

**做法**：联系易拓 AI，申请含 embedding 模型的分组（如 bge-m3）。

**优点**
- 零本地依赖
- 与现有 LLM 调用方式统一

**缺点**
- 需等待平台开通
- 政策正文出本地，有合规考量
- 按 Token 计费，需评估成本

**可行性**：未验证，需向平台确认。

### 路径 B：本地部署 BGE-small-zh-v1.5（推荐）

**做法**：`pip install torch --index-url .../cpu` + `transformers` + 模型 91MB。

**优点**
- 模型仅 91MB，CPU 可跑
- MIT 许可证，商用无限制
- 数据不出本地
- 4GB 显存轻松跑，甚至不需要 GPU

**缺点**
- 新增约 300MB（CPU 版 torch）依赖
- 首次加载模型约 2-3 秒

**可行性**：高。i5-8265U 8 线程跑 33M 参数模型，单条 512 token 编码预计 20-50ms，26 个切片全部向量化不到 2 秒。

### 路径 C：暂不做向量，先做 BM25

**做法**：用已确认可用的 FTS5 trigram 建词法检索基线。

**优点**
- 零依赖
- 立即可做
- 建立评测基线，为后续向量方案提供对照

**缺点**
- 无同义词扩展
- 中文按 3 字切分

**可行性**：已确认可用（SQLite 3.53.1 + FTS5 trigram）。

---

## 五、建议

**先做路径 C，同时准备路径 B。**

理由：

1. **路径 C 零成本立即可做**，能建立评测基线。没有基线就无法判断向量方案是否真的更好。

2. **路径 B 的依赖代价需要你确认**。虽然 BGE-small 只有 91MB，但 torch 有 300MB（CPU）到 2.5GB（CUDA）。这会打破项目「零新增依赖」的一贯原则，需要单独决策。

3. **路径 A 需要外部协调**，不在我们控制范围内。

4. **26 个切片用不到向量数据库**。512 维 × 26 条 = 13,312 个浮点数，NumPy 暴力计算余弦相似度 < 1ms，引入 Milvus/Qdrant 是过度设计。

### 待你决策的三件事

1. **是否允许安装 CPU 版 PyTorch（约 300MB）** 用于本地 BGE-small？
2. **是否同时向平台申请 embedding 通道**（作为备选/对照）？
3. **BGE-small 的 512 维是否够用**，还是需要 BGE-M3 的 1024 维（模型 2.1GB）？

---

## 六、本次核实未做的事

- ❌ 未安装任何 ML 依赖
- ❌ 未下载任何模型
- ❌ 未发起任何计费调用（所有探测都在参数校验阶段被拒）
- ❌ 未修改任何代码

---

## 七、参考

- [BGE-small-zh-v1.5 模型卡](https://mteb-leaderboard.hf.space/models/BAAI/bge-small-zh-v1.5)（访问 2026-11-26）
- [BGE-M3 模型卡](https://leaderboard.mteb.org/models/dragonkue/BGE-m3-ko)（访问 2026-11-26）
- [Qwen3-Embedding-0.6B](https://www.siliconflow.com/models/qwen3-embedding-0-6b)（访问 2026-11-26）
- 项目内证据：本报告 §1.2 的探测记录
