# dsh-mail-plugin 交付脚本

把本仓库的 `dsh-mail-plugin` 安装到 DSH profile、卸载或升级的三个 PowerShell 7 脚本。
所有操作都可以**重复执行**（幂等）；修改文件前会自动备份。

| 脚本 | 作用 |
|---|---|
| `install.ps1` | 安装/修复：junction、`package.json` 依赖与 bundles、`cordis.patch.yml` 条目、数据目录 |
| `uninstall.ps1` | 卸载：移除上述全部登记；可选删除数据目录 |
| `upgrade.ps1` | 升级：拉取最新代码，补齐三处登记，只补 patch 里缺失的配置键 |
| `_mail-plugin-common.ps1` | 三个脚本共用的函数库，**不要单独运行** |

> 所有脚本都只使用 PowerShell 7 内置能力，不需要安装任何模块。
> 执行策略受限时用：`pwsh -ExecutionPolicy Bypass -File .\install.ps1`

## 前置条件

- Windows + PowerShell 7（`pwsh`）；`#Requires -Version 7` 会拦住旧版 PowerShell。
- 目标 profile 目录已存在（通常由 DSH 创建）。
- `upgrade.ps1` 的默认升级方式需要 `git` 在 PATH 中；没有 git 时可用 `-SkipPull`。

## profile 路径怎么解析

三个脚本的 `-Profile` 参数支持两种写法：

1. profile 名称（默认 `desktop`）→ 解析为 `%USERPROFILE%\.dsh\profiles\<名称>`；
2. profile 目录的完整路径（例如 `D:\dsh\profiles\work`）。

如果设置了环境变量 `DSH_PROFILES_ROOT`，名称会解析到 `<DSH_PROFILES_ROOT>\<名称>`。
在 DSH 里运行的终端中，若 `DSH_PROFILE` 与传入名称一致，也会优先用 `DSH_PROFILE_DIR`。

## install.ps1

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `-Profile` | string | `desktop` | profile 名称或完整路径 |
| `-RepoPath` | string | 脚本上一级目录 | 仓库根目录（必须包含 `dsh-mail-plugin\lib\index.js`） |
| `-DataDir` | string | `<RepoPath>\data\mail` | 数据目录。**显式传入**才覆盖 patch 里的已有值 |
| `-WorkspaceDir` | string | `<RepoPath>\data\mail\workspace` | 邮件工作区目录。规则同上 |
| `-Force` | switch | 关 | junction 或依赖已指向别处时，强制改为本仓库 |

### 做了什么

1. 校验 profile、仓库、插件入口（`dsh-mail-plugin\lib\index.js`）；
2. 创建/修复 `<profile>\node_modules\dsh-mail-plugin` junction（Windows Junction，不需要管理员权限）；
3. 在 `package.json` 里确保：
   - `dependencies["dsh-mail-plugin"] = "link:<RepoPath>/dsh-mail-plugin"`；
   - `dsh.profile.bundles` 数组包含 `dsh-mail-plugin`（自动去重）；
4. 在 `cordis.patch.yml` 里确保 `- id: dsh-mail-plugin` 条目存在，并写入
   `syncIntervalMs` / `dataDir` / `mailWorkspace` / `mailWorkspaceId`，`disabled: false`；
5. 创建数据目录与工作区目录。

说明：

- `-DataDir` / `-WorkspaceDir` **未显式传入**时，沿用 patch 里已有的值，仅在缺失时使用默认值；
  显式传入则以参数为准。
- `mailWorkspaceId` 首次安装时生成 GUID，之后保持不变。
- 写回 `package.json` 前会先备份为 `package.json.bak-<yyyyMMddHHmmss>`，
  并在输出里打印“已备份到 <路径>”；`cordis.patch.yml` 同理。
- `package.json` 写回后**立即重新解析校验**；不合法则自动回滚到备份并以退出码 1 结束。

### 示例

```powershell
# 默认 desktop profile、默认数据目录
pwsh -File .\install.ps1

# 指定 profile 与数据目录
pwsh -File .\install.ps1 -Profile work -DataDir 'D:\MailData' -WorkspaceDir 'D:\MailWorkspace'

# 强制把已有的 junction / 依赖改指向本仓库
pwsh -File .\install.ps1 -Force
```

安装完成后**重启 DSH**（或重启后在「插件管理器」里把 `dsh-mail-plugin` 禁用再启用）使改动生效。

## uninstall.ps1

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `-Profile` | string | `desktop` | profile 名称或完整路径 |
| `-PurgeData` | switch | 关 | 同时删除数据目录（含邮件缓存与附件） |

### 做了什么

