/**
 * dsh-mail-plugin — Host half
 *
 * 职责：
 *   1. 向 DSH Agent 注册只读邮件工具
 *   2. 注册本机 HTTP 通道 /api/dsh-mail/*（UI 的唯一数据通道，自己做同源守门）
 *   3. 用 ctx.subprocess 拉起并托管 Python Mail Core（stdio JSON-RPC，见 docs/IPC_PROTOCOL.md）
 *
 * 账号与密码只经 HTTP 写入（用户操作），**不提供接收口令的 Agent 工具**；
 * 密码在 Core 侧用 Windows DPAPI 加密后落库，插件进程不保存。
 */

import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createCoreClient } from './core-client.js'

export const name = 'dsh-mail-plugin'

/** 硬依赖：缺任何一个就不会激活，避免半死状态。 */
export const inject = ['tools', 'webServer', 'timer', 'subprocess']

/**
 * 配置从 profile 的 patch 行读取，这里做防御式解析（缺省/类型不对都回落到默认）。
 *
 * 注意：暂时没有 `export const Config`。宿主校验 Config 要用 @deepseek-ai/schemastery，
 * 而本地目录安装的插件（pnpm link）在真实路径下解析不到 profile 的 node_modules，
 * 顶层 import 会直接让插件加载失败。等打包成正式依赖后再补 schema。
 */

const API_PREFIX = '/api/dsh-mail'
const MIN_INTERVAL_MS = 60_000
const SECURITY_MODES = new Set(['ssl', 'starttls', 'none'])
const PROTOCOLS = new Set(['imap', 'pop3'])

/** 端口按「协议 × 加密方式」推断，与 Core 侧保持一致。 */
const PORT_DEFAULTS = {
  imap: { ssl: 993, starttls: 143, none: 143 },
  pop3: { ssl: 995, starttls: 110, none: 110 },
  smtp: { ssl: 465, starttls: 587, none: 25 },
}

const here = dirname(fileURLToPath(import.meta.url))
const packageRoot = dirname(here)
const workspaceRoot = dirname(packageRoot)
const DEFAULT_CORE_DIR = join(workspaceRoot, 'mail-core')
const DEFAULT_DATA_DIR = join(workspaceRoot, 'data', 'mail')

// ---------------------------------------------------------------------------
// 路径与解释器
// ---------------------------------------------------------------------------

function pickConfigured(value, fallback) {
  const text = typeof value === 'string' ? value.trim() : ''
  return text.length > 0 ? text : fallback
}

/** DSH 的 Python 装在 $DSH_HOME/dsh-runtimes 下，系统里没有 python。 */
function resolvePython(config) {
  const explicit = typeof config?.pythonPath === 'string' ? config.pythonPath.trim() : ''
  if (explicit.length > 0) return explicit

  const candidates = []
  if (process.env.DSH_HOME) {
    candidates.push(join(process.env.DSH_HOME, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'))
  }
  candidates.push(join(homedir(), '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'))

  for (const candidate of candidates) {
    try {
      if (existsSync(candidate)) return candidate
    } catch {
      // 探测失败就继续下一个候选
    }
  }
  return 'python'
}

function syncIntervalMs(config) {
  const raw = Number(config?.syncIntervalMs)
  return Number.isFinite(raw) && raw > 0 ? Math.max(MIN_INTERVAL_MS, raw) : 300_000
}

// ---------------------------------------------------------------------------
// HTTP 工具
// ---------------------------------------------------------------------------

function writeJson(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' })
  res.end(JSON.stringify(body))
}

const LOOPBACK_HOST = /^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i

/** webServer 不做鉴权，这里自己守：只接受本机同源。 */
function rejectReason(req) {
  const host = String(req.headers.host ?? '')
  if (!LOOPBACK_HOST.test(host)) return 'forbidden host'
  if (req.headers['sec-fetch-site'] === 'cross-site') return 'cross-site request'
  const origin = req.headers.origin
  if (origin !== undefined && origin !== 'null') {
    let originHost
    try {
      originHost = new URL(origin).host
    } catch {
      return 'bad origin'
    }
    if (originHost !== host) return 'forbidden origin'
  }
  return undefined
}

const MAX_BODY_BYTES = 64 * 1024

async function readJsonBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) throw new Error('body too large')
    chunks.push(chunk)
  }
  const text = Buffer.concat(chunks).toString('utf8').trim()
  if (text.length === 0) return {}
  return JSON.parse(text)
}

