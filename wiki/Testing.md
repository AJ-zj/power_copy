# 测试与验收

## 总览

```text
795 passed
```

| 测试组 | 文件 | 覆盖内容 |
|---|---|---|
| core | 9 个 | LLM 客户端、重试、预算、Trace、Agent Loop、消息 |
| tools | 5 个 | 工具定义、注册表、执行器、Function Calling |
| data 4.1 | 1 个 | 数据契约、SQLite 读写、参数化 SQL |
| data 4.2 | 1 个 | 数据源、域名白名单、错误分类、禁重定向 |
| data 4.3 | 2 个 | 编码、CSS Selector、正文清洗、真实 HTML |
| data 4.4 | 2 个 | 三元组、事务回滚、并发 |
| data 4.5 | 2 个 | Pipeline、重试退避、预算、暂停策略 |
| data 4.6 | 2 个 | 真实库查询、只读、注入防护、Agent 全链路 |
| data 4.8 | 2 个 | PDF 逐页、页码映射、审核指纹 |
| rag 5.1 | 2 个 | 切片精确性、覆盖、确定性、身份更新 |

## 运行测试

```powershell
# 全量
python -m pytest tests -q

# 某个阶段
python -m pytest tests/test_policy_versioning.py -q

# 某组
python -m pytest tests/test_chunking.py tests/test_chunking_extra.py -q

# 显示失败详情
python -m pytest tests -q --tb=short
```

## 测试策略

### 测试方式跟随被测对象

| 被测对象 | 方式 | 理由 |
|---|---|---|
| 外部 HTTP API | Mock | 不能真的请求政府网站 |
| SQLite | 真实临时库 | 内存库会掩盖文件级问题 |
| LLM | Mock Chat Completions | 不烧钱、不确定 |
| 真实 HTML/PDF | 离线重放已有文件 | 不重访网站 |

### 真实数据测试自动跳过

```python
pytestmark = pytest.mark.skipif(
    not all(p.is_file() for p in FILES.values()),
    reason="缺少真实采集文件，跳过",
)
```

首次 clone 后 `storage/` 是空的，这些测试会 skip。要跑完整测试需先执行一次采集。

### 真实数据 vs 构造数据

部分测试两者都有：

```text
test_chunking.py        # 21 项，构造数据（开发包）
test_chunking_extra.py  # 44 项，真实政策库 + 边界
```

构造数据保证逻辑正确，真实数据保证接口兼容。

## 验收脚本

每个阶段都有离线验收脚本，输出人类可读的逐项检查：

```powershell
python scripts/check_policy_repository.py   # 4.1  8 项
python scripts/check_fetcher.py --live      # 4.2  10 项（含真实采集）
python scripts/check_parser.py --real       # 4.3  10 项
python scripts/check_versioning.py --real   # 4.4  10 项
python scripts/check_pipeline.py --real     # 4.5  10 项
python scripts/check_policy_tools.py        # 4.6  11 项
python scripts/check_pdf_ingest.py          # 4.8  11 项
python scripts/check_chunking.py            # 5.1  10 项
```

### 输出格式

```text
======================================================================
PowerCopy V2 · 5.1 Document Chunking 文档切片 验收
全程离线：不联网、不调用 LLM、不写数据库
======================================================================

[1/10] 两份真实 HTML 政策完成只读切片

  manifest_sha256 : ea194999ae37ba8647420028543cb1657099f57de2602313f1af80886d691cd2
  documents       : 2
  chunks          : 26

    nea / v1
      正文字数     : 6010
      切片数       : 16
      最小切片     : 284
      最大切片     : 496

  [x] 构建出 2 份文档
  [x] 产出切片非空
  [x] 数据字节未变
```

### 退出码

- `0` — 全部通过
- `1` — 有项目未通过

可用于 CI。

## 关键验收点

### 4.1 数据模型

