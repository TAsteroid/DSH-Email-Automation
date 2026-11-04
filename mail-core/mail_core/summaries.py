"""Agent summaries: fixed categories, related-message search and context.

``summary.context`` is the payload an Agent loads *before* writing a summary:
the mail itself, the rest of its thread, related mail (same subject core,
same sender, same company), the summaries that already exist for all of them
(the key requirement: look at history first) and the message's attachments.

Related-mail matching is deliberately local and cheap: every query runs
against the synced ``messages`` table, so what the user can see is what the
Agent can see.
"""

from __future__ import annotations

import email.utils
import re

from .protocol import NOT_FOUND, MailCoreError
from .store import Store

# Per-search and overall caps keep the context small enough for a prompt.
RELATED_PER_SEARCH = 20
RELATED_LIMIT = 20
CONTEXT_SUMMARY_LIMIT = 100

# Reply/forward prefixes in the languages this product sees; stripping is
# repeated because "Re: Fwd: 主题" is common.
_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|回复|转发|答复|轉發)\s*[:：]\s*)+",
    re.IGNORECASE,
)


def normalize_subject(subject: str) -> str:
    """Return the subject core: no Re:/Fwd: prefix, single-spaced, trimmed."""
    text = str(subject or "")
    previous = None
    while text and previous != text:
        previous = text
        text = _PREFIX_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def address_of(value: str) -> str:
    """The bare address from ``Name <addr>`` (fallback: the raw text)."""
    _name, address = email.utils.parseaddr(str(value or ""))
    return (address or str(value or "")).strip()


def sender_company(store: Store, address: str) -> tuple[str, list[str]]:
    """The sender's company and the other address-book emails in it."""
    target = address.strip().casefold()
    if not target:
        return "", []
    contacts = store.list_contacts()
    company = ""
    for contact in contacts:
        if str(contact.get("email") or "").strip().casefold() == target:
            company = str(contact.get("company") or "").strip()
            break
    if not company:
        return "", []
    colleagues: list[str] = []
    for contact in contacts:
        if str(contact.get("company") or "").strip() != company:
            continue
        email = str(contact.get("email") or "").strip()
        if email and email.casefold() != target:
            colleagues.append(email)
    return company, colleagues


def build_context(store: Store, message_id: str) -> dict:
    """Assemble ``summary.context`` for one message."""
    message = store.get_message(message_id)
    if message is None:
        raise MailCoreError(NOT_FOUND, "message not found")

    thread_id = str(message.get("threadId") or "")
    thread: list[dict] = []
    if thread_id:
        container = store.get_thread(thread_id) or {}
        thread = [
            item
            for item in container.get("messages", [])
            if item["id"] != message_id
        ]

    related: list[dict] = []
    seen = {message_id}

    def add(items: list[dict], reason: str) -> None:
        """Merge one search result, keeping the first (highest-priority) reason."""
        for item in items:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            related.append({**item, "reason": reason})

    # Priority order doubles as the reason shown in the UI.
    add(thread, "同一对话")
    core = normalize_subject(message.get("subject", ""))
    if core:
        add(
            store.messages_matching_subject(
                core, exclude_id=message_id, limit=RELATED_PER_SEARCH
            ),
            "主题相关",
        )
    sender = address_of(message.get("fromAddr", ""))
    if sender:
        add(
            store.messages_from_addresses(
                [sender], exclude_id=message_id, limit=RELATED_PER_SEARCH
            ),
            "同一发件人",
        )
        _company, colleagues = sender_company(store, sender)
        if colleagues:
            add(
                store.messages_from_addresses(
                    colleagues, exclude_id=message_id, limit=RELATED_PER_SEARCH
                ),
                "同一公司",
            )
    related = related[:RELATED_LIMIT]

    summary_ids: list[str] = []
    for candidate in (
        [message_id]
        + [item["id"] for item in thread]
        + [item["id"] for item in related]
    ):
        if candidate not in summary_ids:
            summary_ids.append(candidate)
    summaries = [
        {
            "messageId": item["messageId"],
            "subject": item.get("subject", ""),
            "category": item["category"],
            "summary": item["summary"],
            "keyPoints": item["keyPoints"],
            "createdAt": item["createdAt"],
        }
        for item in store.list_summaries(
            message_ids=summary_ids,
            ascending=True,
            limit=CONTEXT_SUMMARY_LIMIT,
        )
    ]

    attachments = [
        {
            "id": item["id"],
            "filename": item["filename"],
            "size": item["size"],
            "fetchedAt": item["fetchedAt"],
        }
        for item in store.list_attachments(message_id=message_id)
    ]

    return {
        "message": message,
        "thread": thread,
        "related": related,
        "summaries": summaries,
        "attachments": attachments,
    }
