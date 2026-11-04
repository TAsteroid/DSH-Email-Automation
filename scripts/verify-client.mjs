/**
 * 渲染测试：跑两遍 client.js。
 *
 *   A. shim 遍 —— 用最小 hooks 运行时驱动「加载 → 数据回来 → 切 tab」的状态流转；
 *   B. 真实 React 遍 —— 用 react-dom/server 真渲染每个视图，拿静态标记断言。
 *
 * 目的：组件抛异常会让整个 slot 变空白（宿主控制台 `slot entry crashed`），必须先拦住。
 * 不覆盖像素布局 —— 那需要浏览器。
 *
 * 用法：node scripts/verify-client.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const here = dirname(fileURLToPath(import.meta.url))
const clientPath = join(here, '..', 'dsh-mail-plugin', 'lib', 'client.js')
const source = readFileSync(clientPath, 'utf8')

const failures = []
const check = (condition, message) => {
  if (!condition) failures.push(message)
}

/** 每次调用都重新执行一遍 client.js（它会往 window 上挂 loader）。 */
function loadClient(requireImpl) {
  let captured
  globalThis.window = { __ModuleLoader__: { load: (definition) => (captured = definition) } }
  new Function(source)()
  if (!captured) throw new Error('client.js 没有调用 window.__ModuleLoader__.load')
  const module = captured.factory(requireImpl)
  return { id: captured.id, module }
}

function makeFakeCtx(dictionaries, registrations) {
  return {
    effect: (fn) => fn(),
    locale: {
      register: (ns, lang, dict) => {
        dictionaries.push({ ns, lang, dict })
        return () => {}
      },
      bind: (ns) => (key) => `t:${ns}.${key}`,
    },
    slots: {
      inject: (_owner, install) => install(),
      register: (options, component) => {
        registrations.push({ options, component })
        return () => {}
      },
    },
  }
}

function checkRegistrations(id, module, dictionaries, registrations) {
  check(id === 'dsh-mail-plugin', `loader id 应为 dsh-mail-plugin，实际 ${id}`)
  check(Boolean(module && typeof module.apply === 'function'), 'client 模块没有导出 apply')
  check(dictionaries.length === 2, `locale 字典应注册 2 份，实际 ${dictionaries.length}`)
  check(
    dictionaries.every((entry) => entry.ns === 'mail'),
    'locale 命名空间应为 mail',
  )
  const main = registrations.find((entry) => entry.options.name === 'main')
  const panellist = registrations.find((entry) => entry.options.name === 'sidebar.panellist')
  check(Boolean(main), '没有注册 main 面板')
  check(Boolean(panellist), '没有注册 sidebar.panellist 入口')
  check(Boolean(main && panellist && main.options.key === panellist.options.id), 'main.key 与 sidebar.panellist.id 必须相同')
  check(Boolean(panellist && typeof panellist.options.label === 'function'), 'panellist.label 应为函数')
  return { main, panellist }
}

// ===========================================================================
// A. shim 遍：状态流转
// ===========================================================================
let componentSeq = 0
let currentComponent = null
let hookCursor = 0
const hookStates = new Map()
const pendingEffects = []
let dirty = false

function hookKey() {
  const id = (currentComponent.__probeId ??= ++componentSeq)
  return `${id}:${hookCursor++}`
}

const reactShim = {
  createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
  useState(initial) {
    const key = hookKey()
    if (!hookStates.has(key)) hookStates.set(key, typeof initial === 'function' ? initial() : initial)
    return [
      hookStates.get(key),
      (next) => {
        const previous = hookStates.get(key)
        hookStates.set(key, typeof next === 'function' ? next(previous) : next)
        dirty = true
      },
    ]
  },
  useEffect: (effect) => pendingEffects.push(effect),
  useCallback: (fn) => fn,
  useMemo: (fn) => fn(),
  useRef: (initial) => ({ current: initial }),
}
reactShim.default = reactShim

function render(node) {
  if (node === null || node === undefined) return node
  if (typeof node !== 'object') return node
  if (Array.isArray(node)) return node.map(render)
  const { type, props, children } = node
  if (typeof type === 'function') {
    const savedComponent = currentComponent
    const savedCursor = hookCursor
    currentComponent = type
    hookCursor = 0
    try {
      return render(type({ ...props }))
    } finally {
      currentComponent = savedComponent
      hookCursor = savedCursor
    }
  }
  return { type, props, children: (children ?? []).map(render) }
}

