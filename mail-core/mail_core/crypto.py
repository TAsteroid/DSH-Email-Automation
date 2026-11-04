"""Windows DPAPI helpers used to protect stored mailbox passwords.

``CryptProtectData`` / ``CryptUnprotectData`` are called through ``ctypes``;
the ciphertext is stored in SQLite as base64 text.  Outside Windows this
module refuses to operate (``DPAPI_ERROR``) rather than degrading to
plaintext.

Nothing in this module ever returns or logs the plaintext next to its
ciphertext, and error messages contain neither.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import sys
from typing import Optional

from .protocol import DPAPI_ERROR, MailCoreError

IS_WINDOWS = sys.platform == "win32"

# Do not show a UI prompt; Mail Core runs headless under the DSH plugin.
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiError(MailCoreError):
    """DPAPI unavailable / failed; maps to the ``DPAPI_ERROR`` code."""

    def __init__(self, message: str) -> None:
        super().__init__(DPAPI_ERROR, message)


class DATA_BLOB(ctypes.Structure):
    """WIN32 ``DATA_BLOB``.  ``pbData`` stays as a pointer so ``LocalFree`` works."""

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_crypt32 = None
_kernel32 = None


def _libraries():
    """Load and bind crypt32/kernel32 lazily (Windows only)."""
    global _crypt32, _kernel32
    if _crypt32 is not None:
        return _crypt32, _kernel32

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),  # pDataIn
        ctypes.c_wchar_p,  # szDataDescr
        ctypes.POINTER(DATA_BLOB),  # pOptionalEntropy
        ctypes.c_void_p,  # pvReserved
        ctypes.c_void_p,  # pPromptStruct
        ctypes.c_uint32,  # dwFlags
        ctypes.POINTER(DATA_BLOB),  # pDataOut
    ]
    crypt32.CryptProtectData.restype = ctypes.c_bool

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),  # pDataIn
        ctypes.c_void_p,  # ppszDataDescr (LPWSTR*), optional
        ctypes.POINTER(DATA_BLOB),  # pOptionalEntropy
        ctypes.c_void_p,  # pvReserved
        ctypes.c_void_p,  # pPromptStruct
        ctypes.c_uint32,  # dwFlags
        ctypes.POINTER(DATA_BLOB),  # pDataOut
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_bool

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    _crypt32, _kernel32 = crypt32, kernel32
    return _crypt32, _kernel32


def _to_blob(data: bytes):
    """Copy ``data`` into an in/out-capable ``DATA_BLOB`` plus its backing buffer."""
    if not isinstance(data, (bytes, bytearray)):
        raise DpapiError("DPAPI input must be bytes")
    payload = bytes(data)
    buffer = ctypes.create_string_buffer(payload, max(len(payload), 1))
    blob = DATA_BLOB(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _ensure_windows() -> None:
    if not IS_WINDOWS:
        raise DpapiError("DPAPI is only available on Windows")


def _call(name: str, data: bytes, entropy: Optional[bytes]) -> bytes:
    """Invoke one DPAPI function and copy + free its output blob."""
    crypt32, kernel32 = _libraries()

    in_blob, _in_buffer = _to_blob(data)
    entropy_ref = None
    _entropy_buffer = None
    if entropy is not None:
        entropy_blob, _entropy_buffer = _to_blob(entropy)
        entropy_ref = ctypes.byref(entropy_blob)

    out_blob = DATA_BLOB()
    ctypes.set_last_error(0)

    if name == "CryptProtectData":
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            entropy_ref,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            entropy_ref,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )

    if not ok:
        error = ctypes.get_last_error()
        detail = ctypes.FormatError(error).strip() if error else "unknown error"
        raise DpapiError(f"{name} failed (WinError {error}): {detail}")

    try:
        if not out_blob.pbData or not out_blob.cbData:
            return b""
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def protect(data: bytes, entropy: Optional[bytes] = None) -> bytes:
    """Encrypt ``data`` with the current Windows user's DPAPI key."""
    _ensure_windows()
    return _call("CryptProtectData", data, entropy)


def unprotect(blob: bytes, entropy: Optional[bytes] = None) -> bytes:
    """Decrypt a DPAPI blob for the current Windows user."""
    _ensure_windows()
    return _call("CryptUnprotectData", blob, entropy)


def encrypt_password(password: str) -> str:
    """Return ``base64(CryptProtectData(utf8(password)))`` for storage."""
    _ensure_windows()
    if not isinstance(password, str):
        raise DpapiError("password must be a string")
    return base64.b64encode(protect(password.encode("utf-8"))).decode("ascii")


def decrypt_password(token: str) -> str:
    """Decrypt a stored ``password_blob`` back into the plaintext password."""
    _ensure_windows()
    if not isinstance(token, str) or not token:
        raise DpapiError("stored credential is missing or malformed")
    try:
        blob = base64.b64decode(token.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise DpapiError("stored credential is not valid base64") from None
    plaintext = unprotect(blob)
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        raise DpapiError("decrypted credential is not valid UTF-8") from None
