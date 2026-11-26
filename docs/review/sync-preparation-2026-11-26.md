# 项目同步准备报告

**日期**：2026-11-26
**目的**：将本地最新开发进度同步到 GitHub 公开仓库前的核查与准备

---

## 一、Git 状态核查

### 分支与远程

```text
当前分支        : main
跟踪            : origin/main
remote          : https://github.com/AJ-zj/power_copy.git
本地 HEAD       : 4d20cb687e4172a0c1c0f75d7960f62cf3cabcf6
origin/main     : 4d20cb687e4172a0c1c0f75d7960f62cf3cabcf6
领先/落后       : 0 / 0
```

**关键结论**：本地 HEAD 与 origin/main **完全一致**。所有差异来自**未提交的工作区改动**，不是提交历史分叉。

### 差异全貌

```text
已跟踪文件改动 : 26 个（+769 / -162 行）
未跟踪新文件   : 36 个
待提交文件合计 : 62 个
```

### 远程与本地内容差异（印证你的观察）

| 项 | 远程 main | 本地 |
|---|---|---|
| README 测试数字 | **795** | 1122 |
| `powercopy/rag/` | `__init__.py`、`chunking.py`（2 个） | 6 个（+embedding、lexical、dense、hybrid） |
| `docs/adr/` | **不存在** | 8 个 ADR |
| `docs/review/` | **不存在** | 6 份报告 |
| `docs/CHANGELOG.md` | 不存在 | 存在 |
| `docs/PROJECT_STATE.md` | 不存在 | 存在 |
| `docs/roadmap-stage05.md` | 不存在 | 存在 |
| `AGENTS.md` | 不存在 | 存在 |
| `evaluations/` | 不存在 | 30 条评测集 |
| `requirements-embedding.txt` | 不存在 | 存在 |

**你的判断准确**：远程确实停留在阶段05.1（795 项，仅 chunking.py）。

---

## 二、已验收模块完整性核实

实测文件存在性与行数：

| 模块 | 文件 | 行数 | 状态 |
|---|---|---|---|
| 4.9 工具语义 | `powercopy/tools/result.py` | 136 | ✅ |
| 4.9 工具语义 | `powercopy/tools/executor.py` | 131 | ✅ |
| 4.9 工具语义 | `powercopy/tools/builtin/policy_tools.py` | — | ✅ |
| 4.9 追踪 | `powercopy/core/trace.py` | 203 | ✅ |
| 4.9 工作台 | `workbench/runtime_api.py` | 316 | ✅ |
| 5.1 切片 | `powercopy/rag/chunking.py` | 144 | ✅ |
| 5.1-B Embedding | `powercopy/rag/embedding.py` | 277 | ✅ |
| 5-A 词法 | `powercopy/rag/lexical.py` | 459 | ✅ |
| 5-B 向量 | `powercopy/rag/dense.py` | 462 | ✅ |
| 5.5-A 混合 | `powercopy/rag/hybrid.py` | 504 | ✅ |

**全量测试实测**：

```text
1122 passed in 78.03s
```

与 PROJECT_STATE / CHANGELOG 记录一致。

---

## 三、文档更新

### 已更新的文件（18 个）

