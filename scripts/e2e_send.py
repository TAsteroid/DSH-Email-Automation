"""End-to-end check for Phase 5: draft -> approval -> real send.

Runs against the live DSH host and a loopback fake SMTP server:
creates a throwaway account pointing at it, saves a draft, requests approval,
approves, executes, then verifies the mail actually arrived exactly once.

Usage:
    python scripts/e2e_send.py [origin]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mail-core"))

from tests.fake_servers import FakeSmtpServer  # noqa: E402

ORIGIN = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:52452"
API = f"{ORIGIN}/api/dsh-mail"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        failures.append(message)


def post(path: str, body: dict | None = None, timeout: int = 120) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(
        API + path, data=data, headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8")
        try:
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, {"raw": payload}


def main() -> int:
    smtp = FakeSmtpServer(auth=True)
    smtp.start()
    account_id = None
    print(f"fake SMTP on 127.0.0.1:{smtp.port}")

    try:
        status, body = post(
            "/accounts/add",
            {
                "address": "sender@example.invalid",
                "displayName": "e2e sender",
                "protocol": "imap",
                "imapHost": "127.0.0.1",
                "imapPort": 1,  # 不用于发信
                "imapSecurity": "none",
                "username": "sender@example.invalid",
                "password": "e2e-password",
                "smtpHost": "127.0.0.1",
                "smtpPort": smtp.port,
                "smtpSecurity": "none",
            },
        )
        check(status == 200 and body.get("ok") is True, f"添加带 SMTP 的账号（HTTP {status}）")
        account_id = (body.get("value") or {}).get("id")
        if not account_id:
            return 1

        status, body = post(
            "/drafts/save",
            {
                "accountId": account_id,
                "to": "receiver@example.com",
                "cc": "",
                "subject": "e2e 报价回复",
                "body": "这是一封端到端测试邮件。",
            },
        )
        draft = body.get("value") or {}
        check(status == 200 and bool(draft.get("id")), f"保存草稿（HTTP {status}）")
        draft_id = draft.get("id")
        content_hash = draft.get("contentHash")

        status, body = post("/approvals/request", {"draftId": draft_id})
        approval = body.get("value") or {}
        check(status == 200 and bool(approval.get("id")), f"提交审批（HTTP {status}）")
        approval_id = approval.get("id")
        check(approval.get("contentHash") == content_hash, "审批哈希与草稿一致")

        # 未批准时执行必须被拒
        status, body = post("/actions/execute", {"approvalId": approval_id})
        check(status != 200 and body.get("code") == "APPROVAL_NOT_APPROVED", f"未批准不能发送（{status} {body.get('code')}）")

        status, body = post("/approvals/approve", {"id": approval_id})
        check(status == 200 and body.get("ok") is True, f"批准（HTTP {status}）")

        status, body = post("/actions/execute", {"approvalId": approval_id})
        sent = body.get("value") or {}
        check(status == 200 and sent.get("sent") is True, f"执行发送（HTTP {status}）")
        check(bool(sent.get("messageId")), "返回 Message-ID")
        check(len(smtp.messages) == 1, f"SMTP 服务器收到 1 封（实际 {len(smtp.messages)}）")
        if smtp.messages:
            import email
            from email.header import decode_header, make_header

            received = email.message_from_bytes(smtp.messages[0])
            subject = str(make_header(decode_header(received.get("Subject", ""))))
            payload = received.get_payload(decode=True) or b""
            body_text = payload.decode(received.get_content_charset() or "utf-8", "replace")
            check(subject == "e2e 报价回复", f"收到邮件主题正确（实际 {subject!r}）")
            check("这是一封端到端测试邮件" in body_text, "收到邮件正文正确")
            check("receiver@example.com" in smtp.recipients[0] if smtp.recipients else False, "收件人正确")
            check(received.get("From", "").find("sender@example.invalid") >= 0, "发件人使用账号地址")

        # 重复执行：绝不重发
        status, body = post("/actions/execute", {"approvalId": approval_id})
        check(body.get("code") == "ALREADY_EXECUTED", f"重复执行被拒（{body.get('code')}）")
        check(len(smtp.messages) == 1, f"重复执行没有重发（仍 {len(smtp.messages)} 封）")

        # 改草稿后审批失效
        status, body = post(
            "/drafts/save",
            {"draftId": draft_id, "accountId": account_id, "to": "x@example.com", "cc": "", "subject": "改过的主题", "body": "改过的正文"},
        )
        check(status == 200, f"修改草稿（HTTP {status}）")
        status, body = post("/approvals/request", {"draftId": draft_id})
        second = body.get("value") or {}
        post("/approvals/approve", {"id": second.get("id")})
        post(
            "/drafts/save",
            {"draftId": draft_id, "accountId": account_id, "to": "x@example.com", "cc": "", "subject": "又改了", "body": "又改了"},
        )
        status, body = post("/actions/execute", {"approvalId": second.get("id")})
        check(body.get("code") == "HASH_MISMATCH", f"批准后改内容不能发送（{body.get('code')}）")
        check(len(smtp.messages) == 1, f"仍未重发（{len(smtp.messages)} 封）")
    finally:
        if account_id:
            post("/accounts/remove", {"id": account_id})
            print("  cleaned up account")
        smtp.stop()

    if failures:
        print(f"\nFAIL: {len(failures)} 项")
        return 1
    print("\nOK: 草稿 → 审批 → 发送 → 幂等 → 哈希失效 全链路通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
