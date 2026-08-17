"""Parse Kompass v2 firm profile pages (authenticated)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from skysnap.contact_extract import extract_contacts_from_html
from skysnap.scrape import html_to_text
from skysnap.validation import is_role_email, normalize_phone_pl

# English + Polish labels used on kompasinwestycji.pl firm profiles.
_WEBSITE_RE = re.compile(
    r"(?:company website|strona (?:www|internetowa)(?: firmy)?)\s*[:\n]\s*(https?://\S+)",
    re.I,
)
_ADDRESS_RE = re.compile(
    r"(?:company address|adres(?: firmy)?)\s*[:\n]\s*"
    r"(.+?)(?=\n\s*(?:Tax Identification|NIP|Company phone|Telefon|Company email|Email|Poland|Polska)|\Z)",
    re.S | re.I,
)
_NIP_RE = re.compile(
    r"(?:tax identification number|nip|numer nip)\s*[:\n]\s*(\d[\d\s\-]{8,12}\d)",
    re.I,
)
_PHONES_RE = re.compile(
    r"(?:company phone number|telefon(?: firmy)?|numer telefonu)\s*[:\n]\s*([^\n]+)",
    re.I,
)
_EMAIL_RE = re.compile(
    r"(?:company email|e-?mail(?: firmy)?|adres e-?mail)\s*[:\n]\s*([^\s@]+@[^\s@]+\.[^\s@]+)",
    re.I,
)
_PROFILE_ID_RE = re.compile(r"/(?:v2/)?firma/(\d+)", re.I)


@dataclass(frozen=True)
class KompassFirmProfile:
    profile_url: str
    company_name: str | None = None
    website: str | None = None
    address: str | None = None
    nip: str | None = None
    email: str | None = None
    phones: str | None = None  # raw display string, may list multiple numbers
    # True when the "Pokaż kontakt" click actually exposed an email or phone.
    contact_revealed: bool = False


def profile_url_from_href(href: str, *, base_url: str = "https://www.kompasinwestycji.pl") -> str | None:
    match = _PROFILE_ID_RE.search(href or "")
    if not match:
        return None
    return f"{base_url.rstrip('/')}/v2/firma/{match.group(1)}"


def parse_firm_profile_from_page(
    *,
    html: str,
    visible_text: str,
    profile_url: str,
    company_name_hint: str | None = None,
) -> KompassFirmProfile:
    """Extract labeled firm fields from profile HTML/text after 'show contact' if needed."""
    text = visible_text.strip()
    if len(text) < 40:
        text = html_to_text(html)

    website = _first_group(_WEBSITE_RE, text)
    address = _clean_multiline(_first_group(_ADDRESS_RE, text))
    nip_raw = _first_group(_NIP_RE, text)
    nip = re.sub(r"\D", "", nip_raw) if nip_raw else None
    phones = _first_group(_PHONES_RE, text)
    email = (_first_group(_EMAIL_RE, text) or "").strip().lower() or None

    extracted = extract_contacts_from_html(html, url=profile_url)
    if not email and extracted.emails:
        role_emails = [c.value for c in extracted.emails if is_role_email(c.value)]
        email = (role_emails[0] if role_emails else extracted.emails[0].value).strip().lower()
    if not phones and extracted.phones:
        normalized = [normalize_phone_pl(c.value) or c.value.strip() for c in extracted.phones[:3]]
        phones = ",".join(p for p in normalized if p)

    company_name = (company_name_hint or "").strip() or None
    if not company_name:
        company_name = _company_name_from_html(html)

    return KompassFirmProfile(
        profile_url=profile_url,
        company_name=company_name,
        website=website,
        address=address,
        nip=nip,
        email=email,
        phones=phones,
        contact_revealed=bool(email or phones),
    )


def merge_firm_profile_into_updates(
    profile: KompassFirmProfile,
    *,
    existing_company: str | None = None,
    existing_website: str | None = None,
) -> dict[str, str]:
    """Build EnrichmentResult field updates from a firm profile scrape."""
    updates: dict[str, str] = {}
    if profile.company_name and not (existing_company or "").strip():
        updates["company_name"] = profile.company_name
    if profile.website and not (existing_website or "").strip():
        updates["website"] = profile.website
    if profile.email:
        updates["company_generic_email"] = profile.email
    if profile.phones:
        updates["company_generic_phone"] = profile.phones
    if profile.address:
        updates["company_address"] = profile.address
    if profile.nip:
        updates["company_nip"] = profile.nip
    return updates


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).strip()


def _clean_multiline(value: str | None) -> str | None:
    if not value:
        return None
    lines = [ln.strip() for ln in value.splitlines() if ln.strip()]
    return ", ".join(lines) if lines else None


def _company_name_from_html(html: str) -> str | None:
    match = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    if match:
        name = match.group(1).strip()
        if name and "kompas" not in name.lower():
            return name
    return None