function walk(node, visit) {
  if (node === null || node === undefined || typeof node !== 'object') return
  if (Array.isArray(node)) {
    for (const child of node) walk(child, visit)
    return
  }
  visit(node)
  for (const child of node.children ?? []) walk(child, visit)
}

function texts(node) {
  const out = []
  walk(node, (element) => {
    for (const child of element.children ?? []) if (typeof child === 'string') out.push(child)
  })
  return out
}

function findButtonByText(node, label) {
  let found
  walk(node, (element) => {
    if (found || element.type !== 'button') return
    const own = (element.children ?? []).filter((child) => typeof child === 'string')
    if (own.includes(label) || texts(element).includes(label)) found = element
  })
  return found
}

const flush = () => new Promise((resolve) => setImmediate(resolve))

const statusPayload = {
  plugin: 'dsh-mail-plugin',
  version: '0.1.0',
  phase: 'phase1',
  startedAt: '2026-11-04T11:44:48.184Z',
  syncIntervalMs: 300000,
  core: {
    state: 'ready',
    python: 'C:\\python\\python.exe',
    coreDir: 'C:\\core',
    dataDir: 'C:\\data',
    version: '0.1.0',
    dbPath: 'C:\\data\\mail.db',
    error: null,
  },
  accounts: [],
  accountsError: null,
  counters: { accounts: 0, messages: 0, unread: 0, drafts: 0, pendingApprovals: 0, pendingAi: 0 },
  lastSyncAt: '',
}

const fetchCalls = []
globalThis.fetch = async (url, init) => {
  fetchCalls.push({ url: String(url), body: init && init.body ? String(init.body) : '' })
  return {
    ok: true,
    status: 200,
    json: async () => ({ ok: true, value: statusPayload }),
  }
}

const t = (key) => `t:mail.${key}`

async function runShimPass() {
  const dictionaries = []
  const registrations = []
  const { id, module } = loadClient((name) => {
    if (name === 'react') return reactShim
    throw new Error(`未预期的外部模块: ${name}`)
  })
  module.apply(makeFakeCtx(dictionaries, registrations))
  const { main } = checkRegistrations(id, module, dictionaries, registrations)
  const Panel = main.component

  let tree = render(reactShim.createElement(Panel, { t }))
  check(texts(tree).includes('t:mail.loading'), '首屏应显示加载态')

  for (let pass = 0; pass < 8 && (pendingEffects.length > 0 || dirty); pass += 1) {
    for (const effect of pendingEffects.splice(0)) {
      try {
        effect()
      } catch (error) {
        failures.push(`effect 抛异常: ${error.message}`)
      }
    }
    await flush()
    await flush()
    tree = render(reactShim.createElement(Panel, { t }))
  }

  const loaded = texts(tree)
  check(loaded.includes('t:mail.noMessages'), '状态回来后应收件箱空状态')
  check(loaded.includes('t:mail.selectMessage'), '状态回来后应显示右侧占位')
  check(!loaded.includes('t:mail.loading'), '加载态没有被状态替换')

  const inboxCall = fetchCalls.find((entry) => entry.url.includes('/messages'))
  check(Boolean(inboxCall), '收件箱应请求 /messages')
  check(
    Boolean(inboxCall) && inboxCall.body.includes('"folder":"INBOX"'),
    '收件箱请求必须限定 folder=INBOX（不带会把已发送也列出来）',
  )

  const settingsButton = findButtonByText(tree, 't:mail.settings')
  check(Boolean(settingsButton), '找不到设置 tab 按钮')
  if (settingsButton) {
    settingsButton.props.onClick()
    for (let pass = 0; pass < 8 && (pendingEffects.length > 0 || dirty); pass += 1) {
      pendingEffects.splice(0).forEach((effect) => effect())
      await flush()
      tree = render(reactShim.createElement(Panel, { t }))
    }
    const settings = texts(tree)
    check(settings.includes('t:mail.coreDb'), '设置页应显示数据库行')
    check(settings.includes('t:mail.settingsCredsValue'), '设置页应显示凭据存储说明')
    check(settings.includes('C:\\data\\mail.db'), '设置页应回显真实 db 路径')
  }
}

