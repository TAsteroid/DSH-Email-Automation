# Phase 2 状态：邮件同步与本地索引

- 完成时间：2026-11-04
- 目标：把服务器上的真实邮件同步进本地 SQLite，并让界面显示列表与正文
- 结论：**通过**（用本地假 IMAP 服务器做了全链路实测；真实邮箱仍未验证）

## 交付物

```
mail-core/mail_core/
  sync.py           IMAP 增量同步（SELECT → UIDVALIDITY → UID SEARCH/FETCH）、POP3 UIDL 同步
  mime_parse.py     RFC 2047 头解码、日期转 UTC、text/plain 优先、HTML 去标签、snippet、大正文截断
  store.py          schema v3：messages 表 + 索引 + CRUD + 计数
  service.py        sync.run / message.list / message.get，sync.status 改为真实计数
mail-core/tests/
  fake_servers.py   FakeImapServer、扩展的 FakePop3Server（UIDL/RETR/LIST）
  test_sync.py      同步、幂等、UIDVALIDITY 重置、正文读取、计数
packages/dsh-mail-plugin/lib/
  index.js          新增 mail_sync / mail_messages / mail_message 工具；/sync /messages /message 路由；心跳兜底同步
  client.js         收件箱中间栏渲染邮件列表，右栏渲染正文，列表头加「立即同步」按钮
scripts/
  e2e_sync.py       全链路脚本：假 IMAP → Core → SQLite → HTTP → 断言
```

## 全链路实测结果

`python scripts/e2e_sync.py <gui-origin>`（假 IMAP 服务器 + 真实运行中的插件）：

| 步骤 | 结果 |
|---|---|
| 添加账号（127.0.0.1:随机端口，不加密） | 200 |
| 首次同步 | `fetched=3, stored=3` |
| **重复同步** | `stored=0`（幂等） |
| 列表 | `total=3`，3 条，按时间倒序 |
| 读正文 | 主题 `imap message 3`，正文 `body of imap message 3` |
| 未知 id | 404 `NOT_FOUND` |
| 清理 | 账号删除，`accounts/messages/folders/sync_state` 全部归零 |

Python 单元测试 **45 项**（原 37），JS 两套全过（含新增的邮件列表/正文渲染断言）。

## 行为说明

- **同步粒度**：一个账号一个文件夹一次调用，默认 `INBOX`、默认拉 50 封（上限 200）。
- **幂等**：靠 `UNIQUE (account_id, folder, uidvalidity, uid)` + `INSERT OR IGNORE`；`stored` 只统计真正新增。
- **UIDVALIDITY 变化**：删除该文件夹旧记录、游标清零、重新拉取，不报错。
- **自动同步**：插件心跳每 `syncIntervalMs`（默认 5 分钟）对所有启用账号同步一次 INBOX；界面「立即同步」按钮手动触发。
- **错误处理**：网络错误返回 `IMAP_ERROR`，已入库邮件不回滚，错误写进 `sync_state`。

## 已知限制

1. **没有 IDLE**：只靠 5 分钟轮询兜底，新邮件最多延迟 5 分钟。
2. **没有真实邮箱验证**：所有验证都基于本地假 IMAP 服务器（明文连接）。SSL/STARTTLS 的真实服务器行为仍未实测。
3. **POP3 uid 取 UIDL 的数字段**：极端碰撞未处理；POP3 的 `uidvalidity` 固定 0。
4. **大邮件**：正文超过 1 MB 时入库截断到 512 KB 并标记；但单封仍会整封读入内存再截断，超大附件场景要注意。
5. **只同步 INBOX**：其他文件夹还没有入口。
6. **附件**：未解析、未存储（计划书 Phase 3 范围）。

## 下一步（Phase 3）

- FTS5 全文索引 + `mail.search`
- 30 天原文缓存与清理策略、历史回溯
- 附件元数据（哈希、大小、缓存状态）
- 知识库实体抽取（计划书 §7.4）
