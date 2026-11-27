# 政策采集现状与扩充记录

**日期**：2026-11-27
**分支**：`feat/mvp05-cost`
**结论**：政策库从 **2 份 / 26 切片 / 约 1 万字** 扩充到
**7 份 / 293 切片 / 约 9.6 万字**。

---

## 一、原先的问题（实测确认）

| 问题 | 实测结果 |
|---|---|
| 发改委列表页 | `https://zfxxgk.ndrc.gov.cn/web/` → **HTTP 404**（已失效） |
| 政策库规模 | 只有 2 份国家级政策，26 个切片 |
| 华东监管局 15 个候选 | **从未被抓取**，列表页仅做「发现」，不入库 |
| 定时采集 | 无调度器（已在本轮补上） |

**不是「抓不到」，而是「只抓过 2 份」。**

---

## 二、实测确认可用的链路

| 环节 | 结果 |
|---|---|
| 三个站点网络 | ✅ 全部连通（510–720ms） |
| `ndrc/market_rules` | ✅ 200，解析 3829 字 |
| `nea/registration_rules` | ✅ 200，解析 6010 字 |
| `nea_east/policy_index` | ✅ 200，发现 15 个候选 |
| 抓取 → 解析 → 入库 | ✅ 全链路可用 |

### 关键发现：15 个候选全是 PDF

```text
PDF : 15
HTML: 0
```

而且**不是扫描件** —— pypdf 能提取完整文本层。

---

## 三、PDF 空白页策略（本轮修改）

### 原规则的问题

`preview_pdf` 原本要求「每一页都必须有文字」，任一无文字页即拒绝整篇：

```text
[拒绝] PAGE_WITHOUT_TEXT:1   ← 15 个里 8 个卡在这
```

原因：这些文件第 1 页是**红头文件的图片封面**，正文页都有文本层。

原规则的设计意图是对的 —— 防止「解析失败被当成空白页而静默丢内容」。
所以不能简单放开。

### 本轮的做法

新增 `powercopy/data/pdf_blank_policy.py`：

- **允许空白页，但把空白页号写进审批指纹**
- 操作员确认 `approved_sha256` 时，确认的包括「第 N 页确实空白」
- 空白页数有上限（≤20 页 且 ≤30%）
- **整篇无文字仍拒绝**（那是真扫描件，需要 OCR）

### 默认行为不变

`preview_pdf(artifact)` 默认仍是严格模式，
既有调用方与既有审批完全不受影响。
只有显式传 `allow_blank_pages=True` 才放宽。

### 效果

```text
严格模式通过 : 3
放宽后通过   : 5      ← 多放行 2 篇
仍拒绝       : 6      ← 5 篇整篇无文字（真扫描件）+ 1 篇解析失败
超 10MiB     : 4
```

**如实说明**：放宽空白页只多解决 2 篇。剩下 5 篇是**真正的扫描件**，
需要 OCR 才能处理，项目当前没有 OCR 依赖。

---

## 四、新增工具

### `scripts/prepare_pdf_candidates.py`

**这是「准备」工具，不是「自动入库」工具。**

```powershell
# 准备（下载 + 预览，不入库）
python scripts/prepare_pdf_candidates.py --source nea_east --target policy_index

# 入库（需先审批）
python scripts/prepare_pdf_candidates.py --ingest storage/pdf_staging/manifest.json
```

**为什么不做全自动入库：**

PDF 摄取要求 `approved_sha256 == preview.review_sha256`。
这道闸门的目的正是让人**在内容进库之前**看一眼提取对不对。
自动填 SHA 等于把闸门拆掉 —— 提取错误（导航混入正文、目录当成正文）
会静默污染政策库，而下游文案会引用这些错误内容。

---

## 五、本轮实际入库的 5 篇

| # | 标题 | 页数 | 字数 | 空白页 |
|---|---|---|---|---|
| 1 | 长三角电力辅助服务管理实施细则（结算试运行稿） | 92 | 46,261 | 无 |
| 2 | 关于修订印发《安徽电力中长期交易规则》的通知 | 44 | 21,962 | 第 1、2 页 |
| 3 | 华东能源监管局关于扩展华东电力调峰辅助服务市场交易至日内实施的通知 | 17 | 8,012 | 第 1、2 页 |
| 4 | 长三角省市间富余新能源消纳互济交易规则（试行） | 15 | 7,360 | 无 |
| 5 | 关于监管辖区煤电容量电费考核有关事项的通知 | 5 | 2,226 | 无 |

### 入库后统计

```text
policy_documents : 7 份
policy_versions  : 7
policy_fetches   : 7
切片             : 293（原 26）
总字数           : 95,660（原约 10,000）
```

