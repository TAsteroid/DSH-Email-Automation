# 计划书进度对照与条款变更

> **本文是唯一准绳。** `docs/PLAN.md` 定格为 v1.0 历史文档，不再修改；
> 两者冲突时以本文为准。计划书里没被本文列出的条款仍然有效。

对照对象：`docs/PLAN.md`（v1.0，不再更新）
更新时间：2026-11-07
代码：`mail-core/`（Python）+ `packages/dsh-mail-plugin/`（Cordis 插件），**schema v15**，插件版本 **v0.3.0**，真实账号已接入

---

## 0. 怎么读这份文档

- **第 1 节**：哪个阶段做到哪了（含证据）
- **第 2 节**：计划书里**已经不成立**的条款，以及现在是什么
- **第 3 节**：计划书**没写但已实现**的功能
- **第 4 节**：§19 首期交付清单逐条状态
- **第 5 节**：计划书**没覆盖、需要决策**的空白
- **第 6 节**：建议的后续阶段顺序
- **第 7 节**：发送安全模型

---

## 1. Phase 0–8 状态

| 阶段 | 计划书要求 | 实际状态 | 证据 |
|---|---|---|---|
| Phase 0 插件勘察 | 兼容性文档 + 最小插件 + 三项验收 | ✅ 完成 | `DSH_PLUGIN_COMPATIBILITY.md`；`mail_status`、`main` 面板、`sidebar.panellist` 实测 |
| Phase 1 Mail Core 基础 | 服务可启停、库可迁移、API 认证、任务队列可恢复 | ✅ 主体 | Core 由插件 `ctx.subprocess` 拉起；schema 自动迁移 v1→v15（迁移前自动备份）；**持久化任务队列未做**（同步为同步调用） |
| Phase 2 连接与同步 | IMAP/IDLE/增量、UIDVALIDITY、去重、线程关联 | ✅ 完成 | IMAP + POP3 增量、UIDVALIDITY 重置、幂等去重、多账户隔离、线程关联；**IMAP IDLE 实时推送**；每次同步按服务器 FLAGS 刷新已读状态 |
| Phase 3 解析/缓存/知识库 | MIME 解析、FTS5、30 天缓存、备份 | ✅ 完成 | MIME 解析、FTS5（含中文子串回退）、历史回溯、备份（迁移前自动 + 手动，保留 10 份）、**从备份恢复**、**30 天正文清理（带守卫，schema v12）** |
| Phase 4 AI 工作流 | Agent Adapter、摘要/分类/提取/待办 | ✅ 人工触发闭环 | 通过 DSH 会话实现：待跟进/收件箱/通讯录「在会话中讨论」→ 建会话（按发件人复用）→ 附件作为文件发送 → Agent 总结并按 8 类归档；**自动触发仍受限于宿主接口**（见第 5 节） |
| Phase 5 草稿与审批 | 草稿版本、审批哈希、SMTP 发送、删除/移动 | ✅ 完成 | `draft.*` / `approval.*` / `action.execute`；哈希（含 HTML）、过期、幂等、审计；删除/恢复到回收站、批量移动（IMAP MOVE，回退 COPY+EXPUNGE） |
| Phase 6 插件适配层 | 工具注册、事件、权限、健康检查 | ✅ 完成 | **19 个工具**、**68 条路由**、SSE 事件、Core 健康状态 |
| Phase 7 邮箱 UI | 总览/收件箱/AI/草稿/审批/知识库/任务/设置/审计 | ✅ 主体完成 | 10 个页签全部可用；AI 工作台已并入待跟进；审批改为右侧历史栏（只读）；知识库含总结/文件分类；任务页与独立审计页未做 |
| Phase 8 安装升级发布 | PowerShell 脚本、开机启动、备份恢复 | ✅ 完成 | `install/uninstall/upgrade.ps1`、tgz 打包（`dsh plugin add file:`）、备份恢复、用户手册、中英双语 README、GitHub Releases（v0.1.0→v0.3.0） |

