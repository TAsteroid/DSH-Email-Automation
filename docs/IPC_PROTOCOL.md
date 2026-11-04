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
| `INTERNAL` | 未预期异常（message 带异常类型，不打印堆栈到 stdout） |

## 方法

### `core.handshake`

参数：`{}`

结果：

```json
{"protocol": 1, "version": "0.1.0", "dbPath": "...", "dataDir": "...", "schemaVersion": 2}
```

父进程启动后第一条请求必须是它；`protocol` 不等于 1 时插件应拒绝工作并在界面上报错。

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
{"counters": {"accounts": 1, "messages": 42, "unread": 3, "drafts": 0, "pendingApprovals": 0, "pendingAi": 0},
 "lastSyncAt": "2026-11-04T12:30:00Z"}
```

### `sync.run`

参数：

```json
{"accountId": "acc_xxx", "folder": "INBOX", "limit": 50}
```

- `folder` 缺省 `INBOX`；`limit` 缺省 50，上限 200（一次同步最多拉多少封）。
- `folder` 用**解码后的 UTF-8 名字**（例如 `已发送`），核心层在发 `SELECT` / `STATUS` 前再编码成 modified UTF-7。
- POP3 账号忽略 `folder`，`uidValidity` 固定 0。

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
  "date": "2026-11-04T09:12:00Z", "snippet": "正文前 200 字…", "seen": false
}]}
```

按 `date` 倒序。

### `message.get`

参数：`{"id": "msg_xxx"}`

结果：

```json
{"id": "msg_xxx", "accountId": "acc_xxx", "folder": "INBOX", "uid": 4567,
 "subject": "合同确认", "fromAddr": "a@b.com", "toAddr": "me@b.com", "ccAddr": "",
 "date": "2026-11-04T09:12:00Z", "snippet": "…", "text": "完整纯文本正文",
 "html": "<p>…</p>", "size": 4812, "seen": false}
```

`html` 可能为空串；返回值**不包含**附件内容，也不包含原始 MIME。

### `shutdown`

参数：`{}` → 结果 `{"ok": true}`，随后进程自行退出。

## 启动约定

- 父进程调用：`<python> -m mail_core --data-dir <绝对路径>`
- 工作目录：`mail-core/`（包所在目录的父目录）
- 环境变量：`MAIL_CORE_DATA_DIR` 可作为 `--data-dir` 的替代；两者都缺则用 `./data/mail`
- 子进程启动后**不主动输出**任何内容，等父进程发 `core.handshake`
- 数据库文件：`<data-dir>/mail.db`，首次启动自动建表

## 版本策略

- `protocol` 变化才需要双方同时升级；`version` 仅用于展示。
- 新增方法不做破坏性变更；删除或改签名的旧方法必须提升 `protocol`。
