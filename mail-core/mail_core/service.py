"""Method dispatch for the Mail Core stdio service.

Every method documented in ``docs/IPC_PROTOCOL.md`` v1 lives here.  Handlers
raise :class:`mail_core.protocol.MailCoreError` for expected failures; any
other exception is converted into ``INTERNAL`` (type name only, so neither a
stack trace nor request data can reach stdout).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import crypto, imap_client, pop3_client, smtp_client
from . import protocol
from . import sync
from .protocol import (
    BAD_REQUEST,
    CREDENTIAL_MISSING,
    IMAP_ERROR,
    INTERNAL,
    NOT_FOUND,
    UNKNOWN_METHOD,
    MailCoreError,
    bad_request,
)
from .store import Store

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


class Service:
    """Owns the data directory, the SQLite store and the method table."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "mail.db"
        self.store = Store(self.db_path)
        self.shutdown_requested = False
        self._methods: dict[str, Callable[[dict], Any]] = {
            "core.handshake": self._handshake,
            "account.list": self._account_list,
            "account.add": self._account_add,
            "account.remove": self._account_remove,
            "account.test": self._account_test,
            "sync.status": self._sync_status,
            "sync.run": self._sync_run,
            "sync.reset": self._sync_reset,
            "message.list": self._message_list,
            "message.get": self._message_get,
            "shutdown": self._shutdown,
        }

    def close(self) -> None:
        self.store.close()

    def dispatch(self, method: str, params: dict) -> Any:
        """Run one method; returns its ``result`` payload or raises."""
        handler = self._methods.get(method)
        if handler is None:
            raise MailCoreError(UNKNOWN_METHOD, f"unknown method: {method}")
        if not isinstance(params, dict):
            raise bad_request("field params must be an object")
        try:
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

    # -- sync --------------------------------------------------------------

    def _sync_status(self, _params: dict) -> dict:
        counters = {
            "accounts": self.store.count_accounts(),
            "messages": self.store.count_messages(),
            "unread": self.store.count_unread_messages(),
            # No draft / approval / AI tables yet: these are real zeros.
            "drafts": 0,
            "pendingApprovals": 0,
            "pendingAi": 0,
        }
        return {"counters": counters, "lastSyncAt": self.store.latest_sync_at()}

    def _sync_run(self, params: dict) -> dict:
        account_id = _require_str(params, "accountId")
        folder = _optional_str(params, "folder", sync.DEFAULT_FOLDER) or ""
        if not folder.strip():
            raise bad_request("field folder must be a non-empty string")
        limit = _optional_int(
            params, "limit", sync.DEFAULT_LIMIT, minimum=1, maximum=sync.MAX_LIMIT
        )

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

    # -- lifecycle ---------------------------------------------------------

    def _shutdown(self, _params: dict) -> dict:
        self.shutdown_requested = True
        return {"ok": True}
