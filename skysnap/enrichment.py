from __future__ import annotations

import re
from typing import Literal

from skysnap.db import Lead
from skysnap.models import EnrichmentResult, WebsiteContact
from skysnap.validation import is_role_email

_KOMPASS_NOISE_RE = re.compile(
    r"widoczny|ukryty|zamówienia publiczne|zamowienia publiczne|data kontaktu|"
    r"bez telefonu|tylko email|visible email|hidden",
    re.I,
)

ContactGapKind = Literal["complete", "partial", "no_name"]

_GENERIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "wp.pl",
        "onet.pl",
        "interia.pl",
        "o2.pl",
        "poczta.fm",
        "hotmail.com",
        "outlook.com",
        "yahoo.com",
        "icloud.com",
    }
)


# --- Self / platform contact leakage -------------------------------------- #
# Kompass renders the *logged-in* account (SkySnap's own login) and a platform
# switchboard number on nearly every project page. Without scrubbing, the same
# email / phone / website leak into most leads. These are safe hard defaults;
# extend them via config (kompass_username is auto-added).
_SELF_EMAIL_DOMAINS_DEFAULT = frozenset({"skysnap.pl"})
_BLOCKED_EMAILS_DEFAULT = frozenset({"pawel.wojcik@skysnap.pl"})
_BLOCKED_PHONE_NATIONALS_DEFAULT = frozenset({"226332685"})
_BLOCKED_WEBSITE_HOSTS_DEFAULT = frozenset({"skysnap.pl"})


