"""SQLite persistence for Mail Core.

Schema versioning uses ``PRAGMA user_version``; :meth:`Store.migrate` is
idempotent and runs on every connection.  ``password_blob`` is the only
secret column and it never appears in the account dictionaries returned to
the service layer (``hasCredential`` stands in for it).

Schema v2 added ``accounts.protocol`` (``imap`` | ``pop3``, default
``imap``) so an existing v1 database upgrades without losing its accounts.
Schema v3 adds the ``messages`` table (synced mail, one row per
account/folder/UIDVALIDITY/UID) with indexes for the list view.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

SCHEMA_VERSION = 3

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id             TEXT PRIMARY KEY,
    address        TEXT NOT NULL,
    display_name   TEXT NOT NULL DEFAULT '',
    imap_host      TEXT NOT NULL,
    imap_port      INTEGER NOT NULL DEFAULT 993,
    imap_security  TEXT NOT NULL DEFAULT 'ssl',
    smtp_host      TEXT NOT NULL DEFAULT '',
    smtp_port      INTEGER NOT NULL DEFAULT 465,
    smtp_security  TEXT NOT NULL DEFAULT 'ssl',
    username       TEXT NOT NULL,
    password_blob  TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_address ON accounts (address);

CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    delimiter   TEXT NOT NULL DEFAULT '',
    flags       TEXT NOT NULL DEFAULT '[]',
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS sync_state (
    account_id   TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    folder_name  TEXT NOT NULL DEFAULT '',
    uidvalidity  INTEGER,
    last_uid     INTEGER NOT NULL DEFAULT 0,
    last_sync_at TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'idle',
    error        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, folder_name)
);
"""


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Add ``accounts.protocol`` (default ``imap``) in a repeatable way.

    ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS`` in SQLite, so the
    column list is inspected first: running this migration twice (or on a
    database that somehow already has the column) is a no-op.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "protocol" in columns:
        return
    conn.execute(
        "ALTER TABLE accounts ADD COLUMN protocol TEXT NOT NULL DEFAULT 'imap'"
    )


_MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    folder      TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    uid         INTEGER NOT NULL,
    message_id  TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL DEFAULT '',
    from_addr   TEXT NOT NULL DEFAULT '',
    to_addr     TEXT NOT NULL DEFAULT '',
    cc_addr     TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL DEFAULT '',
    snippet     TEXT NOT NULL DEFAULT '',
    body_text   TEXT NOT NULL DEFAULT '',
    body_html   TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    seen        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (account_id, folder, uidvalidity, uid)
);

