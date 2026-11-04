# DSH Host 运行时兼容性勘察（Phase 0）

- 目标：为「自动化邮箱管理插件」（IMAP/SMTP 同步 + AI 分析 + 草稿审批 + UI 工作区）确认宿主真实接口。
- 环境：DSH `0.2.1-rc.5`，profile `desktop`，工作目录 `<repo>`。
- 取证方式：`cordis_inspect_query`（Host `Service` / `Config` / `Tool`）、读取已安装插件的真实实现、读取 `app.asar` 内的官方包 README/源码、对运行中的 Web 路由做了一次 HTTP 探测。
- 标记约定：**【验证】** 有 inspect 契约或真实文件证据；**【判断】** 基于已验证事实的工程结论；**【未验证】** 见第 8 节。

---

## 0. 结论速览

| # | 问题 | 结论 | 置信度 |
|---|---|---|---|
| 1 | Tool 注册与 ContentBlock | 可用。`output.render(args, value)` 必须返回 `ContentBlock[]`；文本块写作 `{ type: "text", text }`。**没有 `message` 类型的 ContentBlock**。 | 验证 |
| 2 | 持久化 | `ctx.storageDomain` 可用，但它是「全量载入内存 + 逐条 JSON 文件」的 schema 校验 KV，不是数据库。适合账号/配置/草稿审批等小表，不适合当邮件正文/元数据主库。 | 验证 + 判断 |
| 3 | 凭据 | `ctx.credentials` 可用，但 `$DSH_HOME/.credentials.yaml` **明文落盘、不加密**。`ctx.authorization` 只是「获取凭据的流程注册表」，最终仍写入同一文件，并不更安全。 | 验证 |
| 4 | 本机 HTTP 路由 | 可用。同源 JSON API；`WebRoute = { kind: 'exact'\|'prefix', path, handler(req, res) }`。已在当前 GUI 上实测 `POST /api/dsh-free-search-settings/describe` 返回 200。无内建鉴权。 | 验证 |
| 5 | 后台定时 | `ctx.schedule` **不是 host cron**：它绑定某个 Session，到点以 follow-up 消息投递并可能唤醒模型；`every_seconds >= 60`。常驻轮询应使用 `ctx.timer.interval`（进程内、随插件卸载清理）。DSH 关闭后两者都不运行。 | 验证 |
| 6 | 子进程 | 可拉起常驻 Python 进程（`ctx.subprocess.spawn`），但生命周期绑定 Host：正常卸载/退出会终止并等待退出；崩溃/强杀依赖平台兜底。不是独立守护进程。 | 验证 |
| 7 | `export const Config` | schema 库是 `@deepseek-ai/schemastery`（已装 3.18.5），`import z from "@deepseek-ai/schemastery"`；Loader 在激活时校验。 | 验证 |

**对架构的直接影响**：如果产品要求「DSH 关掉也持续收邮件」，Host 插件做不到；Host 插件只能保证「DSH 运行期间」的同步与后台轮询。见第 5、6 节。

---

## 1. Tool 注册与 ContentBlock

### 1.1 精确契约

`cordis_inspect_query(host / Service, {"service":"tools"})` 返回的 `referencedTypes`：

```ts
interface ToolDefinition extends ToolSchema {
  readonly output: ToolOutputDefinition;
  execute(args: unknown, exec: ToolRunContext): Promise<unknown>;
  projectContent?(exec, result): ContentBlock[] | undefined;
  finalizeContent?(exec, result): ContentBlock[] | undefined;
  timeoutMs?: number;
  isConcurrencySafe?(args: unknown): boolean;
  presentCall?(args: unknown): ToolCallView | undefined;
  presentResult?(args: unknown, result: ToolResult): ToolResultView | undefined;
}

interface ToolOutputDefinition {
  readonly schema: JsonSchemaNode;
  render(args: unknown, value: JsonValue): ContentBlock[];
  presentationMeta?(args: unknown, value: JsonValue): JsonValue;
}

interface ToolSchema {
  deferLoading?: true;
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}
```

`ctx.tools.register(definition)` 返回注销 disposer；同名重复注册会失败，`run_code` 是保留名（inspect `tools.register` 描述）。

### 1.2 ContentBlock 的真实形状

`cordis_inspect_query(host / Service, {"service":"tools"})` 的 `referencedTypes` 给出完整联合：

```ts
interface ContentBlockMap {
  text: TextBlock;                 // { type: 'text'; text: string }
  reasoning: ReasoningBlock;       // { type: 'reasoning'; text: string }
  image: ImageBlock;               // { type: 'image'; attachment: ImageAttachmentRef; offloaded?: true }
  file: FileBlock;                 // { type: 'file'; attachment: FileAttachmentRef }
  'tool-call': ToolCallBlock;      // { type: 'tool-call'; id; name; arguments: string }
  'tool-addition': ToolAdditionBlock; // { type: 'tool-addition'; toolName: string }
  'tool-removal': ToolRemovalBlock;   // { type: 'tool-removal'; toolName: string }
}
type ContentBlock = ContentBlockMap[keyof ContentBlockMap];
```

关键结论：

- **没有 `message` 块。** 工具输出只能是上述 7 种 block 的组合。
- 「一条消息」是 `MessageBase { id: MessageId; content: readonly ContentBlock[]; source }` + role（`UserMessage` 等），不是 ContentBlock。
- 想展示一封邮件：用 `text`（markdown 文本）或 `image`/`file`（需要先经 `ctx.attachments` 存成附件引用，`ImageAttachmentRef` / `FileAttachmentRef`）。**不要**把 IMAP 原始 MIME 当消息块塞进工具结果。
- `execute()` 的返回值必须能作为 `JsonValue`；`output.render(args, value)` 中的 `value` 就是这个返回值。时间、二进制等要先转成字符串/数字。

`dsh-free-search` 的真实写法（`C:\Users\<you>\.dsh\profiles\desktop\node_modules\dsh-free-search\lib\index.js:5228-5241`）：

```js
output: {
  schema: {
    type: "object",
    additionalProperties: false,
    properties: { results: { type: "array", items: { /* ... */ } } },
  },
  render(args, value) {
    // 契约要求直接返回 ContentBlock[]：只返回字符串会让 post-execute 阶段拿到裸字符串崩溃
    return [{ type: "text", text: `Search engine test:\n${lines.join("\n")}` }];
  },
},
```

