"""Loopback fake servers for the receive/send protocol probe tests.

POP3 and SMTP are tested against real sockets on ``127.0.0.1`` so the clients
run their actual code paths (``poplib`` / ``smtplib``) without leaving the
machine.  Each server records the commands it received, which lets a test
assert e.g. that the SMTP probe never issues ``MAIL FROM``.
"""

from __future__ import annotations

import email.header
import re
import socket
import threading


def _header_value(text: str) -> str:
    """RFC 2047-encode a header value when it is not plain ASCII."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return email.header.Header(text, "utf-8").encode()


def make_message(
    *,
    subject: str = "",
    from_addr: str = "sender@example.com",
    to_addr: str = "me@example.com",
    cc_addr: str = "",
    date: str = "Mon, 03 Nov 2026 09:12:00 +0000",
    message_id: str = "<message@example.com>",
    body: str = "",
    html: str = "",
) -> bytes:
    """Build a raw RFC 822 message for the fake servers and tests."""
    lines = [
        f"From: {from_addr}",
        f"To: {to_addr}",
    ]
    if cc_addr:
        lines.append(f"Cc: {cc_addr}")
    lines += [
        f"Subject: {_header_value(subject)}",
        f"Date: {date}",
        f"Message-ID: {message_id}",
        "MIME-Version: 1.0",
    ]
    if html:
        boundary = "----fake-mail-core-boundary"
        lines += [
            f'Content-Type: multipart/alternative; boundary="{boundary}"',
            "",
            f"--{boundary}",
            'Content-Type: text/plain; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
            "",
            body,
            f"--{boundary}",
            'Content-Type: text/html; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
            "",
            html,
            f"--{boundary}--",
        ]
    else:
        lines += [
            'Content-Type: text/plain; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
            "",
            body,
        ]
    lines.append("")
    return "\r\n".join(lines).encode("utf-8")


def _synthetic_message(tag: str, index: int) -> bytes:
    return make_message(
        subject=f"{tag} message {index}",
        date=f"Mon, {index:02d} Nov 2026 0{index}:00:00 +0000",
        message_id=f"<{tag}{index}@example.com>",
        body=f"body of {tag} message {index}",
    )


class _LoopbackServer:
    """Accepts one connection at a time on an ephemeral loopback port."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self.host = "127.0.0.1"
        self.port = int(self._listener.getsockname()[1])
        self.commands: list[str] = []
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc_info) -> bool:
        self.stop()
        return False

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _address = self._listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            try:
                connection.settimeout(5.0)
                self.handle(connection)
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def handle(self, connection: socket.socket) -> None:  # pragma: no cover
        raise NotImplementedError


class FakePop3Server(_LoopbackServer):
    """Speaks just enough POP3 for ``USER`` / ``PASS`` / ``STAT`` / ``UIDL``.

    ``bodies`` (one raw RFC 822 byte string per message) enables the sync
    path; when omitted, synthetic messages are generated.  ``UIDL`` values are
    zero-padded message numbers, so their "integer part" is stable across
    repeated syncs.
    """

    def __init__(
        self,
        *,
        messages: int = 3,
        total_size: int = 4096,
        bodies: list[bytes] | None = None,
    ) -> None:
        super().__init__()
        if bodies is not None:
            self.bodies = [bytes(body) for body in bodies]
            self.messages = len(self.bodies)
        else:
            self.messages = int(messages)
            self.bodies = [
                _synthetic_message("pop3", index)
                for index in range(1, self.messages + 1)
            ]
        self.total_size = int(total_size)
        self.credentials: list[tuple[str, str]] = []
        self._user = ""

    def handle(self, connection: socket.socket) -> None:
        connection.sendall(b"+OK fake POP3 server ready\r\n")
        stream = connection.makefile("rb")
        with stream:
            for raw in stream:
                line = raw.decode("ascii", "replace").strip()
                if not line:
                    continue
                self.commands.append(line)
                command, _, argument = line.partition(" ")
                command = command.upper()
                if command == "USER":
                    self._user = argument
                    connection.sendall(b"+OK user accepted\r\n")
                elif command == "PASS":
                    self.credentials.append((self._user, argument))
                    connection.sendall(b"+OK maildrop locked\r\n")
                elif command == "STAT":
                    payload = f"+OK {self.messages} {self.total_size}\r\n"
                    connection.sendall(payload.encode("ascii"))
                elif command == "UIDL":
                    connection.sendall(b"+OK unique-id listing follows\r\n")
                    for index in range(1, self.messages + 1):
                        line = f"{index} {index:06d}\r\n"
                        connection.sendall(line.encode("ascii"))
                    connection.sendall(b".\r\n")
                elif command == "LIST":
                    connection.sendall(b"+OK scan listing follows\r\n")
                    for index, body in enumerate(self.bodies, start=1):
                        line = f"{index} {len(body)}\r\n"
                        connection.sendall(line.encode("ascii"))
                    connection.sendall(b".\r\n")
                elif command == "RETR":
                    connection.sendall(b"+OK message follows\r\n")
                    try:
                        body = self.bodies[int(argument) - 1]
                    except (ValueError, IndexError):
                        connection.sendall(b".\r\n")
                        continue
                    for body_line in body.splitlines():
                        if body_line.startswith(b"."):
                            body_line = b"." + body_line
                        connection.sendall(body_line + b"\r\n")
                    connection.sendall(b".\r\n")
                elif command == "QUIT":
                    connection.sendall(b"+OK bye\r\n")
                    return
                elif command == "CAPA":
                    connection.sendall(b"-ERR no capabilities\r\n")
                elif command == "NOOP":
                    connection.sendall(b"+OK\r\n")
                else:
                    connection.sendall(b"-ERR unknown command\r\n")


