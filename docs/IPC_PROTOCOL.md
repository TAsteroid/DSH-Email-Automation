# 插件 ↔ Mail Core IPC 协议 v1

- 双方：`dsh-mail-plugin`（Node，父进程） ↔ `mail-core`（Python，子进程）
- 传输：**stdio**，UTF-8，**一行一个 JSON**（`\n` 分隔）
- 方向：父进程写子进程 stdin 发请求；子进程写 stdout 回响应；**stderr 只放日志，不参与协议**
- 决策依据：`DSH_PLUGIN_COMPATIBILITY.md` §5.1（D5 选 stdio，D6 选纯标准库）

## 信封

请求：

```json
{"id": "7", "method": "account.list", "params": {}}
```

成功响应：

```json
{"id": "7", "ok": true, "result": {"accounts": []}}
```

失败响应：

```json
{"id": "7", "ok": false, "error": {"code": "NOT_FOUND", "message": "account not found"}}
```

约定：

- `id` 由父进程生成，字符串，单调递增；响应必须原样带回。
- 一个请求对应且只对应一条响应；响应顺序不保证与请求顺序一致（v1 实现是串行的，但协议不假设）。
- 子进程**不得**在 stdout 输出任何非协议内容（`print()` 一律写 stderr）。
- 未知字段忽略；缺少必填字段返回 `BAD_REQUEST`。

## 错误码

| code | 含义 |
|---|---|
| `BAD_REQUEST` | 参数缺失 / 类型错误 / JSON 解析失败 |
| `UNKNOWN_METHOD` | 方法名不存在 |
| `NOT_FOUND` | 账号或资源不存在 |
| `CREDENTIAL_MISSING` | 该账号没有可用密码（未录入或 DPAPI 解密失败） |
| `IMAP_ERROR` | IMAP 连接或命令失败（message 带服务器原文） |
| `DPAPI_ERROR` | Windows DPAPI 调用失败 |
| `SMTP_ERROR` | 发信失败（未配置 SMTP、认证失败、服务器拒绝等；message 不含密码） |
| `APPROVAL_NOT_FOUND` | 审批记录不存在 |
| `APPROVAL_NOT_APPROVED` | 审批存在但不是 `approved`（含 pending / rejected / expired 状态） |
| `APPROVAL_EXPIRED` | 审批已过期（到达 `expiresAt` 即视为过期） |
| `HASH_MISMATCH` | 审批后草稿内容被修改，当前 `contentHash` 与审批绑定的不一致 |
| `ALREADY_EXECUTED` | 幂等键已有执行记录，本次绝不重发 |
| `INTERNAL` | 未预期异常（message 带异常类型，不打印堆栈到 stdout） |

## 方法

### `core.handshake`

参数：`{}`

结果：

```json
{"protocol": 1, "version": "0.1.0", "dbPath": "...", "dataDir": "...", "schemaVersion": 7}
```

父进程启动后第一条请求必须是它；`protocol` 不等于 1 时插件应拒绝工作并在界面上报错。

`schemaVersion` 是本地 SQLite schema 版本：v1 账号 / v2 收信协议 / v3 邮件索引 / v4 通讯录、模板与全文检索 / v5 草稿、审批与发送记录 / v6 审批审计字段 / v7 邮件线程。**它和 `protocol` 无关**：v7 只新增列、索引和线程字段，协议版本仍是 1，旧插件遇到新 `schemaVersion` 不需要特殊处理（不认识的字段忽略即可）。

### `account.list`

参数：`{}`

结果：

```json
{"accounts": [{
  "id": "acc_xxx", "address": "a@b.com", "displayName": "工作邮箱",
  "protocol": "imap",
  "imapHost": "imap.b.com", "imapPort": 993, "imapSecurity": "ssl",
  "smtpHost": "smtp.b.com", "smtpPort": 465, "smtpSecurity": "ssl",
  "username": "a@b.com", "enabled": true, "hasCredential": true,
  "createdAt": "2026-11-04T12:00:00Z"
}]}
```