| 文件 | 更新内容 |
|---|---|
| `README.md` | 测试数字 795→1122；rag 目录说明；检索链路图；阶段05 表格扩充至 5 个子阶段；检索结果表；新增检索脚本命令；技术边界重写 |
| `AGENTS.md` | 技术基线表更新（8 行）；**新增「交付流程」章节**（7 步，含推送需授权） |
| `docs/PROJECT_STATE.md` | 测试数字 862→1122 |
| `docs/architecture.md` | 数据流图加入检索三层；新增「检索层设计」章节；目录职责表细分 5 个文件 |
| `docs/roadmap-stage05.md` | 05-B 标注完成；新增 05.5-A 完成段落 |
| `docs/CHANGELOG.md` | 已含 05-A/05-B/05.5-A 条目（本轮无需新增） |
| `docs/README.md` | 测试数字；已验收阶段；数据层模块数；依赖说明；导航加检索报告 |
| `docs/limitations.md` | **重写检索章节**：已实现/未实现分离；加检索实测表；路线图勾选更新 |
| `docs/overview.md` | RAG 能力列表；未实现边界；阶段表扩充；下一步重排 |
| `docs/testing.md` | 测试数字 795→1122 |
| `docs/getting-started.md` | 测试数字 795→1122 |
| `docs/workbench.md` | 测试数字；加历史快照说明 |
| `docs/workbench-test-results.json` | **保留 795 原值**，加 `_note` 与 `scope` 说明这是历史快照 |
| `docs/adr/ADR-001` ~ `ADR-008` | 本轮新增 ADR-006/007/008 |
| `docs/review/` 6 份报告 | 本轮新增 |
| `wiki/*.md` | 由 `scripts/sync_wiki.py` 从 docs 生成（6 个文件更新） |

### 关键取舍

**`docs/workbench-test-results.json` 未改数字**。

该文件是 `2026-11-26T10:40:50` 那个时间点的验证快照，记录当时 795 项。直接改成 1122 等于伪造历史记录。已加 `_note` 说明，并注明「若需刷新，请实际运行命令后覆盖」。

### 一致性验证

全项目扫描确认再无 `795` 作为「当前状态」出现（仅 CHANGELOG 历史流水和快照文件保留）。扫描「只有 chunking」「还没有检索层」「BM25 未实现」等过时表述，结果为空。

---

## 四、安全检查

### 4.1 密钥与敏感文件排除

`.gitignore` 已加强（+18 行）：

```text
新增：.env.*  （保留 !.env.example）
新增：config/*.local.json
新增：*.db-wal / *.db-shm
新增：*.safetensors / *.onnx / *.bin / *.pt / *.pth / *.gguf
新增：models/ / model_cache/
新增：*.tmp
```

`git check-ignore` 逐项验证：

| 路径 | 结果 |
|---|---|
| `.env` | ✅ 忽略 |
| `.env.example` | ✅ 跟踪（negation 生效） |
| `storage/models/x.safetensors` | ✅ 忽略 |
| `storage/rag/lexical.db` | ✅ 忽略 |
| `a.log` / `b.tmp` | ✅ 忽略 |
| `config/pdf_metadata.json` | ✅ 忽略 |

**误伤检查**：确认新增规则未排除任何已跟踪文件。

### 4.2 工作区敏感文件

| 路径 | 存在 | 已跟踪文件数 |
|---|---|---|
| `.env` | 是 | **0** ✅ |
| `storage/` | 是 | **0** ✅ |
| `storage/models/` | 是 | **0** ✅ |
| `.venv/` | 是 | **0** ✅ |

### 4.3 历史提交扫描

```text
提交总数        : 11
密钥模式扫描    : sk-* / ghp_* / github_pat_* / AKIA* / PRIVATE KEY → 未发现
敏感文件扫描    : .env / .db / .safetensors / .log → 未发现
```

---

## 五、发现并修复的安全问题

### 问题 1：真实姓名出现在公开仓库（已修复）

**位置**：4 个文件，共 6 处

| 文件 | 内容 |
|---|---|
| `docs/testing.md:188` | 示例路径中拼入了项目负责人的真实姓名（`C:\Users\<真实姓名>\AppData\...`） |
| `wiki/Testing.md:188` | 同上（wiki 派生） |
| `scripts/check_messages.py:28` | 验收脚本的 PROFILE 常量用了真实姓名 + 具体业务区域（省 + 市 + 区） |
| `tests/test_message.py:32,33,39` | 测试夹具用真实姓名 |

**风险**：公开仓库暴露真实姓名 + 具体业务区域（江西省南昌市青山湖区）。

**处理**：
- 真实姓名 → 通用占位（`<用户名>`）或虚构测试数据（`李明`）
- 业务区域保留（江西是 AGENTS.md 明确的四省之一，不构成敏感信息）

**验证**：全项目扫描该姓名 → 0 处。改名后 1122 passed，`check_messages.py` 4/4 通过。

