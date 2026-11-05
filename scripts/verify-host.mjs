/**
 * Host 侧测试：用假 ctx + 假 Mail Core 子进程装载 dsh-mail-plugin。
 *
 * 覆盖三件真实会出事的东西：
 *   1. stdio JSON-RPC 分帧与握手（假子进程按行回包）
 *   2. HTTP 通道的守门（跨源 / 跨站 / 非 loopback / 错误方法 / 坏 JSON）
 *   3. 工具契约（render 必须返回 ContentBlock[]，且不泄露密码）
 *
 * 不需要真 Python —— 这层测的是协议接线，不是 Core 业务。
 *
 * 用法：node scripts/verify-host.mjs
 */
import { PassThrough, Readable } from 'node:stream'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const hostPath = join(here, '..', 'dsh-mail-plugin', 'lib', 'index.js')
const { apply, inject, name } = await import(`file://${hostPath.replace(/\\/g, '/')}`)

const failures = []
const check = (condition, message) => {
  if (!condition) failures.push(message)
}

// ---- 假 Mail Core ---------------------------------------------------------
const received = []
const configEdits = []
const approvalRequests = []
const approveCalls = []
let approvalOutcome = 'allowed-once'
let approvalPolicyConfig = 'ask'
let tornDown = false

function makeFakeChild() {
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  const stderr = new PassThrough()
  let resolveDone
  const done = new Promise((resolve) => {
    resolveDone = resolve
  })
  let closed = false

  stdin.on('data', (chunk) => {
    for (const line of String(chunk).split('\n')) {
      if (line.trim().length === 0) continue
      const request = JSON.parse(line)
      received.push(request)
      let response
      try {
        response = { id: request.id, ok: true, result: handleMethod(request.method, request.params) }
      } catch (error) {
        response = { id: request.id, ok: false, error: { code: error.code ?? 'INTERNAL', message: error.message } }
      }
      stdout.write(`${JSON.stringify(response)}\n`)
    }
  })

  return {
    stdin,
    stdout,
    stderr,
    done,
    terminate() {
      if (closed) return
      closed = true
      resolveDone({ exitCode: 0 })
    },
  }
}

const fakeAccounts = [
  {
    id: 'acc_1',
    address: 'me@example.com',
    displayName: '工作邮箱',
    imapHost: 'imap.example.com',
    imapPort: 993,
    imapSecurity: 'ssl',
    username: 'me@example.com',
    enabled: true,
    hasCredential: true,
    createdAt: '2026-11-04T12:00:00Z',
  },
]

