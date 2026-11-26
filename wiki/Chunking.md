# 文档切片

> 阶段05 · 5.1 Document Chunking

## 为什么需要切片

RAG 检索不能把 6000 字的政策整篇塞给模型。需要切成合理大小的片段，同时保证：

1. **可追溯**——每个片段能回溯到原文的精确位置
2. **无遗漏**——不能丢任何业务正文
3. **确定性**——同样输入必须得到同样输出

## 核心设计

### 精确字符区间

每个切片记录 `[start, end)`：

```python
assert chunk["text"] == body_text[chunk["start"]:chunk["end"]]
```

这是 Python Unicode 字符位置，**不是字节坐标**。

### 自然边界优先

```python
end = min(start + max_chars, len(text))
if end < len(text):
    floor = start + (max_chars * 3 + 3) // 4     # 窗口最后四分之一
    for marks in ('\n', '。！？；!?;'):           # 换行优先，其次句末
        candidates = [i+1 for i in range(floor-1, end) if text[i] in marks]
        if candidates:
            end = candidates[-1]
            break
```

**为什么在最后四分之一找**：太早断会切出小碎片，最后四分之一是「既能自然断句、又不会太短」的平衡点。

**优先级**：换行 > 句末标点 > 硬切

### 硬长度上限

```python
assert 0 < chunk["end"] - chunk["start"] <= max_chars
```

无论有没有自然边界，都不会超过 `max_chars`。

### Overlap

```python
start = end - config.overlap_chars
```

相邻切片重叠固定字符数，避免边界处丢失上下文。

### 参数约束

```python
@dataclass(frozen=True)
class ChunkConfig:
    max_chars: int = 500
    overlap_chars: int = 80

    def __post_init__(self):
        if (type(self.max_chars) is not int or type(self.overlap_chars) is not int
            or not 32 <= self.max_chars <= 4096
            or not 0 <= self.overlap_chars <= self.max_chars // 4):
            raise ChunkError('INVALID_CHUNK_CONFIG')
```

| 参数 | 范围 | 默认 |
|---|---|---|
| `max_chars` | 32 – 4096 | 500 |
| `overlap_chars` | 0 – `max_chars // 4` | 80 |

`type(x) is not int` 位判断是为了防止 `True` 冒充整数（`type(True)` 是 `bool`）。

## 真实数据结果

### NEA 电力市场注册基本规则

```text
正文长度 : 6010 字符
切片数   : 16
最小切片 : 284 字符
最大切片 : 496 字符
首块     : [0, 465)
末块     : [5726, 6010)
非空白字符 : 5845，未覆盖 0
```

### NDRC 电力市场运行基本规则

```text
正文长度 : 3829 字符
切片数   : 10
最小切片 : 416 字符
最大切片 : 499 字符
首块     : [0, 435)
末块     : [3349, 3829)
非空白字符 : 3703，未覆盖 0
```

### Manifest

```text
schema    : policy-chunk-manifest-v1
algorithm : policy-char-v1
config    : {max_chars: 500, overlap_chars: 80}
chunks    : 26
manifest_sha256 : ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
```

## 确定性

### 重复构建

```text
第一次 manifest_sha256 : ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
第二次 manifest_sha256 : ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
chunk_id 序列一致      : True
```

### chunk_id 的构成

```python
key = dict(
    algorithm=ALGORITHM,              # 'policy-char-v1'
    config=asdict(config),            # max_chars, overlap_chars
    document_id=str(doc.document_id), # 具体文档
    content_sha256=row['content_sha256'],
    start=start,
    end=end,
    page=page,
    text_sha256=text_sha,
)
chunk_id = digest(key)
```

**关键**：`chunk_id` 绑定**具体文档**，不绑定最近一次采集时间。所以同一文档多次采集（`unchanged`），切片 ID 不变。

### 配置敏感性

不同配置的 chunk_id 完全不重叠：

| 配置 | 切片数 | manifest 前 16 位 |
|---|---|---|
| 500 / 80 | 26 | `ea194999ae37ba86` |
| 500 / 0 | 22 | `3529a8dc1e3df521` |
| 300 / 60 | 47 | `38b650bb87fb06ae` |
| 1000 / 200 | 13 | `127d5a1653163b5f` |

## 版本变化

内容变化后，`chunk_id` 全部更新：

```text
V1: version=1, document_id=56642f36-..., 10 chunks
V2: version=2, document_id=0a9168b2-..., 10 chunks

document_id 变化   : True
chunk_id 完全不重叠 : True
默认清单只含 V2    : True
历史 V1 未混入     : True
```

**注意**：V1 仍在库里（`versions` 计数为 2），只是不进入默认清单。默认 `selection='latest_observed_only'`。

## PDF 页码映射

### 分页约定

PDF 正文用 `\n\f\n` 分隔页：

