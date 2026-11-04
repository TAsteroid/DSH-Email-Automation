"""Entry point: ``python -m mail_core --data-dir <absolute path>``.

Reads one JSON request per stdin line, writes exactly one JSON response per
request to stdout and flushes it.  stdout carries protocol lines only;
diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, TextIO

from . import protocol
from .service import Service

DATA_DIR_ENV = "MAIL_CORE_DATA_DIR"
DEFAULT_DATA_DIR = os.path.join(".", "data", "mail")


def _log(message: str) -> None:
    print(f"[mail-core] {message}", file=sys.stderr, flush=True)


def resolve_data_dir(explicit: Optional[str] = None) -> str:
    """``--data-dir`` > ``MAIL_CORE_DATA_DIR`` > ``./data/mail``."""
    if explicit:
        return explicit
    from_env = os.environ.get(DATA_DIR_ENV)
    if from_env:
        return from_env
    return DEFAULT_DATA_DIR


def _configure_streams() -> None:
    """Force UTF-8 and ``\\n`` line endings on the protocol pipes."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream is sys.stdin:
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", newline="\n")
        except (ValueError, OSError):
            # Pipes that cannot be reconfigured keep their defaults.
            pass


def serve(service: Service, stdin: TextIO, stdout: TextIO) -> None:
    """Run the request/response loop until EOF or ``shutdown``."""
    for raw_line in stdin:
        response = protocol.handle_line(service, raw_line)
        if response is None:
            continue
        try:
            stdout.write(protocol.dumps(response))
            stdout.write("\n")
            stdout.flush()
        except (BrokenPipeError, ValueError, OSError):
            return
        if service.shutdown_requested:
            return


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mail_core",
        description="DSH Mail Core stdio service (protocol v1)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=f"data directory holding mail.db (default: ${DATA_DIR_ENV} or ./data/mail)",
    )
    args = parser.parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    _configure_streams()

    try:
        service = Service(data_dir)
    except Exception as exc:
        _log(f"startup failed: {type(exc).__name__}: {exc}")
        return 1

    _log(f"ready (data-dir={service.data_dir})")
    try:
        serve(service, sys.stdin, sys.stdout)
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
