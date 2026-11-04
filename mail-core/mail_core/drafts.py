"""Draft content hashing: the value an approval is bound to.

An approval is granted for one exact draft revision, so the hash must be
stable across ``draft.save`` and ``action.execute`` and must change when any
of the four content fields changes.  The material is ``to``, ``cc``, subject
and body joined with ``\\n`` after CRLF/CR are normalised to ``\\n`` (a draft
typed in a browser must hash the same as the same text pasted from a file),
and the result is the lowercase hexadecimal SHA-256 of its UTF-8 bytes.
"""

from __future__ import annotations

import hashlib

HASH_SEPARATOR = "\n"


def normalize(text: str) -> str:
    """Unify line endings so ``\\r\\n`` and ``\\n`` hash identically."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def content_hash(to_addr: str, cc_addr: str, subject: str, body: str) -> str:
    """Return ``sha256(to\\ncc\\nsubject\\nbody)`` as hexadecimal text."""
    material = HASH_SEPARATOR.join(
        normalize(value) for value in (to_addr, cc_addr, subject, body)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
