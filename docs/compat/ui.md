# DSH 前端 UI 兼容性勘察（邮箱管理 · Phase 0）

环境：DSH 0.2.1-rc.5，profile=desktop，Web GUI `http://127.0.0.1:52452`，工作目录 `<repo>`。
本文只写验证过的东西；未验证项集中放在第 8 节。

## 0. 证据分级（先看这条）

| 级别 | 来源 | 可信度 |
|---|---|---|
| A | `cordis_inspect_query`（host 侧可用：`Service webServer`、`Service typert`、`Service typertGateway`） | 运行时真实契约 |
| B | 已安装插件源码/构建产物（`~/.dsh/profiles/desktop/node_modules/*`） | 真实调用点，随版本可能变 |
| C | DSH 官方 skill 文档与包内源码（从 `app.asar` 原始字节中读出） | 官方口径，但为旁路读取 |
| D | 推断（标注「推断」） | 不作为设计依据 |

> **重要限制**：client 侧 inspect（`Slots.listSubTree` / `Theme.listTokens` / `Builtin.listBuiltins`）在本会话连续超时 5 次（每次 10s），原因是浏览器页面没有回应 inspect 通道，不是权限问题。因此 `main` / `sidebar.panellist` 的 live props **没有拿到**，本文改用 B + C 级证据（ui-layout、ui-sidebar 的实际注册代码与官方文档），并在第 8 节标出待复核点。
>
> Desktop 的 `app.asar` 无法被 shell / glob / rg / node 直接打开（官方文档原话见 `references/persistent-plugins.md`）；本次用 `dd` 读原始字节定位文本，属于只读旁路，**升级版本后偏移全部失效，结论必须重核**。

---

## 1. Client 插件模块形态

### 1.1 清单字段

`dsh-free-search/package.json` 是现成范本（行号为准）：

- `:7-11` `"exports": { ".": "./lib/index.js", "./client": "./lib/client.js" }`
- `:102-114` `"dsh": { "bundle": { "patch": ... }, "client": { "inject": [...] , "platform": "web" } }`

即：**同一个包同时有 host 半（`lib/index.js`）和 client 半（`lib/client.js`）**，client 半靠 `exports["./client"]` + `dsh.client.platform: "web"` 被发现，不需要额外字段。

`dsh.client.inject` 的语义（官方文档 `references/ui-plugin.md`）：只用于**激活顺序**，不是运行时 `require` 白名单；`inject` 里的包会先激活。`dsh.client.immediately: true` 在官方模板里出现，作用是让 client 模块页面启动即加载；已装插件里 `dsh-archive-manager` 没写 `immediately` 也能注册侧栏入口，**说明非必需（语义未验证，见第 8 节）**。

### 1.2 文件格式：不是 ESM，是浏览器模块工厂

`dsh-free-search/lib/client.js:1-10`：

```js
window.__ModuleLoader__.load({
  id: "dsh-free-search",            // 必须等于 package name
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    let react = require("react");
    let react_dom = require("react-dom");
    let react_jsx_runtime = require("react/jsx-runtime");
    // ...
```

`:2434-2436`：

```js
exports.apply = apply;
exports.inject = inject;
return module.exports;
```

所以 client 半导出的是 **`{ inject: string[], apply(ctx) }`**（`inject` 是 client 服务名数组，如 `["slots","commandUi"]`，见 `:2362`）。没有 `default`。

- `id` 必须等于包名（官方模板 `@local/my-decoration` 同包名）。
- 只能 `require` 浏览器模块表里的模块：`react`、`react-dom`、`react-dom/client`、`react/jsx-runtime`，以及 `dsh.client.inject` / `dsh.client.external` 声明的非基线包（C 级）。
- 文件里**不要写 `import`/`export`**，它是全局脚本；`dsh-better-sidebar/lib/client.js` 也是这种包法。

### 1.3 React / 组件库 / 主题怎么拿

- **React**：`require("react")`，来自浏览器的模块表，不需要自己装 React（官方文档 `references/ui-plugin.md`）。
- **组件库**：**没有受支持的共享组件 API**。官方口径（`references/practices.md` 的 `## UI` 段）明确写：

  > Do not `require('@deepseek-ai/dsh-client-ui-primitives')` or load any other Harness Client package as a module …
  > Write your own controls and match the host instead: copy markup, CSS, and behavior from the primitive into the plugin.

  已安装的 `dsh-better-sidebar` 确实 `require("@deepseek-ai/dsh-client-ui-primitives")`（`lib/client.js:32`），`@michengai/dsh-archive-manager/lib/sidebar-entry.js:2` 也在用 `IconArchiveOutlineMedium / SegmentedControl / Switch`。**能跑，但不受保证**：组件抛异常会让整个 slot entry 变成空白（控制台 `slot entry crashed in '<slot>'`）。邮箱插件建议自写控件 + 复制宿主样式。
