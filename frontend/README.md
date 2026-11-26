# PowerCopy V2 工作台前端

使用 React、TypeScript、Vite 和 Lucide 图标。工作台使用午夜黑、电蓝和橙色主题。

```powershell
pnpm install --frozen-lockfile
pnpm dev
pnpm typecheck
pnpm build
```

开发服务默认监听 `http://127.0.0.1:5173`，转发工作台 API 到 `http://127.0.0.1:8000`。通过 `POWERCOPY_API_URL` 可以指定本地 API 地址。后端启动及联调命令见 [工作台交付文档](../docs/workbench.md)。
