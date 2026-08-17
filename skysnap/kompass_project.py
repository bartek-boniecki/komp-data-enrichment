"""Parse Kompass project-page labels (Typ, sektor, location)."""

from __future__ import annotations

import re
from dataclasses import dataclass


def _label_value(text: str, labels: tuple[str, ...]) -> str | None:
    """Return the first non-empty value after a label on its own line / colon form."""
    if not text:
        return None
    for label in labels:
        # Label on its own line, value on the next non-empty line.
        pat = re.compile(
            rf"(?im)^\s*{re.escape(label)}\s*$[\r\n]+\s*(.+?)\s*$"
        )
        m = pat.search(text)
        if m:
            val = m.group(1).strip()
            if val and not _looks_like_label(val):
                return val
        # Inline "Label: value"
        pat2 = re.compile(
            rf"(?im)^\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$"
        )
        m2 = pat2.search(text)
        if m2:
            val = m2.group(1).strip()
            if val and not _looks_like_label(val):
                return val
    return None


_KNOWN_LABELS = frozenset(
    {
        "typ",
        "sektor, podsektor",
        "sektor",
        "podsektor",
        "województwo",
        "wojewodztwo",
        "powiat",
        "miasto",
        "kod pocztowy",
        "adres",
        "numery działek",
        "numery dzialek",
        "ogólne informacje",
        "ogolne informacje",
        "etap",
    }
)


def _looks_like_label(value: str) -> bool:
    return value.strip().lower().rstrip(":") in _KNOWN_LABELS


@dataclass(frozen=True)
class KompassProjectMeta:
    investment_type: str | None = None  # raw Kompass Typ, e.g. Publiczna
    sector_subsector: str | None = None
    city: str | None = None
    voivodeship: str | None = None
    street: str | None = None
    building_number: str | None = None
    project_description: str | None = None


def parse_kompass_project_meta(text: str) -> KompassProjectMeta:
    """Extract structured project fields from noisy Kompass page text."""
    raw = text or ""
    typ = _label_value(raw, ("Typ", "TYP"))
    sector = _label_value(raw, ("Sektor, podsektor", "Sektor", "Podsektor"))
    city = _label_value(raw, ("Miasto",))
    voiv = _label_value(raw, ("Województwo", "Wojewodztwo"))
    address = _label_value(raw, ("Adres",))
    street, number = _split_street_number(address)
    description = _label_value(
        raw,
        ("Ogólne informacje", "Ogolne informacje", "Opis inwestycji", "Opis"),
    )
    return KompassProjectMeta(
        investment_type=typ,
        sector_subsector=sector,
        city=city,
        voivodeship=voiv,
        street=street,
        building_number=number,
        project_description=description,
    )


_STREET_NUM_RE = re.compile(
    r"^(?P<street>.+?)\s+(?P<num>\d[\w/\-]*)\s*$",
    re.UNICODE,
)


def _split_street_number(address: str | None) -> tuple[str | None, str | None]:
    if not address:
        return None, None
    addr = address.strip()
    # "ul. Jaćmierz" — no building number
    m = _STREET_NUM_RE.match(addr)
    if not m:
        return addr, None
    street = m.group("street").strip()
    # Avoid treating "ul. 3 Maja" style as number-only endings incorrectly —
    # if street is very short after split, keep whole address as street.
    if len(street) < 3:
        return addr, None
    return street, m.group("num").strip()


def map_kompass_typ_to_hubspot(raw: str | None) -> str | None:
    """Map Kompass Typ label to HubSpot typ_inwestycji option value."""
    if not raw:
        return None
    key = re.sub(r"\s+", " ", raw.strip().lower())
    key = (
        key.replace("ą", "a")
        .replace("ć", "c")
        .replace("ę", "e")
        .replace("ł", "l")
        .replace("ń", "n")
        .replace("ó", "o")
        .replace("ś", "s")
        .replace("ź", "z")
        .replace("ż", "z")
    )
    if "publiczno" in key or "prawn" in key:
        return "publiczno-prawne"
    if key.startswith("publicz") or key == "publiczna" or key == "publiczne":
        return "publiczne"
    if key.startswith("prywat"):
        return "prywatne"
    return None


def sector_hubspot_value(raw: str | None) -> str | None:
    """Reduce Kompass 'niemieszkaniowy - X' to HubSpot checkbox option text X."""
    if not raw:
        return None
    text = raw.strip()
    if " - " in text:
        text = text.split(" - ", 1)[1].strip()
    elif " – " in text:
        text = text.split(" – ", 1)[1].strip()
    return text or None


def merge_project_meta_into_enrichment_updates(
    meta: KompassProjectMeta,
    *,
    existing: dict[str, object | None],
) -> dict[str, object]:
    """Fill empty enrichment fields from parsed Kompass meta (never overwrite non-empty)."""
    updates: dict[str, object] = {}
    mapping = {
        "investment_type": meta.investment_type,
        "sector_subsector": meta.sector_subsector,
        "project_city": meta.city,
        "project_voivodeship": meta.voivodeship,
        "project_street": meta.street,
        "project_building_number": meta.building_number,
        "project_description": meta.project_description,
    }
    for key, value in mapping.items():
        if not value:
            continue
        cur = existing.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            updates[key] = value
    return updates
