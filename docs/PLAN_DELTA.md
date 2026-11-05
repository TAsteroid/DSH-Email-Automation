# 计划书进度对照与条款变更

> **本文是唯一准绳。** `DSH_Email_Automation.md` 定格为 v1.0 历史文档，不再修改；
> 两者冲突时以本文为准。计划书里没被本文列出的条款仍然有效。

对照对象：`DSH_Email_Automation.md`（v1.0，不再更新）
更新时间：2026-11-04
代码：`mail-core/`（Python）+ `dsh-mail-plugin/`（Cordis 插件），schema v7，真实账号已接入

---

## 0. 怎么读这份文档

- **第 1 节**：哪个阶段做到哪了（含证据）
- **第 2 节**：计划书里**已经不成立**的条款，以及现在是什么
- **第 3 节**：计划书**没写但已实现**的功能
- **第 4 节**：§19 首期交付清单逐条状态
- **第 5 节**：计划书**没覆盖、需要决策**的空白
- **第 6 节**：建议的后续阶段顺序

---

## 1. Phase 0–8 状态

| 阶段 | 计划书要求 | 实际状态 | 证据 |
|---|---|---|---|
| Phase 0 插件勘察 | 兼容性文档 + 最小插件 + 三项验收 | ✅ 完成 | `DSH_PLUGIN_COMPATIBILITY.md`；`mail_status` 工具、`main` 面板、`sidebar.panellist` 均实测 |
| Phase 1 Mail Core 基础 | 服务可启停、库可迁移、API 认证、任务队列可恢复 | ⚠️ 大部分 | Core 由插件 `ctx.subprocess` 拉起（随 DSH 启停）；schema v1→v4 迁移；**持久化任务队列未做**（同步是同步调用） |
| Phase 2 连接与同步 | IMAP/IDLE/增量、UIDVALIDITY、去重、线程关联 | ⚠️ 大部分 | IMAP + POP3 增量同步、UIDVALIDITY 重置、幂等去重、多账户隔离、**线程关联（v7）** ✅；**IDLE 未做**（轮询） | ⚠️ 大部分 | IMAP + POP3 增量同步、UIDVALIDITY 重置、幂等去重、多账户数据隔离；**IDLE 未做**（5 分钟轮询）；**线程关联未做** |
| Phase 3 解析/缓存/知识库 | MIME 解析、FTS5、30 天缓存、备份 | ✅ 主体完成 | MIME 解析、FTS5（含中文子串回退）✅；**历史回溯** ✅（按日期向前拉取）；**备份** ✅（迁移前自动 + 手动，保留 10 份）；**30 天清理被 Phase 4 阻塞**（见第 5 节） | ⚠️ 部分 | MIME 解析、FTS5（+ 中文子串回退）✅；**30 天缓存/历史回溯未做**；**备份未做** |
| Phase 4 AI 工作流 | Agent Adapter、摘要/分类/提取/待办 | ❌ 未做 | 无 Agent 调用通道（见第 5 节） |
| Phase 5 草稿与审批 | 草稿版本、审批哈希、SMTP 发送、删除/移动 | ✅ 发送闭环完成 | `draft.*` / `approval.*` / `action.execute`；哈希绑定、过期、幂等、审计；`scripts/e2e_send.py` 全链路实测；删除/移动等受控操作未做 |
| Phase 6 插件适配层 | 工具注册、事件、权限、健康检查 | ✅ 完成 | 11 个工具、33 条路由、Core 健康状态 |
| Phase 7 邮箱 UI | 总览/收件箱/AI/草稿/审批/知识库/任务/设置/审计 | ⚠️ 部分 | 收件箱、发件箱、草稿、已删除、垃圾、历史、模板、通讯录、搜索、设置已可用；AI 工作台、审批中心、知识库是骨架；自动化任务页、审计页未做 |
| Phase 8 安装升级发布 | PowerShell 脚本、开机启动、备份恢复 | ❌ 未做 | 插件经 Plugin Manager 安装；无安装/升级/备份脚本 |

测试现状：Python **153 项**（协议/存储/DPAPI/IMAP/POP3/SMTP/同步/搜索/联系人/模板/草稿/审批/发送/迁移/线程/回溯/备份），JS 两套（`verify-host` 假子进程测协议接线与守门、`verify-client` 真实 React 渲染），外加两个全链路脚本 `scripts/e2e_sync.py`（假 IMAP 同步）与 `scripts/e2e_send.py`（草稿→审批→真发信→幂等）。

---

## 2. 计划书条款变更清单

