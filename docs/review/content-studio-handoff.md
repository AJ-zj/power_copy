# Content Studio 集成交接

Content Studio 的创作请求经 `workbench` 交给共享 `ContentWorkflowService`。本地政策库与索引支撑 Mock 研究、创作和审核。Live 需要服务端开关、可用模型配置以及请求内的确认。接口详见 [Content Studio API](../contracts/content-studio-api.md)。

## 实际验证

- Windows Node 24.22.3、pnpm 11.25.0 与 Vite 6.4.6。TypeScript 类型检查和生产构建通过。
- Edge 通过生产预览提交安徽工商业电力市场的 Mock 创作任务。FastAPI 返回 `pending_review`、16 条政策证据、四个审核维度、修订记录和 7/12 次共享模型尝试。验收中 Live 模式关闭。
- 浏览器验证草稿引用能打开对应政策原文，修改正文后当前草稿显示待重新审核。剪贴板与文本导出都与编辑后的标题和正文完全一致。[创作结果](../screenshots/content-studio-mock.png) · [编辑后的草稿](../screenshots/content-studio-edited.png)
- 后端全量回归 1395 项通过。共享预算回归验证研究与文案阶段的尝试总和，修订上限与 Live 门禁等 Content API 专项测试共 10 项通过。

## 已知限制

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

启动前后端后，在已配置 Playwright 和 Chromium 的环境中，从项目根目录运行 `node scripts/check_content_studio.cjs`。用 `POWERCOPY_WEB_URL` 设置前端地址，脚本默认访问 `http://127.0.0.1:6184`。需要复用本机安装时可设置 `POWERCOPY_PLAYWRIGHT` 和 `POWERCOPY_BROWSER`。