`protocol` 取值 `imap` | `pop3`（收信协议）。POP3 账号的 `imapHost/imapPort/imapSecurity` 表示 **POP3 服务器**——字段名沿用，避免两套结构。

**任何方法都不得返回密码或 DPAPI 密文**，只允许 `hasCredential` 布尔。

### `account.add`

参数：

```json
{"address": "a@b.com", "displayName": "工作邮箱",
 "protocol": "imap",
 "imapHost": "imap.b.com", "imapPort": 993, "imapSecurity": "ssl",
 "username": "a@b.com", "password": "<明文，仅此一次>",
 "smtpHost": "smtp.b.com", "smtpPort": 465, "smtpSecurity": "ssl"}
```

结果：`{"id": "acc_xxx"}`

- `protocol` 缺省 `imap`；只接受 `imap` / `pop3`，其它值返回 `BAD_REQUEST`。
- 密码必须立即 DPAPI 加密后入库；明文不得落盘、不得写日志。
- `imapSecurity` / `smtpSecurity` 取值：`ssl` | `starttls` | `none`。
- 端口缺省按协议与加密方式推断：IMAP `993/143/143`、POP3 `995/110/110`、SMTP `465/587/25`（依次对应 ssl/starttls/none）。

### `account.remove`

参数：`{"id": "acc_xxx"}` → 结果 `{"removed": true}`

### `account.test`

参数：`{"id": "acc_xxx"}`

IMAP 结果：

```json
{"ok": true, "protocol": "imap",
 "folders": [{"name": "INBOX", "delimiter": "/", "flags": ["\\HasNoChildren"]}],
 "smtp": {"ok": true, "detail": ""}}
```

POP3 结果：

```json
{"ok": true, "protocol": "pop3", "messages": 128, "smtp": {"ok": true, "detail": ""}}
```

- `smtp` 字段**只在账号配置了 `smtpHost` 时出现**；SMTP 测试失败不使整体失败（收信成功即 `ok: true`），失败原因放 `smtp.detail`。
- 收信失败返回 `IMAP_ERROR`（POP3 沿用同一个错误码，message 带协议前缀）或 `CREDENTIAL_MISSING`。
- `folders[].name` 是**解码后的 UTF-8 名字**：IMAP 服务器返回的 modified UTF-7（RFC 3501 §5.1.3，如 `&XfJT0ZAB-`）已在核心层解码成 `已发送`，可直接显示、可按名字匹配。

### `folder.list`

参数：`{"accountId": "acc_xxx"}`

结果：

```json
{"folders": [{"name": "已发送", "delimiter": "/", "flags": ["\\HasNoChildren"]}]}
```

- 只读本地 `folders` 表（`account.test` 成功时会落库），**不联网**。
- `name` 是解码后的 UTF-8 名字，与 `account.test` 返回的一致；`delimiter` 可能是空串（服务器回 `NIL` 时）。
- 账号不存在返回 `NOT_FOUND`；账号存在但还没有文件夹记录（从没成功跑过 `account.test`）返回空数组 `{"folders": []}`。

### `sync.status`

参数：`{}`

结果：

```json
{"counters": {"accounts": 1, "messages": 42, "unread": 3, "drafts": 2, "pendingApprovals": 1, "pendingAi": 0},
 "lastSyncAt": "2026-11-04T12:30:00Z"}
```

- `drafts`：还在草稿箱里的草稿数（`status != sent`；已发送的草稿不计入）。
- `pendingApprovals`：`status = pending` 的审批数（过期但未处理的仍计入，与 `approval.list.pending` 一致）。
- `pendingAi` 目前恒为 0：AI 任务表属于后续阶段。

### `sync.run`

参数：

```json
{"accountId": "acc_xxx", "folder": "INBOX", "limit": 50}
```