- **主题**：只用 CSS 变量 `--dsw-*`，见第 4 节。不需要 JS 主题 API。

---

## 2. 中央面板 + 侧边栏入口（一级工作区的关键）

### 2.1 slot 的真实声明

来自 `app.asar` 中 `@deepseek-ai/dsh-client-ui-layout` 的注册代码（C 级，与 B 级插件调用一致）：

```
name: "root", locale: "common", children: {
  "sidebar":            { kind: "single", scope: "root" },
  "main":               { kind: "keyed",  scope: "root" },   // ← 中央面板
  "rightbar":           { kind: "single", scope: "root" },
  "shell.bottom":       { kind: "single", scope: "root" },
  "shell.overlay":      { kind: "list",   scope: "root" },
  "shell.leading":      { kind: "single", scope: "root" }
}
```

`@deepseek-ai/dsh-client-ui-sidebar` 的注册代码：

```
children: {
  "sidebar.panellist":          { kind: "list", scope: "root" },   // ← 图标入口
  "sidebar.panellist.actions":  { kind: "list", scope: "root" },   // More 菜单
  "sidebar.workspaces":         { kind: "single", scope: "root" },
  "sidebar.settings":           { kind: "single", scope: "root" },
  "sidebar.footer.action":      { kind: "list",   scope: "root" }
}
```

渲染方式（`app.asar` 内 ui-layout 的 `MainPanel`）：

```js
renderSlot("main", {}, { entryKey: usePanelInfo((i) => i.activePanelId) ?? "conversation" })
```

→ **`main` 是按 key 选的 keyed slot；注册时用 `key`，key 就是侧栏 `panellist` 的 `id`。**
`main` 的 owner props 是空对象 `{}`，所以**面板能拿到的数据只有 `inject()` 返回的东西**。

侧栏图标渲染（ui-sidebar 的 `PanelRow`）：

```js
renderSlot("sidebar.panellist", { size: wide ? 16 : 18, active }, { only: id })
```

→ **`sidebar.panellist` 组件收到 `{ size, active }`**；`label` 被用作可见文字（展开态）、`aria-label` 和折叠态 tooltip；点击行时宿主自己调用 `selectPanel(id)`。

官方文档（`app.asar` 内 `references/…`「Global panel entries」段）原话：

> The same id addresses the component registered in the layout's root-scoped `main` keyed slot; selecting a missing main entry throws without changing the current selection.

代码印证（`LayoutController.selectPanel`）：

```js
selectPanel(panelId) {
  if (panelId !== null && !this.hasMainPanel(panelId))
    throw new Error(`layout.selectPanel: main panel "${panelId}" is not registered`)
  ...
}
```

### 2.2 确切的注册调用（可直接抄）

`@michengai/dsh-archive-manager/lib/sidebar-entry.js:245-253`（`main`）与 `:268-275`（`sidebar.panellist`）（B 级，真实可用）：

```js
// 中央面板：keyed by panel id
ctx.slots.register({
  name: "main",
  key: SIDEBAR_PANEL_ID,          // 必须与 panellist 的 id 相同
  locale: ns,
  inject: () => {
    const extra = options.injectProps ? options.injectProps() : {};
    return Object.assign({ t: ctx.locale.bind(ns) }, extra);
  },
}, (ownerProps) => h("div", { className: "dsham_mainPanel" }, renderPanel(ownerProps)));

// 侧栏图标入口：list slot，用 id
ctx.slots.register({
  name: "sidebar.panellist",
  id: SIDEBAR_PANEL_ID,
  order: SIDEBAR_PANEL_ORDER,     // 数字，升序（推断：plugins=0、market=1、archive=4、schedule=10）
  label: () => ctx.locale.bind(ns)("archives.title"),
  locale: ns,
}, (ownerProps) => ArchiveSidebarIcon({ size: ownerProps?.size }));
```

**注册必须包在 `ctx.slots.inject(ownerKey, () => ...)` 里**（官方文档 `references/practices.md`）：

```js
ctx.slots.inject("main", () => {
  mainSupported = true;
  mountMain();
  return () => { mainSupported = false; unmountMain(); };
});
ctx.slots.inject("sidebar.panellist", () => {
  sidebarSupported = true;
  mountRow();
  return () => { sidebarSupported = false; unmountRow(); };
});
```