// ===========================================================================
// B. 真实 React 遍：逐视图静态渲染
// ===========================================================================
function runRealReactPass() {
  const dictionaries = []
  const registrations = []
  const { id, module } = loadClient((name) => {
    if (name === 'react') return React
    throw new Error(`未预期的外部模块: ${name}`)
  })
  module.apply(makeFakeCtx(dictionaries, registrations))
  checkRegistrations(id, module, dictionaries, registrations)

  const views = module.__test
  check(Boolean(views), 'client 模块没有导出 __test（渲染测试需要）')
  if (!views) return

  const h = React.createElement
  const warnings = []
  const originalError = console.error
  console.error = (...args) => warnings.push(args.map(String).join(' '))

  try {
    const loading = renderToStaticMarkup(h(views.MailPanel, { t }))
    check(loading.includes('t:mail.loading'), '[真实渲染] 面板首屏应为加载态')
    check(loading.includes('t:mail.title'), '[真实渲染] 面板应包含标题')

    const emptyInbox = renderToStaticMarkup(h(views.InboxView, { t, status: statusPayload }))
    check(emptyInbox.includes('t:mail.noAccount'), '[真实渲染] 无账户时应显示空账户态')
    check(emptyInbox.includes('t:mail.noMessages'), '[真实渲染] 空收件箱文案缺失')
    check(emptyInbox.includes('t:mail.aiPanel'), '[真实渲染] 右侧 AI 分析卡片缺失')

    const withAccount = renderToStaticMarkup(
      h(views.InboxView, {
        t,
        status: { ...statusPayload, accounts: [{ id: 'a1', address: 'me@example.com' }] },
      }),
    )
    check(withAccount.includes('me@example.com'), '[真实渲染] 有账户时应显示账户地址')
    check(!withAccount.includes('t:mail.noAccount'), '[真实渲染] 有账户时不应再显示空账户态')

    const settings = renderToStaticMarkup(h(views.SettingsView, { t, status: statusPayload }))
    check(settings.includes('C:\\data\\mail.db'), '[真实渲染] 设置页应回显 Core 数据库路径')
    check(settings.includes('t:mail.settingsCredsValue'), '[真实渲染] 设置页应说明凭据存储')
    check(settings.includes('t:mail.settingsSyncInterval'), '[真实渲染] 设置页应显示自动同步间隔')
    check(/value="5"/.test(settings), '[真实渲染] 同步间隔应回显 5 分钟')
    check(settings.includes('t:mail.settingsSyncIntervalHint'), '[真实渲染] 同步间隔应有 0=关闭 的说明')
    check(settings.includes('t:mail.accountAdd'), '[真实渲染] 设置页应有添加账户表单')
    check(settings.includes('t:mail.passwordHint'), '[真实渲染] 设置页应说明密码如何保存')
    check(settings.includes('type="password"'), '[真实渲染] 密码输入框应为 password 类型')
    check(settings.includes('t:mail.accountsEmpty'), '[真实渲染] 无账户时应显示空态')
    check(settings.includes('t:mail.protocolLabel'), '[真实渲染] 应有收信协议选择')
    check(settings.includes('t:mail.protocolImap'), '[真实渲染] 协议应有 IMAP 选项')
    check(settings.includes('t:mail.protocolPop3'), '[真实渲染] 协议应有 POP3 选项')
    check(settings.includes('t:mail.smtpSection'), '[真实渲染] 应有 SMTP 发信分组')
    check(settings.includes('t:mail.smtpHost'), '[真实渲染] 应有 SMTP 服务器输入')
    check(settings.includes('t:mail.usernameHint'), '[真实渲染] 应说明用户名默认等于邮箱地址')
    check(settings.includes('t:mail.smtpSecurity'), '[真实渲染] 应有 SMTP 加密选择')

    const withAccountSettings = renderToStaticMarkup(
      h(views.SettingsView, {
        t,
        status: {
          ...statusPayload,
          accounts: [{ id: 'a1', address: 'me@example.com', imapHost: 'imap.example.com', imapPort: 993, imapSecurity: 'ssl', hasCredential: true }],
        },
      }),
    )
    check(withAccountSettings.includes('me@example.com'), '[真实渲染] 设置页应列出已配置账户')
    check(withAccountSettings.includes('t:mail.accountTest'), '[真实渲染] 账户行应有测试连接按钮')

    const pane = renderToStaticMarkup(h(views.EmptyPane, { title: 'X', hint: 'Y' }))
    check(pane.includes('X') && pane.includes('Y'), '[真实渲染] EmptyPane 应渲染标题与提示')

    const icon = renderToStaticMarkup(h(views.MailIcon, { size: 20 }))
    check(icon.includes('width="20"'), '[真实渲染] 侧栏图标应接收 size')
    check(icon.includes('svg'), '[真实渲染] 侧栏图标应为 svg')

    // 分隔条与默认宽度
    const separators = (emptyInbox.match(/role="separator"/g) || []).length
    check(separators === 2, `[真实渲染] 收件箱应有 2 个分隔条，实际 ${separators}`)
    if (views.LAYOUT) {
      check(emptyInbox.includes(`width:${views.LAYOUT.RAIL_DEFAULT}px`), '[真实渲染] 账户列应使用默认宽度')
      check(emptyInbox.includes(`width:${views.LAYOUT.LIST_DEFAULT}px`), '[真实渲染] 列表列应使用默认宽度')

      const { clampWidth, LAYOUT } = views
      check(clampWidth('rail', 50, 320, 1200) === LAYOUT.RAIL_MIN, 'rail 过窄时应抬到下限')
      check(clampWidth('rail', 9999, 320, 1200) === LAYOUT.RAIL_MAX, 'rail 过宽时应压到上限')
      check(clampWidth('list', 100, 200, 1200) === LAYOUT.LIST_MIN, 'list 过窄时应抬到下限')
      check(clampWidth('list', 9999, 200, 1200) === LAYOUT.LIST_MAX, 'list 过宽时应压到上限')
      check(clampWidth('rail', 300, 320, 700) === LAYOUT.RAIL_MIN, '容器过窄时 rail 应让位给阅读区')
      check(clampWidth('list', 600, 200, 700) === LAYOUT.LIST_MIN, '容器过窄时 list 应让位给阅读区')
    } else {
      failures.push('client 模块没有导出 LAYOUT 常量')
    }

    // 各页骨架：能渲染、且恰好一条分隔条
    const viewCases = [
      ['待跟进', views.TodoView, 't:mail.aiEmptyTitle', 2],
      ['草稿箱', views.DraftsView, 't:mail.draftDetailTitle', 1],
      ['知识库', views.KnowledgeView, 't:mail.kbContacts', 2],
    ]
    for (const [label, View, needle, expectedSplitters] of viewCases) {
      const markup = renderToStaticMarkup(h(View, { t, status: statusPayload }))
      check(markup.includes(needle), `[真实渲染] ${label} 应包含 ${needle}`)
      const splitters = (markup.match(/role="separator"/g) || []).length
      check(splitters === expectedSplitters, `[真实渲染] ${label} 应有 ${expectedSplitters} 条分隔条，实际 ${splitters}`)
    }

    // 审批栏：全局常驻，默认只看待审批，历史模式带返回
    const railPending = renderToStaticMarkup(h(views.ApprovalsRail, { t, status: statusPayload }))
    check(railPending.includes('t:mail.approvals'), '[真实渲染] 审批栏应显示标题')
    check(railPending.includes('t:mail.emptyApprovals'), '[真实渲染] 待审批空态缺失')
    check(/aria-label="t:mail.approvalHistory"/.test(railPending), '[真实渲染] 待审批模式应有历史按钮')
    check(
      railPending.includes('role="separator"') && railPending.includes('aria-orientation="vertical"'),
      '[真实渲染] 审批栏应可拖动改宽度',
    )

    const railBusy = renderToStaticMarkup(
      h(views.ApprovalsRail, {
        t,
        status: { ...statusPayload, counters: { ...statusPayload.counters, pendingApprovals: 4 } },
      }),
    )
    check(railBusy.includes('>4<'), '[真实渲染] 有待审批时应显示数量')

    const railHistory = renderToStaticMarkup(h(views.ApprovalsRail, { t, status: statusPayload, initialMode: 'history' }))
    check(railHistory.includes('t:mail.approvalHistoryEmpty'), '[真实渲染] 审批历史空态缺失')
    check(/aria-label="t:mail.back"/.test(railHistory), '[真实渲染] 历史模式应有返回按钮')
    check(!railHistory.includes('t:mail.emptyApprovals'), '[真实渲染] 历史模式不应显示待审批空态')

    const pageMarkup = renderToStaticMarkup(h(views.MailPanel, { t }))
    check(pageMarkup.includes('dsh-mail-select'), '[真实渲染] 加密方式下拉框应带 dsh-mail-select 类（暗色靠它）')
    check(pageMarkup.includes('color-scheme'), '[真实渲染] 应注入 color-scheme 规则')
    check(pageMarkup.includes('role="separator"') === false, '[真实渲染] 首屏加载态不应有分隔条')

    // 收件箱：邮件列表与正文
    const sampleMessages = [
      { id: 'm1', subject: '合同确认', fromAddr: 'a@b.com', date: '2026-11-04T09:12:00Z' },
      { id: 'm2', subject: '(no subject)', fromAddr: 'c@d.com', date: '2026-11-03T08:00:00Z' },
    ]
    const listMarkup = renderToStaticMarkup(
      h(views.MessageList, { t, messages: sampleMessages, selectedId: 'm1', onSync: () => {}, syncing: false }),
    )
    check(listMarkup.includes('合同确认'), '[真实渲染] 邮件列表应显示主题')
    check(listMarkup.includes('a@b.com'), '[真实渲染] 邮件列表应显示发件人')
    check(listMarkup.includes('>2<'), '[真实渲染] 邮件列表应显示数量')
    check((listMarkup.match(/aria-pressed="true"/g) || []).length === 1, '[真实渲染] 应只有一封被选中')
    check(listMarkup.includes('t:mail.syncNow'), '[真实渲染] 列表头应有同步按钮占位')

    const emptyListMarkup = renderToStaticMarkup(h(views.MessageList, { t, messages: [], selectedId: null }))
    check(emptyListMarkup.includes('t:mail.noMessages'), '[真实渲染] 空列表应显示空态')

    const detailMarkup = renderToStaticMarkup(
      h(views.MessageDetail, {
        t,
        message: { id: 'm1', subject: '合同确认', fromAddr: 'a@b.com', date: '2026-11-04T09:12:00Z', text: '正文第一行\n第二行' },
      }),
    )
    check(detailMarkup.includes('合同确认'), '[真实渲染] 正文应显示主题')
    check(detailMarkup.includes('正文第一行'), '[真实渲染] 正文应显示内容')

    const noSelectionMarkup = renderToStaticMarkup(h(views.MessageDetail, { t, message: null }))
    check(noSelectionMarkup.includes('t:mail.selectMessage'), '[真实渲染] 未选中时应显示占位')

    // 回复 / 回复全部 / 转发 / 发送 的预填规则
    const baseMessage = {
      id: 'm1',
      subject: '项目 A',
      fromAddr: 'a@b.com',
      toAddr: 'me@b.com',
      ccAddr: 'c@b.com',
      date: '2026-11-04T09:00:00Z',
      text: '正文内容',
    }
    const replyDraft = views.buildCompose('reply', baseMessage, t)
    check(replyDraft.to === 'a@b.com' && replyDraft.cc === '', '[撰写] 回复应只填发件人')
    check(replyDraft.subject === 'Re: 项目 A', '[撰写] 回复主题应加 Re:')
    check(replyDraft.body.includes('原邮件'), '[撰写] 回复应带原文引用')

    const replyAllDraft = views.buildCompose('replyAll', baseMessage, t)
    check(replyAllDraft.cc.includes('me@b.com') && replyAllDraft.cc.includes('c@b.com'), '[撰写] 回复全部应带上原收件人与抄送')

    const forwardDraft = views.buildCompose('forward', baseMessage, t)
    check(forwardDraft.subject === 'Fwd: 项目 A' && forwardDraft.to === '', '[撰写] 转发应加 Fwd: 且收件人为空')

    const sendDraft = views.buildCompose('send', baseMessage, t)
    check(sendDraft.to === 'me@b.com' && sendDraft.body === '正文内容', '[撰写] 草稿发送应沿用原收件人与正文')

    check(
      views.buildCompose('reply', { ...baseMessage, subject: 'Re: 已有前缀' }, t).subject === 'Re: 已有前缀',
      '[撰写] 已有 Re: 前缀时不应重复添加',
    )

    const composeMarkup = renderToStaticMarkup(h(views.ComposeModal, { t, initial: replyDraft, onClose: () => {} }))
    check(composeMarkup.includes('t:mail.composeTo'), '[真实渲染] 编辑页应有收件人')
    check(composeMarkup.includes('t:mail.composeBody'), '[真实渲染] 编辑页应有正文')
    check(composeMarkup.includes('a@b.com'), '[真实渲染] 编辑页应回显预填收件人')
    check(composeMarkup.includes('t:mail.composeSubmit'), '[真实渲染] 编辑页应有发送按钮')
    check(composeMarkup.includes('t:mail.composeSendBlocked'), '[真实渲染] 编辑页应说明要先审批')

    // 审批栏：待审批项要能批准发送 / 拒绝
    const pendingStatus = {
      ...statusPayload,
      approvals: {
        pending: [{ id: 'apr_1', subject: '报价回复', to: 'x@b.com', expiresAt: '2026-11-05T10:00:00Z' }],
        history: [],
      },
      counters: { ...statusPayload.counters, pendingApprovals: 1 },
    }
    const railWithPending = renderToStaticMarkup(h(views.ApprovalsRail, { t, status: pendingStatus }))
    check(railWithPending.includes('报价回复'), '[真实渲染] 待审批应显示草稿主题')
    check(railWithPending.includes('t:mail.copySendCommand'), '[真实渲染] 待审批应有「复制发送指令」')
    check(railWithPending.includes('t:mail.approvalSendHint'), '[真实渲染] 应说明发送需经 Agent 审批')
    check(railWithPending.includes('t:mail.rejectApproval'), '[真实渲染] 待审批应有「拒绝」')

    const agentMarkup = renderToStaticMarkup(h(views.AgentMailComposer, { t, message: baseMessage }))
    check(agentMarkup.includes('t:mail.agentChatTitle'), '[真实渲染] 详情页应有 Agent 区')
    check(agentMarkup.includes('t:mail.agentChatCopy'), '[真实渲染] Agent 区应有复制提示词')

    const detailWithActions = renderToStaticMarkup(
      h(views.MessageDetail, {
        t,
        message: baseMessage,
        actions: [{ key: 'reply', label: '回复', onClick: () => {} }],
        agentChat: true,
      }),
    )
    check(detailWithActions.includes('回复'), '[真实渲染] 详情页应渲染操作按钮')
    check(detailWithActions.includes('t:mail.agentChatTitle'), '[真实渲染] 详情页底部应渲染 Agent 区')
    check(
      detailWithActions.includes('data-mail-agent-dock') &&
        detailWithActions.indexOf('data-mail-scroll') < detailWithActions.indexOf('data-mail-agent-dock'),
      '[真实渲染] Agent 区应在滚动容器之外，常驻底部',
    )
    check(
      detailWithActions.includes('aria-orientation="horizontal"'),
      '[真实渲染] Agent 区应有可上下拖动的高度手柄',
    )
    check(detailWithActions.includes('t:mail.agentChatResize'), '[真实渲染] 高度手柄应带可访问标签')

    // 引用拆分与气泡
    const quotedBody = [
      '这是当前这封邮件的新内容。',
      '',
      '------------------ 原始邮件 ------------------',
      '发件人: "Serena SONG" <s@b.com>',
      '发送时间: 2026年10月9日 21:25',
      '收件人: me@b.com',
      '主题: Re: 测试',
      '',
      '这是被引用的历史内容。',
      '',
      'QUOTED-TAIL-MARKER 引用末尾的独有内容',
    ].join('\n')
    const segments = views.splitQuoted(quotedBody)
    check(segments.length === 2, `[拆分] 应切成 2 段，实际 ${segments.length}`)
    check(segments[0] && segments[0].quoted === false && segments[0].body.includes('新内容'), '[拆分] 第 0 段是当前邮件')
    check(segments[1] && segments[1].quoted === true, '[拆分] 第 1 段应标记为引用')
    check(segments[1] && String(segments[1].header.from).includes('Serena'), '[拆分] 应解析出引用发件人')
    check(segments[1] && String(segments[1].header.date).includes('2026'), '[拆分] 应解析出引用日期')
    check(segments[1] && segments[1].body.includes('被引用的历史内容'), '[拆分] 引用正文应保留')
    check(!(segments[1] && segments[1].body.includes('发件人:')), '[拆分] 头部不应混进正文')

    const bubbleMarkup = renderToStaticMarkup(
      h(views.ThreadBubbles, {
        t,
        message: { id: 'm1', text: quotedBody, fromAddr: 'me@b.com', date: '2026-11-04' },
        accountAddress: 'me@b.com',
      }),
    )
    check(bubbleMarkup.includes('新内容'), '[气泡] 最新一封应展开')
    check(!bubbleMarkup.includes('QUOTED-TAIL-MARKER'), '[气泡] 历史引用默认折叠（只给预览，不渲染全文）')
    check(bubbleMarkup.includes('t:mail.clickToExpand'), '[气泡] 折叠气泡应有展开提示')
    check(bubbleMarkup.includes('Serena'), '[气泡] 折叠时仍显示对方与时间')
    check(
      bubbleMarkup.indexOf('新内容') >= 0 &&
        bubbleMarkup.indexOf('新内容') < bubbleMarkup.indexOf('Serena'),
      '[气泡] 最新一封应排在最上方',
    )

    // 发件箱页签位置：待跟进 < 发件箱 < 草稿箱
    const todoAt = pageMarkup.indexOf('t:mail.todo')
    const sentAt = pageMarkup.indexOf('t:mail.sent')
    const draftsAt = pageMarkup.indexOf('t:mail.drafts')
    check(
      todoAt >= 0 && sentAt > todoAt && draftsAt > sentAt,
      '[真实渲染] 页签顺序应为 待跟进 < 发件箱 < 草稿箱',
    )

    const sentMarkup = renderToStaticMarkup(h(views.SentView, { t, status: statusPayload }))
    check(sentMarkup.includes('t:mail.sent'), '[真实渲染] 发件箱页应渲染')

    // 新增页签与顺序
    const inboxTabAt = pageMarkup.indexOf('t:mail.inbox')
    const todoTabAt = pageMarkup.indexOf('t:mail.todo')
    check(todoTabAt >= 0 && inboxTabAt > todoTabAt, '[真实渲染] 待跟进应排在收件箱之前')
    check(pageMarkup.includes('t:mail.searchPlaceholder'), '[真实渲染] 顶栏应有搜索框')
    const orderKeys = ['t:mail.sent', 't:mail.deleted', 't:mail.junk', 't:mail.templates', 't:mail.contacts', 't:mail.knowledge']
    const positions = orderKeys.map((key) => pageMarkup.indexOf(key))
    check(pageMarkup.indexOf('t:mail.historyMail') < 0, '[真实渲染] 历史邮件页签已移除')
    check(pageMarkup.indexOf('t:mail.ai') < 0, '[真实渲染] AI 工作台页签已并入待跟进')
    check(
      positions.every((position, index) => position >= 0 && (index === 0 || position > positions[index - 1])),
      '[真实渲染] 页签顺序应为 发件箱 < 已删除 < 垃圾邮件 < 模板 < 通讯录 < 知识库',
    )

    // 账户显示名优先；待跟进按账户分组
    const twoAccounts = [
      { id: 'a1', address: 'one@b.com', displayName: '工作邮箱' },
      { id: 'a2', address: 'two@b.com', displayName: '' },
    ]
    const inboxWithNames = renderToStaticMarkup(
      h(views.InboxView, { t, status: { ...statusPayload, accounts: twoAccounts } }),
    )
    check(inboxWithNames.includes('工作邮箱'), '[真实渲染] 有显示名称时应显示名称')
    check(!inboxWithNames.includes('one@b.com'), '[真实渲染] 有显示名称时不再显示地址')
    check(inboxWithNames.includes('two@b.com'), '[真实渲染] 没有显示名称时回落到地址')
    check(inboxWithNames.includes('t:mail.allAccounts'), '[真实渲染] 收件箱账户栏应有「全部账户」')
    check(
      (inboxWithNames.match(/aria-pressed="(true|false)"/g) || []).length >= 2,
      '[真实渲染] 账户栏应渲染为可点击按钮（带 aria-pressed）',
    )

    const todoWithAccounts = renderToStaticMarkup(
      h(views.TodoView, { t, status: { ...statusPayload, accounts: twoAccounts } }),
    )
    check(todoWithAccounts.includes('t:mail.allAccounts'), '[真实渲染] 待跟进应有全部账户项')
    check(todoWithAccounts.includes('工作邮箱'), '[真实渲染] 待跟进账户栏应显示名称')
    const todoSplitters = (todoWithAccounts.match(/role="separator"/g) || []).length
    check(todoSplitters === 2, `[真实渲染] 待跟进应有两处分隔条（账户栏与列表），实际 ${todoSplitters}`)

    // 其它页签也带账户栏
    for (const [label, View] of [
      ['发件箱', views.SentView],
      ['模板', views.TemplatesView],
      ['通讯录', views.ContactsView],
    ]) {
      const markup = renderToStaticMarkup(
        h(View, { t, status: { ...statusPayload, accounts: twoAccounts }, accountId: null, onSelectAccount: () => {} }),
      )
      check(markup.includes('t:mail.allAccounts'), `[真实渲染] ${label} 应有账户栏`)
      const splitters = (markup.match(/role="separator"/g) || []).length
      check(splitters >= 2, `[真实渲染] ${label} 应至少有账户栏与内容两处分隔条，实际 ${splitters}`)
    }

    check(pageMarkup.includes('t:mail.composeNew'), '[真实渲染] 顶栏应有新建邮件按钮')

    // 通讯录：字段 + Agent 提示词区
    const contactsMarkup = renderToStaticMarkup(h(views.ContactsView, { t }))
    for (const key of ['contactName', 'contactEmail', 'contactPhone', 'contactCompany', 'contactTitle', 'contactNotes']) {
      check(contactsMarkup.includes(`t:mail.${key}`), `[真实渲染] 通讯录缺少字段 ${key}`)
    }
    check(contactsMarkup.includes('t:mail.contactChatTitle'), '[真实渲染] 通讯录应有 Agent 区')
    check(contactsMarkup.includes('t:mail.contactChatCopy'), '[真实渲染] 应有复制提示词按钮')

    // 模板：本地保存
    const templatesMarkup = renderToStaticMarkup(h(views.TemplatesView, { t }))
    check(templatesMarkup.includes('t:mail.templateName'), '[真实渲染] 模板页应有名称字段')
    check(templatesMarkup.includes('t:mail.templateSave'), '[真实渲染] 模板页应有保存按钮')

    // 搜索结果空态
    const searchMarkup = renderToStaticMarkup(h(views.SearchView, { t, query: '合同', status: statusPayload }))
    check(searchMarkup.includes('t:mail.searchResults'), '[真实渲染] 搜索页应有结果标题')
    check(searchMarkup.includes('t:mail.searchEmpty'), '[真实渲染] 无结果时应有空态')

    const kbMarkup = renderToStaticMarkup(h(views.KnowledgeView, { t, status: statusPayload }))
    for (const key of ['kbContacts', 'kbCompanies', 'kbProjects', 'kbFiles']) {
      check(kbMarkup.includes(`t:mail.${key}`), `[真实渲染] 知识库缺少分类 ${key}`)
    }

    // AI 卡片：上半内容 + 下半对话 + 三个动作
    const cardMarkup = renderToStaticMarkup(
      h(views.AiCard, { t, item: { id: 'i1', title: '示例事项', summary: '摘要' }, onAct: () => {} }),
    )
    for (const key of ['actDone', 'actLater', 'actDelete', 'cardPendingContent', 'cardChat', 'cardChatPlaceholder']) {
      check(cardMarkup.includes(`t:mail.${key}`), `[真实渲染] AI 卡片缺少 ${key}`)
    }

    // 顶栏待处理红标
    const busyTopBar = renderToStaticMarkup(
      h(views.TopBar, {
        t,
        status: { ...statusPayload, counters: { ...statusPayload.counters, pendingAi: 3 } },
        onRefresh: () => {},
      }),
    )
    check(busyTopBar.includes('t:mail.pendingAi 3'), '[真实渲染] 顶栏应显示待处理总数 3')
    check(busyTopBar.includes('state-error-primary'), '[真实渲染] 有待处理时红标应使用 error 色')

    const idleTopBar = renderToStaticMarkup(h(views.TopBar, { t, status: statusPayload, onRefresh: () => {} }))
    check(idleTopBar.includes('t:mail.pendingAi 0'), '[真实渲染] 无积压时顶栏应显示 0')
  } finally {
    console.error = originalError
  }

  const reactWarnings = warnings.filter((line) => /Warning:|validateDOMNesting|Each child/i.test(line))
  if (reactWarnings.length > 0) {
    failures.push(`React 告警 ${reactWarnings.length} 条: ${reactWarnings[0].slice(0, 200)}`)
  }
}

await runShimPass()
runRealReactPass()

if (failures.length > 0) {
  console.error('FAIL')
  for (const message of failures) console.error(` - ${message}`)
  process.exit(1)
}

const reactVersion = React.version
console.log('OK')
console.log(`  shim 遍: 加载态 → 收件箱 → 设置页`)
console.log(`  真实 React 遍: ${reactVersion}，覆盖 面板/收件箱（空/有账户）/设置页/空态/图标`)