### 1.3 最小可拷贝代码

```js
// index.js
export const inject = ["tools"];

export function apply(ctx, config) {
  ctx.effect(
    () =>
      ctx.tools.register({
        name: "mail_sync",
        description: "Synchronize the configured mailbox and return a summary.",
        parameters: {
          type: "object",
          additionalProperties: false,
          properties: {
            account: { type: "string", description: "Account id" },
            full: { type: "boolean", description: "Full resync" },
          },
          required: ["account"],
        },
        output: {
          schema: {
            type: "object",
            additionalProperties: false,
            properties: {
              synced: { type: "number" },
              lastUid: { type: "string" },
            },
            required: ["synced", "lastUid"],
          },
          render(args, value) {
            return [
              {
                type: "text",
                text: `Synced ${value.synced} message(s) for ${args.account}; lastUid=${value.lastUid}`,
              },
            ];
          },
        },
        async execute(args, exec) {
          // exec.signal 可取消；exec.agent 可能为 undefined
          return { synced: 0, lastUid: "" };
        },
        timeoutMs: 120_000,
        isConcurrencySafe: () => false,
      }),
    "mail: sync tool",
  );
}
```

注意：

- `output.schema` 是 DSH 的 **受限** `JsonSchemaNode`：支持 `type / oneOf / properties / required / additionalProperties / items / enum / const / description / title / default / examples`；**不支持** `$ref`、`pattern`、`minimum` 等完整 JSON Schema 关键字（inspect `tools` 的 `JsonSchemaNode`）。
- `parameters` 是普通对象，会原样投影给模型（`Record<string, unknown>`）。
- `presentCall`/`presentResult` 只支持内建卡片：`generic` / `terminal` / `diff` / `search` / `read` / `web`（inspect `tools` 的 `ToolCallView` / `ToolResultView`）。**没有邮件卡片**；自定义邮件 UI 必须走 Client 插件的 slot，而不是工具结果卡片。
- `finalizeContent` 可选，且在 `tools/post-execute` 之后执行；若 `render` 已返回 block 数组，不要再用 `finalizeContent` 返回裸字符串（free-search 的注释就踩过这个坑：`index.js:5238-5296`）。

---

## 2. 持久化：`ctx.storage` / `ctx.storageDomain`

### 2.1 精确契约

`cordis_inspect_query(host / Service, {"service":"storageDomain"})`：

```ts
interface Domain<S extends DomainSpec> {
  readonly name: string;
  readonly global: DomainGlobalHandleOf<S>;   // { get(): G; set(value: G): Promise<void> }
  table<N extends keyof S['tables'] & string>(name: N): KvTable<TableKeyOf<S,N>, TableValueOf<S,N>>;
  close(): Promise<void>;
}

interface KvTable<K extends string, V> {
  get(key: K): V | undefined;
  entries(): IterableIterator<[K, V]>;
  keys(): IterableIterator<K>;
  readonly size: number;
  put(key: K, value: V): Promise<void>;
  delete(key: K): Promise<boolean>;
  update(key: K, fn: (current: V) => V): Promise<V>;
}

interface DomainSpec {
  readonly name: string;
  readonly version: number;
  readonly layout?: 'single' | 'per-record';
  readonly compatibleVersions?: readonly number[];
  readonly invalidRecords?: 'backup-and-skip';
  readonly global?: { readonly schema: ZodType<G>; readonly initial: G };
  readonly tables: Record<string, { readonly valueSchema: ZodType<V>; readonly __key?: K }>;
}
```

`ctx.storageDomain.open(spec)` 一次性完成：名称去重（`already-open`）、按 `backend` 路由、要求后端有 `kv` facet、载入并**逐条校验全部已存记录**，失败即整体失败（inspect `storageDomain.open` 描述）。调用方持有句柄，必须自己 `domain.close()`；通常挂在 `ctx.effect` 上。

`dsh-archive-manager` 的真实用法（`...\@michengai\dsh-archive-manager\lib\workspace.js:11,14-20,487-488`）：

```js
import { defineDomain } from "@deepseek-ai/dsh-storage-domain";

const favoriteDomainSpec = defineDomain({
  name: "archive_manager_favorites",
  version: 1,
  tables: {},
  global: { schema: favoriteStateSchema, initial: { favoriteSessionIds: [] } },
});

this.favoriteDomainPromise = this.ctx.storageDomain.open(favoriteDomainSpec).then((domain) => {
  this.ctx.effect(() => () => domain.close(), "archive-manager: 收藏存储关闭");
  return domain;
});
```

同一文件 `workspace.js:18` 的注释说明：宿主运行期只调用 `parse/safeParse`，因此 `favoriteStateSchema` 是自写的 `{ parse, safeParse }` 对象（`lib/archive-organizer.js:13-26`），不是 zod。也就是说：**声明层文档写 zod，但运行层接受任何带 `parse/safeParse` 的校验器**。最稳妥仍是 zod 或自写校验器；schemastery 是否满足未验证。

### 2.2 数据落在哪

【验证】基础组合的配置（`C:\Users\<you>\AppData\Local\Programs\DeepSeek Harness\resources\app.asar\dsh\node_modules\@deepseek-ai\dsh-base\cordis.patch.yml:179-190`）：

```yaml
- id: storage
  name: '@deepseek-ai/dsh-storage'
- id: storage-json
  name: '@deepseek-ai/dsh-storage-json'
  config:
    root: !!js dshHomePath('storages')
- id: storage-domain
  name: '@deepseek-ai/dsh-storage-domain'
  config:
    backend: json
```

JSON 后端的落盘格式（`...\@deepseek-ai\dsh-storage-json\lib\index.js:56-63,178-179`）：

- `single` 布局：`<root>/<单元名>.json`，一个文件包含 `unit/global/tables`。
- `per-record` 布局：`<root>/<单元名>/<表名>/<key>.json`，外加 `global.json`。

实际观察：