function handleMethod(method, params) {
  if (method === 'core.handshake') {
    return { protocol: 1, version: '0.1.0', dbPath: 'C:\\data\\mail.db', dataDir: 'C:\\data', schemaVersion: 1 }
  }
  if (method === 'account.list') return { accounts: fakeAccounts }
  if (method === 'account.add') return { id: 'acc_new' }
  if (method === 'account.remove') return { removed: true }
  if (method === 'account.test') {
    if (params && params.id === 'acc_broken') {
      const error = new Error('connect failed: getaddrinfo failed')
      error.code = 'IMAP_ERROR'
      throw error
    }
    return { ok: true, folders: [{ name: 'INBOX', delimiter: '/' }] }
  }
  if (method === 'sync.status') {
    return {
      counters: { accounts: 1, messages: 42, unread: 3, drafts: 0, pendingApprovals: 0, pendingAi: 0 },
      lastSyncAt: '2026-11-04T12:30:00Z',
    }
  }
  if (method === 'sync.run') {
    return {
      accountId: params.accountId,
      folder: params.folder ?? 'INBOX',
      uidValidity: 1,
      fetched: 2,
      stored: 2,
      skipped: 0,
      lastUid: 10,
      startedAt: '2026-11-04T12:00:00Z',
      finishedAt: '2026-11-04T12:00:02Z',
    }
  }
  if (method === 'sync.reset') {
    return { accountId: params.accountId, folder: params.folder ?? 'INBOX', removed: 3 }
  }
  if (method === 'folder.list') {
    return {
      folders: [
        { name: 'INBOX', delimiter: '/', flags: [] },
        { name: '已发送', delimiter: '/', flags: [] },
        { name: '草稿', delimiter: '/', flags: [] },
        { name: '已删除邮件', delimiter: '/', flags: [] },
        { name: '垃圾邮件', delimiter: '/', flags: [] },
      ],
    }
  }
  if (method === 'message.search') {
    return { total: 1, messages: [{ id: 'msg_1', subject: '合同确认', fromAddr: 'a@b.com', date: '2026-11-04T09:12:00Z', snippet: '…«合同»…' }] }
  }
  if (method === 'contact.list') {
    return { contacts: [{ id: 'con_1', name: '张三', email: 'z@b.com', phone: '', company: 'CBE', title: '', notes: '' }] }
  }
  if (method === 'contact.add') return { id: 'con_new' }
  if (method === 'contact.remove') return { removed: true }
  if (method === 'template.list') {
    return { templates: [{ id: 'tpl_1', name: '报价回复', subject: 'Re', body: '你好' }] }
  }
  if (method === 'template.add') return { id: 'tpl_new' }
  if (method === 'template.remove') return { removed: true }
  if (method === 'draft.list') {
    return {
      drafts: [{ id: 'drf_1', accountId: 'acc_1', to: 'x@b.com', subject: '报价回复', contentHash: 'h1', status: 'approved', updatedAt: '2026-11-04T10:00:00Z' }],
    }
  }
  if (method === 'draft.save') return { id: params.draftId ?? 'drf_new', contentHash: 'deadbeef', status: 'draft' }
  if (method === 'draft.get') {
    return { id: params.id, accountId: 'acc_1', to: 'x@b.com', cc: '', subject: '报价回复', body: '正文', contentHash: 'h1', status: 'approved' }
  }
  if (method === 'draft.remove') return { removed: true }
  if (method === 'approval.list') {
    return {
      pending: [{ id: 'apr_1', draftId: 'drf_1', action: 'send', contentHash: 'h1', status: 'pending', expiresAt: '2026-11-05T10:00:00Z' }],
      history: [{ id: 'apr_0', draftId: 'drf_1', action: 'send', contentHash: 'h0', status: 'sent', createdAt: '2026-11-03T10:00:00Z' }],
    }
  }
  if (method === 'approval.request') return { id: 'apr_new', draftId: params.draftId, contentHash: 'h1', expiresAt: '2026-11-05T10:00:00Z' }
  if (method === 'approval.approve') {
    approveCalls.push(params)
    return { approved: true }
  }
  if (method === 'approval.reject') return { rejected: true }
  if (method === 'action.execute') return { sent: true, messageId: '<msg@example>', executedAt: '2026-11-04T10:05:00Z' }
  if (method === 'thread.list') {
    return {
      threads: [
        { threadId: 'thr_1', subject: '报价回复', messageCount: 2, lastDate: '2026-11-04T10:00:00Z', unreadCount: 1, participants: ['a@b.com'] },
      ],
    }
  }
  if (method === 'thread.get') return { threadId: params.threadId, subject: '报价回复', messages: [{ id: 'm1' }, { id: 'm2' }] }
  if (method === 'history.backfill') return { fetched: 1, stored: 1, oldestDate: '2026-10-01T00:00:00Z' }
  if (method === 'admin.backup') return { path: 'C:\\data\\backups\\mail-20261104-120000.db', size: 4096, createdAt: '2026-11-04T12:00:00Z' }
  if (method === 'admin.backups') {
    return { backups: [{ path: 'C:\\data\\backups\\mail-20261104-120000.db', size: 4096, createdAt: '2026-11-04T12:00:00Z' }] }
  }
  if (method === 'message.delete') return { deleted: true, trashed: params.permanent !== true, localOnly: false }
  if (method === 'message.restore') return { restored: true, folder: params.folder ?? 'INBOX' }
  if (method === 'attachment.list') {
    return {
      attachments: [
        {
          id: 'att_1',
          messageId: 'msg_1',
          accountId: 'acc_1',
          subject: '报价回复',
          fromAddr: 'a@b.com',
          filename: '报价表.xlsx',
          contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          size: 2048,
          fetchedAt: '',
        },
      ],
    }
  }
  if (method === 'attachment.fetch') {
    return { id: params.id, path: 'C:\\data\\attachments\\msg_1\\报价表.xlsx', size: 2048, sha256: 'abc123', fetchedAt: '2026-11-04T12:00:00Z' }
  }
  if (method === 'todo.list') {
    return {
      todos: [
        { messageId: 'msg_1', accountId: 'acc_1', subject: '报价回复', fromAddr: 'a@b.com', date: '2026-11-04T09:12:00Z', folder: 'INBOX', todoState: 'pending', hasSummary: true, attachmentCount: 1 },
      ],
    }
  }
  if (method === 'todo.set') return { messageId: params.messageId, state: params.state }
  if (method === 'summary.save') return { id: 'sum_1', createdAt: '2026-11-04T12:00:00Z' }
  if (method === 'summary.list') return { summaries: [{ id: 'sum_1', messageId: 'msg_1', category: '合同协议', summary: '旧总结', createdAt: '2026-11-03T00:00:00Z' }] }
  if (method === 'summary.context') {
    return {
      message: { id: params.messageId, subject: '报价回复', fromAddr: 'a@b.com' },
      thread: [{ id: 'msg_0', subject: '报价回复' }],
      related: [{ id: 'msg_2', subject: '报价回复（续）', reason: '同一发件人：a@b.com' }],
      summaries: [{ messageId: 'msg_1', category: '合同协议', summary: '旧总结', keyPoints: [] }],
      attachments: [{ id: 'att_1', filename: '报价表.xlsx', size: 2048, fetchedAt: '' }],
    }
  }
  if (method === 'contactSession.get') {
    return params.email === 'a@b.com'
      ? { sessionId: 'sess_1', workspaceId: 'ws_1', title: 'A', updatedAt: '2026-11-04T12:00:00Z' }
      : { sessionId: '', workspaceId: '', title: '', updatedAt: '' }
  }
  if (method === 'contactSession.set') {
    return {
      sessionId: params.sessionId,
      workspaceId: params.workspaceId ?? '',
      title: params.title ?? '',
      updatedAt: '2026-11-04T12:00:00Z',
    }
  }
  if (method === 'message.list') {
    return {
      total: 1,
      messages: [
        {
          id: 'msg_1',
          accountId: 'acc_1',
          folder: 'INBOX',
          uid: 10,
          subject: '合同确认',
          fromAddr: 'a@b.com',
          toAddr: 'me@b.com',
          date: '2026-11-04T09:12:00Z',
          snippet: '正文前 200 字',
          seen: false,
        },
      ],
    }
  }
  if (method === 'message.get') {
    if (params && params.id === 'missing') {
      const error = new Error('message not found')
      error.code = 'NOT_FOUND'
      throw error
    }
    return {
      id: params.id,
      accountId: 'acc_1',
      folder: 'INBOX',
      uid: 10,
      subject: '合同确认',
      fromAddr: 'a@b.com',
      toAddr: 'me@b.com',
      ccAddr: '',
      date: '2026-11-04T09:12:00Z',
      snippet: '正文前 200 字',
      text: '这是一封测试邮件的正文。',
      html: '',
      size: 4812,
      seen: false,
      threadId: 'acc_1:<root@example>',
    }
  }
  const error = new Error(`unknown method ${method}`)
  error.code = 'UNKNOWN_METHOD'
  throw error
}

// ---- 假 ctx ---------------------------------------------------------------
const tools = new Map()
const routes = new Map()
const intervals = []

const ctx = {  effect: (fn) => {
    const result = fn()
    return () => (typeof result === 'function' ? result() : undefined)
  },
  tools: {
    register: (definition) => {
      tools.set(definition.name, definition)
      return () => tools.delete(definition.name)
    },
  },
  webServer: {
    register: (route) => {
      routes.set(route.path, route)
      return () => routes.delete(route.path)
    },
  },
  timer: {
    interval: (callback, ms) => {
      intervals.push({ callback, ms })
      return () => {}
    },
  },
  subprocess: {
    spawn: () => makeFakeChild(),
  },
  configEditor: {
    entries: () => [
      { id: 'dsh-mail-plugin', name: 'dsh-mail-plugin' },
      { id: 'include:approval', name: '@deepseek-ai/dsh-user-approval', config: { policy: approvalPolicyConfig } },
    ],
    edit: async (entry, change) => {
      configEdits.push({ entry, next: change({ syncIntervalMs: 300_000 }) })
    },
  },
  approval: {
    request: async (request) => {
      approvalRequests.push(request)
      return approvalOutcome
    },
  },
  logger: { info: () => {} },
}

