"""Entry point: ``python -m mail_core --data-dir <absolute path>``.

Reads one JSON request per stdin line, writes exactly one JSON response per
request to stdout and flushes it.  stdout carries protocol lines only;
diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional, TextIO

from . import backup, protocol, store
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
    """Force UTF-8 on the protocol pipes and on the log stream.

    stderr is UTF-8 as well, otherwise a Chinese Windows error message (for
    example the reason a pre-migration backup was refused) reaches the parent
    as cp936 bytes and shows up garbled.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream is sys.stdin:
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(
                    encoding="utf-8", errors="replace", newline="\n"
                )
        except (ValueError, OSError):
            # Pipes that cannot be reconfigured keep their defaults.
            pass


def serve(service: Service, stdin: TextIO, stdout: TextIO) -> None:
    """Run the request/response loop until EOF or ``shutdown``.

    Responses and unsolicited IDLE events share the service write lock, so a
    ``mail.new`` line can never interleave with a response line.
    """
    service.attach_stdout(stdout)
    for raw_line in stdin:
        response = protocol.handle_line(service, raw_line)
        if response is None:
            continue
        if not service.emit_response(response):
            return
        if service.shutdown_requested:
            return


def _read_user_version(db_path: Path) -> int:
    """Read ``PRAGMA user_version`` without triggering a migration."""
    try:
        connection = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        raise backup.BackupError(f"cannot open database {db_path}: {exc}") from None
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise backup.BackupError(
            f"cannot read schema version of {db_path}: {exc}"
        ) from None
    finally:
        connection.close()
    return int(row[0]) if row is not None else 0


def backup_before_migration(
    data_dir: str | Path, *, target_version: Optional[int] = None
) -> Optional[dict]:
    """Back up ``mail.db`` when it predates the current schema.

    Returns the backup metadata, or ``None`` when nothing needs protecting: a
    missing database, a zero-byte database, or a schema that is already
    current.  Raises :class:`mail_core.backup.BackupError` when an existing
    database cannot be backed up -- the caller must refuse to start rather
    than let a migration run on an unprotected database.
    """
    directory = Path(data_dir)
    db_path = directory / "mail.db"
    target = store.SCHEMA_VERSION if target_version is None else int(target_version)

    if not db_path.exists():
        return None
    try:
        if db_path.stat().st_size == 0:
            return None
    except OSError as exc:
        raise backup.BackupError(f"cannot inspect database {db_path}: {exc}") from None

    current = _read_user_version(db_path)
    if current >= target:
        return None

    try:
        result = backup.create_backup(db_path, directory / backup.BACKUP_DIRNAME)
    except backup.BackupError as exc:
        raise backup.BackupError(
            "refusing to migrate an unprotected database "
            f"(user_version {current} -> {target}): {exc.message}"
        ) from None
    _log(
        f"pre-migration backup: user_version {current} -> {target}, "
        f"saved {result['path']}"
    )
    return result


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
        backup_before_migration(data_dir)
    except Exception as exc:
        _log(f"startup backup failed, refusing to start: {exc}")
        return 1

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