- `folder` 缺省 `INBOX`；`limit` 缺省 50，上限 200（一次同步最多拉多少封）。
- `folder` 用**解码后的 UTF-8 名字**（例如 `已发送`），核心层在发 `SELECT` / `STATUS` 前再编码成 modified UTF-7。
- POP3 账号忽略 `folder`，`uidValidity` 固定 0。
- 可选 `mode: "backfill"` + `before`：等价于直接调用 [`history.backfill`](#historybackfill)，只拉比 `before` 更早、本地还没有的邮件，返回结构也换成 backfill 的结果（不改动前向游标）。`before` 必填，ISO-8601（`2026-11-03` / `2026-11-03T00:00:00Z` 均可）。

结果：

```json
{"accountId": "acc_xxx", "folder": "INBOX", "uidValidity": 12345,
 "fetched": 12, "stored": 10, "skipped": 2, "lastUid": 4567,
 "startedAt": "2026-11-04T12:00:00Z", "finishedAt": "2026-11-04T12:00:04Z"}
```

**幂等要求**：同一封邮件（account + folder + uidValidity + uid）重复同步不得产生第二条记录；`stored` 只统计真正新增的数量。

**UIDVALIDITY 变化**：重置该文件夹游标、按新值重建记录，不得因为旧 uid 冲突而报错。

### `sync.reset`

参数：`{"accountId": "acc_xxx", "folder": "INBOX"}`

结果：`{"accountId": "acc_xxx", "folder": "INBOX", "removed": 42}`

删除该文件夹的本地副本并把同步游标清零，下一次 `sync.run` 从头重拉。用于「本地索引和服务器对不上」的场景（例如改了同步窗口之后）。

### `message.list`

参数：

```json
{"accountId": "acc_xxx", "folder": "INBOX", "limit": 50, "offset": 0}
```

全部可省略：省略 `accountId` 表示所有账户，省略 `folder` 表示所有文件夹。

结果：

```json
{"total": 42, "messages": [{
  "id": "msg_xxx", "accountId": "acc_xxx", "folder": "INBOX", "uid": 4567,
  "subject": "合同确认", "fromAddr": "a@b.com", "toAddr": "me@b.com",
  "date": "2026-11-04T09:12:00Z", "snippet": "正文前 200 字…", "seen": false,
  "threadId": "acc_xxx:<root@b.com>"
}]}
```

按 `date` 倒序。`threadId` 的取值与归并规则见 [`thread.list`](#threadlist)。

### `message.get`

参数：`{"id": "msg_xxx"}`

结果：

```json
{"id": "msg_xxx", "accountId": "acc_xxx", "folder": "INBOX", "uid": 4567,
 "subject": "合同确认", "fromAddr": "a@b.com", "toAddr": "me@b.com", "ccAddr": "",
 "date": "2026-11-04T09:12:00Z", "snippet": "…", "text": "完整纯文本正文",
 "html": "<p>…</p>", "size": 4812, "seen": false,
 "threadId": "acc_xxx:<root@b.com>"}
```

`html` 可能为空串；返回值**不包含**附件内容，也不包含原始 MIME。

### `message.search`

参数：`{"query": "季度报告", "limit": 50, "offset": 0}`

结果：

```json
{"total": 3, "messages": [{
  "id": "msg_xxx", "accountId": "acc_xxx", "folder": "INBOX", "uid": 4567,
  "subject": "季度报告", "fromAddr": "a@b.com", "toAddr": "me@b.com",
  "date": "2026-11-04T09:12:00Z", "snippet": "…«季度报告»…", "seen": false,
  "threadId": "acc_xxx:<root@b.com>"
}]}
```

- 检索**本地已同步的邮件**（`messages` 表），不联网：本地没有的邮件搜不到，先 `sync.run` 再搜。
- 检索字段：`subject`、`body_text`（纯文本正文）、`from_addr`、`to_addr`。命中 HTML 里的文字不算命中——`body_text` 才是索引源。
- `query` 必填，空串 / 全空白 → `BAD_REQUEST`。`limit` 缺省 50、上限 200（超过按 200 处理，小于 1 → `BAD_REQUEST`）；`offset` 缺省 0，负数 → `BAD_REQUEST`。
- 字段与 `message.list` **完全一致**；`snippet` 换成命中的高亮片段（命中词用 `«` `»` 包住，最长约 12 个词，**不返回整篇正文**）。`total` 是命中的总数，与分页无关。
- 按相关度（FTS5 `rank`）排序，不是按日期。
- 查询按**空白切词、逐词加引号转义后 AND**：`合同 报表` = 同时含两个词。`*`、`-`、`OR`、`NEAR(...)`、引号、括号等 FTS5 语法字符都当普通文本处理，**不会**报语法错误；只由标点组成的词没有可检索的字，直接忽略（全都忽略时返回空数组而不是报错）。
- 分词器是 FTS5 默认的 `unicode61`：**连续汉字算一个词**，所以 `季度报告` 能命中「季度报告」，但 `合同` 命中不了「合同确认书」（两个不同的词）。调用方要按用户输入原样传，别自己加通配符。
- 索引由触发器维护，`sync.run` / `sync.reset` 增删的邮件自动进/出索引；v3 老库升级到 v4 时会重建一次索引，不需要重新同步。

### `thread.list`

参数（全部可省略）：

```json
{"accountId": "acc_xxx", "folder": "INBOX", "limit": 50}
```

结果：

```json
{"threads": [{
  "threadId": "acc_xxx:<root@b.com>",
  "subject": "合同确认",
  "messageCount": 3,
  "lastDate": "2026-11-04T09:12:00Z",
  "unreadCount": 2,
  "participants": ["a@b.com", "me@b.com"]
}]}
```

只读本地已同步的邮件，不联网。按 `lastDate` 倒序（同一时间按 `threadId`）；`limit` 缺省 50、上限 200。

**线程归并规则**（v7，入库时计算；v6 及更老的库在启动迁移时回填一次）：

1. `threadId` 的格式是 `<accountId>:<rootMessageId>`。**账户前缀是身份的一部分**：两个账户即使收到同一个 `Message-ID`，线程也互不相串。
2. 邮件的 `References` / `In-Reply-To` 能在**同账户**本地已索引邮件里命中一条 → 直接归入那条邮件的线程。
3. 有引用但一条都命中不了 → 以**最早引用**为根：有 `References` 时取第一个 id，否则取 `In-Reply-To`；`threadId = <accountId>:<最早引用>`。
4. 完全没有引用 → `threadId = <accountId>:<本条 Message-ID>`，孤立成线程。邮件缺 `Message-ID` 时核心层会生成稳定的 `<sha1>@mail-core>`（同步场景按 account/folder/UIDVALIDITY/UID 派生，重复同步与 reset 后都不变）。
5. 迟到的父邮件不会拆散子线程：子邮件当初以“最早引用”为根，父邮件入库时按同一规则算出的根与它一致。`history.backfill` 拉回旧父邮件后线程会自动合并（已验证）。

字段说明：

- `subject`：线程内**最早**一封邮件的主题（通常是根邮件；不自动去掉 `Re:`）。
- `messageCount` / `unreadCount` / `lastDate`：只统计 `accountId` / `folder` 过滤范围内的邮件；`lastDate` 是该范围内最新一封的 `date`。
- `participants`：线程内出现过的 `From` / `To` / `Cc` 地址去重后的列表，按首次出现顺序，比较时忽略大小写。
- 没有 `thread_id` 的空行不会出现（迁移会先回填；新邮件入库即算好）。

### `thread.get`

参数：`{"threadId": "acc_xxx:<root@b.com>"}`

结果：

```json
{"threadId": "acc_xxx:<root@b.com>", "subject": "合同确认",
 "messages": [{
   "id": "msg_xxx", "accountId": "acc_xxx", "folder": "INBOX", "uid": 4567,
   "subject": "合同确认", "fromAddr": "a@b.com", "toAddr": "me@b.com",
   "date": "2026-11-04T09:12:00Z", "snippet": "…", "seen": false,
   "threadId": "acc_xxx:<root@b.com>"
 }]}
```

- `messages` 按 `date` **升序**（最早的在前），每项字段与 `message.list` 完全一致；不含正文，正文用 `message.get`。
- 主题同 `thread.list`：最早一封的主题。
- `threadId` 不存在 → `NOT_FOUND`。

### `history.backfill`

参数：

```json
{"accountId": "acc_xxx", "folder": "INBOX",
 "before": "2026-11-03T00:00:00Z", "limit": 50}
```

结果：

```json
{"accountId": "acc_xxx", "folder": "INBOX", "before": "2026-11-03T00:00:00Z",
 "fetched": 12, "stored": 10, "oldestDate": "2026-10-01T08:00:00Z"}
```

- 回溯比 `before` **更早**、且本地还没有的邮件：IMAP 发 `UID SEARCH BEFORE <date>`，并用邮件头 `Date` 再校验一次；POP3 从最老的邮件开始按 UIDL 派生的 uid 去重。`before` 必填，ISO-8601，可写成日期或带时区的时间，核心层统一成 UTC `...Z`。
- `limit` 缺省 50、上限 200（本次最多取多少封候选）；`fetched` 是本次真正取回并保存的旧邮件数，`stored` 是新增行数——重复调用时 `stored = 0`。
- **幂等**：本地已有的 UID 直接跳过，同一封邮件不会第二次入库。
- **不动前向游标**：不修改 `sync_state.last_uid`，`sync.run` / `sync.status` 的增量同步不受影响。
- `oldestDate`：该 folder 本地当前最老的非空 `date`（没有邮件时为空串），用于展示回溯进度。
- 账号不存在 → `NOT_FOUND`；没有可用密码 → `CREDENTIAL_MISSING`；网络失败 → `IMAP_ERROR`（POP3 同样沿用）。

### `contact.list`

参数：`{}`

结果：

```json
{"contacts": [{
  "id": "con_xxx", "name": "张三", "email": "zhang@example.com",
  "phone": "13800000000", "company": "示例公司", "title": "经理",
  "notes": "老客户", "createdAt": "2026-11-04T12:00:00Z",
  "updatedAt": "2026-11-04T12:00:00Z"
}]}
```

按 `name` 升序（同名的按 `id`）。通讯录**只存本地**，不同步到邮箱服务器。

### `contact.add`

参数：

```json
{"name": "张三", "email": "zhang@example.com", "phone": "13800000000",
 "company": "示例公司", "title": "经理", "notes": "老客户"}
```

结果：`{"id": "con_xxx"}`

- `name` 必填且不能是空串 / 全空白，否则 `BAD_REQUEST`；其余字段可省略，缺省是空串。
- 不校验邮箱格式（`email` 可以是空串或多个地址），也不去重。

### `contact.update`

参数：`{"id": "con_xxx", "email": "new@example.com", "title": "总监"}`

结果：`{"updated": true}`

- 只改传了的字段，其余保持原值；`updatedAt` 每次都会刷新。
- `id` 不存在 → `NOT_FOUND`；`name` 传了但为空 → `BAD_REQUEST`；字段类型不是字符串 → `BAD_REQUEST`。
- 未知字段忽略。

### `contact.remove`

参数：`{"id": "con_xxx"}` → 结果 `{"removed": true}`；`id` 不存在 → `NOT_FOUND`。

### `template.list`

参数：`{}`

结果：

```json
{"templates": [{
  "id": "tpl_xxx", "name": "报价", "subject": "报价单 - {{客户}}",
  "body": "您好，报价见附件。",
  "createdAt": "2026-11-04T12:00:00Z", "updatedAt": "2026-11-04T12:00:00Z"
}]}
```

按 `createdAt` 升序。模板是**纯本地数据**，不和任何邮箱同步；`{{客户}}` 之类的占位符原样保存，替换由调用方完成。

### `template.add`

参数：`{"name": "报价", "subject": "报价单 - {{客户}}", "body": "您好，报价见附件。"}`

结果：`{"id": "tpl_xxx"}`

- `name` 必填且不能为空串 / 全空白，否则 `BAD_REQUEST`；`subject` / `body` 可省略，缺省空串。

### `template.remove`

参数：`{"id": "tpl_xxx"}` → 结果 `{"removed": true}`；`id` 不存在 → `NOT_FOUND`（与 `account.remove` 一致）。

### `draft.save`

参数：

```json
{"draftId": "dft_xxx", "accountId": "acc_xxx", "to": "you@example.com",
 "cc": "", "subject": "季度报告", "body": "您好，附件是报告。"}
```

结果：`{"id": "dft_xxx", "contentHash": "9f2c…", "status": "draft"}`

- 不带 `draftId` 新建（id 由核心层生成，`dft_` 前缀）；带 `draftId` 就地替换该草稿的 `accountId` / `to` / `cc` / `subject` / `body`，`id` 不存在 → `NOT_FOUND`。
- `accountId` / `to` 必填；`accountId` 必须存在，否则 `NOT_FOUND`；`cc` / `subject` / `body` 可省略，缺省空串。
- 每次保存都重算 `contentHash` 和 `updatedAt`；`createdAt` 只在新建时写入。
- 已批准 / 已驳回 / 已发送的草稿被再次保存时，状态退回 `draft`。旧审批记录保持原状态，但绑定的是旧哈希，执行时必然 `HASH_MISMATCH`。
- `contentHash = sha256(to + "\n" + cc + "\n" + subject + "\n" + body)` 的小写十六进制；四个字段先统一换行（`\r\n`、`\r` → `\n`）再拼接，按 UTF-8 编码。除 `to` 在校验时去掉首尾空格外，字段内容不做 trim。

### `draft.get`

参数：`{"id": "dft_xxx"}`

结果：

```json
{"id": "dft_xxx", "accountId": "acc_xxx", "to": "you@example.com", "cc": "",
 "subject": "季度报告", "body": "您好，附件是报告。", "contentHash": "9f2c…",
 "status": "draft", "createdAt": "2026-11-04T12:00:00Z", "updatedAt": "2026-11-04T12:00:00Z"}
```

`status` 取值 `draft` | `approved` | `rejected` | `sent`。不存在 → `NOT_FOUND`。

### `draft.list`

参数：`{"accountId": "acc_xxx"}`（可省略 = 全部账户）

结果：

```json
{"drafts": [{"id": "dft_xxx", "accountId": "acc_xxx", "to": "you@example.com",
  "subject": "季度报告", "contentHash": "9f2c…", "status": "draft",
  "updatedAt": "2026-11-04T12:00:00Z"}]}
```

按 `updatedAt` 倒序（最近修改的在前）。列表**不含** `body`；要正文请调 `draft.get`。

### `draft.remove`

参数：`{"id": "dft_xxx"}` → 结果 `{"removed": true}`；`id` 不存在 → `NOT_FOUND`。

草稿的审批记录随草稿级联删除；已经发生的发送记录（`mail_actions`）保留，作为审计日志。

### `approval.request`

参数：`{"draftId": "dft_xxx", "ttlMinutes": 1440}`

结果：

```json
{"id": "apr_xxx", "draftId": "dft_xxx", "contentHash": "9f2c…",
 "expiresAt": "2026-11-05T12:00:00Z"}
```

- 绑定草稿**当前**的 `contentHash`；`action` 固定 `send`，初始状态 `pending`。
- `ttlMinutes` 缺省 1440（一天），上限 525600（一年）；`0` 表示立即过期（`expiresAt == createdAt`）。
- 草稿不存在 → `NOT_FOUND`。同一草稿可以有多个待审批记录，核心层不去重。

### `approval.list`

参数：`{}`

结果：

```json
{"pending": [{"id": "apr_xxx", "draftId": "dft_xxx", "action": "send",
  "contentHash": "9f2c…", "status": "pending",
  "createdAt": "2026-11-04T12:00:00Z", "expiresAt": "2026-11-05T12:00:00Z",
  "decidedAt": "", "decidedVia": "", "decidedBy": ""}],
 "history": [ ...同样结构，status 为 approved / rejected... ]}
```

- `pending`：所有 `status = pending` 的记录，按 `createdAt` **升序**（队列顺序）。过期但未处理的仍是 `pending`，`expiresAt` 已过；对它们 `approve` / `reject` / `execute` 返回 `APPROVAL_EXPIRED`。
- `history`：已决定的记录（`approved` / `rejected`），按 `createdAt` **倒序**，最多 50 条。
- `decidedVia` / `decidedBy` 是 v6 审计字段：**pending 项为空串**；已决定项取 `approve` / `reject` 传入的值（未传也是空串，v5 老记录迁移后同样为空串）。
- 审批卡片要显示主题 / 收件人时，用 `draftId` 再调 `draft.get`（草稿已删则 `NOT_FOUND`）。

### `approval.approve` / `approval.reject`

参数：

```json
{"id": "apr_xxx", "decidedVia": "prompt", "decidedBy": "user"}
```

结果：`{"approved": true}` / `{"rejected": true}`

- `decidedVia` / `decidedBy` 可省略，缺省存空串（= 未记录来源）。
- `decidedVia` 仅允许 `""` | `"prompt"` | `"policy-never"` | `"api"`：用户弹窗批准、宿主策略（`policy=never`）自动放行、API 直接调用。
- `decidedBy` 仅允许 `""` | `"user"` | `"agent"`。
- 非法值（大小写不符、带空格、非字符串）→ `BAD_REQUEST`，**审批保持 pending**，不会消耗这次决定。
- 只接受 `status = pending` 的审批：不存在 → `APPROVAL_NOT_FOUND`；已过期（`now >= expiresAt`）→ `APPROVAL_EXPIRED`；已决定 → `BAD_REQUEST`（message 带当前状态，如 `approval is already approved`）。
- 通过 / 驳回后写 `decidedAt` 和两个审计字段，并把草稿状态改为 `approved` / `rejected`。审批自身状态此后保持 `approved`（发送成功后也不变）。

### `action.execute`

参数：`{"approvalId": "apr_xxx", "idempotencyKey": "apr_xxx"}`（`idempotencyKey` 可省略，缺省 = `approvalId`）

结果：

```json
{"sent": true, "messageId": "<20261104120005.123@b.com>", "executedAt": "2026-11-04T12:00:05Z"}
```

校验顺序（每一步对应一个错误码）：

1. 审批存在 → 否则 `APPROVAL_NOT_FOUND`
2. `status = approved` → 否则 `APPROVAL_NOT_APPROVED`（pending 未批准 / rejected 被驳回）
3. 未过期（`now < expiresAt`）→ 否则 `APPROVAL_EXPIRED`
4. 草稿当前 `contentHash` 等于审批绑定的哈希 → 否则 `HASH_MISMATCH`
5. 幂等键没有执行记录 → 否则 `ALREADY_EXECUTED`
6. 真正发信

发送与结果：

- 用草稿所属账号的 `smtpHost` / `smtpPort` / `smtpSecurity`（`ssl` → `SMTP_SSL`；`starttls` → `SMTP` + `STARTTLS`；`none` → `SMTP`），用 DPAPI 解出的密码 `login`，超时 20 秒，证书用 `ssl.create_default_context()`。
- `From` = 账号地址，`To` / `Cc` 取草稿，`Subject` 取草稿，正文为**纯文本**；`Message-ID` 由核心层生成并原样返回。
- 成功：草稿 `status = sent`、审批保持 `approved`、写一条 `mail_actions(result = sent)`，返回上面三个字段。
- 失败（未配置 `smtpHost`、没有可用密码、DPAPI 解密失败、连接 / 认证 / 发信出错）：写一条 `mail_actions(result = failed, error = 原因)`，**草稿状态不变**，返回 `SMTP_ERROR`。`error` 和响应 message **绝不包含密码**。
- **幂等**：`idempotency_key` 在 `mail_actions` 里是 UNIQUE。首次执行记录写入后（无论 `sent` 还是 `failed`），同一个键再次 `execute` 一律返回 `ALREADY_EXECUTED`，绝不重发。发送失败后要重试，调用方需要提供**新的** `idempotencyKey`，并在界面上明确这是另一次发送尝试。

### `admin.backup`

参数：`{}`

结果：

```json
{"path": "C:\\...\\backups\\mail-20261105-001500.db", "size": 45056,
 "createdAt": "2026-11-05T00:15:00Z"}
```

- 用 SQLite 在线备份 API 把当前 `mail.db` 完整快照到 `<data-dir>/backups/`，文件名 `mail-YYYYMMDD-HHMMSS.db`（UTC，与 `createdAt` 一致）；同一秒内的第二次备份加 `-2` / `-3` 后缀，不覆盖已有文件。
- 先写临时文件、成功后原子重命名；失败不会留下半成品文件。
- 成功后按文件名时间只保留最近 10 个备份，更老的自动删除。
- 备份是完整可打开的 SQLite 库（含当时所有表和数据），可直接用于回滚。
- 失败返回 `INTERNAL`，message 说明原因（如磁盘只读、数据库被占用）；不包含密码或凭据内容。

### `admin.backups`

参数：`{}`

结果：

```json
{"backups": [{"path": "C:\\...\\backups\\mail-20261105-001500.db",
  "size": 45056, "createdAt": "2026-11-05T00:15:00Z"}]}
```

- 按 `createdAt` 倒序（新的在前）；没有备份目录或没有备份时返回空数组（不报错）。
- 只列出 `mail-*.db`，目录里的其它文件忽略。

### `shutdown`

参数：`{}` → 结果 `{"ok": true}`，随后进程自行退出。

## 启动约定

- 父进程调用：`<python> -m mail_core --data-dir <绝对路径>`
- 工作目录：`mail-core/`（包所在目录的父目录）
- 环境变量：`MAIL_CORE_DATA_DIR` 可作为 `--data-dir` 的替代；两者都缺则用 `./data/mail`
- 子进程启动后**不主动输出**任何内容，等父进程发 `core.handshake`
- 数据库文件：`<data-dir>/mail.db`，首次启动自动建表
- **升级前自动备份**：启动时若 `mail.db` 已存在、非 0 字节，且 `PRAGMA user_version` 小于当前 `schemaVersion`，核心层会先把整库备份到 `<data-dir>/backups/`（命名与 `admin.backup` 相同），再执行迁移。备份失败时**拒绝启动**（退出码 1，stderr 写明原因），绝不带着未备份的库跑迁移。新建库（文件不存在或 0 字节）和已是最新 schema 的库不需要备份。

## 版本策略

- `protocol` 变化才需要双方同时升级；`version` 仅用于展示。
- 新增方法不做破坏性变更；删除或改签名的旧方法必须提升 `protocol`。
- 数据库 `schemaVersion` 独立于 `protocol`：v4 只**新增** `contacts`、`templates` 和 `messages_fts`（FTS5 external content 表 + 三个同步触发器），不改旧表；老库（v1~v3）启动时自动逐级迁移，v4 迁移会把已有 `messages` 全量重建进索引，不需要重新同步。
- v5 只**新增** `drafts`、`approvals`、`mail_actions` 三张表及其索引，不改旧表；老库（v1~v4）启动时自动迁移，账号 / 邮件 / 通讯录 / 模板全部保留。`drafts.account_id` 与 `approvals.draft_id` 带 `ON DELETE CASCADE`（删账号连带草稿、删草稿连带审批），`mail_actions` 故意不设外键，发送审计记录不随草稿删除而消失。
- v6 只给 `approvals` **新增** `decided_via`、`decided_by` 两列，默认空串，不改旧表结构、不丢旧数据；已有决定迁移后审计字段为空（v5 记录本来就没有来源信息，不回填猜测值）。迁移幂等，老库（v1~v5）启动时自动升级。
- v7 只给 `messages` **新增** `thread_id`、`in_reply_to`、`refs` 三列（`refs` 是 `References` 头；`references` 是 SQL 关键字，故列名缩短）以及两个线程索引，不改旧表结构、不删旧邮件；迁移后对 `thread_id = ''` 的旧行按线程规则回填一次（幂等，重复迁移不再改动已回填的行）。老库（v1~v6）启动时先自动备份再逐级升级。新增字段都可空串，旧插件忽略 `threadId` 即可继续工作。
