# 待跟进 + 邮件总结：实施方案（Phase 4 前置）

> 状态：**待用户确认**，未开始实现
> 目标：新邮件自动进入待跟进 → 用户点「总结」交给 Agent → 总结按分类归档 → 有历史总结时先协商 → 处理完自动标记已处理

---

## 1. 目标流程

```
邮件同步进来
   ↓
自动登记为「待跟进」事项（待处理）
   ↓
用户在待跟进里点「总结」
   ↓
打开/复用一条 DSH 会话，把该邮件交给 Agent
   ↓
Agent 先拉上下文：本封正文 + 同线程 + 相关邮件 + 历史总结 + 已下载附件
   ↓
   ├─ 没有历史总结 → 直接总结
   └─ 有历史总结   → 列出历史结论并询问用户如何合并/取舍
   ↓
Agent 调用工具写入数据库（带分类）
   ↓
用户要求的事项处理完后 → Agent 标记该邮件「已处理」
   ↓
待跟进列表里它从「待处理」移到「已处理」，摘要可回看
```

---

## 2. 数据模型（schema v9）

### 2.1 `messages` 增两列

```sql
ALTER TABLE messages ADD COLUMN todo_state TEXT NOT NULL DEFAULT '';
ALTER TABLE messages ADD COLUMN todo_updated_at TEXT NOT NULL DEFAULT '';
-- todo_state: ''（不跟踪）| 'pending'（待处理）| 'done'（已处理）
```

**登记规则**（待确认项 A）：
- 只对 **INBOX** 的邮件登记
- 只登记**最近 7 天收到**的（按邮件头 Date），避免首次同步把 100 封存量全塞进待办
- 首次 `attachment.backfill` / 历史回溯拉进来的老邮件不登记

### 2.2 `summaries` 表

```sql
CREATE TABLE IF NOT EXISTS summaries (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  thread_id TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',          -- 见 2.3
  summary TEXT NOT NULL,                      -- 正文总结
  key_points TEXT NOT NULL DEFAULT '[]',      -- JSON 数组
  entities TEXT NOT NULL DEFAULT '{}',        -- JSON：people/companies/projects/dates/amounts/files
  model TEXT NOT NULL DEFAULT '',             -- 哪个模型写的
  session_id TEXT NOT NULL DEFAULT '',        -- 来自哪条会话，便于回溯
  created_at TEXT NOT NULL
);
CREATE INDEX idx_summaries_message ON summaries(message_id);
CREATE INDEX idx_summaries_thread ON summaries(thread_id);
CREATE INDEX idx_summaries_category ON summaries(category);
```

一封邮件允许多条总结（重写/修订），`summary.list` 按 `created_at desc` 返回，最新为当前。

### 2.3 分类（待确认项 B）

建议固定 8 类，Agent 只能从里面选，避免落一堆同义标签：

`报价询价` / `合同协议` / `会议安排` / `项目进展` / `发票付款` / `人事招聘` / `通知公告` / `其他`

---

## 3. Core RPC

| 方法 | 参数 | 返回 |
|---|---|---|
| `todo.list` | `{accountId?, state?, limit?}` | 事项列表（邮件，含是否已总结、附件数），按收到时间倒序 |
| `todo.set` | `{messageId, state}` | `{state}`；`state` 只接受 `pending`/`done`/`''` |
| `summary.save` | `{messageId, category, summary, keyPoints?, entities?, model?, sessionId?}` | `{id, createdAt}` |
| `summary.list` | `{messageId?, threadId?, category?, limit?}` | 总结列表 |
| `summary.context` | `{messageId}` | **给 Agent 的一次性上下文**：本封元数据+正文摘要、同线程邮件、相关邮件、**这些邮件已有的历史总结**、已下载附件清单 |

`summary.context` 是给 Agent 用的聚合接口，避免它自己串四五个调用。

---

## 4. Agent 工具