check(inject.includes('tools') && inject.includes('webServer'), 'inject 必须声明工具与路由')
check(inject.includes('subprocess'), 'inject 必须声明 subprocess（要拉 Core 子进程）')

apply(ctx, { dataDir: 'C:\\data', coreDir: 'C:\\core', syncIntervalMs: 1000 })

// 让握手（异步流）跑完
await new Promise((resolve) => setTimeout(resolve, 50))

// ---- 协议接线 -------------------------------------------------------------
check(received.length > 0, 'Core 子进程没有收到任何请求')
check(received[0] && received[0].method === 'core.handshake', `第一条请求应是 core.handshake，实际 ${received[0] && received[0].method}`)

// ---- 工具 ----------------------------------------------------------------
for (const toolName of ['mail_status', 'mail_accounts', 'mail_folders']) {
  check(tools.has(toolName), `没有注册工具 ${toolName}`)
}

const statusTool = tools.get('mail_status')
if (statusTool) {
  check(statusTool.parameters.type === 'object', 'mail_status parameters 应为 object schema')
  const value = await statusTool.execute({}, {})
  check(value.core.state === 'ready', `core 应为 ready，实际 ${value.core.state}`)
  check(value.core.version === '0.1.0', '应透传 Core 版本')
  check(value.counters.messages === 42, '应透传 Core 计数')
  const blocks = statusTool.output.render({}, value)
  check(Array.isArray(blocks) && blocks[0].type === 'text', 'mail_status.render 必须返回 ContentBlock[]')
}

const accountsTool = tools.get('mail_accounts')
if (accountsTool) {
  const value = await accountsTool.execute({}, {})
  check(value.available === true, 'mail_accounts 应可用')
  check(value.accounts.length === 1, 'mail_accounts 应返回 1 个账号')
  check(!JSON.stringify(value).includes('password'), 'mail_accounts 不得返回密码字段')
}

const foldersTool = tools.get('mail_folders')
if (foldersTool) {
  const value = await foldersTool.execute({ accountId: 'acc_1' }, {})
  check(value.available === true, 'mail_folders 应可用')
  check(value.folders.length === 1 && value.folders[0].name === 'INBOX', 'mail_folders 应返回 INBOX')
}

// ---- 同步与消息工具 -------------------------------------------------------
const syncTool = tools.get('mail_sync')
if (syncTool) {
  const value = await syncTool.execute({ accountId: 'acc_1' }, {})
  check(value.available === true, 'mail_sync 应可用')
  check(value.stored === 2 && value.fetched === 2, 'mail_sync 应透传 Core 的计数')
  check(syncTool.isConcurrencySafe() === false, 'mail_sync 不应声明并发安全')
  const blocks = syncTool.output.render({}, value)
  check(Array.isArray(blocks) && blocks[0].type === 'text', 'mail_sync.render 必须返回 ContentBlock[]')
}

const messagesTool = tools.get('mail_messages')
if (messagesTool) {
  const value = await messagesTool.execute({}, {})
  check(value.available === true && value.total === 1, 'mail_messages 应返回 1 封邮件')
  check(value.messages[0].subject === '合同确认', 'mail_messages 应带主题')
}

const messageTool = tools.get('mail_message')
if (messageTool) {
  const value = await messageTool.execute({ id: 'msg_1' }, {})
  check(value.available === true, 'mail_message 应可用')
  check(value.text.includes('测试邮件'), 'mail_message 应返回正文')
  const missing = await messageTool.execute({ id: 'missing' }, {})
  check(missing.available === false, '未知 id 应返回 available=false 而不是抛错')
}

// ---- HTTP 守门 ------------------------------------------------------------
function makeRes() {
  const captured = { status: null, headers: null, body: null }
  return {
    captured,
    writeHead(status, headers) {
      captured.status = status
      captured.headers = headers
    },
    end(body) {
      captured.body = body
    },
  }
}

function makeReq({ host = '127.0.0.1:52452', method = 'GET', origin, site, body } = {}) {
  const headers = { host }
  if (origin !== undefined) headers.origin = origin
  if (site !== undefined) headers['sec-fetch-site'] = site
  const req = Readable.from(body === undefined ? [] : [Buffer.from(body)])
  req.headers = headers
  req.method = method
  return req
}

async function invoke(path, options) {
  const route = routes.get(path)
  if (!route) {
    failures.push(`没有注册路由 ${path}`)
    return null
  }
  const req = makeReq(options)
  const res = makeRes()
  await route.handler(req, res)
  return res.captured
}

for (const path of ['/api/dsh-mail/ping', '/api/dsh-mail/status', '/api/dsh-mail/accounts/add', '/api/dsh-mail/accounts/test']) {
  check(routes.has(path), `没有注册路由 ${path}`)
}

const okPing = await invoke('/api/dsh-mail/ping', { method: 'GET' })
check(okPing && okPing.status === 200, `本机 GET ping 应为 200，实际 ${okPing && okPing.status}`)

const crossSite = await invoke('/api/dsh-mail/ping', { method: 'GET', site: 'cross-site' })
check(crossSite && crossSite.status === 403, `跨站请求应 403，实际 ${crossSite && crossSite.status}`)

const crossOrigin = await invoke('/api/dsh-mail/status', { method: 'POST', origin: 'http://evil.example', body: '{}' })
check(crossOrigin && crossOrigin.status === 403, `跨源请求应 403，实际 ${crossOrigin && crossOrigin.status}`)

const remoteHost = await invoke('/api/dsh-mail/ping', { method: 'GET', host: '10.0.0.5:52452' })
check(remoteHost && remoteHost.status === 403, `非 loopback Host 应 403，实际 ${remoteHost && remoteHost.status}`)

const badJson = await invoke('/api/dsh-mail/status', { method: 'POST', body: '{oops' })
check(badJson && badJson.status === 400, `坏 JSON 应 400，实际 ${badJson && badJson.status}`)

const okStatus = await invoke('/api/dsh-mail/status', { method: 'POST', body: '{}' })
check(okStatus && okStatus.status === 200, `合法 POST status 应为 200，实际 ${okStatus && okStatus.status}`)
if (okStatus && okStatus.body) {
  const parsed = JSON.parse(okStatus.body)
  check(parsed.ok === true && parsed.value.plugin === name, 'status 响应体结构不对')
  check(parsed.value.core && parsed.value.core.state === 'ready', 'status 应包含 core.state')
  check(!JSON.stringify(parsed.value).includes('password'), 'status 不得泄露密码')
}

