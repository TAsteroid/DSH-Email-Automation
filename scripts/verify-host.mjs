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

const ctx = {
  effect: (fn) => {
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

const syncRoute = await invoke('/api/dsh-mail/sync', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1' }),
})
check(syncRoute && syncRoute.status === 200, `sync 路由应 200，实际 ${syncRoute && syncRoute.status}`)

const syncBadLimit = await invoke('/api/dsh-mail/sync', {
  method: 'POST',
  body: JSON.stringify({ accountId: 'acc_1', limit: 999 }),
})
check(syncBadLimit && syncBadLimit.status === 400, `limit 越界应 400，实际 ${syncBadLimit && syncBadLimit.status}`)

const syncNoAccount = await invoke('/api/dsh-mail/sync', { method: 'POST', body: '{}' })
check(syncNoAccount && syncNoAccount.status === 400, `缺 accountId 应 400，实际 ${syncNoAccount && syncNoAccount.status}`)

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