- `C:\Users\<you>\.dsh\storages\workspace.json`（`unit.name = "workspace"`，single 布局）
- `C:\Users\<you>\.dsh\storages\session_projcache\sessions\<sessionId>.json`（per-record 布局；base patch 注释 `cordis.patch.yml:192-195` 明确说它是 per-record）

因此【判断】新 domain `email_automation` 会落在：

```text
%USERPROFILE%\.dsh\storages\email_automation.json                 # 若 layout 省略（single）
%USERPROFILE%\.dsh\storages\email_automation\<table>\<key>.json   # 若 layout: 'per-record'
```

`$DSH_HOME` 可被环境变量覆盖；默认 `~/.dsh`。这个目录**不在 profile 下**，是跨 profile 共享的。

### 2.3 能不能替代 SQLite 存邮件元数据

【判断】不能作为邮件主库，理由全部来自已验证实现：

1. **全量内存 + 全量校验**：`open` 时载入并校验所有记录（inspect `storageDomain.open`；README `...\dsh-storage-domain\README.md:88,94`）。邮箱几万封时启动时间/内存都不可接受。
2. **没有查询能力**：`KvTable` 只有 `get/entries/keys/size/put/delete/update`，没有范围扫描、二级索引、join、聚合。按日期/发件人/未读筛选只能全表遍历。
3. **写放大**：single 布局每次写重写整个 JSON 文件；per-record 布局每封邮件一个文件（`storage-json/lib/index.js:160-167` 与 `:56-63`）。十万级文件数会拖垮目录操作和备份。
4. **无跨表事务、无迁移**：README `dsh-storage-domain\README.md:146-148` 明确列为限制；改 schema 必须手工迁移，版本不一致直接 `version-mismatch` 拒绝打开。
5. **单进程可见**：`domain/changed` 是进程内事件；第二个 Host 进程看不到变更（README `:146`）。
6. **单开限制**：同名 domain 同时只能开一个（`already-open`），多窗口/多进程方案要另想办法。

适合放的东西（量小、读多写少、需要 schema 校验）：

- 账号配置（IMAP host/port/user、TLS 选项、同步间隔）
- 同步游标（每个文件夹的 `lastUid` / `uidValidity`）
- 草稿审批状态、AI 分析结果缓存（单条）
- 规则/白名单

不适合：邮件正文、原始 MIME、附件 bytes、需要按条件检索的元数据表。

如果确实需要 SQLite：DSH 自带 `@deepseek-ai/dsh-session-query-sqlite`（`Config.listConfigs` 的 `include:session-query-sqlite`），但它是**会话搜索**的专用组件，不是给第三方插件用的通用 DB；插件自己要引入 SQLite 依赖与原生模块，安装/打包/Windows 编译风险未评估（第 8 节）。

### 2.4 最小可拷贝代码

```js
import { defineDomain } from "@deepseek-ai/dsh-storage-domain";

// 校验器：可换成 zod；下面这种 { parse, safeParse } 形态已被 archive-manager 验证可用
const accountSchema = {
  parse(v) {
    if (!v || typeof v !== "object" || typeof v.id !== "string") throw new TypeError("bad account");
    return { id: v.id, host: String(v.host ?? ""), user: String(v.user ?? "") };
  },
  safeParse(v) {
    try { return { success: true, data: this.parse(v) }; }
    catch (error) { return { success: false, error }; }
  },
};

const syncStateSchema = {
  parse(v) {
    if (!v || typeof v !== "object") throw new TypeError("bad state");
    return { lastSyncAt: v.lastSyncAt ?? null };
  },
  safeParse(v) {
    try { return { success: true, data: this.parse(v) }; }
    catch (error) { return { success: false, error }; }
  },
};

const mailSpec = defineDomain({
  name: "email_automation",
  version: 1,
  // tables 的声明形态见 inspect DomainTableSpec；README 用 domainTable(schema)，但本次未实测该 helper
  tables: { accounts: { valueSchema: accountSchema } },
  global: { schema: syncStateSchema, initial: { lastSyncAt: null } },
});

export const inject = ["storageDomain"];

export function apply(ctx) {
  ctx.effect(() => {
    let closed = false;
    void ctx.storageDomain
      .open(mailSpec)
      .then((opened) => {
        if (closed) { void opened.close(); return; }
        ctx.effect(() => () => opened.close(), "mail: close domain");
      })
      .catch((error) => { /* log and disable mail features */ });
    return () => { closed = true; };
  }, "mail: open domain");
}
```

> 说明：`tables` 的运行期写法（`{ valueSchema }` vs `domainTable(schema)`）未在已安装插件中找到真实用例；`domainTable` 只出现在官方 README 示例。Phase 1 应先用一个最小域实测。

---

## 3. 凭据：`ctx.credentials` 与 `ctx.authorization`

### 3.1 契约

`cordis_inspect_query(host / Service, {"service":"credentials"})`：

```ts
type CredentialRef = Branded<'CredentialRef'>;   // 运行时就是 string
type CredentialKey = Branded<'CredentialKey'>;

interface CredentialInfo { configured: boolean; source?: string; writable: boolean; }
interface ResolvedCredential { value: string; source: string; }

abstract resolve(ref: CredentialRef): Promise<ResolvedCredential | undefined>;
abstract describe(ref: CredentialRef): Promise<CredentialInfo>;
abstract set(ref: CredentialRef, value: string): Promise<void>;
abstract unset(ref: CredentialRef): Promise<void>;

abstract readRecord(key: CredentialKey): Promise<CredentialRecord | undefined>;
abstract describeRecord(key: CredentialKey): Promise<CredentialRecordInfo>;
abstract listRecords(): Promise<readonly CredentialRecordEntry[]>;
abstract modifyRecord(key: CredentialKey, mutate): Promise<CredentialRecord | undefined>;
abstract deleteRecord(key: CredentialKey): Promise<void>;
```

两套空间的含义（inspect 描述）：

- `CredentialRef`：回答「某个环境变量名背后是什么值」。查找顺序是 **托管文件 → 启动环境 → 项目 `.env` → `$DSH_HOME/.env`**。空值视为未配置。
- `CredentialKey`：回答「这个插件为这个 id 持有什么凭据」。只能是记录（`api-key` / `grant`），没有环境层；唯一写路径是 `modifyRecord`（串行读改写，适合 token refresh）。

