"""On-disk SQLite backups for the Mail Core database.

A backup is produced with SQLite's online backup API into a temporary file in
the target directory and then renamed into place, so a crash or a failed copy
never leaves a half-written ``mail-*.db`` behind.  Backup names are
``mail-YYYYMMDD-HHMMSS.db`` in UTC (matching ``createdAt``); a second backup
inside the same second gets a ``-2`` / ``-3`` suffix instead of overwriting.

Only ``mail-*.db`` files are listed or pruned: anything else in the backup
directory (including orphaned ``.tmp`` files) is left alone.
"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .protocol import INTERNAL, MailCoreError

BACKUP_DIRNAME = "backups"
DEFAULT_KEEP = 10

_NAME_RE = re.compile(
    r"^mail-(?P<date>\d{8})-(?P<time>\d{6})(?:-(?P<seq>\d+))?\.db$"
)

# Refuse to scan forever if somebody drops thousands of same-second backups.
_MAX_SUFFIX = 999


class BackupError(MailCoreError):
    """Backup could not be created; maps to the ``INTERNAL`` protocol code."""

    def __init__(self, message: str) -> None:
        super().__init__(INTERNAL, message)


def _as_utc(now: Optional[datetime]) -> datetime:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unique_target(backup_dir: Path, name: str) -> Path:
    """Pick ``name``, or the first free ``-N`` variant in the same second."""
    target = backup_dir / name
    if not target.exists():
        return target
    stem = name[: -len(".db")]
    for suffix in range(2, _MAX_SUFFIX + 1):
        candidate = backup_dir / f"{stem}-{suffix}.db"
        if not candidate.exists():
            return candidate
    raise BackupError(f"too many backups for {name} in {backup_dir}")


def _parse_name(name: str) -> Optional[tuple[str, int]]:
    """Return ``(createdAt ISO Z, sequence)`` for one backup filename."""
    match = _NAME_RE.match(name)
    if match is None:
        return None
    try:
        created = datetime.strptime(
            match.group("date") + match.group("time"), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    sequence = int(match.group("seq") or 1)
    return _iso_utc(created), sequence


def create_backup(
    db_path: str | Path,
    backup_dir: str | Path,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Copy ``db_path`` into ``backup_dir`` and return its metadata.

    Returns ``{"path": str, "size": int, "createdAt": ISO-8601 UTC}``.
    Raises :class:`BackupError` on any failure; a partially written temporary
    file is removed before the error propagates.
    """
    source_path = Path(db_path)
    if not source_path.exists():
        raise BackupError(f"database not found: {source_path}")
    if not source_path.is_file():
        raise BackupError(f"database path is not a file: {source_path}")

    target_dir = Path(backup_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create backup directory {target_dir}: {exc}") from None
    if not target_dir.is_dir():
        raise BackupError(f"backup path is not a directory: {target_dir}")

    stamp = _as_utc(now)
    target = _unique_target(target_dir, f"mail-{stamp:%Y%m%d-%H%M%S}.db")
    temporary = target_dir / (
        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    )

    source = None
    destination = None
    try:
        source = sqlite3.connect(str(source_path))
        destination = sqlite3.connect(str(temporary))
        source.backup(destination)
        destination.close()
        destination = None
        os.replace(temporary, target)
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"backup failed: {exc}") from None
    finally:
        if destination is not None:
            try:
                destination.close()
            except sqlite3.Error:
                pass
        if source is not None:
            try:
                source.close()
            except sqlite3.Error:
                pass
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise BackupError(f"backup written but cannot stat {target}: {exc}") from None
    return {"path": str(target), "size": size, "createdAt": _iso_utc(stamp)}


def list_backups(backup_dir: str | Path) -> list[dict]:
    """List ``mail-*.db`` backups, newest first."""
    target_dir = Path(backup_dir)
    if not target_dir.is_dir():
        return []
    entries: list[tuple[str, int, str, str, int]] = []
    try:
        candidates = list(target_dir.iterdir())
    except OSError:
        return []
    for path in candidates:
        parsed = _parse_name(path.name)
        if parsed is None:
            continue
        created_at, sequence = parsed
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        entries.append((created_at, sequence, path.name, str(path), size))
    entries.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [
        {"path": path, "size": size, "createdAt": created_at}
        for created_at, _sequence, _name, path, size in entries
    ]


def prune_backups(backup_dir: str | Path, keep: int = DEFAULT_KEEP) -> list[str]:
    """Delete all but the newest ``keep`` backups; returns the removed paths."""
    try:
        keep_count = max(0, int(keep))
    except (TypeError, ValueError):
        keep_count = DEFAULT_KEEP
    removed: list[str] = []
    for entry in list_backups(backup_dir)[keep_count:]:
        try:
            Path(entry["path"]).unlink()
        except OSError:
            continue
        removed.append(entry["path"])
    return removed
