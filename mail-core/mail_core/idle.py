"""IMAP IDLE workers: one background thread per enabled IMAP account.

The flow is the one AliMail documents for IDLE:

1. connect, ``LOGIN`` and ``SELECT INBOX``;
2. send ``IDLE``, wait for the ``+ idling`` continuation, then block on
   ``readline()`` with a 240 second socket timeout;
3. ``* n EXISTS`` means new mail: call the service callback (which syncs the
   INBOX through the normal sync path and emits ``mail.new``), then keep
   reading — the connection stays in IDLE;
4. a timeout sends ``DONE`` (servers drop an IDLE after about five minutes)
   and re-enters IDLE;
5. a broken connection is retried with exponential backoff (1 s doubling to
   60 s) without ever letting the thread die.

The thread never touches SQLite itself: the ``on_new_mail`` callback runs the
sync inside the service lock, which is what keeps a single SQLite connection
safe.
"""

from __future__ import annotations

import imaplib
import re
import socket
import threading
from typing import Callable

from . import imap_client
from .protocol import MailCoreError
from .store import utc_now

IDLE_TIMEOUT = 240.0
DONE_TIMEOUT = 10.0
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
FOLDER = "INBOX"

# imaplib failures that mean "the server or the network failed".
_IMAP_FAILURES = (imaplib.IMAP4.error, OSError, socket.timeout, ValueError)

_EXISTS_RE = re.compile(rb"^\* (\d+) EXISTS", re.IGNORECASE)

# A login that the server rejected (bad/expired password) is terminal until
# the user updates the credential: retrying only feeds provider rate limits.
_AUTH_FAILURE_RE = re.compile(
    r"AUTHENTICATIONFAILED|AUTHENTICATION FAILED|AUTHENTICATION ERROR|"
    r"LOGIN FAILED|LOGIN AUTHENTICATION FAILED|AUTH FAILED|"
    r"INVALID CREDENTIALS|INVALID USERNAME|INVALID PASSWORD",
    re.IGNORECASE,
)


class IdleTimeout(Exception):
    """The idle read timed out; the caller sends DONE and re-enters IDLE."""


class _AuthFailed(Exception):
    """Terminal login failure; the worker stops instead of retrying."""


def _one_line(text) -> str:
    return " ".join(str(text).split())