真实用法参考：`dsh-free-search` 的 `KEY_REF_MAP`（`...\dsh-free-search\lib\index.js:3665-3688`）与 `credentials.set/describe` 调用（`:3459-3505`）。它把 `DEEPSEEK_API_KEY` 这类普通字符串直接当 ref 用，不需要 import 任何 helper。

### 3.2 存在哪里、是否加密

【验证】`...\@deepseek-ai\dsh-credentials-local\README.md`：

- `:12` —— "keeps API keys and other secrets in a private file under your harness home"；"Only your OS user can read the file, but agent tool processes run as that same user, so this store cannot isolate secrets from the agent."
- `:44` —— 默认路径 `<harness home>/.credentials.yaml`（`$DSH_HOME` 或 `~/.dsh`）。
- `:84-109` —— 文件是版本化 YAML，`refs` 存环境变量名到值的映射，示例直接写 `DEEPSEEK_API_KEY: sk-…`；`records` 存 `<owner>/<id>` 记录，`grant` payload 原样保存。
- `:113-115` —— POSIX 上文件权限仅 owner 可读（其他用户可读会拒绝加载）；**Windows 没有 mode 可检查，跳过该检查**；agent 与用户同权限。

本机实测（不回显密钥）：`C:\Users\<you>\.dsh\.credentials.yaml:19` 的 `DEEPSEEK_API_KEY` 值为 35 字符、以 `sk-` 开头；文件里还有一个 43 字符的 `secret`。这与 README 的明文示例一致。**结论：不加密，明文 YAML 落盘。**

### 3.3 `ctx.authorization` 是不是更合适

不是「更安全的存储」，只是「获取凭据的流程注册表」（inspect `authorization`）：

```ts
interface AuthorizationFlow {
  key: CredentialKey;
  label: string;
  methods: readonly AuthorizationMethod[];
  run(session: AuthorizationSession): Promise<void>;
}
interface AuthorizationSession {
  readonly method: string;
  readonly signal: AbortSignal;
  commit(record: CredentialRecord): Promise<void>;
  notify(notice): void;
  prompt(prompt): Promise<string>;   // kind: 'text' | 'secret' | 'select'
}
```

`begin({ key, method?, interaction, signal? })` 会跑一次交互式授权；`run` 里通过 `session.prompt({ kind: "secret" })` 向用户要密码，再 `session.commit({ kind: "grant", payload })`。`commit` 最终仍写进同一个 `credentials` 存储（同一 YAML）。所以：

- 对 **OAuth2 / XOAUTH2**（Gmail、Microsoft 365）有价值：拿授权码、存 refresh token、`modifyRecord` 刷新。
- 对 **IMAP 用户名/密码** 没有安全增益；只是多了一层交互 UI。
- 没有 flow 就 `begin` 会 `NO_FLOW`；同一 key 同时只能有一次尝试（`ALREADY_IN_FLIGHT`）。

### 3.4 判断

- 功能上：`ctx.credentials.set("EMAIL_IMAP_PASSWORD", password)` 能存、能取、能跨重启；`describe` 不会回显值。
- 安全上：**不能称为「安全存储」**。任何以当前 Windows 用户身份运行的进程/agent 工具都能读该文件；备份、同步盘、误提交也会带走明文。
- 建议：优先用授权码/应用专用密码；在 UI 与文档中明确「明文存储在 `~/.dsh/.credentials.yaml`」；高价值场景考虑插件自管 Windows DPAPI/Credential Manager（需另写实现，未验证）。
- `ctx.credentials.describe(ref).writable` 可用来判断当前是否可写（托管文件总是可写，环境层不可写）。

### 3.5 最小可拷贝代码

```js
export const inject = ["credentials"];

export function apply(ctx) {
  const ref = "EMAIL_IMAP_PASSWORD"; // 运行时就是普通字符串

  async function savePassword(password) {
    if (typeof password !== "string" || password.length === 0) throw new Error("empty password");
    await ctx.credentials.set(ref, password);
  }

  async function loadPassword() {
    const info = await ctx.credentials.describe(ref);
    if (!info.configured) return undefined;
    const resolved = await ctx.credentials.resolve(ref); // { value, source } | undefined
    return resolved?.value;
  }
}
```

---

## 4. 本机 HTTP 路由：`ctx.webServer`

### 4.1 契约

`cordis_inspect_query(host / Service, {"service":"webServer"})`：

```ts
type WebRouteKind = 'exact' | 'prefix';

interface WebRoute {
  kind: WebRouteKind;
  path: string;
  handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>;
}

register(route: WebRoute): () => void;
registerUpgrade(route: WebUpgradeRoute): () => void;
registerFallback(handler: WebRoute['handler']): () => void;
getOrigin(): string;
```

说明：

- `req/res` 是 **Node 的** `node:http` 对象（`res.writeHead()` / `res.end()`）。inspect 的 `referencedTypes` 里另有一个同名的 RPC `ServerResponse`（`{ type: 'server-response', rpcId, result }`），那是 Typert 的类型，不是这里；以 `dsh-free-search` 的 `writeJson` 实现为准（`...\dsh-free-search\lib\index.js:3321-3325`）。
- 匹配顺序：exact 全表 → 最长 prefix → fallback（`...\dsh-host-webserver\README.md:61`）。
- 同一 `(kind, path)` 重复注册会抛错；返回的 disposer 负责移除。
- **没有服务器级鉴权/同源策略**；README `:130` 明确说 "No server-wide authentication or origin policy — route owners ... enforce their own request policy"。绑定非 loopback 地址时，未受保护的路由会暴露给该网络。

### 4.2 实测：当前 desktop GUI 上路由可达

GUI URL：`http://127.0.0.1:52452`。在 Windows 侧执行：

```text
curl.exe -sS -m 10 -X POST http://127.0.0.1:52452/api/dsh-free-search-settings/describe -H "content-type: application/json" -d "{}"
```

结果（2026-11-04）：

```text
HTTP/1.1 200 OK
content-type: application/json; charset=utf-8
referrer-policy: no-referrer
...
{"ok":true,"value":{"namespaces":[{"ns":"web-search-free", ...}],"writable":true}}
```

