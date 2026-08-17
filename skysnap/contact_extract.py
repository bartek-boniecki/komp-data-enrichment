"""Deterministic contact extraction from HTML/text (no LLM, no paid APIs).

Pulls emails and phones out of raw HTML using high-signal sources first
(``mailto:``/``tel:`` links, schema.org JSON-LD) then falls back to
de-obfuscated regex over visible text. Results are normalized/validated and
scored so callers can prefer programmatically-found contacts over LLM guesses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape

from bs4 import BeautifulSoup

from skysnap.validation import (
    email_domain,
    is_free_email_domain,
    is_role_email,
    is_valid_email_syntax,
    normalize_phone_pl,
)

# Email regex tolerant of surrounding noise; de-obfuscation handled separately.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Polish / international phone shapes: optional +48, separators, 9 national digits.
_PHONE_RE = re.compile(
    r"(?:(?:\+|00)\s?48[\s\-\.]?)?(?:\d[\s\-\.]?){8,11}\d"
)

# Common obfuscations: "jan [at] firma [dot] pl", "jan (małpa) firma", etc.
_OBFUSCATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\s*\(?\[?\s*(?:at|małpa|malpa|monkey|atsign)\s*\]?\)?\s*", re.I), "@"),
    (re.compile(r"\s*\(?\[?\s*(?:dot|kropka)\s*\]?\)?\s*", re.I), "."),
)

# Strings that signal a phone-shaped match is actually something else.
_PHONE_REJECT_CONTEXT = re.compile(
    r"nip|regon|krs|konto|iban|pln|zł|kod\s*poczt|faktur|ul\.|nr\b", re.I
)

_ROLE_HINT_RE = re.compile(
    r"kierownik|dyrektor|prezes|inżynier|inzynier|geodeta|kontakt|sekretariat|"
    r"specjalista|manager|menad|właściciel|wlasciciel|projektant|handlowy|"
    r"przedstawiciel|koordynator",
    re.I,
)


@dataclass
class ContactCandidate:
    value: str
    kind: str  # "email" | "phone"
    source_url: str | None = None
    method: str = "regex"  # mailto | tel | jsonld | regex | deobfuscated
    is_role: bool = False
    is_free_domain: bool = False
    context: str | None = None
    score: float = 0.0


@dataclass
class ExtractedContacts:
    emails: list[ContactCandidate] = field(default_factory=list)
    phones: list[ContactCandidate] = field(default_factory=list)

    def best_email(self, *, prefer_personal: bool = True) -> ContactCandidate | None:
        if not self.emails:
            return None
        if prefer_personal:
            for c in self.emails:  # already score-sorted; first non-generic wins
                if not c.is_role and not c.is_free_domain:
                    return c
        return self.emails[0]

    def is_empty(self) -> bool:
        return not self.emails and not self.phones


_METHOD_WEIGHT = {
    "mailto": 1.0,
    "tel": 1.0,
    "jsonld": 0.95,
    "deobfuscated": 0.7,
    "regex": 0.6,
}


def _deobfuscate(text: str) -> str:
    out = text
    for pattern, repl in _OBFUSCATION_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _score_email(c: ContactCandidate) -> float:
    score = _METHOD_WEIGHT.get(c.method, 0.5)
    if c.is_role:
        score -= 0.25
    if c.is_free_domain:
        score -= 0.15
    if c.context and _ROLE_HINT_RE.search(c.context):
        score += 0.1
    return round(max(0.0, min(score, 1.0)), 3)


def _score_phone(c: ContactCandidate) -> float:
    score = _METHOD_WEIGHT.get(c.method, 0.5)
    if c.context and _ROLE_HINT_RE.search(c.context):
        score += 0.1
    return round(max(0.0, min(score, 1.0)), 3)


def _clean_email(raw: str) -> str | None:
    candidate = unescape(raw).strip().strip(".,;:<>()[]\"' ").lower()
    candidate = candidate.split("?")[0]  # mailto:?subject=
    if not is_valid_email_syntax(candidate):
        return None
    # Drop obvious asset filenames mis-parsed as emails.
    if any(candidate.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return None
    return candidate


def _extract_from_jsonld(soup: BeautifulSoup, url: str | None) -> list[ContactCandidate]:
    found: list[ContactCandidate] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                k = key.lower()
                if k == "email" and isinstance(value, str):
                    email = _clean_email(value.replace("mailto:", ""))
                    if email:
                        found.append(
                            ContactCandidate(email, "email", url, "jsonld")
                        )
                elif k in ("telephone", "phone") and isinstance(value, (str, int)):
                    phone = normalize_phone_pl(str(value))
                    if phone:
                        found.append(ContactCandidate(phone, "phone", url, "jsonld"))
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            _walk(json.loads(raw))
        except Exception:
            continue
    return found


def _context_for(haystack: str, index: int, *, window: int = 60) -> str:
    start = max(0, index - window)
    end = min(len(haystack), index + window)
    return " ".join(haystack[start:end].split())


def extract_contacts_from_html(
    html: str,
    *,
    url: str | None = None,
    restrict_email_domain: str | None = None,
) -> ExtractedContacts:
    """Extract emails/phones from a page, best signal first.

    ``restrict_email_domain`` (optional): when set, role-mailbox scoring still
    applies but emails on other domains are kept and simply scored lower.
    """
    result = ExtractedContacts()
    if not html or not html.strip():
        return result

    soup = BeautifulSoup(html, "lxml")
    seen_emails: set[str] = set()
    seen_phones: set[str] = set()

    def _add_email(raw: str, method: str, context: str | None) -> None:
        email = _clean_email(raw)
        if not email or email in seen_emails:
            return
        seen_emails.add(email)
        dom = email_domain(email)
        cand = ContactCandidate(
            value=email,
            kind="email",
            source_url=url,
            method=method,
            is_role=is_role_email(email),
            is_free_domain=is_free_email_domain(dom),
            context=context,
        )
        if restrict_email_domain and dom and dom != restrict_email_domain.lower():
            cand.score = _score_email(cand) - 0.2
        else:
            cand.score = _score_email(cand)
        result.emails.append(cand)

    def _add_phone(raw: str, method: str, context: str | None, prefix: str | None = None) -> None:
        phone = normalize_phone_pl(raw)
        if not phone or phone in seen_phones:
            return
        # Reject only when an identifier keyword *precedes* the number (i.e. the
        # number is a NIP/REGON/IBAN/postcode), not merely nearby in the page.
        if method == "regex" and prefix and _PHONE_REJECT_CONTEXT.search(prefix):
            return
        seen_phones.add(phone)
        cand = ContactCandidate(
            value=phone,
            kind="phone",
            source_url=url,
            method=method,
            context=context,
        )
        cand.score = _score_phone(cand)
        result.phones.append(cand)

    # 1) mailto: / tel: anchors (highest signal)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        anchor_ctx = " ".join(a.get_text(" ").split()) or None
        if low.startswith("mailto:"):
            _add_email(href[len("mailto:") :], "mailto", anchor_ctx)
        elif low.startswith("tel:"):
            _add_phone(href[len("tel:") :], "tel", anchor_ctx)

    # 2) JSON-LD structured data
    for cand in _extract_from_jsonld(soup, url):
        if cand.kind == "email":
            _add_email(cand.value, "jsonld", None)
        else:
            _add_phone(cand.value, "jsonld", None)

    # 3) Regex over visible (de-obfuscated) text
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ")
    deobf = _deobfuscate(text)

    for m in _EMAIL_RE.finditer(deobf):
        method = "deobfuscated" if m.group(0) not in text else "regex"
        _add_email(m.group(0), method, _context_for(deobf, m.start()))

    for m in _PHONE_RE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if not (9 <= len(digits) <= 13):
            continue
        prefix = " ".join(text[max(0, m.start() - 18) : m.start()].split())
        _add_phone(raw, "regex", _context_for(text, m.start()), prefix=prefix)

    result.emails.sort(key=lambda c: c.score, reverse=True)
    result.phones.sort(key=lambda c: c.score, reverse=True)
    return result


def merge_extracted(parts: list[ExtractedContacts]) -> ExtractedContacts:
    """Combine extractions across pages; dedupe and corroborate.

    A value found on more than one source gets a small confidence boost.
    """
    merged = ExtractedContacts()
    email_index: dict[str, ContactCandidate] = {}
    phone_index: dict[str, ContactCandidate] = {}

    for part in parts:
        for c in part.emails:
            existing = email_index.get(c.value)
            if existing is None:
                email_index[c.value] = c
            elif c.score > existing.score:
                existing.score = min(1.0, c.score + 0.05)
                existing.method = c.method
                existing.source_url = existing.source_url or c.source_url
            else:
                existing.score = min(1.0, existing.score + 0.05)
        for c in part.phones:
            existing = phone_index.get(c.value)
            if existing is None:
                phone_index[c.value] = c
            elif c.score > existing.score:
                existing.score = min(1.0, c.score + 0.05)
            else:
                existing.score = min(1.0, existing.score + 0.05)

    merged.emails = sorted(email_index.values(), key=lambda c: c.score, reverse=True)
    merged.phones = sorted(phone_index.values(), key=lambda c: c.score, reverse=True)
    return merged


def candidates_summary(extracted: ExtractedContacts, *, max_each: int = 8) -> dict:
    """Compact JSON-able view for passing to the LLM as a high-trust hint."""
    return {
        "emails": [
            {
                "email": c.value,
                "is_role": c.is_role,
                "is_free_domain": c.is_free_domain,
                "method": c.method,
                "source_url": c.source_url,
                "score": c.score,
            }
            for c in extracted.emails[:max_each]
        ],
        "phones": [
            {
                "phone": c.value,
                "method": c.method,
                "source_url": c.source_url,
                "score": c.score,
            }
            for c in extracted.phones[:max_each]
        ],
    }