// 账号写入：缺密码 / 端口非法必须被路由拒绝（不能打到 Core）
const beforeAdd = received.length
const missingPassword = await invoke('/api/dsh-mail/accounts/add', {
  method: 'POST',
  body: JSON.stringify({ address: 'a@b.com', imapHost: 'imap.b.com', username: 'a' }),
})
check(missingPassword && missingPassword.status === 400, `缺少密码应 400，实际 ${missingPassword && missingPassword.status}`)
check(received.length === beforeAdd, '参数校验失败时不应把请求打到 Core')

const badPort = await invoke('/api/dsh-mail/accounts/add', {
  method: 'POST',
  body: JSON.stringify({
    address: 'a@b.com',
    imapHost: 'imap.b.com',
    imapPort: 99999,
    username: 'a',
    password: 'x',
  }),
})
check(badPort && badPort.status === 400, `非法端口应 400，实际 ${badPort && badPort.status}`)

const goodAdd = await invoke('/api/dsh-mail/accounts/add', {
  method: 'POST',
  body: JSON.stringify({
    address: 'a@b.com',
    imapHost: 'imap.b.com',
    imapPort: 993,
    imapSecurity: 'ssl',
    username: 'a@b.com',
    password: 'super-secret',
  }),
})
check(goodAdd && goodAdd.status === 200, `合法添加应为 200，实际 ${goodAdd && goodAdd.status}`)
const addRequest = received.find((request) => request.method === 'account.add')
check(Boolean(addRequest), '合法添加应把 account.add 打到 Core')
check(addRequest && addRequest.params.password === 'super-secret', '密码应原样传给 Core（由 Core 加密）')
check(addRequest && addRequest.params.imapPort === 993, '端口应转成数字')
check(addRequest && addRequest.params.protocol === 'imap', '未指定协议时应默认 imap')

// POP3：端口按协议与加密方式推断（ssl → 995），SMTP 未填时也要带默认端口
const pop3Add = await invoke('/api/dsh-mail/accounts/add', {
  method: 'POST',
  body: JSON.stringify({
    address: 'p@b.com',
    protocol: 'pop3',
    imapHost: 'pop3.b.com',
    username: 'p@b.com',
    password: 'x',
  }),
})
check(pop3Add && pop3Add.status === 200, `POP3 添加应为 200，实际 ${pop3Add && pop3Add.status}`)
const pop3Request = received.filter((request) => request.method === 'account.add').at(-1)
check(pop3Request && pop3Request.params.protocol === 'pop3', 'POP3 账号应带上 protocol=pop3')
check(pop3Request && pop3Request.params.imapPort === 995, `POP3 ssl 端口应推断为 995，实际 ${pop3Request && pop3Request.params.imapPort}`)
check(pop3Request && pop3Request.params.smtpPort === 465, `SMTP ssl 端口应推断为 465，实际 ${pop3Request && pop3Request.params.smtpPort}`)

// 不填用户名时，默认用邮箱地址当登录名
const noUsername = await invoke('/api/dsh-mail/accounts/add', {
  method: 'POST',
  body: JSON.stringify({ address: 'no-user@b.com', imapHost: 'imap.b.com', password: 'x' }),
})
check(noUsername && noUsername.status === 200, `缺用户名应仍可添加，实际 ${noUsername && noUsername.status}`)
const noUsernameRequest = received.filter((request) => request.method === 'account.add').at(-1)
check(
  noUsernameRequest && noUsernameRequest.params.username === 'no-user@b.com',
  `用户名应回落到邮箱地址，实际 ${noUsernameRequest && noUsernameRequest.params.username}`,
)

const badProtocol = await invoke('/api/dsh-mail/accounts/add', {
  method: 'POST',
  body: JSON.stringify({
    address: 'x@b.com',
    protocol: 'exchange',
    imapHost: 'x.b.com',
    username: 'x',
    password: 'x',
  }),
})
check(badProtocol && badProtocol.status === 400, `非法协议应 400，实际 ${badProtocol && badProtocol.status}`)

// Core 活着但调用失败时，错误码必须原样透出（不能一律说成 CORE_DOWN）
const imapFailure = await invoke('/api/dsh-mail/accounts/test', {
  method: 'POST',
  body: JSON.stringify({ id: 'acc_broken' }),
})
check(imapFailure && imapFailure.status === 502, `IMAP 失败应 502，实际 ${imapFailure && imapFailure.status}`)
check(
  imapFailure && imapFailure.body && JSON.parse(imapFailure.body).code === 'IMAP_ERROR',
  'IMAP 失败应保留 IMAP_ERROR 码，而不是 CORE_DOWN',
)

// ---- 同步与消息路由 -------------------------------------------------------
const messagesRoute = await invoke('/api/dsh-mail/messages', { method: 'POST', body: '{}' })
check(messagesRoute && messagesRoute.status === 200, `messages 路由应 200，实际 ${messagesRoute && messagesRoute.status}`)
if (messagesRoute && messagesRoute.body) {
  const parsed = JSON.parse(messagesRoute.body)
  check(parsed.value.total === 1 && parsed.value.messages.length === 1, 'messages 路由应透传 Core 结果')
}

const messageRoute = await invoke('/api/dsh-mail/message', {
  method: 'POST',
  body: JSON.stringify({ id: 'msg_1' }),
})
check(messageRoute && messageRoute.status === 200, `message 路由应 200，实际 ${messageRoute && messageRoute.status}`)

const missingMessage = await invoke('/api/dsh-mail/message', {
  method: 'POST',
  body: JSON.stringify({ id: 'missing' }),
})
check(missingMessage && missingMessage.status === 404, `未知 message 应 404，实际 ${missingMessage && missingMessage.status}`)
check(
  missingMessage && JSON.parse(missingMessage.body).code === 'NOT_FOUND',
  'NOT_FOUND 应原样透出',
)

// 重建本地缓存：/sync/reset 与 /sync?reset=true 都应先清再拉
const resetRoute = await invoke('/api/dsh-mail/sync/reset', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1' }),
})
check(resetRoute && resetRoute.status === 200, `sync/reset 应 200，实际 ${resetRoute && resetRoute.status}`)
check(
  Boolean(received.find((request) => request.method === 'sync.reset')),
  'sync/reset 应把请求打到 Core',
)

const resetThenSync = await invoke('/api/dsh-mail/sync', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1', reset: true }),
})
check(resetThenSync && resetThenSync.status === 200, `reset 后同步应 200，实际 ${resetThenSync && resetThenSync.status}`)