class IdleWorker(threading.Thread):
    """One IDLE loop for one IMAP account (daemon thread)."""

    def __init__(
        self,
        *,
        account: dict,
        password: str,
        on_new_mail: Callable[[str], None],
        idle_timeout: float = IDLE_TIMEOUT,
        backoff_initial: float = BACKOFF_INITIAL,
        backoff_max: float = BACKOFF_MAX,
    ) -> None:
        super().__init__(name=f"mail-idle-{account['id']}", daemon=True)
        self.account = dict(account)
        self.account_id = str(account["id"])
        self.password = password
        self._on_new_mail = on_new_mail
        self._idle_timeout = float(idle_timeout)
        self._backoff_initial = float(backoff_initial)
        self._backoff_max = float(backoff_max)
        self._stop_event = threading.Event()
        self._state = "stopped"
        self._last_event_at = ""
        self._reconnects = 0
        self._error = ""
        self._connection = None
        self._connection_lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def status(self) -> dict:
        """Protocol shape for ``idle.status`` (``lastError`` is redacted)."""
        return {
            "accountId": self.account_id,
            "state": self._state,
            "lastEventAt": self._last_event_at,
            "reconnects": self._reconnects,
            "lastError": self._error,
        }

    @property
    def error(self) -> str:
        return self._error

    def stop(self, *, join: bool = True, timeout: float = 5.0) -> None:
        """Ask the thread to exit; optionally wait for it.

        The service calls this with ``join=False`` while holding its RPC lock:
        the worker may be waiting for that same lock inside its sync callback,
        and joining it there would deadlock.
        """
        self._stop_event.set()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.send(b"DONE\r\n")
            except Exception:
                pass
            try:
                connection.shutdown()
            except Exception:
                try:
                    connection.sock.close()
                except Exception:
                    pass
        if join and self.is_alive() and self is not threading.current_thread():
            self.join(timeout)

    # -- thread body -------------------------------------------------------

    def run(self) -> None:
        backoff = self._backoff_initial
        auth_failed = False
        while not self._stop_event.is_set():
            connection = None
            try:
                self._state = "connecting"
                connection = imap_client.connect(
                    self.account["imapHost"],
                    int(self.account["imapPort"]),
                    self.account.get("imapSecurity") or "ssl",
                )
                with self._connection_lock:
                    self._connection = connection
                try:
                    connection.login(
                        self.account["username"], self.password
                    )
                except _IMAP_FAILURES as exc:
                    message = self._redact(exc)
                    if _AUTH_FAILURE_RE.search(message):
                        raise _AuthFailed(message) from None
                    raise imap_client.ImapError(
                        f"login failed: {message}"
                    ) from None

                status, data = connection.select(FOLDER)
                if status != "OK":
                    raise imap_client.ImapError(
                        f"SELECT {FOLDER} failed: {self._redact(data)}"
                    )
                self._state = "connected"
                self._error = ""
                backoff = self._backoff_initial

                while not self._stop_event.is_set():
                    self._idle_once(connection)
            except _AuthFailed as exc:
                # Terminal until the credential is updated: no backoff, no
                # reconnect.  ``idle.start`` can start a fresh worker.
                self._state = "auth-failed"
                self._error = _one_line(str(exc))
                auth_failed = True
            except MailCoreError as exc:
                if self._stop_event.is_set():
                    break
                self._state = "error"
                self._error = exc.message
            except Exception as exc:  # never let the thread die
                if self._stop_event.is_set():
                    break
                self._state = "error"
                self._error = self._redact(f"{type(exc).__name__}: {exc}")
            finally:
                self._close(connection)
                with self._connection_lock:
                    self._connection = None

            if self._stop_event.is_set() or auth_failed:
                break
            self._reconnects += 1
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, self._backoff_max)
        if not auth_failed:
            self._state = "stopped"

    # -- IDLE --------------------------------------------------------------

    def _idle_once(self, connection) -> None:
        """One IDLE session: wait for new mail or run out the timeout."""
        tag = connection._new_tag()
        self._state = "connected"
        try:
            connection.send(tag + b" IDLE\r\n")
        except OSError as exc:
            raise imap_client.ImapError(
                f"IDLE send failed: {self._redact(exc)}"
            ) from None

        # Wait for the "+ idling" continuation before treating the session as
        # open; a tagged reply here means the server refused IDLE.
        while True:
            try:
                line = self._read_line(connection)
            except IdleTimeout:
                raise imap_client.ImapError(
                    "IDLE was not acknowledged in time"
                ) from None
            if line.startswith(b"+"):
                break
            if line.startswith(tag):
                return
            if self._is_bye(line):
                raise imap_client.ImapError("connection closed while idling")

        self._state = "idling"
        while not self._stop_event.is_set():
            try:
                line = self._read_line(connection)
            except IdleTimeout:
                self._finish_idle(connection, tag)
                return
            if _EXISTS_RE.match(line):
                self._handle_exists(line)
                continue
            if line.startswith(tag):
                # The server ended IDLE itself (its own timeout, BYE, ...):
                # the caller re-issues IDLE on the same connection.
                return
            if self._is_bye(line):
                raise imap_client.ImapError("connection closed while idling")
            # Other untagged lines (RECENT, OK, ...) carry no action.

    def _read_line(self, connection) -> bytes:
        try:
            connection.sock.settimeout(self._idle_timeout)
            line = connection.readline()
        except (TimeoutError, socket.timeout):
            raise IdleTimeout() from None
        except OSError as exc:
            raise imap_client.ImapError(
                f"idle read failed: {self._redact(exc)}"
            ) from None
        if not line:
            raise imap_client.ImapError("connection closed while idling")
        return line

    def _finish_idle(self, connection, tag: bytes) -> None:
        """Leave IDLE gracefully so the next IDLE can be sent.

        A socket timeout poisons the buffered reader imaplib keeps in
        ``connection.file`` ("cannot read from timed out object"), so the
        reader is recreated before the DONE reply is read.  The socket itself
        is still healthy, which is why the same connection can re-enter IDLE.
        """
        self._state = "connected"
        try:
            connection.sock.settimeout(DONE_TIMEOUT)
            connection.send(b"DONE\r\n")
            self._reopen_reader(connection)
            for _ in range(20):
                line = connection.readline()
                if not line:
                    raise imap_client.ImapError(
                        "connection closed while ending IDLE"
                    )
                if line.startswith(tag):
                    return
        except (TimeoutError, socket.timeout):
            raise imap_client.ImapError("DONE was not acknowledged in time") from None
        except OSError as exc:
            raise imap_client.ImapError(
                f"IDLE DONE failed: {self._redact(exc)}"
            ) from None
        raise imap_client.ImapError("DONE was not acknowledged in time")

    @staticmethod
    def _reopen_reader(connection) -> None:
        """Replace imaplib's buffered reader after a socket timeout."""
        try:
            connection.file.close()
        except Exception:
            pass
        connection.file = connection.sock.makefile("rb")

    # -- helpers -----------------------------------------------------------

    def _handle_exists(self, line: bytes) -> None:
        """React to ``* n EXISTS``: sync through the service callback."""
        self._last_event_at = utc_now()
        try:
            self._on_new_mail(self.account_id)
        except Exception as exc:  # sync failure only changes the local state
            self._state = "error"
            self._error = self._redact(f"{type(exc).__name__}: {exc}")
        else:
            if not self._stop_event.is_set():
                self._state = "idling"

    def _close(self, connection) -> None:
        if connection is None:
            return
        try:
            connection.logout()
        except Exception:
            try:
                connection.shutdown()
            except Exception:
                pass

    def _redact(self, text) -> str:
        value = _one_line(text)
        return value.replace(self.password, "***") if self.password else value

    @staticmethod
    def _is_bye(line: bytes) -> bool:
        return line.upper().startswith(b"* BYE")