| 工具 | 作用 | 是否破坏性 |
|---|---|---|
| `mail_triage_context {messageId?}` | 拉 §3 的 `summary.context`（默认当前选中） | 只读 |
| `mail_summary_save {messageId, category, summary, keyPoints[], entities{}}` | 写总结并发 todo 状态保持 pending | 写库 |
| `mail_mark_done {messageId}` | 标记已处理 | 写库 |

**约束**：
- `category` 必须在 §2.3 的 8 类里，否则 `BAD_REQUEST`
- 写总结前**必须先调用** `mail_triage_context`（工具内部校验：该 message 的上下文是否在当前 turn 拉过；否则拒绝并提示）
- 发现历史总结时，工具返回 `hasHistory:true` + 历史列表，Agent 必须把它们写进回复并征求用户意见

---

## 5. 界面

### 5.1 待跟进页

```
工作邮箱 · 待处理
  📄 LIMES — CIM October 2026.pdf 相关邮件      [总结]    ← 未总结
  📎 报价表.xlsx · 来自「Re: 报价确认」          [下载分析] ← 附件事项
  ✅ 已处理（折叠）
     合同确认书 · 合同协议 · 2 小时前
```

事项来源两类，合并展示：
1. **邮件事项**（`todo_state='pending'`）
2. **附件事项**（已有，未下载的附件）

### 5.2 「总结」按钮

点一下 → 客户端调用 `ctx.uiWorkspace.startSession()` 开一条会话（方案 A2：每封邮件一条，第二次复用），首条消息是一段固定模板：

```
请总结邮件 <messageId>。
先调用 mail_triage_context 拉取上下文；
如果返回 hasHistory=true，先把历史结论列出来问我怎么处理，等我回答后再继续；
总结完成后调用 mail_summary_save（分类只能从给定 8 类里选）；
我确认处理完毕后调用 mail_mark_done。
```

**会话复用**：`localStorage` 里存 `邮件id → sessionId`；点第二次直接 `openSession` 回去。

### 5.3 邮件详情

有总结时在正文上方显示一张只读卡片：分类标签 + 总结 + 要点。

---

## 6. 需要你确认的四件事

| # | 问题 | 我的建议 |
|---|---|---|
| A | 哪些邮件自动进待跟进 | 仅 INBOX、仅最近 7 天；历史回溯进来的不登记 |
| B | 分类怎么定 | 固定 8 类（见 2.3），Agent 只能选不能造 |
| C | 「总结」按钮开新会话还是复用 | 每封邮件一条会话，第二次复用（A2） |
| D | 什么时候算「已处理」 | Agent 调用 `mail_mark_done` 才算；不自动按保存总结就算完 |

---

## 7. 实施顺序与验收

| # | 任务 | 交付 | 验收 |
|---|---|---|---|
| 1 | schema v9 + todo/summary 存储与 RPC | Core | 迁移幂等；`todo.list/set`、`summary.save/list/context` 单测全过 |
| 2 | 新邮件自动登记待办 | Core（sync 入库时） | 新同步的 INBOX 邮件自动 pending；历史回溯的不登记 |
| 3 | 三个 Agent 工具 | 插件 | 假子进程测试：分类校验、未拉上下文拒写、hasHistory 透传 |
| 4 | 待跟进页合并邮件事项 + 总结按钮 | 客户端 | 文案/按钮/分组断言；点击能开会话并带模板 |
| 5 | 邮件详情摘要卡片 | 客户端 | 有总结才显示 |
| 6 | 端到端 | 全链 | 真实邮箱：同步 → 待跟进出现 → 点总结 → Agent 总结 → 落库 → 标记已处理 |

风险：
- 「点总结开会话」依赖客户端 `uiWorkspace.startSession`（已确认存在），但**会话标题/首条消息注入**能力有限，模板只能作为用户消息发送。
- 自动登记待办会让待跟进很快变长，可能需要「批量忽略/全部标为已处理」按钮（建议第 4 步顺带加）。