// ---- 搜索 / 通讯录 / 模板路由 ---------------------------------------------
const searchRoute = await invoke('/api/dsh-mail/search', {
  method: 'POST',
  body: JSON.stringify({ query: '合同' }),
})
check(searchRoute && searchRoute.status === 200, `search 应 200，实际 ${searchRoute && searchRoute.status}`)
if (searchRoute && searchRoute.body) {
  const parsed = JSON.parse(searchRoute.body)
  check(parsed.value.total === 1 && parsed.value.messages.length === 1, 'search 应透传 Core 结果')
  check(parsed.value.messages[0].snippet.includes('«'), 'search 结果应带高亮片段')
}

const searchEmptyQuery = await invoke('/api/dsh-mail/search', { method: 'POST', body: JSON.stringify({ query: '  ' }) })
check(searchEmptyQuery && searchEmptyQuery.status === 400, `空 query 应 400，实际 ${searchEmptyQuery && searchEmptyQuery.status}`)

const contactsRoute = await invoke('/api/dsh-mail/contacts', { method: 'POST', body: '{}' })
check(contactsRoute && contactsRoute.status === 200, `contacts 应 200，实际 ${contactsRoute && contactsRoute.status}`)
if (contactsRoute && contactsRoute.body) {
  const parsed = JSON.parse(contactsRoute.body)
  check(parsed.value.contacts.length === 1 && parsed.value.contacts[0].name === '张三', 'contacts 应返回联系人')
}

const contactAddNoName = await invoke('/api/dsh-mail/contacts/add', {
  method: 'POST',
  body: JSON.stringify({ email: 'a@b.com' }),
})
check(contactAddNoName && contactAddNoName.status === 400, `缺姓名应 400，实际 ${contactAddNoName && contactAddNoName.status}`)

const contactAdd = await invoke('/api/dsh-mail/contacts/add', {
  method: 'POST',
  body: JSON.stringify({ name: '李四', email: 'l@b.com', company: 'CBE' }),
})
check(contactAdd && contactAdd.status === 200, `新增联系人应 200，实际 ${contactAdd && contactAdd.status}`)

const templatesRoute = await invoke('/api/dsh-mail/templates', { method: 'POST', body: '{}' })
check(templatesRoute && templatesRoute.status === 200, `templates 应 200，实际 ${templatesRoute && templatesRoute.status}`)

const templateAddNoName = await invoke('/api/dsh-mail/templates/add', {
  method: 'POST',
  body: JSON.stringify({ subject: 'x' }),
})
check(templateAddNoName && templateAddNoName.status === 400, `模板缺名称应 400，实际 ${templateAddNoName && templateAddNoName.status}`)

const foldersRoute = await invoke('/api/dsh-mail/folders', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1' }),
})
check(foldersRoute && foldersRoute.status === 200, `folders 应 200，实际 ${foldersRoute && foldersRoute.status}`)

// ---- 草稿 / 审批 / 执行 ---------------------------------------------------
const draftNoAccount = await invoke('/api/dsh-mail/drafts/save', {
  method: 'POST',
  body: JSON.stringify({ to: 'a@b.com', subject: 's', body: 'b' }),
})
check(draftNoAccount && draftNoAccount.status === 400, `草稿缺 accountId 应 400，实际 ${draftNoAccount && draftNoAccount.status}`)

const draftSave = await invoke('/api/dsh-mail/drafts/save', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1', to: 'a@b.com', subject: 's', body: 'b' }),
})
check(draftSave && draftSave.status === 200, `保存草稿应 200，实际 ${draftSave && draftSave.status}`)
if (draftSave && draftSave.body) {
  const parsed = JSON.parse(draftSave.body)
  check(parsed.value.id === 'drf_new' && parsed.value.contentHash === 'deadbeef', '草稿应返回 id 与 hash')
}

const approvalsRoute = await invoke('/api/dsh-mail/approvals', { method: 'POST', body: '{}' })
check(approvalsRoute && approvalsRoute.status === 200, `approvals 应 200，实际 ${approvalsRoute && approvalsRoute.status}`)

const approvalRequestNoDraft = await invoke('/api/dsh-mail/approvals/request', { method: 'POST', body: '{}' })
check(approvalRequestNoDraft && approvalRequestNoDraft.status === 400, `缺 draftId 应 400，实际 ${approvalRequestNoDraft && approvalRequestNoDraft.status}`)

// 批准与执行不应有 HTTP 入口（否则 Agent 可用命令行自我批准）
check(!routes.has('/api/dsh-mail/approvals/approve'), '不应存在 HTTP 批准入口')

const executeRoute = await invoke('/api/dsh-mail/actions/execute', {
  method: 'POST',
  body: JSON.stringify({ approvalId: 'apr_1' }),
})
check(executeRoute && executeRoute.status === 403, `HTTP 发送入口应 403，实际 ${executeRoute && executeRoute.status}`)
check(executeRoute && JSON.parse(executeRoute.body).code === 'FORBIDDEN', 'HTTP 发送入口应返回 FORBIDDEN')

// 发送只能经 Agent 工具 + 宿主审批
const sendTool = tools.get('mail_send_draft')
if (sendTool) {
  approvalOutcome = 'allowed-once'
  const approvedSend = await sendTool.execute({ draftId: 'drf_1' }, { agent: { id: 'sess_1' }, signal: new AbortController().signal })
  check(approvedSend.sent === true, `批准后应发送成功，实际 ${JSON.stringify(approvedSend)}`)
  check(approvedSend.messageId === '<msg@example>', '应返回 Message-ID')
  check(approvalRequests.length === 1, '应调用一次宿主审批')
  check(approvalRequests[0].toolName === 'mail_send_draft', '审批应标明来源工具')
  check(String(approvalRequests[0].displayReason.zh).includes('报价回复'), '审批文案应带上邮件主题')
  check(approveCalls[0]?.decidedVia === 'prompt', '审计应记录 decidedVia=prompt')
  check(approveCalls[0]?.decidedBy === 'user', '审计应记录 decidedBy=user')

  approvalOutcome = 'rejected'
  const rejectedSend = await sendTool.execute({ draftId: 'drf_1' }, { agent: { id: 'sess_1' }, signal: new AbortController().signal })
  check(rejectedSend.sent === false, '用户拒绝时不应发送')
  check(String(rejectedSend.reason).includes('rejected'), '应把拒绝原因带回来')

  // 策略为 never 时宿主会直接放行、不弹框，必须拒绝发送
  approvalPolicyConfig = 'never'
  const sendsBefore = approveCalls.length
  const neverSend = await sendTool.execute({ draftId: 'drf_1' }, { agent: { id: 'sess_1' }, signal: new AbortController().signal })
  check(neverSend.sent === false, 'policy=never 时不应发送')
  check(String(neverSend.reason).includes('never'), '应说明策略原因')
  check(approveCalls.length === sendsBefore, 'policy=never 时不应产生批准记录')
  approvalPolicyConfig = 'ask'
}