class FakeSmtpServer(_LoopbackServer):
    """Speaks just enough SMTP for ``EHLO`` / ``QUIT`` (and a busy signal)."""

    def __init__(
        self,
        *,
        reject: bool = False,
        reject_reply: str = "554 no SMTP service here",
        greeting: str = "220 fake SMTP ready",
    ) -> None:
        super().__init__()
        self.reject = reject
        self.greeting = reject_reply if reject else greeting

    def handle(self, connection: socket.socket) -> None:
        connection.sendall(f"{self.greeting}\r\n".encode("ascii"))
        if self.reject:
            return
        stream = connection.makefile("rb")
        with stream:
            for raw in stream:
                line = raw.decode("ascii", "replace").strip()
                if not line:
                    continue
                self.commands.append(line)
                command = line.partition(" ")[0].upper()
                if command in ("EHLO", "HELO"):
                    connection.sendall(b"250 fake\r\n")
                elif command == "STARTTLS":
                    connection.sendall(b"502 STARTTLS not supported\r\n")
                elif command == "QUIT":
                    connection.sendall(b"221 bye\r\n")
                    return
                elif command == "NOOP":
                    connection.sendall(b"250 ok\r\n")
                else:
                    connection.sendall(b"502 command not implemented\r\n")


def _imap_tokens(text: str) -> list[str]:
    """Split an IMAP argument string, honouring quoted strings and escapes."""
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and in_quotes:
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char.isspace() and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