| 条款 | 原文要求 | 现状 | 原因 |
|---|---|---|---|
| §1.3 / §11.2 | DSH 关闭后继续收信、用任务计划程序开机启动 | **放弃**，只在 DSH 运行时同步，启动补拉 | 宿主 timer/subprocess 随 DSH 退出终止 |
| §4.1 | 账户字段含 `credential_reference` | 改为 `password_blob`（DPAPI 密文）+ `protocol` | 实际加密方式不同 |
| §4.2 | IMAP IDLE + 指数退避重连 | **只做轮询**（默认 5 分钟） | IDLE 未实现 |
| §4.4 | AI 自动流水线 | **未实现** | 无 Agent 调用通道 |
| §5.1 | 13 个工具 | 实际 11 个：`mail_status` `mail_accounts` `mail_folders` `mail_sync` `mail_messages` `mail_message` `mail_search` `mail_contacts` `mail_draft_save` `mail_request_approval` `mail_send_draft` | 其余依赖 AI 阶段 |
| §5.3 | 6 个 DSH 事件 | **不实现** | 宿主无插件→Agent 事件通道 |
| §6.1 | 9 个一级页面 | **10 个页签**：待跟进（含 AI 卡片工作台）/收件箱/发件箱/草稿箱/已删除/垃圾邮件/模板/通讯录/知识库/设置 | 用户评审后的实际形态；AI 工作台已并入待跟进 |
| §6.2 | 三栏阅读 | 三栏 + **对话气泡**（最新在上，历史引用折叠） | 用户评审后的实际形态 |
| §7.1 | FastAPI + SQLAlchemy + Alembic | **纯标准库**：`sqlite3` 手写迁移 + stdio JSON-RPC | 决策 D6；避免第三方依赖 |
| §7.2 | 18 张表 | 实际 schema v7：`meta` `accounts` `folders` `messages` `sync_state` `contacts` `templates` `drafts` `approvals` `mail_actions` `messages_fts` | 按阶段演进 |
| §8 | 30 天缓存 + 历史回溯 | 回溯 ✅（`history.backfill`）；清理暂缓 | §8.1 要求摘要先入库，依赖 Phase 4 |
| §9.1 | Agent Adapter 抽象 | **未实现** | 见第 5 节 |
| §10.3 | 本机 API 127.0.0.1 + 访问令牌 | 插件 HTTP 路由（同源守门）+ 到 Core 的 **stdio 管道**（无令牌） | 决策 D5：父子进程管道天然可信 |
| §12 | REST 风格 `/api/v1/*` | Core 用 RPC 方法名（`message.list` 等）；插件对外是 `/api/dsh-mail/*` | 实际实现 |
| §14 | 技术栈表 | 去掉 FastAPI/SQLAlchemy/Alembic | 同上 |
| §16 | 阶段顺序 | Phase 2 先于 3 完成；期间插入了计划外功能 | 见第 3 节 |
| §17 | 测试策略 | 实际 153 + JS；**未做故障注入与提示词注入测试** | 待 Phase 4 |
| §19 | 首期交付清单 | 见第 4 节 | — |

---

## 3. 计划书未覆盖但已实现

| 功能 | 位置 |
|---|---|
| POP3 收信（ssl/starttls/none） | `mail_core/pop3_client.py` |
| SMTP 连通性测试（EHLO，不发信） | `mail_core/smtp_client.py` |
| IMAP 文件夹名 modified UTF-7 编解码 | `mail_core/imap_utf7.py` |
| `folder.list` RPC（本地文件夹缓存） | `service.py` |
| 通讯录（增删改查 + Agent 提示词生成） | schema v4 + UI |
| 附件元数据与按需下载（schema v8） | `attachments` 表 + `attachment.list/fetch/backfill`；待跟进里以事项列出，用户确认后才下载 |
| 数据库位置可配置 | 设置页「数据库位置」：选目录后复制 mail.db/attachments/backups 并重启 Core |
| 邮件专用工作区 + 联系人会话复用（schema v10） | `contact_sessions` 表；点「总结」时才建会话，同发件人复用；工作区可在设置里选 |
| Agent 上下文工具（`mail_current` / `mail_thread` / `mail_related` / `mail_attachments`） | 让 Agent 知道「用户在看哪封」，并自行检索相关材料与附件 |
| 邮件模板（本地保存，不同步） | schema v4 + UI |
| 全文搜索（FTS5 + 中文子串回退） | `store.search_messages` + 顶栏搜索框 |
| 文件夹页签（发件箱/草稿/已删除/垃圾/历史） | `MailFolderView` |
| 对话气泡与引用拆分 | `client.js: splitQuoted` |
| 回复/回复全部/转发/发送编辑器（预填） | `ComposeModal` |
| 列宽拖动与记忆、Agent 区高度拖动 | `localStorage: dsh-mail:layouts` |
| 本机 HTTP 同源守门 | `rejectReason()` |

---

## 4. 首期交付清单（§19）状态