```python
PAGE_BREAK = '\n\f\n'
body_text = PAGE_BREAK.join(page.text for page in pages)
```

### 页码验证

```text
第 1 页 [  0,  25)  '测试材料 非真实政策\n第一条 交易周期为15分钟。'
第 2 页 [ 28,  53)  '测试材料 非真实政策\n第二条 电价为3元/千瓦时。'
```

与 4.8 预览结果逐字符一致。

### 不跨页保证

```python
assert page.start <= chunk["start"] < chunk["end"] <= page.end
assert "\f" not in chunk["text"]
```

切片在页边界处强制断开，不会跨越物理页。

### 页码来源

```python
def _page_ranges(doc, row, raw_pdf_dir):
    fid = row['fetch_id']
    path = Path(raw_pdf_dir) / (fid + '.json')
    if path.is_file():
        artifact = load_pdf_artifact(path)
        # 验证 fetch_id、raw_sha256、来源、元数据、时间全部匹配
        if (artifact.raw_sha256 != row['raw_sha256'] or ...):
            raise ChunkError('PDF_PROVENANCE_MISMATCH')
        pieces = doc.body_text.split(PAGE_BREAK)
        ...
    else:
        if '\f' in doc.body_text or source_url.endswith('.pdf'):
            raise ChunkError('PDF_ARTIFACT_REQUIRED')
        yield None, 0, doc.body_text    # HTML 文档，无页码
```

**HTML 文档**：`page=None`
**PDF 文档**：必须有匹配的原始档案，否则拒绝

### 缺档保护

| 场景 | 错误码 |
|---|---|
| PDF 原始档案缺失 | `PDF_ARTIFACT_REQUIRED` |
| `raw_sha256` 被篡改 | `INVALID_PDF_ARTIFACT` |
| 元数据被篡改 | `PDF_PROVENANCE_MISMATCH` |

## 清单结构

```json
{
  "schema": "policy-chunk-manifest-v1",
  "algorithm": "policy-char-v1",
  "config": {"max_chars": 500, "overlap_chars": 80},
  "selection": "latest_observed_only",
  "documents": [
    {
      "policy_id": "...",
      "document_id": "...",
      "version": 1,
      "content_sha256": "...",
      "source_key": "ndrc",
      "source_url": "https://...",
      "title": "...",
      "body_length": 3829,
      "chunk_count": 10,
      "reference_fetch_id": "...",
      "raw_sha256": "..."
    }
  ],
  "chunks": [
    {
      "chunk_id": "...",
      "policy_id": "...",
      "document_id": "...",
      "version": 1,
      "start": 0,
      "end": 435,
      "page": null,
      "text": "...",
      "text_sha256": "...",
      "reference_fetch_id": "...",
      "raw_sha256": "...",
      "content_sha256": "..."
    }
  ],
  "manifest_sha256": "...",
  "notice": "Latest observed, not verified legally effective..."
}
```

## 使用

### 构建清单

```powershell
python -m scripts.build_chunks --db storage/policies_versioned.db --out storage/rag/chunks.json
```

自定义配置：

```powershell
python -m scripts.build_chunks --max-chars 300 --overlap-chars 60
```

### 输出保护

CLI 会拒绝覆盖非清单文件：

```python
if out.exists():
    previous = json.loads(out.read_text(encoding='utf-8'))
    if previous.get('schema') != 'policy-chunk-manifest-v1':
        parser.error('refusing to overwrite a non-manifest file')
```

### 原子替换

```python
with tempfile.NamedTemporaryFile(..., dir=out.parent, delete=False) as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)
tmp.replace(out)
```

先写临时文件再替换，避免写一半崩溃留下损坏文件。

## 只读保证

```python
sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
conn.execute('PRAGMA query_only=ON')
```

构建前后数据库 SHA256 完全一致：

```text
构建前 : E452BE78560755C98C5688B195E375EDD4A2E44822A65FBB9668F1404D72A337
构建后 : E452BE78560755C98C5688B195E375EDD4A2E44822A65FBB9668F1404D72A337
```

## 局限

| 限制 | 说明 |
|---|---|
| 无增量索引 | 每次全量重建 |
| 字符数不是 Token | 实际 Token 取决于模型 tokenizer |
| 无页码查询接口 | `page` 字段已存，但 `PolicyQueryService` 未暴露 |
| 无并发协调 | 多个构建进程同时写同一输出无锁 |
| 内容哈希含页边界 | 仅分页方式变化也会产生新版本 |
| 依赖 4.8 解析 | 不重新验证 PDF 内部结构 |

## 下一步

在切片之上做检索：

- 向量化（embedding）
- BM25 关键词检索
- 混合检索与重排

这些是阶段 05 后续内容，尚未开发。

## 相关文档

- [数据中心详解](Data-Center) — 切片的上游
- [测试与验收](Testing) — 切片的验证方式
