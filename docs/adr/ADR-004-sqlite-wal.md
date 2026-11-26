# ADR-004：SQLite 启用 WAL 模式

- **状态**：提议（待项目负责人确认）
- **日期**：2026-11-26
- **决策者**：项目负责人
- **相关阶段**：阶段04 Data Center
- **关联问题**：P0-5

---

## 背景

PowerCopy V2 使用 SQLite 单文件数据库，存在三类并发访问：

| 访问者 | 模式 | 连接方式 |
|---|---|---|
| `VersionedPolicyStore` | 写（`BEGIN IMMEDIATE`） | `sqlite3.connect(path)` |
| `PolicyQueryService` | 只读 | `mode=ro` + `PRAGMA query_only=ON` |
| `workbench/data_api.py` | 只读 | 独立连接 |

复审发现：**代码中未设置 `journal_mode`**，SQLite 使用默认的 rollback journal 模式。

在此模式下：

- 写事务需要获取排它锁
- 读事务在写事务进行时被阻塞
- 工作台的只读查询会等待采集写入完成

当前数据量小、访问频率低，问题尚未暴露。但工作台的"采集流水线"页面在展示运行状态时会并发查询，而采集过程可能正在写入。

---

## 备选方案

### 方案 A：保持默认 rollback journal

**优点**
- 零改动
- 单线程批量写反而更快

**缺点**
- 读写互斥
- 长时间查询会阻塞写入

### 方案 B：启用 WAL 模式（推荐）

```python
conn.execute("PRAGMA journal_mode=WAL")
```

**优点**
- 读写不互斥：读者读自己的快照，写者追加 WAL 文件
- 公开实测：并发读提升约 12.6 倍，写提升约 6.6 倍
- 只需一次设置，持久化在数据库文件里

**缺点**
- 单线程大批量写反而慢约 1.8 倍（写放大）
- 需要 `-wal` 和 `-shm` 辅助文件
- 网络文件系统（NFS）上不可用

### 方案 C：改用 PostgreSQL

**优点**
- 原生并发控制

**缺点**
- 需要独立服务与运维
- 当前数据量完全不需要

---

## 决策建议

**推荐方案 B：启用 WAL。**

理由：

1. **并发形态匹配**。当前是"多个读者 + 偶尔一个写者"，正是 WAL 的优势场景。实测数据显示这种模式读提升 12.6 倍。

2. **单线程批量写的劣势在本项目不成立**。项目中没有"单事务写 50000 行"的场景。采集是逐条写入，每份政策一个事务。WAL 的写放大劣势不明显。

3. **无额外依赖**。`PRAGMA journal_mode=WAL` 是 SQLite 内置能力。

4. **已有代码兼容**。`BEGIN IMMEDIATE` 在 WAL 模式下仍然有效，语义不变。

### 实施位置

| 文件 | 改动 |
|---|---|
| `data/versioning.py` | `_connect()` 中加 `PRAGMA journal_mode=WAL` |
| `data/query.py` | 只读连接**不需要**改（WAL 由数据库文件属性决定） |
| `workbench/data_api.py` | 只读连接不需要改 |

**关键**：WAL 是数据库级别的持久属性，只需在任一次连接时设置，之后该文件永久生效。所以只在 `versioning.py` 的写连接里设置即可。

### 风险控制

1. **辅助文件已在 gitignore 中**。`.gitignore` 已有 `storage/*.db-*`，覆盖 `-wal` 和 `-shm`。
2. **备份需包含辅助文件**。WAL 模式下直接复制 `.db` 文件可能丢失未 checkpoint 的数据。备份脚本需 `PRAGMA wal_checkpoint(TRUNCATE)` 后再复制。
3. **需要在测试中验证**。现有测试用临时数据库，WAL 模式不影响临时文件的创建与清理。

---

## 后果

### 正面

- 工作台查询不再被采集写入阻塞
- 并发读性能显著提升
- 崩溃恢复能力更好（WAL 提供原子提交保证）

### 负面

- 数据库目录多出 `-wal` / `-shm` 文件
- 备份流程需要调整
- 单事务大批量写性能略降（本项目无此场景）

---

## 实施检查清单

- [ ] `versioning.py` 的 `_connect()` 增加 `PRAGMA journal_mode=WAL`
- [ ] 验证 `PRAGMA journal_mode` 返回 `wal`
- [ ] 现有 820 项测试全部通过
- [ ] 增补测试：并发读写在 WAL 模式下的行为
- [ ] 文档记录备份流程变更
- [ ] 确认 `.gitignore` 覆盖 `-wal` / `-shm`（已确认 `storage/*.db-*`）

---

## 重新评估触发条件

1. 若未来迁移到 PostgreSQL/MySQL，本决策自动失效
2. 若数据库放在网络文件系统上（WAL 不支持）
3. 若出现"单事务大批量写入"场景，需重新权衡
4. 若辅助文件管理造成运维困扰

---

## 参考

- [SQLite WAL 并发机制讨论](https://www.sqlite.org/forum/forumpost/ab2f360c5010c025?raw)（访问 2026-11-26）
- [SQLite WAL 实测数据](https://bbs.huaweicloud.com/blogs/508031)（访问 2026-11-26）
- 当前实现：`powercopy/data/versioning.py` L84-L92
