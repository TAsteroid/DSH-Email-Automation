"""SQLite persistence for Mail Core.

Schema versioning uses ``PRAGMA user_version``; :meth:`Store.migrate` is
idempotent and runs on every connection.  ``password_blob`` is the only
secret column and it never appears in the account dictionaries returned to
the service layer (``hasCredential`` stands in for it).

Schema v2 added ``accounts.protocol`` (``imap`` | ``pop3``, default
``imap``) so an existing v1 database upgrades without losing its accounts.
Schema v3 adds the ``messages`` table (synced mail, one row per
account/folder/UIDVALIDITY/UID) with indexes for the list view.
Schema v4 adds the local address book (``contacts``), message templates
(``templates``, deliberately never synced to a mail server) and
``messages_fts``, an FTS5 external-content index over ``messages`` kept in
sync by triggers; the migration rebuilds it from the rows already on disk,
so a v3 mailbox becomes searchable without resyncing mail.
Schema v5 adds the approval-bound send pipeline: ``drafts`` (one row per
editable draft, with the content hash the approval is bound to),
``approvals`` (a send request with a TTL) and ``mail_actions`` (the immutable
execute log, keyed by an idempotency key).  Schema v6 adds the approval audit
columns ``approvals.decided_via`` / ``approvals.decided_by`` so a decision can
be attributed to the host prompt, a policy auto-approval or the API, and to a
user or an agent.  Every migration is additive and repeatable, so a v5
mailbox upgrades in place.  Schema v7 adds conversation threading to
``messages``: ``thread_id`` (account-scoped ``<accountId>:<rootMessageId>``)
plus the ``in_reply_to`` / ``references`` headers it is derived from; the v7
upgrade backfills ``thread_id`` for mail synced before threading existed.
Schema v8 adds attachment metadata (``attachments``, one row per MIME part,
cascade-deleted with its message) and ``messages.attachments_synced`` so
pre-v8 mail can be re-parsed for attachments without refetching it twice.
Schema v9 adds the follow-up state (``messages.todo_state`` /
``messages.todo_updated_at``, ``accounts.todo_baseline_done``) and the
append-only Agent ``summaries`` table, cascade-deleted with its message.
Schema v10 adds ``contact_sessions``, the persisted mailbox-address -> DSH
session mapping used to reuse one conversation per sender across restarts.
"""

from __future__ import annotations

import email.utils
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from . import threads

SCHEMA_VERSION = 10

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


# Schema v4: local address book, templates and full-text search.
#
# ``messages_fts`` is an FTS5 *external content* table: the indexed columns
# are read straight from ``messages`` (``content='messages'``,
# ``content_rowid='rowid'``) instead of being copied, so there is one source
# of truth and no duplicated body text on disk.  The three triggers keep the
# index in step for rows written, updated or deleted by the sync layer; the
# final ``rebuild`` re-indexes everything already present (an upgrade from v3
# has messages on disk but no index yet).  The default ``unicode61``
# tokenizer treats a run of CJK characters as one token, which is why the
# service layer quotes every term of a search instead of passing the raw
# string to the FTS parser.
#
# Because the index is joined to ``messages`` by ``rowid``, those rowids must
# never be renumbered: ``VACUUM`` may reassign the rowid of a table whose
# primary key is not an ``INTEGER PRIMARY KEY`` (``messages.id`` is TEXT).
# If a VACUUM ever runs, follow it with
# ``INSERT INTO messages_fts(messages_fts) VALUES('rebuild')``.
_MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS contacts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    company    TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts (name);

CREATE TABLE IF NOT EXISTS templates (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject,
    body_text,
    from_addr,
    to_addr,
    content='messages',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, subject, body_text, from_addr, to_addr)
    VALUES (new.rowid, new.subject, new.body_text, new.from_addr, new.to_addr);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, subject, body_text, from_addr, to_addr)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.from_addr, old.to_addr);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, subject, body_text, from_addr, to_addr)
    VALUES ('delete', old.rowid, old.subject, old.body_text, old.from_addr, old.to_addr);
    INSERT INTO messages_fts (rowid, subject, body_text, from_addr, to_addr)
    VALUES (new.rowid, new.subject, new.body_text, new.from_addr, new.to_addr);
END;

