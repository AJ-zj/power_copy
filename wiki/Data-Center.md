# 数据中心详解

数据中心是 PowerCopy V2 最核心的部分。它负责把**不可信的官方网站内容**变成**可审计的业务证据**。

## 全链路

```text
①  Source Registry        白名单：许访问哪些地址
        ↓
②  HttpFetcher           拿原始字节，不解码
        ↓
③  Raw JSON 存档          Base64 + SHA-256 + 元数据
        ↓
④  Parser / PDF Ingest   解码 → 提取 → 清洗
        ↓
⑤  PolicyDocument        严格数据契约
        ↓
⑥  VersionedPolicyStore  身份/版本/观测三元组
        ↓
⑦  PolicyQueryService    只读查询
        ↓
⑧  Chunking              可追溯切片
```

## ① Source Registry

### 为什么需要白名单

如果让模型直接传 URL：

```python
fetch(url=llm_generated_url)   # ❌ 危险
```

模型可能编造网址、访问内部地址、被恶意页面注入。所以采用**受控数据源注册**。

### 精确域名匹配

```python
if parsed.hostname != source.allowed_host:   # ✅ 精确匹配
    raise FetchError("host_not_allowed")

if "ndrc.gov.cn" in url:                     # ❌ 子串匹配
```

子串匹配会错误接受 `zfxxgk.ndrc.gov.cn.evil.com`。

### 注册的数据源

| source_key | 名称 | 域名 | 采集目标 |
|---|---|---|---|
| `ndrc` | 国家发展和改革委员会 | `zfxxgk.ndrc.gov.cn` | `market_rules`, `policy_index` |
| `nea` | 国家能源局 | `zfxxgk.nea.gov.cn` | `registration_rules` |
| `nea_east` | 国家能源局华东监管局 | `hdj.nea.gov.cn` | `anhui_market_news`, `policy_index` |

### 三重校验

注册时校验（配置错误尽早暴露）：

1. URL 必须是 https
2. URL 的 host 必须等于 `allowed_host`
3. 不能有账号密码、片段标识

采集时校验：

1. 请求前检查目标 URL
2. 响应后检查最终 URL（纵深防御，防止重定向或 opener 被替换）

## ② HttpFetcher

### 安全措施

```python
build_opener(
    ProxyHandler({}),      # 禁用环境代理
    NoRedirect(),          # 禁止自动重定向
)
```

### 为什么不自动重试

采集工作流尚未建立。贸然重试可能增加政府网站压力。重试、限流、退避统一在 `IngestionPipeline` 中设计。

### 响应大小限制

```python
body = response.read(self.max_bytes + 1)   # 多读 1 字节
if len(body) > self.max_bytes:
    raise FetchError("response_too_large")
```

**为什么 +1**：如果只读 `max_bytes`，无法判断响应恰好等于上限还是被截断。

### 为什么返回 bytes 而不是 str

政府网站可能用 UTF-8，也可能用 GBK、GB18030。直接 `.decode("utf-8")` 会乱码。

**Fetcher 只采集，Parser 才解码。** 这样 Parser 出错不必重访网站。

### 错误分类

| 错误码 | 触发条件 | 重试 |
|---|---|---|
| `http_403` | Forbidden | 否（暂停整个 run） |
| `http_429` | Too Many Requests | 否（暂停整个 run） |
| `http_5xx` | 服务端错误 | 是 |
| `timeout` | 网络超时 | 是 |
| `network_error` | 连接失败 | 是 |
| `redirect_not_allowed` | 3xx | 否 |
| `response_too_large` | 超限 | 否 |
| `empty_response` | 空 body | 否 |

## ③ Raw JSON 存档

原始字节以 Base64 存入 JSON，与元数据放在一起：