### 提取质量抽查

三篇的开头/结尾人工看过，正文完整、条款清晰、页码保留但未混入实质内容。

---

## 六、索引重建

```powershell
# 切片（必须指定 raw_pdf_dir，否则 PDF 政策无法切片）
python scripts/build_chunks.py --db storage/policies_versioned.db \
    --raw-pdf-dir storage/raw_pdf --out storage/rag/chunks.json

# 词法索引
python -c "from powercopy.rag.lexical import build_index; \
    build_index('storage/rag/chunks.json','storage/rag/lexical.db',overwrite=True)"

# 向量索引
python scripts/build_dense_index.py --rebuild
```

结果：

```text
chunks    : 293
documents : 7
lexical   : 293 条
dense     : 293 条，512 维
```

---

## 七、检索质量验证

```text
「安徽电力中长期交易」 -> 2 条  ✅ 命中目标政策
「调峰辅助服务市场」   -> 3 条  ✅ 命中目标政策
「长三角辅助服务」     -> 0 条  ← trigram 短语需连续匹配
「煤电容量电费」       -> 0 条  ← 同上
```

后两个 0 命中**不是索引缺失**：
- 原文是「长三角**电力**辅助服务」，查询「长三角辅助服务」不是连续子串
- 原文是「容量电费」，查询「煤电容量电费」中间隔着字

这是 FTS5 trigram 的固有行为（既有设计，非本轮引入）。

---

## 八、因语料扩充而修改的测试

**原则：不为了让测试通过而放宽断言，而是把「硬编码绝对值」改成「断言真正的意图」。**

| 测试 | 原断言 | 改为 | 理由 |
|---|---|---|---|
| `test_two_documents` | 恰好 2 份 | ≥2 且包含 ndrc/nea | 语料会持续增长 |
| `test_html_documents_have_no_page_number` | 全部 page=None | PDF 有页码、HTML 无 | 现在两种来源并存 |
| `test_index_has_all_chunks` | ==26 | 与清单一致 | 同上 |
| `test_region_not_in_corpus` | 查「安徽」0 条 | 查「西藏」0 条 | 安徽政策已入库 |
| `test_no_false_confidence_on_absent_topic` | 用「安徽」演示 | 用「新疆」 | 同上 |
| `test_no_real_pdf_in_project` | 必须是合成样本 | 来源说明必须可追溯 | 已有真实 PDF |
| `test_backup_opens_without_migration` | 精确计数 ==2 | ≥2 且 policies==documents | 同上 |
| `test_search_returns_real_policies` | ==2 | ≥2 且包含原始两份 | 同上 |
| `test_search_title_and_body_both_match` | 恰好只有 NDRC | 包含 NDRC | 其他政策也可能含该词 |
| `test_sql_injection_is_literal` | 表内 ==2 | ≥2 | 同上 |

**测试结果**：1530 passed, 1 skipped。

---

## 九、新增 PDF 空白页策略测试

`tests/test_pdf_blank_policy.py`（14 项 + 1 skip）：

- 严格模式行为完全不变
- 放宽模式允许空白页
- 整篇无文字仍拒绝
- 空白过多仍拒绝
- **指纹绑定空白页**（空白页变化使旧审批失效）

---

## 十、发改委列表页 404

**仍未修复。** 原因是需要重新校准选择器：

```text
失效：https://zfxxgk.ndrc.gov.cn/web/      → 404
可用：https://zfxxgk.ndrc.gov.cn/           → 200（17126 字节）
可用：https://www.ndrc.gov.cn/xxgk/zcfb/ghxwj/ → 200（44565 字节）
```

改 URL 简单，但 `config/index_selectors.json` 里需要为该页面配选择器，
且必须实测能提取出正确候选链接。**本轮未做**，因为优先级低于把已有
15 个候选消化掉。

---

## 十一、安全

| 项 | 处理 |
|---|---|
| `storage/pdf_staging/` | 已 gitignore |
| `config/pdf_approval.json` | 已 gitignore（含 reviewer 与审批记录） |
| `storage/raw_pdf/` | 原本已 gitignore |
| `config/pdf_approval.example.json` | 可提交（只有格式说明） |
| 敏感扫描 | 未发现密钥/姓名/手机号/绝对路径 |

---

## 十二、下一步建议

1. **修发改委 404**：重新校准选择器
2. **OCR 那 5 篇扫描件**：需引入 tesseract 或 paddleocr（本轮未做）
3. **扩充更多源**：目前只有 3 个源，可加广东、山西（业务四省中还缺）
4. **定时采集已就绪**：跑 `run_ingestion_schedule.py` 即可