CREATE INDEX IF NOT EXISTS idx_messages_date ON messages (account_id, date);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages (account_id, message_id);
"""


MIGRATIONS: dict[int, Union[str, Callable[[sqlite3.Connection], None]]] = {
    1: _MIGRATION_1,
    2: _migrate_to_v2,
    3: _MIGRATION_3,
}


def _utc_now() -> str:
    """Protocol timestamp: ISO-8601 UTC with a ``Z`` suffix, second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """Thin wrapper over one SQLite connection (single-threaded service)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # -- lifecycle ---------------------------------------------------------

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    def migrate(self) -> int:
        """Bring the database up to :data:`SCHEMA_VERSION`; safe to repeat."""
        version = self.schema_version
        while version < SCHEMA_VERSION:
            target = version + 1
            migration = MIGRATIONS.get(target)
            if migration is None:
                raise RuntimeError(f"missing migration {target}")
            with self._conn:
                if callable(migration):
                    migration(self._conn)
                else:
                    self._conn.executescript(migration)
                self._conn.execute(f"PRAGMA user_version={target}")
            version = target
        return version

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    # -- accounts ----------------------------------------------------------

    @staticmethod
    def _account_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "address": row["address"],
            "displayName": row["display_name"],
            "protocol": row["protocol"] or "imap",
            "imapHost": row["imap_host"],
            "imapPort": int(row["imap_port"]),
            "imapSecurity": row["imap_security"],
            "smtpHost": row["smtp_host"],
            "smtpPort": int(row["smtp_port"]),
            "smtpSecurity": row["smtp_security"],
            "username": row["username"],
            "enabled": bool(row["enabled"]),
            "hasCredential": bool(row["password_blob"]),
            "createdAt": row["created_at"],
        }

    def list_accounts(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM accounts ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [self._account_to_dict(row) for row in rows]

    def get_account(self, account_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return self._account_to_dict(row) if row is not None else None

    def add_account(
        self,
        *,
        account_id: str,
        address: str,
        imap_host: str,
        imap_port: int,
        imap_security: str,
        username: str,
        display_name: str = "",
        protocol: str = "imap",
        smtp_host: str = "",
        smtp_port: int = 465,
        smtp_security: str = "ssl",
        password_blob: Optional[str] = None,
        enabled: bool = True,
        created_at: Optional[str] = None,
    ) -> str:
        timestamp = created_at or _utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO accounts (
                    id, address, display_name, protocol, imap_host, imap_port,
                    imap_security, smtp_host, smtp_port, smtp_security, username,
                    password_blob, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    address,
                    display_name,
                    protocol,
                    imap_host,
                    int(imap_port),
                    imap_security,
                    smtp_host,
                    int(smtp_port),
                    smtp_security,
                    username,
                    password_blob,
                    1 if enabled else 0,
                    timestamp,
                    timestamp,
                ),
            )
        return account_id

    def remove_account(self, account_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return cursor.rowcount > 0

    def set_credential(self, account_id: str, password_blob: Optional[str]) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET password_blob = ?, updated_at = ? WHERE id = ?",
                (password_blob, _utc_now(), account_id),
            )
        return cursor.rowcount > 0

    def get_credential(self, account_id: str) -> Optional[str]:
        """Return the stored DPAPI blob (base64) or ``None``; never plaintext."""
        row = self._conn.execute(
            "SELECT password_blob FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None or not row["password_blob"]:
            return None
        return str(row["password_blob"])

    def has_credential(self, account_id: str) -> bool:
        return self.get_credential(account_id) is not None

    def count_accounts(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        return int(row["n"])

    # -- folders -----------------------------------------------------------

    def replace_folders(self, account_id: str, folders: Iterable[dict]) -> int:
        """Replace the cached folder list of one account with ``folders``."""
        rows = []
        for folder in folders:
            name = str(folder.get("name", ""))
            delimiter = folder.get("delimiter")
            flags = folder.get("flags") or []
            rows.append(
                (
                    account_id,
                    name,
                    "" if delimiter is None else str(delimiter),
                    json.dumps(list(flags), ensure_ascii=False),
                )
            )
        with self._conn:
            self._conn.execute("DELETE FROM folders WHERE account_id = ?", (account_id,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO folders (account_id, name, delimiter, flags) VALUES (?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def list_folders(self, account_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM folders WHERE account_id = ? ORDER BY id ASC", (account_id,)
        ).fetchall()
        folders = []
        for row in rows:
            try:
                flags = json.loads(row["flags"]) if row["flags"] else []
            except json.JSONDecodeError:
                flags = []
            folders.append(
                {
                    "name": row["name"],
                    "delimiter": row["delimiter"],
                    "flags": list(flags),
                }
            )
        return folders

    def count_folders(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM folders").fetchone()
        return int(row["n"])

    # -- sync state --------------------------------------------------------

    def get_sync_state(self, account_id: str, folder_name: str = "") -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM sync_state WHERE account_id = ? AND folder_name = ?",
            (account_id, folder_name),
        ).fetchone()
        if row is None:
            return None
        return {
            "accountId": row["account_id"],
            "folderName": row["folder_name"],
            "uidvalidity": row["uidvalidity"],
            "lastUid": int(row["last_uid"]),
            "lastSyncAt": row["last_sync_at"],
            "status": row["status"],
            "error": row["error"],
        }

    def set_sync_state(
        self,
        account_id: str,
        folder_name: str = "",
        *,
        uidvalidity: Optional[int] = None,
        last_uid: int = 0,
        last_sync_at: Optional[str] = None,
        status: str = "idle",
        error: str = "",
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sync_state (
                    account_id, folder_name, uidvalidity, last_uid,
                    last_sync_at, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (account_id, folder_name) DO UPDATE SET
                    uidvalidity = excluded.uidvalidity,
                    last_uid = excluded.last_uid,
                    last_sync_at = excluded.last_sync_at,
                    status = excluded.status,
                    error = excluded.error
                """,
                (
                    account_id,
                    folder_name,
                    uidvalidity,
                    int(last_uid),
                    last_sync_at or "",
                    status,
                    error,
                ),
            )

    def latest_sync_at(self) -> str:
        row = self._conn.execute("SELECT MAX(last_sync_at) AS latest FROM sync_state").fetchone()
        if row is None or not row["latest"]:
            return ""
        return str(row["latest"])

    # -- messages ----------------------------------------------------------

    @staticmethod
    def _message_summary(row: sqlite3.Row) -> dict:
        """Protocol shape for ``message.list`` (never the body)."""
        return {
            "id": row["id"],
            "accountId": row["account_id"],
            "folder": row["folder"],
            "uid": int(row["uid"]),
            "subject": row["subject"],
            "fromAddr": row["from_addr"],
            "toAddr": row["to_addr"],
            "date": row["date"],
            "snippet": row["snippet"],
            "seen": bool(row["seen"]),
        }

    @classmethod
    def _message_detail(cls, row: sqlite3.Row) -> dict:
        """Protocol shape for ``message.get``: summary plus bodies and size."""
        detail = cls._message_summary(row)
        detail.update(
            {
                "ccAddr": row["cc_addr"],
                "text": row["body_text"],
                "html": row["body_html"],
                "size": int(row["size"]),
            }
        )
        return detail

    def insert_message(
        self,
        *,
        account_id: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        message_id: str = "",
        subject: str = "",
        from_addr: str = "",
        to_addr: str = "",
        cc_addr: str = "",
        date: str = "",
        snippet: str = "",
        body_text: str = "",
        body_html: str = "",
        size: int = 0,
        seen: bool = False,
        created_at: Optional[str] = None,
    ) -> bool:
        """Store one synced message; returns ``True`` only when it is new.

        ``INSERT OR IGNORE`` plus the ``UNIQUE (account_id, folder,
        uidvalidity, uid)`` constraint makes re-syncing the same message a
        no-op, so the boolean is exactly "rowcount == 1".
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    id, account_id, folder, uidvalidity, uid, message_id,
                    subject, from_addr, to_addr, cc_addr, date, snippet,
                    body_text, body_html, size, seen, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"msg_{uuid.uuid4().hex}",
                    account_id,
                    folder,
                    int(uidvalidity),
                    int(uid),
                    message_id,
                    subject,
                    from_addr,
                    to_addr,
                    cc_addr,
                    date,
                    snippet,
                    body_text,
                    body_html,
                    int(size),
                    1 if seen else 0,
                    created_at or _utc_now(),
                ),
            )
        return cursor.rowcount > 0

    def list_messages(
        self,
        *,
        account_id: Optional[str] = None,
        folder: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """Return ``(total, page)`` ordered by ``date`` descending."""
        clauses: list[str] = []
        args: list = []
        if account_id is not None:
            clauses.append("account_id = ?")
            args.append(account_id)
        if folder is not None:
            clauses.append("folder = ?")
            args.append(folder)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total_row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM messages{where}", args
        ).fetchone()
        total = int(total_row["n"])

        rows = self._conn.execute(
            f"""
            SELECT * FROM messages{where}
            ORDER BY date DESC, uid DESC, rowid DESC
            LIMIT ? OFFSET ?
            """,
            [*args, int(limit), int(offset)],
        ).fetchall()
        return total, [self._message_summary(row) for row in rows]

    def get_message(self, message_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return self._message_detail(row) if row is not None else None

    def delete_messages(self, account_id: str, folder: str) -> int:
        """Drop the cached rows of one folder (used when UIDVALIDITY changed)."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE account_id = ? AND folder = ?",
                (account_id, folder),
            )
        return cursor.rowcount

    def count_messages(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"])

    def count_unread_messages(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE seen = 0"
        ).fetchone()
        return int(row["n"])

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