测试现状：Python **236 项**（协议/存储/DPAPI/IMAP/POP3/SMTP/同步/搜索/联系人/模板/草稿/审批/发送/签名/附件/批量操作/迁移/线程/回溯/备份/清理），JS 两套（`verify-host` 假子进程测协议接线与守门、`verify-client` 真实 React 渲染），外加 `scripts/e2e_sync.py` 与 `scripts/e2e_send.py`。
全量 Python 约 97 秒（新库初始化跳过逐步骤 fsync 后从 450 秒降下来）。

---

## 2. 计划书条款变更清单

| 条款 | 原文要求 | 现状 | 原因 |
|---|---|---|---|
| §1.3 / §11.2 | DSH 关闭后继续收信、任务计划程序开机启动 | **放弃**，只在 DSH 运行时同步 | 宿主 timer/subprocess 随 DSH 退出终止 |
| §4.1 | 账户字段含 `credential_reference` | 改为 `password_blob`（DPAPI 密文）+ `protocol` + `signature_html` | 实际加密与扩展不同 |
| §4.2 | IMAP IDLE + 指数退避重连 | IMAP 走 IDLE（阿里邮箱约 5 分钟断连，240 秒主动重发）；POP3 仍轮询 | 按阿里云官方示例实现 |
| §4.4 | AI 自动流水线 | **半自动**：摘要/分类由人工点「总结/在会话中讨论」触发，Agent 调 `mail_summary_save` 归档 | 宿主无插件→Agent 调用接口 |
| §5.1 | 13 个工具 | 实际 **19 个**：新增 `mail_attachments` `mail_attachment_text` `mail_triage_context` `mail_current` `mail_thread` `mail_related` `mail_summary_save` `mail_mark_done` 等 | 会话与总结阶段扩展 |
| §5.3 | 6 个 DSH 事件 | **不实现** | 宿主无插件→Agent 事件通道 |
| §6.1 | 9 个一级页面 | **10 个页签**：待跟进/收件箱/发件箱/草稿箱/已删除/垃圾邮件/模板/通讯录/知识库/设置 | AI 工作台并入待跟进；审批改为右侧栏 |
| §6.2 | 三栏阅读 | 三栏 + 对话气泡；**详情可切 HTML / 纯文本** | 用户评审后的实际形态 |
| §7.1 | FastAPI + SQLAlchemy + Alembic | **纯标准库**：`sqlite3` 手写迁移 + stdio JSON-RPC | 决策 D6 |
| §7.2 | 18 张表 | schema v15 实际 15 张：`meta` `accounts` `folders` `messages` `sync_state` `contacts` `templates` `drafts` `approvals` `mail_actions` `contact_sessions` `summaries` `attachments` `signature_images` `messages_fts` | 按阶段演进 |
| §8 | 30 天缓存 + 历史回溯 | 回溯 ✅；**清理已实现**（v12 `admin.cleanup`）：只清「早于 N 天 + 已有摘要 + 不在待办」正文，保留回溯定位字段 | 守卫条件已具备 |
| §9.1 | Agent Adapter 抽象 | 由「DSH 会话 + 工具」替代，无自动调用 | 见第 5 节 |
| §10.3 | 本机 API 127.0.0.1 + 访问令牌 | 插件 HTTP 路由（同源守门）+ 到 Core 的 stdio 管道 | 决策 D5 |
| §12 | REST 风格 `/api/v1/*` | Core 用 RPC 方法名；插件对外 `/api/dsh-mail/*` | 实际实现 |
| §14 | 技术栈表 | 去掉 FastAPI/SQLAlchemy/Alembic | 同上 |
| §16 | 阶段顺序 | Phase 2 先于 3；期间插入大量计划外功能 | 见第 3 节 |
| §17 | 测试策略 | 236 Python + JS 双套；**未做故障注入与提示词注入测试** | 按需补 |
| §19 | 首期交付清单 | 见第 4 节 | — |