这条路由来自 `dsh-free-search`（`...\dsh-free-search\lib\index.js:3523-3527` 注册，`:238` 定义前缀），说明 **Host 插件注册的 `/api/*` 路由在当前 desktop 运行时是真实可达的**。

注意：WSL 内的 `curl http://127.0.0.1:52452` 连不上（WSL2 独立网络命名空间），必须用 `curl.exe` 或浏览器。

### 4.3 前端怎么访问

`dsh-free-search` 的做法（可作为模板）：

- 包声明 Client 半部：`package.json` 的 `dsh.client = { inject: [...], platform: "web" }`，并通过 `exports["./client"] = "./lib/client.js"` 暴露（`...\dsh-free-search\package.json:102-112`）。
- Client 代码用**同源相对路径** `fetch`：`...\dsh-free-search\lib\client.js:286` 定义 `/api/dsh-free-search-settings`，`:688-693` 发起 `POST` 并 `response.json()`。
- Host 端自己做同源/loopback 防护：`isBridgeRequestAllowed` 检查 `Host`、`sec-fetch-site: cross-site`、`Origin`、`socket.remoteAddress`（`...\dsh-free-search\lib\index.js:3294-3319`）；请求体上限 64 KiB（`:3259`）。
- 不要用 Host 返回 HTML + iframe：官方实践明确说 iframe 拿不到主题 token、light/dark 切换和 `ctx.locale`，应把页面做成 Client 插件 slot 里的 React 组件（`...\skills\cordis-plugin-development\references\practices.md:33`）。

### 4.4 最小可拷贝代码

```js
export const inject = ["webServer"];

export function apply(ctx) {
  ctx.effect(
    () =>
      ctx.webServer.register({
        kind: "exact",
        path: "/api/email-automation/sync",
        handler: async (req, res) => {
          // 最小同源防护：loopback Host + 同源 Origin（完整做法见 free-search 的 isBridgeRequestAllowed）
          const host = req.headers.host ?? "";
          if (!/^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(host)) {
            res.writeHead(403, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: "forbidden host" }));
            return;
          }
          if (req.headers["sec-fetch-site"] === "cross-site") {
            res.writeHead(403, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: "cross-site request" }));
            return;
          }
          const origin = req.headers.origin;
          if (origin !== undefined) {
            let originHost;
            try { originHost = new URL(origin).host; } catch { originHost = ""; }
            if (originHost !== host) {
              res.writeHead(403, { "content-type": "application/json" });
              res.end(JSON.stringify({ error: "forbidden origin" }));
              return;
            }
          }
          if (req.method !== "POST") {
            res.writeHead(405, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: "method not allowed" }));
            return;
          }

          const chunks = [];
          let size = 0;
          for await (const chunk of req) {
            size += chunk.length;
            if (size > 64 * 1024) {
              res.writeHead(413, { "content-type": "application/json" });
              res.end(JSON.stringify({ error: "body too large" }));
              return;
            }
            chunks.push(chunk);
          }
          let body;
          try { body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"); }
          catch {
            res.writeHead(400, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: "bad json" }));
            return;
          }

          // 交给 Mail Core 处理
          res.writeHead(200, { "content-type": "application/json; charset=utf-8" });
          res.end(JSON.stringify({ ok: true, account: body.account ?? null }));
        },
      }),
    "mail: http bridge",
  );
}
```

---

## 5. 后台定时：`ctx.schedule` 与 `ctx.timer`

### 5.1 `ctx.schedule` 的真实语义

`cordis_inspect_query(host / Service, {"service":"schedule"})`：

```ts
async create(sessionId: SessionId, request: ScheduleCreateRequest, signal?: AbortSignal): Promise<ScheduleRecord>;
@Remote('list') async list(request: ScheduleListRequest): Promise<ScheduleRecord[]>;
@Remote('catalog') async catalog(): Promise<ScheduleCatalogEntry[]>;
@Remote('history') async history(request): Promise<ScheduleDeliveryHistoryResult>;
@Remote('delete') async delete(request, signal?): Promise<ScheduleDeleteResult>;
@Remote('update') async update(request, signal?): Promise<ScheduleUpdateResult>;

interface ScheduleCreateRequest {
  prompt: string;
  title: string;
  after_seconds?: number;
  at?: AtInput;
  every_seconds?: number;
  daily?: DailyInput;
  weekly?: WeeklyInput;
  cron?: CronInput;
}
```

官方 README（`...\app.asar\dsh\node_modules\@deepseek-ai\dsh-schedule\README.md`）：

- `:12` —— "delivers ... reminders as follow-up messages in their original Session"；Host 会为到期投递恢复冷 Session。
- `:26` —— "Schedule cannot be mounted alone in a headless or SDK-only composition"；投递依赖 Host Web Session controller 和 Session persistence。
- `:34` —— `every_seconds` 最小 60 秒，初始对齐创建时间。
- `:47` —— 提醒绑定调用方 Agent 的 Session；`catalog` 能看到所有 Session 的任务，但 `list/update/delete` 都要显式 Session id。
- `:153` —— "**The Host must be running to deliver reminders.**"。
- `:63,73` —— 任务存在一个 version-1 的 `schedule` domain（whole-unit 布局，按 JSON 后端规则通常落盘为 `storages/schedule.json`），由 Host 的一个 timer 驱动。

**判断：不能用它做「每 N 分钟静默轮询邮箱」。**

1. 它必须绑定 Session，没有「host 全局任务」入口。
2. 到点投递的是 user-role follow-up 消息，会进入会话并**可能唤醒模型**，每 N 分钟消耗一次模型调用；这不是后台同步。
3. DSH 关闭后不投递；`create` 在 headless 组合下不可用。
4. 最小 60 秒，且只有 6 种 selector。

### 5.2 `ctx.timer.interval` 的语义

`cordis_inspect_query(host / Service, {"service":"timer"})`：

```ts
timeout(callback: () => void, delay: number): () => void;
interval(callback: () => void, delay: number): () => void;
interval<R = any>(delay: number): AsyncIterableIterator<void, R, void>;
```

官方 README（`...\app.asar\dsh\node_modules\@deepseek-ai\cordis-plugin-timer\README.md:21-22,30`）：

> "Timer handles are registered on the current fiber, so they are cleared automatically when the plugin that created them is disposed."