（`lib/sidebar-entry.js:473-490`。`slots.inject` 会等宿主的 `root` 声明就位；回调返回的 disposer 在声明塌缩时执行。）

同样的写法在 `dshmarket/client/client.js:18412-18429` 出现（`main` + `sidebar.panellist`），`settings.section` 在 `:18377-18399`，可作第二份样本。

### 2.3 面板内部要自己处理滚动

`dsh-archive-manager` 的面板样式（`lib/sidebar-entry.js:8-10`）：

```css
.dsham_mainPanel{box-sizing:border-box;height:100%;min-height:0;overflow-y:auto;
  display:flex;flex-direction:column;align-items:center;padding:28px clamp(24px,4vw,48px) 48px}
.dsham_mainPanel>*{width:100%;max-width:960px}
```

中央列不会替插件面板加内边距/滚动。

### 2.4 结论：一级工作区怎么做

**能做成一级页面**，形态是：**1 个 `main` keyed 面板 + 1 个 `sidebar.panellist` 图标入口**。宿主没有「一级 → 二级导航」的 slot；收件箱 / AI 工作台 / 草稿箱 / 审批中心 / 知识库 / 设置 这 6 个分区，只能在**这一个面板内部**用自定义 tab/侧列实现（dshmarket 与 plugins 页都是这个模式：一个面板内部再分 tab）。「设置」如果指账号/服务配置，建议同时注册一个 `settings.section`（第 3 节），与面板内的「设置」tab 二选一或互链。

---

## 3. 设置页与插件行配置

### 3.1 `settings.section`（设置里的一页）

声明：`{ kind: "list", scope: "root" }`（`app.asar` 内 ui-settings 注册处）。
宿主渲染：`renderSlot("settings.section", { close: onClose }, { only: active })` → 组件收到 `close`。

范本 `dsh-better-sidebar/src/client/index.tsx:458-465`：

```js
ctx.slots.inject('settings.section', () => ctx.slots.register({
  name: 'settings.section',
  id: 'better-sidebar',
  order: 100,
  label: () => t('settingsNav'),
  inject: () => ({ store: sidebarStore, service }),
}, SideCardSection))
```

`dshmarket/client/client.js:18377-18399` 同一写法（`id: "market", order: 40, label: () => t("nav"), locale: NS, inject: () => ({ t })`）。

- `label` 是函数（返回本地化标题），也可传字符串。
- **`icon` 字段**：`@michengai/dsh-archive-manager` 的 `settings.section` 注册里带了 `icon: "archive"`（字符串）；但 `dsh-better-sidebar` 的注释说 0.1.x 的设置页不认 icon、它自己画了 SVG。**未验证，建议不依赖**（见第 8 节）。

### 3.2 `plugins.row.config`（插件页里某一行展开的配置）

声明：`{ kind: "keyed", scope: "root" }`；宿主渲染两处：

```js
renderSlot("plugins.row.config", { view: "summary" }, { entryKey: key })   // 行摘要，期望返回字符串
renderSlot("plugins.row.config", { view: "page", form }, { entryKey: key }) // 详情页，返回 React
```

`key` 的规则（`dsh-free-search/lib/client.js:2369-2379` 注释 + `:2373` 实测值）：**`<包名>#<补丁行 id>`**，例如 `dsh-free-search#web-search-free`（补丁行 id 见 `dsh-free-search/cordis.patch.yml` 的 `- id: web-search-free`）。

范本：

```js
ctx.slots.inject("plugins.row.config", () => ctx.slots.register({
  name: "plugins.row.config",
  key: "dsh-free-search#web-search-free",
}, (slotProps) =>
  slotProps && slotProps.view === "summary"
    ? summaryText()
    : h(FreeSearchCard, { page: true })))
```

另有 `plugins.bundle.config`（keyed，key = 包名），`dsh-free-search/lib/client.js:2389-2398` 同时注册了它，让配置卡在 bundle 页也显示。0.2.x 里这是推荐入口。

**注意**：`free-search` 的配置读写完全不走 `form`，走的是自建 HTTP bridge（第 5 节）；`form` 参数是什么契约**未验证**。

---

## 4. 主题与组件

### 4.1 主题 token（A/B/C 混合，来源可靠）