def phone_national_digits(value: str | None) -> str:
    """Last 9 (Polish national) digits, ignoring +/country code/formatting."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return digits[-9:] if len(digits) > 9 else digits


def website_host(value: str | None) -> str:
    if not value:
        return ""
    host = re.sub(r"^https?://", "", value.strip().lower()).split("/", 1)[0]
    return host[4:] if host.startswith("www.") else host


def is_blocked_email(
    value: str | None,
    *,
    domains: frozenset[str] = _SELF_EMAIL_DOMAINS_DEFAULT,
    emails: frozenset[str] = _BLOCKED_EMAILS_DEFAULT,
) -> bool:
    if not value or not value.strip():
        return False
    low = value.strip().lower()
    if low in emails:
        return True
    domain = low.split("@", 1)[-1] if "@" in low else ""
    return bool(domain) and domain in domains


def is_blocked_phone(
    value: str | None,
    *,
    nationals: frozenset[str] = _BLOCKED_PHONE_NATIONALS_DEFAULT,
) -> bool:
    nat = phone_national_digits(value)
    return bool(nat) and nat in nationals


def is_blocked_website(
    value: str | None,
    *,
    hosts: frozenset[str] = _BLOCKED_WEBSITE_HOSTS_DEFAULT,
) -> bool:
    host = website_host(value)
    return bool(host) and host in hosts


def scrub_platform_contacts(
    enrichment: EnrichmentResult | None,
    *,
    email_domains: frozenset[str] | None = None,
    emails: frozenset[str] | None = None,
    phone_nationals: frozenset[str] | None = None,
    website_hosts: frozenset[str] | None = None,
) -> EnrichmentResult | None:
    """Strip the SkySnap login / Kompass platform contact that leaks onto pages."""
    if not enrichment:
        return enrichment

    email_domains = _SELF_EMAIL_DOMAINS_DEFAULT | (email_domains or frozenset())
    emails = _BLOCKED_EMAILS_DEFAULT | (emails or frozenset())
    phone_nationals = _BLOCKED_PHONE_NATIONALS_DEFAULT | (phone_nationals or frozenset())
    website_hosts = _BLOCKED_WEBSITE_HOSTS_DEFAULT | (website_hosts or frozenset())

    def _bad_email(v: str | None) -> bool:
        return is_blocked_email(v, domains=email_domains, emails=emails)

    def _bad_phone(v: str | None) -> bool:
        return is_blocked_phone(v, nationals=phone_nationals)

    updates: dict[str, object] = {}
    if is_blocked_website(enrichment.website, hosts=website_hosts):
        updates["website"] = None
    if _bad_email(enrichment.company_generic_email):
        updates["company_generic_email"] = None
    if _bad_phone(enrichment.company_generic_phone):
        updates["company_generic_phone"] = None

    contact = enrichment.contact
    if contact:
        c_updates: dict[str, object] = {}
        for attr in ("email", "direct_email"):
            if _bad_email(getattr(contact, attr, None)):
                c_updates[attr] = None
        for attr in ("phone", "direct_phone"):
            if _bad_phone(getattr(contact, attr, None)):
                c_updates[attr] = None
        if c_updates:
            new_contact = contact.model_copy(update=c_updates)
            has_name = bool(
                new_contact.full_name
                and not is_placeholder_contact_value(new_contact.full_name)
            )
            if not (has_name or _contact_has_personal_fields(new_contact)):
                new_contact = None
            updates["contact"] = new_contact

    if not updates:
        return enrichment
    return enrichment.model_copy(update=updates)


def is_placeholder_contact_value(value: str | None) -> bool:
    if not value or not value.strip():
        return True
    low = value.strip().lower()
    if "widoczny" in low or "ukryty" in low or "hidden" in low:
        return True
    if low in {"email", "telefon", "phone", "e-mail", "tel"}:
        return True
    if low.startswith("widoczny "):
        return True
    return False


def is_real_email(email: str | None) -> bool:
    if not email or is_placeholder_contact_value(email):
        return False
    return "@" in email and "." in email.split("@", 1)[-1]


def has_contact_name(enrichment: EnrichmentResult | None) -> bool:
    if not enrichment or not enrichment.contact:
        return False
    return bool(
        enrichment.contact.full_name
        and not is_placeholder_contact_value(enrichment.contact.full_name)
    )


def needs_name_gap_fill(
    enrichment: EnrichmentResult | None,
    *,
    company_name: str | None,
) -> bool:
    return bool(company_name and company_name.strip()) and not has_contact_name(enrichment)


def needs_channel_gap_fill(enrichment: EnrichmentResult | None) -> bool:
    if not has_contact_name(enrichment) or not enrichment or not enrichment.contact:
        return False
    contact = enrichment.contact
    has_email = is_real_email(contact.email)
    has_phone = bool(contact.phone and not is_placeholder_contact_value(contact.phone))
    return not has_email or not has_phone


def contact_gap_kind(enrichment: EnrichmentResult | None) -> ContactGapKind:
    """Whether OSINT should try to complete the contact."""
    if not has_contact_name(enrichment):
        return "no_name"
    if needs_channel_gap_fill(enrichment):
        return "partial"
    return "complete"


def needs_contact_gap_search(
    enrichment: EnrichmentResult | None,
    *,
    company_name: str | None,
) -> bool:
    if has_personal_contact_data(enrichment):
        return needs_channel_gap_fill(enrichment)
    if has_generic_contact_data(enrichment):
        # Generic company channels are exportable but OSINT should still try to upgrade.
        return True
    return needs_name_gap_fill(enrichment, company_name=company_name) or needs_channel_gap_fill(
        enrichment
    )


def sanitize_search_term(text: str | None, *, max_len: int = 60) -> str:
    """Strip Kompass noise so web dorks stay short and valid."""
    if not text or not text.strip():
        return ""
    cleaned = " ".join(text.split())
    if ":" in cleaned and _KOMPASS_NOISE_RE.search(cleaned):
        cleaned = cleaned.split(":", 1)[0].strip()
    cleaned = _KOMPASS_NOISE_RE.sub("", cleaned).strip(" ,:-")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rsplit(" ", 1)[0]
    return cleaned


_LEGAL_SUFFIXES: tuple[str, ...] = (
    " Sp. z o.o.",
    " sp. z o.o.",
    " Spółka z o.o.",
    " spółka z o.o.",
    " S.A.",
    " SA",
    " Sp. j.",
    " Sp.k.",
    " sp.k.",
    " Sp. z o.o. Sp.k.",
    " sp. z o.o. sp.k.",
)
_LEGAL_PREFIX_RE = re.compile(
    r"^(?:przedsiębiorstwo|przedsiebiorstwo|firma|grupa|zakład|zaklad)\s+",
    re.I,
)


def short_company_name_for_search(company_name: str | None) -> str:
    """Brand-style short name for web dorks, e.g. 'Przedsiębiorstwo MOLTER Sp. z o.o.' -> 'MOLTER'."""
    cleaned = sanitize_search_term(company_name, max_len=120)
    if not cleaned:
        return ""

    name = cleaned
    for suffix in _LEGAL_SUFFIXES:
        if name.lower().endswith(suffix.lower()):
            name = name[: -len(suffix)].strip()
            break
    name = re.sub(r"\s+sp\.?\s*z\.?\s*o\.?\s*o\.?\s*$", "", name, flags=re.I).strip()
    name = re.sub(r"\s+s\.?\s*a\.?\s*$", "", name, flags=re.I).strip()
    if " w " in name.lower():
        name = re.split(r"\s+w\s+", name, maxsplit=1, flags=re.I)[0].strip()

    while True:
        stripped = _LEGAL_PREFIX_RE.sub("", name, count=1).strip()
        if stripped == name:
            break
        name = stripped

    tokens = [t for t in name.split() if t]
    if not tokens:
        return ""

    # Distinctive ALL-CAPS brand (MOLTER, Jot-Ł, M&J)
    caps_brands = [
        t
        for t in tokens
        if len(t) >= 2 and t.upper() == t and any(c.isalpha() for c in t)
    ]
    if len(caps_brands) == 1:
        return caps_brands[0]
    if len(caps_brands) > 1:
        return caps_brands[-1]

    if len(tokens) <= 3:
        return " ".join(tokens)
    # Long descriptive name with no distinctive brand: keep the first few words
    # so the query stays specific (e.g. "Wojewódzki Szpital Specjalistyczny"),
    # rather than collapsing to a single, often meaningless, leading adjective.
    return " ".join(tokens[:3])


def resolve_company_name(lead: Lead, enrichment: EnrichmentResult | None) -> str | None:
    """Company shown in the sheet / used for searches and dedupe.

    Precedence matters for attribution: ``lead.company_name`` comes from the
    notification EMAIL and often names the investor/procuring authority, while
    both extraction prompts define ``enrichment.company_name`` as "the
    organization the contact works for". Preferring the lead name paired a GW
    employee with the investor's company in the export.

    The enriched name therefore wins when it is trustworthy:
    - ``source == "kompass"`` — read off the authenticated project page /
      firm profile / contact modal, or
    - the enrichment carries a NAMED contact — person and employer must
      travel together.
    Otherwise (contact-less OSINT guesses) the email-derived name stays first.
    """
    enriched = (
        enrichment.company_name.strip()
        if enrichment and enrichment.company_name and enrichment.company_name.strip()
        else None
    )
    if enriched:
        kompass_verified = enrichment.source == "kompass"
        anchored_to_contact = bool(
            enrichment.contact
            and enrichment.contact.full_name
            and not is_placeholder_contact_value(enrichment.contact.full_name)
        )
        if kompass_verified or anchored_to_contact:
            return enriched
    for value in (lead.company_name, enriched):
        if value and value.strip():
            return value.strip()
    return None


_STAKEHOLDER_SIGNAL_RE = re.compile(
    r"\bgw\b|generalny\s+wykonaw|generalnego\s+wykonawcy|"
    r"\binwestor\b|zamawiaj[aą]c",
    re.I,
)

_GW_BRANZA = "Generalni wykonawcy"


def has_identified_stakeholder(
    lead: Lead,
    enrichment: EnrichmentResult | None,
) -> bool:
    """True when an investor or GW company/role is known (no personal contact required)."""
    company = resolve_company_name(lead, enrichment)
    if company and not is_placeholder_contact_value(company) and len(company.strip()) >= 2:
        return True

    if enrichment and enrichment.sheet_branza == _GW_BRANZA:
        return True

    signal_parts: list[str] = []
    if lead.company_name:
        signal_parts.append(lead.company_name)
    if lead.project_phase:
        signal_parts.append(lead.project_phase)
    if lead.icp_reason:
        signal_parts.append(lead.icp_reason)
    if enrichment:
        for part in (
            enrichment.company_name,
            enrichment.sheet_role,
            enrichment.sheet_branza,
            enrichment.notes,
            enrichment.contact.role if enrichment.contact else None,
        ):
            if part and str(part).strip():
                signal_parts.append(str(part))
    hay = " ".join(signal_parts)
    return bool(_STAKEHOLDER_SIGNAL_RE.search(hay))


def qualifies_for_stakeholder_export(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    *,
    min_icp: int = 60,
) -> bool:
    """Export without personal contact when ICP is high and GW/investor is identified."""
    if int(lead.icp_score) < int(min_icp):
        return False
    return has_identified_stakeholder(lead, enrichment)


def qualifies_for_phase_a_export(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    *,
    min_icp: int = 60,
) -> bool:
    """Phase A (Kompass) sheet export: personal, generic company channels, or ICP threshold."""
    if has_personal_contact_data(enrichment):
        return True
    if has_generic_contact_data(enrichment):
        return True
    return int(lead.icp_score) >= int(min_icp)


def infer_website_from_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    if not local.strip() or not domain.strip():
        return None
    domain = domain.lower().strip()
    if domain in _GENERIC_EMAIL_DOMAINS:
        return None
    return f"https://{domain}"


def resolve_company_website(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    *,
    kompass_url_ok: bool = False,
) -> str:
    if enrichment and enrichment.website:
        url = enrichment.website.strip()
        if url and (kompass_url_ok or "kompas" not in url.lower()):
            return url
    if enrichment and enrichment.contact and enrichment.contact.email:
        inferred = infer_website_from_email(enrichment.contact.email)
        if inferred:
            return inferred
    if lead.project_url and "kompas" not in lead.project_url.lower():
        return lead.project_url
    return ""


def _contact_has_personal_fields(contact: WebsiteContact) -> bool:
    if contact.full_name and not is_placeholder_contact_value(contact.full_name):
        return True
    for email in (contact.direct_email, contact.email):
        if is_real_email(email) and not is_role_email(email):
            return True
    return False


def has_personal_contact_data(enrichment: EnrichmentResult | None) -> bool:
    """True when we have a named person or a non-generic personal email."""
    if not enrichment or not enrichment.contact:
        return False
    return _contact_has_personal_fields(enrichment.contact)


def has_generic_contact_data(enrichment: EnrichmentResult | None) -> bool:
    """True when only company switchboard channels are available (Kompass profile)."""
    if not enrichment:
        return False
    if enrichment.company_generic_email and is_real_email(enrichment.company_generic_email):
        return True
    if enrichment.company_generic_phone and not is_placeholder_contact_value(
        enrichment.company_generic_phone
    ):
        return True
    return False


def has_exportable_contact_data(enrichment: EnrichmentResult | None) -> bool:
    """Personal contact, or generic company channels as a last-resort sheet fallback."""
    return has_personal_contact_data(enrichment) or has_generic_contact_data(enrichment)


def separate_generic_contact_channels(
    enrichment: EnrichmentResult | None,
) -> EnrichmentResult | None:
    """Move role/company-only channels out of contact into company_generic_* fields."""
    if not enrichment or not enrichment.contact:
        return enrichment

    contact = enrichment.contact
    generic_email = enrichment.company_generic_email
    generic_phone = enrichment.company_generic_phone
    if has_contact_name(enrichment):
        return enrichment

    contact_updates: dict[str, object] = {}
    for attr in ("email", "direct_email"):
        value = getattr(contact, attr, None)
        if is_real_email(value):
            generic_email = generic_email or value.strip()
            contact_updates[attr] = None
    for attr in ("phone", "direct_phone"):
        value = getattr(contact, attr, None)
        if value and not is_placeholder_contact_value(value):
            generic_phone = generic_phone or value.strip()
            contact_updates[attr] = None

    if not contact_updates and generic_email == enrichment.company_generic_email:
        if generic_phone == enrichment.company_generic_phone:
            return enrichment

    updated_contact = contact.model_copy(update=contact_updates)
    if not _contact_has_personal_fields(updated_contact):
        updated_contact = None

    return enrichment.model_copy(
        update={
            "contact": updated_contact,
            "company_generic_email": generic_email,
            "company_generic_phone": generic_phone,
        }
    )


def has_contact_data(enrichment: EnrichmentResult | None) -> bool:
    """Alias for personal contact — used for Kompass daily quota."""
    return has_personal_contact_data(enrichment)


def enrichment_source_label(source: str) -> str:
    labels = {
        "kompass": "Kompass",
        "osint": "OSINT",
        "website": "Website",
    }
    return labels.get(source, source)
