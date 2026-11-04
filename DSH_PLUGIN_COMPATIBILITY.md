# DSH 插件兼容性调查报告（Phase 0）

> 依据计划书第 3 节要求交付。本文只写**已用证据确认**的接口；未验证项单列第 7 节。
> 详细取证过程见 `docs/compat/host-runtime.md`（宿主能力）与 `docs/compat/ui.md`（前端 UI）。

- **DSH 版本**：`0.2.1-rc.5`（Electron 桌面版，`resources/runtime/primary-runtime/runtime.json` 的 `desktopVersion`）
- **profile**：`desktop`，目录 `C:\Users\<you>\.dsh\profiles\desktop`
- **源码 commit**：无法取得。安装包内只有构建产物（`resources/app.asar`），没有源码仓库与 git 元数据，因此**以运行时 Inspect 契约 + 已安装插件实现为准**，不引用未证实的 commit。
- **取证方式**：`cordis_inspect_query`（Host/Client 实时契约）> 已安装插件源码 > `app.asar` 内官方 README > 实机 HTTP 探测
- **调查日期**：2026-11-04

---

## 1. 结论速览：计划书 10 个待核实问题

| # | 计划书 §3.1 问题 | 结论 | 与计划书的差异 |
|---|---|---|---|
| 1 | 清单格式、发现与加载 | 插件 = Cordis **bundle**：`package.json` 声明 `dsh.bundle.patch` 指向 `cordis.patch.yml`，patch 用 `- insert:` 插入插件行。安装由 Plugin Manager 完成，**不要手写 profile** | 一致，但落地形态是 Cordis 插件包，不是自造清单 |
| 2 | 生命周期 | 安装 / 启用 / 停用 / 卸载 / 升级均由 Plugin Manager 管理；停用再启用即重新挂载（已实测）。**替换已安装包需重启 DSH** 才加载新 JS 模块代 | 一致 |
| 3 | 后台常驻 | **做不到脱离 DSH 常驻**。`ctx.timer` 是进程内定时器；`ctx.schedule` 是"到点向 Session 投递 follow-up 并可能唤醒模型"的提醒，不是 cron；`ctx.subprocess` 拉起的子进程随 Host 退出被 terminate | **重要差异**，见 §5 决策 D1 |
| 4 | Tool 注册 | `ctx.tools.register(ToolDefinition)` 可用，已实测注册并调用 | 一致 |
| 5 | 事件 | 宿主事件极多（`tools/execute`、`agent/*` 等），但**没有"插件向 Agent 主动发业务事件"的公开入口**。替代：工具轮询 + HTTP 通道 + 定时器 | 与原设想不同，见 §5 决策 D2 |
| 6 | 前端页面扩展 | 可行。React 组件注册进 Slot：1 个 `main` keyed 面板 + 1 个 `sidebar.panellist` 图标（id 必须相同）。**禁止 iframe**（拿不到主题 token / locale） | 一致，机制是 Slot |
| 7 | 设置页 / 密钥 / 权限 | 设置页 = `settings.section` slot；插件行配置 = `plugins.row.config`/`plugins.bundle.config`；密钥 = `ctx.credentials`，**但明文落盘** | 安全要求不满足，见 §5 决策 D3 |
| 8 | 插件↔宿主通信 | Host 侧 `ctx.webServer.register({kind,path,handler})` 注册本机 HTTP 路由，Client 同源 `fetch`。**Typert `@Remote` RPC 需构建期生成物，第三方无可复用样本** | 一致，采用 HTTP |
| 9 | 市场打包 / 签名 | 未调查（Phase 8 范围）。当前以本地目录 bundle 安装可用 | 留待后续 |
| 10 | 前端技术栈 | React 18 + CSS 变量主题（`--dsw-alias-*`）；**没有可依赖的共享组件库**，需自写控件并对齐宿主样式；i18n 走 `ctx.locale` | 与原设想不同（不引入 UI 框架） |

---

## 2. 环境事实（影响技术选型）