- 官方要求：只用 `--dsw-alias-*` token，字面色值只用于插图（`references/practices.md` `## UI`）。
- token 定义文件：`@deepseek-ai/dsh-client-ui-theme` 的 `src/styles/design-platform.css`（`app.asar` 内）。
- **浅色**在 `body { --dsw-alias-* }`；**深色覆盖**在 `body[data-ds-dark-theme] { ... }`；主题来源属性是 `data-ds-theme-source`（`light|dark|system`）。插件只要用 `var(--dsw-*)`，宿主切换深浅色时自动跟随，**不需要自己写 dark 覆盖**。
- 从该 CSS 中实测提取到 **101 个 `--dsw-alias-*`**，另有 `--dsw-static-*`、`--dsw-radius-*`、`--dsw-font-*`、`--dsw-focus-ring-*`、`--dsw-elevation-*`、`--dsw-specific-*`。常用的（按宿主自身使用次数排序，`app.asar` 全文统计）：

```
--dsw-alias-label-primary       360   --dsw-alias-label-tertiary      349
--dsw-alias-label-secondary     270   --dsw-alias-state-business-primary 188
--dsw-alias-interactive-bg-hover 164  --dsw-font-family               135
--dsw-alias-state-error-primary 130   --dsw-radius-sm                 106
--dsw-focus-ring-color          106   --dsw-alias-border-l2            99
--dsw-alias-label-caption        98   --dsw-radius-md                  86
--dsw-alias-bg-layer-1           57   --dsw-alias-bg-base              52
--dsw-alias-bg-module-platform   37   --dsw-alias-state-success-primary 37
--dsw-alias-border-l1/l3/l4      64/57/54   --dsw-alias-bg-layer-2/3     31/19
```

现有插件里的用法（可直接抄）：`@michengai/dsh-archive-manager/lib/sidebar-entry.js:8-24` 用了 `--dsw-alias-border-l2`、`--dsw-alias-label-primary`、`--dsw-alias-label-secondary`、`--dsw-alias-state-error-primary`。

`Theme.listTokens` 本该给出权威清单，**本会话拿不到**（超时）。等页面可连时用它复核。

### 4.2 组件从哪来

- **没有可依赖的共享组件库**（见 1.3）。
- 做法：复制宿主同类页面的 markup + CSS class 结构 + 交互（Modal 焦点/Escape、`role="switch"` + `aria-checked`、Tooltip 位置），class 名改成自己的前缀，颜色只留 `--dsw-*`。
- 官方推荐的管理列表参照物是 **Plugin Manager 页**。
- 若坚持 `require` 客户端包：必须在 `dsh.client.inject` 里声明（只影响顺序）+ `dsh.client.external` 声明非基线运行时 import（C 级，语义未验证）。

---

## 5. 前后端通信：走 HTTP 路由，不要自己造 RPC

### 5.1 结论

**邮箱 UI 拉数据走宿主 HTTP 路由**（`ctx.webServer.register`），client 侧同源 `fetch`。理由：
1. 三条独立先例都是这条路：`dsh-free-search`（`/api/dsh-free-search-settings/*`）、`dsh-better-sidebar`（`/sidebar/api/*`）、`@michengai/dsh-archive-manager`（`/api/michengai/dsh-archive-manager/*`）。
2. Host 侧 `webServer` 是公开 Service（A 级，见下），契约稳定。
3. Typert `@Remote` RPC 需要**构建期生成** `InvocationDescriptor` / `TypertContribution`（A 级 `typert.register(contribution)` 契约），已安装的第三方插件没有一个走它 —— 对纯 JS bundle 成本高且无样本。

### 5.2 Host 侧（A 级契约）

`cordis_inspect_query(host, Service, "webServer")` 返回：

```
register(route: WebRoute): () => void
WebRoute = { kind: 'exact' | 'prefix', path: string,
             handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void> }
```

真实调用（`dsh-free-search/lib/index.js:4612-4626`）：

```js
ctx.inject(["webServer", "settings"], (sctx) => {
  sctx.effect(() => {
    const disposers = makeBridgeRoutes(...)
      .map((route) => sctx.webServer.register(route))
    return () => disposers.forEach((d) => d())
  }, "free-search: settings bridge")
})
```

路由对象形状（`lib/index.js:3522-3530` 起）：

```js
{ kind: "exact", path: `${BRIDGE_PREFIX}/raw-search`, handler: async (req, res) => { ... } }
```

handler 直接写 Node 的 `res`（自己 `writeJson`），**没有框架包装**。

**安全**：`webServer` 不替你鉴权。`dsh-free-search/lib/index.js:3294-3311` 的 `isBridgeRequestAllowed` 是现成 guard：拒绝 `sec-fetch-site: cross-site`，校验 `Origin` 与 `Host` 同源，loopback 只允许本机来源，非 loopback 走受信 host 列表。**邮箱接口照抄这段，再额外校验 HTTP method。**