// ---- 线程 / 回溯 / 备份 -----------------------------------------------------
const threadsRoute = await invoke('/api/dsh-mail/threads', { method: 'POST', body: JSON.stringify({ folder: 'INBOX' }) })
check(threadsRoute && threadsRoute.status === 200, `threads 应 200，实际 ${threadsRoute && threadsRoute.status}`)
if (threadsRoute && threadsRoute.body) {
  const parsed = JSON.parse(threadsRoute.body)
  check(parsed.value.threads.length === 1, 'threads 应返回线程列表')
  check(parsed.value.threads[0].messageCount === 2, '线程应带消息数')
}

const threadNoId = await invoke('/api/dsh-mail/thread', { method: 'POST', body: '{}' })
check(threadNoId && threadNoId.status === 400, `thread 缺 threadId 应 400，实际 ${threadNoId && threadNoId.status}`)

const threadGet = await invoke('/api/dsh-mail/thread', { method: 'POST', body: JSON.stringify({ threadId: 'thr_1' }) })
check(threadGet && threadGet.status === 200, `thread 应 200，实际 ${threadGet && threadGet.status}`)

const backfillNoAccount = await invoke('/api/dsh-mail/history/backfill', { method: 'POST', body: JSON.stringify({ folder: 'INBOX' }) })
check(backfillNoAccount && backfillNoAccount.status === 400, `backfill 缺 accountId 应 400，实际 ${backfillNoAccount && backfillNoAccount.status}`)

const backfill = await invoke('/api/dsh-mail/history/backfill', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1', folder: 'INBOX', before: '2026-10-01T00:00:00Z', limit: 50 }),
})
check(backfill && backfill.status === 200, `backfill 应 200，实际 ${backfill && backfill.status}`)
if (backfill && backfill.body) {
  const parsed = JSON.parse(backfill.body)
  check(parsed.value.stored === 1, 'backfill 应返回 stored')
}

const backupRoute = await invoke('/api/dsh-mail/backup', { method: 'POST', body: '{}' })
check(backupRoute && backupRoute.status === 200, `backup 应 200，实际 ${backupRoute && backupRoute.status}`)
if (backupRoute && backupRoute.body) {
  const parsed = JSON.parse(backupRoute.body)
  check(String(parsed.value.path).includes('backups'), '备份路径应在 backups 目录')
  check(parsed.value.size > 0, '备份应返回文件大小')
}

const backupsRoute = await invoke('/api/dsh-mail/backups', { method: 'POST', body: '{}' })
check(backupsRoute && backupsRoute.status === 200, `backups 应 200，实际 ${backupsRoute && backupsRoute.status}`)
if (backupsRoute && backupsRoute.body) {
  const parsed = JSON.parse(backupsRoute.body)
  check(parsed.value.backups.length === 1, 'backups 应返回列表')
}

// ---- 删除邮件 -------------------------------------------------------------
const deleteNoId = await invoke('/api/dsh-mail/messages/delete', { method: 'POST', body: '{}' })
check(deleteNoId && deleteNoId.status === 400, `删信缺 id 应 400，实际 ${deleteNoId && deleteNoId.status}`)

const deleteToTrash = await invoke('/api/dsh-mail/messages/delete', {
  method: 'POST',
  body: JSON.stringify({ id: 'msg_1' }),
})
check(deleteToTrash && deleteToTrash.status === 200, `删信应 200，实际 ${deleteToTrash && deleteToTrash.status}`)
if (deleteToTrash && deleteToTrash.body) {
  const parsed = JSON.parse(deleteToTrash.body)
  check(parsed.value.deleted === true && parsed.value.trashed === true, '默认应移到已删除')
}

const deletePermanent = await invoke('/api/dsh-mail/messages/delete', {
  method: 'POST',
  body: JSON.stringify({ id: 'msg_1', permanent: true }),
})
if (deletePermanent && deletePermanent.body) {
  const parsed = JSON.parse(deletePermanent.body)
  check(parsed.value.trashed === false, 'permanent=true 应为永久删除')
}

// ---- 上下文工具：mail_current / mail_thread / mail_related ---------------
const uiStateRoute = await invoke('/api/dsh-mail/ui/state', {
  method: 'POST',
  body: JSON.stringify({ messageId: 'msg_1', accountId: 'acc_1', folder: 'INBOX', tab: 'inbox' }),
})
check(uiStateRoute && uiStateRoute.status === 200, `ui/state 应 200，实际 ${uiStateRoute && uiStateRoute.status}`)

const currentTool = tools.get('mail_current')
if (currentTool) {
  const current = await currentTool.execute({}, { agent: { id: 'sess_1' } })
  check(current.hasSelection === true, 'mail_current 应返回已选中')
  check(current.messageId === 'msg_1' && current.subject === '合同确认', 'mail_current 应带回邮件摘要')
  check(current.tab === 'inbox', 'mail_current 应带回页签')
}

const threadTool = tools.get('mail_thread')
if (threadTool) {
  const thread = await threadTool.execute({ messageId: 'msg_1' }, { agent: { id: 'sess_1' } })
  check(thread.available === true && thread.threadId === 'acc_1:<root@example>', 'mail_thread 应从 messageId 解析线程')
  check(thread.messages.length === 2, 'mail_thread 应返回线程消息')
}

const relatedTool = tools.get('mail_related')
if (relatedTool) {
  const related = await relatedTool.execute({ messageId: 'msg_1' }, { agent: { id: 'sess_1' } })
  check(related.available === true, `mail_related 应可用，实际 ${JSON.stringify(related.reason)}`)
  check(related.items.length > 0, 'mail_related 应给出相关邮件')
  check(
    related.items.some((item) => String(item.reason).includes('同一对话')),
    'mail_related 应标记同线程来源',
  )
  check(
    related.items.every((item) => item.reason),
    'mail_related 每条都要说明命中原因',
  )
}