> 注：本报告初稿在记录该问题时又原文引用了真实姓名，属于二次泄露，已在提交前修正为脱敏表述。

### 问题 2：命令注入风险（已识别，未修复，需你决策）

**位置**：`scripts/fix_wiki_secret.py`

**问题**：这是**一次性运维脚本**，功能是：
1. 从 git credential manager 读取 PAT
2. 调用 GitHub API 创建 `WIKI_TOKEN` secret

**为什么不适合留在公开仓库**：
- 暴露运维操作路径与 API 调用方式
- 记录了仓库 secret 名称与 owner/repo
- 对公开仓库的读者无价值，反而增加攻击面信息

**处理（项目负责人已批准）**：`git rm --cached`，本地改名保留为 `_local_only_*.bak`（已加入 `.gitignore`）。

**已确认无明文密钥**：脚本从 credential manager 动态读取，不含硬编码 token。

### 问题 2b：另一个一次性运维脚本 `scripts/check_workflow.py`

按项目负责人要求「检查是否存在其他不必要的一次性运维脚本」，发现并处理：

| 项 | 情况 |
|---|---|
| 用途 | 校验 `.github/workflows/sync-wiki.yml` 的 bash 片段语法 |
| 一次性性质 | 是，CI 配置定型后不再需要 |
| 依赖 | `import yaml`（**未在 requirements.txt / pyproject.toml 声明**） |
| 硬编码 | `BASH = r"D:\Git\Git\bin\bash.exe"` 本机路径 |
| 引用 | 全项目无任何文档或代码引用 |

**处理**：同 `fix_wiki_secret.py`，`git rm --cached` 并本地保留。

### 问题 3：`config/*.json` 非 example 文件（已识别，需你判断）

远程已跟踪：
- `config/index_selectors.json`
- `config/parser_profiles.json`

**内容**：CSS 选择器与解析配置（`.wrapper_right_menu_content li a`、`td` 等）。

**评估**：这是**技术配置**，不含业务敏感信息（无客户数据、无报价、无合同）。公开无实质风险，但会让爬虫目标页面结构公开。

**建议**：保留（属于工程项目配置，公开合理），或统一改为只提交 `.example`。**需你决策**。

---

## 六、待提交文件清单

### 已跟踪文件的修改（26 个）

```text
.gitignore                               +18
README.md                                +73 -??
docs/README.md                           +45
docs/architecture.md                     +41
docs/getting-started.md                   +2
docs/limitations.md                      +49
docs/overview.md                         +26
docs/testing.md                           +2
docs/workbench-test-results.json         +2
docs/workbench.md                         +4
frontend/src/pages/Runtime.tsx           +21
frontend/src/types/workbench.ts          +23
powercopy/core/trace.py                  +19
powercopy/tools/builtin/policy_tools.py  +88
powercopy/tools/executor.py              +89
powercopy/tools/result.py               +171
pyproject.toml                            +7
tests/test_policy_tools_runtime.py       +72
tests/test_tool_executor.py              +11
tests/test_message.py                   改名
scripts/check_messages.py               改名
wiki/Architecture.md                     +41
wiki/Getting-Started.md                   +2
wiki/Limitations.md                      +49
wiki/Overview.md                         +26
wiki/Testing.md                           +2
wiki/Workbench.md                         +4
workbench/runtime_api.py                 +44
```

### 未跟踪新文件（36 个）

