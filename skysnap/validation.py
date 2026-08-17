"""Email and phone validation / normalization for contact enrichment.

All helpers are free (no paid APIs) and degrade gracefully when the optional
``phonenumbers`` / ``dnspython`` packages are unavailable. Email "validation"
is deliverability-of-domain (MX record) plus syntax — never SMTP probing, which
is unreliable and often blocked.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

try:  # optional dependency
    import phonenumbers  # type: ignore

    _HAS_PHONENUMBERS = True
except Exception:  # pragma: no cover - import guard
    _HAS_PHONENUMBERS = False

try:  # optional dependency
    import dns.resolver  # type: ignore

    _HAS_DNS = True
except Exception:  # pragma: no cover - import guard
    _HAS_DNS = False


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Generic mailbox local-parts that are not a person's direct address.
_ROLE_LOCALPARTS = frozenset(
    {
        "biuro",
        "office",
        "kontakt",
        "contact",
        "info",
        "sekretariat",
        "recepcja",
        "reception",
        "rekrutacja",
        "kariera",
        "praca",
        "hr",
        "marketing",
        "sprzedaz",
        "sales",
        "handel",
        "zamowienia",
        "zamowienie",
        "sklep",
        "shop",
        "serwis",
        "service",
        "pomoc",
        "support",
        "reklamacje",
        "faktury",
        "ksiegowosc",
        "administracja",
        "poczta",
        "mail",
        "email",
        "no-reply",
        "noreply",
        "newsletter",
    }
)

# Free / personal mail providers — a match here means "not a company domain".
_FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "wp.pl",
        "o2.pl",
        "onet.pl",
        "onet.eu",
        "interia.pl",
        "interia.eu",
        "poczta.fm",
        "poczta.onet.pl",
        "op.pl",
        "go2.pl",
        "tlen.pl",
        "gazeta.pl",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "yahoo.com",
        "yahoo.pl",
        "icloud.com",
        "proton.me",
        "protonmail.com",
    }
)


def is_valid_email_syntax(email: str | None) -> bool:
    if not email:
        return False
    return bool(_EMAIL_RE.match(email.strip()))


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower().rstrip(".")
    return domain or None


def email_localpart(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[0].strip().lower() or None


def is_free_email_domain(domain: str | None) -> bool:
    return bool(domain) and domain in _FREE_EMAIL_DOMAINS


def is_role_email(email: str | None) -> bool:
    """True for generic mailboxes (biuro@, kontakt@, info@ ...)."""
    local = email_localpart(email)
    if not local:
        return False
    base = re.split(r"[._\-+]", local)[0]
    return local in _ROLE_LOCALPARTS or base in _ROLE_LOCALPARTS


@lru_cache(maxsize=2048)
def domain_has_mx(domain: str, *, timeout: float = 4.0) -> bool:
    """Return True if the domain resolves an MX (or A as fallback) record.

    Cached per-process. Returns True when DNS lookups are unavailable so we
    never drop a syntactically valid address just because dnspython is missing
    or the network is flaky.
    """
    if not domain:
        return False
    if not _HAS_DNS:
        return True
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout
    try:
        answers = resolver.resolve(domain, "MX")
        return len(answers) > 0
    except Exception:
        pass
    try:  # some small PL hosts have only A records but still accept mail
        answers = resolver.resolve(domain, "A")
        return len(answers) > 0
    except Exception:
        return False


def validate_email(email: str | None, *, check_mx: bool = True) -> dict:
    """Return a structured verdict for an email address.

    Keys: ``email`` (normalized), ``valid_syntax``, ``has_mx``,
    ``is_role``, ``is_free_domain``, ``deliverable`` (syntax AND mx).
    """
    normalized = (email or "").strip().lower()
    syntax_ok = is_valid_email_syntax(normalized)
    domain = email_domain(normalized) if syntax_ok else None
    has_mx = bool(domain) and domain_has_mx(domain) if (syntax_ok and check_mx) else syntax_ok
    return {
        "email": normalized if syntax_ok else None,
        "valid_syntax": syntax_ok,
        "has_mx": bool(has_mx),
        "is_role": is_role_email(normalized) if syntax_ok else False,
        "is_free_domain": is_free_email_domain(domain),
        "deliverable": bool(syntax_ok and has_mx),
    }


def is_real_email_deliverable(email: str | None, *, check_mx: bool = True) -> bool:
    """True when the email is syntactically valid and its domain accepts mail."""
    verdict = validate_email(email, check_mx=check_mx)
    return bool(verdict["deliverable"])


def normalize_phone_pl(raw: str | None, *, default_region: str = "PL") -> str | None:
    """Return an E.164 phone string (e.g. +48221234567) or None if invalid."""
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    if not _HAS_PHONENUMBERS:
        digits = re.sub(r"[^\d+]", "", candidate)
        if digits.startswith("+") and 8 <= len(digits) - 1 <= 15:
            return digits
        if len(digits) == 9:  # bare PL national number
            return f"+48{digits}"
        if digits.startswith("48") and len(digits) == 11:
            return f"+{digits}"
        return None
    try:
        parsed = phonenumbers.parse(candidate, default_region)
    except Exception:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


# Letters that NFKD does not decompose to ASCII (stroke is part of the glyph).
_SPECIAL_LETTER_MAP = str.maketrans(
    {
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ß": "ss",
    }
)


def _strip_accents(text: str) -> str:
    text = text.translate(_SPECIAL_LETTER_MAP)
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def _split_name(full_name: str) -> tuple[str, str] | None:
    cleaned = _strip_accents(full_name).lower().strip()
    cleaned = re.sub(r"[^a-z\s\-]", "", cleaned)
    parts = [p for p in re.split(r"[\s\-]+", cleaned) if len(p) > 1]
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def infer_email_pattern(example_emails: list[str], domain: str) -> str | None:
    """Infer the corporate local-part template from personal emails on *domain*.

    E.g. seeing ``j.kowalski@firma.pl`` and ``a.nowak@firma.pl`` yields
    ``"{fi}.{last}"`` — the strongest possible prior for guessing a new
    person's address at the same company. Returns the most common template
    or None when nothing unambiguous is found.
    """
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain:
        return None
    counts: dict[str, int] = {}
    for email in example_emails:
        email = (email or "").strip().lower()
        if "@" not in email:
            continue
        local, _, dom = email.partition("@")
        if dom != domain or is_role_email(email):
            continue
        template: str | None = None
        if re.fullmatch(r"[a-z]\.[a-z][a-z-]{2,}", local):
            template = "{fi}.{last}"
        elif re.fullmatch(r"[a-z]{2,}\.[a-z][a-z-]{2,}", local):
            template = "{first}.{last}"
        elif re.fullmatch(r"[a-z]{2,}_[a-z]{2,}", local):
            template = "{first}_{last}"
        elif re.fullmatch(r"[a-z][a-z]", local):
            template = "{fi}{li}"
        if template:
            counts[template] = counts.get(template, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def guess_email_patterns(
    full_name: str,
    domain: str,
    *,
    preferred_pattern: str | None = None,
) -> list[str]:
    """Common PL/EU corporate email patterns for a person at a domain.

    Ordered most-likely first; when *preferred_pattern* (a template inferred
    from other emails on the same domain) is given, that guess goes first.
    Used only as a *candidate* generator; callers should confirm the domain
    has MX before trusting any of these.
    """
    domain = (domain or "").strip().lower().lstrip("@")
    split = _split_name(full_name or "")
    if not domain or not split:
        return []
    first, last = split
    fi, li = first[0], last[0]
    fields = {"first": first, "last": last, "fi": fi, "li": li}
    templates = [
        "{first}.{last}",
        "{fi}.{last}",
        "{first}{last}",
        "{fi}{last}",
        "{last}.{first}",
        "{first}",
        "{last}",
        "{first}_{last}",
        "{fi}{li}",
    ]
    if preferred_pattern and preferred_pattern in templates:
        templates.remove(preferred_pattern)
        templates.insert(0, preferred_pattern)
    seen: set[str] = set()
    ordered: list[str] = []
    for template in templates:
        p = f"{template.format(**fields)}@{domain}"
        if p not in seen and is_valid_email_syntax(p):
            seen.add(p)
            ordered.append(p)
    return ordered