---

## 3. 计划书未覆盖但已实现

| 功能 | 位置 |
|---|---|
| POP3 收信（ssl/starttls/none） | `mail_core/pop3_client.py` |
| SMTP 连通性测试（EHLO，不发信） | `mail_core/smtp_client.py` |
| IMAP 文件夹名 modified UTF-7 编解码 | `mail_core/imap_utf7.py` |
| 附件元数据、按需下载、sha256、删除本地副本 | `attachments` 表 + `attachment.*` RPC |
| 附件正文提取（PDF/xlsx/docx/txt，纯标准库） | `mail_core/attachment_text.py` |
| 数据库位置可配置（含 WAL、附件、备份迁移） | 设置页「数据库位置」+ `admin.rebasePaths` |
| 备份恢复（列表恢复 + 指定文件恢复） | `/backups/restore` + 设置页 |
| 30 天正文清理（守卫） | `admin.cleanup` + 设置页 |
| 邮件专用工作区 + 联系人会话复用 | `contact_sessions` 表 + `sessions.create/bind` |
| Agent 上下文工具 | `mail_current` / `mail_thread` / `mail_related` / `mail_triage_context` |
| 邮件模板、通讯录 | schema v4 + UI |
| 全文搜索（FTS5 + 中文子串回退） | `store.search_messages` |
| 未读状态（服务器 FLAGS 同步 + 打开即已读） | `seen` 列 + `message.markRead/markSeen` |
| 多选批量：已读/未读、移动、删除、全选 | 各列表标题行 + `message.markSeen/move` |
| 移动邮件（IMAP MOVE，回退 COPY+EXPUNGE） | `message.move` |
| 审批历史栏（状态角标、只读、复制发送指令） | `ApprovalsRail` |
| 每账户 HTML 签名（CID 内嵌图片，Outlook 可见） | `accounts.signature_html` + `signature_images` |
| 富文本撰写（格式工具栏、插入签名、HTML 源码、存为草稿、内联链接面板） | `ComposeModal` + schema v15 `drafts.body_html` |
| HTML 邮件阅读（sandbox iframe、深色适配、纯文本回退） | `MessageHtml` / `ThreadBubbles` |
| 签名位置：新邮件末尾、回复/转发在引用之前 | 客户端 `withSignature` + `sender._compose_bodies` |
| 列宽拖动与记忆、Agent 区高度拖动 | `localStorage: dsh-mail:layouts` |
| 本机 HTTP 同源守门 | `rejectReason()` |

---

## 4. 首期交付清单（§19）状态

| # | 条目 | 状态 |
|---|---|---|
| 1 | DSH 插件兼容性调查文档 | ✅ |
| 2 | Mail Core Windows 后台服务 | ⚠️ 子进程形式，非独立服务（按 §1.3 决策） |
| 3 | 多邮箱账户配置和凭据保护 | ✅ DPAPI；账户栏可切换账户 |
| 4 | IMAP IDLE 与增量同步 | ✅ IDLE + 增量（POP3 轮询） |
| 5 | 邮件解析、去重和线程关联 | ✅ 线程关联（引用链归并，跨账户不串） |
| 6 | SQLite 知识库与 FTS5 搜索 | ⚠️ FTS5 ✅；知识库有总结归档与文件分类，跨邮件实体索引未做 |
| 7 | AI 摘要、分类和结构化信息提取 | ✅ 人工触发，Agent 按 8 类归档 |
| 8 | 自动生成回复草稿 | ⚠️ 可让 Agent 起草（会话/工具），无自动生成 |
| 9 | 人工审批与受控 SMTP 执行 | ✅ 必走宿主审批弹窗；HTTP 无批准/执行入口 |
| 10 | DSH Agent 工具注册 | ✅ 19 个 |
| 11 | DSH 邮箱管理 UI | ✅ 全部页签可用 |
| 12 | 30 天缓存和历史邮件回溯 | ✅ 回溯 + 清理（带守卫） |
| 13 | 日志、审计、备份和恢复 | ✅ 备份 + 恢复 + 审批审计字段；Core 日志仍写 stderr |
| 14 | Windows 安装、启动、停止、升级脚本 | ✅ 安装/卸载/升级脚本 + tgz 安装 |
| 15 | 完整部署、使用和故障排查文档 | ✅ `docs/USER_GUIDE.md` + 双语 README |
| 16 | 端到端验收测试报告 | ⚠️ 有 `e2e_sync.py` / `e2e_send.py` 与测试套件，无正式报告 |