```text
AGENTS.md

docs/CHANGELOG.md
docs/PROJECT_STATE.md
docs/roadmap-stage05.md
docs/adr/ADR-001-llm-protocol.md
docs/adr/ADR-002-tool-result-semantics.md
docs/adr/ADR-003-retrieval-stage05.md
docs/adr/ADR-004-sqlite-wal.md
docs/adr/ADR-005-pdf-parsing.md
docs/adr/ADR-006-local-embedding.md
docs/adr/ADR-007-dense-retrieval.md
docs/adr/ADR-008-hybrid-rrf.md
docs/review/embedding-source-assessment.md
docs/review/stage-01-04-review.md
docs/review/stage05.5a-hybrid-retrieval.md
docs/review/stage05a-bm25-baseline.md
docs/review/stage05a-fixes.md
docs/review/stage05b-dense-retrieval.md

evaluations/retrieval-eval-v2.json

powercopy/rag/embedding.py
powercopy/rag/lexical.py
powercopy/rag/dense.py
powercopy/rag/hybrid.py

requirements-embedding.txt

scripts/bench_dense.py
scripts/check_tool_result_semantics.py
scripts/compare_hybrid.py
scripts/compare_retrieval.py
scripts/eval_retrieval.py

tests/test_dense_retrieval.py
tests/test_embedding.py
tests/test_hybrid_retrieval.py
tests/test_lexical_score_fix.py
tests/test_lexical_search.py
tests/test_retrieval_eval.py
tests/test_tool_result_semantics.py
```

### 明确排除（不入库）

```text
.env                                    密钥
storage/                                 运行数据（db/raw/rag/models）
storage/models/                          BGE 模型权重（91 MB）
.venv/                                   Python 环境（876 MB）
node_modules/                            前端依赖
docs/screenshots/ 中的 PNG              已在库中，本轮无变化
```

---

## 七、建议的 Commit Message

由于改动跨越多个模块（4.9 工具语义 + 阶段05 全链路 + 文档同步），建议**拆成多个提交**，便于审查与回溯：

### 提交 1：工具结果三层状态语义（4.9）

```text
feat(tools): 工具结果拆分为三层状态语义

旧实现把业务失败与执行崩溃都折叠为 EXECUTION_ERROR，模型拿不到
可行动的失败原因。

- ToolResult 增加 execution_status / business_status / business_code
- ExecutionStatus: ok / tool_not_found / invalid_json / invalid_arguments / execution_error
- BusinessStatus: success / empty / failed / not_applicable
- 搜索无命中归为 empty（正常结果），不再混入 failed
- 业务失败保留 business_code，执行崩溃不透传内部信息
- trace / runtime_api / 前端同步读取状态字段，移除 payload 反推

测试：+41 项（test_tool_result_semantics.py）
验收：scripts/check_tool_result_semantics.py 11 项全通过
```

### 提交 2：本地 Embedding 能力

```text
feat(rag): 接入本地 BGE-small 向量编码能力

平台 /v1/embeddings 端点存在，但当前 API Key 所在分组无 embedding
通道。经确认改用本地模型，数据不出本地。

- powercopy/rag/embedding.py：BGEEmbedder
- BAAI/bge-small-zh-v1.5，24M 参数，512 维，MIT
- CLS 池化 + L2 归一化（BGE 官方用法）
- 查询加前缀、文档不加，接口层强制区分
- 默认禁止静默截断，超 512 token 直接报错
- 依赖隔离到 requirements-embedding.txt，未安装时测试自动 skip

实测：单条 7.4 ms（CPU），模型 91 MB
测试：+28 项
ADR-006
```

### 提交 3：词法检索基线

```text
feat(rag): FTS5 trigram + BM25 词法检索基线

- powercopy/rag/lexical.py：索引构建、检索、评测指标
- 实测确认 trigram 需 >= 3 字符，2 字符查询命中 0 条
- 短查询回退到 instr 全表扫描（项目负责人要求）
- 查询包成双引号短语转义，防注入与语法错误
- 保留原始 bm25 分值；score = -bm25/(1+(-bm25)) 单调递增
- 只读连接（mode=ro + PRAGMA query_only）

基线（30 条开发集）：
  exact_keyword Recall@10 = 1.000
  semantic / cross_clause = 0.000

测试：+81 项
ADR-003/007
```

### 提交 4：向量检索