### 5.3 Client 侧

```js
const BRIDGE = "/api/dsh-email"
const res = await fetch(`${BRIDGE}/inbox?folder=inbox`, { method: "POST",
  headers: { "content-type": "application/json" }, body: "{}" })
const json = await res.json()
```

`dsh-free-search/lib/client.js:286` + `:688-733` 是完整样本（describe / mutate / raw-search / credentials-*）。`@michengai/dsh-archive-manager` 的 client 用 `fetch(Rn('/api/michengai/dsh-archive-manager' + path))`，`Rn` 是反代前缀拼接 helper —— **如果宿主可能被反代到子路径，路由前缀要跟宿主一致（未验证具体 helper 契约）**。

### 5.4 架构建议（与官方 practices 对齐）

1. Host 侧把「列邮件 / 读邮件 / 发草稿 / 审批」实现成**普通 service 方法**，返回 `{ok:true,value}` / `{ok:false,code,message}`。
2. HTTP 路由只做薄封装（解析 body → 调方法 → 写 JSON）。
3. 同样的方法再暴露成 agent tool，UI 与 agent 共用一份逻辑（`references/user-actions.md` 的「One operation, two callers」）。
4. **不要**用 iframe 托管页面（拿不到主题 token / 深浅色 / locale），不要替换 app root 或往 `document.body` 追加应用（`references/practices.md`）。

---

## 6. i18n / 图标 / 资源

### 6.1 locale/*.json 只负责「显示元数据」

`dsh-free-search`、`dsh-better-sidebar`、`@michengai/dsh-archive-manager` 都导出 `./locale/*.json`，文件内容只有：

```json
{ "meta": { "title": "Better Sidebar",
            "description": "更好用的右侧边栏——…" } }
```

（`dsh-better-sidebar/locale/zh.json` 全文即此。）官方文档明确：Plugin Manager / 设置页**不激活插件**就读这里的 `meta.title` / `meta.description`。**它不是运行时 UI 文案表**。

### 6.2 运行时文案：client locale service

`dsh-better-sidebar/src/client/index.tsx:69-72`（真实可用）：

```js
attachLocale(ctx.locale)
ctx.effect(() => {
  const offZh = ctx.locale.register(LOCALE_NS, 'zh', zh)
  const offEn = ctx.locale.register(LOCALE_NS, 'en', en)
  return () => { offZh(); offEn() }
})
```

- `ctx.locale.register(ns, lang, dict) -> disposer`
- `ctx.locale.bind(ns) -> (key, params?) => string`
- `ctx.locale.getSnapshot().active`、`ctx.locale.subscribe(cb)`（`src/client/Sidebar.tsx:105-106`）
- 组件里拿到 `t` 的两种方式：在 `register(..., { inject: () => ({ t: ctx.locale.bind(ns) }) })` 里传；或在组件内部用 `ctx.locale.bind(ns)` 闭包（面板是普通函数组件，`ctx` 从 `apply` 闭包拿）。
- 客户端服务名声明：`dsh-better-sidebar/lib/client.js:32800-32806` 的 `inject` 数组含 `"slots","sessions","locale","modules","connection","remote"`；`locale` 可直接写进模块的 `inject`。

字典 key 规范（从各插件观察）：**点号分层**（`archives.title`、`entry.location.both`、`menu.archiveWorkspace`），`{name}` 占位符由宿主 `t(key, params)` 插值。

### 6.3 图标与资源

- 包根放 `icon.svg`（或 png/jpeg/webp，≤256 KiB），并在 `exports` 里给 `"./icon": "./icon.svg"`，`files` 里包含它 —— `dsh-better-sidebar/package.json:33`。
- 也可在 `package.json` 顶层写 `"icon": "path"`，优先级高于 `./icon`（官方文档）。
- 子路径插件（非包）导出 `./<sub>/locale/*.json` 和 `./<sub>/icon`。
- **侧栏 `panellist` 的图标不是这个 `icon.svg`**：它是注册时传的 React 组件（返回内联 SVG，用 `currentColor`），收到 `{size}`。参考 `lib/sidebar-entry.js:26-29`、`dshmarket/client/client.js:18423`。

---

## 7. 最小可运行 client.js 骨架

打包结构（`package.json` 关键字段）：

