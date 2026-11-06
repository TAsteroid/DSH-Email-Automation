# v0.1.0 — 首个可安装版本

把邮箱接进 DeepSeek Harness：实时收信、待跟进、Agent 总结归档、人工审批发信。

## 安装

```powershell
# 方式一：release 附件里的 tgz（推荐）
dsh plugin --profile desktop add file:./dsh-mail-plugin-0.1.0.tgz

# 方式二：脚本
pwsh -File scripts/install.ps1 -Profile desktop

# 方式三：从源码打包
node scripts/build-package.mjs
dsh plugin --profile desktop add file:./dist/dsh-mail-plugin-0.1.0.tgz
```

## 本次包含

- **收信**：IMAP IDLE 实时推送（约 30 秒）；POP3 按间隔轮询
- **待跟进**：新邮件自动登记；附件、总结、已处理一处处理
- **Agent 总结**：一键开会话，附件作为文件传给 Agent，按 8 类归档
- **发信审批**：必须宿主弹窗由真人允许；无 HTTP 批准入口；审计记录批准来源
- **附件**：元数据入库、按需下载（sha256 校验）、上传给会话
- **检索**：FTS5 全文（中文子串回退）、线程归并、历史回溯
- **数据管理**：备份/恢复、切换数据目录（含 WAL 一起复制）、30 天正文清理（有守卫）
- **知识库**：总结 / 文档 / 表格 / 图片 / 日程 / 压缩文件 / 视频 / 其他，可搜索、折叠、删除
- **安装**：tgz 打包安装、安装/卸载/升级脚本、中英双语 README

## 已知限制

- 只有 DSH 运行时才同步，没有独立后台服务
- 摘要需要手动点「总结」；自动触发需要宿主开放 Agent 调用接口
- PDF 文本提取只覆盖文本层，扫描件请把文件直接交给 Agent
- POP3 不能按需取附件，删除只删本地副本
- 知识库暂无跨邮件的实体索引

## 数据库

schema v12，升级时会自动备份数据库。
