# MVP-01-F RAG Studio API 与验收

2026-11-27 · 开发分支 `feat/mvp01-rag-studio` · 起点 `73cdd8b5e2cadd3f8662112704b22526e3eb8c38`

## API 契约

前缀为 `/api/workbench/rag`。成功响应使用 `{"data":{...}}`。请求参数不合法时使用 HTTP 422 和 `{"error":{"code":"INVALID_INPUT","message":"..."}}`。检索完成后的状态放在 `data.status` 中，即使索引不可用也返回结构化诊断。

| 方法 | 路径 | 主要响应 |
| --- | --- | --- |
| GET | `/status` | `documents`、`chunks`、`manifest_fingerprint`、`freshness`、`consistency_status`、各路 `status/schema/fingerprint/manifest_fingerprint`，向量索引另有 `model/model_commit/dimension`。就绪表示索引文件已通过结构校验。实际编码器身份在 Dense 查询时验证 |
| POST | `/search` | `status`、`code`、`message`、`elapsed_ms`、`paths`、`consistency_status`、`freshness`、`degraded_status`、`candidates` 和候选声明 |
| GET | `/benchmark` | 只读 `compare-hybrid-final.json`。状态为 `ready/missing/invalid`。返回开发评测集文件名、版本、语料规模、实际记录的时间和各路聚合指标 |

请求示例

```json
{"query":"参与电力市场需要先办理哪些手续？","mode":"hybrid","top_k":5}
```

`query` 去除首尾空白后为 1 至 200 字符。模式为 `lexical`、`dense` 或 `hybrid`，默认 `hybrid`。整数 `top_k` 为 1 至 20，默认 5。客户端只能提交这三个字段。索引目录只读自服务端配置 `POWERCOPY_RAG_DIR`，相对路径基于项目根目录，默认 `storage/rag`。

- `success` 表示返回候选。 `empty` 表示检索成功且无候选。 `degraded` 表示实际只走了部分检索路径，仍可能正常无命中。 `failed` 表示索引或执行失败。缺指纹或跨版本冲突会阻止 Hybrid 返回融合候选。
- `paths.lexical` 和 `paths.dense` 分别返回 `status/match_mode/returned`。未请求、未执行、缺索引、索引无效、执行失败、有候选、无候选都有独立状态。耗时是服务端完成状态校验和检索的实测时间，冷加载模型会计入。
- 候选返回排名、切片 ID、政策 ID、文档 ID、政策版本、标题、来源、正文哈希、`start/end/page`、完整切片 `text`、分数和路径贡献。字符区间采用 **[start, end)**，`page` 为 PDF 物理页，从 1 开始。没有页码时返回 null。
- `contributions` 保留各路排名、原始分数、匹配方式和 `rrf`。单路模式的 RRF 为 null。Hybrid 中未命中的路径排名和原始分数为 null，贡献为 0。分数用于展示排序。引用前仍需核对原文、适用范围和有效性。
- `freshness` 对照政策库最近观测版本和正文哈希。状态为 `stale` 或无法核对时，结果区保留警示。更新索引应在任务间进行。
- Benchmark 从文件读取 Recall、MRR、nDCG、Hit Rate、CER 和分类指标。报告缺项显示为空。当前报告只有 Hybrid 记录了 Top 5，缺少运行时间，页面单独标记文件修改时间。报告描述已有开发评测，不承担业务验收。

## PowerShell

从指定项目根目录运行。已有索引使用过旧的分词器指纹或索引文件缺失时，使用现有本地模型重建。

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:POWERCOPY_WORKBENCH_ALLOW_LIVE = '0'
$python = '.\.venv\Scripts\python.exe'
# 已有本地 CPU Embedding 环境的工程可直接执行
& $python .\scripts\build_dense_index.py --rebuild
# 仅在缺少词法索引时需要
& $python .\scripts\eval_retrieval.py --rebuild
& .\scripts\start-workbench-api.ps1
```

另开一个 PowerShell 运行前端。使用 `pnpm preview` 可预览生产构建。

```powershell
Set-Location frontend
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
pnpm preview
```

打开 `http://127.0.0.1:5173/#/rag`。新环境先安装 [项目依赖](../../requirements.txt) 和 [本地 Embedding 依赖](../../requirements-embedding.txt)。已有评测报告缺失时，可以在同一语料和开发评测集上运行 `python scripts/compare_hybrid.py --json storage/rag/compare-hybrid-final.json` 生成报告。

## 源码交叉审查