也就是说：

- 它是**进程内**定时器，生命周期 = 创建它的 Cordis fiber（插件）。插件卸载/DSH 退出时自动清理。
- 不持久化、不补跑：DSH 关闭期间不会执行；没有「错过一次后补跑」语义（这是 Node timer 的常规行为，未单独查实现）。
- 与 `ctx.effect(() => ctx.timer.interval(...))` 搭配即可获得卸载清理。

### 5.3 推荐写法

```js
export const inject = ["timer", "subprocess"];

export function apply(ctx, config) {
  let running = false;

  async function syncOnce() {
    if (running) return;          // 防重入：上一轮未结束就跳过
    running = true;
    try {
      // await mailCore.sync();
    } finally {
      running = false;
    }
  }

  const intervalMs = Math.max(60_000, Number(config.pollIntervalMs) || 300_000);
  ctx.effect(() => ctx.timer.interval(() => { void syncOnce(); }, intervalMs), "mail: poll timer");
}
```

**结论**：Host 插件能做「DSH 运行期间的每 N 分钟轮询」；做不到「DSH 关闭后仍在收邮件」。若产品必须后者，需要 Windows 服务/计划任务等外部常驻进程，这已经超出 Host 插件生命周期（见第 8 节风险）。

---

## 6. 子进程：`ctx.subprocess`

### 6.1 契约

`cordis_inspect_query(host / Service, {"service":"subprocess"})`：

```ts
interface SubprocessSpawnSpec {
  argv: readonly string[];
  cwd: string;
  stdio: SubprocessStdio;
  graceMs: number;
  signal?: AbortSignal | undefined;
  env?: NodeJS.ProcessEnv | undefined;
}

interface SubprocessStdio {
  stdin: 'ignore' | 'pipe' | { readonly data: string };
  stdout: 'pipe' | 'inherit' | { maxBytes: number; spill?: { maxBytes: number } };
  stderr: SubprocessOutputMode;
  control?: 'pipe';
}

interface SubprocessHandle {
  readonly stdin: Writable | undefined;
  readonly stdout: Readable | undefined;
  readonly stderr: Readable | undefined;
  readonly control: Duplex | undefined;
  readonly collected: { stdout?: SubprocessOutputReader; stderr?: SubprocessOutputReader };
  readonly done: Promise<SubprocessOutcome>;   // { exitCode, signal }
  terminate(): void;
  waitForExit(signal?: AbortSignal): Promise<boolean>;
}
```

`spawn` 同步返回句柄；参数非法（预取消、argv/cwd/env/grace 无效）会**同步抛错**（inspect `spawn` 描述）。`resolveExecutable(command, env?, signal?)` 负责把 `python` 解析成绝对路径；裸名走 provider 的 **scrubbed PATH**，相对路径含分隔符会被拒绝。

### 6.2 生命周期：DSH 关闭时子进程会死吗

官方 README（`...\app.asar\dsh\node_modules\@deepseek-ai\dsh-subprocess-local\README.md`）：

- `:12` —— "children start from a scrubbed environment, and disposal terminates and joins every selected range or session."
- `:28,43` —— Windows 非终端子进程用私有 Job runner 启动，`windowsHide`；**Job 是 kill-on-close**（`:86`）。
- `:64-66` —— "Normal disposal terminates every running managed range and terminal session and awaits quiescence."；"During a JavaScript-observable host exit ... kills each Windows runner so its sole Job handle closes"；"Unhandled SIGTERM/SIGINT/SIGHUP, SIGKILL, fatal OOM, native crashes, and power loss need an external supervisor."
- `:111` —— 安全不变式：清理失败会保持可观察，不会静默。

**结论**：

- 正常卸载插件、正常退出 DSH（`process.exit()` / 未捕获异常 / 未处理 rejection）→ 子进程及其 Job 内后代会被终止。
- Windows 上 DSH 进程被强杀/崩溃时，Job 的最后一个句柄随进程关闭 → 子进程大概率也会被内核杀掉（这是 kill-on-close Job 的语义）。
- 但 POSIX 回退路径可能漏掉「逃逸的后代」；断电/内核崩溃也没有保证。
- **它不是独立守护进程**：想让 Mail Core 在 DSH 关闭后继续跑，必须放到外部服务（Windows Service / Task Scheduler / systemd），由插件按需连接，而不是用 `ctx.subprocess` 常驻。

### 6.3 环境变量与路径

`subprocess-local` 从 **scrubbed environment** 启动子进程，`resolveExecutable` 也只用 scrubbed PATH + 显式 `env` 覆盖（README `:12,32,41`）。因此：

- `cwd` 用绝对路径（相对路径含分隔符会被拒绝）。
- `argv[0]` 先用 `resolveExecutable("python")` 拿到绝对路径，或直接给绝对路径。
- `env` 显式传 `{ ...process.env, PYTHONUNBUFFERED: "1", ... }`；不要假设继承。
- `stdio` 必填；要读输出用 `{ maxBytes, spill?: { maxBytes } }`，读取是**按偏移、非消费式**（README `:47`）。

### 6.4 最小可拷贝代码

```js
export const inject = ["subprocess"];

export function apply(ctx, config) {
  ctx.effect(() => {
    let handle;
    let disposed = false;

    void (async () => {
      const python = await ctx.subprocess.resolveExecutable(config.python ?? "python");
      if (disposed) return;
      handle = ctx.subprocess.spawn({
        argv: [python, "-u", "-m", "mail_core", "--stdio"],
        cwd: config.coreDir,                       // 绝对路径
        stdio: {
          stdin: "pipe",
          stdout: { maxBytes: 1024 * 1024, spill: { maxBytes: 64 * 1024 * 1024 } },
          stderr: { maxBytes: 1024 * 1024, spill: { maxBytes: 16 * 1024 * 1024 } },
        },
        graceMs: 5000,
        env: { ...process.env, PYTHONUNBUFFERED: "1", MAIL_CORE_HOME: config.dataDir },
      });
      handle.done.then(
        (outcome) => { /* exitCode / signal */ },
        (error) => { /* spawn 或 provider 失败 */ },
      );
    })();

    return () => {
      disposed = true;
      handle?.terminate();
    };
  }, "mail: core process");
}
```

