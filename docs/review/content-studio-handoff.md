# Content Studio 集成交接

Content Studio 的创作请求经 `workbench` 交给共享 `ContentWorkflowService`。本地政策库与索引支撑 Mock 研究、创作和审核。Live 需要服务端开关、可用模型配置以及请求内的确认。接口详见 [Content Studio API](../contracts/content-studio-api.md)。

## 实际验证

- Windows Node 24.22.3 与 pnpm。TypeScript 类型检查及 Vite 6.4.6 生产构建通过。
- 从真实 FastAPI 发起 Mock 创作得到 HTTP 200、`pending_review`、政策证据、可编辑草稿和机器审核报告。测试问题为“工商业企业参与电力市场有哪些基本规则”，请求地区为安徽。
- 同一服务的 RAG 状态显示 2 份文档、26 个切片，词法与 Dense 索引就绪。此前 RAG 浏览器验证及其截图见 [RAG Studio 契约](../contracts/mvp01-retrieval-api.md)。
- 后端全量回归 1395 项通过（67.23 秒），含 Content API 可用性、Live 双门禁、参数约束和异常脱敏用例。
- 已启动生产预览。浏览器操作通道因无法可靠识别当前 Windows 浏览器地址而终止。Content Studio 的浏览器证据展开、编辑、空证据及截图验收待完成。真实付费模型调用未执行。

## 已知限制

- 当前整合的后端版本中，CopywritingAgent 创建独立预算。研究与审核使用工作流共享快照，快照不包含文案阶段的完整模型尝试与 Token 消耗。预算快照适用于查看已记录用量，跨阶段总限额由后端业务分支补齐后复验。
- 政策地区与现行效力、证据与主张的语义支持性均需人工核查。草稿编辑仅保存在当前页面，复制与文本导出可带走编辑结果。

## 本机运行

在项目根目录打开一个 PowerShell 启动服务。在第二个 PowerShell 切换到前端目录运行 Vite。

```powershell
$env:POWERCOPY_WORKBENCH_ALLOW_LIVE='0'
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
.\.venv\Scripts\python.exe -m uvicorn workbench.main:app --host 127.0.0.1 --port 8000
```

```powershell
Set-Location .\frontend
pnpm dev
```

打开 http://127.0.0.1:5173/#/content。