| 范围 | 核查及本轮处理 |
| --- | --- |
| 索引指纹 | 原 Hybrid 已拒绝双路冲突及缺指纹融合。本轮同步检查向量与当前切片清单的指纹，Agent 工具先校验可用的索引身份，避免把冲突当成可降级 |
| 模型及缓存 | 旧实现先分别加载模型、分词器再独立解析提交。现在两者从同一个已解析快照加载，分词器指纹覆盖词表及分词规则。Dense 拒绝不受支持的 schema、缺失的索引指纹和已声明但不一致的编码配置。旧缓存按身份不匹配失效 |
| 反序列化与离线 | Dense NPZ 加载保持 `allow_pickle=False`，模型权重启用 Safetensors。解析快照默认只读本地缓存，模型与分词器都从该目录加载 |
| 状态与脱敏 | 区分真实空、检索故障、降级与过期，校验版本相同但正文哈希变更的情况。对外返回固定错误信息 |
| Agent Runtime | 原 `use_retrieval` 继续控制本地运行是否选用 `search_policy_evidence`。实际 HTTP 验证了工具业务成功与真实候选，工作台路径配置现统一以项目根目录解析 |

保留现有 FTS5 BM25、CPU BGE 和 RRF，适配层只读取状态、调用检索器、映射输出。分数与召回效果以本项目冻结开发集为准。模型身份处理参照 [Hugging Face 快照接口](https://huggingface.co/docs/huggingface_hub/package_reference/file_download) 和 [本地缓存布局](https://huggingface.co/docs/huggingface_hub/v1.29.0/en/guides/manage-cache)。[SQLite FTS5 与 BM25](https://www.sqlite.org/fts5.html)

## 实际验收

- 原始 HEAD 全量 1189 项通过。最终离线回归 **1211 项通过**，58.69 秒。新增 10 项检索链路断言和 12 项工作台接口测试。检索专项组合 213 项通过，接口专项 12 项通过。
- Windows 本机 Node 24.22.3、pnpm 11.25.0。锁定依赖从缓存安装，TypeScript `tsc --noEmit` 与 Vite 6.4.6 生产构建均成功。
- FastAPI 实际启动两个实例，分别读取真实本地索引和空的索引目录。Edge 用 Vite 生产预览实际提交三种检索模式，覆盖候选详情、政策版本跳转、正常空结果、索引异常、评测缺失、非法 Top-K、Agent 链路和兼容入口。9 个浏览器检查点通过。
- 「电力市场运行基本规则」词法检索返回 2 条。相同问题经 Hybrid 返回排名、正文和双路贡献。「参与电力市场需要先办理哪些手续？」词法无候选，Dense 候选命中办理市场注册原文。「安徽电力现货市场交易规则」词法为空，Hybrid 仍可返回待核实候选。
- 本地离线重建向量索引，模型 `BAAI/bge-small-zh-v1.5`，提交 `7999e1d3359715c523056ef9478215996d62a620`，512 维，对应 2 份政策的 26 个切片。真实 Agent 调试的外部 LLM 调用数为 0。政策库、词法索引和切片清单的 SHA-256 校验值前后一致。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retrieval_integration.py tests/test_dense_retrieval.py tests/test_embedding.py tests/test_hybrid_retrieval.py tests/test_workbench_retrieval_api.py -q
.\.venv\Scripts\python.exe -m pytest tests -q
```

浏览器脚本默认用 `5174` 和 `5175`。正常后端在 8121 启动。第二个 PowerShell 先将 `POWERCOPY_RAG_DIR` 设置为一个空目录，再在 8122 启动异常场景后端。前端两个生产预览分别设置 `POWERCOPY_API_URL` 为两端地址。安装了 Playwright 模块和浏览器后，从项目根目录运行 `node scripts/check_rag_studio.cjs`。也可通过 `POWERCOPY_PLAYWRIGHT` 和 `POWERCOPY_BROWSER` 指向本机已有模块和浏览器。

[RAG Studio](../screenshots/rag-studio.png) · [开发评测](../screenshots/rag-studio-benchmark.png) · [正常空结果](../screenshots/rag-studio-empty.png) · [索引不可用](../screenshots/rag-studio-index-unavailable.png)

## 已知限制

- Dense/Hybrid 可向证据不足的问题返回候选。证据充分性、适用性和政策效力仍需核实。当前评测以 2 份政策、26 个切片为主，PDF 页码映射由隔离测试覆盖。
- Agent 在单次任务缓存已加载的检索器。替换整套索引时应在任务间操作，避免单一任务内读到不同时间的产物。Agent 集成验收使用工具调用内的候选；Agent Playground 顶层证据摘要仍只聚合政策搜索与详情。
- 模型、大型索引和本地评测文件按仓库约定保存在本地。部署需要准备对应版本的本地缓存和索引。