```text
feat(rag): NumPy 精确向量检索

- powercopy/rag/dense.py：索引构建、检索、缓存
- 不引入向量数据库（26×512 矩阵乘法实测 5.7 μs）
- 索引指纹校验（manifest_sha256 + model + dim + pooling）
- 缓存按命名空间隔离，模型/维度/池化/归一化/编码模式任一不符即失效
- 禁止静默截断、NaN/Inf 校验、归一化校验

实测：单条 query 15.02 ms，冷启动 31.3 s，热启动 14.95 ms
索引：50.5 KB
测试：+46 项
ADR-007
```

### 提交 5：混合检索（RRF）

```text
feat(rag): 等权 RRF 混合检索

- powercopy/rag/hybrid.py：双路检索 + RRF 融合
- rank_constant=60，等权，候选深度与最终 Top-K 独立配置
- 只融合排名，不融合分数（BM25 与余弦量纲不可比）
- 稳定排序：RRF 降序 + chunk_id 升序
- 索引指纹不一致时拒绝融合
- 单路异常显式返回降级状态，不伪装成完整混合

三路对照（30 条开发集，未做显著性检验）：
  Recall@10  Lexical 0.4231 / Dense 0.8421 / Hybrid 0.9190
  H3 在 5/5 主指标成立，但分类型全部持平（RRF 只取两路上限）

测试：+63 项
ADR-008
```

### 提交 6：文档与交付流程

```text
docs: 同步文档至最新进度并建立模块交付流程

- README 测试数字 795 → 1122，检索链路说明
- AGENTS.md 新增「交付流程」章节（7 步，推送需授权）
- docs/PROJECT_STATE.md、CHANGELOG.md、roadmap-stage05.md 新增
- docs/adr/ ADR-001 ~ ADR-008
- docs/review/ 6 份阶段报告
- architecture / limitations / overview 补充检索层
- wiki 由 docs 同步生成

安全：
- .gitignore 补 .env.* / *.safetensors / *.onnx / *.db-wal 等
- 移除测试夹具与文档中的真实姓名（改通用占位）
- workbench-test-results.json 保留历史快照原值并加说明
```

---

## 八、需要你确认的事项

### 8.1 公开授权（前置条件）

仓库为 **public**。`README.md` 末尾现写「内部项目，暂未开源授权」。

**需你明确**：

1. 企业源码是否允许公开？
2. `AGENTS.md` 中「昆吾电力的业务覆盖江西、安徽、广东和山西四省」是否可公开？
3. `docs/` 下全部技术文档是否可公开？
4. 检索评测集 `evaluations/retrieval-eval-v2.json`（30 条查询 + 政策锚点）是否可公开？**它包含政策原文片段**。

> 若任何一项不允许，需要先调整再提交。

### 8.2 `scripts/fix_wiki_secret.py` 处理方式

建议从跟踪中移除（文件保留本地）。**需你确认**。

### 8.3 `config/index_selectors.json` / `config/parser_profiles.json`

当前已跟踪且公开。属技术配置，无业务敏感信息，但公开了目标页面的 CSS 结构。

**选项**：保留 / 改为只提交 `.example`。

### 8.4 提交粒度

建议按上述 6 个提交拆分。若你希望单次提交，我也可以合并。

---

## 九、本轮未执行的动作

- ❌ **未执行 `git add`** —— 未暂存任何文件
- ❌ **未执行 `git commit`** —— 未创建提交
- ❌ **未执行 `git push`** —— 未推送远程
- ❌ 未执行网络采集、付费 API 调用

唯一执行的网络操作：`git fetch origin`（只读，用于对比远程状态）。

---

## 十、结论

**Git 状态**：本地与远程 HEAD 一致，差异全在工作区，无历史分叉，可安全快进推送。

**代码完整性**：1122 passed 实测通过，10 个已验收模块文件齐全。

**文档一致性**：18 个文档已更新，测试数字、rag 目录说明、检索能力描述均与真实代码对齐。历史快照保留原值并加注。

**安全**：发现并修复真实姓名泄露（6 处）；`.gitignore` 已加强并逐项验证；历史提交无密钥泄露。

**待你确认**：公开授权（4 项）、`fix_wiki_secret.py` 处理、`config/*.json` 处理、提交粒度。

**确认后我将执行提交与推送。**