| 事实 | 证据 |
|---|---|
| 系统 Python **不存在**（`python.exe` 只是 Microsoft Store 占位符） | `where.exe python` + 运行即报 "Python was not found" |
| DSH **自带 Python 3.12.14**，`pip 26.2.1`、`ensurepip` 可用、SQLite **含 FTS5** | `~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe`，实测 `pip --version`、FTS5 建表成功 |
| DSH 自带 **Node 24.21.0** 与 **pnpm 11.28.5** | `resources/runtime/versions.json` |
| WSL 有 Python 3.14.4，但 **WSL2 网络命名空间独立**，访问 `127.0.0.1:52452` 失败 | 实测 `curl` 失败；Windows 侧 `curl.exe` 成功 |
| 当前 GUI 为 HTTP 形态，本机路由可达 | `curl.exe` POST `/api/dsh-free-search-settings/describe` → 200 |

**结论**：计划书的「Python 3.12+」前提成立，但**必须使用 DSH 自带的 Python**，不能用系统 Python。

---

## 3. 已验证接口（可直接依赖）

### 3.1 Host 插件

```js
// index.js
export const name = 'dsh-mail-plugin'
export const inject = ['tools', 'webServer', 'timer']   // 硬依赖，缺一不激活
export function apply(ctx, config) { /* 注册一律挂 ctx.effect */ }
```

- 资源注册必须包在 `ctx.effect(fn, label)` 里，返回值即清理函数。
- 可选服务用 `ctx.inject([...], cb)`，避免在缺服务的组合里半死。

**Tool**（实测调用成功）：

```js
ctx.tools.register({
  name: 'mail_status', description: '...', parameters: { type: 'object', properties: {} },
  output: { schema: { /* 受限 JsonSchemaNode */ },
            render: (args, value) => [{ type: 'text', text: '...' }] },  // 必须 ContentBlock[]
  execute: async (args, exec) => ({ /* JsonValue */ }),
  timeoutMs: 15000, isConcurrencySafe: () => true,
})
```

- `ContentBlock` 只有 7 种：`text / reasoning / image / file / tool-call / tool-addition / tool-removal`，**无 `message` 块**。
- `output.schema` 是受限 JSON Schema：不支持 `$ref`、`pattern`、`minimum`；`type` 只能是**单个字符串**（不能写 `["string","null"]`）。
- 工具卡片只有 `generic / terminal / diff / search / read / web` 五类，**没有邮件卡片** → 邮件 UI 必须走 Client slot。

**HTTP 通道**（实测 200 / 403 / 405）：

```js
ctx.webServer.register({ kind: 'exact', path: '/api/dsh-mail/status',
  handler: async (req, res) => { /* 自己 writeHead/end；无框架 */ } })
```

- 服务端**不做鉴权**，路由必须自守：校验 `Host` 为 loopback、拒绝 `sec-fetch-site: cross-site`、`Origin` 与 `Host` 同源、限制 body 64 KiB。

**定时器**：`ctx.timer.interval(cb, ms)`，绑定插件 fiber，卸载自动清理。

**配置**：`export const Config` 用 `@deepseek-ai/schemastery`（profile 内已装 3.18.5）。Phase 0 未启用，Phase 1 补。

### 3.2 Client 插件

```js
window.__ModuleLoader__.load({ id: '<包名>', factory(require) {
  const React = require('react')          // 浏览器模块表；不写 import/export
  return { inject: ['slots', 'locale'], apply(ctx) {
    ctx.slots.inject('main', () => ctx.slots.register(
      { name: 'main', key: PANEL_ID, locale: NS, inject: () => ({ t }) }, Panel))
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register(
      { name: 'sidebar.panellist', id: PANEL_ID, order: 30, label: () => t('nav'), locale: NS }, Icon))
  } }
} })
```