// ---- 从已删除恢复 ---------------------------------------------------------
const restoreNoId = await invoke('/api/dsh-mail/messages/restore', { method: 'POST', body: '{}' })
check(restoreNoId && restoreNoId.status === 400, `恢复缺 id 应 400，实际 ${restoreNoId && restoreNoId.status}`)

const restoreRoute = await invoke('/api/dsh-mail/messages/restore', {
  method: 'POST',
  body: JSON.stringify({ id: 'msg_1' }),
})
check(restoreRoute && restoreRoute.status === 200, `恢复应 200，实际 ${restoreRoute && restoreRoute.status}`)
if (restoreRoute && restoreRoute.body) {
  const parsed = JSON.parse(restoreRoute.body)
  check(parsed.value.restored === true && parsed.value.folder === 'INBOX', '恢复默认回 INBOX')
}

// ---- 附件 ---------------------------------------------------------------
const attachmentsRoute = await invoke('/api/dsh-mail/attachments', {
  method: 'POST',
  body: JSON.stringify({ messageId: 'msg_1' }),
})
check(attachmentsRoute && attachmentsRoute.status === 200, `attachments 应 200，实际 ${attachmentsRoute && attachmentsRoute.status}`)
if (attachmentsRoute && attachmentsRoute.body) {
  const parsed = JSON.parse(attachmentsRoute.body)
  check(parsed.value.attachments[0].filename === '报价表.xlsx', '附件应带文件名')
  check(parsed.value.attachments[0].size === 2048, '附件应带大小')
}

const fetchNoId = await invoke('/api/dsh-mail/attachments/fetch', { method: 'POST', body: '{}' })
check(fetchNoId && fetchNoId.status === 400, `附件下载缺 id 应 400，实际 ${fetchNoId && fetchNoId.status}`)

const fetchRoute = await invoke('/api/dsh-mail/attachments/fetch', {
  method: 'POST',
  body: JSON.stringify({ id: 'att_1' }),
})
check(fetchRoute && fetchRoute.status === 200, `附件下载应 200，实际 ${fetchRoute && fetchRoute.status}`)
if (fetchRoute && fetchRoute.body) {
  const parsed = JSON.parse(fetchRoute.body)
  check(String(parsed.value.path).includes('attachments'), '下载后应返回本地路径')
  check(parsed.value.sha256 === 'abc123', '下载后应有 sha256')
}

const attachmentTool = tools.get('mail_attachments')
if (attachmentTool) {
  const listed = await attachmentTool.execute({ messageId: 'msg_1' }, { agent: { id: 'sess_1' } })
  check(listed.available === true && listed.attachments.length === 1, 'mail_attachments 应列出附件')
  check(listed.attachments[0].fetched === false, '未下载的附件 fetched 应为 false')
}

// ---- 待办与总结 -----------------------------------------------------------
const todoRoute = await invoke('/api/dsh-mail/todo', { method: 'POST', body: JSON.stringify({ state: 'pending' }) })
check(todoRoute && todoRoute.status === 200, `todo 应 200，实际 ${todoRoute && todoRoute.status}`)
if (todoRoute && todoRoute.body) {
  const parsed = JSON.parse(todoRoute.body)
  check(parsed.value.todos.length === 1 && parsed.value.todos[0].hasSummary === true, 'todo 应返回事项与总结标记')
}

const todoSetNoId = await invoke('/api/dsh-mail/todo/set', { method: 'POST', body: JSON.stringify({ state: 'done' }) })
check(todoSetNoId && todoSetNoId.status === 400, `todo/set 缺 id 应 400，实际 ${todoSetNoId && todoSetNoId.status}`)

const contextNoId = await invoke('/api/dsh-mail/summary/context', { method: 'POST', body: '{}' })
check(contextNoId && contextNoId.status === 400, `summary/context 缺 id 应 400，实际 ${contextNoId && contextNoId.status}`)

const summaryRoute = await invoke('/api/dsh-mail/summary', { method: 'POST', body: JSON.stringify({ messageId: 'msg_1' }) })
check(summaryRoute && summaryRoute.status === 200, `summary 应 200，实际 ${summaryRoute && summaryRoute.status}`)

const triageTool = tools.get('mail_triage_context')
if (triageTool) {
  const triage = await triageTool.execute({ messageId: 'msg_1' }, { agent: { id: 'sess_1' } })
  check(triage.available === true && triage.hasHistory === true, 'mail_triage_context 应标记 hasHistory')
  check(triage.related.length === 1 && triage.related[0].reason, '相关邮件应带命中原因')
  check(triage.attachments.length === 1, '应带出附件')
}

const summarySaveTool = tools.get('mail_summary_save')
if (summarySaveTool) {
  const badCategory = await summarySaveTool.execute({ messageId: 'msg_1', category: '乱写的', summary: 'x' }, { agent: { id: 'sess_1' } })
  check(badCategory.available === false && String(badCategory.reason).includes('category'), '非法分类应拒绝')
  check(Array.isArray(badCategory.categories) && badCategory.categories.length === 8, '应返回 8 个固定分类')

  const saved = await summarySaveTool.execute({ messageId: 'msg_1', category: '合同协议', summary: '总结内容' }, { agent: { id: 'sess_1' } })
  check(saved.available === true && saved.id === 'sum_1', '合法分类应保存成功')
}

const markDoneTool = tools.get('mail_mark_done')
if (markDoneTool) {
  const done = await markDoneTool.execute({ messageId: 'msg_1' }, { agent: { id: 'sess_1' } })
  check(done.available === true && done.state === 'done', 'mail_mark_done 应把事项标为 done')
}

// ---- 邮件工作区与联系人会话 -----------------------------------------------
const workspaceRoute = await invoke('/api/dsh-mail/config/workspace', { method: 'POST', body: '{}' })
check(workspaceRoute && workspaceRoute.status === 200, `config/workspace 应 200，实际 ${workspaceRoute && workspaceRoute.status}`)
if (workspaceRoute && workspaceRoute.body) {
  const parsed = JSON.parse(workspaceRoute.body)
  check(String(parsed.value.path).includes('workspace'), '默认工作区路径应以 workspace 结尾')
}

const workspaceSet = await invoke('/api/dsh-mail/config/workspace/set', {
  method: 'POST',
  body: JSON.stringify({ workspaceId: 'ws_test' }),
})
check(workspaceSet && workspaceSet.status === 200, `设置工作区应 200，实际 ${workspaceSet && workspaceSet.status}`)

