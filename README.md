# DSH 邮箱自动化插件

[English](README.en.md) | 中文

把邮箱接进 DeepSeek Harness：收信实时推送、邮件自动进待办、把邮件和附件交给 Agent 总结归档、发信必须人工审批。

插件由两部分组成：一个 DSH 宿主插件（`packages/dsh-mail-plugin/`）+ 一个纯标准库的 Python Core（`mail-core/`，由插件自动拉起，无需单独运行）。数据全部落本地 SQLite，邮箱密码用 Windows DPAPI 加密。

当前版本：**v0.3.0**（数据库 schema v15）

## 能做什么

| 能力 | 说明 |
|---|---|
| 收信 | IMAP 账号用 **IDLE 实时推送**（约 30 秒内到达）；POP3 账号按间隔轮询 |
| 未读与批量 | 收件箱区分未读（加粗 + 圆点）；全页签多选：批量已读/未读、移动、删除、全选 |
| 阅读 | 邮件详情支持 **HTML / 纯文本** 切换，HTML 在 sandbox iframe 中渲染并适配深色主题 |
| 撰写 | **富文本编辑器**：加粗/斜体/列表/链接/清除格式、插入签名、HTML 源码切换、存为草稿 |
| 待跟进 | 新邮件自动进待办，直接显示正文；正文下可开 Agent 对话框；附件、总结、已处理一站式处理 |
| Agent 总结 | 一键建会话，把邮件（含附件文件）交给 Agent，按 8 类归档到本地库 |
| 发信审批 | 必须你在 DSH 界面点「允许」；Agent 无法自我批准，插件也不提供 HTTP 批准接口 |
| 签名 | 每个账户单独设置，支持 HTML 与**内嵌图片**（CID，Outlook 可见）；新邮件附末尾，回复/转发附在引用之前 |
| 附件 | 只存元数据，你确认后才下载；下载后可**作为文件原样传给 Agent** |
| 检索 | FTS5 全文搜索（含中文子串回退）、线程归并、历史回溯 |
| 知识库 | 总结 / 文档 / 表格 / 图片 / 日程 / 压缩文件 / 视频 / 其他；可搜索、折叠、删除 |
| 数据管理 | 备份 / 从备份恢复 / 切换数据目录 / 30 天正文清理（有守卫） |

## 快速开始

### 从 Release 安装（推荐）

```powershell
# 下载 https://github.com/TAsteroid/DSH-Email-Automation/releases/latest 里的 tgz
dsh plugin --profile desktop add file:<下载位置>/dsh-mail-plugin-0.3.0.tgz
```

`dsh` 是 DSH 自带 CLI，会自动把包装进 profile 并在 `dependencies` 与 `dsh.profile.bundles` 登记。

### 从源码打包安装

```powershell
node scripts/build-package.mjs     # 产出 dist\dsh-mail-plugin-<version>.tgz
dsh plugin --profile desktop add file:./dist/dsh-mail-plugin-0.3.0.tgz
```

> 换机器时把 `.tgz` 拷到目标机器，把上面的 `./dist/...` 换成它的实际路径即可。

### 从源码目录安装

```powershell
pwsh -File scripts/install.ps1 -Profile desktop
```

脚本建立目录联接、写入 profile 配置、创建数据目录；重启 DSH 生效。

### 首次配置

设置页 → 添加账户：填 IMAP/SMTP 主机端口、邮箱地址、密码（企业邮箱通常要填**客户端授权码**），点「测试连接」确认 IMAP 文件夹数与 `smtp ok`。

## 工作方式

```
DSH 宿主插件（Node）
 ├── 19 个 Agent 工具（只读查询 / 起草 / 提交审批 / 发送 / 总结）
 ├── 68 条本机 HTTP 路由（界面唯一数据通道，同源守门）
 ├── SSE 实时事件推送
 └── 拉起并托管 Python Core（stdio JSON-RPC）
        └── SQLite（WAL）· DPAPI 凭据 · IMAP/POP3/SMTP · FTS5 · 附件 · 签名图片
```

**安全模型**：发信只能由 Agent 工具 `mail_send_draft` 发起，且必须经过宿主审批弹窗（真人点允许）；HTTP 侧没有批准/执行入口。审批记录带内容哈希、过期时间、幂等键和批准来源审计字段。

## 项目状态

| 阶段 | 状态 |
|---|---|
| 插件勘察 / Core / 同步 | ✅ |
| 解析 · 缓存 · 备份 · 线程 · 回溯 | ✅ |
| 草稿 · 审批 · 受控发送 · HTML 签名 | ✅ |
| 待跟进 · 总结归档 · 附件 · 实时推送 | ✅ |
| 多选批量 · 未读状态 · 知识库分类 | ✅ |
| 富文本撰写 · HTML 阅读 · 存为草稿 | ✅ |
| 安装 / 升级 / 文档 / Release | ✅ |
| 自动总结（无需点击） | ⏳ 需要宿主提供 Agent 调用接口 |
| 知识库跨邮件实体索引 | ⏳ 未开始 |

数据库 schema v15；Python 测试 244 项，另有 JS 双套（宿主假子进程 + 真实 React 渲染）。

## 文档

| 文档 | 内容 |
|---|---|
| [用户手册](docs/USER_GUIDE.md) | 安装、配账号、界面导览、故障排查、升级卸载 |
| [计划对照与偏差](docs/PLAN_DELTA.md) | 实际实现与原计划的差异（唯一准绳） |
| [Core 协议](docs/IPC_PROTOCOL.md) | stdio JSON-RPC 方法、字段、错误码、迁移策略 |
| [待跟进与总结方案](docs/PLAN_TODO_SUMMARY.md) | 待办登记、总结流程、分类 |
| [脚本说明](scripts/README.md) | 安装/卸载/升级脚本参数与打包 |

原始需求计划书为本地私有文档（`docs/PLAN.md`，未入库），实际状态以 `docs/PLAN_DELTA.md` 为准。

## 已知限制

- 只有 DSH 运行时才同步，关掉 DSH 即停止收信（不使用独立后台服务）
- 摘要需要手动点「总结」：自动触发需要宿主开放 Agent 调用能力
- 纯文本邮件客户端看不到 HTML 签名与格式
- HTML 阅读为深色可读性会抹平邮件里的彩色块背景，品牌色文字变浅灰
- 富文本基于 `contenteditable`，复杂 HTML 粘贴后的清理能力有限
- 草稿箱暂不支持点开继续编辑（草稿会保存，重新编辑需从撰写窗口）
- 移动邮件会删掉本地记录，目标文件夹同步后重新入库（该邮件的总结/待办状态会丢失）
- PDF 文本提取只覆盖文本层，扫描件请把文件直接交给 Agent
- POP3 不支持按需取附件，删除只删本地副本

## 开发

```powershell
# Python Core 测试
python -m unittest discover -s mail-core/tests -t mail-core

# 宿主/客户端静态验证
node scripts/verify-host.mjs
node scripts/verify-client.mjs

# 打包
node scripts/build-package.mjs
```
