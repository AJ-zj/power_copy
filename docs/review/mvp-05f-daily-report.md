# MVP-05-F 日报验收

工作台导航「每日报告」支持北京时间日期筛选、研发事项人工补充和 Markdown 导出。人工补充随导出保存，在当前页面内可跨日期切换查看。

| 区域 | 来源 |
| --- | --- |
| 文案与工作流审核 | 本机 `storage/workflow/*.json` 中的 `content-workflow-v1` 导出，按创建日期筛选并按任务去重 |
| 版本与 Git 提交 | 当前工作树的版本与 HEAD，提交按所选日的提交者时间筛选，最多展示 100 条 |
| 测试结果 | `docs/workbench-test-results.json` 的实测快照，日期相符时展示 |
| Token 与费用 | 研发模型和 PowerCopy 运行分别显示待统计及待对账状态 |

`GET /api/workbench/reports/daily?day=YYYY-MM-DD` 读取固定来源并返回摘要，省略日期时默认北京时间当天。工作流完整正文、调用数据和原始导出文件保留在本机。现有 CLI 可用 `--json storage/workflow/<文件名>.json` 保存实际运行结果。

验收使用现有 CLI 生成的本地 Mock 工作流导出。Edge 验证了当日文案、前一日测试快照、日期隔离、缺失费用状态、人工补充及 Markdown 下载。TypeScript 类型检查、Vite 生产构建及后端全量回归（1396 项）通过。

Content Studio 的网页运行历史与人工审批仍需持久化和授权服务。成本管理、调用重试明细、历史趋势、硬上限和软预算配置需接入 Flash 的成本核算契约及后端服务。