| # | 条目 | 状态 |
|---|---|---|
| 1 | DSH 插件兼容性调查文档 | ✅ |
| 2 | Mail Core Windows 后台服务 | ⚠️ 子进程形式，非独立服务 |
| 3 | 多邮箱账户配置和凭据保护 | ✅ DPAPI；UI 目前只用第一个账户 |
| 4 | IMAP IDLE 与增量同步 | ⚠️ 仅增量 + 轮询 |
| 5 | 邮件解析、去重和线程关联 | ✅ 线程关联 v7（引用链归并，跨账户不串） |
| 6 | SQLite 知识库与 FTS5 搜索 | ⚠️ FTS5 ✅，知识库（实体）❌ |
| 7 | AI 摘要、分类和结构化信息提取 | ❌ |
| 8 | 自动生成回复草稿 | ❌ |
| 9 | 人工审批与受控 SMTP 执行 | ✅ 发送必走宿主审批弹窗（真人点允许）；HTTP 无批准/执行入口 |
| 10 | DSH Agent 工具注册 | ✅ |
| 11 | DSH 邮箱管理 UI | ✅ 主体可用 |
| 12 | 30 天缓存和历史邮件回溯 | ⚠️ 回溯 ✅；清理被 Phase 4 阻塞 |
| 13 | 日志、审计、备份和恢复 | ⚠️ 备份 ✅（迁移前自动 + 手动）；恢复流程与日志未做 |
| 14 | Windows 安装、启动、停止、升级脚本 | ❌ |
| 15 | 完整部署、使用和故障排查文档 | ⚠️ 有阶段文档，无用户手册 |
| 16 | 端到端验收测试报告 | ⚠️ 有 e2e 脚本，无正式报告 |

---

## 5. 计划书需要新增或决策的空白

1. **AI 怎么接**（§9.1 的 Agent Adapter）。宿主 `Agent` 对外契约只有 `{ id }`，没有「向运行中的 Agent 注入消息」或「由插件发起一次 Agent 调用」的公开接口。当前只有两条现实路径：
   - 提示词复制（已实现，通讯录与邮件详情各一处）
   - 给 Agent 提供工具，由用户在对话里驱动（部分实现）

   若要真自动化（新邮件 → 自动摘要），需要 DSH 提供调用接口，或由 Core 直接调模型 API（等于绕过 DSH Agent，要单独授权与计费口径）。
2. **发送与审批**（Phase 5）已闭环，安全模型见第 7 节。剩余：删除/移动等其它受控操作尚未走审批。
3. **30 天缓存与回溯**（§8）。历史回溯已实现（`history.backfill`，按日期向前拉取）；**自动清理原文暂缓** —— §8.1 要求「摘要/分类已写入知识库」才允许删本地原文，而那依赖 Phase 4 的 AI 能力。现在删原文会违反计划书，故保持全量保留。
4. **IDLE**（§4.2）。当前 5 分钟轮询；是否值得做取决于能否接受最多 5 分钟延迟。
5. **多账户 UI**（§1.3）。数据层支持多账户，界面目前固定用第一个。
6. **自动同步范围**。INBOX / 已发送 / 草稿 / 已删除 / 垃圾邮件五类已纳入心跳与顶栏同步；其余自定义文件夹仍需手动同步。
7. **Agent 对话框形态**。用户已确认最终要改成与 DSH 主对话框一致的形态（输入框 + 工具行 + 发送），当前是提示词复制式的简化版，等宿主提供注入接口后再改。

---

## 6. 建议的后续阶段顺序

1. **Phase 3 收尾**：✅ 已完成（线程关联、历史回溯、备份）；30 天清理待 Phase 4
2. **Phase 4 AI**：取决于第 5 节第 1 条的决策
3. **Phase 8 安装升级**：PowerShell 脚本、开机启动、用户手册
4. IDLE、多账户 UI、其它受控操作（删除/移动）按需补

---

## 7. 发送安全模型（2026-11-04 修订）

**问题**：早期实现里 `/approvals/approve` 和 `/actions/execute` 都是本机 HTTP 路由。审批记录虽在，但 Agent 用命令行就能自己批准自己起草的邮件——门禁形同虚设。当时确实发生过一次：测试邮件由 Agent 调接口「自我批准」后发出。

**现行规则**：

1. 唯一发信入口是 Agent 工具 `mail_send_draft`（不是 HTTP）。
2. 工具内部先调 `ctx.approval.request` → **DSH 界面弹审批框**；只有真人点「允许」拿到 `allowed-once` 才继续，其余结局（rejected / cancelled / unavailable）一律不发。
3. 拿到允许后再走 Core 的 `approval.approve` + `action.execute`，内容哈希、过期、幂等键三道校验照旧。
4. HTTP 侧无批准/执行入口：`/approvals/approve` 已删除（404），`/actions/execute` 固定返回 `403 FORBIDDEN`。
5. 界面审批栏对挂起项只提供「复制发送指令」（粘到对话里让 Agent 发起）和「拒绝」。

**依赖条件**：宿主 `include:approval` 的 `policy` 必须是 `ask`。若被设成 `never`，审批会被自动放行、不弹框——此时该门禁失效，需在 DSH 设置里改回 `ask`。

**审计（schema v6，已完成）**：`approvals` 增加 `decided_via`（`prompt` / `policy-never` / `api` / 空）与 `decided_by`（`user` / `agent` / 空）。走弹窗批准时写 `prompt` + `user`；界面审批历史直接显示「用户批准 / 策略放行 / Agent 批准」。迁移不回填历史记录（老记录保持空串，不猜测来源）。

**附带收紧**：策略为 `never` 时工具直接拒绝发送（不再静默放行），因为那种情况下宿主根本不弹框，等于没有人工确认。