```json
{
  "fetch_id": "722b8057489c4ac2a54461d62dad117a",
  "source_key": "ndrc",
  "target_key": "market_rules",
  "source_url": "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379",
  "content_kind": "policy",
  "status_code": 200,
  "content_type": "text/html",
  "charset": "utf-8",
  "body_size": 19055,
  "body_base64": "PCFET0NUWVBFIGh0bWw+...",
  "retrieved_at": "2026-11-25T08:51:06.311679+00:00"
}
```

**为什么 Base64**：JSON 不能存 bytes。Base64 是可逆编码，不是加密或压缩，会增加约 1/3 体积。

**好处**：Parser 可以离线重放，不用重访网站。

## ④ Parser

### 字符编码识别

```python
if body.startswith(BOM_UTF8):
    encoding = "utf-8-sig"
elif body.startswith(BOM_UTF32):
    raise ParseError("UNSUPPORTED_CHARSET")
else:
    declarations = _meta_encodings(body)      # 从 <meta> 读
    if charset:
        declarations.add(_encoding(charset))  # HTTP header 也算
    if len(declarations) > 1:
        raise ParseError("CHARSET_CONFLICT")  # 冲突就拒绝
    encoding = next(iter(declarations), "utf-8")
```

**绝不这样写**：

```python
body.decode("utf-8", errors="ignore")   # ❌ 静默丢弃字节
```

如果丢失的是政策条款里的数字或单位，会造成严重业务错误。

### CSS Selector 配置

```json
{
  "ndrc/market_rules": {
    "title_selector": "h1",
    "body_selector": ".zhengwen",
    "published_selector": "meta[name=\"PubDate\"]",
    "issuer_selector": "meta[name=\"SiteName\"]",
    "region": null,
    "expected_title_contains": "电力市场运行基本规则",
    "exclude_selectors": []
  }
}
```

### 严格单节点匹配

```python
def _one(soup, selector):
    matches = soup.select(selector)
    if len(matches) != 1:
        raise ParseError(f"SELECTOR_MATCH_COUNT:{len(matches)}:{selector}")
    return matches[0]
```

| 匹配数 | 含义 | 处理 |
|---|---|---|
| 0 | 未找到，网页可能改版 | 拒绝 |
| 1 | 唯一匹配 | 进入字段提取 |
| 3 | 选择器不够精确 | 拒绝 |

**为什么不选「最像的」**：网站改版时应该报错，而不是随便找一个相似节点继续生成政策文档。

### exclude_selectors

NEA 页面有两个坑：

1. 隐藏的 SEO 节点 `<p style="display:none">电力,市场,主体,...</p>`
2. 文末相关链接「答记者问」

配置：

```json
"exclude_selectors": [
  "p[style*=\"display:none\"]",
  "h1[style*=\"display:none\"]",
  "p:has(a)"
]
```

在正文容器内、清洗之前精确剔除。

## ⑤ PolicyDocument

```python
class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    document_id: UUID
    source_key: str
    source_url: AnyHttpUrl
    title: str
    body_text: str
    region: str | None
    issuer: str | None
    published_on: date | None
    retrieved_at: AwareDatetime
```

### 为什么 frozen

政策文档是**不可变快照**。一旦入库，内容不应被修改——要改就是新版本。

### 为什么 extra="forbid"

防止 Parser 意外写入未定义字段，污染数据契约。

## ⑥ VersionedPolicyStore

### 三种动作

| action | 含义 | 数据库变化 |
|---|---|---|
| `created` | 首次采集 | +1 身份 +1 版本 +1 观测 +1 文档 |
| `unchanged` | 内容相同，新采集 | +1 观测 |
| `updated` | 内容变化 | +1 版本 +1 观测 +1 文档 |
| `replayed` | 同一 fetch_id 重放 | 无变化 |

### 幂等与冲突

```python
old = conn.execute("... WHERE f.fetch_id=?", (fetch_id,)).fetchone()
if old:
    if old['input_sha256'] != fingerprint:
        raise VersionError('FETCH_ID_CONFLICT')   # 同 id 不同内容
    return IngestResult('replayed', ...)          # 同 id 同内容
```

