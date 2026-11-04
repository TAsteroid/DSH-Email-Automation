"""Test helpers.

Why this exists: inside the DSH sandbox ``tempfile.TemporaryDirectory`` makes
its directory with mode ``0o700``; on Windows that directory then refuses new
files (``PermissionError``) and SQLite cannot even create its database there.
The platform temp root is blocked for child processes as well.

So tests create directories under the workspace with the default mode, and
cleanup failures are swallowed (a leftover directory must never fail a test).
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

_WORKSPACE_TMP = Path(__file__).resolve().parents[2] / ".tmp" / "mail-core-tests"


class WorkspaceTempDir:
    """Drop-in subset of ``tempfile.TemporaryDirectory`` with the right permissions."""

    def __init__(self) -> None:
        _WORKSPACE_TMP.mkdir(parents=True, exist_ok=True)
        self.name = str(_WORKSPACE_TMP / f"tmp{uuid.uuid4().hex[:12]}")
        os.makedirs(self.name)

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exc_info: object) -> bool:
        self.cleanup()
        return False


def workspace_temporary_directory() -> WorkspaceTempDir:
    """A temporary directory a sandboxed child process may actually write to."""
    return WorkspaceTempDir()
