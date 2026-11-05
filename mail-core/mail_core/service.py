"""Method dispatch for the Mail Core stdio service.

Every method documented in ``docs/IPC_PROTOCOL.md`` v1 lives here.  Handlers
raise :class:`mail_core.protocol.MailCoreError` for expected failures; any
other exception is converted into ``INTERNAL`` (type name only, so neither a
stack trace nor request data can reach stdout).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import attachments, backup, crypto, delete, drafts, idle, imap_client, pop3_client, sender, smtp_client, summaries
from . import protocol
from . import sync
from .protocol import (
    ALREADY_EXECUTED,
    APPROVAL_EXPIRED,
    APPROVAL_NOT_APPROVED,
    APPROVAL_NOT_FOUND,
    BAD_REQUEST,
    CREDENTIAL_MISSING,
    HASH_MISMATCH,
    IMAP_ERROR,
    INTERNAL,
    NOT_FOUND,
    SMTP_ERROR,
    UNKNOWN_METHOD,
    MailCoreError,
    bad_request,
)
from .store import (
    CONTACT_FIELDS,
    DECIDED_BY_VALUES,
    DECIDED_VIA_VALUES,
    SUMMARY_CATEGORIES,
    TODO_STATES,
    Store,
    utc_now,
)

SECURITY_MODES = imap_client.SECURITY_MODES
PROTOCOLS = ("imap", "pop3")

# Default ports per protocol and security mode: ssl / starttls / none.
PORT_DEFAULTS = {
    "imap": {"ssl": 993, "starttls": 143, "none": 143},
    "pop3": {"ssl": 995, "starttls": 110, "none": 110},
    "smtp": {"ssl": 465, "starttls": 587, "none": 25},
}


def _log(message: str) -> None:
    """Diagnostics go to stderr only; stdout is reserved for protocol lines."""
    print(f"[mail-core] {message}", file=sys.stderr, flush=True)


def _require_str(params: dict, key: str) -> str:
    value = params.get(key)
    if value is None:
        raise bad_request(f"missing field: {key}")
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"field {key} must be a non-empty string")
    return value.strip()


def _optional_str(params: dict, key: str, default: str = "") -> str:
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise bad_request(f"field {key} must be a string")
    return value


def _optional_bool(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise bad_request(f"field {key} must be a boolean")
    return value


def _optional_int(
    params: dict,
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    """Read a non-negative integer, accepting numeric strings like the ports.

    ``maximum`` clamps instead of failing: the protocol makes ``limit`` a cap.
    """
    value = params.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        raise bad_request(f"field {key} must be an integer")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise bad_request(f"field {key} must be an integer")
    if value < minimum:
        raise bad_request(f"field {key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        return maximum
    return value


def _optional_port(params: dict, key: str, default: Optional[int]) -> int:
    value = params.get(key, default)
    if value is None:
        if default is None:
            raise bad_request(f"missing field: {key}")
        return default
    if isinstance(value, bool):
        raise bad_request(f"field {key} must be an integer")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise bad_request(f"field {key} must be an integer")
    if not 1 <= value <= 65535:
        raise bad_request(f"field {key} must be between 1 and 65535")
    return value


def _optional_security(params: dict, key: str, default: str) -> str:
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or value not in SECURITY_MODES:
        raise bad_request(f"field {key} must be one of: {', '.join(SECURITY_MODES)}")
    return value


def _optional_protocol(params: dict, key: str = "protocol", default: str = "imap") -> str:
    """Receive protocol: ``imap`` or ``pop3``; anything else is ``BAD_REQUEST``."""
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or value not in PROTOCOLS:
        raise bad_request(f"field {key} must be one of: {', '.join(PROTOCOLS)}")
    return value


def _optional_id(params: dict, key: str) -> Optional[str]:
    """An id parameter that may be absent but must be a non-empty string."""
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"field {key} must be a non-empty string when given")
    return value.strip()


def _optional_choice(
    params: dict, key: str, allowed: tuple[str, ...], default: str = ""
) -> str:
    """Optional enum parameter; the empty string always means "not recorded"."""
    value = params.get(key, default)
    if value is None:
        return default
    choices = "', '".join(("",) + allowed)
    if not isinstance(value, str) or value not in ("",) + allowed:
        raise bad_request(f"field {key} must be one of: '{choices}'")
    return value


def _normalize_email(value: Any) -> str:
    """Lowercase/strip a mailbox address for the contact-session mapping."""
    if not isinstance(value, str) or not value.strip():
        raise bad_request("field email must be a non-empty string")
    return value.strip().lower()


class Service:
    """Owns the data directory, the SQLite store and the method table."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "mail.db"
        # One RLock serialises every RPC handler and the IDLE-triggered sync:
        # the whole service shares one SQLite connection.
        self._lock = threading.RLock()
        # stdout is shared by responses and IDLE events; every line goes
        # through this lock so concurrent writes never interleave.
        self._write_lock = threading.Lock()
        self._stdout = None
        self.store = Store(self.db_path)
        self.shutdown_requested = False
        self._idle_workers: dict[str, idle.IdleWorker] = {}
        self._methods: dict[str, Callable[[dict], Any]] = {
            "core.handshake": self._handshake,
            "account.list": self._account_list,
            "account.add": self._account_add,
            "account.remove": self._account_remove,
            "account.test": self._account_test,
            "folder.list": self._folder_list,
            "sync.status": self._sync_status,
            "sync.run": self._sync_run,
            "sync.reset": self._sync_reset,
            "message.list": self._message_list,
            "message.get": self._message_get,
            "message.search": self._message_search,
            "message.delete": self._message_delete,
            "message.restore": self._message_restore,
            "todo.list": self._todo_list,
            "todo.set": self._todo_set,
            "summary.save": self._summary_save,
            "summary.list": self._summary_list,
            "summary.context": self._summary_context,
            "thread.list": self._thread_list,
            "thread.get": self._thread_get,
            "history.backfill": self._history_backfill,
            "attachment.list": self._attachment_list,
            "attachment.get": self._attachment_get,
            "attachment.fetch": self._attachment_fetch,
            "attachment.backfill": self._attachment_backfill,
            "attachment.dismiss": self._attachment_dismiss,
            "contact.list": self._contact_list,
            "contact.add": self._contact_add,
            "contact.update": self._contact_update,
            "contact.remove": self._contact_remove,
            "contactSession.get": self._contact_session_get,
            "contactSession.set": self._contact_session_set,
            "contactSession.remove": self._contact_session_remove,
            "contactSession.list": self._contact_session_list,
            "template.list": self._template_list,
            "template.add": self._template_add,
            "template.remove": self._template_remove,
            "draft.save": self._draft_save,
            "draft.get": self._draft_get,
            "draft.list": self._draft_list,
            "draft.remove": self._draft_remove,
            "approval.request": self._approval_request,
            "approval.list": self._approval_list,
            "approval.approve": self._approval_approve,
            "approval.reject": self._approval_reject,
            "action.execute": self._action_execute,
            "admin.backup": self._admin_backup,
            "admin.backups": self._admin_backups,
            "idle.start": self._idle_start,
            "idle.stop": self._idle_stop,
            "idle.status": self._idle_status,
            "shutdown": self._shutdown,
        }

    def close(self) -> None:
        self._stop_idle_workers(join=True)
        self.store.close()

    # -- output (responses and IDLE events share one lock) -----------------

    def attach_stdout(self, stream) -> None:
        """Bind the protocol stream; ``None`` (tests) drops all output."""
        self._stdout = stream

    def _write_line(self, line: str) -> bool:
        stream = self._stdout
        if stream is None:
            return True
        with self._write_lock:
            try:
                stream.write(line)
                stream.write("\n")
                stream.flush()
                return True
            except (BrokenPipeError, ValueError, OSError):
                return False

    def emit_response(self, response: dict) -> bool:
        """Write one response line; ``False`` means the pipe is gone."""
        return self._write_line(protocol.dumps(response))

    def emit_event(self, payload: dict) -> None:
        """Write one unsolicited event line (never a response envelope)."""
        self._write_line(protocol.dumps(payload))

    def dispatch(self, method: str, params: dict) -> Any:
        """Run one method; returns its ``result`` payload or raises.

        Handlers run under the service lock: an IDLE-triggered sync and an
        RPC must never use the SQLite connection at the same time.
        """
        handler = self._methods.get(method)
        if handler is None:
            raise MailCoreError(UNKNOWN_METHOD, f"unknown method: {method}")
        if not isinstance(params, dict):
            raise bad_request("field params must be an object")
        try:
            with self._lock:
                return handler(params)
        except MailCoreError:
            raise
        except Exception as exc:
            _log(f"method {method} failed: {type(exc).__name__}")
            raise MailCoreError(INTERNAL, type(exc).__name__) from None

    # -- core --------------------------------------------------------------

    def _handshake(self, _params: dict) -> dict:
        return {
            "protocol": protocol.PROTOCOL_VERSION,
            "version": protocol.VERSION,
            "dbPath": str(self.db_path),
            "dataDir": str(self.data_dir),
            "schemaVersion": self.store.schema_version,
        }

    # -- accounts ----------------------------------------------------------

    def _account_list(self, _params: dict) -> dict:
        return {"accounts": self.store.list_accounts()}

    def _account_add(self, params: dict) -> dict:
        address = _require_str(params, "address")
        imap_host = _require_str(params, "imapHost")
        protocol = _optional_protocol(params)
        imap_security = _optional_security(params, "imapSecurity", "ssl")
        imap_port = _optional_port(
            params, "imapPort", PORT_DEFAULTS[protocol][imap_security]
        )
        username = _require_str(params, "username")
        display_name = _optional_str(params, "displayName", "")
        smtp_host = _optional_str(params, "smtpHost", "")
        smtp_security = _optional_security(params, "smtpSecurity", "ssl")
        smtp_port = _optional_port(
            params, "smtpPort", PORT_DEFAULTS["smtp"][smtp_security]
        )
        enabled = _optional_bool(params, "enabled", True)

        password = params.get("password")
        if password is not None and not isinstance(password, str):
            raise bad_request("field password must be a string")
        if password == "":
            password = None

        password_blob: Optional[str] = None
        if password is not None:
            # Encrypt before anything touches disk; the plaintext is dropped here.
            password_blob = crypto.encrypt_password(password)

        account_id = f"acc_{uuid.uuid4().hex}"
        self.store.add_account(
            account_id=account_id,
            address=address,
            display_name=display_name,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_security=imap_security,
            protocol=protocol,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_security=smtp_security,
            username=username,
            password_blob=password_blob,
            enabled=enabled,
        )
        return {"id": account_id}

    def _account_remove(self, params: dict) -> dict:
        account_id = _require_str(params, "id")
        if not self.store.remove_account(account_id):
            raise MailCoreError(NOT_FOUND, "account not found")
        return {"removed": True}

    def _account_test(self, params: dict) -> dict:
        account_id = _require_str(params, "id")
        account = self.store.get_account(account_id)
        if account is None:
            raise MailCoreError(NOT_FOUND, "account not found")

        blob = self.store.get_credential(account_id)
        if not blob:
            raise MailCoreError(
                CREDENTIAL_MISSING, "no credential stored for this account"
            )
        try:
            password = crypto.decrypt_password(blob)
        except MailCoreError:
            raise MailCoreError(
                CREDENTIAL_MISSING, "stored credential could not be decrypted"
            ) from None

        receive_protocol = account.get("protocol") or "imap"
        if receive_protocol == "pop3":
            messages = pop3_client.test_connection(
                account["imapHost"],
                account["imapPort"],
                account["imapSecurity"],
                account["username"],
                password,
            )
            result = {"ok": True, "protocol": "pop3", "messages": messages}
        elif receive_protocol == "imap":
            folders = imap_client.test_connection(
                account["imapHost"],
                account["imapPort"],
                account["imapSecurity"],
                account["username"],
                password,
            )
            self.store.replace_folders(account_id, folders)
            result = {"ok": True, "protocol": "imap", "folders": folders}
        else:  # only reachable if a database row was edited by hand
            raise MailCoreError(
                IMAP_ERROR, f"unsupported receive protocol: {receive_protocol!r}"
            )

        # SMTP is optional and never fails the overall test: a working
        # incoming-server probe is what ``ok: true`` means.
        if account.get("smtpHost"):
            result["smtp"] = smtp_client.check_connection(
                account["smtpHost"],
                account["smtpPort"],
                account["smtpSecurity"],
            )
        return result

    # -- folders -----------------------------------------------------------

    def _folder_list(self, params: dict) -> dict:
        """Read the cached folder list of one account (no network access).

        The list is filled by ``account.test``; an account that has never
        been tested successfully simply has no rows yet, which is an empty
        list rather than an error.  Names are stored decoded, so callers can
        match ``已发送`` directly.
        """
        account_id = _require_str(params, "accountId")
        if self.store.get_account(account_id) is None:
            raise MailCoreError(NOT_FOUND, "account not found")
        return {"folders": self.store.list_folders(account_id)}

    # -- sync --------------------------------------------------------------

    def _sync_status(self, _params: dict) -> dict:
        counters = {
            "accounts": self.store.count_accounts(),
            "messages": self.store.count_messages(),
            "unread": self.store.count_unread_messages(),
            # Drafts that have not been sent yet, and undecided approvals.
            "drafts": self.store.count_drafts(),
            "pendingApprovals": self.store.count_pending_approvals(),
            # AI tasks arrive with a later phase; there is no table for them.
            "pendingAi": 0,
        }
        return {"counters": counters, "lastSyncAt": self.store.latest_sync_at()}

    def _account_credentials(self, account_id: str) -> tuple[dict, str]:
        """Resolve an account and its decrypted password (never logged)."""
        account = self.store.get_account(account_id)
        if account is None:
            raise MailCoreError(NOT_FOUND, "account not found")

        blob = self.store.get_credential(account_id)
        if not blob:
            raise MailCoreError(
                CREDENTIAL_MISSING, "no credential stored for this account"
            )
        try:
            return account, crypto.decrypt_password(blob)
        except MailCoreError:
            raise MailCoreError(
                CREDENTIAL_MISSING, "stored credential could not be decrypted"
            ) from None

    def _sync_run(self, params: dict) -> dict:
        account_id = _require_str(params, "accountId")
        folder = _optional_str(params, "folder", sync.DEFAULT_FOLDER) or ""
        if not folder.strip():
            raise bad_request("field folder must be a non-empty string")
        limit = _optional_int(
            params, "limit", sync.DEFAULT_LIMIT, minimum=1, maximum=sync.MAX_LIMIT
        )

        # ``mode: "backfill"`` is the same operation as ``history.backfill``;
        # it lives here too so callers did not have to learn two entry points.
        mode = _optional_str(params, "mode", "")
        if mode:
            if mode != "backfill":
                raise bad_request("field mode must be 'backfill' when given")
            before = sync.normalize_before(_require_str(params, "before"))
            account, password = self._account_credentials(account_id)
            return sync.backfill_history(
                self.store, account, password, folder.strip(), before, limit
            )

        account, password = self._account_credentials(account_id)
        return sync.run_sync(
            self.store, account, password, folder.strip(), limit
        )

    def _sync_reset(self, params: dict) -> dict:
        """Drop the local copy of one folder and restart its cursor.

        Used when the local index is out of sync with the server (for example
        after changing the sync window): the next ``sync.run`` refetches from
        scratch instead of resuming from a cursor that skipped messages.
        """
        account_id = _require_str(params, "accountId")
        folder = _optional_str(params, "folder", sync.DEFAULT_FOLDER) or ""
        if not folder.strip():
            raise bad_request("field folder must be a non-empty string")

        account = self.store.get_account(account_id)
        if account is None:
            raise MailCoreError(NOT_FOUND, "account not found")

        folder = folder.strip()
        removed = self.store.delete_messages(account_id, folder)
        self.store.set_sync_state(account_id, folder, uidvalidity=0, last_uid=0, status="idle", error="")
        return {"accountId": account_id, "folder": folder, "removed": removed}

    def _history_backfill(self, params: dict) -> dict:
        """Fetch mail older than ``before`` that is not indexed yet.

        Idempotent by UID: a second call with the same arguments stores
        nothing.  The forward sync cursor is left untouched.
        """
        account_id = _require_str(params, "accountId")
        folder = _optional_str(params, "folder", sync.DEFAULT_FOLDER) or ""
        if not folder.strip():
            raise bad_request("field folder must be a non-empty string")
        before = sync.normalize_before(_require_str(params, "before"))
        limit = _optional_int(
            params, "limit", sync.DEFAULT_LIMIT, minimum=1, maximum=sync.MAX_LIMIT
        )

        account, password = self._account_credentials(account_id)
        return sync.backfill_history(
            self.store, account, password, folder.strip(), before, limit
        )

    # -- messages ----------------------------------------------------------

    def _message_list(self, params: dict) -> dict:
        account_id = params.get("accountId")
        if account_id is not None:
            if not isinstance(account_id, str) or not account_id.strip():
                raise bad_request(
                    "field accountId must be a non-empty string when given"
                )
            account_id = account_id.strip()

        folder = params.get("folder")
        if folder is not None:
            if not isinstance(folder, str) or not folder.strip():
                raise bad_request("field folder must be a non-empty string when given")
            folder = folder.strip()

        limit = _optional_int(params, "limit", 50, minimum=1, maximum=200)
        offset = _optional_int(params, "offset", 0, minimum=0)

        total, messages = self.store.list_messages(
            account_id=account_id,
            folder=folder,
            limit=limit,
            offset=offset,
        )
        return {"total": total, "messages": messages}

    def _message_get(self, params: dict) -> dict:
        message_id = _require_str(params, "id")
        message = self.store.get_message(message_id)
        if message is None:
            raise MailCoreError(NOT_FOUND, "message not found")
        return message

    def _message_search(self, params: dict) -> dict:
        """Full-text search over the local index (see ``message.search``).

        ``query`` is free text: the store quotes every whitespace-separated
        term so FTS5 operators in the input are literal text, never syntax.
        """
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            raise bad_request("field query must be a non-empty string")
        limit = _optional_int(params, "limit", 50, minimum=1, maximum=200)
        offset = _optional_int(params, "offset", 0, minimum=0)

        total, messages = self.store.search_messages(
            query.strip(), limit=limit, offset=offset
        )
        return {"total": total, "messages": messages}

    def _message_delete(self, params: dict) -> dict:
        """Delete one synced message (see ``message.delete``).

        ``permanent`` is an explicit parameter, never inferred from the
        folder: ``false`` (default) moves the message to the account's Trash
        folder (or permanently deletes it when there is none), ``true``
        expunges it outright.  POP3 cannot delete a single server-side
        message, so those accounts only lose the local row and the result
        carries ``localOnly: true`` for the UI to explain.
        """
        message_id = _require_str(params, "id")
        permanent = _optional_bool(params, "permanent", False)
        message = self.store.get_message(message_id)
        if message is None:
            raise MailCoreError(NOT_FOUND, "message not found")

        account = self.store.get_account(message["accountId"])
        if account is None:  # unreachable: messages cascade with the account
            raise MailCoreError(NOT_FOUND, "account not found")
        if (account.get("protocol") or "imap") == "pop3":
            delete.delete_local(self.store, message)
            return {"deleted": True, "trashed": False, "localOnly": True}

        account, password = self._account_credentials(message["accountId"])
        return delete.delete_imap(
            self.store, account, password, message, permanent=permanent
        )

    def _message_restore(self, params: dict) -> dict:
        """Copy a Trash message back to a real folder (see ``message.restore``).

        Only local preconditions are checked here (IMAP account, source is a
        Trash folder, explicit target exists in the cached ``folder.list``);
        the send-side verification lives in :func:`delete.restore_imap` and
        keeps the local row whenever the server copy did not actually happen.
        """
        message_id = _require_str(params, "id")
        message = self.store.get_message(message_id)
        if message is None:
            raise MailCoreError(NOT_FOUND, "message not found")

        account_id = message["accountId"]
        account = self.store.get_account(account_id)
        if account is None:  # unreachable: messages cascade with the account
            raise MailCoreError(NOT_FOUND, "account not found")
        if (account.get("protocol") or "imap") == "pop3":
            raise bad_request("message.restore only supports IMAP accounts")

        source = str(message.get("folder") or "")
        if not delete.is_trash_folder(source):
            raise bad_request(
                f"message is not in a trash folder (folder: {source!r})"
            )

        target = "INBOX"
        raw_folder = params.get("folder")
        if raw_folder is not None:
            if not isinstance(raw_folder, str) or not raw_folder.strip():
                raise bad_request(
                    "field folder must be a non-empty string when given"
                )
            target = raw_folder.strip()
            folders = {folder["name"] for folder in self.store.list_folders(account_id)}
            if target not in folders:
                raise bad_request(f"folder not found: {target}")

        account, password = self._account_credentials(account_id)
        return delete.restore_imap(self.store, account, password, message, target)

    # -- follow-ups (todo) -------------------------------------------------

    def _todo_list(self, params: dict) -> dict:
        """Messages flagged for follow-up (default: the pending queue)."""
        account_id = _optional_id(params, "accountId")
        state = _optional_str(params, "state", "pending")
        if state not in TODO_STATES:
            raise bad_request("field state must be one of: '', 'pending', 'done'")
        limit = _optional_int(params, "limit", 50, minimum=1, maximum=200)
        return {
            "todos": self.store.list_todos(
                account_id=account_id, state=state, limit=limit
            )
        }

    def _todo_set(self, params: dict) -> dict:
        """Set (or clear, with ``''``) one message's follow-up state."""
        message_id = _require_str(params, "messageId")
        state = params.get("state")
        if not isinstance(state, str) or state not in TODO_STATES:
            raise bad_request("field state must be one of: '', 'pending', 'done'")
        if self.store.get_message(message_id) is None:
            raise MailCoreError(NOT_FOUND, "message not found")
        if not self.store.set_todo_state(message_id, state):
            raise MailCoreError(NOT_FOUND, "message not found")
        return {"messageId": message_id, "state": state}

    # -- Agent summaries ---------------------------------------------------

    def _summary_save(self, params: dict) -> dict:
        """Append one Agent summary; the category must be one of the fixed 8."""
        message_id = _require_str(params, "messageId")
        category = _require_str(params, "category")
        if category not in SUMMARY_CATEGORIES:
            raise bad_request(
                "field category must be one of: "
                + " / ".join(SUMMARY_CATEGORIES)
            )
        text = params.get("summary")
        if not isinstance(text, str) or not text.strip():
            raise bad_request("field summary must be a non-empty string")

        key_points = params.get("keyPoints")
        if key_points is None:
            key_points = []
        if not isinstance(key_points, list) or not all(
            isinstance(item, str) for item in key_points
        ):
            raise bad_request("field keyPoints must be an array of strings")

        entities = params.get("entities")
        if entities is None:
            entities = {}
        if not isinstance(entities, dict):
            raise bad_request("field entities must be an object")
        try:
            json.dumps(entities, ensure_ascii=False)
        except (TypeError, ValueError):
            raise bad_request("field entities must be JSON-serializable") from None

        model = _optional_str(params, "model", "")
        session_id = _optional_str(params, "sessionId", "")

        message = self.store.get_message(message_id)
        if message is None:
            raise MailCoreError(NOT_FOUND, "message not found")
        stored = self.store.add_summary(
            message_id=message_id,
            thread_id=str(message.get("threadId") or ""),
            category=category,
            summary=text,
            key_points=key_points,
            entities=entities,
            model=model,
            session_id=session_id,
        )
        return {"id": stored["id"], "createdAt": stored["createdAt"]}

    def _summary_list(self, params: dict) -> dict:
        message_id = _optional_id(params, "messageId")
        thread_id = _optional_id(params, "threadId")
        category = params.get("category")
        if category is not None:
            if not isinstance(category, str) or category not in SUMMARY_CATEGORIES:
                raise bad_request(
                    "field category must be one of: "
                    + " / ".join(SUMMARY_CATEGORIES)
                )
        limit = _optional_int(params, "limit", 50, minimum=1, maximum=200)
        return {
            "summaries": self.store.list_summaries(
                message_id=message_id,
                thread_id=thread_id,
                category=category,
                limit=limit,
            )
        }

    def _summary_context(self, params: dict) -> dict:
        """Everything an Agent needs before summarising one message."""
        message_id = _require_str(params, "messageId")
        return summaries.build_context(self.store, message_id)

    # -- threads -----------------------------------------------------------

    def _thread_list(self, params: dict) -> dict:
        """Thread summaries, newest activity first (local index only)."""
        account_id = _optional_id(params, "accountId")
        folder = _optional_id(params, "folder")
        limit = _optional_int(params, "limit", 50, minimum=1, maximum=200)
        return {
            "threads": self.store.list_threads(
                account_id=account_id, folder=folder, limit=limit
            )
        }

    def _thread_get(self, params: dict) -> dict:
        thread_id = _require_str(params, "threadId")
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise MailCoreError(NOT_FOUND, "thread not found")
        return thread

    # -- attachments -------------------------------------------------------

    def _attachment_list(self, params: dict) -> dict:
        """List stored attachment metadata (local index only, no download)."""
        return {
            "attachments": self.store.list_attachments(
                message_id=_optional_id(params, "messageId"),
                account_id=_optional_id(params, "accountId"),
                pending_only=_optional_bool(params, "pendingOnly", False),
            )
        }

    def _attachment_get(self, params: dict) -> dict:
        attachment_id = _require_str(params, "id")
        attachment = self.store.get_attachment(attachment_id)
        if attachment is None:
            raise MailCoreError(NOT_FOUND, "attachment not found")
        return attachment

    def _attachment_fetch(self, params: dict) -> dict:
        """Download one attachment part into ``<dataDir>/attachments``."""
        attachment_id = _require_str(params, "id")
        max_bytes = _optional_int(
            params,
            "maxBytes",
            attachments.DEFAULT_MAX_BYTES,
            minimum=1,
            maximum=attachments.MAX_MAX_BYTES,
        )
        attachment = self.store.get_attachment(attachment_id)
        if attachment is None:
            raise MailCoreError(NOT_FOUND, "attachment not found")

        account, password = self._account_credentials(attachment["accountId"])
        return attachments.download(
            self.store,
            self.data_dir,
            attachment,
            account,
            password,
            max_bytes=max_bytes,
        )

    def _attachment_backfill(self, params: dict) -> dict:
        """Extract attachment metadata for mail synced before schema v8."""
        account_id = _require_str(params, "accountId")
        folder = _optional_id(params, "folder")
        limit = _optional_int(params, "limit", 50, minimum=1, maximum=200)
        account, password = self._account_credentials(account_id)
        return sync.backfill_attachment_metadata(
            self.store, account, password, folder, limit
        )

    def _attachment_dismiss(self, params: dict) -> dict:
        """Mark one attachment as already handled (idempotent)."""
        attachment_id = _require_str(params, "id")
        dismissed_at = self.store.dismiss_attachment(attachment_id)
        if dismissed_at is None:
            raise MailCoreError(NOT_FOUND, "attachment not found")
        return {"dismissed": True, "dismissedAt": dismissed_at}

    # -- contacts ----------------------------------------------------------

    def _contact_list(self, _params: dict) -> dict:
        return {"contacts": self.store.list_contacts()}

    def _contact_add(self, params: dict) -> dict:
        name = _require_str(params, "name")
        contact_id = f"con_{uuid.uuid4().hex}"
        self.store.add_contact(
            contact_id=contact_id,
            name=name,
            email=_optional_str(params, "email", ""),
            phone=_optional_str(params, "phone", ""),
            company=_optional_str(params, "company", ""),
            title=_optional_str(params, "title", ""),
            notes=_optional_str(params, "notes", ""),
        )
        return {"id": contact_id}

    def _contact_update(self, params: dict) -> dict:
        contact_id = _require_str(params, "id")
        changes: dict[str, str] = {}
        for field in CONTACT_FIELDS:
            if field not in params or params[field] is None:
                continue
            value = params[field]
            if not isinstance(value, str):
                raise bad_request(f"field {field} must be a string")
            if field == "name" and not value.strip():
                raise bad_request("field name must be a non-empty string")
            changes[field] = value
        if not self.store.update_contact(contact_id, changes):
            raise MailCoreError(NOT_FOUND, "contact not found")
        return {"updated": True}

    def _contact_remove(self, params: dict) -> dict:
        contact_id = _require_str(params, "id")
        if not self.store.remove_contact(contact_id):
            raise MailCoreError(NOT_FOUND, "contact not found")
        return {"removed": True}

    # -- contact sessions (mailbox address -> DSH session) -----------------

    def _contact_session_get(self, params: dict) -> dict:
        """Read one persisted mapping; an absent address is not an error."""
        account_id = _optional_id(params, "accountId")
        email = _normalize_email(params.get("email"))
        mapping = self.store.get_contact_session(
            account_id=account_id, email=email
        )
        if mapping is None:
            return {"sessionId": ""}
        return {
            "sessionId": mapping["sessionId"],
            "workspaceId": mapping["workspaceId"],
            "title": mapping["title"],
            "updatedAt": mapping["updatedAt"],
        }

    def _contact_session_set(self, params: dict) -> dict:
        """Upsert the mapping for one sender (one row per account + email)."""
        account_id = _require_str(params, "accountId")
        email = _normalize_email(params.get("email"))
        session_id = _require_str(params, "sessionId")
        workspace_id = params.get("workspaceId")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise bad_request("field workspaceId must be a string")
        title = params.get("title")
        if title is not None and not isinstance(title, str):
            raise bad_request("field title must be a string")
        if self.store.get_account(account_id) is None:
            raise MailCoreError(NOT_FOUND, "account not found")

        mapping = self.store.set_contact_session(
            account_id=account_id,
            email=email,
            session_id=session_id,
            workspace_id=workspace_id,
            title=title,
        )
        return {
            "sessionId": mapping["sessionId"],
            "workspaceId": mapping["workspaceId"],
            "title": mapping["title"],
            "updatedAt": mapping["updatedAt"],
        }

    def _contact_session_remove(self, params: dict) -> dict:
        """Drop the mapping; removing an absent one is a no-op success."""
        account_id = _require_str(params, "accountId")
        email = _normalize_email(params.get("email"))
        self.store.remove_contact_session(account_id=account_id, email=email)
        return {"removed": True}

    def _contact_session_list(self, params: dict) -> dict:
        account_id = _optional_id(params, "accountId")
        sessions = [
            {
                "email": item["email"],
                "sessionId": item["sessionId"],
                "workspaceId": item["workspaceId"],
                "title": item["title"],
                "updatedAt": item["updatedAt"],
            }
            for item in self.store.list_contact_sessions(account_id)
        ]
        return {"sessions": sessions}

    # -- templates ---------------------------------------------------------

    def _template_list(self, _params: dict) -> dict:
        return {"templates": self.store.list_templates()}

    def _template_add(self, params: dict) -> dict:
        name = _require_str(params, "name")
        template_id = f"tpl_{uuid.uuid4().hex}"
        self.store.add_template(
            template_id=template_id,
            name=name,
            subject=_optional_str(params, "subject", ""),
            body=_optional_str(params, "body", ""),
        )
        return {"id": template_id}

    def _template_remove(self, params: dict) -> dict:
        template_id = _require_str(params, "id")
        if not self.store.remove_template(template_id):
            raise MailCoreError(NOT_FOUND, "template not found")
        return {"removed": True}

    # -- drafts ------------------------------------------------------------

    def _draft_save(self, params: dict) -> dict:
        """Create (no ``draftId``) or replace one draft's editable fields.

        Every save recomputes ``contentHash`` and ``updatedAt``.  A draft that
        had already been approved/rejected goes back to ``draft``: the old
        decision belongs to the previous content and can never execute again,
        because its approval still holds the old hash.
        """
        account_id = _require_str(params, "accountId")
        draft_id = _optional_id(params, "draftId")
        to_addr = _require_str(params, "to")
        cc_addr = _optional_str(params, "cc", "")
        subject = _optional_str(params, "subject", "")
        body = _optional_str(params, "body", "")

        if self.store.get_account(account_id) is None:
            raise MailCoreError(NOT_FOUND, "account not found")

        digest = drafts.content_hash(to_addr, cc_addr, subject, body)
        if draft_id is None:
            saved = self.store.create_draft(
                account_id=account_id,
                to_addr=to_addr,
                cc_addr=cc_addr,
                subject=subject,
                body=body,
                content_hash=digest,
            )
        else:
            saved = self.store.update_draft(
                draft_id,
                account_id=account_id,
                to_addr=to_addr,
                cc_addr=cc_addr,
                subject=subject,
                body=body,
                content_hash=digest,
                status="draft",
            )
            if saved is None:
                raise MailCoreError(NOT_FOUND, "draft not found")

        return {
            "id": saved["id"],
            "contentHash": saved["contentHash"],
            "status": saved["status"],
        }

    def _draft_get(self, params: dict) -> dict:
        draft_id = _require_str(params, "id")
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise MailCoreError(NOT_FOUND, "draft not found")
        return draft

    def _draft_list(self, params: dict) -> dict:
        account_id = _optional_id(params, "accountId")
        return {"drafts": self.store.list_drafts(account_id)}

    def _draft_remove(self, params: dict) -> dict:
        draft_id = _require_str(params, "id")
        if not self.store.remove_draft(draft_id):
            raise MailCoreError(NOT_FOUND, "draft not found")
        return {"removed": True}

    # -- approvals ---------------------------------------------------------

    def _approval_request(self, params: dict) -> dict:
        """Bind a pending approval to the draft's *current* content hash."""
        draft_id = _require_str(params, "draftId")
        ttl_minutes = _optional_int(
            params, "ttlMinutes", 1440, minimum=0, maximum=525600
        )
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise MailCoreError(NOT_FOUND, "draft not found")
        approval = self.store.add_approval(
            draft_id=draft_id,
            content_hash=draft["contentHash"],
            ttl_minutes=ttl_minutes,
        )
        return {
            "id": approval["id"],
            "draftId": approval["draftId"],
            "contentHash": approval["contentHash"],
            "expiresAt": approval["expiresAt"],
        }

    def _approval_list(self, _params: dict) -> dict:
        return {
            "pending": self.store.list_pending_approvals(),
            "history": self.store.list_approval_history(50),
        }

    def _decide_approval(self, params: dict, status: str, result_key: str) -> dict:
        """Shared ``approve`` / ``reject`` path (only a pending approval decides).

        ``decidedVia`` / ``decidedBy`` are the optional v6 audit values: how
        the decision was reached (host user prompt, policy auto-approval, or
        the API) and who made it.  They default to the empty string, and are
        validated before the approval lookup so a typo is always a
        ``BAD_REQUEST`` rather than a decision.

        Expiry is ``now >= expiresAt``: the timestamp is the last instant the
        approval is valid, which also makes ``ttlMinutes: 0`` immediately
        expired instead of depending on the wall clock ticking over.
        """
        approval_id = _require_str(params, "id")
        decided_via = _optional_choice(params, "decidedVia", DECIDED_VIA_VALUES)
        decided_by = _optional_choice(params, "decidedBy", DECIDED_BY_VALUES)
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise MailCoreError(APPROVAL_NOT_FOUND, "approval not found")
        if approval["status"] != "pending":
            raise bad_request(f"approval is already {approval['status']}")
        if utc_now() >= approval["expiresAt"]:
            raise MailCoreError(APPROVAL_EXPIRED, "approval has expired")
        if not self.store.decide_approval(
            approval_id,
            status,
            decided_via=decided_via,
            decided_by=decided_by,
        ):
            raise bad_request("approval is no longer pending")

        self.store.set_draft_status(
            approval["draftId"], "approved" if status == "approved" else "rejected"
        )
        return {result_key: True}

    def _approval_approve(self, params: dict) -> dict:
        return self._decide_approval(params, "approved", "approved")

    def _approval_reject(self, params: dict) -> dict:
        return self._decide_approval(params, "rejected", "rejected")

    # -- actions -----------------------------------------------------------

    def _send_password(self, account: dict) -> str:
        """Decrypt the stored credential for sending, or explain why not.

        Every failure here is a send failure (``SMTP_ERROR``), never a
        ``CREDENTIAL_MISSING``: ``action.execute`` has a single failure code
        and always leaves a ``mail_actions`` row behind.
        """
        if not account.get("smtpHost"):
            raise sender.SendError("SMTP host is not configured for this account")
        blob = self.store.get_credential(account["id"])
        if not blob:
            raise sender.SendError("no credential stored for this account")
        try:
            return crypto.decrypt_password(blob)
        except MailCoreError:
            raise sender.SendError(
                "stored credential could not be decrypted"
            ) from None

    def _action_execute(self, params: dict) -> dict:
        """Send an approved draft exactly once, recording the attempt.

        Validation order (each step has its own error code): approval exists,
        approval is approved, approval has not expired, the draft still hashes
        to the approved content, the idempotency key was never executed.  Only
        then is a message built and sent.
        """
        approval_id = _require_str(params, "approvalId")
        idempotency_key = _optional_id(params, "idempotencyKey") or approval_id

        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise MailCoreError(APPROVAL_NOT_FOUND, "approval not found")
        if approval["status"] != "approved":
            raise MailCoreError(
                APPROVAL_NOT_APPROVED,
                f"approval is {approval['status']}, not approved",
            )
        if utc_now() >= approval["expiresAt"]:
            raise MailCoreError(APPROVAL_EXPIRED, "approval has expired")

        draft = self.store.get_draft(approval["draftId"])
        if draft is None:
            raise MailCoreError(NOT_FOUND, "draft not found")
        if draft["contentHash"] != approval["contentHash"]:
            raise MailCoreError(
                HASH_MISMATCH, "draft content changed after the approval"
            )
        if self.store.get_mail_action_by_key(idempotency_key) is not None:
            raise MailCoreError(
                ALREADY_EXECUTED,
                "this approval (or idempotency key) was already executed",
            )

        password = ""
        try:
            account = self.store.get_account(draft["accountId"])
            if account is None:
                raise sender.SendError("account not found for this draft")
            password = self._send_password(account)
            message_id = sender.send_draft(account, password, draft)
        except Exception as exc:
            error = sender.describe_error(exc, secrets=(password,))
            self._record_action(
                approval, draft, "failed", error, idempotency_key, utc_now()
            )
            raise MailCoreError(SMTP_ERROR, error) from None

        executed_at = utc_now()
        self.store.set_draft_status(draft["id"], "sent")
        self._record_action(
            approval, draft, "sent", "", idempotency_key, executed_at
        )
        return {"sent": True, "messageId": message_id, "executedAt": executed_at}

    def _record_action(
        self,
        approval: dict,
        draft: dict,
        result: str,
        error: str,
        idempotency_key: str,
        executed_at: str,
    ) -> dict:
        """Write the ``mail_actions`` row; a duplicate key means "already done"."""
        try:
            return self.store.add_mail_action(
                approval_id=approval["id"],
                draft_id=draft["id"],
                result=result,
                error=error,
                idempotency_key=idempotency_key,
                executed_at=executed_at,
            )
        except sqlite3.IntegrityError:
            raise MailCoreError(
                ALREADY_EXECUTED,
                "this approval (or idempotency key) was already executed",
            ) from None

    # -- admin -------------------------------------------------------------

    def _admin_backup(self, _params: dict) -> dict:
        """Snapshot mail.db into ``<dataDir>/backups/`` and cap the history."""
        backup_dir = self.data_dir / backup.BACKUP_DIRNAME
        result = backup.create_backup(self.db_path, backup_dir)
        backup.prune_backups(backup_dir, keep=backup.DEFAULT_KEEP)
        return result

    def _admin_backups(self, _params: dict) -> dict:
        return {
            "backups": backup.list_backups(self.data_dir / backup.BACKUP_DIRNAME)
        }

    # -- IMAP IDLE ---------------------------------------------------------

    def _idle_start(self, params: dict) -> dict:
        """Start one IDLE thread per enabled IMAP account (already-run skipped)."""
        account_id = _optional_id(params, "accountId")
        accounts = self.store.list_accounts()
        if account_id is not None:
            accounts = [item for item in accounts if item["id"] == account_id]
            if not accounts:
                raise MailCoreError(NOT_FOUND, "account not found")

        started: list[str] = []
        skipped: list[str] = []
        for account in accounts:
            current_id = account["id"]
            if (account.get("protocol") or "imap") != "imap":
                skipped.append(current_id)  # POP3 has no IDLE
                continue
            if not account.get("enabled"):
                skipped.append(current_id)
                continue
            worker = self._idle_workers.get(current_id)
            if worker is not None and worker.is_alive():
                skipped.append(current_id)
                continue
            try:
                _account, password = self._account_credentials(current_id)
            except MailCoreError:
                # No usable credential: starting would only produce an
                # error/reconnect loop, so report it as skipped.
                skipped.append(current_id)
                continue
            worker = idle.IdleWorker(
                account=account,
                password=password,
                on_new_mail=self._idle_sync_inbox,
            )
            self._idle_workers[current_id] = worker
            worker.start()
            started.append(current_id)
        return {"started": started, "skipped": skipped}

    def _idle_stop(self, params: dict) -> dict:
        account_id = _optional_id(params, "accountId")
        if account_id is None:
            targets = list(self._idle_workers)
        elif account_id in self._idle_workers:
            targets = [account_id]
        else:
            targets = []
        stopped: list[str] = []
        for current_id in targets:
            worker = self._idle_workers.pop(current_id)
            # No join: the worker may be waiting for this very RPC lock inside
            # its sync callback, and joining here would deadlock.
            worker.stop(join=False)
            stopped.append(current_id)
        return {"stopped": stopped}

    def _idle_status(self, _params: dict) -> dict:
        return {
            "workers": [
                self._idle_workers[account_id].status()
                for account_id in sorted(self._idle_workers)
            ]
        }

    def _idle_sync_inbox(self, account_id: str) -> None:
        """IDLE callback: run the normal INBOX sync under the service lock."""
        with self._lock:
            account, password = self._account_credentials(account_id)
            result = sync.run_sync(
                self.store,
                account,
                password,
                sync.DEFAULT_FOLDER,
                sync.DEFAULT_LIMIT,
            )
            self.emit_event(
                {
                    "event": "mail.new",
                    "accountId": account_id,
                    "stored": int(result.get("stored") or 0),
                }
            )

    def _stop_idle_workers(self, *, join: bool) -> None:
        workers = list(self._idle_workers.values())
        self._idle_workers.clear()
        for worker in workers:
            worker.stop(join=join)

    # -- lifecycle ---------------------------------------------------------

    def _shutdown(self, _params: dict) -> dict:
        self.shutdown_requested = True
        # Do not join here: this runs under the RPC lock.  ``close()`` joins.
        self._stop_idle_workers(join=False)
        return {"ok": True}