function requireText(value, field) {
  if (typeof value !== 'string' || value.trim().length === 0) throw new Error(`${field} is required`)
  return value.trim()
}

function requirePort(value, field, fallback) {
  if (value === undefined || value === null || value === '') return fallback
  const port = Number(value)
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`${field} must be a port number`)
  return port
}

function normalizeSecurity(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback
  const mode = String(value).toLowerCase()
  if (!SECURITY_MODES.has(mode)) throw new Error('security must be one of ssl / starttls / none')
  return mode
}

function normalizeProtocol(value, fallback = 'imap') {
  if (value === undefined || value === null || value === '') return fallback
  const protocol = String(value).toLowerCase()
  if (!PROTOCOLS.has(protocol)) throw new Error('protocol must be imap or pop3')
  return protocol
}

// ---------------------------------------------------------------------------
// 插件
// ---------------------------------------------------------------------------

export function apply(ctx, config) {
  const log = (line) => ctx.logger?.info?.(`[${name}] ${line}`)
  const startedAt = new Date().toISOString()
  const coreDir = pickConfigured(config?.coreDir, DEFAULT_CORE_DIR)
  const dataDir = pickConfigured(config?.dataDir, DEFAULT_DATA_DIR)
  const python = resolvePython(config)

  // ctx.subprocess 要求显式 stdio；stdio 三个流都要 pipe，协议走 stdin/stdout
  const spawnCore = (spec) =>
    ctx.subprocess.spawn({
      ...spec,
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' },
      graceMs: 5000,
    })

  const coreOptions = () => ({
    spawn: spawnCore,
    argv: [python, '-m', 'mail_core', '--data-dir', dataDir],
    cwd: coreDir,
    env: { ...process.env, PYTHONUNBUFFERED: '1', MAIL_CORE_DATA_DIR: dataDir },
    log,
  })

  let core = createCoreClient(coreOptions())

  let ready = false
  let readyError = null

  async function ensureCore() {
    if (ready) return
    if (core.state === 'failed') {
      // 失败后允许一次重试：先停掉残留句柄再拉起
      core.stop()
      core = createCoreClient(coreOptions())
    }
    await core.handshake()
    ready = true
    readyError = null
  }

  /** 统一把 Core 调用包成 { available, value | reason }，工具和路由共用。 */
  async function callCore(method, params) {
    try {
      await ensureCore()
      return { available: true, value: await core.request(method, params) }
    } catch (error) {
      ready = core.state === 'ready'
      readyError = { code: error?.code ?? 'INTERNAL', message: error?.message ?? String(error) }
      return { available: false, reason: readyError }
    }
  }

  async function snapshot() {
    const coreStatus = core.status()
    const accounts = ready ? await callCore('account.list') : { available: false, reason: readyError }
    const sync = ready ? await callCore('sync.status') : { available: false, reason: readyError }
    const counters = (sync.available && sync.value?.counters) || {
      accounts: 0,
      messages: 0,
      unread: 0,
      drafts: 0,
      pendingApprovals: 0,
      pendingAi: 0,
    }
    return {
      plugin: name,
      version: '0.1.0',
      phase: 'phase1',
      startedAt: startedAt,
      syncIntervalMs: syncIntervalMs(config),
      core: {
        state: coreStatus.state,
        python,
        coreDir,
        dataDir,
        version: coreStatus.info?.version ?? '',
        dbPath: coreStatus.info?.dbPath ?? '',
        error: coreStatus.lastError,
      },
      accounts: (accounts.available && accounts.value?.accounts) || [],
      accountsError: accounts.available ? null : accounts.reason,
      approvals: { pending: [], history: [] },
      counters,
      lastSyncAt: (sync.available && sync.value?.lastSyncAt) || '',
    }
  }

  // 1) 启动 Core（失败不阻塞插件，状态会上报给界面）
  ctx.effect(() => {
    void ensureCore().catch((error) => {
      ready = false
      readyError = { code: error?.code ?? 'INTERNAL', message: error?.message ?? String(error) }
      log(`core unavailable at startup: ${readyError.message}`)
    })
    return () => core.stop()
  }, 'mail: core process')

  // 2) Agent 工具（只读；账号写入只走 UI）
  ctx.effect(
    () =>
      ctx.tools.register({
        name: 'mail_status',
        description:
          'Mail plugin status: whether the local Mail Core (Python) is running, its version and database path, plus counters and configured accounts. Read-only; use it before any mailbox task.',
        parameters: { type: 'object', additionalProperties: false, properties: {} },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: {
              phase: { type: 'string' },
              core: {
                type: 'object',
                additionalProperties: false,
                properties: {
                  state: { type: 'string' },
                  version: { type: 'string' },
                  dbPath: { type: 'string' },
                  dataDir: { type: 'string' },
                  python: { type: 'string' },
                  coreDir: { type: 'string' },
                  error: { type: 'string' },
                },
                required: ['state'],
              },
              accounts: { type: 'array', items: { type: 'object' } },
              counters: { type: 'object' },
            },
            required: ['phase', 'core', 'accounts', 'counters'],
          },
          render(_args, value) {
            const lines = [
              `mail phase ${value.phase}`,
              `core ${value.core.state}${value.core.version ? ` v${value.core.version}` : ''}`,
              `db ${value.core.dbPath || '(not started)'}`,
              `accounts ${value.accounts.length}`,
              `messages ${value.counters.messages ?? 0}, unread ${value.counters.unread ?? 0}`,
            ]
            if (value.core.error) lines.push(`core error: ${value.core.error}`)
            return [{ type: 'text', text: lines.join('\n') }]
          },
        },
        timeoutMs: 30_000,
        isConcurrencySafe: () => true,
        async execute() {
          const value = await snapshot()
          return {
            phase: value.phase,
            core: { ...value.core, error: value.core.error ? value.core.error.message : '' },
            accounts: value.accounts,
            counters: value.counters,
          }
        },
      }),
    'mail: register mail_status tool',
  )

  ctx.effect(
    () =>
      ctx.tools.register({
        name: 'mail_accounts',
        description:
          'List the mailbox accounts configured in the mail plugin. Returns address, IMAP host/port and whether a credential is stored — never the password itself.',
        parameters: { type: 'object', additionalProperties: false, properties: {} },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: {
              available: { type: 'boolean' },
              reason: { type: 'string' },
              accounts: { type: 'array', items: { type: 'object' } },
            },
            required: ['available', 'accounts'],
          },
          render(_args, value) {
            if (!value.available) return [{ type: 'text', text: `mail core unavailable: ${value.reason ?? 'unknown'}` }]
            if (value.accounts.length === 0) return [{ type: 'text', text: 'no mailbox accounts configured' }]
            const lines = value.accounts.map(
              (account) =>
                `- ${account.address} (${account.id}) imap ${account.imapHost}:${account.imapPort} ${account.imapSecurity}${account.hasCredential ? '' : ' [no credential]'}`,
            )
            return [{ type: 'text', text: lines.join('\n') }]
          },
        },
        timeoutMs: 20_000,
        isConcurrencySafe: () => true,
        async execute() {
          const result = await callCore('account.list')
          if (!result.available) return { available: false, reason: result.reason.message, accounts: [] }
          return { available: true, reason: '', accounts: result.value?.accounts ?? [] }
        },
      }),
    'mail: register mail_accounts tool',
  )

  ctx.effect(
    () =>
      ctx.tools.register({
        name: 'mail_folders',
        description:
          'Connect to one configured mailbox over IMAP and list its folders. Use it to verify credentials and discover folder names before searching.',
        parameters: {
          type: 'object',
          additionalProperties: false,
          properties: { accountId: { type: 'string', description: 'Account id from mail_accounts' } },
          required: ['accountId'],
        },
        output: {
          schema: {
            type: 'object',
            additionalProperties: false,
            properties: {
              available: { type: 'boolean' },
              reason: { type: 'string' },
              folders: { type: 'array', items: { type: 'object' } },
            },
            required: ['available', 'folders'],
          },
          render(_args, value) {
            if (!value.available) return [{ type: 'text', text: `cannot list folders: ${value.reason ?? 'unknown'}` }]
            const names = value.folders.map((folder) => folder.name).join(', ')
            return [{ type: 'text', text: `folders (${value.folders.length}): ${names}` }]
          },
        },
        timeoutMs: 45_000,
        isConcurrencySafe: () => false,
        async execute(args) {
          const result = await callCore('account.test', { id: args.accountId })
          if (!result.available) return { available: false, reason: result.reason.message, folders: [] }
          return { available: true, reason: '', folders: result.value?.folders ?? [] }
        },
      }),
    'mail: register mail_folders tool',
  )

  // 3) 本机 HTTP 通道
  ctx.effect(() => {
    const disposers = []

    const route = (path, handle) => {
      disposers.push(
        ctx.webServer.register({
          kind: 'exact',
          path: `${API_PREFIX}${path}`,
          handler: async (req, res) => {
            const denied = rejectReason(req)
            if (denied) return writeJson(res, 403, { ok: false, code: 'FORBIDDEN', message: denied })
            if (req.method !== 'POST' && req.method !== 'GET') {
              return writeJson(res, 405, { ok: false, code: 'METHOD', message: 'GET or POST only' })
            }
            let body = {}
            if (req.method === 'POST') {
              try {
                body = await readJsonBody(req)
              } catch (error) {
                return writeJson(res, 400, {
                  ok: false,
                  code: 'BAD_BODY',
                  message: error instanceof Error ? error.message : String(error),
                })
              }
            }
            try {
              const value = await handle(body)
              return writeJson(res, 200, { ok: true, value })
            } catch (error) {
              const code = error?.code ?? 'BAD_REQUEST'
              const status = code === 'NOT_FOUND' ? 404 : code === 'BAD_REQUEST' ? 400 : code === 'CREDENTIAL_MISSING' ? 409 : 502
              return writeJson(res, status, { ok: false, code, message: error?.message ?? String(error) })
            }
          },
        }),
      )
    }

    // Core 明明活着、只是这个调用失败时，保留它自己的错误码（别一律说成 CORE_DOWN）
    const requireCoreResult = (result) => {
      if (result.available) return result.value
      const error = new Error(result.reason?.message ?? 'mail core unavailable')
      error.code = result.reason?.code ?? 'CORE_DOWN'
      throw error
    }

    route('/ping', async () => ({ pong: true, at: new Date().toISOString() }))
    route('/status', async () => snapshot())

    route('/accounts', async () => ({ accounts: requireCoreResult(await callCore('account.list'))?.accounts ?? [] }))

    route('/accounts/add', async (body) => {
      const password = requireText(body.password, 'password')
      const protocol = normalizeProtocol(body.protocol)
      const imapSecurity = normalizeSecurity(body.imapSecurity, 'ssl')
      const smtpSecurity = normalizeSecurity(body.smtpSecurity, 'ssl')
      const params = {
        address: requireText(body.address, 'address'),
        displayName: typeof body.displayName === 'string' ? body.displayName.trim() : '',
        protocol,
        imapHost: requireText(body.imapHost, 'imapHost'),
        imapPort: requirePort(body.imapPort, 'imapPort', PORT_DEFAULTS[protocol][imapSecurity]),
        imapSecurity,
        username: requireText(body.username, 'username'),
        password,
        smtpHost: typeof body.smtpHost === 'string' ? body.smtpHost.trim() : '',
        smtpPort: requirePort(body.smtpPort, 'smtpPort', PORT_DEFAULTS.smtp[smtpSecurity]),
        smtpSecurity,
      }
      const result = await callCore('account.add', params)
      return requireCoreResult(result)
    })

    route('/accounts/remove', async (body) => {
      const id = requireText(body.id, 'id')
      return requireCoreResult(await callCore('account.remove', { id }))
    })

    route('/accounts/test', async (body) => {
      const id = requireText(body.id, 'id')
      return requireCoreResult(await callCore('account.test', { id }))
    })

    return () => {
      for (const dispose of disposers) dispose()
    }
  }, 'mail: http bridge')

  // 4) 心跳：定期刷新 Core 状态，让界面能反映崩溃
  ctx.effect(() => {
    const tick = async () => {
      if (core.state === 'failed') {
        ready = false
        return
      }
      if (!ready) {
        try {
          await ensureCore()
        } catch {
          // 状态由 core 自己上报
        }
      }
    }
    void tick()
    return ctx.timer.interval(() => {
      void tick()
    }, syncIntervalMs(config))
  }, 'mail: heartbeat timer')

  log(`phase1 ready; python=${python}; coreDir=${coreDir}; dataDir=${dataDir}`)
}