const lookupRoute = await invoke('/api/dsh-mail/sessions/lookup', {
  method: 'POST',
  body: JSON.stringify({ email: 'a@b.com' }),
})
check(lookupRoute && lookupRoute.status === 200, `sessions/lookup 应 200，实际 ${lookupRoute && lookupRoute.status}`)
if (lookupRoute && lookupRoute.body) {
  const parsed = JSON.parse(lookupRoute.body)
  check(parsed.value.sessionId === 'sess_1', '已绑定邮箱应返回会话 id')
}

const bindNoEmail = await invoke('/api/dsh-mail/sessions/bind', {
  method: 'POST',
  body: JSON.stringify({ sessionId: 'sess_1' }),
})
check(bindNoEmail && bindNoEmail.status === 400, `绑定缺 email 应 400，实际 ${bindNoEmail && bindNoEmail.status}`)

const bindRoute = await invoke('/api/dsh-mail/sessions/bind', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1', email: 'New@Example.com', sessionId: 'sess_2', workspaceId: 'ws_1', title: 'New' }),
})
check(bindRoute && bindRoute.status === 200, `绑定应 200，实际 ${bindRoute && bindRoute.status}`)

const statuses = await invoke('/api/dsh-mail/status', { method: 'POST', body: '{}' })
if (statuses && statuses.body) {
  const parsed = JSON.parse(statuses.body)
  check(parsed.value.approvals.pending.length === 1, 'status 应带待审批列表')
  check(parsed.value.approvals.pending[0].subject === '报价回复', '待审批应补上草稿主题')
  check(parsed.value.counters.pendingApprovals === 1, 'counters.pendingApprovals 应与待审批数一致')
  check(!JSON.stringify(parsed.value).includes('password'), 'status 不得泄露密码')
}

const syncRoute = await invoke('/api/dsh-mail/sync', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1' }),
})
check(syncRoute && syncRoute.status === 200, `sync 路由应 200，实际 ${syncRoute && syncRoute.status}`)

const syncAllRoute = await invoke('/api/dsh-mail/sync/all', { method: 'POST', body: '{}' })
check(syncAllRoute && syncAllRoute.status === 200, `sync/all 应 200，实际 ${syncAllRoute && syncAllRoute.status}`)
if (syncAllRoute && syncAllRoute.body) {
  const parsed = JSON.parse(syncAllRoute.body)
  check(parsed.value.skipped === false, 'sync/all 应真正执行')
  check(parsed.value.accounts.length === 5, `sync/all 应同步 5 个文件夹，实际 ${parsed.value.accounts.length}`)
  const foldersSynced = parsed.value.accounts.map((entry) => entry.folder)
  for (const expected of ['INBOX', '已发送', '草稿', '已删除邮件', '垃圾邮件']) {
    check(foldersSynced.includes(expected), `sync/all 应覆盖 ${expected}`)
  }
  check(parsed.value.accounts.every((entry) => entry.stored === 2), 'sync/all 应透传 stored 计数')
}

const syncBadLimit = await invoke('/api/dsh-mail/sync', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1', limit: 999 }),
})
check(syncBadLimit && syncBadLimit.status === 400, `limit 越界应 400，实际 ${syncBadLimit && syncBadLimit.status}`)

const syncNoAccount = await invoke('/api/dsh-mail/sync', { method: 'POST', body: '{}' })
check(syncNoAccount && syncNoAccount.status === 400, `缺 accountId 应 400，实际 ${syncNoAccount && syncNoAccount.status}`)

// ---- 插件配置（自动同步间隔）----------------------------------------------
const configRoute = await invoke('/api/dsh-mail/config', { method: 'POST', body: '{}' })
check(configRoute && configRoute.status === 200, `config 应 200，实际 ${configRoute && configRoute.status}`)
if (configRoute && configRoute.body) {
  const parsed = JSON.parse(configRoute.body)
  check(parsed.value.syncIntervalMs === 60_000, '配置 1000ms 应被抬到下限 60000ms')
  check(parsed.value.syncIntervalMinutes === 1, '应换算成 1 分钟')
}

const setInterval1 = await invoke('/api/dsh-mail/config/sync-interval', {
  method: 'POST',
  body: JSON.stringify({ minutes: 3 }),
})
check(setInterval1 && setInterval1.status === 200, `设置间隔应 200，实际 ${setInterval1 && setInterval1.status}`)
check(configEdits.some((entry) => entry.next && entry.next.syncIntervalMs === 180_000), '应把 3 分钟换算成 180000ms 写回配置')

const setZero = await invoke('/api/dsh-mail/config/sync-interval', {
  method: 'POST',
  body: JSON.stringify({ minutes: 0 }),
})
check(setZero && setZero.status === 200, `0 应允许（关闭自动同步），实际 ${setZero && setZero.status}`)
check(configEdits.some((entry) => entry.next && entry.next.syncIntervalMs === 0), '0 分钟应写回 syncIntervalMs=0')

const setNegative = await invoke('/api/dsh-mail/config/sync-interval', {
  method: 'POST',
  body: JSON.stringify({ minutes: -1 }),
})
check(setNegative && setNegative.status === 400, `负数应 400，实际 ${setNegative && setNegative.status}`)

const setFraction = await invoke('/api/dsh-mail/config/sync-interval', {
  method: 'POST',
  body: JSON.stringify({ minutes: 1.5 }),
})
check(setFraction && setFraction.status === 400, `小数应 400，实际 ${setFraction && setFraction.status}`)

// ---- 定时器 ---------------------------------------------------------------
check(intervals.length === 1, `应注册 1 个心跳定时器，实际 ${intervals.length}`)
check(intervals[0] && intervals[0].ms === 60_000, `心跳间隔应被下限抬到 60000，实际 ${intervals[0] && intervals[0].ms}`)

// ---- 报告 -----------------------------------------------------------------
if (failures.length > 0) {
  console.error('FAIL')
  for (const message of failures) console.error(` - ${message}`)
  process.exit(1)
}

console.log('OK')
console.log(`  工具: ${[...tools.keys()].join(', ')}`)
console.log(`  路由: ${[...routes.keys()].join(', ')}`)
console.log(`  RPC: 握手 + ${received.length} 条请求（含 account.add 参数转换）`)
console.log(`  守门: 跨源 / 跨站 / 非 loopback / 坏 JSON / 缺密码 / 非法端口 全部拒绝`)
console.log(`  定时器: ${intervals[0].ms}ms（配置 1000ms 被抬到下限）`)