- `main` 是 keyed slot，`sidebar.panellist` 是 list；**`main.key` 必须等于 `panellist.id`**，否则点击侧栏图标会抛 `main panel "x" is not registered`。
- 面板 props：`inject()` 的返回值 + 宿主标准 hooks（`usePanelInfo`、`useSessions` 等）。Phase 0 实测该面板 `active: true`。
- 样式只用 `--dsw-alias-*` 等 CSS 变量，深浅色自动跟随；**不要 import 宿主 Client 组件包**（无兼容保证）。
- 宿主**没有二级导航 slot**：收件箱 / AI / 草稿 / 审批 / 知识库只能在一个面板内部做 tab。

### 3.3 i18n 与资源

- `locale/*.json` 只承载展示元数据：`{ "meta": { "title", "description" } }`。
- 运行时文案走 `ctx.locale.register(ns, lang, dict)` + `ctx.locale.bind(ns)`。
- 包根 `icon.svg`（≤256 KiB）+ `exports["./icon"]`；侧栏图标是注册时传的 React 组件（内联 SVG，`currentColor`）。

---

## 4. 最小可运行示例插件

`dsh-mail-plugin/`（已安装到 `desktop` profile 并验证通过）：

| 能力 | 落点 | 验证结果 |
|---|---|---|
| Agent 工具 | `mail_status` | ✅ 工具出现在工具表并可调用，返回文本块 |
| HTTP 通道 | `GET /api/dsh-mail/ping`、`POST /api/dsh-mail/status` | ✅ 200 + JSON；跨源/跨站 → 403；错误方法 → 405 |
| 后台轮询 | `ctx.timer.interval` | ✅ `pollCount` 生效，首轮立即探测 |
| 一级工作区 | `main` key `mail-workspace` | ✅ Slot 占位符 `active: true` |
| 侧栏入口 | `sidebar.panellist` id `mail-workspace` order 30 | ✅ 与 schedules / dsh-wsl 并列 |
| 生命周期 | 停用 → 启用 | ✅ 修复 inject 后重新挂载成功 |

> 未验证：面板的实际像素渲染（本会话无浏览器控制），只确认注册生效且未崩溃。

---

## 5. 决策结果（2026-11-04 定稿）

### D1  后台同步的生命周期 → **选 A：纯插件，只在 DSH 运行时同步**

- 放弃 7×24 收信；DSH 启动时补拉积压邮件。
- 计划书 §1.3 / §11.2 / §17.3 验收 #10（DSH 关闭后仍监听）**作废**。
- 不再需要 Windows 计划任务 / 服务包装，计划书 §11.2 相应简化。
- 影响：`ctx.timer.interval` 是唯一的同步驱动；DSH 关多久，邮件就积多久。

### D2  插件 → Agent 的事件 → **不做主动事件，走「工具 + 界面主动拉取」**

- 宿主没有「插件向 Agent 发业务事件」的公开入口，计划书 §5.3 的 6 个事件**不实现**。
- 落地：只读工具（`mail_status` 等）+ `/api/dsh-mail/*` 通道；待处理事项靠界面顶栏红标提示。
- 不采用 `ctx.schedule` 投递摘要：它按次唤醒模型，会持续产生 token 成本。

### D3  凭据 → **选 B：Mail Core 侧 DPAPI，插件不接触密码**

- 不使用 `ctx.credentials`（明文 YAML）。Python 用 `ctypes` 调 `CryptProtectData` / `CryptUnprotectData`，密文写入 SQLite。
- DPAPI 绑定当前 Windows 用户：换用户或换机器后密文无法解密，需重新录入（可接受）。
- 插件进程全程拿不到明文密码，只会通过 IPC 让 Core 拨号。

### D4  技术栈 → **Python（DSH 自带 3.12.14，pip 可用，SQLite 含 FTS5）**