注意：

- `terminate()` 只是发起终止；要等真正退出用 `await handle.waitForExit()`。
- 句柄的 `collected.stdout.readFrom(offset)` 是非消费式；`stdout` 是原始 `Readable`，不要和 collect 同时读同一流（除非你确实要双读）。
- 子进程使用的是 Host 的 `ctx.subprocess` 服务，不是 agent 的沙箱 bash；它与 DSH 的 sandbox/permission 策略如何交互**未验证**（第 8 节）。

---

## 7. `export const Config` 与 Loader

### 7.1 schema 库与写法

【验证】`dsh-free-search` 的顶部与定义：

- `...\dsh-free-search\lib\index.js:3` —— `import z from "@deepseek-ai/schemastery";`
- `:3690-3696` —— 注释明确说 rc.1 起可编辑字段用 `.volatile()`；Loader 把 live 引用交给 `apply`，`resolveConfig` 解包，profile 编辑不重挂插件。
- `:3694-3696` ——

```js
const Config = z.object({
  provider: z.string().default("bing").volatile(),
  disabledEngines: z.array(z.string()).default([]).volatile(),
  // ...
});

export { Config };   // :5795 的导出块
```

已安装版本：`...\node_modules\@deepseek-ai\schemastery\package.json` → `3.18.5`。

`Config` 的字段能力：

- `.default(value)`：缺省值，会出现在 Loader 投影的 JSON Schema 里。
- `.volatile()`：标记为「可由设置页实时修改、不重挂插件」；投影里是 `x-cordis.volatile: true`。
- `.role("secret")`：标记为密钥字段；投影里是 `x-cordis.role: "secret"`，设置桥接响应不会回显值，只给出 `{ path, set }`（`...\dsh-free-search\lib\index.js:3350-3352`）。free-search 的 `exaApiKey` 等就是这样声明的（`index.js:3732-3748`）。
- 复杂结构用 `z.object / z.array / z.string / z.number / z.boolean`。

### 7.2 Loader 如何校验

【验证】`...\skills\cordis-plugin-development\references\host-plugin.md:59-64`：

> "`export function apply(ctx, config) {}` with optional `export const inject = [...]` and `export const Config`."
> "A plugin that declares `Config` validates the row's `config` at activation; query `Config.listConfigs` for an installed plugin's schema before writing its `config`, and follow `$defs` references in the returned document."

【验证】`cordis_inspect_query(host / Config, {"name":"dsh-free-search"})` 返回：

```json
{
  "id": "include:web-search-free",
  "name": "dsh-free-search",
  "status": "schema",
  "packageDir": "C:\\Users\\<you>\\.dsh\\profiles\\desktop\\node_modules\\dsh-free-search",
  "acceptsMissing": true,
  "schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": { "loaderExpression": { ... } },
    "anyOf": [ { "type": ["object","null"], "properties": { "provider": { "default": "bing", "x-cordis": { "volatile": true }, ... } } }, ... ]
  }
}
```

说明 Loader 把 schemastery 投影成 JSON Schema（含 `x-cordis.volatile` / `x-cordis.role`），并允许缺省配置（`acceptsMissing: true`）。没有 `Config` 的插件（inspect 里 `status: "absent"` 的条目）不会做 schema 校验。

### 7.3 最小可拷贝代码

```js
import z from "@deepseek-ai/schemastery";

export const name = "email-automation";
export const inject = ["tools", "storageDomain", "credentials", "webServer", "timer"];

export const Config = z.object({
  accounts: z.array(z.string()).default([]).volatile(),
  pollIntervalMs: z.number().default(300_000).volatile(),
  imapHost: z.string().default("").volatile(),
  imapPort: z.number().default(993).volatile(),
  imapUser: z.string().default("").volatile(),
  imapPasswordEnv: z.string().default("EMAIL_IMAP_PASSWORD").volatile(),
  python: z.string().default("python").volatile(),
  coreDir: z.string().default("").volatile(),
});
```

`Config` 的字段会写进 profile 的 `cordis.patch.yml` 对应条目；用户修改由 Loader 校验后再交给 `apply`。

---

## 8. 未验证 / 风险

按对邮件插件的影响排序。

1. **「DSH 关闭后继续收邮件」不成立（最大的功能风险）**
   - `ctx.timer` 是进程内 timer；`ctx.schedule` 需要 Host 运行且会唤醒 Session；`ctx.subprocess` 的子进程随 Host 退出被终止（第 5、6 节）。
   - 若产品要求 7×24 同步，必须引入外部常驻服务（Windows Service / Task Scheduler / systemd），插件只做「连接 + 管理」。这部分本阶段未设计、未验证。

2. **凭据明文（最大的安全风险）**
   - `~/.dsh/.credentials.yaml` 明文 YAML；Windows 上不做权限检查；agent 与用户同权限（`dsh-credentials-local/README.md:12,113-115`）。
   - IMAP 密码/Refresh Token 一旦写入，任何同用户进程可读。建议应用专用密码 + UI 明示；DPAPI/Credential Manager 方案未验证。

3. **邮件规模下的存储选择**
   - `storageDomain` 的全量载入/逐条文件/无索引（第 2 节）不适合邮件主库。
   - 自带 SQLite 组件是会话搜索专用；插件自引 SQLite 的原生模块安装（Windows、Electron ABI、pnpm）未评估。

4. **Web 路由在 desktop/Electron 渲染层的实际可达性**
   - `/api/*` 路由已用 `curl.exe` 证明在 `http://127.0.0.1:52452` 可达；但 `dsh-host-webserver/README.md:12` 说 "Electron loads dist over `file://` and carries fetch over an IPC bridge"。
   - 当前 desktop 运行时显然是 HTTP 形态；**渲染层里的相对 `fetch` 是否走 HTTP 还是 IPC bridge，未在 UI 中验证**。Phase 1 应打开一次插件设置页确认。

5. **`domainTable` / tables 的运行期写法**
   - 官方 README 用 `defineDomain({ tables: { x: domainTable(schema) } })`；已安装插件只有 `tables: {}` + `global` 的真实用例。
   - 具体是 `domainTable` 还是 `{ valueSchema }`，以及 key 是否必须字符串，需最小插件实测。