1. 从 `cordis.patch.yml` 移除 `dsh-mail-plugin` 条目；
2. 删除 `node_modules\dsh-mail-plugin` junction（只删链接，**不删仓库**）；
3. 从 `package.json` 删除 `dsh-mail-plugin` 依赖与 `dsh.profile.bundles` 条目；
4. `-PurgeData` 时，先打印将删除的路径并要求输入 `yes` 确认，然后删除数据目录。

说明：

- 完全幂等：没有可删内容时打印“已是卸载状态”并以退出码 0 结束。
- 不会删除仓库目录、profile 目录、用户主目录或盘符根目录；
  `dataDir` 指向这些位置时拒绝删除并报错。
- `-PurgeData` 删除的是 patch 里配置的 `dataDir`；条目已不存在时用默认 `<RepoPath>\data\mail`。
- 建议先退出 DSH 再用 `-PurgeData`，避免文件被占用。
- 卸载会移除 profile 里的配置；**重装时如果用了自定义数据目录，请显式再传一次
  `-DataDir` / `-WorkspaceDir`**，否则会回落到默认值（旧数据不会自动删除）。

### 示例

```powershell
# 只卸载插件，保留数据
pwsh -File .\uninstall.ps1

# 指定 profile，并交互确认后删除数据目录
pwsh -File .\uninstall.ps1 -Profile work -PurgeData
```

## upgrade.ps1

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `-Profile` | string | `desktop` | profile 名称或完整路径 |
| `-SkipPull` | switch | 关 | 跳过 `git pull --ff-only`，仅做本地补齐 |

### 做了什么

1. `git -C <仓库> pull --ff-only` 拉取最新代码（`-SkipPull` 跳过）；
2. 确保 junction、`package.json` 依赖/bundles、`cordis.patch.yml` 条目都存在（缺则补）；
3. 只补 patch `config` 里**缺失**的键：
   `syncIntervalMs` / `dataDir` / `mailWorkspace` / `mailWorkspaceId`；
   **绝不覆盖用户已有的** `dataDir`、`mailWorkspace`、`syncIntervalMs` 值。
4. 依赖或 junction 指向了别的路径时不会擅自改，提示先跑 `install.ps1 -Force`。

### 数据库备份策略

`upgrade.ps1` **不会复制 `mail.db`**：SQLite 在 WAL 模式下直接复制可能丢数据。

升级数据库由 Mail Core 在下次启动时负责：检测到 schema 需要升级时，会先把整库备份到
`<dataDir>\backups\mail-<时间戳>.db`，再执行迁移；备份失败会拒绝启动而不是带着未备份的库迁移。

因此升级流程是：运行 `upgrade.ps1` → 关闭并重新启动 DSH → 在插件面板确认收件箱与同步状态。
备份文件就在 `<dataDir>\backups\`，需要回滚时停掉 DSH、用备份文件替换 `mail.db` 即可。

### 示例

```powershell
# 正常升级（需要 git）
pwsh -File .\upgrade.ps1

# 只补齐配置/链接，不联网
pwsh -File .\upgrade.ps1 -SkipPull

# 指定 profile
pwsh -File .\upgrade.ps1 -Profile work
```

## 备份与回滚

每次真正写入 `package.json` 或 `cordis.patch.yml` 前，都会在同目录生成备份：

```
package.json.bak-20261105223705
cordis.patch.yml.bak-20261105223706
```

备份文件名带时间戳，不会覆盖已有备份。需要手工回滚时：

```powershell
Copy-Item .\cordis.patch.yml.bak-20261105223706 .\cordis.patch.yml -Force
```

脚本自身在 JSON 写回校验失败时会自动回滚；其它步骤失败会打印具体原因并以退出码 1 结束，
已经写入的文件可在输出里找到对应的 `.bak-*` 路径。

## 故障排查

| 现象 | 处理 |
|---|---|
| `无法加载文件，因为在此系统上禁止运行脚本` | 用 `pwsh -ExecutionPolicy Bypass -File .\install.ps1` |
| `需要 PowerShell 7（pwsh）或更高版本` | 安装 PowerShell 7，用 `pwsh` 而不是 `powershell` 运行 |
| `未找到 git 命令` | 安装 Git 并加入 PATH，或改用 `upgrade.ps1 -SkipPull` |
| `junction 已存在但指向…` | 确认目标后运行 `install.ps1 -Force` 重建 |
| `路径已存在但不是 junction/symlink` | 手工删除该路径（可能是真实目录），再重新安装 |
| `package.json 不是合法 JSON` | 先修复 profile 的 `package.json`，脚本不会改动非法文件 |
| `config 使用了内联写法` | 把该条目的 `config: {…}` 改成块写法后重试 |

## 注意

- 三个脚本只修改目标 profile 目录内的 `package.json`、`cordis.patch.yml`、`node_modules` 链接，
  以及显式/默认的数据目录；不修改仓库源码。
- 不要同时运行多个安装/升级脚本；写文件前请确保没有其它进程正在编辑同一 profile。
