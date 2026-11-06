# DSH 邮箱自动化插件 · 用户手册

> 版本：v0.1.0（schema v12）
> 适用：DeepSeek Harness 桌面版（Windows）

---

## 1. 这是什么

一个跑在 DeepSeek Harness 里的邮箱插件：

- **收信**：IMAP（实时 IDLE 推送）/ POP3（定时轮询）
- **发信**：必须经过你本人审批，Agent 不能自己发
- **待跟进**：新邮件自动进待办，可以交给 Agent 总结归档
- **附件**：只存元数据，你点了才下载；下载后可作为文件直接传给 Agent
- 所有数据在本地 SQLite，密码用 Windows DPAPI 加密

插件由两部分组成：一个 DSH 宿主插件（`dsh-mail-plugin/`）+ 一个 Python Core（`mail-core/`，由插件自动拉起，**不需要你单独运行**）。

---

## 2. 环境要求

| 项 | 要求 |
|---|---|
| DSH | 桌面版，profile 目录存在（默认 `C:\Users\<你>\.dsh\profiles\desktop`） |
| 操作系统 | Windows 10/11 |
| Python | **不需要你装**，使用 DSH 自带运行时 |
| 邮箱 | 支持 IMAP 或 POP3；发信需要 SMTP |

---

## 3. 安装

### 3.1 用脚本（推荐）

```powershell
pwsh -File scripts/install.ps1 -Profile desktop
```

脚本会：建立插件目录联接 → 写入 profile 配置 → 创建数据目录，然后提示你重启 DSH。

可选参数：

| 参数 | 说明 |
|---|---|
| `-Profile` | profile 名，默认 `desktop` |
| `-RepoPath` | 仓库路径，默认脚本上级目录 |
| `-DataDir` | 数据库位置，默认 `<仓库>\data\mail` |
| `-WorkspaceDir` | 邮件会话工作区，默认 `<DataDir>\workspace` |
| `-Force` | 目录联接已指向别处时强制重建 |

### 3.2 手工安装

1. 在 `profiles\desktop\node_modules\` 下建一个目录联接指向 `dsh-mail-plugin\`
2. 在 `profiles\desktop\cordis.patch.yml` 追加：

```yaml
- id: dsh-mail-plugin
  name: dsh-mail-plugin
  config:
    syncIntervalMs: 300000
    dataDir: <仓库路径>\data\mail
  disabled: false