```json
{
  "name": "dsh-email-automation",
  "version": "0.0.1",
  "type": "module",
  "exports": {
    ".": "./lib/index.js",
    "./client": "./lib/client.js",
    "./locale/*.json": "./locale/*.json",
    "./icon": "./icon.svg",
    "./package.json": "./package.json"
  },
  "files": ["lib", "locale", "icon.svg", "cordis.patch.yml"],
  "dsh": {
    "bundle": { "patch": "./cordis.patch.yml" },
    "client": {
      "platform": "web",
      "inject": ["@deepseek-ai/dsh-client-ui-sidebar", "@deepseek-ai/dsh-client-locale"]
    }
  }
}
```

`lib/client.js`（可直接跑，注释里标了必须替换的点）：

```js
// 浏览器全局脚本，不是 ESM：不要写 import/export
window.__ModuleLoader__.load({
  id: 'dsh-email-automation',            // ← 必须等于 package.json 的 name
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const NS = 'email'                    // ← locale 命名空间，改成你登记的 ns
    const PANEL_ID = 'email-workspace'    // ← main.key 与 panellist.id 必须相同
    const API = '/api/dsh-email'          // ← 与 host ctx.webServer.register 的 path 一致

    // 运行时文案：与 locale/*.json（只有 meta）分开维护
    const messages = {
      zh: { nav: '邮箱', panelTitle: '邮箱管理', inbox: '收件箱',
            ai: 'AI 工作台', drafts: '草稿箱', approvals: '审批中心',
            kb: '知识库', settings: '设置', loading: '加载中…', failed: '加载失败' },
      en: { nav: 'Email', panelTitle: 'Email workspace', inbox: 'Inbox',
            ai: 'AI workspace', drafts: 'Drafts', approvals: 'Approvals',
            kb: 'Knowledge', settings: 'Settings', loading: 'Loading…', failed: 'Failed to load' },
    }

    /** 同源 HTTP bridge 调用；host 侧用 ctx.webServer.register 注册同样的 path。 */
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

    /** 侧栏图标：收到 { size }，用 currentColor 跟随主题。 */
    function EmailIcon({ size = 16 }) {
      return h('svg', {
        width: size, height: size, viewBox: '0 0 16 16',
        fill: 'none', stroke: 'currentColor', strokeWidth: 1.3,
        strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true,
      },
        h('rect', { x: 1.5, y: 3, width: 13, height: 10, rx: 2 }),
        h('path', { d: 'M2 4l6 4.5L14 4' }),
      )
    }

    const TABS = [
      ['inbox', 'inbox'], ['ai', 'ai'], ['drafts', 'drafts'],
      ['approvals', 'approvals'], ['kb', 'kb'], ['settings', 'settings'],
    ]

    /** 中央面板。props 只有 inject() 给的 { t }。 */
    function EmailPanel({ t }) {
      const [tab, setTab] = React.useState('inbox')
      const [data, setData] = React.useState(null)
      const [error, setError] = React.useState(null)

      React.useEffect(() => {
        const ac = new AbortController()
        setError(null); setData(null)
        call('/list', { tab }, ac.signal)
          .then((value) => setData(value))
          .catch((e) => { if (!ac.signal.aborted) setError(e.message) })
        return () => ac.abort()
      }, [tab])

      return h('div', { style: styles.panel },
        h('nav', { style: styles.nav, role: 'tablist', 'aria-label': t('panelTitle') },
          TABS.map(([id, key]) => h('button', {
            key: id, type: 'button', role: 'tab',
            'aria-selected': tab === id,
            style: tab === id ? styles.tabActive : styles.tab,
            onClick: () => setTab(id),
          }, t(key)))),
        h('section', { style: styles.body },
          error !== null ? h('p', { role: 'alert', style: styles.error }, t('failed') + ': ' + error)
          : data === null ? h('p', { style: styles.muted }, t('loading'))
          : h('pre', { style: styles.pre }, JSON.stringify(data, null, 2))),
      )
    }

    /** 设置页：宿主传 { close }。 */
    function EmailSettings({ t, close }) {
      return h('div', { style: styles.settings },
        h('h2', { style: styles.h2 }, t('panelTitle')),
        h('p', { style: styles.muted }, 'SMTP / IMAP / AI provider 配置占位'),
        h('button', { type: 'button', onClick: close, style: styles.btn }, 'OK'),
      )
    }

    // 最小样式：全部走主题 token，深色模式自动跟随
    const styles = {
      panel: { boxSizing: 'border-box', height: '100%', minHeight: 0,
               display: 'flex', flexDirection: 'column', padding: '20px 24px',
               color: 'var(--dsw-alias-label-primary)', background: 'var(--dsw-alias-bg-base)' },
      nav: { display: 'flex', gap: 4, borderBottom: '0.5px solid var(--dsw-alias-border-l2)',
             marginBottom: 16, flex: 'none' },
      tab: { background: 'transparent', border: 'none', padding: '8px 12px', cursor: 'pointer',
             color: 'var(--dsw-alias-label-secondary)', fontSize: 13 },
      tabActive: { background: 'transparent', border: 'none', padding: '8px 12px', cursor: 'pointer',
                   color: 'var(--dsw-alias-label-primary)', fontSize: 13,
                   boxShadow: 'inset 0 -2px 0 var(--dsw-alias-state-business-primary)' },
      body: { flex: 1, minHeight: 0, overflowY: 'auto' },
      muted: { color: 'var(--dsw-alias-label-tertiary)', fontSize: 13 },
      error: { color: 'var(--dsw-alias-state-error-primary)', fontSize: 13 },
      pre: { fontSize: 12, whiteSpace: 'pre-wrap' },
      settings: { padding: 24 },
      h2: { fontSize: 16, fontWeight: 500, margin: '0 0 8px' },
      btn: { marginTop: 16, padding: '6px 14px', borderRadius: 'var(--dsw-radius-md)',
             border: 'none', cursor: 'pointer',
             background: 'var(--dsw-alias-button-primary-fill)',
             color: 'var(--dsw-alias-label-primary-inverted)' },
    }

    function apply(ctx) {
      const t = ctx.locale ? ctx.locale.bind(NS) : (key) => key

      // 1) 注册运行时字典（disposer 交给 effect）
      if (ctx.locale && typeof ctx.locale.register === 'function') {
        ctx.effect(() => {
          const offs = [
            ctx.locale.register(NS, 'zh', messages.zh),
            ctx.locale.register(NS, 'en', messages.en),
          ]
          return () => offs.forEach((off) => off())
        }, 'email: locale dictionaries')
      }

      // 2) 中央面板（keyed）。inject 是面板唯一的数据入口。
      ctx.slots.inject('main', () => ctx.slots.register({
        name: 'main',
        key: PANEL_ID,
        locale: NS,
        inject: () => ({ t }),
      }, EmailPanel))

      // 3) 侧栏图标入口（list）。id 必须与 main.key 相同；label 供 tooltip/aria。
      ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
        name: 'sidebar.panellist',
        id: PANEL_ID,
        order: 30,
        label: () => t('nav'),
        locale: NS,
      }, EmailIcon))

      // 4) 设置页（可选）
      ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section',
        id: 'email',
        order: 60,
        label: () => t('settings'),
        locale: NS,
        inject: () => ({ t }),
      }, EmailSettings))
    }

    return { inject: ['slots', 'locale'], apply }
  },
})
```

