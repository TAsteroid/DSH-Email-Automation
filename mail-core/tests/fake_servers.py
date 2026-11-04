"""Loopback fake servers for the receive/send protocol probe tests.

POP3 and SMTP are tested against real sockets on ``127.0.0.1`` so the clients
run their actual code paths (``poplib`` / ``smtplib``) without leaving the
machine.  Each server records the commands it received, which lets a test
assert e.g. that the SMTP probe never issues ``MAIL FROM``.
"""

from __future__ import annotations

import socket
import threading


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
    """Speaks just enough POP3 for ``USER`` / ``PASS`` / ``STAT`` / ``QUIT``."""

    def __init__(self, *, messages: int = 3, total_size: int = 4096) -> None:
        super().__init__()
        self.messages = int(messages)
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
