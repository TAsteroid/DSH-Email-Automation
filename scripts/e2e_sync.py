"""End-to-end check: fake IMAP server -> Mail Core -> SQLite -> host HTTP routes.

Runs against the live DSH host (plugin must be enabled) on the GUI origin.
Creates a throwaway account, syncs, reads back, verifies idempotency, cleans up.

Usage:
    python scripts/e2e_sync.py [origin]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mail-core"))

from tests.fake_servers import FakeImapServer  # noqa: E402

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
    server = FakeImapServer()
    server.start()
    account_id = None
    print(f"fake IMAP on 127.0.0.1:{server.port}")

    try:
        status, body = post(
            "/accounts/add",
            {
                "address": "e2e@example.invalid",
                "displayName": "e2e",
                "protocol": "imap",
                "imapHost": "127.0.0.1",
                "imapPort": server.port,
                "imapSecurity": "none",
                "username": "e2e",
                "password": "e2e-password",
            },
        )
        check(status == 200 and body.get("ok") is True, f"添加账号（HTTP {status}）")
        account_id = (body.get("value") or {}).get("id")
        check(bool(account_id), "拿到 accountId")

        if not account_id:
            return 1

        status, body = post("/sync", {"accountId": account_id, "folder": "INBOX", "limit": 50})
        first = body.get("value") or {}
        check(status == 200, f"首次同步（HTTP {status}）")
        check(first.get("fetched") == 3, f"fetched=3（实际 {first.get('fetched')}）")
        check(first.get("stored") == 3, f"stored=3（实际 {first.get('stored')}）")

        status, body = post("/sync", {"accountId": account_id, "folder": "INBOX", "limit": 50})
        second = body.get("value") or {}
        check(second.get("stored") == 0, f"重复同步幂等 stored=0（实际 {second.get('stored')}）")

        status, body = post("/messages", {"accountId": account_id, "limit": 50})
        listing = body.get("value") or {}
        messages = listing.get("messages") or []
        check(status == 200 and listing.get("total") == 3, f"列表 total=3（实际 {listing.get('total')}）")
        check(len(messages) == 3, f"列表返回 3 条（实际 {len(messages)}）")
        dates = [message.get("date") or "" for message in messages]
        check(dates == sorted(dates, reverse=True), "列表按时间倒序")

        if messages:
            message_id = messages[0]["id"]
            status, body = post("/message", {"id": message_id})
            detail = body.get("value") or {}
            check(status == 200, f"读取正文（HTTP {status}）")
            check(bool(detail.get("subject")), f"正文有主题：{detail.get('subject')!r}")
            check(bool(detail.get("text")), f"正文有文本：{detail.get('text', '')[:40]!r}")

        status, body = post("/message", {"id": "does-not-exist"})
        check(status == 404 and (body or {}).get("code") == "NOT_FOUND", f"未知 id → 404 NOT_FOUND（实际 {status}）")
    finally:
        if account_id:
            status, _ = post("/accounts/remove", {"id": account_id})
            check(status == 200, f"清理账号（HTTP {status}）")
        server.stop()

    if failures:
        print(f"\nFAIL: {len(failures)} 项")
        return 1
    print("\nOK: 全链路通过（添加 → 同步 → 幂等 → 列表 → 正文 → 清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