**必须按真实 API 替换/确认的点**：

1. `id: 'dsh-email-automation'` = 包名（加载器用它匹配）。
2. `ctx.slots.register` 的返回值即 disposer；`slots.inject` 的回调返回它即可（样本均如此）。
3. `main.key` 与 `sidebar.panellist.id` 必须一致；`order` 升序（推断）。
4. `EmailPanel` 只收到 `inject()` 的返回值 —— 需要更多数据就在 `inject` 里闭包 `ctx` 取，或让组件自己 `fetch`。
5. `/api/dsh-email/*` 必须与 host `ctx.webServer.register({kind:'exact', path, handler})` 完全一致，且 handler 自己写 `res`。
6. 样式里若用 `--dsw-alias-*` 之外的 token（如 `--dsw-radius-md`、`--dsw-font-family`）需确认存在（第 4 节清单是实测的）。
7. 面板布局、6 个 tab 的组件、设置页表单都是占位，要按邮箱真实数据模型重写。
8. 骨架里 `return { inject: ['slots', 'locale'], apply }` 把 `locale` 当**硬依赖**：没有 locale 服务的组合里插件不会激活。若希望降级可用，改成 `inject: ['slots']`，并在 `apply` 里用属性判空（骨架里已经是这个写法：`ctx.locale ? ctx.locale.bind(NS) : (k) => k`）。

配套 host 半（`lib/index.js`）最小形状：

