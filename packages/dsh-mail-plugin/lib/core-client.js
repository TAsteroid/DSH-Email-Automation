/**
 * Mail Core 客户端：拉起 Python 子进程，走 stdio 上的行分帧 JSON-RPC。
 *
 * 协议见 docs/IPC_PROTOCOL.md。本文件只关心「怎么把请求送到、把响应取回」，
 * 不关心任何邮件业务；业务方法名由调用方给。
 *
 * 设计要点：
 *   - spawn 是注入的，单元测试可以塞假句柄，不需要真的 Python
 *   - 一个请求一个 id，响应按 id 匹配；不假设响应顺序
 *   - 子进程退出时，所有在途请求统一失败，状态置 failed（不是 throw 到插件里）
 */

const PROTOCOL = 1

function coreError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}

/**
 * @param {object} options
 * @param {(spec: object) => any} options.spawn   返回 { stdin, stdout, stderr, done, terminate }
 * @param {string[]} options.argv                 完整命令行（含 python 与模块）
 * @param {string} options.cwd
 * @param {Record<string,string>} [options.env]
 * @param {(line: string) => void} [options.log]
 * @param {(state: string, error: Error|null) => void} [options.onState]
 * @param {number} [options.requestTimeoutMs]
 */
export function createCoreClient({ spawn, argv, cwd, env, log, onState, onEvent, requestTimeoutMs = 20000 }) {
  let handle = null
  let state = 'stopped'
  let lastError = null
  let stopping = false
  let seq = 0
  let buffer = ''
  let info = null
  const pending = new Map()

  function emit(next, error) {
    state = next
    lastError = error ?? null
    onState?.(state, lastError)
  }

  function settleAll(code, message) {
    for (const entry of pending.values()) {
      clearTimeout(entry.timer)
      entry.reject(coreError(code, message))
    }
    pending.clear()
  }

  function consumeLine(line) {
    const text = line.trim()
    if (text.length === 0) return
    let message
    try {
      message = JSON.parse(text)
    } catch {
      log?.(`core: 忽略非 JSON 输出: ${text.slice(0, 200)}`)
      return
    }
    const entry = pending.get(message.id)
    if (!entry) {
      // 无 id 的是 Core 主动推送的事件（如 IDLE 收到新邮件）
      if (message.event) onEvent?.(message)
      return
    }
    pending.delete(message.id)
    clearTimeout(entry.timer)
    if (message.ok === true) {
      entry.resolve(message.result)
      return
    }
    entry.reject(coreError(message.error?.code ?? 'INTERNAL', message.error?.message ?? 'mail core error'))
  }

  function attach(child) {
    child.stdout?.setEncoding?.('utf8')
    child.stdout?.on?.('data', (chunk) => {
      buffer += chunk
      let index = buffer.indexOf('\n')
      while (index >= 0) {
        consumeLine(buffer.slice(0, index))
        buffer = buffer.slice(index + 1)
        index = buffer.indexOf('\n')
      }
    })
    child.stderr?.setEncoding?.('utf8')
    child.stderr?.on?.('data', (chunk) => {
      for (const line of String(chunk).split('\n')) {
        const text = line.trim()
        if (text.length > 0) log?.(`core: ${text}`)
      }
    })
    child.done?.then(
      (outcome) => {
        handle = null
        settleAll('CORE_EXITED', `mail core exited (code=${outcome?.exitCode ?? 'unknown'})`)
        if (stopping) {
          stopping = false
          emit('stopped')
        } else {
          emit('failed', coreError('CORE_EXITED', `mail core exited (code=${outcome?.exitCode ?? 'unknown'})`))
        }
      },
      (error) => {
        handle = null
        settleAll('CORE_EXITED', `mail core failed: ${error?.message ?? error}`)
        if (stopping) {
          stopping = false
          emit('stopped')
        } else {
          emit('failed', coreError('CORE_EXITED', `mail core failed: ${error?.message ?? error}`))
        }
      },
    )
  }

  function start() {
    if (handle) return
    buffer = ''
    emit('starting')
    try {
      handle = spawn({ argv, cwd, env })
    } catch (error) {
      handle = null
      emit('failed', coreError('SPAWN_FAILED', `cannot spawn mail core: ${error?.message ?? error}`))
      return
    }
    attach(handle)
  }

  async function handshake({ timeoutMs = 30000 } = {}) {
    start()
    if (!handle) throw lastError ?? coreError('SPAWN_FAILED', 'mail core did not start')
    const result = await request('core.handshake', {}, { timeoutMs })
    if (result?.protocol !== PROTOCOL) {
      emit('failed', coreError('PROTOCOL', `unsupported protocol ${String(result?.protocol)}`))
      throw coreError('PROTOCOL', `mail core protocol ${String(result?.protocol)}, expected ${PROTOCOL}`)
    }
    info = result
    emit('ready')
    return result
  }

  function request(method, params = {}, { timeoutMs = requestTimeoutMs } = {}) {
    if (!handle) return Promise.reject(coreError('CORE_DOWN', 'mail core is not running'))
    const id = String(++seq)
    const payload = `${JSON.stringify({ id, method, params })}\n`
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id)
        reject(coreError('TIMEOUT', `${method} timed out after ${timeoutMs}ms`))
      }, timeoutMs)
      pending.set(id, { resolve, reject, timer })
      try {
        handle.stdin.write(payload)
      } catch (error) {
        pending.delete(id)
        clearTimeout(timer)
        reject(coreError('CORE_DOWN', `write failed: ${error?.message ?? error}`))
      }
    })
  }

  function stop() {
    if (!handle) {
      emit('stopped')
      return
    }
    const closing = handle
    handle = null
    stopping = true
    settleAll('CORE_STOPPED', 'mail core stopped')
    try {
      closing.terminate?.()
    } catch {
      // 已经死了就无所谓
    }
  }

  function status() {
    return {
      state,
      lastError: lastError ? { code: lastError.code ?? 'INTERNAL', message: lastError.message } : null,
      info,
    }
  }

  return { start, stop, handshake, request, status, get state() { return state } }
}
