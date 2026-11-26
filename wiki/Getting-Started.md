# 快速开始

## 前置要求

- Python 3.12+
- Node.js 18+（仅前端需要）
- pnpm（前端包管理）
- Git

## 后端安装

```powershell
cd PowerCopy-V2
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装项目本身（含 dev / api 依赖）
pip install -e ".[dev]" -e ".[api]"

# 安装数据层依赖
pip install -r requirements.txt
```

## 配置

```powershell
copy .env.example .env
```

编辑 `.env`，填入你的配置：

```ini
# 旧版配置（core/llm.py 直接读取）
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://你的供应商/v1
LLM_MODEL=你的模型ID
LLM_TIMEOUT=60

# 新版配置（core/config.py 读取）
POWERCOPY_ENV=development
POWERCOPY_LLM_BASE_URL=https://你的供应商/v1
POWERCOPY_LLM_MODEL=你的模型ID
POWERCOPY_LLM_API_KEY=你的密钥
POWERCOPY_LLM_TIMEOUT=90

# 政策库路径
POLICY_DB_PATH=storage/policies_versioned.db
```

> ⚠️ **两套配置并存**：`core/llm.py` 读 `LLM_*`，`core/config.py` 读 `POWERCOPY_*`。接不同模块时注意区分。
>
> `.env` 已在 `.gitignore` 中，永远不会被提交。

## 验证安装

```powershell
python -m pytest tests -q
```

期望输出：

```text
795 passed
```

> 部分依赖真实采集文件的测试会自动跳过。要跑完整测试，需先执行一次采集，或使用仓库自带的合成 PDF 样本。

## 启动后端

```powershell
uvicorn powercopy.api.main:app --reload --port 8000
```

访问：

- http://127.0.0.1:8000/health — 健康检查
- http://127.0.0.1:8000/docs — API 文档

## 启动工作台

工作台需要前后端同时运行。

**后端**（另开一个终端）：

```powershell
uvicorn workbench.main:app --reload --port 8000
```

**前端**：

```powershell
cd frontend
pnpm install
pnpm dev
```

访问 http://127.0.0.1:5173

> `workbench/main.py` 启动时会校验 `POWERCOPY_PROJECT_ROOT` 指向的项目是否含 `powercopy/core/trace.py`。默认指向项目自身，无需额外配置。

## 首次数据采集

如果需要真实政策数据，按以下顺序执行：

```powershell
# 1. 采集官方网页
python -m scripts.fetch_policy ndrc market_rules
python -m scripts.fetch_policy nea registration_rules

# 2. 查看原始文件 ID
Get-ChildItem storage/raw/*.json

# 3. 解析预览（不写库）
python -m scripts.parse_policy parse storage/raw/<fetch_id>.json --show-body

# 4. 确认无误后入库
python -m scripts.ingest_versioned --db storage/policies_versioned.db ingest storage/raw/<fetch_id>.json

# 5. 检查入库结果
python -m scripts.ingest_versioned --db storage/policies_versioned.db counts

# 6. 构建切片
python -m scripts.build_chunks --db storage/policies_versioned.db --out storage/rag/chunks.json
```

> ⚠️ **采集前请确认**：目标网站的 robots.txt、访问条款、企业使用权限。当前采集器不做自动检查，需人工确认。

## 常见问题

### `ModuleNotFoundError: No module named 'dotenv'`

没在虚拟环境里运行。确认激活了 `.venv`：

```powershell
.\.venv\Scripts\Activate.ps1
```

### 测试出现 1 个失败（窗口编码问题）

如果你在 Windows 上遇到 `test_live_replay` 失败，检查 Python 默认编码：

```powershell
python -c "import locale; print(locale.getpreferredencoding())"
```

如果输出 `cp936`，说明系统是 GBK 环境。当前测试代码已全部显式指定 `encoding='utf-8'`，正常不会出问题。

### 前端 `pnpm dev` 报错找不到模块

```powershell
cd frontend
pnpm install --force
```

### 工作台 API 返回 404

确认用的是 `workbench.main:app` 而非 `powercopy.api.main:app`。前者才挂载了 `/api/workbench` 路由。

### 数据库不存在

```powershell
python -m scripts.ingest_versioned --db storage/policies_versioned.db ingest storage/raw/<id>.json
```

首次入库会自动创建表结构。

## 目录权限

项目可以放在任何位置，但注意：

- 路径含中文（如 `D:\桌面\`）在 Windows 下正常工作
- 含中文的路径曾导致一个测试因编码问题失败，已修复
- 建议路径不要太深，避免 Windows 260 字符限制

## 下一步

- [系统架构](architecture.md) — 理解分层设计
- [数据中心详解](data-center.md) — 深入数据链路
- [测试与验收](testing.md) — 跑完整验收