- 架构落地：**Python Mail Core 作为插件的子进程**，用 `ctx.subprocess.spawn` 拉起，生命周期跟随 DSH。
- 计划书里「独立常驻进程」的含义改为「独立进程、同生命周期」：崩溃可单独重启，但不会比 DSH 活得更久。
- 数据目录：`<repo>\data\mail\`（工作区内，便于备份与打包）。

### 5.1  通道与依赖（已定 · 2026-11-04）

| 编号 | 问题 | 结论 |
|---|---|---|
| D5 | 插件 ↔ Core 通道 | **stdio JSON-RPC**：父子进程管道，一行一个 JSON。无端口、无令牌文件、无需鉴权；Core 退出时在途请求立刻失败。协议见 `docs/IPC_PROTOCOL.md` |
| D6 | Core 的依赖 | **纯标准库**：`imaplib` / `smtplib` / `email` / `sqlite3` / `json` / `ctypes`。零安装、零联网；自带 Python 的 FTS5 已验证可用。代价是没有自动 OpenAPI 文档和 ORM |

**已知限制**：插件暂时没有 `export const Config`。宿主校验 Config 需要 `@deepseek-ai/schemastery`，而本地目录安装（pnpm link）的插件在真实路径下解析不到 profile 的 `node_modules`，顶层 import 会让插件直接加载失败。当前配置由 `apply(ctx, config)` 防御式解析（缺省即回落默认值）；打包成正式依赖后再补 schema。

---

## 6. 对计划书其他条款的校准

| 计划书条款 | 校准结论 |
|---|---|
| §12 API 设计 | 本机 HTTP 路由可行，但需同时提供**同源守卫**（`Host`/`Origin`/`sec-fetch-site`），并在文档中声明「无服务级鉴权」。建议保持 `127.0.0.1` 绑定 |
| §6 UI 9 个页面 | 只能落在**一个** `main` 面板内的 tab；若要 6 个独立侧栏入口，需注册 6 组 `main`+`panellist`（技术可行，侧栏会变长） |
| §7.1 技术栈 | FastAPI/SQLAlchemy/Alembic 可用（自带 pip 可装），但需先确认是否允许向自带运行时安装第三方包（Phase 1 第一件事） |
| §5.1 工具清单 | 全部可注册；`mail.execute_action` 的审批约束要用 `ctx.approval` + 自建审批记录实现，不能靠宿主强制 |
| §11.3 安装脚本 | Plugin Manager 已承担安装/升级/卸载，PowerShell 脚本只需覆盖 Mail Core 侧（若选 D1 选项 B） |

---

## 7. 未验证 / 风险

1. **DSH 关闭后的同步**：已按 D1 接受——关闭期间不收信，启动时补拉。这不是缺陷，是选定行为，需在用户文档里写明。
2. **凭据明文**：`ctx.credentials` 不具备保护力；方案是绕开它（D3），插件不接触密码。
3. **`ctx.subprocess` 与 sandbox/审批的关系**：未读实现、未实测；若不受约束，插件拉子进程等于绕过 workspace-write。
4. **Electron 渲染层的 fetch 路径**：桌面版若改为 `file://` + IPC bridge，相对 `/api/*` 可能失效；**当前 HTTP 形态已实测可用**，升级需复核。
5. **`ctx.storageDomain` 不适合邮件主库**（全量内存、无查询、无迁移）；邮件数据应放 Mail Core 的 SQLite。
6. **版本升级影响**：本报告部分证据来自 `app.asar` 旁路读取，升级后需重新核对；Host 侧结论均来自运行时 inspect，较稳定。
7. **插件依赖解析**：本地目录安装可用；若发布到市场，`@deepseek-ai/*` 应放 peerDependencies（参照 free-search / archive-manager）。
8. **`settings.section` 的 icon 字段**语义未验证，不要依赖。

---

## 8. 版本兼容策略

- 插件 `package.json` 声明 `dsh.engines.dsh: ">=0.2.1-rc.5"`，与本次验证版本对齐。
- 只使用**运行时 Inspect 可见**的公开 Service 与 Slot；不触碰内部类。
- 对 `app.asar` 源码级结论（slot 声明、token 名）视为"可降级"：升级后外观可能变化，但不影响功能正确性。
- 每次 DSH 升级后重跑本文 §3 的三项冒烟验证（工具调用、HTTP 200、Slot 注册）。
