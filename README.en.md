# DSH Email Automation Plugin

English | [中文](README.md)

Brings a real mailbox into DeepSeek Harness: real-time mail push, automatic follow-up items, one-click hand-off of a mail (and its attachments) to an Agent for summarization, and human-approved sending.

The plugin has two halves: a DSH host plugin (`dsh-mail-plugin/`) and a pure-stdlib Python Core (`mail-core/`, spawned and supervised by the plugin — you never run it yourself). All data lives in a local SQLite database; mailbox passwords are encrypted with Windows DPAPI.

## What it does

| Capability | Detail |
|---|---|
| Receive | IMAP accounts use **IDLE push** (mail shows up within ~30s); POP3 accounts poll on an interval |
| Follow-up list | New mail becomes a to-do automatically; attachments, summaries and "done" live in one place |
| Agent summaries | One click opens a session and hands the mail (attachments included as files) to an Agent, filed under 8 categories |
| Send approval | You must click **Allow** in the DSH approval prompt; the agent cannot approve itself and no HTTP approve endpoint exists |
| Attachments | Metadata only until you download; once downloaded they can be **passed to the Agent as real files** |
| Search | FTS5 full-text search (with CJK substring fallback), thread grouping, historical backfill |
| Data management | Backup / restore / move the database directory / 30-day body cleanup with guards |

## Quick start

### From the packaged tarball (easiest for another machine)

```powershell
node scripts/build-package.mjs     # produces dist\dsh-mail-plugin-<version>.tgz
dsh plugin --profile desktop add file:./dist/dsh-mail-plugin-0.1.0.tgz
```

> On another machine, copy the `.tgz` over and replace `./dist/...` with its real path.

`dsh` is the CLI shipped with DSH; it installs the package into the profile and registers it in both `dependencies` and `dsh.profile.bundles`.

### From the source checkout

```powershell
pwsh -File scripts/install.ps1 -Profile desktop
```

The script creates the directory junction, writes the profile configuration and creates data directories. Restart DSH afterwards.

### First configuration

Settings → add account: IMAP/SMTP host and port, address, password (enterprise mailboxes usually need an **app password**), then "Test connection" — you should see the folder count and `smtp ok`.

## How it works

```
DSH host plugin (Node)
 ├── 19 agent tools (read-only queries / draft / request approval / send / summarize)
 ├── 57 local HTTP routes (the UI's only data channel, same-origin guarded)
 ├── SSE real-time event stream
 └── spawns and supervises the Python Core (stdio JSON-RPC)
        └── SQLite (WAL) · DPAPI credentials · IMAP/POP3/SMTP · FTS5 · attachments
```

**Security model**: sending can only be started by the agent tool `mail_send_draft`, and only after the harness approval prompt is allowed by a human. There is no HTTP approve/execute endpoint. Approval rows carry a content hash, expiry, idempotency key and an audit field describing how the approval happened.

## Status

| Phase | State |
|---|---|
| Plugin reconnaissance / Core / sync | ✅ |
| Parsing · cache · backup · threads · backfill | ✅ |
| Drafts · approval · controlled sending | ✅ |
| Follow-ups · summaries · attachments · realtime | ✅ |
| Install / upgrade / documentation | ✅ |
| Automatic summarization (no click) | ⏳ blocked on a host-side agent invocation API |
| Knowledge-base entity index | ⏳ not started |

Database schema v12; 210+ Python tests plus two JS suites (fake host child process, real React rendering).

## Documentation

| Document | Content |
|---|---|
| [User guide](docs/USER_GUIDE.md) | Install, accounts, UI tour, troubleshooting, upgrade/uninstall |
| [Plan delta](docs/PLAN_DELTA.md) | How the implementation diverges from the original plan (authoritative) |
| [Core protocol](docs/IPC_PROTOCOL.md) | stdio JSON-RPC methods, fields, error codes, migrations |
| [Follow-up & summary design](docs/PLAN_TODO_SUMMARY.md) | To-do registration, summary flow, categories |
| [Scripts](scripts/README.md) | install/uninstall/upgrade parameters and packaging |

The original requirements plan is a private local document (`docs/PLAN.md`, not committed). `docs/PLAN_DELTA.md` is the source of truth for the actual state.

## Known limitations

- Mail is only synced while DSH runs; there is no standalone background service
- Summaries need a click: automatic triggering requires host support for agent invocation
- PDF text extraction covers only the text layer — hand scanned files to the agent directly
- POP3 cannot fetch attachments on demand; deleting only removes the local copy

## Development

```powershell
# Python Core tests
python -m unittest discover -s mail-core/tests -t mail-core

# Host/client static verification
node scripts/verify-host.mjs
node scripts/verify-client.mjs

# Package
node scripts/build-package.mjs
```