class FakeImapServer(_LoopbackServer):
    """Speaks enough IMAP4rev1 for the sync path.

    Supported: ``CAPABILITY``, ``LOGIN``, ``LIST``, ``SELECT``/``EXAMINE``,
    ``UID SEARCH``, ``UID FETCH`` (literal body) and ``LOGOUT``.  ``messages``
    is one raw RFC 822 byte string per message; mailbox UID ``n`` is
    ``messages[n - 1]``.  ``uidvalidity`` can be changed between syncs to
    exercise the reset path.
    """

    def __init__(
        self,
        messages: list[bytes] | None = None,
        *,
        uidvalidity: int = 12345,
        folder_lines: list[bytes] | None = None,
    ) -> None:
        super().__init__()
        self.messages = (
            list(messages)
            if messages is not None
            else [
                _synthetic_message("imap", index) for index in range(1, 4)
            ]
        )
        self.uidvalidity = int(uidvalidity)
        # Raw ``* LIST`` payloads; the default is the single INBOX line.
        self.folder_lines = (
            [bytes(line) for line in folder_lines]
            if folder_lines is not None
            else [b'* LIST (\\HasNoChildren) "/" INBOX\r\n']
        )
        self.logins: list[tuple[str, str]] = []
        self.selected: list[tuple[str, bool]] = []

    # -- protocol helpers --------------------------------------------------

    @staticmethod
    def _send(connection: socket.socket, text: str | bytes) -> None:
        payload = text.encode("utf-8") if isinstance(text, str) else text
        connection.sendall(payload)

    def _ok(self, connection: socket.socket, tag: str, text: str) -> None:
        self._send(connection, f"{tag} OK {text}\r\n")

    def handle(self, connection: socket.socket) -> None:
        self._send(connection, b"* OK fake IMAP server ready\r\n")
        stream = connection.makefile("rb")
        with stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                self.commands.append(line)
                tag, _, rest = line.partition(" ")
                if not rest:
                    self._send(connection, f"{tag} BAD missing command\r\n")
                    continue
                command, _, arguments = rest.partition(" ")
                command = command.upper()
                arguments = arguments.strip()

                if command == "CAPABILITY":
                    self._send(
                        connection,
                        b"* CAPABILITY IMAP4rev1 UIDPLUS\r\n",
                    )
                    self._ok(connection, tag, "CAPABILITY completed")
                elif command == "LOGIN":
                    tokens = _imap_tokens(arguments)
                    self.logins.append(
                        (tokens[0] if tokens else "", tokens[1] if len(tokens) > 1 else "")
                    )
                    self._ok(connection, tag, "LOGIN completed")
                elif command in ("SELECT", "EXAMINE"):
                    mailbox = _imap_tokens(arguments)
                    name = mailbox[0] if mailbox else ""
                    readonly = command == "EXAMINE"
                    self.selected.append((name, readonly))
                    self._send(
                        connection,
                        b"* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)\r\n",
                    )
                    self._send(connection, f"* {len(self.messages)} EXISTS\r\n")
                    self._send(connection, b"* 0 RECENT\r\n")
                    self._send(
                        connection,
                        f"* OK [UIDVALIDITY {self.uidvalidity}] UIDs valid\r\n",
                    )
                    self._send(
                        connection,
                        f"* OK [UIDNEXT {len(self.messages) + 1}] "
                        "Predicted next UID\r\n",
                    )
                    suffix = " [READ-ONLY]" if readonly else ""
                    self._ok(
                        connection,
                        tag,
                        f"{suffix} {command} completed".strip(),
                    )
                elif command == "LIST":
                    for line in self.folder_lines:
                        self._send(connection, line)
                    self._ok(connection, tag, "LIST completed")
                elif command == "UID":
                    self._handle_uid(connection, tag, arguments)
                elif command == "LOGOUT":
                    self._send(connection, b"* BYE fake IMAP server logging out\r\n")
                    self._ok(connection, tag, "LOGOUT completed")
                    return
                elif command == "NOOP":
                    self._ok(connection, tag, "NOOP completed")
                else:
                    self._send(connection, f"{tag} BAD unknown command\r\n")

    def _handle_uid(
        self, connection: socket.socket, tag: str, arguments: str
    ) -> None:
        subcommand, _, sub_arguments = arguments.partition(" ")
        subcommand = subcommand.upper()
        sub_arguments = sub_arguments.strip()

        if subcommand == "SEARCH":
            uids = self._search_uids(sub_arguments)
            suffix = "".join(f" {uid}" for uid in uids)
            self._send(connection, f"* SEARCH{suffix}\r\n")
            self._ok(connection, tag, "UID SEARCH completed")
        elif subcommand == "FETCH":
            uid_token, _, _spec = sub_arguments.partition(" ")
            try:
                uid = int(uid_token)
            except ValueError:
                self._send(connection, f"{tag} BAD invalid UID\r\n")
                return
            if 1 <= uid <= len(self.messages):
                raw = self.messages[uid - 1]
                header = (
                    f"* {uid} FETCH (UID {uid} RFC822.SIZE {len(raw)} "
                    f"BODY[] {{{len(raw)}}}\r\n"
                )
                self._send(connection, header.encode("ascii"))
                self._send(connection, raw)
                self._send(connection, b")\r\n")
            self._ok(connection, tag, "UID FETCH completed")
        else:
            self._send(connection, f"{tag} BAD unsupported UID command\r\n")

    def _search_uids(self, arguments: str) -> list[int]:
        all_uids = list(range(1, len(self.messages) + 1))
        match = re.search(r"UID\s+(\d+):(\d+|\*)", arguments, re.IGNORECASE)
        if match is None:
            return all_uids
        start = int(match.group(1))
        end_token = match.group(2)
        if end_token == "*":
            if not all_uids:
                return []
            end = all_uids[-1]
            if start > end:
                # RFC 3501: "n:*" still includes the last message.
                return [end]
        else:
            end = int(end_token)
        return [uid for uid in all_uids if start <= uid <= end]