| 验收项 | 验证方式 |
|---|---|
| 数据契约 | 必填可读、UUID 生成、禁止额外字段、frozen |
| 非法数据拒绝 | 空白标题/正文/source_key 共 7 种 |
| URL 与时间 | 拒绝 `file://`、`ftp://`、无时区 datetime |
| 写入读取 | `add` → `get` 对象完全相等 |
| 持久化 | 重建 Repository 数据仍在 |
| 重复主键 | 同 ID 改内容被拒，原数据未覆盖 |
| 参数化 SQL | 注入标题后表完好 |

### 4.4 版本管理

| 验收项 | 验证方式 |
|---|---|
| 身份映射 | 同 URL 同 policy_id，不同 URL 不同 |
| 首次 V1 | created + version=1 |
| 相同内容去重 | unchanged，复用 document_id |
| fetch_id 幂等 | 重放 replayed，改内容 FETCH_ID_CONFLICT |
| 内容变化 | updated + version=2 |
| A→B→A | 版本 1/2/3，A 与 A 哈希相同，三份独立文档 |
| 哈希与血缘 | raw_sha256 / input_sha256 均 64 位 |
| 事务回滚 | 触发器强制失败后计数全零 |
| 迟到事件 | OUT_OF_ORDER_FETCH |

### 4.6 政策工具

| 验收项 | 验证方式 |
|---|---|
| 真实搜索 | 命中 2 份真实政策 |
| 版本选择 | 默认最新，支持显式历史版本 |
| 地区筛选 | 未标注标为 unassigned |
| 数据契约 | search 6 字段，detail 22 字段 |
| 来源哈希 | 与 DB 逐字段比对一致 |
| 只读防护 | DELETE 被拒，注入无命中，记录不变 |
| 分段读取 | 8000 字符上限，next_offset 可续 |
| Registry | 2 个工具，重复注册被拒 |
| Agent 全链路 | 3 轮 Mock，tool_call_id 匹配 |
| 异常映射 | 结构化错误，不含 SQL/堆栈 |

### 5.1 切片

| 验收项 | 验证方式 |
|---|---|
| 真实切片 | 2 份政策，26 切片 |
| 参数约束 | 8 组非法全部拒绝 |
| 自然边界 | 换行/句末优先，硬切不超限 |
| 精确还原 | `text == body[start:end]` 逐块验证 |
| 无遗漏 | 非空白字符全覆盖（5845 + 3703） |
| 确定性 | 重复构建 manifest 一致 |
| 配置敏感 | 4 种配置 chunk_id 不重叠 |
| 版本更新 | V1→V2 全部 ID 更新，历史不混入 |
| PDF 页码 | 页区间内，不跨页，缺档拒绝 |

## 一个真实 bug 的发现过程

测试 `test_live_replay` 在某些环境失败：

```text
AssertionError: {'run_id': '...', 'items': [...]} != {'run_id': '...', 'items': [...]}
```

排查发现：

1. `atomic_json()` 写文件用 `encoding='utf-8'`
2. 测试读取用 `Path.read_text()` **未指定编码**
3. Windows 默认 `cp936`（GBK）
4. 报告里含中文路径 `C:\Users\张建\AppData\...`
5. GBK 读回 UTF-8 内容 → 乱码 → 对象不等

**不是生产代码的 bug，是测试的环境假设问题。**

修复：

```python
# 之前
assert json.loads((p.run_dir/(r['run_id']+'.json')).read_text())==r

# 之后
assert json.loads((p.run_dir/(r['run_id']+'.json')).read_text(encoding='utf-8'))==r
```

这个案例说明：**跨平台测试必须显式指定编码**，不能依赖系统默认。

## 扫描所有缺 encoding 的调用

```powershell
Get-ChildItem -Recurse -Filter '*.py' | ForEach-Object {
  Select-String -Path $_.FullName -Pattern '\.(read_text|write_text)\(' |
    Where-Object { $_.Line -notmatch 'encoding' }
}
```

当前项目已无此问题。

## CI 建议

```yaml
# .github/workflows/test.yml
name: test
on: [push, pull_request]
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -e ".[dev]" -e ".[api]" -r requirements.txt
      - run: python -m pytest tests -q
```

> 注意：真实数据测试在 CI 里会 skip（没有 `storage/`），这是预期行为。

## 下一步

- [技术边界](limitations.md) — 哪些功能未验证