```

3. 重启 DSH

### 3.3 装完检查

左侧栏出现「邮箱」图标；打开后顶栏显示 `Mail Core ready` 即为正常。

### 3.4 用打包好的 tgz 安装（换机器时推荐）

在仓库里先打包：

```powershell
node scripts/build-package.mjs     # 产出 dist\dsh-mail-plugin-<version>.tgz
```

把 tgz 拷到目标机器，然后：

```powershell
dsh plugin --profile desktop add file:./dist/dsh-mail-plugin-0.1.0.tgz
```

`dsh` 是 DSH 自带命令行（`resources\runtime\cli\bin\dsh.cmd`），它会：

1. 把包装进 profile 的 `node_modules`
2. 在 `package.json` 的 `dependencies` 与 `dsh.profile.bundles` 登记
3. 下次启动 DSH 时自动挂载

这种装法下 **Python Core 从包内 `mail-core/` 启动**，数据目录默认在 `%USERPROFILE%\.dsh\dsh-mail\data`（可在设置里改）。

---

## 4. 首次配置：添加邮箱

设置页 → 添加账户：

| 字段 | 说明 |
|---|---|
| 邮箱地址 | 你的完整邮箱 |
| 显示名称 | 可选；填了以后账户栏显示名称而不是地址 |
| 协议 | IMAP（推荐，支持实时推送）或 POP3（只能轮询） |
| IMAP/POP3 主机、端口、加密 | 企业邮箱通常是 `imap.xxx.com:993 SSL` |
| SMTP 主机、端口、加密 | 发信才需要，通常是 `smtp.xxx.com:465 SSL` |
| 密码 | 有客户端授权码的邮箱（如阿里企业邮、163）请填授权码 |

保存后点「测试连接」：能看到文件夹数且 `smtp ok` 才算配好。

---

## 5. 界面导览

| 页签 | 作用 |
|---|---|
| 待跟进 | 新邮件 + 未处理附件的待办清单，Agent 总结入口 |
| 收件箱 | 本地索引的邮件，按对话气泡展示 |
| 发件箱 / 草稿箱 / 已删除 / 垃圾邮件 | 对应服务器文件夹 |
| 模板 | 本地保存的邮件模板（不同步服务器） |
| 通讯录 | 本地联系人；可直接「在会话中讨论」该联系人 |
| 知识库 | 总结 / 文档 / 表格 / 图片 / 日程 / 压缩文件 / 视频 / 其他（按附件扩展名归类，归不进的进「其他」）；分类可折叠，总结可删除、文件可预览或删除本地副本；顶栏搜索在此页只搜文件 |
| 设置 | 账户、同步、数据位置、备份、清理 |

右侧「审批历史」栏可收起/展开，左边缘可拖动改宽度。

---

## 6. 收信与待跟进

- **IMAP 账户**：Core 建立 IDLE 长连接，新邮件到达约 30 秒内推送（服务端约 5 分钟断连，插件会自动重连）
- **POP3 账户**：按「自动同步间隔」（默认 5 分钟）轮询
- 新邮件进入**待跟进**并弹 Windows 通知（标题=主题，正文=发件人）
- **首次接入账户的那次同步不算待办**（避免把存量邮件全塞进来）

待办操作：

- **总结**：新建/复用会话，把邮件（含附件）交给 Agent
- **已处理**：该邮件及其未处理附件一并移出待办
- 点「下载并准备分析」= 下载附件 + 自动进入总结流程

---

## 7. Agent 总结流程

邮件详情底部的 Agent 区：输入问题点「在会话中讨论」，不填则按「分析这封邮件」处理；同一发件人复用同一条会话。\n\n点「总结」后：

1. 插件在「邮件工作区」里新建（或复用同发件人的）会话
2. 已下载的附件作为**文件**随消息上传，Agent 能直接读 PDF/xlsx
3. Agent 先调 `mail_triage_context` 拉上下文（同线程、相关邮件、历史总结、附件）
4. 有历史总结时，会先把旧结论列出来问你如何合并
5. 按 8 类归档：报价询价 / 合同协议 / 会议安排 / 项目进展 / 发票付款 / 人事招聘 / 通知公告 / 其他
6. 你说处理完了，Agent 调 `mail_mark_done`（你也可以自己点「已处理」）

---

## 8. 发信与审批

发送必须经过**你本人**在 DSH 界面点「允许」：

```
Agent 调 mail_send_draft → 弹审批框 → 你点允许 → Core 校验内容哈希 → SMTP 发送
```

- 插件**没有**任何 HTTP 接口能批准或发送，Agent 无法自我批准
- 草稿内容一改，原审批自动失效（内容哈希绑定）
- 审计字段：谁批的（`prompt`=弹窗 / `policy-never`=策略放行）、批的时间
- 宿主审批策略为 `never` 时，发送会被**拒绝**（因为不会弹框 = 没有人工确认）

---

## 9. 数据、备份与恢复

### 9.1 位置

设置页「数据库位置」可改。切换时会**复制** mail.db（含 WAL）、附件、备份到新目录，然后重启 Core；旧目录保留作为天然备份。

### 9.2 备份

- **自动**：Core 每次迁移 schema 前自动备份
- **手动**：设置页「立即备份」
- 备份文件在 `<数据目录>\backups\`，保留最近 10 份

### 9.3 恢复

设置页备份列表里点「恢复」：

1. 当前库先另存为 `pre-restore-<时间>.db`
2. 用选中的备份替换
3. 重启 Core

只允许恢复 `backups` 目录内的文件。

### 9.4 正文清理

计划书 §8.1 的守卫：**只有「早于 30 天 + 已有总结 + 不在待办里」的邮件**才允许清正文。

- 只清正文字段，标题/发件人/日期/摘要/线程/UID 全部保留（仍可回溯原文）
- 附件不动
- 先点「预览」（只统计不写库），确认后再「执行清理」

---

## 10. 设置项

| 设置 | 说明 |
|---|---|
| 自动同步间隔 | 单位分钟，整数；0 = 不自动同步。**仅对 POP3 生效**（IMAP 走 IDLE） |
| 数据库位置 | 见 9.1 |
| 邮件工作区 | 所有邮件会话所在的目录，可在 DSH 工作区列表里看到 |
| 立即备份 / 恢复 | 见 9.2 / 9.3 |
| 正文清理 | 见 9.4 |
| 改密码 | 密码失效时就地更新，不用删账户（不会丢本地数据） |

---

## 11. 故障排查

### 11.1 顶栏显示 `Mail Core failed` 或插件不出现

- 看设置页的 Core 错误码；`SPAWN_FAILED` 通常是 Python 路径或 DSH 运行时缺失
- 重启 DSH；仍不行就查插件管理器里该插件是否启用

### 11.2 `login failed` / `Authentication failure`

密码或授权码失效。去设置点「改密码」填入当前有效密码（企业邮箱常用客户端授权码）。
密码错时 IDLE 会**停止重试**并显示 `auth-failed`，不会反复尝试。

### 11.3 新邮件不出现

1. 设置页「实时推送（IDLE）」是否有时间；`auth-failed` 说明密码问题
2. POP3 账户看「自动同步间隔」是否为 0
3. 手动点顶栏同步按钮试一次

### 11.4 附件下载失败

- 先确认连接测试通过
- POP3 账户不支持按需取单个附件，会提示 `localOnly`
- 超过 25 MiB 的附件默认拒绝

### 11.5 点「总结」没有出现会话

会退回到「新建会话 + 提示词填进输入框」，按发送即可。
若仍失败，按 F12 看控制台 `[dsh-mail] 打开会话失败` 的具体原因。

### 11.6 切换数据目录后附件读不到

切库会自动重定位附件路径。若手工移动过数据目录，可用维护接口重新定位：

```
POST /api/dsh-mail/admin/rebase   {"from": "旧目录", "to": "新目录"}
```

---

## 12. 升级与卸载

### 升级

```powershell
pwsh -File scripts/upgrade.ps1 -Profile desktop
```

拉取代码 → 更新配置补缺失键 → 提示重启。**重启后 Core 自动迁移 schema 并先备份**。

### 卸载

```powershell
pwsh -File scripts/uninstall.ps1 -Profile desktop          # 保留数据
pwsh -File scripts/uninstall.ps1 -Profile desktop -PurgeData  # 连数据一起删
```

---

## 13. 已知限制

- **没有独立后台服务**：只有 DSH 运行时才同步；关掉 DSH 就停止收信（按计划书 §1.3 的决策）
- **不会自动总结**：摘要需要你点「总结」（自动触发需要宿主提供 Agent 调用接口）
- **知识库实体索引未实现**：只有总结归档，没有跨邮件的实体页
- **POP3**：不能按需取附件、删除只删本地副本
- **PDF 文本提取**只支持文本层，扫描件需要把文件直接传给 Agent 看
- **多账户**：账户栏可切换，但文件夹视图的文件夹发现仍以所选账户为准

---

## 14. 相关文档

- 计划书（冻结）：`docs/PLAN.md`
- 计划对照与偏差：`docs/PLAN_DELTA.md`
- Core 协议：`docs/IPC_PROTOCOL.md`
- 实施计划（待跟进+总结）：`docs/PLAN_TODO_SUMMARY.md`