---

## 5. 计划书需要新增或决策的空白

1. **自动摘要**（§9.1 的 Agent Adapter）。宿主仍没有「插件发起一次 Agent 调用」的公开接口，所以新邮件不会自动总结。两条现实路径：继续人工点「总结」，或由 Core 直接调模型 API（绕过 DSH Agent，需要单独授权与计费口径）。**待决策。**
2. **草稿继续编辑**。草稿可保存（含 HTML），但草稿箱列表暂不支持点开回到富文本编辑器；需要把草稿内容回填到 `ComposeModal`。
3. **知识库实体索引**（§7）。现在只有「总结 + 按文件类型分类的附件」，没有公司/项目/人物等跨邮件实体页。
4. **独立任务页与审计页**（§6.1）。审批历史已在右侧栏，但完整的操作审计与定时任务页面未做。
5. **多账户收件箱**。账户栏可切换，但文件夹发现/同步仍以所选账户为准，未做「全部账户合并视图」。
6. **受控操作范围**。删除/移动目前不需要审批（仅发送需要）；计划书 §10 是否要求其它操作也走审批，未决策。
7. **提示词注入与故障注入测试**（§17）。邮件正文是不可信输入，目前没有针对性的注入测试。

---

## 6. 建议的后续阶段顺序

1. **草稿回填编辑**（小而直接，补齐富文本闭环）
2. **知识库实体索引**（公司/项目/联系人聚合页）
3. **自动摘要**（取决于第 5 节第 1 条的决策）
4. **提示词注入测试**（安全相关，成本低）
5. 独立审计页 / 任务页（按需）

---

## 7. 发送安全模型（2026-11-04 制定，2026-11-07 更新）

**问题**：早期实现里 `/approvals/approve` 和 `/actions/execute` 都是本机 HTTP 路由，Agent 用命令行就能自己批准自己起草的邮件，门禁形同虚设。

**现行规则**：

1. 唯一发信入口是 Agent 工具 `mail_send_draft`（不是 HTTP）。
2. 工具内部先调 `ctx.approval.request` → **DSH 界面弹审批框**；只有真人点「允许」拿到 `allowed-once` 才继续，其余结局一律不发。
3. 拿到允许后再走 Core 的 `approval.approve` + `action.execute`，内容哈希（含 HTML）、过期、幂等键三道校验照旧。
4. HTTP 侧无批准/执行入口：`/approvals/approve` 已删除（404），`/actions/execute` 固定返回 `403 FORBIDDEN`。
5. 右侧审批栏是**只读历史**：不再展示待审批项、不再提供「拒绝」按钮、不显示待审批数量气泡；选中条目可「复制发送指令」粘到对话里让 Agent 发起。待审批由 DSH 弹窗处理，过期后自动落到历史并显示「已过期」角标。

**依赖条件**：审批策略是**会话级**的。当前会话必须是 `ask` 才会弹框；`never` 时审批被自动拒绝，工具会直接拒绝发送并提示切回 `ask`。

**审计（schema v6）**：`approvals` 有 `decided_via`（`prompt` / `policy-never` / `api` / 空）与 `decided_by`（`user` / `agent` / 空）。弹窗批准写 `prompt` + `user`；审批历史显示状态角标（已批准/已拒绝/已过期）与批准方式。
