"""Thread-assembly rules shared by the message store.

A thread id is ``<accountId>:<rootMessageId>``.  Prefixing the account keeps
two mailboxes that happen to contain the same ``Message-ID`` from being merged
(two accounts may both hold the very same message, and their threads must stay
separate).  ``rootMessageId`` is the oldest reference the message carries, or
the message's own ``Message-ID`` when it has no usable references.

The rules (also documented in ``docs/IPC_PROTOCOL.md``):

1. A message whose ``References`` / ``In-Reply-To`` hits a message already in
   the local index joins that message's thread.
2. A message with references but no local hit roots the thread at the earliest
   reference (the first ``References`` entry, else ``In-Reply-To``).
3. A message with no references roots its own thread at its ``Message-ID``.

The functions here are pure so both the incremental insert path and the
one-off backfill in :mod:`mail_core.store` use exactly the same rules.
"""

from __future__ import annotations

import re

# RFC 5322 ``msg-id`` in angle brackets.  Real-world headers occasionally
# contain comments or stray words between ids, so the bracketed form wins.
_MSG_ID = re.compile(r"<[^<>\s]+>")


def message_ids(raw: str) -> list[str]:
    """Extract ``Message-ID`` tokens from a ``References``/``In-Reply-To`` value.

    Bracketed ids are preferred (they are the RFC form and skip comments such
    as ``(forwarded by ...)``); a value without angle brackets degrades to
    whitespace-separated tokens so a bare ``abc@example.com`` still works.
    Order is preserved and duplicates are dropped.
    """
    text = raw or ""
    if "<" in text:
        tokens = _MSG_ID.findall(text)
    else:
        tokens = text.split()
    seen: set[str] = set()
    ids: list[str] = []
    for token in tokens:
        token = token.strip()
        if token and token not in seen:
            seen.add(token)
            ids.append(token)
    return ids


def reference_chain(references: str, in_reply_to: str) -> list[str]:
    """All referenced ids, oldest first, with ``In-Reply-To`` appended.

    ``References`` is the full ancestor chain (oldest first); ``In-Reply-To``
    is the immediate parent and normally already appears there.  Adding it at
    the end covers clients that send only ``In-Reply-To``.
    """
    chain = message_ids(references)
    for message_id in message_ids(in_reply_to):
        if message_id not in chain:
            chain.append(message_id)
    return chain


def thread_key(account_id: str, root_message_id: str) -> str:
    """Account-scoped thread id; the root is the oldest referenced msg-id."""
    return f"{account_id}:{root_message_id}"


def resolve_thread_id(
    account_id: str,
    message_id: str,
    references: str,
    in_reply_to: str,
    lookup,
) -> str:
    """Apply the merge rules using ``lookup(msg_id) -> thread_id | None``.

    ``lookup`` must only see messages of the same account (the caller does
    that filtering) and should ignore rows whose ``thread_id`` is still empty.
    """
    chain = reference_chain(references, in_reply_to)
    for reference in chain:
        existing = lookup(reference)
        if existing:
            return str(existing)
    root = chain[0] if chain else (message_id or "")
    return thread_key(account_id, root)
