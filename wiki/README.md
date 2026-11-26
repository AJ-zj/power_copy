# Wiki 发布说明

这个目录存放 GitHub Wiki 的完整内容。**已配置 GitHub Actions 自动同步**，正常情况下不需要手动发布。

## 自动同步（推荐）

推送到 `main` 且改动涉及 `wiki/**` 时，`.github/workflows/sync-wiki.yml` 会自动把本目录内容同步到 Wiki。

### 一次性配置：添加 WIKI_TOKEN

GitHub Actions 自带的 `GITHUB_TOKEN` **没有权限推送 Wiki 仓库**，需要单独创建一个 Personal Access Token。

#### 1. 创建 PAT

访问 https://github.com/settings/tokens/new

| 字段 | 值 |
|---|---|
| Note | `PowerCopy Wiki Sync` |
| Expiration | 建议 90 天或自定义 |
| Scopes | 勾选 **`repo`**（完整仓库权限，Wiki 推送需要） |

点 **Generate token**，复制生成的 `ghp_...`（只显示一次）。

#### 2. 添加到仓库 Secrets

访问 https://github.com/AJ-zj/power_copy/settings/secrets/actions

点 **New repository secret**：

- Name: `WIKI_TOKEN`
- Secret: 粘贴刚才的 token

#### 3. 验证

访问 https://github.com/AJ-zj/power_copy/actions/workflows/sync-wiki.yml

点 **Run workflow** → 勾选 `dry_run`（先试运行）→ **Run workflow**

试运行会检测差异但不推送。确认无误后，再跑一次不勾选 `dry_run` 的。

### 工作流行为

| 触发条件 | 行为 |
|---|---|
| 推送 `wiki/**` 到 main | 自动同步 |
| 手动触发 + `dry_run=true` | 只检测差异，不推送 |
| 手动触发 + `dry_run=false` | 完整同步并推送 |
| 内容无变化 | 跳过提交，直接成功 |

安全措施：

- `concurrency` 保证同一时刻只有一个同步任务
- 同步前清空 Wiki 仓库旧内容（保留 `.git`）
- `README.md`（本文件）不会被发布到 Wiki
- 缺少 `WIKI_TOKEN` 时明确报错并提示配置方法

## 手动发布（备用）

如果 Actions 不可用，可手动执行：

```powershell
cd D:\桌面\昆吾电力项目
git clone https://github.com/AJ-zj/power_copy.wiki.git
Copy-Item -Path .\PowerCopy-V2\wiki\* -Destination .\power_copy.wiki\ -Recurse -Force
Remove-Item .\power_copy.wiki\README.md
cd .\power_copy.wiki
git add -A
git commit -m "docs: 手动同步 wiki"
git push
```

## 目录内容

| 文件 | 对应 Wiki 页面 |
|---|---|
| `Home.md` | 首页（导航 + 界面预览） |
| `Overview.md` | 项目概览 |
| `Getting-Started.md` | 快速开始 |
| `Architecture.md` | 系统架构 |
| `Data-Center.md` | 数据中心详解 |
| `Chunking.md` | 文档切片 |
| `Workbench.md` | 工作台 |
| `Testing.md` | 测试与验收 |
| `Limitations.md` | 技术边界 |
| `README.md` | 本文件（不发布到 Wiki） |
| `images/` | 9 张界面截图 |

## 链接语法

**Wiki 页面**用页面名（无后缀）：

```markdown
[项目概览](Overview)
```

**仓库内文档**（`docs/`）用相对路径：

```markdown
[项目概览](overview.md)
```

两套语法各自匹配各自场景，不要混用。

## 内容来源

本目录内容与 `docs/` 基本一致，差异：

- `Home.md` 是 Wiki 专用首页，`docs/` 里没有对应文件
- 其他 8 篇与 `docs/` 同名文件内容相同

更新时的推荐流程：

1. 改 `docs/` 下的源文件
2. 同步到 `wiki/`（保持文件名映射）
3. 提交推送到 main
4. Actions 自动同步到 Wiki

截图来自 `docs/screenshots/`：

```powershell
Copy-Item -Path .\docs\screenshots\*.png -Destination .\wiki\images\ -Force
```

## 卸载自动同步

如果不再需要：

```powershell
Remove-Item .\.github\workflows\sync-wiki.yml
```

然后删除仓库的 `WIKI_TOKEN` secret。
