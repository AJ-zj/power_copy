# 定时增量采集说明

**版本**：`ingest-schedule-v1`
**日期**：2026-11-27
**分支**：`feat/mvp05-cost`（本轮附带交付）

---

## 一、解决什么问题

之前采集政策必须人工触发，每次手动跑命令、手写审批文件。
数据会停留在上次人工采集的状态，没有「自动发现新政策」的能力。

本轮新增**轻量定时采集**：

```text
列表页  → 每天扫一次，发现新政策
详情页  → 每周抓一次，正文可能有更新
```

---

## 二、设计边界（刻意做小）

| 不做 | 原因 |
|---|---|
| cron 表达式解析 | 只需要每天 / 每周两种，多写是浪费 |
| 分布式协调 | 单机单进程，SQLite 状态表足够 |
| 任务队列 | 与既有 `IngestionPipeline` 一致，顺序执行 |
| 后台常驻进程 | 崩溃重启/时钟漂移/单实例锁问题太多，改用系统计划任务 |
| 自动决定审批 | **审批必须由操作员逐条确认，调度器不能替代** |

**为什么用「一次性执行 + 系统计划任务」而不是常驻进程：**

常驻进程要处理崩溃恢复、时钟漂移、重复启动。而「跑一次就退出」由
操作系统定时调用，状态落 SQLite，天然幂等。复杂度低一个数量级。

---

## 三、模块

```text
powercopy/data/scheduling.py        到期判断 + 状态存储 + 配置
powercopy/data/schedule_runner.py   执行器（审批过滤 + 失败计数 + 暂停）
scripts/run_ingestion_schedule.py   CLI
config/ingest_schedule.json         调度配置（可提交）
config/ingestion_approval.json      真实审批（gitignore，本地填写）
```

---

## 四、用法

### 4.1 查看计划（不发请求）

```powershell
python scripts/run_ingestion_schedule.py --plan
```

输出每个目标的到期状态、上次成功时间、下次到期时间。

### 4.2 执行

```powershell
python scripts/run_ingestion_schedule.py
```

只跑**到期且有审批**的目标。

### 4.3 查看状态与历史

```powershell
python scripts/run_ingestion_schedule.py --status
python scripts/run_ingestion_schedule.py --history
```

### 4.4 恢复被暂停的目标

```powershell
python scripts/run_ingestion_schedule.py --resume ndrc/policy_index
```

### 4.5 强制跑所有启用目标

```powershell
python scripts/run_ingestion_schedule.py --force
```

---

## 五、配置定时执行

**本项目不内置调度器守护进程。** 用操作系统自带能力：

### Windows（计划任务）

```powershell
# 每天 09:00 执行
schtasks /create /tn "PowerCopy-PolicyIngest" /tr ^
  "D:\...\PowerCopy-V2\.venv\Scripts\python.exe D:\...\scripts\run_ingestion_schedule.py" ^
  /sc daily /st 09:00

# 立即测试
schtasks /run /tn "PowerCopy-PolicyIngest"

# 删除
schtasks /delete /tn "PowerCopy-PolicyIngest" /f
```

### Linux（cron）

```cron
0 9 * * * cd /path/to/PowerCopy-V2 && .venv/bin/python scripts/run_ingestion_schedule.py >> storage/ingest/cron.log 2>&1
```

**每天跑一次足够**：调度器内部会判断哪些目标到期
（列表页每天跑，详情页 7 天内的会自动跳过）。

---

## 六、配置格式

`config/ingest_schedule.json`（**可以提交**，不含敏感信息）：

```json
{
  "max_requests_per_run": 8,
  "interval_seconds": 2.0,
  "pause_after_failures": 3,
  "targets": [
    {
      "target": "ndrc/policy_index",
      "frequency": "daily",
      "enabled": true,
      "note": "发改委政策列表，发现新发布"
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `frequency` | `daily` / `weekly` / `manual` |
| `enabled` | `false` 则永不自动运行 |
| `max_requests_per_run` | 单次运行的总请求上限（1-100） |
| `interval_seconds` | 请求间隔，最小 2 秒 |
| `pause_after_failures` | 连续失败多少次自动暂停 |

**到期基准是「上次成功」而非「上次尝试」** —— 失败后会继续重试，
除非连续失败达阈值被暂停。

---

## 七、安全约束

### 7.1 审批是硬门槛

`config/ingestion_approval.json`（**gitignore**）必须为每个目标提供：

```json
{
  "ndrc/policy_index": {
    "url": "https://zfxxgk.ndrc.gov.cn/web/",
    "approved": true,
    "evidence": "2026-11-27 人工核对 robots.txt 与使用条款"
  }
}
```

三项缺一不可：`url` 必须与注册表一致、`approved` 必须为 `true`、
`evidence` 必须非空白。

**调度器不会伪造审批。** 缺审批的目标直接跳过并在结果中记录
`NO_OPERATOR_APPROVAL`，不降级、不静默放行。

### 7.2 自动暂停机制

| 触发 | 行为 |
|---|---|
| 连续失败达 `pause_after_failures` | 暂停该目标，需 `--resume` |
| 目标站返回 **403 / 429** | **立即**暂停，不等计数 |

403/429 是对方明确拒绝，继续抓既无礼也可能被封 IP。

### 7.3 请求预算

单次运行有总请求上限（默认 8）。超出即停止，
不会因为「今天目标多」就无限抓。

### 7.4 不记录敏感信息

运行记录只存 `run_id` / 时间 / 状态 / 请求数 / 错误码。
**不存网页内容、不存响应头、不存凭据。**

---

## 八、状态存储

```text
storage/ingest/schedule.db    ← 已 gitignore
```

表：

```sql
ingest_state   -- 每个目标的最后成功/尝试时间、失败计数、暂停状态
ingest_runs    -- 每次运行的记录
```

**与政策库分开**：`storage/policies_versioned.db` 需要备份，
采集状态丢了只是重跑一次。两者生命周期不同。

---

## 九、与既有采集的关系

```text
run_ingestion.py            手动指定目标，一次性执行（原有）
run_ingestion_schedule.py   按到期判断，自动决定跑哪些（新增）
```

两者共用 `IngestionPipeline` 与审批机制。调度器只是**在外面加了一层
「该不该跑」的判断**，不改变采集本身的语义。

---

## 十、已知限制

| 限制 | 说明 |
|---|---|
| **无 robots.txt 自动检查** | 仍靠人工确认（`evidence` 字段就是这个用途） |
| **无令牌桶限流** | 只有 2 秒固定间隔 |
| 无跨进程协调 | 两个进程同时跑会重复采集（有幂等，但浪费请求） |
| 不做新政策正文自动抓取 | 列表页只「发现候选」，正文需另行审批与配置 |
| 无失败告警通知 | 只写状态表，不主动推送 |
| 依赖系统计划任务 | 本模块不含守护进程 |

---

## 十一、验收方式

```powershell
# 1. 计划正确
python scripts/run_ingestion_schedule.py --plan

# 2. 无审批时不发请求
python scripts/run_ingestion_schedule.py
# 期望：status = no_approved_targets，skipped 列出所有目标

# 3. 补齐审批后执行（需真实授权）
python scripts/run_ingestion_schedule.py

# 4. 状态与历史
python scripts/run_ingestion_schedule.py --status
python scripts/run_ingestion_schedule.py --history
```
