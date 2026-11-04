/**
 * dsh-mail-plugin — Client half
 *
 * 浏览器全局脚本，**不是 ESM**：不要写 import / export。
 * 注册一个一级工作区：`main` keyed 面板 + `sidebar.panellist` 图标入口，
 * 两者 id 必须相同（宿主用侧栏 id 去 main 里取面板）。
 * 证据：docs/compat/ui.md 第 2 节。
 *
 * 页面结构：
 *   收件箱    三栏（账户 / 列表 / 阅读 + AI 分析），两条分隔条可拖动
 *   待跟进    两栏（待办标题 / 详情）
 *   AI 工作台 两栏（事项列表：待处理 + 已处理；右侧卡片横向滑动，上内容下对话）
 *   草稿箱    两栏（草稿标题 / 详情）
 *   审批中心  两栏（待审批 + 审批历史 / 详情）
 *   知识库    两栏（联系人 / 公司 / 项目 / 文件类型 分类 / 条目）
 *   设置      收窄的单栏
 */
window.__ModuleLoader__.load({
  id: 'dsh-mail-plugin',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const NS = 'mail'
    const PANEL_ID = 'mail-workspace'
    const API = '/api/dsh-mail'

    // ---- 列宽 --------------------------------------------------------------
    const LAYOUT_KEY = 'dsh-mail:layouts'
    const LEGACY_LAYOUT_KEY = 'dsh-mail:layout'
    const RAIL_DEFAULT = 200
    const RAIL_MIN = 140
    const RAIL_MAX = 360
    const LIST_DEFAULT = 320
    const LIST_MIN = 240
    const LIST_MAX = 640
    const READING_MIN = 320
    const DETAIL_MIN = 360
    const HANDLE = 5

    const RAIL_SPEC = { initial: RAIL_DEFAULT, min: RAIL_MIN, max: RAIL_MAX }
    const LIST_SPEC = { initial: LIST_DEFAULT, min: LIST_MIN, max: LIST_MAX }
    const PANE_SPEC = { initial: 300, min: 220, max: 520 }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value))
    }

    /** 两栏页面：左列宽度夹进 [min, min(max, 容器宽 - 右列下限)]。 */
    function clampPane(value, spec, minRight, total) {
      const available = total > 0 ? total - minRight - HANDLE : spec.max
      return Math.round(clamp(value, spec.min, Math.max(spec.min, Math.min(spec.max, available))))
    }

    /** 收件箱三栏：左列还要给中间列和阅读区留位置，语义与 clampPane 略有不同。 */
    function clampWidth(which, value, otherWidth, total) {
      const isRail = which === 'rail'
      const spec = isRail ? RAIL_SPEC : LIST_SPEC
      const available = total > 0 ? total - otherWidth - READING_MIN - HANDLE * 2 : spec.max
      return Math.round(clamp(value, spec.min, Math.max(spec.min, Math.min(spec.max, available))))
    }

    function readStoredWidths() {
      try {
        const raw = window.localStorage.getItem(LAYOUT_KEY)
        if (raw) {
          const parsed = JSON.parse(raw)
          if (parsed && typeof parsed === 'object') return parsed
        }
        // 兼容上一版只存 { rail, list } 的键
        const legacy = window.localStorage.getItem(LEGACY_LAYOUT_KEY)
        if (legacy) {
          const parsed = JSON.parse(legacy)
          if (parsed && typeof parsed === 'object') return { rail: parsed.rail, list: parsed.list }
        }
      } catch {
        // 存储不可用时退回默认宽度
      }
      return {}
    }

    function storeWidth(name, width) {
      try {
        const all = readStoredWidths()
        all[name] = width
        window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(all))
      } catch {
        // 写不进去不影响使用
      }
    }

    function storedWidth(name, spec) {
      const stored = readStoredWidths()[name]
      return Number.isFinite(stored) ? clamp(stored, spec.min, spec.max) : spec.initial
    }

    // ---- 文案 --------------------------------------------------------------
    const messages = {
      zh: {
        nav: '邮箱',
        title: '邮箱管理',
        inbox: '收件箱',
        sent: '发件箱',
        sentFolderMissing: '没找到「已发送」文件夹，先到设置里点一次「测试连接」把文件夹列表拉下来。',
        sentEmpty: '发件箱为空',
        sentEmptyHint: '点右上角同步按钮，从服务器拉取已发送的邮件。',
        earlierMail: '更早的邮件',
        clickToExpand: '点击展开',
        clickToCollapse: '点击收起',
        deleted: '已删除',
        junk: '垃圾邮件',
        historyMail: '历史邮件',
        templates: '模板',
        contacts: '通讯录',
        searchPlaceholder: '搜索联系人 / 主题 / 邮件内容',
        searchResults: '搜索结果',
        searchEmpty: '没有匹配的邮件',
        folderMissing: '没找到对应文件夹，先到设置里点一次「测试连接」把文件夹列表拉下来。',
        templateNew: '新建模板',
        templateName: '模板名称',
        templateSubject: '默认主题',
        templateBody: '正文',
        templateSave: '保存',
        templateDelete: '删除',
        templateSaved: '已保存',
        templatesEmpty: '还没有模板',
        contactNew: '新建联系人',
        contactName: '姓名',
        contactEmail: '邮箱',
        contactPhone: '电话',
        contactCompany: '公司',
        contactTitle: '职位',
        contactNotes: '备注',
        contactSave: '保存',
        contactDelete: '删除',
        contactSaved: '已保存',
        contactsEmpty: '还没有联系人',
        contactChatTitle: '让 Agent 处理这个联系人',
        contactChatPlaceholder: '例如：给这个人起草一封回复邮件，语气正式一些…',
        contactChatCopy: '复制提示词',
        contactChatCopied: '已复制到剪贴板',
        contactChatHint: 'DSH 目前没有向运行中的 Agent 注入消息的公开接口，这里生成一段带联系人上下文的提示词，复制到对话里发出。',
        reply: '回复',
        replyAll: '回复全部',
        forward: '转发',
        send: '发送',
        composeTitle: '编辑邮件',
        composeNew: '新建邮件',
        composeTo: '收件人',
        composeCc: '抄送',
        composeSubject: '主题',
        composeBody: '正文',
        composeClose: '关闭',
        composeCopyBody: '复制邮件',
        composeCopied: '已复制',
        composeSendBlocked: '点「发送」会先存为草稿并提交审批；批准在右侧审批栏完成，Core 在校验内容哈希后才真正发信。',
        composeSubmit: '发送',
        composePendingHint: '已提交审批，去右侧审批栏批准后才会真正发送。',
        approveAndSend: '批准并发送',
        rejectApproval: '拒绝',
        approvalSendHint: '发送只能由 Agent 发起并弹出 DSH 审批确认。点「复制发送指令」，把它粘到对话里即可。',
        copySendCommand: '复制发送指令',
        sendCommandCopied: '指令已复制',
        sentOk: '已发送',
        decidedByUser: '用户批准',
        decidedByPolicy: '策略放行',
        decidedByAgent: 'Agent 批准',
        approvalExpired: '已过期',
        agentChatTitle: '与 Agent 对话处理这封邮件',
        agentChatPlaceholder: '例如：帮我起草一封回复，确认周五 9:30 的会议…',
        agentChatCopy: '复制提示词',
        agentChatResize: '拖动调整高度，双击复位',
        todo: '待跟进',
        ai: 'AI 工作台',
        drafts: '草稿箱',
        approvals: '审批中心',
        knowledge: '知识库',
        settings: '设置',
        accounts: '账户',
        allAccounts: '全部账户',
        resize: '调整宽度',
        resizeHint: '拖动调整宽度，双击恢复默认',
        noAccount: '尚未配置邮箱账户',
        noAccountHint: 'Phase 1 接入 IMAP 后，账户与文件夹会显示在这里。',
        messages: '邮件',
        noMessages: '暂无邮件',
        noMessagesHint: '同步接通后，这里显示邮件列表：发件人、主题、摘要、标签。',
        syncNow: '立即同步',
        noSubject: '（无主题）',
        selectMessage: '选择一封邮件',
        selectMessageHint: '右侧显示正文、线程和 AI 分析结果。',
        aiPanel: 'AI 分析',
        aiPanelHint: '摘要、分类、实体、金额与待办会显示在这里。',
        emptyList: '暂无',
        pendingAi: '待处理',
        todoDetailTitle: '待办详情',
        todoDetailHint: '选中左侧待办后，这里显示事项内容、来源邮件、承诺期限和负责人。',
        draftDetailTitle: '草稿详情',
        draftDetailHint: '收件人、主题、正文预览与审批状态显示在这里。',
        approvalPending: '待审批',
        approvalHistory: '审批历史',
        approvalHistoryEmpty: '暂无审批历史',
        approvalHistoryHint: '已批准或已拒绝的操作会留在这里。',
        back: '返回',
        approvalDetailTitle: '审批详情',
        approvalDetailHint: '操作类型、目标账户、收件人、正文、附件、影响范围与有效期显示在这里。',
        kbContacts: '联系人',
        kbCompanies: '公司',
        kbProjects: '项目',
        kbFiles: '文件类型',
        kbDetailTitle: '知识库条目',
        kbDetailHint: '选择左侧分类后，这里显示该分类下的条目。',
        aiQueue: '待处理',
        aiDone: '已处理',
        aiEmptyTitle: '暂无待处理事项',
        aiEmptyHint: '新邮件经 AI 处理后，事项会以卡片出现在这里：上方是待处理内容，下方是与 Agent 的对话栏。',
        aiDoneEmpty: '暂无已处理事项',
        prevCard: '上一张',
        nextCard: '下一张',
        cardPendingContent: '待处理内容',
        cardChat: '与 Agent 的对话',
        cardChatEmpty: '还没有对话',
        cardChatPlaceholder: '让 Agent 处理这件事…',
        actDone: '处理完成',
        actLater: '稍后',
        actDelete: '删除',
        backend: '邮件服务',
        connected: '已连接',
        offline: '未连接',
        coreState: 'Mail Core',
        coreVersion: 'Core 版本',
        coreDb: '数据库',
        coreDataDir: '数据目录',
        coreError: 'Core 错误',
        accountsEmpty: '还没有配置任何邮箱账户',
        accountAdd: '添加邮箱账户',
        accountAddress: '邮箱地址',
        accountDisplayName: '显示名称（可选）',
        accountHostImap: 'IMAP 服务器',
        accountHostPop3: 'POP3 服务器',
        accountImapPort: '端口',
        accountSecurity: '加密方式',
        protocolLabel: '收信协议',
        protocolImap: 'IMAP',
        protocolPop3: 'POP3',
        smtpSection: '发信（SMTP）',
        smtpHost: 'SMTP 服务器',
        smtpPort: 'SMTP 端口',
        smtpSecurity: 'SMTP 加密',
        smtpOptionalHint: '可以留空：不填就只能收信、不能发信。',
        accountUsername: '用户名',
        usernameHint: '默认与邮箱地址相同；服务器要求别的登录名时再改（如 域\\账号）。',
        accountPassword: '密码 / 应用专用密码',
        passwordHint: '保存时用 Windows DPAPI 加密，插件进程拿不到明文。',
        accountSubmit: '保存账户',
        accountSubmitting: '保存中…',
        accountSaved: '账户已保存',
        accountFailed: '保存失败',
        accountTest: '测试连接',
        accountTesting: '连接中…',
        accountRemove: '删除',
        accountTestOk: '连接成功，文件夹数',
        accountTestFailed: '连接失败',
        accountNoCredential: '无凭据',
        securitySsl: 'SSL',
        securityStarttls: 'STARTTLS',
        securityNone: '不加密',
        loading: '加载中…',
        failed: '加载失败',
        retry: '重试',
        refreshStatus: '同步所有账户并刷新',
        phase0: 'Phase 0 骨架：入口与数据通道已打通，邮件同步尚未实现。',
        settingsBackend: 'Mail Core 地址',
        settingsPoll: '自动同步间隔',
        settingsSyncInterval: '自动同步间隔',
        settingsSyncIntervalHint: '单位分钟，填整数；0 表示不自动同步。只同步收件箱，界面仍需手动刷新。',
        save: '保存',
        syncIntervalInvalid: '请输入不小于 0 的整数',
        syncIntervalSaved: '已保存',
        settingsCreds: '凭据存储',
        settingsCredsValue: 'Windows DPAPI（由 Mail Core 管理）',
        settingsAccounts: '邮箱账户',
        settingsAccountsValue: '尚未配置',
        minute: '分钟',
      },
      en: {
        nav: 'Mail',
        title: 'Mail',
        inbox: 'Inbox',
        sent: 'Sent',
        sentFolderMissing: 'No "Sent" folder found. Run "Test connection" in Settings once to load the folder list.',
        sentEmpty: 'Sent is empty',
        sentEmptyHint: 'Use the sync button at the top right to fetch sent mail from the server.',
        earlierMail: 'Earlier message',
        clickToExpand: 'Click to expand',
        clickToCollapse: 'Click to collapse',
        deleted: 'Deleted',
        junk: 'Junk',
        historyMail: 'Archive',
        templates: 'Templates',
        contacts: 'Contacts',
        searchPlaceholder: 'Search contacts, subjects, mail bodies',
        searchResults: 'Search results',
        searchEmpty: 'No matching mail',
        folderMissing: 'Folder not found. Run "Test connection" in Settings once to load the folder list.',
        templateNew: 'New template',
        templateName: 'Template name',
        templateSubject: 'Default subject',
        templateBody: 'Body',
        templateSave: 'Save',
        templateDelete: 'Delete',
        templateSaved: 'Saved',
        templatesEmpty: 'No templates yet',
        contactNew: 'New contact',
        contactName: 'Name',
        contactEmail: 'Email',
        contactPhone: 'Phone',
        contactCompany: 'Company',
        contactTitle: 'Title',
        contactNotes: 'Notes',
        contactSave: 'Save',
        contactDelete: 'Delete',
        contactSaved: 'Saved',
        contactsEmpty: 'No contacts yet',
        contactChatTitle: 'Ask the Agent about this contact',
        contactChatPlaceholder: 'e.g. Draft a formal reply to this person…',
        contactChatCopy: 'Copy prompt',
        contactChatCopied: 'Copied to clipboard',
        contactChatHint: 'DSH exposes no public API to inject a message into a running Agent, so this composes a prompt with the contact context for you to paste into the conversation.',
        reply: 'Reply',
        replyAll: 'Reply all',
        forward: 'Forward',
        send: 'Send',
        composeTitle: 'Compose',
        composeNew: 'New mail',
        composeTo: 'To',
        composeCc: 'Cc',
        composeSubject: 'Subject',
        composeBody: 'Body',
        composeClose: 'Close',
        composeCopyBody: 'Copy mail',
        composeCopied: 'Copied',
        composeSendBlocked: 'Sending saves the draft and requests approval first; approving happens in the Approvals rail, and the Core verifies the content hash before sending.',
        composeSubmit: 'Send',
        composePendingHint: 'Submitted for approval — it is sent only after you approve it in the rail.',
        approveAndSend: 'Approve and send',
        rejectApproval: 'Reject',
        approvalSendHint: 'Sending is agent-initiated and confirmed through the DSH approval prompt. Use "Copy send command" and paste it into the conversation.',
        copySendCommand: 'Copy send command',
        sendCommandCopied: 'Command copied',
        sentOk: 'Sent',
        decidedByUser: 'approved by user',
        decidedByPolicy: 'auto (policy)',
        decidedByAgent: 'approved by agent',
        approvalExpired: 'expired',
        agentChatTitle: 'Ask the Agent about this mail',
        agentChatPlaceholder: 'e.g. Draft a reply confirming the meeting on Friday 9:30…',
        agentChatCopy: 'Copy prompt',
        agentChatResize: 'Drag to resize, double-click to reset',
        todo: 'Follow-ups',
        ai: 'AI workspace',
        drafts: 'Drafts',
        approvals: 'Approvals',
        knowledge: 'Knowledge',
        settings: 'Settings',
        accounts: 'Accounts',
        allAccounts: 'All accounts',
        resize: 'Resize',
        resizeHint: 'Drag to resize, double-click to reset',
        noAccount: 'No mailbox configured',
        noAccountHint: 'Accounts and folders appear here once IMAP is wired up in Phase 1.',
        messages: 'Messages',
        noMessages: 'No messages',
        noMessagesHint: 'Once syncing works, this list shows sender, subject, summary and labels.',
        syncNow: 'Sync now',
        noSubject: '(no subject)',
        selectMessage: 'Select a message',
        selectMessageHint: 'Body, thread and AI analysis render on the right.',
        aiPanel: 'AI analysis',
        aiPanelHint: 'Summary, category, entities, amounts and action items appear here.',
        emptyList: 'None',
        pendingAi: 'Pending',
        todoDetailTitle: 'Follow-up detail',
        todoDetailHint: 'Pick a follow-up on the left to see the item, its source mail, due date and owner.',
        draftDetailTitle: 'Draft detail',
        draftDetailHint: 'Recipients, subject, body preview and approval state appear here.',
        approvalPending: 'Awaiting approval',
        approvalHistory: 'Approval history',
        approvalHistoryEmpty: 'No approval history',
        approvalHistoryHint: 'Approved and rejected actions stay here.',
        back: 'Back',
        approvalDetailTitle: 'Approval detail',
        approvalDetailHint: 'Action type, target account, recipients, body, attachments, impact and expiry appear here.',
        kbContacts: 'Contacts',
        kbCompanies: 'Companies',
        kbProjects: 'Projects',
        kbFiles: 'File types',
        kbDetailTitle: 'Knowledge entries',
        kbDetailHint: 'Pick a category on the left to list its entries here.',
        aiQueue: 'Pending',
        aiDone: 'Done',
        aiEmptyTitle: 'Nothing pending',
        aiEmptyHint: 'After AI processes new mail, items appear here as cards: pending content on top, a chat with the Agent below.',
        aiDoneEmpty: 'Nothing processed yet',
        prevCard: 'Previous',
        nextCard: 'Next',
        cardPendingContent: 'Pending content',
        cardChat: 'Chat with the Agent',
        cardChatEmpty: 'No messages yet',
        cardChatPlaceholder: 'Ask the Agent to handle this…',
        actDone: 'Mark done',
        actLater: 'Later',
        actDelete: 'Delete',
        backend: 'Mail service',
        connected: 'connected',
        offline: 'offline',
        coreState: 'Mail Core',
        coreVersion: 'Core version',
        coreDb: 'Database',
        coreDataDir: 'Data directory',
        coreError: 'Core error',
        accountsEmpty: 'No mailbox accounts yet',
        accountAdd: 'Add mailbox account',
        accountAddress: 'Address',
        accountDisplayName: 'Display name (optional)',
        accountHostImap: 'IMAP host',
        accountHostPop3: 'POP3 host',
        accountImapPort: 'Port',
        accountSecurity: 'Encryption',
        protocolLabel: 'Incoming protocol',
        protocolImap: 'IMAP',
        protocolPop3: 'POP3',
        smtpSection: 'Sending (SMTP)',
        smtpHost: 'SMTP host',
        smtpPort: 'SMTP port',
        smtpSecurity: 'SMTP encryption',
        smtpOptionalHint: 'Optional: leave it empty to read mail without sending.',
        accountUsername: 'Username',
        usernameHint: 'Defaults to the address; change it only if the server expects another login name.',
        accountPassword: 'Password / app password',
        passwordHint: 'Encrypted with Windows DPAPI before storage; the plugin process never sees the plaintext.',
        accountSubmit: 'Save account',
        accountSubmitting: 'Saving…',
        accountSaved: 'Account saved',
        accountFailed: 'Save failed',
        accountTest: 'Test connection',
        accountTesting: 'Connecting…',
        accountRemove: 'Remove',
        accountTestOk: 'Connected, folders',
        accountTestFailed: 'Connection failed',
        accountNoCredential: 'no credential',
        securitySsl: 'SSL',
        securityStarttls: 'STARTTLS',
        securityNone: 'None',
        polls: 'polls',
        loading: 'Loading…',
        failed: 'Failed to load',
        retry: 'Retry',
        refreshStatus: 'Sync all accounts and refresh',
        phase0: 'Phase 0 skeleton: entry point and data channel work; mail sync is not implemented yet.',
        settingsBackend: 'Mail Core URL',
        settingsPoll: 'Auto sync interval',
        settingsSyncInterval: 'Auto sync interval',
        settingsSyncIntervalHint: 'Integer minutes; 0 disables automatic sync. Only INBOX is synced; the panel still needs a manual refresh.',
        save: 'Save',
        syncIntervalInvalid: 'Enter an integer >= 0',
        syncIntervalSaved: 'Saved',
        settingsCreds: 'Credential storage',
        settingsCredsValue: 'Windows DPAPI (managed by Mail Core)',
        settingsAccounts: 'Mailbox accounts',
        settingsAccountsValue: 'Not configured',
        minute: 'min',
      },
    }

    const TABS = [
      ['todo', 'todo'],
      ['inbox', 'inbox'],
      ['sent', 'sent'],
      ['draftsMail', 'drafts'],
      ['deleted', 'deleted'],
      ['junk', 'junk'],
      ['templates', 'templates'],
      ['contacts', 'contacts'],
      ['knowledge', 'knowledge'],
      ['settings', 'settings'],
    ]

    const CARD_WIDTH = 520

    // ---- 样式（只用主题 token） --------------------------------------------
    const styles = {
      panel: {
        boxSizing: 'border-box',
        height: '100%',
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        color: 'var(--dsw-alias-label-primary)',
        background: 'var(--dsw-alias-bg-base)',
        fontFamily: 'var(--dsw-font-family)',
      },
      topbar: { flex: 'none', display: 'flex', alignItems: 'center', gap: 10, padding: '14px 20px 10px' },
      title: { fontSize: 16, fontWeight: 600, margin: 0 },
      spacer: { flex: 1 },
      pill: (ok) => ({
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '3px 10px',
        borderRadius: 999,
        fontSize: 12,
        color: 'var(--dsw-alias-label-secondary)',
        background: 'var(--dsw-alias-bg-layer-2)',
        border: '1px solid var(--dsw-alias-border-l2)',
      }),
      dot: (ok) => ({
        width: 7,
        height: 7,
        borderRadius: 4,
        background: ok ? 'var(--dsw-alias-state-success-primary)' : 'var(--dsw-alias-state-error-primary)',
      }),
      badge: (active) => ({
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '3px 10px',
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 500,
        color: active ? '#fff' : 'var(--dsw-alias-label-secondary)',
        background: active ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-bg-layer-2)',
        border: `1px solid ${active ? 'var(--dsw-alias-state-error-primary)' : 'var(--dsw-alias-border-l2)'}`,
      }),
      iconBtn: {
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 28,
        height: 28,
        borderRadius: 'var(--dsw-radius-sm)',
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'transparent',
        color: 'var(--dsw-alias-label-secondary)',
        cursor: 'pointer',
      },
      nav: { flex: 'none', display: 'flex', gap: 2, padding: '0 20px', borderBottom: '1px solid var(--dsw-alias-border-l2)' },
      tab: (active) => ({
        background: 'transparent',
        border: 'none',
        padding: '8px 12px',
        cursor: 'pointer',
        fontSize: 13,
        color: active ? 'var(--dsw-alias-label-primary)' : 'var(--dsw-alias-label-secondary)',
        fontWeight: active ? 500 : 400,
        boxShadow: active ? 'inset 0 -2px 0 var(--dsw-alias-state-business-primary)' : 'none',
      }),
      body: { flex: 1, minHeight: 0, display: 'flex' },
      handle: {
        width: HANDLE,
        flex: 'none',
        cursor: 'col-resize',
        touchAction: 'none',
        display: 'flex',
        alignItems: 'stretch',
        justifyContent: 'center',
        background: 'transparent',
      },
      handleLine: (active) => ({
        width: 1,
        background: active ? 'var(--dsw-alias-state-business-primary)' : 'var(--dsw-alias-border-l2)',
        transition: 'background 120ms',
      }),
      column: { flex: 'none', minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' },
      rail: { flex: 'none', overflowY: 'auto', padding: '12px 8px' },
      railTitle: {
        fontSize: 11,
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        color: 'var(--dsw-alias-label-tertiary)',
        padding: '4px 8px 8px',
      },
      railItem: {
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 8px',
        fontSize: 13,
        borderRadius: 'var(--dsw-radius-sm)',
        color: 'var(--dsw-alias-label-secondary)',
      },
      list: { flex: 'none', overflowY: 'auto', display: 'flex', flexDirection: 'column' },
      listColumn: { flex: 1, minHeight: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column' },
      listHead: {
        flex: 'none',
        padding: '12px 16px 8px',
        fontSize: 11,
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        color: 'var(--dsw-alias-label-tertiary)',
        borderBottom: '1px solid var(--dsw-alias-border-l1)',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
      },
      listCount: { color: 'var(--dsw-alias-label-tertiary)', fontVariantNumeric: 'tabular-nums' },
      listItem: {
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        padding: '9px 16px',
        borderBottom: '1px solid var(--dsw-alias-border-l1)',
        cursor: 'pointer',
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        fontSize: 13,
        textAlign: 'left',
        border: 'none',
        width: '100%',
      },
      listItemMeta: { fontSize: 11, color: 'var(--dsw-alias-label-tertiary)' },
      msgItem: {
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        width: '100%',
        textAlign: 'left',
        padding: '9px 16px',
        border: 'none',
        borderBottom: '1px solid var(--dsw-alias-border-l1)',
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        cursor: 'pointer',
        fontSize: 13,
      },
      msgTitle: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      msgMeta: {
        fontSize: 11,
        color: 'var(--dsw-alias-label-tertiary)',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
      },
      msgSubject: { fontSize: 16, fontWeight: 600, margin: '0 0 6px' },
      thread: { display: 'flex', flexDirection: 'column', gap: 10, paddingTop: 4 },
      bubble: {
        maxWidth: '78%',
        display: 'flex',
        flexDirection: 'column',
        gap: 2,
        padding: '8px 12px',
        borderRadius: 'var(--dsw-radius-md)',
        border: '1px solid var(--dsw-alias-border-l1)',
      },
      bubbleMine: {
        alignSelf: 'flex-end',
        background: 'var(--dsw-alias-state-business-primary)',
        color: 'var(--dsw-alias-label-primary-inverted)',
        borderColor: 'transparent',
      },
      bubbleTheirs: { alignSelf: 'flex-start', background: 'var(--dsw-alias-bg-layer-2)' },
      bubbleHead: { fontSize: 11, opacity: 0.8, display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' },
      bubbleName: { fontWeight: 500 },
      bubbleBody: {
        fontSize: 13,
        lineHeight: 1.7,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        margin: 0,
        fontFamily: 'var(--dsw-font-family)',
        color: 'inherit',
      },
      bubbleToggle: {
        marginTop: 6,
        fontSize: 11,
        background: 'transparent',
        border: 'none',
        padding: 0,
        color: 'inherit',
        opacity: 0.75,
        cursor: 'pointer',
        textAlign: 'left',
      },
      msgBody: {
        fontSize: 13,
        lineHeight: 1.7,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        fontFamily: 'var(--dsw-font-family)',
        color: 'var(--dsw-alias-label-primary)',
        margin: 0,
      },
      detail: { flex: 1, minWidth: 0, overflowY: 'auto', padding: '16px 20px' },
      paneHeader: {
        flex: 'none',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '10px 16px',
        borderBottom: '1px solid var(--dsw-alias-border-l1)',
      },
      paneHeaderText: { fontSize: 12, color: 'var(--dsw-alias-label-tertiary)' },
      reading: { flex: 1, minWidth: 0, overflowY: 'auto', padding: '16px 20px' },
      empty: { margin: 'auto', textAlign: 'center', padding: '40px 24px', color: 'var(--dsw-alias-label-tertiary)' },
      emptyIcon: {
        width: 40,
        height: 40,
        margin: '0 auto 12px',
        borderRadius: 10,
        border: '1px dashed var(--dsw-alias-border-l3)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'var(--dsw-alias-label-tertiary)',
      },
      emptyTitle: { fontSize: 13, color: 'var(--dsw-alias-label-secondary)', margin: '0 0 4px' },
      emptyHint: { fontSize: 12, margin: 0, lineHeight: 1.6 },
      card: {
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 'var(--dsw-radius-md)',
        background: 'var(--dsw-alias-bg-layer-1)',
        padding: 16,
        marginBottom: 12,
      },
      row: {
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        gap: 16,
        padding: '7px 0',
        fontSize: 13,
      },
      rowKey: { color: 'var(--dsw-alias-label-secondary)' },
      rowVal: { color: 'var(--dsw-alias-label-primary)', textAlign: 'right' },
      sectionTitle: { fontSize: 13, fontWeight: 500, margin: '0 0 10px' },
      muted: { color: 'var(--dsw-alias-label-tertiary)', fontSize: 12, margin: '4px 0 0' },
      error: { color: 'var(--dsw-alias-state-error-primary)', fontSize: 13, margin: '0 0 8px' },
      errBox: { padding: '16px 20px' },
      btn: {
        marginTop: 8,
        padding: '5px 12px',
        borderRadius: 'var(--dsw-radius-sm)',
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        fontSize: 12,
        cursor: 'pointer',
      },
      btnSmall: {
        padding: '3px 10px',
        borderRadius: 'var(--dsw-radius-sm)',
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'transparent',
        color: 'var(--dsw-alias-label-secondary)',
        fontSize: 11,
        cursor: 'pointer',
      },
      btnDisabled: {
        padding: '3px 10px',
        borderRadius: 'var(--dsw-radius-sm)',
        border: '1px solid var(--dsw-alias-border-l1)',
        background: 'transparent',
        color: 'var(--dsw-alias-label-tertiary)',
        fontSize: 11,
        cursor: 'default',
        opacity: 0.6,
      },
      carousel: {
        flex: 1,
        minHeight: 0,
        display: 'flex',
        gap: 12,
        overflowX: 'auto',
        overflowY: 'hidden',
        padding: '16px 20px',
        scrollSnapType: 'x mandatory',
      },
      cardHost: {
        flex: 1,
        minHeight: 0,
        minWidth: 0,
        display: 'flex',
        padding: '16px 20px',
        overflow: 'hidden',
      },
      aiCard: {
        width: CARD_WIDTH,
        flex: 'none',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 'var(--dsw-radius-md)',
        background: 'var(--dsw-alias-bg-layer-1)',
        overflow: 'hidden',
        scrollSnapAlign: 'start',
      },
      aiCardHead: {
        flex: 'none',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '10px 14px',
        borderBottom: '1px solid var(--dsw-alias-border-l1)',
      },
      aiCardTitle: { fontSize: 13, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
      aiCardTop: { flex: '1 1 auto', minHeight: 0, overflowY: 'auto', padding: '12px 14px' },
      aiCardBottom: {
        flex: '0 0 46%',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        borderTop: '1px solid var(--dsw-alias-border-l2)',
        background: 'var(--dsw-alias-bg-base)',
      },
      chatList: { flex: 1, minHeight: 0, overflowY: 'auto', padding: '10px 14px' },
      chatInputRow: { flex: 'none', display: 'flex', gap: 8, padding: '10px 14px', borderTop: '1px solid var(--dsw-alias-border-l1)' },
      input: {
        flex: 1,
        background: 'transparent',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 'var(--dsw-radius-sm)',
        padding: '6px 10px',
        color: 'var(--dsw-alias-label-primary)',
        fontSize: 12,
      },
      settingsBox: { maxWidth: 720, margin: '0 auto' },
      navScroll: {
        flex: 'none',
        display: 'flex',
        gap: 2,
        padding: '0 20px',
        borderBottom: '1px solid var(--dsw-alias-border-l2)',
        overflowX: 'auto',
        overflowY: 'hidden',
      },
      searchWrap: {
        flex: '0 1 420px',
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        minWidth: 160,
        padding: '3px 10px',
        borderRadius: 999,
        border: '1px solid var(--dsw-alias-border-l2)',
        background: 'var(--dsw-alias-bg-layer-2)',
      },
      searchInput: {
        flex: 1,
        minWidth: 0,
        border: 'none',
        outline: 'none',
        background: 'transparent',
        color: 'var(--dsw-alias-label-primary)',
        fontSize: 12,
        fontFamily: 'inherit',
      },
      formRow: { display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 10 },
      textArea: {
        background: 'transparent',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 'var(--dsw-radius-sm)',
        padding: '8px 10px',
        color: 'var(--dsw-alias-label-primary)',
        fontSize: 13,
        fontFamily: 'inherit',
        minHeight: 72,
        resize: 'vertical',
      },
      formActions: { display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 },
      chatBox: {
        marginTop: 18,
        borderTop: '1px solid var(--dsw-alias-border-l2)',
        paddingTop: 12,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      },
      detailActions: { display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' },
      modalBackdrop: {
        position: 'fixed',
        inset: 0,
        background: 'rgba(0, 0, 0, 0.45)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 60,
        padding: 24,
      },
      modal: {
        width: 'min(860px, 92vw)',
        maxHeight: '86vh',
        overflowY: 'auto',
        background: 'var(--dsw-alias-bg-base)',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 'var(--dsw-radius-md)',
        padding: 18,
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        boxShadow: 'var(--dsw-elevation-lg, 0 12px 40px rgba(0,0,0,0.35))',
      },
      modalHead: { display: 'flex', alignItems: 'center', gap: 8 },
      readingScroll: { flex: 1, minHeight: 0, overflowY: 'auto', padding: '16px 20px' },
      agentWrap: { flex: 'none', display: 'flex', flexDirection: 'column' },
      rowHandle: {
        height: 7,
        flex: 'none',
        cursor: 'row-resize',
        touchAction: 'none',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'transparent',
      },
      rowHandleLine: (active) => ({
        height: 1,
        width: '100%',
        background: active ? 'var(--dsw-alias-state-business-primary)' : 'var(--dsw-alias-border-l2)',
        transition: 'background 120ms',
      }),
      agentDock: {
        flex: 'none',
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        padding: '4px 20px 12px',
        borderTop: '1px solid var(--dsw-alias-border-l2)',
        background: 'var(--dsw-alias-bg-layer-1)',
        minHeight: 0,
      },
      detailShell: { display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, minHeight: 0 },
      railPanel: {
        width: 280,
        flex: 'none',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        borderLeft: '1px solid var(--dsw-alias-border-l2)',
        background: 'var(--dsw-alias-bg-layer-1)',
      },
      railHeader: {
        flex: 'none',
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '10px 12px',
        borderBottom: '1px solid var(--dsw-alias-border-l1)',
      },
      railHeaderTitle: { fontSize: 12, fontWeight: 500, color: 'var(--dsw-alias-label-primary)' },
      railCount: {
        minWidth: 18,
        padding: '0 6px',
        borderRadius: 999,
        fontSize: 11,
        lineHeight: '16px',
        textAlign: 'center',
        color: '#fff',
        background: 'var(--dsw-alias-state-error-primary)',
      },
      railBody: { flex: 1, minHeight: 0, overflowY: 'auto', padding: '8px 0' },
      railEmpty: { padding: '16px 14px', fontSize: 12, color: 'var(--dsw-alias-label-tertiary)', lineHeight: 1.6 },
      accountRow: {
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '10px 0',
        borderTop: '1px solid var(--dsw-alias-border-l1)',
      },
      dangerBtn: {
        padding: '3px 10px',
        borderRadius: 'var(--dsw-radius-sm)',
        border: '1px solid var(--dsw-alias-state-error-primary)',
        background: 'transparent',
        color: 'var(--dsw-alias-state-error-primary)',
        fontSize: 11,
        cursor: 'pointer',
      },
      formGrid: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 },
      formSubhead: {
        margin: '6px 0 0',
        paddingTop: 10,
        borderTop: '1px solid var(--dsw-alias-border-l1)',
        fontSize: 12,
        fontWeight: 500,
        color: 'var(--dsw-alias-label-secondary)',
      },
      field: { display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 },
      label: { fontSize: 12, color: 'var(--dsw-alias-label-secondary)' },
      textInput: {
        background: 'transparent',
        border: '1px solid var(--dsw-alias-border-l2)',
        borderRadius: 'var(--dsw-radius-sm)',
        padding: '7px 10px',
        color: 'var(--dsw-alias-label-primary)',
        fontSize: 13,
        fontFamily: 'inherit',
      },
    }

    // 原生 <select> 的弹出列表由浏览器绘制，内联样式管不到，只能靠 color-scheme 跟随主题
    const SELECT_CSS = [
      '.dsh-mail-select{color-scheme:light;}',
      'body[data-ds-dark-theme] .dsh-mail-select{color-scheme:dark;}',
      '.dsh-mail-select option{background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);}',
    ].join('')

    // ---- 数据通道 ----------------------------------------------------------
    async function call(path, body, signal) {
      const res = await fetch(API + path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body ?? {}),
        signal,
      })
      const json = await res.json().catch(() => null)
      if (!res.ok || json === null || json.ok === false) {
        throw new Error((json && json.message) || `HTTP ${res.status}`)
      }
      return json.value
    }

    // ---- 图标 --------------------------------------------------------------
    function MailIcon({ size = 16 }) {
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 16 16',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.3,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': true,
        },
        h('rect', { x: 1.5, y: 3, width: 13, height: 10, rx: 2 }),
        h('path', { d: 'M2 4l6 4.5L14 4' }),
      )
    }

    function RefreshIcon({ size = 14 }) {
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 16 16',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.4,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': true,
        },
        h('path', { d: 'M13.5 8a5.5 5.5 0 1 1-1.7-3.97' }),
        h('path', { d: 'M13.5 2.5v3h-3' }),
      )
    }

    /** 时钟：与宿主「自动化任务」侧栏入口同款字形（描边圆 + 指向 12 与 4 点的指针）。 */
    function ClockIcon({ size = 14 }) {
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 16 16',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.3,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': true,
        },
        h('circle', { cx: 8, cy: 8, r: 6.1 }),
        h('path', { d: 'M8 4.5V8l2.6 2.2' }),
      )
    }

    /** 返回：左向折角。 */
    function BackIcon({ size = 14 }) {
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 16 16',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.5,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': true,
        },
        h('path', { d: 'M9.8 3.4L5.2 8l4.6 4.6' }),
      )
    }

    // ---- 基础块 ------------------------------------------------------------
    function Empty({ title, hint }) {
      return h(
        'div',
        { style: styles.empty },
        h('div', { style: styles.emptyIcon }, h(MailIcon, { size: 18 })),
        h('p', { style: styles.emptyTitle }, title),
        h('p', { style: styles.emptyHint }, hint),
      )
    }

    function Row({ label, value }) {
      return h(
        'div',
        { style: styles.row },
        h('span', { style: styles.rowKey }, label),
        h('span', { style: styles.rowVal }, value),
      )
    }

    function EmptyPane({ title, hint }) {
      return h(
        'div',
        { style: { ...styles.reading, display: 'flex', alignItems: 'center', justifyContent: 'center' } },
        h(Empty, { title, hint }),
      )
    }

    /** 两栏布局：左列宽度可拖动，右列吃掉剩余空间。 */
    function SplitPane({ storageKey, spec, minRight = DETAIL_MIN, label, left, right }) {
      const containerRef = React.useRef(null)
      const dragRef = React.useRef(null)
      const [width, setWidth] = React.useState(() => storedWidth(storageKey, spec))
      const [dragging, setDragging] = React.useState(false)
      const [hovered, setHovered] = React.useState(false)

      React.useEffect(() => {
        if (!dragging) storeWidth(storageKey, width)
      }, [width, dragging, storageKey])

      // 挂载后按实际容器宽度收一次，避免从宽窗口切到窄窗口时挤掉右侧
      React.useEffect(() => {
        const total = containerRef.current ? containerRef.current.clientWidth : 0
        if (!total) return
        setWidth((prev) => clampPane(prev, spec, minRight, total))
      }, [])

      const down = (event) => {
        if (typeof event.button === 'number' && event.button !== 0) return
        event.preventDefault()
        dragRef.current = {
          startX: event.clientX,
          startWidth: width,
          total: containerRef.current ? containerRef.current.clientWidth : 0,
        }
        setDragging(true)
        if (typeof event.currentTarget.setPointerCapture === 'function') {
          try {
            event.currentTarget.setPointerCapture(event.pointerId)
          } catch {
            // 捕获失败不影响基本拖动
          }
        }
      }

      const move = (event) => {
        const session = dragRef.current
        if (!session) return
        const next = clampPane(session.startWidth + (event.clientX - session.startX), spec, minRight, session.total)
        setWidth((prev) => (prev === next ? prev : next))
      }

      const up = (event) => {
        if (!dragRef.current) return
        dragRef.current = null
        setDragging(false)
        if (typeof event.currentTarget.releasePointerCapture === 'function') {
          try {
            event.currentTarget.releasePointerCapture(event.pointerId)
          } catch {
            // 同上
          }
        }
      }

      const nudge = (event) => {
        const step = event.key === 'ArrowRight' ? 16 : event.key === 'ArrowLeft' ? -16 : 0
        if (step === 0) return
        event.preventDefault()
        const total = containerRef.current ? containerRef.current.clientWidth : 0
        setWidth((prev) => clampPane(prev + step, spec, minRight, total))
      }

      return h(
        'div',
        {
          ref: containerRef,
          style: { display: 'flex', flex: 1, minHeight: 0, userSelect: dragging ? 'none' : undefined },
        },
        h('div', { style: { ...styles.column, width } }, left),
        h(
          'div',
          {
            role: 'separator',
            'aria-orientation': 'vertical',
            'aria-label': label,
            'aria-valuenow': width,
            tabIndex: 0,
            title: label,
            style: styles.handle,
            onPointerDown: down,
            onPointerMove: move,
            onPointerUp: up,
            onPointerCancel: up,
            onPointerEnter: () => setHovered(true),
            onPointerLeave: () => setHovered(false),
            onDoubleClick: () => setWidth(spec.initial),
            onKeyDown: nudge,
          },
          h('div', { style: styles.handleLine(dragging || hovered) }),
        ),
        h('div', { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' } }, right),
      )
    }

    /** 左列：分组列表。groups = [{ id, title, items: [{ id, title, meta }] }] */
    function GroupList({ t, groups, selectedId, onSelect, headerActions }) {
      return h(
        'div',
        { style: styles.listColumn },
        groups.map((group, index) =>
          h(
            'div',
            { key: group.id },
            h(
              'div',
              { style: index > 0 ? { ...styles.listHead, borderTop: '1px solid var(--dsw-alias-border-l2)' } : styles.listHead },
              group.title,
              h('span', { style: styles.listCount }, group.items.length),
              index === 0 && headerActions ? h('span', { style: styles.spacer }) : null,
              index === 0 ? headerActions : null,
            ),
            group.items.length === 0
              ? h('p', { style: { ...styles.muted, padding: '8px 16px', margin: 0 } }, t('emptyList'))
              : group.items.map((item) =>
                  h(
                    'button',
                    {
                      key: item.id,
                      type: 'button',
                      'aria-pressed': selectedId === item.id,
                      onClick: () => onSelect && onSelect(item.id),
                      style:
                        selectedId === item.id
                          ? { ...styles.listItem, background: 'var(--dsw-alias-interactive-bg-hover)' }
                          : styles.listItem,
                    },
                    h('span', null, item.title),
                    item.meta ? h('span', { style: styles.listItemMeta }, item.meta) : null,
                  ),
                ),
          ),
        ),
      )
    }

    function DetailPane({ t, titleKey, hintKey }) {
      return h(
        'div',
        { style: { ...styles.detail, display: 'flex', alignItems: 'center', justifyContent: 'center' } },
        h(Empty, { title: t(titleKey), hint: t(hintKey) }),
      )
    }

    // ---- 正文：把引用历史拆成对话气泡 ---------------------------------------
    const QUOTE_BOUNDARY = [
      /^-{2,}\s*(原始邮件|转发邮件|Original Message|Forwarded message)\s*-{2,}\s*$/im,
      /^-{2,}\s*Original Appointment\s*-{2,}\s*$/im,
      /^-{5,}\s*$/m,
      /^On .{4,120} wrote:\s*$/im,
      /^在 .{4,120} 写道[:：]\s*$/m,
    ]

    const HEADER_LINE = /^(From|发件人|Sent|Send Time|发送时间|Date|日期|To|收件人|Cc|抄送|Subject|主题)\s*[:：]\s*(.*)$/i

    /** 边界行本身（分割线 / "On ... wrote:"）不算正文，切段后要丢掉。 */
    const BOUNDARY_LINE =
      /^(-{2,}\s*(原始邮件|转发邮件|Original Message|Forwarded message|Original Appointment)\s*-{2,}|-{5,}|On .{4,120} wrote:|在 .{4,120} 写道[:：])\s*$/i

    /** 从一段引用里剥出 From/To/Subject/Date 头，剩下的算正文。 */
    function parseQuoteHeaders(block) {
      const lines = block.split('\n')
      const header = {}
      let index = 0
      while (index < lines.length) {
        const match = HEADER_LINE.exec(lines[index].trim())
        if (!match) break
        const key = match[1].toLowerCase()
        const value = match[2].trim()
        if (/^(from|发件人)$/.test(key)) header.from = header.from || value
        else if (/^(to|收件人)$/.test(key)) header.to = header.to || value
        else if (/^(subject|主题)$/.test(key)) header.subject = header.subject || value
        else if (/^(sent|send time|发送时间|date|日期)$/.test(key)) header.date = header.date || value
        index += 1
      }
      return { header, rest: lines.slice(index).join('\n') }
    }

    /** 第 0 段是当前邮件；其余是被引用的历史邮件。 */
    function splitQuoted(text) {
      const source = String(text || '').replace(/\r\n/g, '\n')
      const marks = []
      for (const pattern of QUOTE_BOUNDARY) {
        const flags = pattern.flags.includes('g') ? pattern.flags : `${pattern.flags}g`
        const global = new RegExp(pattern.source, flags)
        let match = global.exec(source)
        while (match !== null) {
          if (match.index > 0) marks.push(match.index)
          match = global.exec(source)
        }
      }
      const cuts = [...new Set(marks)].sort((a, b) => a - b)
      const parts = []
      let start = 0
      for (const cut of cuts) {
        if (cut <= start) continue
        parts.push(source.slice(start, cut))
        start = cut
      }
      parts.push(source.slice(start))
      return parts
        .map((part) => part.replace(/^\s+|\s+$/g, ''))
        .filter((part) => part.length > 0)
        .map((part, index) => {
          const lines = part.split('\n')
          // 丢掉开头的空行和边界行，否则引用头解析不到 From/Date
          while (lines.length > 0 && (lines[0].trim() === '' || BOUNDARY_LINE.test(lines[0].trim()))) {
            lines.shift()
          }
          const { header, rest } = parseQuoteHeaders(lines.join('\n'))
          const body = rest
            .split('\n')
            .map((line) => line.replace(/^\s*>+\s?/, ''))
            .join('\n')
            .replace(/^\s+|\s+$/g, '')
          return { header, body, quoted: index > 0 }
        })
    }

    function isMine(header, accountAddress) {
      if (!header || !header.from || !accountAddress) return false
      return String(header.from).toLowerCase().includes(String(accountAddress).toLowerCase())
    }

    /** 对话气泡：最新一封在最上方且常显，历史引用排在下面、默认折叠。 */
    function ThreadBubbles({ t, message, accountAddress }) {
      const segments = React.useMemo(() => splitQuoted(message.text), [message.text])
      const [expanded, setExpanded] = React.useState({})
      const ordered = segments.map((segment, index) => ({ segment, latest: index === 0 }))

      return h(
        'div',
        { style: styles.thread },
        ordered.map(({ segment, latest }, position) => {
          const key = `seg-${position}`
          const open = latest || expanded[key] === true
          const header = latest ? { from: message.fromAddr, date: message.date } : segment.header
          const mine = isMine(header, accountAddress)
          return h(
            'div',
            { key, style: { ...styles.bubble, ...(mine ? styles.bubbleMine : styles.bubbleTheirs) } },
            h(
              'div',
              { style: styles.bubbleHead },
              h('span', { style: styles.bubbleName }, header.from || (latest ? t('selectMessage') : t('earlierMail'))),
              h('span', null, header.date || ''),
            ),
            open ? h('pre', { style: styles.bubbleBody }, segment.body) : null,
            latest
              ? null
              : h(
                  'button',
                  {
                    type: 'button',
                    style: styles.bubbleToggle,
                    'aria-expanded': open,
                    onClick: () => setExpanded((prev) => ({ ...prev, [key]: !prev[key] })),
                  },
                  open
                    ? t('clickToCollapse')
                    : `${t('clickToExpand')} · ${(segment.body.split('\n').find((line) => line.trim().length > 0) || '').slice(0, 40)}`,
                ),
          )
        }),
      )
    }

    // ---- 撰写 / Agent 区 ----------------------------------------------------
    function buildCompose(mode, message, t) {
      const subject = message.subject || ''
      const prefixed = (prefix) => (/^re[:：]/i.test(subject) ? subject : `${prefix}${subject}`)
      const quoted = [
        '',
        '------------------ 原邮件 ------------------',
        `发件人: ${message.fromAddr || ''}`,
        `时间: ${message.date || ''}`,
        `主题: ${subject}`,
        '',
        message.text || '',
      ].join('\n')

      if (mode === 'forward') {
        return { mode, to: '', cc: '', subject: prefixed('Fwd: '), body: quoted }
      }
      if (mode === 'replyAll') {
        const cc = [message.toAddr, message.ccAddr].filter(Boolean).join(', ')
        return { mode, to: message.fromAddr || '', cc, subject: prefixed('Re: '), body: quoted }
      }
      if (mode === 'reply') {
        return { mode, to: message.fromAddr || '', cc: '', subject: prefixed('Re: '), body: quoted }
      }
      // 草稿/模板的“发送”：沿用原邮件的收件人与正文
      return {
        mode: 'send',
        to: message.toAddr || '',
        cc: message.ccAddr || '',
        subject,
        body: message.text || '',
      }
    }

    /** 回复/转发/发送共用的编辑页。发送未接通（Phase 5 需审批），先提供复制。 */
    function ComposeModal({ t, initial, onClose, accountId, onSubmitted }) {
      const [draft, setDraft] = React.useState(initial)
      const [copied, setCopied] = React.useState(false)
      const [busy, setBusy] = React.useState(false)
      const [note, setNote] = React.useState(null)

      const submit = async () => {
        if (busy) return
        if (!accountId) {
          setNote(t('accountNoCredential'))
          return
        }
        setBusy(true)
        setNote(null)
        try {
          const saved = await call('/drafts/save', {
            accountId,
            to: draft.to,
            cc: draft.cc,
            subject: draft.subject,
            body: draft.body,
          })
          await call('/approvals/request', { draftId: saved.id })
          setNote(t('composePendingHint'))
          if (onSubmitted) onSubmitted()
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(false)
        }
      }
      const field = (labelKey, key, extra) =>
        h(
          'label',
          { style: styles.formRow },
          h('span', { style: styles.label }, t(labelKey)),
          h('input', {
            style: styles.textInput,
            value: draft[key],
            onChange: (event) => setDraft((prev) => ({ ...prev, [key]: event.target.value })),
            ...extra,
          }),
        )

      const copy = async () => {
        const text = `收件人: ${draft.to}\n抄送: ${draft.cc}\n主题: ${draft.subject}\n\n${draft.body}`
        try {
          if (typeof navigator !== 'undefined' && navigator.clipboard) await navigator.clipboard.writeText(text)
          setCopied(true)
        } catch {
          setCopied(false)
        }
      }

      return h(
        'div',
        { style: styles.modalBackdrop, role: 'dialog', 'aria-modal': 'true', 'aria-label': t('composeTitle') },
        h(
          'div',
          { style: styles.modal },
          h(
            'div',
            { style: styles.modalHead },
            h('span', { style: styles.sectionTitle }, t('composeTitle')),
            h('span', { style: styles.spacer }),
            h('button', { type: 'button', style: styles.iconBtn, title: t('composeClose'), onClick: onClose }, '✕'),
          ),
          h('div', { style: styles.formGrid }, field('composeTo', 'to'), field('composeCc', 'cc')),
          field('composeSubject', 'subject'),
          h(
            'label',
            { style: styles.formRow },
            h('span', { style: styles.label }, t('composeBody')),
            h('textarea', {
              style: { ...styles.textArea, minHeight: 220 },
              value: draft.body,
              onChange: (event) => setDraft((prev) => ({ ...prev, body: event.target.value })),
            }),
          ),
          h('p', { style: { ...styles.muted, margin: 0 } }, t('composeSendBlocked')),
          note ? h('p', { style: { ...styles.muted, margin: 0 } }, note) : null,
          h(
            'div',
            { style: styles.formActions },
            h('button', { type: 'button', style: styles.btn, disabled: busy, onClick: submit }, t('composeSubmit')),
            h('button', { type: 'button', style: styles.btnSmall, onClick: copy }, t('composeCopyBody')),
            copied ? h('span', { style: styles.muted }, t('composeCopied')) : null,
            h('span', { style: styles.spacer }),
            h('button', { type: 'button', style: styles.btnSmall, onClick: onClose }, t('composeClose')),
          ),
        ),
      )
    }

    /** 详情页底部的 Agent 区：常驻阅读区底部，高度可上下拖动，正文单独滚动。 */
    function AgentMailComposer({ t, message }) {
      const [text, setText] = React.useState('')
      const [copied, setCopied] = React.useState(false)
      const DOCK_SPEC = { initial: 120, min: 76, max: 420 }
      const [height, setHeight] = React.useState(() => storedWidth('agentDock', DOCK_SPEC))
      const [dragging, setDragging] = React.useState(false)
      const [hovered, setHovered] = React.useState(false)
      const dragRef = React.useRef(null)

      React.useEffect(() => {
        storeWidth('agentDock', height)
      }, [height])

      const down = (event) => {
        if (typeof event.button === 'number' && event.button !== 0) return
        event.preventDefault()
        dragRef.current = { startY: event.clientY, startHeight: height }
        setDragging(true)
        if (typeof event.currentTarget.setPointerCapture === 'function') {
          try {
            event.currentTarget.setPointerCapture(event.pointerId)
          } catch {
            // 捕获失败不影响基本拖动
          }
        }
      }

      const move = (event) => {
        const session = dragRef.current
        if (!session) return
        // 往上拖变高
        const next = Math.round(clamp(session.startHeight + (session.startY - event.clientY), DOCK_SPEC.min, DOCK_SPEC.max))
        setHeight((prev) => (prev === next ? prev : next))
      }

      const up = (event) => {
        if (!dragRef.current) return
        dragRef.current = null
        setDragging(false)
        if (typeof event.currentTarget.releasePointerCapture === 'function') {
          try {
            event.currentTarget.releasePointerCapture(event.pointerId)
          } catch {
            // 同上
          }
        }
      }

      const prompt = [
        `邮件主题：${message.subject || ''}`,
        `发件人：${message.fromAddr || ''}`,
        `时间：${message.date || ''}`,
        '',
        (message.text || '').slice(0, 2000),
        '',
        `请基于以上邮件：${text.trim() || '帮我起草一封回复。'}`,
      ].join('\n')

      const copy = async () => {
        try {
          if (typeof navigator !== 'undefined' && navigator.clipboard) await navigator.clipboard.writeText(prompt)
          setCopied(true)
        } catch {
          setCopied(false)
        }
      }

      return h(
        'div',
        { style: styles.agentWrap, 'data-mail-agent-dock': 'true' },
        h(
          'div',
          {
            role: 'separator',
            'aria-orientation': 'horizontal',
            'aria-label': t('agentChatResize'),
            title: t('agentChatResize'),
            style: styles.rowHandle,
            onPointerDown: down,
            onPointerMove: move,
            onPointerUp: up,
            onPointerCancel: up,
            onPointerEnter: () => setHovered(true),
            onPointerLeave: () => setHovered(false),
            onDoubleClick: () => setHeight(DOCK_SPEC.initial),
          },
          h('div', { style: styles.rowHandleLine(dragging || hovered) }),
        ),
        h(
          'div',
          { style: { ...styles.agentDock, height } },
          h(
            'div',
            { style: { display: 'flex', alignItems: 'center', gap: 8, flex: 'none' } },
            h('span', { style: { ...styles.sectionTitle, margin: 0 } }, t('agentChatTitle')),
            h('span', { style: styles.spacer }),
            copied ? h('span', { style: { ...styles.muted, margin: 0 } }, t('composeCopied')) : null,
            h('button', { type: 'button', style: styles.btnSmall, onClick: copy }, t('agentChatCopy')),
          ),
          h('textarea', {
            style: { ...styles.textArea, flex: 1, minHeight: 0, resize: 'none' },
            value: text,
            placeholder: t('agentChatPlaceholder'),
            onChange: (event) => {
              setText(event.target.value)
              setCopied(false)
            },
          }),
        ),
      )
    }

    /** 账户栏：收件箱与待跟进共用；null 表示「全部账户」。 */
    function AccountRail({ t, accounts, accountId, onSelect }) {
      const label = (account) => (account.displayName ? account.displayName : account.address)
      const row = (key, text, withIcon) =>
        h(
          'button',
          {
            key,
            type: 'button',
            onClick: () => onSelect(key === 'all' ? null : key),
            'aria-pressed': key === 'all' ? accountId === null : accountId === key,
            style: {
              ...styles.railItem,
              width: '100%',
              border: 'none',
              textAlign: 'left',
              cursor: 'pointer',
              background:
                (key === 'all' ? accountId === null : accountId === key)
                  ? 'var(--dsw-alias-interactive-bg-hover)'
                  : 'transparent',
            },
          },
          withIcon ? h(MailIcon, { size: 14 }) : null,
          text,
        )

      return h(
        'div',
        { style: { ...styles.rail, width: '100%', boxSizing: 'border-box' }, 'aria-label': t('accounts') },
        h('div', { style: styles.railTitle }, t('accounts')),
        accounts.length === 0
          ? h(
              'div',
              { style: { padding: '4px 8px' } },
              h('div', { style: { fontSize: 13, color: 'var(--dsw-alias-label-secondary)' } }, t('noAccount')),
              h('p', { style: styles.muted }, t('noAccountHint')),
            )
          : [
              row('all', t('allAccounts'), false),
              ...accounts.map((account) => row(account.id, label(account), true)),
            ],
      )
    }

    // ---- 收件箱 ------------------------------------------------------------
    /** 中间栏：本地索引的邮件列表（按时间倒序）。 */
    function MessageList({ t, messages, selectedId, onSelect, onSync, syncing }) {
      return h(
        'div',
        { style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } },
        h(
          'div',
          { style: styles.listHead },
          t('messages'),
          h('span', { style: styles.spacer }),
          h('span', { style: styles.listCount }, String(messages.length)),
          onSync
            ? h(
                'button',
                {
                  type: 'button',
                  style: styles.iconBtn,
                  title: t('syncNow'),
                  'aria-label': t('syncNow'),
                  disabled: syncing,
                  onClick: onSync,
                },
                h(RefreshIcon, {}),
              )
            : null,
        ),
        messages.length === 0
          ? h(Empty, { title: t('noMessages'), hint: t('noMessagesHint') })
          : h(
              'div',
              { style: { flex: 1, minHeight: 0, overflowY: 'auto' } },
              messages.map((message) =>
                h(
                  'button',
                  {
                    key: message.id,
                    type: 'button',
                    'aria-pressed': selectedId === message.id,
                    onClick: () => onSelect && onSelect(message.id),
                    style:
                      selectedId === message.id
                        ? { ...styles.msgItem, background: 'var(--dsw-alias-interactive-bg-hover)' }
                        : styles.msgItem,
                  },
                  h('span', { style: styles.msgTitle }, message.subject || t('noSubject')),
                  h('span', { style: styles.msgMeta }, `${message.fromAddr || '?'} · ${message.date || '?'}`),
                ),
              ),
            ),
      )
    }

    /** 右栏：选中邮件的正文。 */
    function MessageDetail({ t, message, accountAddress, actions, agentChat }) {
      if (!message) {
        return h(
          'div',
          { style: styles.reading, 'aria-label': t('selectMessage') },
          h(Empty, { title: t('selectMessage'), hint: t('selectMessageHint') }),
          h(
            'div',
            { style: styles.card },
            h('p', { style: styles.sectionTitle }, t('aiPanel')),
            h('p', { style: styles.muted }, t('aiPanelHint')),
          ),
        )
      }
      return h(
        'div',
        { style: styles.detailShell },
        h(
          'div',
          { style: styles.readingScroll, 'data-mail-scroll': 'true' },
          actions && actions.length > 0
            ? h(
                'div',
                { style: styles.detailActions },
                actions.map((action) =>
                  h('button', { key: action.key, type: 'button', style: styles.btnSmall, onClick: action.onClick }, action.label),
                ),
              )
            : null,
          h('h2', { style: styles.msgSubject }, message.subject || t('noSubject')),
          h('p', { style: { ...styles.muted, marginBottom: 12 } }, `${message.fromAddr || '?'} · ${message.date || '?'}`),
          h(ThreadBubbles, { t, message, accountAddress }),
        ),
        agentChat ? h(AgentMailComposer, { t, message }) : null,
      )
    }
    function InboxView({ t, status, onRefresh, accountId, onSelectAccount }) {
      const accounts = (status && status.accounts) || []
      const bodyRef = React.useRef(null)
      const dragRef = React.useRef(null)
      const [layout, setLayout] = React.useState(() => ({
        rail: storedWidth('rail', RAIL_SPEC),
        list: storedWidth('list', LIST_SPEC),
      }))
      const [dragging, setDragging] = React.useState(null)
      const [hovered, setHovered] = React.useState(null)
      const [messages, setMessages] = React.useState([])
      const [selectedId, setSelectedId] = React.useState(null)
      const [detail, setDetail] = React.useState(null)
      const [syncing, setSyncing] = React.useState(false)
      const [compose, setCompose] = React.useState(null)

      const openCompose = (mode) => {
        if (detail) setCompose(buildCompose(mode, detail, t))
      }

      const loadMessages = React.useCallback(async () => {
        try {
          // 必须限定文件夹：不带 folder 会把这个账号所有邮件（含已发送）都列出来
          const params = { folder: 'INBOX', limit: 100 }
          if (accountId) params.accountId = accountId
          const value = await call('/messages', params)
          setMessages(value && value.messages ? value.messages : [])
        } catch {
          setMessages([])
        }
      }, [accountId])

      React.useEffect(() => {
        void loadMessages()
      }, [loadMessages])

      const selectMessage = async (id) => {
        setSelectedId(id)
        setDetail(null)
        try {
          setDetail(await call('/message', { id }))
        } catch {
          setDetail(null)
        }
      }

      const syncNow = async () => {
        if (syncing) return
        const targets = accountId ? accounts.filter((account) => account.id === accountId) : accounts
        if (targets.length === 0) return
        setSyncing(true)
        try {
          for (const account of targets) {
            await call('/sync', { accountId: account.id, folder: 'INBOX', limit: 50 })
          }
          await loadMessages()
        } catch {
          // 同步失败保留原列表；错误由顶栏的 Core 状态反映
        } finally {
          setSyncing(false)
        }
      }

      React.useEffect(() => {
        if (dragging === null) {
          storeWidth('rail', layout.rail)
          storeWidth('list', layout.list)
        }
      }, [layout, dragging])

      React.useEffect(() => {
        const total = bodyRef.current ? bodyRef.current.clientWidth : 0
        if (!total) return
        setLayout((prev) => {
          const rail = clampWidth('rail', prev.rail, prev.list, total)
          const list = clampWidth('list', prev.list, rail, total)
          return rail === prev.rail && list === prev.list ? prev : { rail, list }
        })
      }, [])

      const beginDrag = (which) => (event) => {
        if (typeof event.button === 'number' && event.button !== 0) return
        event.preventDefault()
        dragRef.current = {
          which,
          startX: event.clientX,
          startWidth: layout[which],
          otherWidth: which === 'rail' ? layout.list : layout.rail,
          total: bodyRef.current ? bodyRef.current.clientWidth : 0,
        }
        setDragging(which)
        if (typeof event.currentTarget.setPointerCapture === 'function') {
          try {
            event.currentTarget.setPointerCapture(event.pointerId)
          } catch {
            // 捕获失败不影响基本拖动
          }
        }
      }

      const moveDrag = (event) => {
        const session = dragRef.current
        if (!session) return
        const delta = event.clientX - session.startX
        const next = clampWidth(session.which, session.startWidth + delta, session.otherWidth, session.total)
        setLayout((prev) => (prev[session.which] === next ? prev : { ...prev, [session.which]: next }))
      }

      const endDrag = (event) => {
        if (!dragRef.current) return
        dragRef.current = null
        setDragging(null)
        if (typeof event.currentTarget.releasePointerCapture === 'function') {
          try {
            event.currentTarget.releasePointerCapture(event.pointerId)
          } catch {
            // 同上
          }
        }
      }

      const nudge = (which) => (event) => {
        const step = event.key === 'ArrowRight' ? 16 : event.key === 'ArrowLeft' ? -16 : 0
        if (step === 0) return
        event.preventDefault()
        setLayout((prev) => {
          const other = which === 'rail' ? prev.list : prev.rail
          const total = bodyRef.current ? bodyRef.current.clientWidth : 0
          const next = clampWidth(which, prev[which] + step, other, total)
          return next === prev[which] ? prev : { ...prev, [which]: next }
        })
      }

      const handle = (which) => {
        const active = dragging === which || (dragging === null && hovered === which)
        return h(
          'div',
          {
            role: 'separator',
            'aria-orientation': 'vertical',
            'aria-label': t('resize'),
            'aria-valuenow': layout[which],
            tabIndex: 0,
            title: t('resizeHint'),
            style: styles.handle,
            onPointerDown: beginDrag(which),
            onPointerMove: moveDrag,
            onPointerUp: endDrag,
            onPointerCancel: endDrag,
            onPointerEnter: () => setHovered(which),
            onPointerLeave: () => setHovered(null),
            onDoubleClick: () => setLayout((prev) => ({ ...prev, [which]: which === 'rail' ? RAIL_DEFAULT : LIST_DEFAULT })),
            onKeyDown: nudge(which),
          },
          h('div', { style: styles.handleLine(active) }),
        )
      }

      return h(
        'div',
        {
          ref: bodyRef,
          style: { display: 'flex', flex: 1, minHeight: 0, userSelect: dragging ? 'none' : undefined },
        },
        h(
          'div',
          { style: { flex: 'none', width: layout.rail, minWidth: 0, overflow: 'hidden' } },
          h(AccountRail, { t, accounts, accountId, onSelect: onSelectAccount }),
        ),
        handle('rail'),
        h(
          'section',
          { style: { ...styles.list, width: layout.list }, 'aria-label': t('messages') },
          h(MessageList, { t, messages, selectedId, onSelect: selectMessage, onSync: syncNow, syncing }),
        ),
        handle('list'),
        h(MessageDetail, {
          t,
          message: detail,
          accountAddress: (accountId ? accounts.find((account) => account.id === accountId) : accounts[0] || {})
            .address || '',
          actions: detail
            ? [
                { key: 'reply', label: t('reply'), onClick: () => openCompose('reply') },
                { key: 'replyAll', label: t('replyAll'), onClick: () => openCompose('replyAll') },
                { key: 'forward', label: t('forward'), onClick: () => openCompose('forward') },
              ]
            : [],
          agentChat: true,
        }),
        compose
          ? h(ComposeModal, {
              t,
              initial: compose,
              accountId: accounts.length > 0 ? accounts[0].id : undefined,
              onSubmitted: () => {
                setCompose(null)
                if (onRefresh) onRefresh()
              },
              onClose: () => setCompose(null),
            })
          : null,
      )
    }

    // ---- 待跟进 / 草稿箱 / 审批中心 / 知识库 --------------------------------
    /** 待跟进：账户栏 + 待处理/已处理列表 + 右侧卡片工作台（原 AI 工作台）。 */
    function TodoView({ t, status, accountId, onSelectAccount }) {
      const accounts = (status && status.accounts) || []
      const ai = (status && status.ai) || { pending: [], done: [] }
      const [items, setItems] = React.useState({ pending: ai.pending || [], done: ai.done || [] })
      const [selectedId, setSelectedId] = React.useState(null)

      // status 刷新后同步一次（AI 结果目前是内存态，Phase 4 接后端后会改成持久化）
      React.useEffect(() => {
        setItems({ pending: ai.pending || [], done: ai.done || [] })
      }, [status])

      const label = (account) => (account.displayName ? account.displayName : account.address)
      const visible = accountId === null ? accounts : accounts.filter((account) => account.id === accountId)
      const toItem = (item) => ({
        id: item.id,
        title: item.title || item.subject || t('noSubject'),
        meta: item.accountId ? item.accountId : '',
      })

      const groups = [
        ...visible.map((account) => ({
          id: `${account.id}-pending`,
          title: `${label(account)} · ${t('aiQueue')}`,
          items: (items.pending || [])
            .filter((item) => !item.accountId || item.accountId === account.id)
            .map(toItem),
        })),
        { id: 'done', title: t('aiDone'), items: (items.done || []).map(toItem) },
      ]

      const selected = [...(items.pending || []), ...(items.done || [])].find((item) => item.id === selectedId) || null

      const act = (action, item) => {
        setItems((previous) => {
          const pending = (previous.pending || []).filter((entry) => entry.id !== item.id)
          if (action === 'done') return { pending, done: [{ ...item, status: 'done' }, ...(previous.done || [])] }
          if (action === 'delete') return { pending, done: previous.done || [] }
          if (action === 'later') return { pending: [...pending, item], done: previous.done || [] }
          return previous
        })
        if (action !== 'later') setSelectedId(null)
      }

      return h(SplitPane, {
        storageKey: 'todoAccounts',
        spec: RAIL_SPEC,
        label: t('resize'),
        left: h(AccountRail, { t, accounts, accountId, onSelect: onSelectAccount }),
        right: h(SplitPane, {
          storageKey: 'todo',
          spec: PANE_SPEC,
          label: t('resize'),
          left: h(GroupList, { t, groups, selectedId, onSelect: setSelectedId }),
          right: selected
            ? h('div', { style: styles.cardHost }, h(AiCard, { t, item: selected, onAct: act, fluid: true }))
            : h(EmptyPane, { title: t('aiEmptyTitle'), hint: t('aiEmptyHint') }),
        }),
      })
    }

    function DraftsView({ t }) {
      const [selected, setSelected] = React.useState(null)
      const groups = [{ id: 'drafts', title: t('drafts'), items: [] }]
      return h(SplitPane, {
        storageKey: 'drafts',
        spec: PANE_SPEC,
        label: t('resize'),
        left: h(GroupList, { t, groups, selectedId: selected, onSelect: setSelected }),
        right: h(DetailPane, { t, titleKey: 'draftDetailTitle', hintKey: 'draftDetailHint' }),
      })
    }

    /** 通用文件夹视图：左侧邮件标题，右侧正文；文件夹名从服务器解码结果里按模式找。 */
    function MailFolderView({ t, status, storageKey, folderPattern, titleKey, emptyHintKey, composeMode, agentChat, onRefresh, accountId, onSelectAccount }) {
      const accounts = (status && status.accounts) || []
      const account = accounts.length > 0 ? accounts[0] : null
      const [folder, setFolder] = React.useState(null)
      const [messages, setMessages] = React.useState([])
      const [selectedId, setSelectedId] = React.useState(null)
      const [detail, setDetail] = React.useState(null)
      const [note, setNote] = React.useState(null)
      const [busy, setBusy] = React.useState(false)
      const [compose, setCompose] = React.useState(null)

      const accountAddress = account ? account.address : ''

      const folderActions = (message) => {
        if (!message) return []
        if (composeMode === 'send') {
          return [{ key: 'send', label: t('send'), onClick: () => setCompose(buildCompose('send', message, t)) }]
        }
        if (composeMode === 'reply') {
          return [
            { key: 'reply', label: t('reply'), onClick: () => setCompose(buildCompose('reply', message, t)) },
            { key: 'replyAll', label: t('replyAll'), onClick: () => setCompose(buildCompose('replyAll', message, t)) },
            { key: 'forward', label: t('forward'), onClick: () => setCompose(buildCompose('forward', message, t)) },
          ]
        }
        return []
      }

      const load = React.useCallback(
        async (targetFolder) => {
          if (!account || !targetFolder) return
          try {
            const params = { folder: targetFolder, limit: 100 }
            if (accountId) params.accountId = accountId
            const value = await call('/messages', params)
            setMessages((value && value.messages) || [])
          } catch (error) {
            setNote(error.message)
          }
        },
        [account, accountId],
      )

      React.useEffect(() => {
        let cancelled = false
        async function boot() {
          if (!account) {
            setNote(t('folderMissing'))
            return
          }
          try {
            const value = await call('/folders', { accountId: account.id })
            const found = (value.folders || []).find((item) => folderPattern.test(item.name || ''))
            if (cancelled) return
            if (!found) {
              setNote(t('folderMissing'))
              return
            }
            setNote(null)
            setFolder(found.name)
          } catch (error) {
            if (!cancelled) setNote(error.message)
          }
        }
        void boot()
        return () => {
          cancelled = true
        }
      }, [account])

      React.useEffect(() => {
        if (folder) void load(folder)
      }, [folder, load])

      const select = async (id) => {
        setSelectedId(id)
        setDetail(null)
        try {
          setDetail(await call('/message', { id }))
        } catch {
          setDetail(null)
        }
      }

      const syncNow = async () => {
        if (!account || !folder || busy) return
        setBusy(true)
        try {
          await call('/sync', { accountId: account.id, folder, limit: 50 })
          await load(folder)
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(false)
        }
      }

      const items = messages.map((message) => ({
        id: message.id,
        title: message.subject || t('noSubject'),
        meta: `${message.toAddr || ''} · ${message.date || ''}`,
      }))
      const groups = [{ id: storageKey, title: folder || t(titleKey), items }]

      const content = h(SplitPane, {
        storageKey,
        spec: PANE_SPEC,
        label: t('resize'),
        left: h(GroupList, {
          t,
          groups,
          selectedId,
          onSelect: select,
          headerActions: h(
            'button',
            {
              type: 'button',
              style: styles.iconBtn,
              title: t('syncNow'),
              'aria-label': t('syncNow'),
              disabled: busy || !folder,
              onClick: syncNow,
            },
            h(RefreshIcon, {}),
          ),
        }),
        right: note
          ? h('div', { style: styles.detail }, h('p', { style: styles.muted }, note))
          : h(MessageDetail, {
              t,
              message: detail,
              accountAddress,
              actions: folderActions(detail),
              agentChat: agentChat === true,
            }),
      })
      return h(
        'div',
        { style: { display: 'flex', flex: 1, minHeight: 0 } },
        h(SplitPane, {
          storageKey: `${storageKey}-accounts`,
          spec: RAIL_SPEC,
          label: t('resize'),
          left: h(AccountRail, {
            t,
            accounts,
            accountId,
            onSelect: onSelectAccount,
          }),
          right: content,
        }),
        compose
          ? h(ComposeModal, {
              t,
              initial: compose,
              accountId: accountId || (account ? account.id : undefined),
              onSubmitted: () => {
                setCompose(null)
                if (onRefresh) onRefresh()
              },
              onClose: () => setCompose(null),
            })
          : null,
      )
    }

    function SentView({ t, status, onRefresh, accountId, onSelectAccount }) {
      return h(MailFolderView, {
        t,
        status,
        onRefresh,
        accountId,
        onSelectAccount,
        storageKey: 'sent',
        folderPattern: /sent|已发送/i,
        titleKey: 'sent',
        emptyHintKey: 'sentEmptyHint',
        composeMode: 'reply',
      })
    }

    function DraftsMailView({ t, status, onRefresh, accountId, onSelectAccount }) {
      return h(MailFolderView, {
        t,
        status,
        onRefresh,
        accountId,
        onSelectAccount,
        storageKey: 'draftsMail',
        folderPattern: /draft|草稿/i,
        titleKey: 'drafts',
        emptyHintKey: 'sentEmptyHint',
        composeMode: 'send',
      })
    }

    function DeletedView({ t, status, onRefresh, accountId, onSelectAccount }) {
      return h(MailFolderView, {
        t,
        status,
        onRefresh,
        accountId,
        onSelectAccount,
        storageKey: 'deleted',
        folderPattern: /deleted|trash|已删除|废纸/i,
        titleKey: 'deleted',
        emptyHintKey: 'sentEmptyHint',
      })
    }

    function JunkView({ t, status, onRefresh, accountId, onSelectAccount }) {
      return h(MailFolderView, {
        t,
        status,
        onRefresh,
        accountId,
        onSelectAccount,
        storageKey: 'junk',
        folderPattern: /junk|spam|垃圾/i,
        titleKey: 'junk',
        emptyHintKey: 'sentEmptyHint',
      })
    }

    /** 模板：纯本地保存，不和邮箱同步。 */
    function TemplatesView({ t, status, onRefresh, accountId, onSelectAccount }) {
      const accounts = (status && status.accounts) || []
      const senderAccountId =
        accountId || (accounts.length > 0 ? accounts[0].id : undefined)
      const [templates, setTemplates] = React.useState([])
      const [selectedId, setSelectedId] = React.useState(null)
      const [draft, setDraft] = React.useState({ name: '', subject: '', body: '' })
      const [note, setNote] = React.useState(null)
      const [busy, setBusy] = React.useState(false)
      const [compose, setCompose] = React.useState(null)

      const load = React.useCallback(async () => {
        try {
          const value = await call('/templates', {})
          setTemplates((value && value.templates) || [])
        } catch (error) {
          setNote(error.message)
        }
      }, [])

      React.useEffect(() => {
        void load()
      }, [load])

      const select = (id) => {
        const found = templates.find((item) => item.id === id)
        setSelectedId(id)
        setDraft(found ? { name: found.name, subject: found.subject, body: found.body } : { name: '', subject: '', body: '' })
        setNote(null)
      }

      const startNew = () => {
        setSelectedId(null)
        setDraft({ name: '', subject: '', body: '' })
        setNote(null)
      }

      const save = async (event) => {
        event.preventDefault()
        if (busy) return
        setBusy(true)
        try {
          await call('/templates/add', draft)
          await load()
          setNote(t('templateSaved'))
          setDraft({ name: '', subject: '', body: '' })
          setSelectedId(null)
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(false)
        }
      }

      const remove = async () => {
        if (!selectedId || busy) return
        setBusy(true)
        try {
          await call('/templates/remove', { id: selectedId })
          setSelectedId(null)
          setDraft({ name: '', subject: '', body: '' })
          await load()
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(false)
        }
      }

      const groups = [
        {
          id: 'templates',
          title: t('templates'),
          items: templates.map((item) => ({ id: item.id, title: item.name, meta: item.subject || '' })),
        },
      ]

      const content = h(SplitPane, {
        storageKey: 'templates',
        spec: PANE_SPEC,
        label: t('resize'),
        left: h(GroupList, {
          t,
          groups,
          selectedId,
          onSelect: select,
          headerActions: h(
            'button',
            { type: 'button', style: styles.iconBtn, title: t('templateNew'), onClick: startNew },
            '＋',
          ),
        }),
        right: h(
          'form',
          { style: styles.detail, onSubmit: save },
          h('p', { style: styles.sectionTitle }, selectedId ? draft.name || t('templates') : t('templateNew')),
          h(
            'label',
            { style: styles.formRow },
            h('span', { style: styles.label }, t('templateName')),
            h('input', {
              style: styles.textInput,
              value: draft.name,
              onChange: (event) => setDraft((prev) => ({ ...prev, name: event.target.value })),
            }),
          ),
          h(
            'label',
            { style: styles.formRow },
            h('span', { style: styles.label }, t('templateSubject')),
            h('input', {
              style: styles.textInput,
              value: draft.subject,
              onChange: (event) => setDraft((prev) => ({ ...prev, subject: event.target.value })),
            }),
          ),
          h(
            'label',
            { style: styles.formRow },
            h('span', { style: styles.label }, t('templateBody')),
            h('textarea', {
              style: styles.textArea,
              value: draft.body,
              onChange: (event) => setDraft((prev) => ({ ...prev, body: event.target.value })),
            }),
          ),
          h(
            'div',
            { style: styles.formActions },
            h('button', { type: 'submit', style: styles.btn, disabled: busy }, t('templateSave')),
            h(
              'button',
              {
                type: 'button',
                style: styles.btnSmall,
                onClick: () =>
                  setCompose({ mode: 'send', to: '', cc: '', subject: draft.subject || '', body: draft.body || '' }),
              },
              t('send'),
            ),
            selectedId ? h('button', { type: 'button', style: styles.dangerBtn, onClick: remove, disabled: busy }, t('templateDelete')) : null,
            note ? h('span', { style: styles.muted }, note) : null,
          ),
          templates.length === 0 ? h('p', { style: styles.muted }, t('templatesEmpty')) : null,
        ),
      })
      const composeModal = compose
        ? h(ComposeModal, {
            t,
            initial: compose,
            accountId: senderAccountId,
            onSubmitted: () => {
              setCompose(null)
              if (onRefresh) onRefresh()
            },
            onClose: () => setCompose(null),
          })
        : null
      return h(
        'div',
        { style: { display: 'flex', flex: 1, minHeight: 0 } },
        h(SplitPane, {
          storageKey: 'templates-accounts',
          spec: RAIL_SPEC,
          label: t('resize'),
          left: h(AccountRail, { t, accounts, accountId, onSelect: onSelectAccount }),
          right: content,
        }),
        composeModal,
      )
    }

    /** 通讯录：左列表 / 右详情 + Agent 提示词生成。 */
    function ContactsView({ t, status, accountId, onSelectAccount }) {
      const accounts = (status && status.accounts) || []
      const blank = { name: '', email: '', phone: '', company: '', title: '', notes: '' }
      const [contacts, setContacts] = React.useState([])
      const [selectedId, setSelectedId] = React.useState(null)
      const [draft, setDraft] = React.useState(blank)
      const [note, setNote] = React.useState(null)
      const [prompt, setPrompt] = React.useState('')
      const [copied, setCopied] = React.useState(false)
      const [busy, setBusy] = React.useState(false)

      const load = React.useCallback(async () => {
        try {
          const value = await call('/contacts', {})
          setContacts((value && value.contacts) || [])
        } catch (error) {
          setNote(error.message)
        }
      }, [])

      React.useEffect(() => {
        void load()
      }, [load])

      const select = (id) => {
        const found = contacts.find((item) => item.id === id)
        setSelectedId(id)
        setDraft(
          found
            ? {
                name: found.name || '',
                email: found.email || '',
                phone: found.phone || '',
                company: found.company || '',
                title: found.title || '',
                notes: found.notes || '',
              }
            : blank,
        )
        setPrompt('')
        setNote(null)
      }

      const field = (labelKey, key, extra) =>
        h(
          'label',
          { style: styles.formRow },
          h('span', { style: styles.label }, t(labelKey)),
          h('input', {
            style: styles.textInput,
            value: draft[key],
            onChange: (event) => setDraft((prev) => ({ ...prev, [key]: event.target.value })),
            ...extra,
          }),
        )

      const save = async (event) => {
        event.preventDefault()
        if (busy) return
        setBusy(true)
        try {
          await call('/contacts/add', draft)
          setDraft(blank)
          setSelectedId(null)
          setNote(t('contactSaved'))
          await load()
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(false)
        }
      }

      const remove = async () => {
        if (!selectedId || busy) return
        setBusy(true)
        try {
          await call('/contacts/remove', { id: selectedId })
          setSelectedId(null)
          setDraft(blank)
          await load()
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(false)
        }
      }

      const composePrompt = () => {
        const who = [draft.name, draft.company, draft.title].filter(Boolean).join(' / ')
        const lines = [`联系人：${who || draft.email || '(未命名)'}`]
        if (draft.email) lines.push(`邮箱：${draft.email}`)
        if (draft.phone) lines.push(`电话：${draft.phone}`)
        if (draft.notes) lines.push(`备注：${draft.notes}`)
        lines.push('', prompt.trim() || '请根据以上联系人信息帮我处理相关邮件。')
        return lines.join('\n')
      }

      const copyPrompt = async () => {
        const text = composePrompt()
        try {
          if (typeof navigator !== 'undefined' && navigator.clipboard) {
            await navigator.clipboard.writeText(text)
          }
          setCopied(true)
        } catch {
          setNote(text)
        }
      }

      const groups = [
        {
          id: 'contacts',
          title: t('contacts'),
          items: contacts.map((item) => ({
            id: item.id,
            title: item.name || item.email || '(未命名)',
            meta: [item.company, item.email].filter(Boolean).join(' · '),
          })),
        },
      ]

      const content = h(SplitPane, {
        storageKey: 'contacts',
        spec: PANE_SPEC,
        label: t('resize'),
        left: h(GroupList, {
          t,
          groups,
          selectedId,
          onSelect: select,
          headerActions: h(
            'button',
            {
              type: 'button',
              style: styles.iconBtn,
              title: t('contactNew'),
              onClick: () => {
                setSelectedId(null)
                setDraft(blank)
                setPrompt('')
                setNote(null)
              },
            },
            '＋',
          ),
        }),
        right: h(
          'form',
          { style: styles.detail, onSubmit: save },
          h('p', { style: styles.sectionTitle }, selectedId ? draft.name || t('contacts') : t('contactNew')),
          h('div', { style: styles.formGrid }, field('contactName', 'name'), field('contactEmail', 'email', { type: 'email' })),
          h('div', { style: styles.formGrid }, field('contactPhone', 'phone'), field('contactCompany', 'company')),
          h('div', { style: styles.formGrid }, field('contactTitle', 'title')),
          h(
            'label',
            { style: styles.formRow },
            h('span', { style: styles.label }, t('contactNotes')),
            h('textarea', {
              style: styles.textArea,
              value: draft.notes,
              onChange: (event) => setDraft((prev) => ({ ...prev, notes: event.target.value })),
            }),
          ),
          h(
            'div',
            { style: styles.formActions },
            h('button', { type: 'submit', style: styles.btn, disabled: busy }, t('contactSave')),
            selectedId ? h('button', { type: 'button', style: styles.dangerBtn, onClick: remove, disabled: busy }, t('contactDelete')) : null,
            note ? h('span', { style: styles.muted }, note) : null,
          ),
          contacts.length === 0 ? h('p', { style: styles.muted }, t('contactsEmpty')) : null,
          h(
            'div',
            { style: styles.chatBox },
            h('p', { style: styles.sectionTitle }, t('contactChatTitle')),
            h('textarea', {
              style: styles.textArea,
              value: prompt,
              placeholder: t('contactChatPlaceholder'),
              onChange: (event) => {
                setPrompt(event.target.value)
                setCopied(false)
              },
            }),
            h(
              'div',
              { style: styles.formActions },
              h('button', { type: 'button', style: styles.btn, onClick: copyPrompt }, t('contactChatCopy')),
              copied ? h('span', { style: styles.muted }, t('contactChatCopied')) : null,
            ),
            h('p', { style: styles.muted }, t('contactChatHint')),
          ),
        ),
      })
      return h(
        'div',
        { style: { display: 'flex', flex: 1, minHeight: 0 } },
        h(SplitPane, {
          storageKey: 'contacts-accounts',
          spec: RAIL_SPEC,
          label: t('resize'),
          left: h(AccountRail, { t, accounts, accountId, onSelect: onSelectAccount }),
          right: content,
        }),
      )
    }

    /** 搜索结果：查询非空时接管主区域。 */
    function SearchView({ t, query, status }) {
      const accounts = (status && status.accounts) || []
      const accountAddress = accounts.length > 0 ? accounts[0].address : ''
      const [messages, setMessages] = React.useState([])
      const [selectedId, setSelectedId] = React.useState(null)
      const [detail, setDetail] = React.useState(null)
      const [total, setTotal] = React.useState(0)
      const [note, setNote] = React.useState(null)

      React.useEffect(() => {
        let cancelled = false
        async function search() {
          try {
            const value = await call('/search', { query, limit: 100 })
            if (cancelled) return
            setMessages((value && value.messages) || [])
            setTotal((value && value.total) || 0)
            setNote(null)
          } catch (error) {
            if (!cancelled) setNote(error.message)
          }
        }
        void search()
        return () => {
          cancelled = true
        }
      }, [query])

      const select = async (id) => {
        setSelectedId(id)
        setDetail(null)
        try {
          setDetail(await call('/message', { id }))
        } catch {
          setDetail(null)
        }
      }

      const groups = [
        {
          id: 'search',
          title: `${t('searchResults')} · ${total}`,
          items: messages.map((message) => ({
            id: message.id,
            title: message.subject || t('noSubject'),
            meta: `${message.fromAddr || ''} · ${message.date || ''}`,
          })),
        },
      ]

      return h(SplitPane, {
        storageKey: 'search',
        spec: PANE_SPEC,
        label: t('resize'),
        left: h(GroupList, { t, groups, selectedId, onSelect: select }),
        right:
          note !== null
            ? h('div', { style: styles.detail }, h('p', { style: styles.muted }, note))
            : messages.length === 0
              ? h('div', { style: styles.detail }, h('p', { style: styles.muted }, t('searchEmpty')))
              : h(MessageDetail, { t, message: detail, accountAddress }),
      })
    }

    /**
     * 全部页签共用的最右侧审批栏：
     * 默认只列待审批；点历史图标切到审批历史；历史里有返回按钮。
     */
    function ApprovalsRail({ t, status, initialMode = 'pending', onRefresh }) {
      const RAIL_SPEC_APPROVALS = { initial: 280, min: 200, max: 520 }
      const [width, setWidth] = React.useState(() => storedWidth('approvalsRail', RAIL_SPEC_APPROVALS))
      const [dragging, setDragging] = React.useState(false)
      const [hovered, setHovered] = React.useState(false)
      const dragRef = React.useRef(null)
      const [mode, setMode] = React.useState(initialMode)
      const [busy, setBusy] = React.useState(null)
      const [note, setNote] = React.useState(null)
      const pending = (status && status.approvals && status.approvals.pending) || []
      const history = (status && status.approvals && status.approvals.history) || []
      const count = (status && status.counters && status.counters.pendingApprovals) || pending.length
      const showingHistory = mode === 'history'
      const items = showingHistory ? history : pending

      React.useEffect(() => {
        storeWidth('approvalsRail', width)
      }, [width])

      const handleDown = (event) => {
        if (typeof event.button === 'number' && event.button !== 0) return
        event.preventDefault()
        dragRef.current = { startX: event.clientX, startWidth: width }
        setDragging(true)
        if (typeof event.currentTarget.setPointerCapture === 'function') {
          try {
            event.currentTarget.setPointerCapture(event.pointerId)
          } catch {
            // 捕获失败不影响基本拖动
          }
        }
      }

      const handleMove = (event) => {
        const session = dragRef.current
        if (!session) return
        // 往左拖变宽
        const next = Math.round(
          clamp(session.startWidth + (session.startX - event.clientX), RAIL_SPEC_APPROVALS.min, RAIL_SPEC_APPROVALS.max),
        )
        setWidth((prev) => (prev === next ? prev : next))
      }

      const handleUp = (event) => {
        if (!dragRef.current) return
        dragRef.current = null
        setDragging(false)
        if (typeof event.currentTarget.releasePointerCapture === 'function') {
          try {
            event.currentTarget.releasePointerCapture(event.pointerId)
          } catch {
            // 同上
          }
        }
      }

      const reject = async (approval) => {
        if (busy) return
        setBusy(approval.id)
        setNote(null)
        try {
          await call('/approvals/reject', { id: approval.id })
          if (onRefresh) onRefresh()
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(null)
        }
      }

      // 发送改为 Agent 发起 + 宿主审批框；这里只提供把指令复制到对话的入口
      const copySendCommand = async (approval) => {
        const text = `请发送审批编号 ${approval.id} 的邮件（主题：${approval.subject || '(无主题)'}）`
        try {
          if (typeof navigator !== 'undefined' && navigator.clipboard) await navigator.clipboard.writeText(text)
          setNote(t('sendCommandCopied'))
        } catch {
          setNote(text)
        }
      }

      const body =
        items.length === 0
          ? h(
              'div',
              null,
              h('p', { style: styles.railEmpty }, showingHistory ? t('approvalHistoryEmpty') : t('emptyApprovals')),
              h(
                'p',
                { style: { ...styles.railEmpty, paddingTop: 0 } },
                showingHistory ? t('approvalHistoryHint') : t('emptyApprovalsHint'),
              ),
              note ? h('p', { style: styles.railEmpty }, note) : null,
            )
          : h(
              'div',
              null,
              items.map((item) =>
                h(
                  'div',
                  { key: item.id, style: { ...styles.listItem, cursor: 'default' } },
                  h('span', null, item.subject || item.draftId || item.id),
                  h(
                    'span',
                    { style: styles.listItemMeta },
                    (() => {
                      const via =
                        item.decidedVia === 'prompt'
                          ? t('decidedByUser')
                          : item.decidedVia === 'policy-never'
                            ? t('decidedByPolicy')
                            : item.decidedVia === 'api'
                              ? t('decidedByAgent')
                              : ''
                      return showingHistory
                        ? [item.status || '', via, item.to || ''].filter(Boolean).join(' · ')
                        : `${item.to || ''} · ${item.expiresAt || ''}`
                    })(),
                  ),
                  showingHistory
                    ? null
                    : h(
                        'div',
                        { style: { display: 'flex', gap: 6, marginTop: 6 } },
                        h(
                          'button',
                          {
                            type: 'button',
                            style: styles.btnSmall,
                            disabled: busy === item.id,
                            onClick: () => copySendCommand(item),
                          },
                          t('copySendCommand'),
                        ),
                        h(
                          'button',
                          {
                            type: 'button',
                            style: styles.dangerBtn,
                            disabled: busy === item.id,
                            onClick: () => reject(item),
                          },
                          t('rejectApproval'),
                        ),
                      ),
                ),
              ),
              showingHistory ? null : h('p', { style: styles.railEmpty }, t('approvalSendHint')),
              note ? h('p', { style: styles.railEmpty }, note) : null,
            )

      return h(
        'div',
        { style: { display: 'flex', flex: 'none', alignItems: 'stretch' } },
        h(
          'div',
          {
            role: 'separator',
            'aria-orientation': 'vertical',
            'aria-label': t('resize'),
            title: t('resizeHint'),
            style: styles.handle,
            onPointerDown: handleDown,
            onPointerMove: handleMove,
            onPointerUp: handleUp,
            onPointerCancel: handleUp,
            onPointerEnter: () => setHovered(true),
            onPointerLeave: () => setHovered(false),
            onDoubleClick: () => setWidth(RAIL_SPEC_APPROVALS.initial),
          },
          h('div', { style: styles.handleLine(dragging || hovered) }),
        ),
        h(
          'aside',
          {
            style: { ...styles.railPanel, width },
            'aria-label': showingHistory ? t('approvalHistory') : t('approvals'),
          },
          h(
            'div',
            { style: styles.railHeader },
            h('span', { style: styles.railHeaderTitle }, showingHistory ? t('approvalHistory') : t('approvals')),
            !showingHistory && count > 0 ? h('span', { style: styles.railCount }, String(count)) : null,
            h('span', { style: styles.spacer }),
          showingHistory
            ? h(
                'button',
                { type: 'button', style: styles.iconBtn, title: t('back'), 'aria-label': t('back'), onClick: () => setMode('pending') },
                h(BackIcon, {}),
              )
            : h(
                'button',
                {
                  type: 'button',
                  style: styles.iconBtn,
                  title: t('approvalHistory'),
                  'aria-label': t('approvalHistory'),
                  onClick: () => setMode('history'),
                },
                h(ClockIcon, {}),
              ),
        ),
          h('div', { style: styles.railBody }, body),
        ),
      )
    }

    function KnowledgeView({ t, status, accountId, onSelectAccount }) {
      const accounts = (status && status.accounts) || []
      const [selected, setSelected] = React.useState('contacts')
      const groups = [
        { id: 'contacts', title: t('kbContacts'), items: [] },
        { id: 'companies', title: t('kbCompanies'), items: [] },
        { id: 'projects', title: t('kbProjects'), items: [] },
        { id: 'files', title: t('kbFiles'), items: [] },
      ]
      const content = h(SplitPane, {
        storageKey: 'knowledge',
        spec: PANE_SPEC,
        label: t('resize'),
        left: h(GroupList, { t, groups, selectedId: selected, onSelect: setSelected }),
        right: h(DetailPane, { t, titleKey: 'kbDetailTitle', hintKey: 'kbDetailHint' }),
      })
      return h(
        'div',
        { style: { display: 'flex', flex: 1, minHeight: 0 } },
        h(SplitPane, {
          storageKey: 'knowledge-accounts',
          spec: RAIL_SPEC,
          label: t('resize'),
          left: h(AccountRail, { t, accounts, accountId, onSelect: onSelectAccount }),
          right: content,
        }),
      )
    }

    // ---- AI 工作台 ---------------------------------------------------------
    /** 单张卡片：上半待处理内容，下半与 Agent 的对话栏。 */
    function AiCard({ t, item, onAct, fluid }) {
      return h(
        'article',
        { style: fluid ? { ...styles.aiCard, width: '100%', height: '100%' } : styles.aiCard, 'aria-label': item.title },
        h(
          'div',
          { style: styles.aiCardHead },
          h('span', { style: styles.aiCardTitle }, item.title),
          h('span', { style: styles.spacer }),
          h('button', { type: 'button', style: styles.btnSmall, onClick: () => onAct && onAct('done', item) }, t('actDone')),
          h('button', { type: 'button', style: styles.btnSmall, onClick: () => onAct && onAct('later', item) }, t('actLater')),
          h('button', { type: 'button', style: styles.btnSmall, onClick: () => onAct && onAct('delete', item) }, t('actDelete')),
        ),
        h(
          'div',
          { style: styles.aiCardTop },
          h('p', { style: styles.sectionTitle }, t('cardPendingContent')),
          h('p', { style: styles.muted }, item.summary || t('emptyList')),
        ),
        h(
          'div',
          { style: styles.aiCardBottom },
          h(
            'div',
            { style: styles.chatList },
            h('p', { style: styles.sectionTitle }, t('cardChat')),
            h('p', { style: styles.muted }, t('cardChatEmpty')),
          ),
          h(
            'div',
            { style: styles.chatInputRow },
            h('input', {
              style: styles.input,
              placeholder: t('cardChatPlaceholder'),
              'aria-label': t('cardChat'),
              disabled: true,
            }),
          ),
        ),
      )
    }

    // ---- 设置 --------------------------------------------------------------
    function AccountRow({ t, account, onChanged }) {
      const [busy, setBusy] = React.useState(null)
      const [note, setNote] = React.useState(null)

      const test = async () => {
        setBusy('test')
        setNote(null)
        try {
          const value = await call('/accounts/test', { id: account.id })
          setNote(`${t('accountTestOk')} ${(value.folders || []).length}`)
        } catch (error) {
          setNote(`${t('accountTestFailed')}: ${error.message}`)
        } finally {
          setBusy(null)
        }
      }

      const remove = async () => {
        setBusy('remove')
        setNote(null)
        try {
          await call('/accounts/remove', { id: account.id })
          if (onChanged) onChanged()
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(null)
        }
      }

      return h(
        'div',
        { style: styles.accountRow },
        h(
          'div',
          { style: { flex: 1, minWidth: 0 } },
          h('div', { style: { fontSize: 13 } }, account.displayName ? `${account.displayName} · ${account.address}` : account.address),
          h(
            'div',
            { style: { ...styles.muted, margin: 0 } },
            `${account.imapHost}:${account.imapPort} ${account.imapSecurity}${
              account.hasCredential ? '' : ` · ${t('accountNoCredential')}`
            }`,
          ),
          note ? h('div', { style: { ...styles.muted, margin: 0 } }, note) : null,
        ),
        h(
          'button',
          { type: 'button', style: styles.btnSmall, disabled: busy !== null, onClick: test },
          busy === 'test' ? t('accountTesting') : t('accountTest'),
        ),
        h(
          'button',
          { type: 'button', style: styles.dangerBtn, disabled: busy !== null, onClick: remove },
          t('accountRemove'),
        ),
      )
    }

    // 端口按「协议 × 加密方式」推断，与 Core / Host 保持一致
    const PORT_DEFAULTS = {
      imap: { ssl: '993', starttls: '143', none: '143' },
      pop3: { ssl: '995', starttls: '110', none: '110' },
      smtp: { ssl: '465', starttls: '587', none: '25' },
    }

    function portTable(channel) {
      return PORT_DEFAULTS[channel] || PORT_DEFAULTS.imap
    }

    function defaultPort(channel, security) {
      const table = portTable(channel)
      return table[security] || table.ssl
    }

    /** 端口仍停留在某个默认值时，才跟着协议/加密方式自动改，避免覆盖用户手填的端口。 */
    function isDefaultPort(channel, value) {
      return Object.values(portTable(channel)).includes(String(value ?? '').trim())
    }

    function AccountForm({ t, onSaved }) {
      const blank = {
        address: '',
        displayName: '',
        protocol: 'imap',
        imapHost: '',
        imapPort: defaultPort('imap', 'ssl'),
        imapSecurity: 'ssl',
        username: '',
        password: '',
        smtpHost: '',
        smtpPort: defaultPort('smtp', 'ssl'),
        smtpSecurity: 'ssl',
      }
      const [draft, setDraft] = React.useState(blank)
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = React.useState(null)
      const [saved, setSaved] = React.useState(false)
      // 用户名默认跟随邮箱地址；用户自己改过之后就不再覆盖
      const usernameTouched = React.useRef(false)

      const update = (key) => (event) => {
        const value = event.target.value
        if (key === 'username') usernameTouched.current = value.trim().length > 0
        setDraft((prev) => {
          const next = { ...prev, [key]: value }
          if (key === 'address' && !usernameTouched.current) {
            next.username = value
          }
          if (key === 'protocol' && isDefaultPort(prev.protocol, prev.imapPort)) {
            next.imapPort = defaultPort(value, prev.imapSecurity)
          }
          if (key === 'imapSecurity' && isDefaultPort(prev.protocol, prev.imapPort)) {
            next.imapPort = defaultPort(prev.protocol, value)
          }
          if (key === 'smtpSecurity' && isDefaultPort('smtp', prev.smtpPort)) {
            next.smtpPort = defaultPort('smtp', value)
          }
          return next
        })
      }

      const submit = async (event) => {
        event.preventDefault()
        setBusy(true)
        setError(null)
        setSaved(false)
        try {
          await call('/accounts/add', {
            ...draft,
            username: draft.username.trim() || draft.address.trim(),
            imapPort: Number(draft.imapPort),
            smtpPort: Number(draft.smtpPort),
          })
          setDraft(blank)
          usernameTouched.current = false
          setSaved(true)
          if (onSaved) onSaved()
        } catch (e) {
          setError(e.message)
        } finally {
          setBusy(false)
        }
      }

      const field = (labelKey, key, extra) =>
        h(
          'label',
          { style: styles.field },
          h('span', { style: styles.label }, t(labelKey)),
          h('input', { style: styles.textInput, value: draft[key], onChange: update(key), ...extra }),
        )

      const selectField = (labelKey, key, options) =>
        h(
          'label',
          { style: styles.field },
          h('span', { style: styles.label }, t(labelKey)),
          h(
            'select',
            { className: 'dsh-mail-select', style: styles.textInput, value: draft[key], onChange: update(key) },
            options.map(([value, labelKey2]) => h('option', { key: value, value }, t(labelKey2))),
          ),
        )

      const SECURITY_OPTIONS = [
        ['ssl', 'securitySsl'],
        ['starttls', 'securityStarttls'],
        ['none', 'securityNone'],
      ]

      return h(
        'form',
        { style: { marginTop: 12, display: 'flex', flexDirection: 'column', gap: 10 }, onSubmit: submit },
        h('p', { style: { ...styles.sectionTitle, margin: 0 } }, t('accountAdd')),
        h(
          'div',
          { style: styles.formGrid },
          field('accountAddress', 'address', { type: 'email', required: true, autoComplete: 'off' }),
          field('accountDisplayName', 'displayName', { autoComplete: 'off' }),
          selectField('protocolLabel', 'protocol', [
            ['imap', 'protocolImap'],
            ['pop3', 'protocolPop3'],
          ]),
          field(draft.protocol === 'pop3' ? 'accountHostPop3' : 'accountHostImap', 'imapHost', {
            required: true,
            placeholder: draft.protocol === 'pop3' ? 'pop3.example.com' : 'imap.example.com',
          }),
          field('accountImapPort', 'imapPort', { type: 'number', min: 1, max: 65535 }),
          selectField('accountSecurity', 'imapSecurity', SECURITY_OPTIONS),
          field('accountUsername', 'username', { autoComplete: 'off', placeholder: 'me@example.com' }),
          field('accountPassword', 'password', { type: 'password', required: true, autoComplete: 'new-password' }),
        ),
        h('p', { style: { ...styles.muted, margin: 0 } }, t('usernameHint')),
        h('p', { style: { ...styles.muted, margin: 0 } }, t('passwordHint')),
        h('p', { style: styles.formSubhead }, t('smtpSection')),
        h(
          'div',
          { style: styles.formGrid },
          field('smtpHost', 'smtpHost', { placeholder: 'smtp.example.com', autoComplete: 'off' }),
          field('smtpPort', 'smtpPort', { type: 'number', min: 1, max: 65535 }),
          selectField('smtpSecurity', 'smtpSecurity', SECURITY_OPTIONS),
        ),
        h('p', { style: { ...styles.muted, margin: 0 } }, t('smtpOptionalHint')),
        error !== null ? h('p', { style: styles.error }, `${t('accountFailed')}: ${error}`) : null,
        saved ? h('p', { style: { ...styles.muted, margin: 0 } }, t('accountSaved')) : null,
        h(
          'button',
          { type: 'submit', style: { ...styles.btn, alignSelf: 'flex-start' }, disabled: busy },
          busy ? t('accountSubmitting') : t('accountSubmit'),
        ),
      )
    }

    /** 自动同步间隔：整数分钟，0 = 不自动同步；写回 profile 配置。 */
    function SyncIntervalField({ t, status, onRefresh }) {
      const stored = status && typeof status.syncIntervalMs === 'number' ? status.syncIntervalMs : 300000
      const current = Math.round(stored / 60000)
      const [minutes, setMinutes] = React.useState(String(current))
      const [note, setNote] = React.useState(null)
      const [busy, setBusy] = React.useState(false)

      React.useEffect(() => {
        setMinutes(String(Math.round(stored / 60000)))
      }, [stored])

      const save = async (event) => {
        event.preventDefault()
        const value = Number(minutes)
        if (!Number.isInteger(value) || value < 0) {
          setNote(t('syncIntervalInvalid'))
          return
        }
        setBusy(true)
        setNote(null)
        try {
          await call('/config/sync-interval', { minutes: value })
          setNote(t('syncIntervalSaved'))
          if (onRefresh) onRefresh()
        } catch (error) {
          setNote(error.message)
        } finally {
          setBusy(false)
        }
      }

      return h(
        'form',
        { onSubmit: save },
        h(
          'div',
          { style: styles.row },
          h('span', { style: styles.rowKey }, t('settingsSyncInterval')),
          h(
            'span',
            { style: { display: 'flex', alignItems: 'center', gap: 6 } },
            h('input', {
              type: 'number',
              min: 0,
              step: 1,
              value: minutes,
              onChange: (event) => setMinutes(event.target.value),
              style: { ...styles.textInput, width: 88, padding: '4px 8px' },
            }),
            h('span', { style: styles.rowKey }, t('minute')),
            h('button', { type: 'submit', style: styles.btnSmall, disabled: busy }, t('save')),
          ),
        ),
        h('p', { style: { ...styles.muted, margin: 0 } }, t('settingsSyncIntervalHint')),
        note ? h('p', { style: { ...styles.muted, margin: 0 } }, note) : null,
      )
    }

    function SettingsView({ t, status, onRefresh }) {
      const core = (status && status.core) || {}
      const accounts = (status && status.accounts) || []
      return h(
        'div',
        { style: styles.reading },
        h(
          'div',
          { style: { ...styles.card, ...styles.settingsBox } },
          h('p', { style: styles.sectionTitle }, t('settings')),
          h(Row, { label: t('coreState'), value: core.state || '—' }),
          h(Row, { label: t('coreVersion'), value: core.version || '—' }),
          h(Row, { label: t('coreDb'), value: core.dbPath || '—' }),
          h(Row, { label: t('coreDataDir'), value: core.dataDir || '—' }),
          h(SyncIntervalField, { t, status, onRefresh }),
          h(Row, { label: t('settingsCreds'), value: t('settingsCredsValue') }),
          core.error
            ? h('p', { style: { ...styles.error, marginTop: 8 } }, `${t('coreError')}: ${core.error.code} · ${core.error.message}`)
            : null,
        ),
        h(
          'div',
          { style: { ...styles.card, ...styles.settingsBox } },
          h('p', { style: styles.sectionTitle }, t('settingsAccounts')),
          accounts.length === 0 ? h('p', { style: { ...styles.muted, margin: 0 } }, t('accountsEmpty')) : null,
          accounts.map((account) => h(AccountRow, { key: account.id, t, account, onChanged: onRefresh })),
        ),
        h('div', { style: { ...styles.card, ...styles.settingsBox } }, h(AccountForm, { t, onSaved: onRefresh })),
      )
    }

    // ---- 顶栏 --------------------------------------------------------------
    function TopBar({ t, status, onSync, syncing, query, onQueryChange, onCompose }) {
      const core = (status && status.core) || null
      const ok = Boolean(core && core.state === 'ready')
      const pending = (status && status.counters && status.counters.pendingAi) || 0
      const label = core
        ? ok
          ? `Mail Core v${core.version || '?'}`
          : `Mail Core ${core.state}`
        : t('coreState')
      const title = core && core.error ? `${core.error.code}: ${core.error.message}` : core ? core.dbPath : ''
      return h(
        'header',
        { style: styles.topbar },
        h('h1', { style: styles.title }, t('title')),
        h(
          'button',
          { type: 'button', style: styles.iconBtn, title: t('composeNew'), 'aria-label': t('composeNew'), onClick: onCompose },
          '＋',
        ),
        h('span', { style: styles.spacer }),
        h(
          'div',
          { style: styles.searchWrap },
          h(
            'svg',
            {
              width: 13,
              height: 13,
              viewBox: '0 0 16 16',
              fill: 'none',
              stroke: 'currentColor',
              strokeWidth: 1.4,
              strokeLinecap: 'round',
              'aria-hidden': true,
            },
            h('circle', { cx: 7, cy: 7, r: 4.2 }),
            h('path', { d: 'M10.2 10.2L13.5 13.5' }),
          ),
          h('input', {
            style: styles.searchInput,
            value: query,
            placeholder: t('searchPlaceholder'),
            'aria-label': t('searchPlaceholder'),
            onChange: (event) => onQueryChange(event.target.value),
          }),
        ),
        h('span', { style: styles.badge(pending > 0), title: t('pendingAi') }, `${t('pendingAi')} ${pending}`),
        h('span', { style: styles.pill(ok), title }, h('span', { style: styles.dot(ok) }), label),
        h(
          'button',
          { type: 'button', style: styles.iconBtn, onClick: onSync, disabled: syncing, title: t('refreshStatus'), 'aria-label': t('refreshStatus') },
          h(RefreshIcon, {}),
        ),
      )
    }

    // ---- 面板 --------------------------------------------------------------
    function MailPanel({ t }) {
      const [tab, setTab] = React.useState('inbox')
      const [status, setStatus] = React.useState(null)
      const [error, setError] = React.useState(null)
      const [query, setQuery] = React.useState('')
      const [accountId, setAccountId] = React.useState(null)
      const [newCompose, setNewCompose] = React.useState(null)

      const load = React.useCallback(
        (signal) =>
          call('/status', {}, signal)
            .then((value) => {
              setStatus(value)
              setError(null)
            })
            .catch((e) => {
              if (!signal || !signal.aborted) setError(e.message)
            }),
        [],
      )

      React.useEffect(() => {
        const ac = new AbortController()
        load(ac.signal)
        return () => ac.abort()
      }, [load])

      const refresh = () => load(new AbortController().signal)

      const [syncingAll, setSyncingAll] = React.useState(false)
      const syncAll = async () => {
        if (syncingAll) return
        setSyncingAll(true)
        try {
          await call('/sync/all', {})
        } catch {
          // 同步失败也要刷新状态，让顶栏反映 Core 状态
        } finally {
          setSyncingAll(false)
          refresh()
        }
      }

      const activeAccountId =
        accountId || (status && status.accounts && status.accounts.length > 0 ? status.accounts[0].id : undefined)

      let content
      if (query.trim().length > 0) content = h(SearchView, { t, query: query.trim(), status })
      else if (tab === 'inbox')
        content = h(InboxView, { t, status, onRefresh: refresh, accountId, onSelectAccount: setAccountId })
      else if (tab === 'todo') content = h(TodoView, { t, status, accountId, onSelectAccount: setAccountId })
      else if (tab === 'sent')
        content = h(SentView, { t, status, onRefresh: refresh, accountId, onSelectAccount: setAccountId })
      else if (tab === 'draftsMail')
        content = h(DraftsMailView, { t, status, onRefresh: refresh, accountId, onSelectAccount: setAccountId })
      else if (tab === 'deleted')
        content = h(DeletedView, { t, status, onRefresh: refresh, accountId, onSelectAccount: setAccountId })
      else if (tab === 'junk')
        content = h(JunkView, { t, status, onRefresh: refresh, accountId, onSelectAccount: setAccountId })
      else if (tab === 'templates')
        content = h(TemplatesView, { t, status, onRefresh: refresh, accountId, onSelectAccount: setAccountId })
      else if (tab === 'contacts') content = h(ContactsView, { t, status, accountId, onSelectAccount: setAccountId })
      else if (tab === 'ai') content = h(AiWorkbench, { t, status })
      else if (tab === 'knowledge')
        content = h(KnowledgeView, { t, status, accountId, onSelectAccount: setAccountId })
      else content = h(SettingsView, { t, status, onRefresh: refresh })

      return h(
        'div',
        { style: styles.panel },
        h('style', { dangerouslySetInnerHTML: { __html: SELECT_CSS } }),
        h(TopBar, {
          t,
          status,
          onSync: syncAll,
          syncing: syncingAll,
          query,
          onQueryChange: setQuery,
          onCompose: () => setNewCompose({ mode: 'send', to: '', cc: '', subject: '', body: '' }),
        }),
        newCompose
          ? h(ComposeModal, {
              t,
              initial: newCompose,
              accountId: activeAccountId,
              onSubmitted: () => {
                setNewCompose(null)
                refresh()
              },
              onClose: () => setNewCompose(null),
            })
          : null,
        h(
          'nav',
          { style: styles.navScroll, role: 'tablist', 'aria-label': t('title') },
          TABS.map(([id, key]) =>
            h(
              'button',
              {
                key: id,
                type: 'button',
                role: 'tab',
                'aria-selected': tab === id,
                style: styles.tab(tab === id),
                onClick: () => setTab(id),
              },
              t(key),
            ),
          ),
        ),
        error !== null
          ? h(
              'div',
              { style: styles.errBox, role: 'alert' },
              h('p', { style: styles.error }, `${t('failed')}: ${error}`),
              h('button', { type: 'button', style: styles.btn, onClick: refresh }, t('retry')),
            )
          : status === null
            ? h('p', { style: { padding: '20px', fontSize: 13, color: 'var(--dsw-alias-label-tertiary)' } }, t('loading'))
            : h(
                'div',
                { style: styles.body },
                h('div', { style: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' } }, content),
                h(ApprovalsRail, { t, status, onRefresh: refresh }),
              ),
      )
    }

    function apply(ctx) {
      const t = ctx.locale ? ctx.locale.bind(NS) : (key) => key

      if (ctx.locale && typeof ctx.locale.register === 'function') {
        ctx.effect(() => {
          const offs = [ctx.locale.register(NS, 'zh', messages.zh), ctx.locale.register(NS, 'en', messages.en)]
          return () => offs.forEach((off) => off())
        }, 'mail: locale dictionaries')
      }

      // 一级工作区面板：keyed main，key = 侧栏 entry id
      ctx.slots.inject('main', () =>
        ctx.slots.register(
          {
            name: 'main',
            key: PANEL_ID,
            locale: NS,
            inject: () => ({ t }),
          },
          MailPanel,
        ),
      )

      // 侧栏图标入口：list，id 必须与 main.key 相同
      ctx.slots.inject('sidebar.panellist', () =>
        ctx.slots.register(
          {
            name: 'sidebar.panellist',
            id: PANEL_ID,
            order: 30,
            label: () => t('nav'),
            locale: NS,
          },
          MailIcon,
        ),
      )
    }

    return {
      inject: ['slots', 'locale'],
      apply,
      // 仅用于本地渲染测试（scripts/verify-client.mjs）；宿主只读 inject / apply。
      __test: {
        MailPanel,
        InboxView,
        AccountRail,
        SentView,
        DraftsMailView,
        DeletedView,
        JunkView,
        MailFolderView,
        TemplatesView,
        ContactsView,
        SearchView,
        MessageList,
        MessageDetail,
        ThreadBubbles,
        splitQuoted,
        buildCompose,
        ComposeModal,
        AgentMailComposer,
        TodoView,
        DraftsView,
        ApprovalsRail,
        KnowledgeView,
        AiCard,
        SettingsView,
        AccountForm,
        AccountRow,
        EmptyPane,
        TopBar,
        SplitPane,
        MailIcon,
        PANEL_ID,
        clampPane,
        clampWidth,
        LAYOUT: {
          RAIL_DEFAULT,
          RAIL_MIN,
          RAIL_MAX,
          LIST_DEFAULT,
          LIST_MIN,
          LIST_MAX,
          READING_MIN,
          DETAIL_MIN,
          HANDLE,
          PANE_SPEC,
        },
      },
    }
  },
})
