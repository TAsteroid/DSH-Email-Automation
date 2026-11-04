# Phase 1 状态：Mail Core 基础服务

- 完成时间：2026-11-04
- 目标：插件能拉起独立 Python Core，握手成功，账号可录入、凭据加密、连接可测试
- 结论：**通过**（验收第 4 条「Core 崩溃恢复」只验证了代码路径，未做真实 kill 测试）

## 交付物

```
mail-core/
  mail_core/protocol.py     行分帧、信封、错误码
  mail_core/store.py        SQLite（WAL）+ user_version 幂等迁移 + accounts/folders/sync_state/meta
  mail_core/crypto.py       DPAPI（ctypes，CryptProtectData/UnprotectData + LocalFree）
  mail_core/imap_client.py  IMAP ssl/starttls/none、login、LIST 解析
  mail_core/service.py      方法分发（handshake/account.*/sync.status/shutdown）
  mail_core/__main__.py     --data-dir、stdio 循环、stdout 只写协议行
  tests/                    29 个 unittest（协议/存储/加密/服务）
dsh-mail-plugin/
  lib/core-client.js        stdio JSON-RPC 客户端（注入式 spawn，可按 id 匹配、超时、退出清理）
  lib/index.js              拉起 Core、三个只读工具、六条 HTTP 路由
scripts/
  verify-host.mjs           假子进程测协议接线与守门（不需要 Python）
  verify-client.mjs         真实 React 渲染测试
```

## 怎么跑

```powershell
# 单元测试（必须用 DSH 自带 Python，系统没有 python）
& 'C:\Users\<you>\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -m unittest discover -s mail-core/tests -t mail-core

# JS 侧
& '<DSH 自带 node>' scripts/verify-host.mjs
& '<DSH 自带 node>' scripts/verify-client.mjs

# 直接驱动 Core（不经插件）
'{"id":"1","method":"core.handshake","params":{}}' | & $py -m mail_core --data-dir <repo>\data\mail
```

数据位置：`data/mail/mail.db`（插件默认与 `--data-dir` 一致）。

## 验证结果

| 验收项 | 结果 |
|---|---|
| 插件启动 → 拉起 Core → 握手 | ✅ `mail_status` 返回 `core ready v0.1.0` 与 dbPath |
| 录入 IMAP 账号（密码 DPAPI） | ✅ 路由 `POST /accounts/add` 写入；库中 `password_blob` 为 DPAPI 密文（base64，`AQAAANCMnd8BFdERjHoAwE...`），全库字节扫描无明文 |
| 测试连接并列文件夹 | ✅ 路由与 `mail_folders` 工具都可用；错误码原样透出（DNS 失败 → `IMAP_ERROR` + 502） |
| Core 崩溃不拖垮插件 | ⚠️ 只验证了代码路径（`state: failed` + 界面报错），未做真实 kill |
| 建库 / 迁移 / 重开不丢 | ✅ 单元测试覆盖（幂等迁移、重开、级联删除） |
| Python 单元测试 | ✅ 29 项通过 |
| JS 测试 | ✅ verify-host / verify-client 全过 |

## 已知问题

1. **`export const Config` 缺失**。本地目录安装的插件解析不到 profile 的 `@deepseek-ai/schemastery`，顶层 import 会让插件加载失败。当前配置靠防御式解析；打包成正式依赖后补。
2. **DPAPI 绑定当前 Windows 用户**。换用户/换机器后密文不可解，`account.test` 返回 `CREDENTIAL_MISSING`，需重新录入密码。
3. **证书校验严格**。`imaplib` 默认不校验证书，Core 显式传了 `ssl.create_default_context()`；自签名证书的服务器会连不上，协议里没有「跳过校验」选项。
4. **没有真实 IMAP 账号验证过**。SSL/STARTTLS 的实际服务器行为未实测，只测到 DNS 失败路径。
5. **同步尚未实现**。`sync.status` 的 messages/unread 恒为 0，还没有邮件表。
6. **测试临时目录**。DSH 沙箱里 `tempfile.TemporaryDirectory()`（0o700）建出的目录拒绝写入，SQLite 都开不了库；测试改用 `.tmp/mail-core-tests` 下默认权限的目录（见 `tests/support.py`）。

## 下一步

Phase 2：IMAP 增量同步（文件夹同步、UIDVALIDITY 处理、去重、线程关联），然后把这几个空态换成真实邮件。