```js
export const inject = ['webServer']
export function apply(ctx) {
  ctx.inject(['webServer'], (sctx) => {
    sctx.effect(() => {
      const off = sctx.webServer.register({
        kind: 'exact',
        path: '/api/dsh-email/list',
        handler: async (req, res) => {
          // 1) 校验 method / 同源（照抄 free-search isBridgeRequestAllowed）
          // 2) 解析 body，调用业务 service 方法
          // 3) 自己写出 JSON
          res.writeHead(200, { 'content-type': 'application/json' })
          res.end(JSON.stringify({ ok: true, value: { items: [] } }))
        },
      })
      return () => off()
    }, 'email: bridge routes')
  })
}
```

---

## 8. 风险与未验证（必须带进 Phase 1）

| # | 项 | 现状 | 影响 |
|---|---|---|---|
| R1 | **client inspect 全超时**（Slots/Theme/Builtin，5 次） | 拿不到 live `main`/`panellist` 的 catalog、props、occupants | slot 结构依据 asar 源码 + 已装插件；`main` 的 owner props、`panellist` 的 `{size,active}` 已由源码印证，但**未在活页面核对**。做实现前建议在可连页面重跑 `cordis_inspect_query` |
| R2 | 6 个分区没有二级导航 slot | 只能一个 `main` 面板内部做 tab | 若产品要求「6 个独立一级入口」，需要 6 组 `main`+`panellist` 注册（可行但侧栏图标会变多）；**这是产品形态决策，不是技术限制** |
| R3 | `selectPanel` 对未注册 key 抛错 | `layout.selectPanel: main panel "x" is not registered` | 注册顺序/时序不当会抛异常；`main` 必须先于用户点击可用（用 `slots.inject` 等声明，不要裸 `register`） |
| R4 | 组件库不可依赖 | 官方明确禁止 `require('@deepseek-ai/dsh-client-ui-primitives')`，但 3 个已装插件都在用 | 抄 primitives 会随版本炸（组件抛错 → 整个 slot 空白）。邮箱插件**自写控件** |
| R5 | `app.asar` 旁路读取不可复现 | 版本升级后字节偏移失效 | 本文的 C 级证据（slot 声明、token 清单、官方 practices）升级后必须重核；优先改用可连页面的 inspect |
| R6 | Typert `@Remote` 需要生成物 | `ctx.typert.register(TypertContribution)` 要求 `InvocationDescriptor` + `TypertPackageModel`（构建期产物） | 纯 JS bundle 手写成本高、无第三方样本；**不要为邮箱 UI 选 RPC** |
| R7 | 反代前缀 | archive-manager 用 `Rn()` 拼前缀，free-search 直接用绝对 `/api/...` | 若 Web GUI 被反代到子路径，绝对路径会 404。需要确认宿主是否有公开的前缀 helper（**未验证**） |
| R8 | `settings.section` 的 `icon` 字段 | archive-manager 传 `icon:"archive"`，better-sidebar 注释说不支持 | 图标可能不显示；不要把它当作契约 |
| R9 | `dsh.client.immediately` / `external` 语义 | 只在官方模板/文档一句话里出现 | 不写 `immediately` 也有可用先例；`external` 未验证 |
| R10 | 宿主 locale namespace 冲突 | `ctx.locale.register(ns,...)` 用自定 ns | 若 ns 与宿主已有 namespace 同名会覆盖/被覆盖；用插件前缀（如 `email`） |
| R11 | 样式无作用域隔离 | 宿主不隔离插件 CSS | 所有 class 加插件前缀（`dsh-email-*`），避免污染全局 |
| R12 | 面板滚动/尺寸 | 中央列不给内边距与滚动 | 必须 `height:100%; min-height:0; overflow-y:auto`（archive-manager 已踩过） |

---

## 附：本次实际执行的 inspect 调用与结果

| 调用 | 结果 |
|---|---|
| `Slots.listSubTree {root:"main"}` | ❌ 超时 10s（2 次） |
| `Slots.listSubTree {root:"sidebar.panellist"}` | ❌ 超时 10s |
| `Theme.listTokens` | ❌ 超时 10s |
| `Builtin.listBuiltins` | ❌ 超时 10s |
| `Service.listService {service:"webServer"}` | ✅ `register(WebRoute) => disposer`，`WebRoute{kind,path,handler}` |
| `Service.listService {service:"typert"}` | ✅ `register(TypertContribution)`，需生成物 |
| `Service.listService {service:"typertGateway"}` | ✅ `invoke/stream/registerRemoteEvents`，面向生成的 Remote |

超时的处理建议：刷新/重开 `http://127.0.0.1:52452` 页面后重试；若仍超时，说明该页面的 inspect 接收端未挂载，需要宿主侧排查，**不是权限或 sandbox 问题**（本次 read-only 的 host inspect 正常）。