6. **子进程与 sandbox/permission 的关系**
   - `ctx.subprocess` 是 Host seam，`dsh-subprocess-local` 直接在本机执行；它是否受 `ctx.sandbox` / approval 约束，未读实现、未实测。
   - 若不受约束，插件拉起 Python 等于绕过 agent 的 workspace-write；需要在设计上自我约束（固定 argv、绝对 cwd、白名单）。

7. **`ctx.schedule` 的模型成本**
   - 到期投递是 follow-up 消息，可能触发模型调用；如果误用为邮件轮询，会持续产生 token 费用。本阶段未量化。

8. **域数据的跨 profile 可见性**
   - 落盘在 `$DSH_HOME/storages`，不在 profile 目录；多个 profile 共用同一份数据。对「每个 profile 独立邮箱配置」的产品假设是一个冲突点，需明确隔离键。

9. **插件依赖解析**
   - `dsh-free-search` 把 `@deepseek-ai/schemastery` 放在 `dependencies`，把 `@deepseek-ai/dsh-tools` 等放在 **optional peerDependencies**；`@michengai/dsh-archive-manager` 把 `@deepseek-ai/dsh-*` 放 peerDependencies。
   - 本阶段按要求未安装、未改 profile，因此打包/安装时的依赖解析（尤其 `@deepseek-ai/dsh-tools` 的 import 能否在 profile 安装后解析）未验证。

10. **`ContentBlock` 的 `image` / `file` 展示**
    - 展示附件需要 `ctx.attachments.saveImages/saveFile` 产生 `ImageAttachmentRef` / `FileAttachmentRef`；附件存储位置、大小限制、清理策略未评估。

---

## 9. 证据索引

### 9.1 inspect 查询

| 查询 | 用途 |
|---|---|
| `cordis_inspect_query(host / Service, {"service":"tools"})` | ToolDefinition / ToolOutputDefinition / ContentBlockMap / JsonSchemaNode |
| `... {"service":"storage"}`、`{"service":"storageDomain"}` | Domain / KvTable / DomainSpec / open 语义 |
| `... {"service":"credentials"}`、`{"service":"authorization"}` | CredentialRef / CredentialKey / set/resolve/describe；AuthorizationFlow |
| `... {"service":"webServer"}` | WebRoute / register / index 注入 |
| `... {"service":"schedule"}` | ScheduleCreateRequest / ScheduleRecord / 生命周期 |
| `... {"service":"timer"}` | interval / timeout |
| `... {"service":"subprocess"}` | SubprocessSpawnSpec / SubprocessHandle |
| `cordis_inspect_query(host / Config, {"name":"dsh-free-search"})` | 已挂载插件 Config 的 JSON Schema 投影 |
| `cordis_inspect_query(host / Config, {"entry":"include:storage-json"})` | storage-json 的 `root` 必填字段 |
| `cordis_inspect_query(host / Config, {"entry":"include:credentials"})` | credentials-local 的 `path` / `dshHome` / `watch` |
| `cordis_inspect_query(host / Tool, {})` | `schedule_create` 的 `every_seconds >= 60` 等真实工具 schema |

### 9.2 真实文件

| 路径 | 关键行 |
|---|---|
| `C:\Users\<you>\.dsh\profiles\desktop\node_modules\dsh-free-search\lib\index.js` | 5180-5304 工具注册；5228-5241 render；3294-3319 同源守卫；3321-3341 JSON 读写；3357-3580 路由表；3665-3688 ref 映射；3690-3696 Config；4614-4624 路由注册 |
| `...\dsh-free-search\lib\client.js` | 286 前缀；688-693 相对 fetch |
| `...\dsh-free-search\package.json` | 102-112 `dsh.client` / `platform: web` |
| `C:\Users\<you>\.dsh\profiles\desktop\node_modules\@michengai\dsh-archive-manager\lib\workspace.js` | 11 defineDomain；14-20 spec；487-488 open + effect close |
| `...\@michengai\dsh-archive-manager\lib\archive-organizer.js` | 13-26 `{parse,safeParse}` 校验器 |
| `C:\Users\<you>\AppData\Local\Programs\DeepSeek Harness\resources\app.asar\dsh\node_modules\@deepseek-ai\dsh-base\cordis.patch.yml` | 24-25 timer；113-126 credentials；179-190 storage；234-235 subprocess |
| `...\@deepseek-ai\dsh-storage-json\lib\index.js` | 56-63 两种布局；160-167 single 写放大；178-179 `<root>/<name>.json` |
| `...\@deepseek-ai\dsh-storage-domain\README.md` | 36-58 声明/打开；88-90 内存权威；146-148 限制 |
| `...\@deepseek-ai\dsh-credentials-local\README.md` | 12 明文/同用户；44 默认路径；84-109 YAML；113-115 权限 |
| `...\@deepseek-ai\dsh-host-webserver\README.md` | 12 Electron 说明；59-65 路由/fallback；124-131 无鉴权 |
| `...\@deepseek-ai\dsh-schedule\README.md` | 12/26/34/47/63/73/153 语义与限制 |
| `...\@deepseek-ai\dsh-subprocess-local\README.md` | 12/28/41-43/64-66/86/107/111 生命周期与 Windows Job |
| `...\@deepseek-ai\cordis-plugin-timer\README.md` | 21-22 fiber 清理；30 interval |
| `...\dsh-agent-preset\skills\cordis-plugin-development\references\host-plugin.md` | 59-64 Host 导出与 Config 校验 |
| `...\references\practices.md` | 21 插件数据应放 storage；29 timer 清理；33 不要 iframe |

### 9.3 本机观察

- `C:\Users\<you>\.dsh\storages\workspace.json`：single 布局实际样例（`unit.name = "workspace"`）。
- `C:\Users\<you>\.dsh\storages\session_projcache\sessions\*.json`：per-record 布局实际样例。
- `C:\Users\<you>\.dsh\.credentials.yaml`：`refs.DEEPSEEK_API_KEY` 为 35 字符、`sk-` 前缀（明文）。
- `curl.exe -X POST http://127.0.0.1:52452/api/dsh-free-search-settings/describe`：200 + JSON（2026-11-04）。