`input_sha256` 是**输入指纹**，包含来源、时间、原始哈希、内容哈希、解析配置。

### 迟到事件

```python
if stamp < last:
    raise VersionError('OUT_OF_ORDER_FETCH')
if stamp == last and digest != latest['content_sha256']:
    raise VersionError('AMBIGUOUS_FETCH_ORDER')
```

按采集时间排序处理，迟到事件被拒绝，避免版本链错乱。

### 事务原子性

```python
conn.execute('BEGIN IMMEDIATE')    # 先拿写锁
# ... 所有读写在这个事务内
# 退出 with 块时 commit 或 rollback
```

测试验证：通过 SQLite 触发器强制失败，确认四张表计数全归零——**无半成品数据**。

## ⑦ PolicyQueryService

### 只读连接

```python
sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
conn.execute('PRAGMA query_only=ON')
```

双保险：文件系统只读 + SQLite 层拒绝写。

### 为什么 window 函数选最新版本

```sql
WHERE v.version = (
    SELECT MAX(y.version) FROM policy_versions y
    WHERE y.policy_id = i.policy_id
)
```

一个 policy 可能有多个版本，默认只查最新观测版本。

### 字面匹配而非 LIKE

```sql
AND (instr(d.title, ?) > 0 OR instr(d.body_text, ?) > 0)
```

`instr()` 是字面子串匹配。如果用 `LIKE`，`%` 和 `_` 会变成通配符，导致注入式误匹配。

### 长文档分段

每次最多返回 8000 字符：

```sql
SELECT substr(body_text, ?, ?)   -- offset+1, 8000
```

返回 `next_offset`，客户端用同一个 `policy_id + version + next_offset` 继续读。

**注意**：8000 是字符数不是 Token 数。

## ⑧ PDF 摄取

### 逐页提取

```python
for number, page in enumerate(reader.pages, 1):
    text = page.extract_text()
    if not text.strip():
        raise PdfIngestError(f'PAGE_WITHOUT_TEXT:{number}')
    pages.append(PdfPage(number, text, cursor, cursor + len(text)))
    cursor += len(text) + len(PAGE_BREAK)
```

正文用 `\n\f\n` 分隔页。

### 审核指纹

```python
fingerprint = _json_hash({
    'fetch_id': artifact.fetch_id,
    'raw_sha256': artifact.raw_sha256,
    'content_sha256': digest,
    'metadata': m.model_dump(mode='json'),
    'parser': version,
    'page_ranges': [(p.number, p.start, p.end) for p in pages],
    'kind': artifact.acquisition_kind,
})
```

提交时必须提供这个指纹：

```powershell
python -m scripts.ingest_pdf commit <raw.json> --approve-sha256 <review_sha256>
```

**目的**：强制人工逐页核对后才允许入库。任何元数据或解析结果变化都会让指纹失效。

## ⑨ Chunking

见 [文档切片](chunking.md)。

## 数据完整性检查点

| 检查点 | 验证内容 |
|---|---|
| Base64 解码 | `validate=True`，非法字符拒绝 |
| 字节长度 | `len(body) == body_size` |
| PDF 魔数 | `body.startswith(b'%PDF-')` |
| 原始哈希 | `_sha(body) == raw_sha256` |
| 内容哈希 | `content_hash(doc) == row.content_sha256` |
| 审核指纹 | `approved_sha256 == preview.review_sha256` |

每一层都独立校验，不假设上游可信。

## 数据血缘

每个切片可以追溯到：

```text
chunk_id
  ├── document_id ──► body_text 的精确字符区间
  ├── policy_id
  ├── version
  ├── content_sha256
  ├── reference_fetch_id ──► 采集事件
  ├── raw_sha256 ──► 原始字节
  └── source_url ──► 官方网站
```

完整链条，无断点。

## 下一步

- [文档切片](chunking.md) — 切片如何保证可追溯
- [测试与验收](testing.md) — 数据中心的验收方式
- [技术边界](limitations.md) — 已知限制