INSERT INTO messages_fts (messages_fts) VALUES ('rebuild');
"""


# Schema v5: drafts, approvals and the execute log.
#
# ``drafts.content_hash`` is recomputed by ``draft.save`` on every edit; an
# approval stores the hash it was granted for, so editing the draft after
# approval makes the approval unusable (``HASH_MISMATCH``) without touching
# the approval row.  Deleting a draft cascades its approvals; the
# ``mail_actions`` log deliberately has no foreign keys so the record that a
# message was actually sent survives draft cleanup.  ``idempotency_key`` is
# UNIQUE, which is what guarantees one approval can be executed only once.
_MIGRATION_5 = """
CREATE TABLE IF NOT EXISTS drafts (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    to_addr      TEXT NOT NULL DEFAULT '',
    cc_addr      TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_account ON drafts (account_id);

CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    draft_id     TEXT NOT NULL REFERENCES drafts (id) ON DELETE CASCADE,
    action       TEXT NOT NULL DEFAULT 'send',
    content_hash TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    decided_at   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals (status);

CREATE TABLE IF NOT EXISTS mail_actions (
    id              TEXT PRIMARY KEY,
    approval_id     TEXT NOT NULL,
    draft_id        TEXT NOT NULL,
    action          TEXT NOT NULL DEFAULT 'send',
    result          TEXT NOT NULL,
    error           TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    executed_at     TEXT NOT NULL
);
"""


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    """Add the approval audit columns (``decided_via`` / ``decided_by``).

    ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS``, so the column
    list is inspected first; running the migration twice (or on a database
    that already has the columns) is a no-op.  Existing decisions keep the
    empty default: a v5 approval has no recorded provenance, and inventing
    one would be worse than admitting the gap.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(approvals)")}
    if "decided_via" not in columns:
        conn.execute(
            "ALTER TABLE approvals ADD COLUMN decided_via TEXT NOT NULL DEFAULT ''"
        )
    if "decided_by" not in columns:
        conn.execute(
            "ALTER TABLE approvals ADD COLUMN decided_by TEXT NOT NULL DEFAULT ''"
        )


# Schema v7: conversation threading.  ``thread_id`` is ``<accountId>:<root>``
# so two accounts holding the same Message-ID never share a thread; the two
# raw header columns keep enough information to recompute (and therefore
# backfill) that id without refetching mail.  The add-column statements are
# guarded by a PRAGMA inspection because ``ADD COLUMN`` has no
# ``IF NOT EXISTS``; the indexes and the backfill in ``Store.migrate`` are
# repeatable.
def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    additions = (
        ("thread_id", "TEXT NOT NULL DEFAULT ''"),
        ("in_reply_to", "TEXT NOT NULL DEFAULT ''"),
        # ``references`` is a SQL keyword (ADD COLUMN REFERENCES is parsed as
        # a foreign-key clause), hence the shortened column name.
        ("refs", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread "
        "ON messages (account_id, thread_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread_date "
        "ON messages (thread_id, date)"
    )


# Schema v8: attachment metadata only -- file bytes are downloaded on demand
# into ``<data-dir>/attachments`` and never stored in SQLite.  ``part_id`` is
# the IMAP body section (``2`` / ``1.3``) used by ``attachment.fetch`` when
# the user asks for the file.  ``attachments_synced`` marks a message whose
# attachment list has been extracted; rows from an older schema start at 0
# so ``attachment.backfill`` knows what still needs a refetch.
def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id           TEXT PRIMARY KEY,
            message_id   TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
            filename     TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT '',
            size         INTEGER NOT NULL DEFAULT 0,
            part_id      TEXT NOT NULL DEFAULT '',
            inline       INTEGER NOT NULL DEFAULT 0,
            sha256       TEXT NOT NULL DEFAULT '',
            local_path   TEXT NOT NULL DEFAULT '',
            fetched_at   TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachments_message "
        "ON attachments (message_id)"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "attachments_synced" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN attachments_synced "
            "INTEGER NOT NULL DEFAULT 0"
        )


# Schema v9: follow-up state and Agent summaries.  ``messages.todo_state`` is
# the per-message follow-up flag (``''`` = never registered, ``pending`` =
# needs attention, ``done`` = handled); ``todo_updated_at`` records the last
# change.  ``accounts.todo_baseline_done`` marks the first normal INBOX sync:
# its messages are a baseline (not "new mail") and only later arrivals are
# auto-registered as pending.  ``summaries`` is append-only Agent output bound
# to a message; key points and entities are JSON text so the schema never has
# to change when the Agent enriches them.
def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    for name, definition in (
        ("todo_state", "TEXT NOT NULL DEFAULT ''"),
        ("todo_updated_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in message_columns:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

    account_columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "todo_baseline_done" not in account_columns:
        conn.execute(
            "ALTER TABLE accounts ADD COLUMN todo_baseline_done "
            "INTEGER NOT NULL DEFAULT 0"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            id         TEXT PRIMARY KEY,
            message_id TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
            thread_id  TEXT NOT NULL DEFAULT '',
            category   TEXT NOT NULL DEFAULT '',
            summary    TEXT NOT NULL,
            key_points TEXT NOT NULL DEFAULT '[]',
            entities   TEXT NOT NULL DEFAULT '{}',
            model      TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_message "
        "ON summaries (message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_thread "
        "ON summaries (thread_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_category "
        "ON summaries (category)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_todo "
        "ON messages (todo_state, date)"
    )


# Schema v10: mailbox-address -> DSH session mapping.  One row per
# (account, email) so the same sender keeps reusing their session even after
# DSH restarts; ``email`` is always stored lowercased by the service layer.
# The account foreign key cascades so deleting an account drops its mappings.
def _migrate_to_v10(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_sessions (
            id           TEXT PRIMARY KEY,
            account_id   TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
            email        TEXT NOT NULL,
            session_id   TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT '',
            title        TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            UNIQUE (account_id, email)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_sessions_email "
        "ON contact_sessions (email)"
    )


MIGRATIONS: dict[int, Union[str, Callable[[sqlite3.Connection], None]]] = {
    1: _MIGRATION_1,
    2: _migrate_to_v2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
    8: _migrate_to_v8,
    9: _migrate_to_v9,
    10: _migrate_to_v10,
}


# Columns of ``contacts`` a caller may change through ``contact.update``;
# iterating this tuple (instead of trusting request keys) keeps the SQL free
# of caller-controlled identifiers.
CONTACT_FIELDS = ("name", "email", "phone", "company", "title", "notes")

# Draft/approval state machines.  The store never validates a caller's value,
# but these tuples document the only statuses the service layer produces.
DRAFT_STATUSES = ("draft", "approved", "sent", "rejected")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")

# Approval audit values (schema v6).  The empty string means "not recorded"
# and is always accepted; it is what a v5 row or a bare ``approval.approve``
# call carries.
DECIDED_VIA_VALUES = ("prompt", "policy-never", "api")
DECIDED_BY_VALUES = ("user", "agent")

# Follow-up states (schema v9).  The empty string means "not registered"
# (baseline sync or mail imported by a backfill); the today list defaults to
# ``pending``.
TODO_STATES = ("", "pending", "done")

# Fixed Agent summary categories (schema v9); validated by the service layer.
SUMMARY_CATEGORIES = (
    "报价询价",
    "合同协议",
    "会议安排",
    "项目进展",
    "发票付款",
    "人事招聘",
    "通知公告",
    "其他",
)

# Protocol timestamps are fixed-width UTC, so plain string comparison is also
# chronological (``now >= expires_at`` is the approval expiry test).
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# ``snippet()`` arguments shared by every search: highlight the matched term,
# elide with ``…`` and keep the excerpt short (12 tokens) so a hit never
# returns a whole body.
_FTS_SNIPPET = "snippet(messages_fts, -1, '«', '»', '…', 12)"


def _fts_query(query: str) -> str:
    """Turn free user input into a safe FTS5 ``AND`` query.

    FTS5 has its own query grammar (``*``, ``-``, ``OR``, ``NEAR(...)``,
    quotes, parentheses) and raises ``OperationalError`` on input that does
    not parse.  Splitting on whitespace and wrapping every term in a quoted
    string, with embedded quotes doubled, makes any input a literal phrase:
    ``合同 报表`` becomes ``"合同" "报表"`` (implicit AND).  Terms with no
    letter or digit carry no token for the tokenizer and are dropped; an
    input made only of punctuation therefore yields an empty query, which
    callers treat as "no hits" rather than sending ``MATCH ''``.
    """
    terms: list[str] = []
    for raw in _search_terms(query):
        terms.append('"' + raw.replace('"', '""') + '"')
    return " ".join(terms)


# FTS5's unicode61 tokenizer treats a run of CJK characters as ONE token, so
# searching 合同 never matches 合同确认书 — useless for Chinese mail.  Queries
# containing CJK therefore take a substring (LIKE) path; ASCII queries keep the
# faster FTS index.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def _search_terms(query: str) -> list[str]:
    """Whitespace terms that carry a character to match on.

    Surrounding quotes/parentheses are FTS syntax for users, not content, so
    they are stripped before either search path sees the term.
    """
    terms: list[str] = []
    for raw in str(query).split():
        cleaned = raw.strip('"\'()（）「」『』')
        if any(character.isalnum() for character in cleaned):
            terms.append(cleaned)
    return terms


def _like_pattern(term: str) -> str:
    """LIKE pattern with user wildcards escaped (paired with ``ESCAPE '\\'``)."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _highlight(text: str, term: str, width: int = 40) -> str:
    """Substring excerpt with the hit wrapped in ``«»`` (matches the FTS path)."""
    source = text or ""
    index = source.lower().find(term.lower())
    if index < 0:
        return source[: width * 2]
    start = max(0, index - width)
    end = min(len(source), index + len(term) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(source) else ""
    return (
        f"{prefix}{source[start:index]}"
        f"«{source[index:index + len(term)]}»"
        f"{source[index + len(term):end]}{suffix}"
    )


def _snippet_for(row, term: str) -> str:
    """Highlight the term in whichever field actually contains it."""
    for field in ("subject", "body_text", "from_addr", "to_addr"):
        try:
            text = row[field] or ""
        except (IndexError, KeyError):
            continue
        if term.lower() in text.lower():
            return _highlight(text, term)
    try:
        return (row["body_text"] or row["subject"] or "")[:80]
    except (IndexError, KeyError):
        return ""


def utc_now() -> str:
    """Protocol timestamp: ISO-8601 UTC with a ``Z`` suffix, second precision."""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def add_minutes(timestamp: str, minutes: int) -> str:
    """Shift one protocol timestamp by ``minutes`` (used for approval TTLs)."""
    moment = datetime.strptime(timestamp, TIMESTAMP_FORMAT).replace(
        tzinfo=timezone.utc
    )
    return (moment + timedelta(minutes=int(minutes))).strftime(TIMESTAMP_FORMAT)


class Store:
    """Thin wrapper over one SQLite connection (shared by the service threads)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The IDLE worker thread runs the IDLE-triggered sync, so the
        # connection is created in one thread and used from another.  Safety
        # comes from Service._lock (RLock), which serialises every RPC handler
        # and the IDLE callback, never from sqlite3's per-thread check.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
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
        if version >= 7:
            # A v6 mailbox has messages but no thread ids.  Recompute them once
            # (repeatable: with nothing left to fill this is a cheap no-op).
            self.backfill_threads()
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
        timestamp = created_at or utc_now()
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
                (password_blob, utc_now(), account_id),
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

    def get_todo_baseline_done(self, account_id: str) -> bool:
        """Whether the account's first normal INBOX sync has been recorded."""
        row = self._conn.execute(
            "SELECT todo_baseline_done FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return bool(row["todo_baseline_done"]) if row is not None else False

    def set_todo_baseline_done(self, account_id: str, done: bool = True) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET todo_baseline_done = ? WHERE id = ?",
                (1 if done else 0, account_id),
            )
        return cursor.rowcount > 0

    # -- contact sessions (mailbox address -> DSH session) ----------------

    @staticmethod
    def _contact_session_to_dict(row: sqlite3.Row) -> dict:
        return {
            "accountId": row["account_id"],
            "email": row["email"],
            "sessionId": row["session_id"],
            "workspaceId": row["workspace_id"],
            "title": row["title"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def get_contact_session(
        self,
        *,
        account_id: Optional[str] = None,
        email: str,
    ) -> Optional[dict]:
        """Look one mapping up by (account, email), or by email across accounts.

        The email is the caller-normalised (lowercased) value; without an
        account the most recently updated mapping for that address wins.
        """
        if account_id is None:
            row = self._conn.execute(
                "SELECT * FROM contact_sessions WHERE email = ? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (email,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM contact_sessions WHERE account_id = ? AND email = ?",
                (account_id, email),
            ).fetchone()
        return self._contact_session_to_dict(row) if row is not None else None

    def set_contact_session(
        self,
        *,
        account_id: str,
        email: str,
        session_id: str,
        workspace_id: Optional[str] = None,
        title: Optional[str] = None,
        now: Optional[str] = None,
    ) -> dict:
        """Upsert one mapping; omitted metadata keeps the stored value.

        The UNIQUE (account_id, email) constraint is what keeps exactly one
        row per sender per account, and ``updated_at`` moves on every write.
        """
        existing = self.get_contact_session(account_id=account_id, email=email) or {}
        workspace = (
            workspace_id
            if workspace_id is not None
            else str(existing.get("workspaceId") or "")
        )
        display_title = (
            title if title is not None else str(existing.get("title") or "")
        )
        timestamp = now or utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO contact_sessions (
                    id, account_id, email, session_id, workspace_id,
                    title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (account_id, email) DO UPDATE SET
                    session_id = excluded.session_id,
                    workspace_id = excluded.workspace_id,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (
                    f"csn_{uuid.uuid4().hex}",
                    account_id,
                    email,
                    session_id,
                    workspace,
                    display_title,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_contact_session(account_id=account_id, email=email) or {}

    def remove_contact_session(self, *, account_id: str, email: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM contact_sessions WHERE account_id = ? AND email = ?",
                (account_id, email),
            )
        return cursor.rowcount > 0

    def list_contact_sessions(
        self, account_id: Optional[str] = None
    ) -> list[dict]:
        """Mappings ordered by most recent update, then email."""
        if account_id is None:
            rows = self._conn.execute(
                "SELECT * FROM contact_sessions "
                "ORDER BY updated_at DESC, email ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM contact_sessions WHERE account_id = ? "
                "ORDER BY updated_at DESC, email ASC",
                (account_id,),
            ).fetchall()
        return [self._contact_session_to_dict(row) for row in rows]

    def count_contact_sessions(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM contact_sessions"
        ).fetchone()
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
            "threadId": row["thread_id"],
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

    def _thread_lookup(self, account_id: str, reference: str) -> Optional[str]:
        """Thread of the oldest local message carrying ``reference`` as msg-id."""
        row = self._conn.execute(
            "SELECT thread_id FROM messages "
            "WHERE account_id = ? AND message_id = ? AND thread_id <> '' "
            "ORDER BY date ASC, rowid ASC LIMIT 1",
            (account_id, reference),
        ).fetchone()
        return str(row["thread_id"]) if row is not None else None

    def resolve_thread_id(
        self,
        account_id: str,
        message_id: str,
        references: str = "",
        in_reply_to: str = "",
    ) -> str:
        """Apply the thread merge rules against the rows already stored."""
        return threads.resolve_thread_id(
            account_id,
            message_id,
            references,
            in_reply_to,
            lambda reference: self._thread_lookup(account_id, reference),
        )

    def backfill_threads(self) -> int:
        """Fill ``thread_id`` (and missing ``message_id``) for existing rows.

        Rows are walked from oldest to newest so a reply usually finds its
        root already assigned.  A single dictionary of
        ``(account, message_id) -> thread_id`` is loaded up front, which keeps
        the whole backfill linear instead of one query per reference.  Safe to
        repeat: the second run finds no empty ``thread_id`` and returns 0.
        """
        try:
            pending = self._conn.execute(
                "SELECT id, account_id, message_id, in_reply_to, refs "
                "FROM messages WHERE thread_id = '' ORDER BY date ASC, rowid ASC"
            ).fetchall()
        except sqlite3.OperationalError:  # no messages table (never in practice)
            return 0
        if not pending:
            return 0

        known: dict[tuple[str, str], str] = {}
        for row in self._conn.execute(
            "SELECT account_id, message_id, thread_id FROM messages "
            "WHERE thread_id <> ''"
        ):
            if not row["message_id"]:
                continue
            known.setdefault(
                (row["account_id"], row["message_id"]), str(row["thread_id"])
            )

        updates: list[tuple[str, str, str]] = []
        for row in pending:
            account_id = str(row["account_id"])
            message_id = str(row["message_id"] or "") or f"<{row['id']}@mail-core>"
            chain = threads.reference_chain(
                row["refs"] or "", row["in_reply_to"] or ""
            )
            thread_id = ""
            for reference in chain:
                existing = known.get((account_id, reference))
                if existing:
                    thread_id = existing
                    break
            if not thread_id:
                root = chain[0] if chain else message_id
                thread_id = threads.thread_key(account_id, root)
            known.setdefault((account_id, message_id), thread_id)
            updates.append((thread_id, message_id, str(row["id"])))

        with self._conn:
            self._conn.executemany(
                "UPDATE messages SET thread_id = ?, message_id = ? WHERE id = ?",
                updates,
            )
        return len(updates)

    def insert_message(
        self,
        *,
        account_id: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        message_id: str = "",
        in_reply_to: str = "",
        references: str = "",
        thread_id: str = "",
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
        attachments: Optional[Iterable[dict]] = None,
        todo_state: str = "",
        created_at: Optional[str] = None,
    ) -> bool:
        """Store one synced message; returns ``True`` only when it is new.

        ``INSERT OR IGNORE`` plus the ``UNIQUE (account_id, folder,
        uidvalidity, uid)`` constraint makes re-syncing the same message a
        no-op, so the boolean is exactly "rowcount == 1".  ``thread_id`` is
        computed from the references against the rows already stored when the
        caller does not pass one.  ``attachments`` (the MIME metadata from
        :func:`mail_core.mime_parse.parse_message`) is written in the same
        transaction and marks the message as attachment-synced.
        ``todo_state`` is ``pending`` only for mail that arrived after the
        account's baseline sync (see ``sync``); everything else, including
        backfill imports, leaves it empty.
        """
        row_id = f"msg_{uuid.uuid4().hex}"
        if not message_id:
            # A mail without Message-ID still needs a stable identity for
            # threading; tie it to the row id, which lives as long as the row.
            message_id = f"<{row_id}@mail-core>"
        if not thread_id:
            thread_id = self.resolve_thread_id(
                account_id, message_id, references, in_reply_to
            )
        timestamp = created_at or utc_now()
        todo_updated_at = timestamp if todo_state else ""
        attachment_rows = self._attachment_rows(
            row_id, attachments or (), timestamp
        )
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    id, account_id, folder, uidvalidity, uid, message_id,
                    thread_id, in_reply_to, refs,
                    subject, from_addr, to_addr, cc_addr, date, snippet,
                    body_text, body_html, size, seen, attachments_synced,
                    todo_state, todo_updated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    account_id,
                    folder,
                    int(uidvalidity),
                    int(uid),
                    message_id,
                    thread_id,
                    in_reply_to,
                    references,
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
                    1,
                    todo_state,
                    todo_updated_at,
                    timestamp,
                ),
            )
            if cursor.rowcount > 0 and attachment_rows:
                self._conn.executemany(
                    """
                    INSERT INTO attachments (
                        id, message_id, filename, content_type, size,
                        part_id, inline, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    attachment_rows,
                )
        return cursor.rowcount > 0

    @staticmethod
    def _attachment_rows(
        message_id: str,
        attachments: Iterable[dict],
        created_at: str,
    ) -> list[tuple]:
        rows: list[tuple] = []
        for attachment in attachments:
            rows.append(
                (
                    f"att_{uuid.uuid4().hex}",
                    message_id,
                    str(attachment.get("filename", "") or ""),
                    str(attachment.get("contentType", "") or ""),
                    int(attachment.get("size", 0) or 0),
                    str(attachment.get("partId", "") or ""),
                    1 if attachment.get("inline") else 0,
                    created_at,
                )
            )
        return rows

    def insert_attachments(
        self,
        message_id: str,
        attachments: Iterable[dict],
        *,
        created_at: Optional[str] = None,
    ) -> int:
        """Insert attachment metadata for an existing message (idempotent-ish)."""
        rows = self._attachment_rows(
            message_id, attachments, created_at or utc_now()
        )
        with self._conn:
            if rows:
                self._conn.executemany(
                    """
                    INSERT INTO attachments (
                        id, message_id, filename, content_type, size,
                        part_id, inline, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            self._conn.execute(
                "UPDATE messages SET attachments_synced = 1 WHERE id = ?",
                (message_id,),
            )
        return len(rows)

    def replace_attachments(
        self,
        message_id: str,
        attachments: Iterable[dict],
        *,
        created_at: Optional[str] = None,
    ) -> int:
        """Replace the attachment list of a message (used by backfill)."""
        rows = self._attachment_rows(
            message_id, attachments, created_at or utc_now()
        )
        with self._conn:
            self._conn.execute(
                "DELETE FROM attachments WHERE message_id = ?", (message_id,)
            )
            if rows:
                self._conn.executemany(
                    """
                    INSERT INTO attachments (
                        id, message_id, filename, content_type, size,
                        part_id, inline, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            self._conn.execute(
                "UPDATE messages SET attachments_synced = 1 WHERE id = ?",
                (message_id,),
            )
        return len(rows)

    def mark_attachments_synced(self, message_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE messages SET attachments_synced = 1 WHERE id = ?",
                (message_id,),
            )
        return cursor.rowcount > 0

    def pending_attachment_messages(
        self,
        account_id: str,
        folder: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Messages whose attachment metadata has not been extracted yet."""
        clauses = ["account_id = ?", "attachments_synced = 0"]
        args: list = [account_id]
        if folder is not None:
            clauses.append("folder = ?")
            args.append(folder)
        rows = self._conn.execute(
            f"""
            SELECT id, account_id, folder, uidvalidity, uid
            FROM messages WHERE {' AND '.join(clauses)}
            ORDER BY date DESC, rowid DESC LIMIT ?
            """,
            [*args, int(limit)],
        ).fetchall()
        return [
            {
                "id": row["id"],
                "accountId": row["account_id"],
                "folder": row["folder"],
                "uidvalidity": int(row["uidvalidity"]),
                "uid": int(row["uid"]),
            }
            for row in rows
        ]

    # -- attachment metadata -----------------------------------------------

    @staticmethod
    def _attachment_list_item(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "messageId": row["message_id"],
            "subject": row["subject"],
            "fromAddr": row["from_addr"],
            "filename": row["filename"],
            "contentType": row["content_type"],
            "size": int(row["size"]),
            "fetchedAt": row["fetched_at"],
        }

    def list_attachments(
        self,
        *,
        message_id: Optional[str] = None,
        account_id: Optional[str] = None,
        pending_only: bool = False,
        include_inline: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        """Attachment metadata joined with its message, newest mail first.

        Inline resources (HTML images, calendar parts) are not shown by
        default: they are not "attachments to analyse", even though their
        metadata is stored so ``attachment.get`` can still inspect it.
        """
        clauses: list[str] = []
        args: list = []
        if message_id is not None:
            clauses.append("a.message_id = ?")
            args.append(message_id)
        if account_id is not None:
            clauses.append("m.account_id = ?")
            args.append(account_id)
        if pending_only:
            clauses.append("a.fetched_at = ''")
        if not include_inline:
            clauses.append("a.inline = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT a.*, m.subject, m.from_addr
            FROM attachments a
            JOIN messages m ON m.id = a.message_id
            {where}
            ORDER BY m.date DESC, m.rowid DESC, a.id ASC
            LIMIT ?
            """,
            [*args, int(limit)],
        ).fetchall()
        return [self._attachment_list_item(row) for row in rows]

    def get_attachment(self, attachment_id: str) -> Optional[dict]:
        """Full attachment metadata plus the message fields a fetch needs."""
        row = self._conn.execute(
            """
            SELECT a.*, m.account_id, m.folder, m.uidvalidity, m.uid,
                   m.subject, m.from_addr
            FROM attachments a
            JOIN messages m ON m.id = a.message_id
            WHERE a.id = ?
            """,
            (attachment_id,),
        ).fetchone()
        if row is None:
            return None
        item = self._attachment_list_item(row)
        item.update(
            {
                "accountId": row["account_id"],
                "folder": row["folder"],
                "uidvalidity": int(row["uidvalidity"]),
                "uid": int(row["uid"]),
                "partId": row["part_id"],
                "inline": bool(row["inline"]),
                "sha256": row["sha256"],
                "path": row["local_path"],
                "downloaded": bool(row["fetched_at"] and row["local_path"]),
            }
        )
        return item

    def set_attachment_download(
        self,
        attachment_id: str,
        *,
        local_path: str,
        sha256: str,
        fetched_at: Optional[str] = None,
    ) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE attachments SET local_path = ?, sha256 = ?, fetched_at = ? "
                "WHERE id = ?",
                (local_path, sha256, fetched_at or utc_now(), attachment_id),
            )
        return cursor.rowcount > 0

    def count_attachments(self, message_id: Optional[str] = None) -> int:
        if message_id is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM attachments"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM attachments WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return int(row["n"])

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

    def delete_message(self, message_id: str) -> bool:
        """Delete one cached message row; ``False`` when it was already gone.

        The FTS triggers keep the search index in step; the caller only gets
        here after the server operation succeeded, so a failed delete leaves
        the row (and its body) in place.
        """
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE id = ?", (message_id,)
            )
        return cursor.rowcount > 0

    def count_messages(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"])

    def count_unread_messages(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE seen = 0"
        ).fetchone()
        return int(row["n"])

    def list_message_uids(
        self,
        account_id: str,
        folder: str,
        uidvalidity: Optional[int] = None,
    ) -> set[int]:
        """UIDs already stored for one folder (optionally for one UIDVALIDITY).

        ``history.backfill`` uses this to skip messages it already has, so a
        repeated backfill does not refetch the whole older window.
        """
        if uidvalidity is None:
            rows = self._conn.execute(
                "SELECT uid FROM messages WHERE account_id = ? AND folder = ?",
                (account_id, folder),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT uid FROM messages "
                "WHERE account_id = ? AND folder = ? AND uidvalidity = ?",
                (account_id, folder, int(uidvalidity)),
            ).fetchall()
        return {int(row["uid"]) for row in rows}

    def oldest_message_date(self, account_id: str, folder: str) -> str:
        """Oldest non-empty ``date`` locally stored for one folder ("" if none)."""
        row = self._conn.execute(
            "SELECT MIN(date) AS oldest FROM messages "
            "WHERE account_id = ? AND folder = ? AND date <> ''",
            (account_id, folder),
        ).fetchone()
        return str(row["oldest"] or "") if row is not None else ""

    # -- threads -----------------------------------------------------------

    @staticmethod
    def _participants(rows: Iterable[sqlite3.Row]) -> list[str]:
        """Distinct addresses in a thread, in order of first appearance."""
        seen: set[str] = set()
        participants: list[str] = []
        for row in rows:
            for field in ("from_addr", "to_addr", "cc_addr"):
                value = row[field] or ""
                for _name, address in email.utils.getaddresses([value]):
                    address = address.strip()
                    if not address:
                        continue
                    key = address.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    participants.append(address)
        return participants

    def list_threads(
        self,
        *,
        account_id: Optional[str] = None,
        folder: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Thread summaries, newest activity first.

        ``messageCount`` / ``unreadCount`` / ``lastDate`` aggregate only the
        messages matching ``account_id`` / ``folder``; the subject and the
        participant list come from the thread's oldest message.
        """
        clauses = ["thread_id <> ''"]
        args: list = []
        if account_id is not None:
            clauses.append("account_id = ?")
            args.append(account_id)
        if folder is not None:
            clauses.append("folder = ?")
            args.append(folder)
        where = " AND ".join(clauses)

        summaries = self._conn.execute(
            f"""
            SELECT thread_id,
                   COUNT(*) AS message_count,
                   COALESCE(MAX(date), '') AS last_date,
                   SUM(CASE WHEN seen = 0 THEN 1 ELSE 0 END) AS unread_count
            FROM messages
            WHERE {where}
            GROUP BY thread_id
            ORDER BY last_date DESC, thread_id ASC
            LIMIT ?
            """,
            [*args, int(limit)],
        ).fetchall()
        if not summaries:
            return []

        thread_ids = [str(row["thread_id"]) for row in summaries]
        placeholders = ", ".join("?" for _ in thread_ids)
        members = self._conn.execute(
            f"""
            SELECT * FROM messages
            WHERE thread_id IN ({placeholders}) AND {where}
            ORDER BY date ASC, rowid ASC
            """,
            [*thread_ids, *args],
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in members:
            grouped.setdefault(str(row["thread_id"]), []).append(row)

        result: list[dict] = []
        for summary in summaries:
            thread_id = str(summary["thread_id"])
            rows = grouped.get(thread_id, [])
            result.append(
                {
                    "threadId": thread_id,
                    "subject": rows[0]["subject"] if rows else "",
                    "messageCount": int(summary["message_count"]),
                    "lastDate": str(summary["last_date"] or ""),
                    "unreadCount": int(summary["unread_count"] or 0),
                    "participants": self._participants(rows),
                }
            )
        return result

    def get_thread(self, thread_id: str) -> Optional[dict]:
        """One thread with its messages in chronological order."""
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? "
            "ORDER BY date ASC, rowid ASC",
            (thread_id,),
        ).fetchall()
        if not rows:
            return None
        return {
            "threadId": thread_id,
            "subject": rows[0]["subject"],
            "messages": [self._message_summary(row) for row in rows],
        }

    # -- follow-ups (todo) -------------------------------------------------

    @staticmethod
    def _todo_item(row: sqlite3.Row) -> dict:
        """Protocol shape for ``todo.list`` (no body, no message detail)."""
        return {
            "messageId": row["id"],
            "subject": row["subject"],
            "fromAddr": row["from_addr"],
            "date": row["date"],
            "folder": row["folder"],
            "todoState": row["todo_state"],
            "hasSummary": bool(row["has_summary"]),
            "attachmentCount": int(row["attachment_count"] or 0),
        }

    def list_todos(
        self,
        *,
        account_id: Optional[str] = None,
        state: str = "pending",
        limit: int = 50,
    ) -> list[dict]:
        """Messages in one follow-up state, newest mail first.

        ``hasSummary`` and ``attachmentCount`` are computed in the same query
        so the follow-up list can render badges without extra round trips;
        inline MIME resources are not counted as attachments.
        """
        clauses = ["m.todo_state = ?"]
        args: list[object] = [state]
        if account_id is not None:
            clauses.append("m.account_id = ?")
            args.append(account_id)
        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"""
            SELECT m.id, m.subject, m.from_addr, m.date, m.folder, m.todo_state,
                   EXISTS(
                       SELECT 1 FROM summaries s WHERE s.message_id = m.id
                   ) AS has_summary,
                   (
                       SELECT COUNT(*) FROM attachments a
                       WHERE a.message_id = m.id AND a.inline = 0
                   ) AS attachment_count
            FROM messages m
            WHERE {where}
            ORDER BY m.date DESC, m.rowid DESC
            LIMIT ?
            """,
            [*args, int(limit)],
        ).fetchall()
        return [self._todo_item(row) for row in rows]

    def set_todo_state(self, message_id: str, state: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE messages SET todo_state = ?, todo_updated_at = ? "
                "WHERE id = ?",
                (state, utc_now(), message_id),
            )
        return cursor.rowcount > 0

    def messages_matching_subject(
        self, core: str, *, exclude_id: str = "", limit: int = 20
    ) -> list[dict]:
        """Messages whose subject contains the normalised core (LIKE)."""
        if not core:
            return []
        clauses = ["subject LIKE ? ESCAPE '\\'"]
        args: list[object] = [_like_pattern(core)]
        if exclude_id:
            clauses.append("id <> ?")
            args.append(exclude_id)
        rows = self._conn.execute(
            f"""
            SELECT * FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY date DESC, rowid DESC
            LIMIT ?
            """,
            [*args, int(limit)],
        ).fetchall()
        return [self._message_summary(row) for row in rows]

    def messages_from_addresses(
        self,
        addresses: Iterable[str],
        *,
        exclude_id: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Messages sent by any of ``addresses`` (substring on ``from_addr``)."""
        patterns: list[str] = []
        for address in addresses:
            text = str(address or "").strip()
            if text:
                pattern = _like_pattern(text)
                if pattern not in patterns:
                    patterns.append(pattern)
        if not patterns:
            return []
        clauses = ["(" + " OR ".join("from_addr LIKE ? ESCAPE '\\'" for _ in patterns) + ")"]
        args: list[object] = list(patterns)
        if exclude_id:
            clauses.append("id <> ?")
            args.append(exclude_id)
        rows = self._conn.execute(
            f"""
            SELECT * FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY date DESC, rowid DESC
            LIMIT ?
            """,
            [*args, int(limit)],
        ).fetchall()
        return [self._message_summary(row) for row in rows]

    # -- summaries ---------------------------------------------------------

    @staticmethod
    def _summary_to_dict(row: sqlite3.Row) -> dict:
        try:
            key_points = json.loads(row["key_points"]) if row["key_points"] else []
        except (TypeError, json.JSONDecodeError):
            key_points = []
        try:
            entities = json.loads(row["entities"]) if row["entities"] else {}
        except (TypeError, json.JSONDecodeError):
            entities = {}
        item = {
            "id": row["id"],
            "messageId": row["message_id"],
            "threadId": row["thread_id"],
            "category": row["category"],
            "summary": row["summary"],
            "keyPoints": key_points if isinstance(key_points, list) else [],
            "entities": entities if isinstance(entities, dict) else {},
            "model": row["model"],
            "sessionId": row["session_id"],
            "createdAt": row["created_at"],
        }
        try:
            item["subject"] = row["subject"]
        except (IndexError, KeyError):
            pass
        return item

    def add_summary(
        self,
        *,
        message_id: str,
        thread_id: str = "",
        category: str = "",
        summary: str,
        key_points: Optional[Iterable[str]] = None,
        entities: Optional[dict] = None,
        model: str = "",
        session_id: str = "",
        created_at: Optional[str] = None,
    ) -> dict:
        """Append one Agent summary; returns the stored row."""
        summary_id = f"sum_{uuid.uuid4().hex}"
        timestamp = created_at or utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO summaries (
                    id, message_id, thread_id, category, summary,
                    key_points, entities, model, session_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id,
                    message_id,
                    thread_id,
                    category,
                    summary,
                    json.dumps(list(key_points or []), ensure_ascii=False),
                    json.dumps(dict(entities or {}), ensure_ascii=False),
                    model,
                    session_id,
                    timestamp,
                ),
            )
        return self.get_summary(summary_id) or {
            "id": summary_id,
            "createdAt": timestamp,
        }

    def get_summary(self, summary_id: str) -> Optional[dict]:
        row = self._conn.execute(
            """
            SELECT s.*, m.subject FROM summaries s
            JOIN messages m ON m.id = s.message_id
            WHERE s.id = ?
            """,
            (summary_id,),
        ).fetchone()
        return self._summary_to_dict(row) if row is not None else None

    def list_summaries(
        self,
        *,
        message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        category: Optional[str] = None,
        message_ids: Optional[Iterable[str]] = None,
        limit: int = 50,
        ascending: bool = False,
    ) -> list[dict]:
        """Summaries newest first (or chronologically for context building)."""
        clauses: list[str] = []
        args: list[object] = []
        if message_id is not None:
            clauses.append("s.message_id = ?")
            args.append(message_id)
        if thread_id is not None:
            clauses.append("s.thread_id = ?")
            args.append(thread_id)
        if category is not None:
            clauses.append("s.category = ?")
            args.append(category)
        if message_ids is not None:
            ids = [str(item) for item in message_ids]
            if not ids:
                return []
            placeholders = ", ".join("?" for _ in ids)
            clauses.append(f"s.message_id IN ({placeholders})")
            args.extend(ids)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if ascending else "DESC"
        rows = self._conn.execute(
            f"""
            SELECT s.*, m.subject FROM summaries s
            JOIN messages m ON m.id = s.message_id
            {where}
            ORDER BY s.created_at {order}, s.id {order}
            LIMIT ?
            """,
            [*args, int(limit)],
        ).fetchall()
        return [self._summary_to_dict(row) for row in rows]

    def count_summaries(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM summaries").fetchone()
        return int(row["n"])

    # -- contacts ----------------------------------------------------------

    @staticmethod
    def _contact_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone"],
            "company": row["company"],
            "title": row["title"],
            "notes": row["notes"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_contacts(self) -> list[dict]:
        """Address book entries ordered by ``name`` (then id, for stability)."""
        rows = self._conn.execute(
            "SELECT * FROM contacts ORDER BY name ASC, id ASC"
        ).fetchall()
        return [self._contact_to_dict(row) for row in rows]

    def get_contact(self, contact_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        return self._contact_to_dict(row) if row is not None else None

    def add_contact(
        self,
        *,
        name: str,
        contact_id: Optional[str] = None,
        email: str = "",
        phone: str = "",
        company: str = "",
        title: str = "",
        notes: str = "",
        created_at: Optional[str] = None,
    ) -> str:
        timestamp = created_at or utc_now()
        contact_id = contact_id or f"con_{uuid.uuid4().hex}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO contacts (
                    id, name, email, phone, company, title, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact_id,
                    name,
                    email,
                    phone,
                    company,
                    title,
                    notes,
                    timestamp,
                    timestamp,
                ),
            )
        return contact_id

    def update_contact(self, contact_id: str, changes: dict) -> bool:
        """Apply the given ``CONTACT_FIELDS``; ``False`` when the id is unknown."""
        assignments: list[str] = []
        args: list = []
        for field in CONTACT_FIELDS:
            if field not in changes:
                continue
            assignments.append(f"{field} = ?")
            args.append(str(changes[field]))
        assignments.append("updated_at = ?")
        args.extend([utc_now(), contact_id])
        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE contacts SET {', '.join(assignments)} WHERE id = ?", args
            )
        return cursor.rowcount > 0

    def remove_contact(self, contact_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM contacts WHERE id = ?", (contact_id,)
            )
        return cursor.rowcount > 0

    def count_contacts(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()
        return int(row["n"])

    # -- templates ---------------------------------------------------------

    @staticmethod
    def _template_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "subject": row["subject"],
            "body": row["body"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_templates(self) -> list[dict]:
        """Local templates, oldest first like ``account.list``."""
        rows = self._conn.execute(
            "SELECT * FROM templates ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [self._template_to_dict(row) for row in rows]

    def get_template(self, template_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
        return self._template_to_dict(row) if row is not None else None

    def add_template(
        self,
        *,
        name: str,
        subject: str = "",
        body: str = "",
        template_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> str:
        timestamp = created_at or utc_now()
        template_id = template_id or f"tpl_{uuid.uuid4().hex}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO templates (id, name, subject, body, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (template_id, name, subject, body, timestamp, timestamp),
            )
        return template_id

    def remove_template(self, template_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM templates WHERE id = ?", (template_id,)
            )
        return cursor.rowcount > 0

    def count_templates(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM templates").fetchone()
        return int(row["n"])

    # -- search ------------------------------------------------------------

    def search_messages(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[dict]]:
        """Full-text search over subject / body / from / to.

        Returns ``(total, page)`` exactly like :meth:`list_messages`, with
        the row's own ``snippet`` replaced by an FTS5 excerpt that marks the
        matched term with ``«`` ``»``.  Rows are ordered by ``rank`` (best
        match first), never by the query string's column order.
        """
        match = _fts_query(query)
        if not match:
            return 0, []

        terms = _search_terms(query)
        if any(_CJK_RE.search(term) for term in terms):
            return self._search_substring(terms, limit=limit, offset=offset)

        total_row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages_fts WHERE messages_fts MATCH ?",
            (match,),
        ).fetchone()
        total = int(total_row["n"])

        # ``rowid`` breaks rank ties so consecutive pages stay stable.
        rows = self._conn.execute(
            f"""
            SELECT messages.*, {_FTS_SNIPPET} AS hit_snippet
            FROM messages_fts
            JOIN messages ON messages.rowid = messages_fts.rowid
            WHERE messages_fts MATCH ?
            ORDER BY rank, messages.rowid
            LIMIT ? OFFSET ?
            """,
            (match, int(limit), int(offset)),
        ).fetchall()

        messages = []
        for row in rows:
            summary = self._message_summary(row)
            summary["snippet"] = row["hit_snippet"]
            messages.append(summary)
        return total, messages

    def _search_substring(self, terms: list[str], *, limit: int, offset: int) -> tuple[int, list[dict]]:
        """CJK queries: AND over ``LIKE '%term%'`` on the same four columns."""
        clauses = []
        params: list[object] = []
        for term in terms:
            pattern = _like_pattern(term)
            clauses.append(
                "(subject LIKE ? ESCAPE '\\' OR body_text LIKE ? ESCAPE '\\' "
                "OR from_addr LIKE ? ESCAPE '\\' OR to_addr LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern, pattern])
        where = " AND ".join(clauses)

        total_row = self._conn.execute(f"SELECT COUNT(*) AS n FROM messages WHERE {where}", params).fetchone()
        total = int(total_row["n"])

        rows = self._conn.execute(
            f"""
            SELECT * FROM messages
            WHERE {where}
            ORDER BY date DESC, rowid
            LIMIT ? OFFSET ?
            """,
            [*params, int(limit), int(offset)],
        ).fetchall()

        messages = []
        for row in rows:
            summary = self._message_summary(row)
            summary["snippet"] = _snippet_for(row, terms[0])
            messages.append(summary)
        return total, messages

    # -- drafts ------------------------------------------------------------

    @staticmethod
    def _draft_to_dict(row: sqlite3.Row) -> dict:
        """Full draft shape returned by ``draft.save`` / ``draft.get``."""
        return {
            "id": row["id"],
            "accountId": row["account_id"],
            "to": row["to_addr"],
            "cc": row["cc_addr"],
            "subject": row["subject"],
            "body": row["body"],
            "contentHash": row["content_hash"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _draft_summary(row: sqlite3.Row) -> dict:
        """List shape for ``draft.list`` (no body, newest edit first)."""
        return {
            "id": row["id"],
            "accountId": row["account_id"],
            "to": row["to_addr"],
            "subject": row["subject"],
            "contentHash": row["content_hash"],
            "status": row["status"],
            "updatedAt": row["updated_at"],
        }

    def create_draft(
        self,
        *,
        account_id: str,
        to_addr: str,
        cc_addr: str = "",
        subject: str = "",
        body: str = "",
        content_hash: str = "",
        draft_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> dict:
        timestamp = created_at or utc_now()
        draft_id = draft_id or f"dft_{uuid.uuid4().hex}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO drafts (
                    id, account_id, to_addr, cc_addr, subject, body,
                    content_hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    draft_id,
                    account_id,
                    to_addr,
                    cc_addr,
                    subject,
                    body,
                    content_hash,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_draft(draft_id) or {}

    def update_draft(
        self,
        draft_id: str,
        *,
        account_id: str,
        to_addr: str,
        cc_addr: str = "",
        subject: str = "",
        body: str = "",
        content_hash: str = "",
        status: str = "draft",
        updated_at: Optional[str] = None,
    ) -> Optional[dict]:
        """Replace the editable fields; ``None`` when the id is unknown."""
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE drafts SET
                    account_id = ?, to_addr = ?, cc_addr = ?, subject = ?,
                    body = ?, content_hash = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    account_id,
                    to_addr,
                    cc_addr,
                    subject,
                    body,
                    content_hash,
                    status,
                    updated_at or utc_now(),
                    draft_id,
                ),
            )
        if cursor.rowcount <= 0:
            return None
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return self._draft_to_dict(row) if row is not None else None

    def list_drafts(self, account_id: Optional[str] = None) -> list[dict]:
        """Draft summaries ordered by most recent edit, then id."""
        if account_id is None:
            rows = self._conn.execute(
                "SELECT * FROM drafts ORDER BY updated_at DESC, id ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM drafts WHERE account_id = ? "
                "ORDER BY updated_at DESC, id ASC",
                (account_id,),
            ).fetchall()
        return [self._draft_summary(row) for row in rows]

    def remove_draft(self, draft_id: str) -> bool:
        """Delete a draft; its approvals cascade, its action log remains."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM drafts WHERE id = ?", (draft_id,)
            )
        return cursor.rowcount > 0

    def set_draft_status(self, draft_id: str, status: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), draft_id),
            )
        return cursor.rowcount > 0

    def count_drafts(self) -> int:
        """Drafts still in the draft box (``sent`` has left it)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE status <> 'sent'"
        ).fetchone()
        return int(row["n"])

    # -- approvals ---------------------------------------------------------

    @staticmethod
    def _approval_to_dict(row: sqlite3.Row) -> dict:
        """Approval shape shared by ``approval.*`` and ``action.execute``."""
        return {
            "id": row["id"],
            "draftId": row["draft_id"],
            "action": row["action"],
            "contentHash": row["content_hash"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
            "decidedAt": row["decided_at"],
            "decidedVia": row["decided_via"],
            "decidedBy": row["decided_by"],
        }

    def add_approval(
        self,
        *,
        draft_id: str,
        content_hash: str,
        action: str = "send",
        status: str = "pending",
        ttl_minutes: int = 1440,
        approval_id: Optional[str] = None,
        created_at: Optional[str] = None,
    ) -> dict:
        """Insert a pending approval expiring ``ttl_minutes`` after creation."""
        created = created_at or utc_now()
        expires = add_minutes(created, int(ttl_minutes))
        approval_id = approval_id or f"apr_{uuid.uuid4().hex}"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO approvals (
                    id, draft_id, action, content_hash, status,
                    created_at, expires_at, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    approval_id,
                    draft_id,
                    action,
                    content_hash,
                    status,
                    created,
                    expires,
                ),
            )
        return self.get_approval(approval_id) or {}

    def get_approval(self, approval_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return self._approval_to_dict(row) if row is not None else None

    def list_pending_approvals(self) -> list[dict]:
        """Undecided approvals, oldest first (a queue, not a log)."""
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [self._approval_to_dict(row) for row in rows]

    def list_approval_history(self, limit: int = 50) -> list[dict]:
        """Decided approvals, newest first (``limit`` caps the returned page)."""
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status <> 'pending' "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [self._approval_to_dict(row) for row in rows]

    def decide_approval(
        self,
        approval_id: str,
        status: str,
        decided_at: Optional[str] = None,
        *,
        decided_via: str = "",
        decided_by: str = "",
    ) -> bool:
        """Set the decision on a still-pending approval; ``False`` if lost.

        ``decided_via`` / ``decided_by`` are the v6 audit values; they stay
        empty when the caller did not say how the decision was reached.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, "
                "decided_via = ?, decided_by = ? "
                "WHERE id = ? AND status = 'pending'",
                (
                    status,
                    decided_at or utc_now(),
                    decided_via,
                    decided_by,
                    approval_id,
                ),
            )
        return cursor.rowcount > 0

    def count_pending_approvals(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'"
        ).fetchone()
        return int(row["n"])

    # -- mail actions ------------------------------------------------------

    @staticmethod
    def _action_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "approvalId": row["approval_id"],
            "draftId": row["draft_id"],
            "action": row["action"],
            "result": row["result"],
            "error": row["error"],
            "idempotencyKey": row["idempotency_key"],
            "executedAt": row["executed_at"],
        }

    def add_mail_action(
        self,
        *,
        approval_id: str,
        draft_id: str,
        result: str,
        action: str = "send",
        error: str = "",
        idempotency_key: str,
        action_id: Optional[str] = None,
        executed_at: Optional[str] = None,
    ) -> dict:
        """Record one execute attempt; ``idempotency_key`` is UNIQUE.

        A duplicate key raises ``sqlite3.IntegrityError``; the service turns
        that into ``ALREADY_EXECUTED`` instead of sending a second message.
        """
        action_id = action_id or f"act_{uuid.uuid4().hex}"
        timestamp = executed_at or utc_now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO mail_actions (
                    id, approval_id, draft_id, action, result, error,
                    idempotency_key, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    approval_id,
                    draft_id,
                    action,
                    result,
                    error,
                    idempotency_key,
                    timestamp,
                ),
            )
        return {
            "id": action_id,
            "approvalId": approval_id,
            "draftId": draft_id,
            "action": action,
            "result": result,
            "error": error,
            "idempotencyKey": idempotency_key,
            "executedAt": timestamp,
        }

    def get_mail_action_by_key(self, idempotency_key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM mail_actions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return self._action_to_dict(row) if row is not None else None

    def list_mail_actions(self, approval_id: Optional[str] = None) -> list[dict]:
        if approval_id is None:
            rows = self._conn.execute(
                "SELECT * FROM mail_actions ORDER BY executed_at ASC, id ASC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM mail_actions WHERE approval_id = ? "
                "ORDER BY executed_at ASC, id ASC",
                (approval_id,),
            ).fetchall()
        return [self._action_to_dict(row) for row in rows]

    def count_mail_actions(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM mail_actions").fetchone()
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
