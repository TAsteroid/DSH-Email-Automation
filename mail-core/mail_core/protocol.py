"""Line-framed JSON protocol for the Mail Core stdio channel.

Contract: ``docs/IPC_PROTOCOL.md`` (protocol v1).

One request per line, one response per line.  This module owns request
validation, the response envelopes and the canonical error codes; it never
touches ``sys.stdin`` / ``sys.stdout`` itself, so the framing rules stay
testable without subprocesses.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple, Optional

PROTOCOL_VERSION = 1
VERSION = "0.1.0"

BAD_REQUEST = "BAD_REQUEST"
UNKNOWN_METHOD = "UNKNOWN_METHOD"
NOT_FOUND = "NOT_FOUND"
CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
IMAP_ERROR = "IMAP_ERROR"
DPAPI_ERROR = "DPAPI_ERROR"
SMTP_ERROR = "SMTP_ERROR"
APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
HASH_MISMATCH = "HASH_MISMATCH"
ALREADY_EXECUTED = "ALREADY_EXECUTED"
INTERNAL = "INTERNAL"

ERROR_CODES = frozenset(
    {
        BAD_REQUEST,
        UNKNOWN_METHOD,
        NOT_FOUND,
        CREDENTIAL_MISSING,
        IMAP_ERROR,
        DPAPI_ERROR,
        SMTP_ERROR,
        APPROVAL_NOT_FOUND,
        APPROVAL_NOT_APPROVED,
        APPROVAL_EXPIRED,
        HASH_MISMATCH,
        ALREADY_EXECUTED,
        INTERNAL,
    }
)


class MailCoreError(Exception):
    """An error that maps 1:1 onto a protocol error response.

    ``message`` must never contain a password, DPAPI ciphertext or other
    secret: it is forwarded verbatim to the parent process.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in ERROR_CODES else INTERNAL
        self.message = str(message)


def bad_request(message: str) -> MailCoreError:
    """Build a ``BAD_REQUEST`` error (missing field, wrong type, ...)."""
    return MailCoreError(BAD_REQUEST, message)


class Request(NamedTuple):
    id: Any
    method: str
    params: dict


def parse_request(payload: Any) -> Request:
    """Validate a decoded request object and return it as a :class:`Request`.

    Unknown fields are ignored; missing required fields raise ``BAD_REQUEST``.
    """
    if not isinstance(payload, dict):
        raise bad_request("request must be a JSON object")

    request_id = payload.get("id")
    if "id" not in payload:
        raise bad_request("missing field: id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise bad_request("field id must be a string")

    method = payload.get("method")
    if not isinstance(method, str) or not method.strip():
        raise bad_request("missing field: method")

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise bad_request("field params must be an object")

    return Request(id=request_id, method=method.strip(), params=params)


def build_success(request_id: Any, result: Any) -> dict:
    """Build a success response envelope."""
    return {"id": request_id, "ok": True, "result": result}


def build_error(request_id: Any, code: str, message: str) -> dict:
    """Build a failure response envelope."""
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def dumps(obj: Any) -> str:
    """Encode one response as a single line of JSON (no embedded newlines)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _parse_line(text: str) -> Any:
    """Decode one input line; raises ``MailCoreError`` on malformed JSON."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise bad_request(f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})") from None
    except (UnicodeDecodeError, TypeError) as exc:
        raise bad_request(f"invalid JSON input: {type(exc).__name__}") from None


def handle_line(service, line: str) -> Optional[dict]:
    """Turn one input line into exactly one response dict.

    Returns ``None`` for a blank line (nothing to answer).  Malformed input
    still produces a ``BAD_REQUEST`` response, with ``id: null`` when the
    request id could not be recovered.
    """
    text = line.strip()
    if not text:
        return None

    request_id: Any = None
    try:
        payload = _parse_line(text)
        if isinstance(payload, dict) and "id" in payload:
            request_id = payload["id"]
        request = parse_request(payload)
        result = service.dispatch(request.method, request.params)
        return build_success(request.id, result)
    except MailCoreError as exc:
        return build_error(request_id, exc.code, exc.message)
    except Exception as exc:  # defensive: never let a traceback reach stdout
        return build_error(request_id, INTERNAL, type(exc).__name__)
