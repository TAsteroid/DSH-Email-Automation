"""IMAP IDLE worker tests against a push-capable fake server.

Only four focused cases:

* a pushed ``* n EXISTS`` triggers one INBOX sync and exactly one
  ``mail.new`` event line;
* a short idle timeout sends ``DONE`` and re-enters ``IDLE``;
* ``stop()`` ends the thread and clears ``idle.status``;
* ``idle.start`` skips POP3 / already-running accounts.

The fake server handles connections concurrently because the IDLE connection
stays open while the sync callback opens its own connection.
"""

from __future__ import annotations

import io
import json
import time
import unittest
from pathlib import Path

from mail_core.idle import IdleWorker
from mail_core.service import Service
from tests.fake_servers import FakeImapServer, make_message
from tests.support import workspace_temporary_directory

PASSWORD = "s3cret-pass"


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class IdleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self._stdout = io.StringIO()
        self.service.attach_stdout(self._stdout)
        # Stop workers before the store is closed (cleanups run LIFO).
        self.addCleanup(self._stop_idle)
        self.addCleanup(self.service.close)

    def _stop_idle(self) -> None:
        try:
            self.service.dispatch("idle.stop", {})
        except Exception:
            pass

    def _add_imap_account(self, port: int) -> str:
        return self.service.dispatch(
            "account.add",
            {
                "address": "me@example.com",
                "imapHost": "127.0.0.1",
                "imapPort": port,
                "imapSecurity": "none",
                "username": "me@example.com",
                "password": PASSWORD,
            },
        )["id"]

    def _events(self) -> list[dict]:
        lines = [line for line in self._stdout.getvalue().splitlines() if line]
        return [json.loads(line) for line in lines]

    # -- tests -------------------------------------------------------------

    def test_exists_push_triggers_one_sync_and_one_event(self):
        messages = [
            make_message(
                subject="第一封",
                message_id="<idle1@example.com>",
                date="Mon, 02 Mar 2026 09:00:00 +0000",
                body="body 1",
            ),
            make_message(
                subject="第二封",
                message_id="<idle2@example.com>",
                date="Tue, 03 Mar 2026 09:00:00 +0000",
                body="body 2",
            ),
        ]
        with FakeImapServer(messages, concurrent=True) as server:
            account_id = self._add_imap_account(server.port)
            # Establish the sync cursor first, as the UI does on account test.
            self.service.dispatch("sync.run", {"accountId": account_id})
            self.assertEqual(
                self.service.dispatch("message.list", {})["total"], 2
            )

            started = self.service.dispatch(
                "idle.start", {"accountId": account_id}
            )
            self.assertEqual(started, {"started": [account_id], "skipped": []})
            self.assertTrue(server.idle_event.wait(5))

            server.messages.append(
                make_message(
                    subject="新到的邮件",
                    message_id="<idle3@example.com>",
                    date="Wed, 04 Mar 2026 09:00:00 +0000",
                    body="body 3",
                )
            )
            self.assertTrue(server.push_exists(3))
            self.assertTrue(
                _wait_for(
                    lambda: any(
                        event.get("event") == "mail.new"
                        for event in self._events()
                    )
                )
            )

            events = [e for e in self._events() if e.get("event") == "mail.new"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["accountId"], account_id)
            self.assertEqual(events[0]["stored"], 1)

            self.assertEqual(
                self.service.dispatch("message.list", {})["total"], 3
            )
            workers = self.service.dispatch("idle.status", {})["workers"]
            self.assertEqual(len(workers), 1)
            self.assertEqual(workers[0]["accountId"], account_id)
            self.assertEqual(workers[0]["state"], "idling")
            self.assertTrue(workers[0]["lastEventAt"].endswith("Z"))
            self.assertEqual(workers[0]["reconnects"], 0)

            worker = self.service._idle_workers[account_id]
            self.service.dispatch("idle.stop", {})
            self.assertTrue(_wait_for(lambda: not worker.is_alive(), timeout=5))

    def test_idle_timeout_sends_done_and_reenters_idle(self):
        with FakeImapServer(
            [make_message(subject="一", message_id="<t1@example.com>")],
            concurrent=True,
        ) as server:
            account_id = self._add_imap_account(server.port)
            worker = IdleWorker(
                account=self.service.store.get_account(account_id),
                password=PASSWORD,
                on_new_mail=lambda _account_id: None,
                idle_timeout=0.3,
                backoff_initial=1.0,
                backoff_max=2.0,
            )
            self.addCleanup(worker.stop)
            worker.start()

            self.assertTrue(
                _wait_for(lambda: server.idle_count >= 2, timeout=10)
            )
            commands = [
                line.strip().upper() for line in server.commands if line.strip()
            ]
            status = worker.status()
            self.assertEqual(status["reconnects"], 0)
            self.assertIn(status["state"], {"connected", "idling"})

        idle_positions = [
            i for i, line in enumerate(commands) if line.endswith(" IDLE")
        ]
        done_positions = [i for i, line in enumerate(commands) if line == "DONE"]
        self.assertGreaterEqual(len(idle_positions), 2)
        self.assertTrue(done_positions)
        self.assertLess(idle_positions[0], done_positions[0])
        self.assertLess(done_positions[0], idle_positions[1])

    def test_stop_ends_the_thread_and_clears_status(self):
        with FakeImapServer(
            [make_message(subject="一", message_id="<s1@example.com>")],
            concurrent=True,
        ) as server:
            account_id = self._add_imap_account(server.port)
            self.service.dispatch("idle.start", {})
            self.assertTrue(server.idle_event.wait(5))
            worker = self.service._idle_workers[account_id]

            stopped = self.service.dispatch("idle.stop", {})
            self.assertEqual(stopped, {"stopped": [account_id]})
            self.assertTrue(_wait_for(lambda: not worker.is_alive(), timeout=5))

            self.assertEqual(worker.status()["state"], "stopped")
            self.assertEqual(self.service.dispatch("idle.status", {})["workers"], [])
            self.assertTrue(
                any(line.strip().upper() == "DONE" for line in server.commands)
            )

    def test_start_skips_pop3_and_already_running(self):
        with FakeImapServer(
            [make_message(subject="一", message_id="<a1@example.com>")],
            concurrent=True,
        ) as server:
            account_id = self._add_imap_account(server.port)
            pop3_id = self.service.dispatch(
                "account.add",
                {
                    "address": "pop@example.com",
                    "protocol": "pop3",
                    "imapHost": "127.0.0.1",
                    "imapPort": 110,
                    "imapSecurity": "none",
                    "username": "pop@example.com",
                    "password": PASSWORD,
                },
            )["id"]

            first = self.service.dispatch("idle.start", {})
            self.assertEqual(first["started"], [account_id])
            self.assertIn(pop3_id, first["skipped"])
            self.assertTrue(server.idle_event.wait(5))

            second = self.service.dispatch("idle.start", {})
            self.assertEqual(second["started"], [])
            self.assertIn(account_id, second["skipped"])

            status = self.service.dispatch("idle.status", {})["workers"]
            self.assertEqual(len(status), 1)
            self.assertEqual(
                set(status[0]),
                {"accountId", "state", "lastEventAt", "reconnects", "lastError"},
            )
            self.assertEqual(
                self.service.dispatch(
                    "idle.stop", {"accountId": account_id}
                ),
                {"stopped": [account_id]},
            )

    def test_login_failure_stops_the_worker_without_retrying(self):
        with FakeImapServer(
            [make_message(subject="一", message_id="<auth@example.com>")],
            concurrent=True,
            reject_login=True,
        ) as server:
            account_id = self._add_imap_account(server.port)
            started = self.service.dispatch(
                "idle.start", {"accountId": account_id}
            )
            self.assertEqual(started["started"], [account_id])
            worker = self.service._idle_workers[account_id]

            self.assertTrue(_wait_for(lambda: not worker.is_alive(), timeout=5))
            status = self.service.dispatch("idle.status", {})["workers"]

            self.assertEqual(len(server.logins), 1, "no retry after auth failure")
            self.assertEqual(
                sum(
                    1
                    for line in server.commands
                    if line.strip().upper().endswith(" IDLE")
                ),
                0,
            )
            self.assertEqual(status[0]["state"], "auth-failed")
            self.assertEqual(status[0]["reconnects"], 0)
            self.assertTrue(status[0]["lastError"])
            self.assertNotIn(PASSWORD, status[0]["lastError"])

            # After the credential is updated, idle.start replaces the dead
            # worker with a fresh one.
            self.service.dispatch(
                "account.updateCredential",
                {"id": account_id, "password": "new-pass"},
            )
            restarted = self.service.dispatch(
                "idle.start", {"accountId": account_id}
            )
            self.assertEqual(restarted["started"], [account_id])
            self.assertIsNot(
                self.service._idle_workers[account_id], worker
            )

    def test_network_error_keeps_retrying(self):
        with FakeImapServer(
            [make_message(subject="一", message_id="<net@example.com>")],
        ) as server:
            account_id = self._add_imap_account(server.port)
            account = self.service.store.get_account(account_id)
            server.stop()  # connection refused from now on

            worker = IdleWorker(
                account=account,
                password=PASSWORD,
                on_new_mail=lambda _account_id: None,
                backoff_initial=0.05,
                backoff_max=0.1,
            )
            self.addCleanup(worker.stop)
            worker.start()

            self.assertTrue(
                _wait_for(
                    lambda: worker.status()["reconnects"] >= 1, timeout=5
                )
            )
            status = worker.status()
            self.assertNotEqual(status["state"], "auth-failed")

        self.assertGreaterEqual(status["reconnects"], 1)
        self.assertIn("lastError", status)


if __name__ == "__main__":
    unittest.main()
