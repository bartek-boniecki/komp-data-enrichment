from __future__ import annotations

import email
from email.message import Message
import html as html_lib
import imaplib
from typing import Any, Iterable


def _iter_parts(msg: Message) -> Iterable[Message]:
    if msg.is_multipart():
        for part in msg.walk():
            yield part
    else:
        yield msg


def _decode_text_payload(part: Message) -> str | None:
    payload = part.get_payload(decode=True)
    if not payload:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_best_html(msg: Message) -> str | None:
    html_parts: list[str] = []
    for part in _iter_parts(msg):
        ctype = (part.get_content_type() or "").lower()
        if ctype != "text/html":
            continue
        decoded = _decode_text_payload(part)
        if decoded:
            html_parts.append(decoded)
    if html_parts:
        # Prefer the longest HTML part.
        return sorted(html_parts, key=len, reverse=True)[0]
    return None


def extract_best_body_as_html(msg: Message) -> str | None:
    """Prefer HTML; fall back to longest text/plain wrapped for Claude."""
    best = extract_best_html(msg)
    if best:
        return best
    plain_parts: list[str] = []
    for part in _iter_parts(msg):
        ctype = (part.get_content_type() or "").lower()
        if ctype != "text/plain":
            continue
        decoded = _decode_text_payload(part)
        if decoded and decoded.strip():
            plain_parts.append(decoded)
    if not plain_parts:
        return None
    text = sorted(plain_parts, key=len, reverse=True)[0]
    return f"<pre>{html_lib.escape(text)}</pre>"


class ImapEmail:
    def __init__(self, *, message_id: str | None, subject: str | None, received_at: str | None, html: str) -> None:
        self.message_id = message_id
        self.subject = subject
        self.received_at = received_at
        self.html = html


def _search_charset_for_query(search_query: str) -> str | None:
    """Non-ASCII in the query requires CHARSET UTF-8 for SUBJECT/BODY on most servers."""
    if any(ord(c) > 127 for c in search_query):
        return "UTF-8"
    return None


def _parse_search_criteria(search_query: str) -> list[str]:
    """Turn env search string into imaplib SEARCH *criteria arguments.

    Gmail extension searches must be two atoms, e.g. SEARCH X-GM-RAW "is:unread foo".
    A single string like ``X-GM-RAW inwestycjach`` is rejected by Gmail with BAD.
    Use ``X-GM-RAW:inwestycjach`` or ``X-GM-RAW is:unread subject:foo`` in .env.
    """
    q = search_query.strip()
    upper = q.upper()
    if upper.startswith("X-GM-RAW:"):
        return ["X-GM-RAW", q[len("X-GM-RAW:") :].strip()]
    if upper.startswith("X-GM-RAW "):
        return ["X-GM-RAW", q[len("X-GM-RAW ") :].strip()]
    return [q]


def _gmail_imap_hint(search_query: str, *, host: str, unseen_count: int) -> str | None:
    if "gmail" not in host.lower():
        return None
    if unseen_count != 0:
        return None
    q_upper = search_query.upper()
    if "UNSEEN" not in q_upper and "NOT SEEN" not in q_upper and "IS:UNREAD" not in q_upper:
        return None
    return (
        "Gmail INBOX often has 0 IMAP UNSEEN messages even when the web UI still shows unread "
        "(preview pane, categories, or prior IMAP clients mark \\Seen). "
        'Try IMAP_SEARCH_QUERY=X-GM-RAW:inwestycjach (or your subject fragment) without UNSEEN.'
    )


def fetch_unseen_html_emails(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    search_query: str,
    mark_seen: bool = True,
) -> tuple[list[ImapEmail], dict[str, Any]]:
    meta: dict[str, Any] = {
        "imap_search_typ": None,
        "imap_search_charset": None,
        "imap_search_criteria": None,
        "imap_mailbox_total": None,
        "imap_unseen_count": None,
        "imap_search_matched": 0,
        "imap_skipped_empty_body": 0,
        "imap_skipped_fetch_failed": 0,
        "imap_hint": None,
    }
    m = imaplib.IMAP4_SSL(host, port)
    try:
        m.login(username, password)
        typ, select_data = m.select(folder)
        meta["imap_select_typ"] = typ
        if typ == "OK" and select_data and select_data[0]:
            try:
                meta["imap_mailbox_total"] = int(select_data[0])
            except (TypeError, ValueError):
                pass
        criteria = _parse_search_criteria(search_query)
        meta["imap_search_criteria"] = criteria
        charset = _search_charset_for_query(search_query)
        # UTF8=ACCEPT (used by some servers) forbids non-None charset on SEARCH.
        if charset and getattr(m, "utf8_enabled", False):
            charset = None
        meta["imap_search_charset"] = charset
        if charset:
            typ, data = m.search(charset, *criteria)
        else:
            typ, data = m.search(None, *criteria)
        meta["imap_search_typ"] = typ
        if typ != "OK":
            return [], meta
        unseen_typ, unseen_data = m.search(None, "UNSEEN")
        if unseen_typ == "OK" and unseen_data and unseen_data[0] is not None:
            meta["imap_unseen_count"] = len(unseen_data[0].split())
        meta["imap_hint"] = _gmail_imap_hint(
            search_query,
            host=host,
            unseen_count=int(meta["imap_unseen_count"] or 0),
        )
        ids = (data[0] or b"").split()
        meta["imap_search_matched"] = len(ids)
        results: list[ImapEmail] = []
        for msg_id in ids:
            typ, fetched = m.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not fetched or not fetched[0]:
                meta["imap_skipped_fetch_failed"] += 1
                continue
            raw = fetched[0][1]
            msg = email.message_from_bytes(raw)
            html = extract_best_body_as_html(msg)
            if not html:
                meta["imap_skipped_empty_body"] += 1
                continue
            message_id = msg.get("Message-ID")
            subject = msg.get("Subject")
            received_at = msg.get("Date")
            results.append(ImapEmail(message_id=message_id, subject=subject, received_at=received_at, html=html))
            if mark_seen:
                m.store(msg_id, "+FLAGS", "\\Seen")
        return results, meta
    finally:
        try:
            m.close()
        except Exception:
            pass
        try:
            m.logout()
        except Exception:
            pass

