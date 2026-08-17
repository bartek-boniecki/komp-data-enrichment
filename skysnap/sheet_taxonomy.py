from __future__ import annotations

import re

from skysnap.models import EnrichmentResult

SHEET_ROLES: tuple[str, ...] = (
    "Kierownik do spraw branżowych",
    "Kierownik budowy",
    "Kierownik robót ziemnych",
    "Geodeta",
    "Inżynier budowy",
    "Rozliczeniowiec",
    "Koordynator do spraw geodezji",
    "Inżynier kontraktu",
    "BHP",
    "Specjalista do spraw ofertowania",
    "Kierownik wytwarzania mas bitumicznych",
    "Kierownik projektu",
    "Właściciel DSP",
    "Właściciel firmy geodyzyjnej",
    "Sekretariat",
    "Kierownik kontraktu",
    "Przedstawiciel Wykonawcy",
    "Biuro geodezyjne",
    "Dyrektor kontraktu",
    "Inne.",
)

SHEET_BRANZE: tuple[str, ...] = (
    "Dostawca Usług Dronowych (DSP)",
    "Nieruchomości",
    "Samorząd",
    "Ubezpieczenia",
    "Geodezja",
    "Kopalnie",
    "Zarządzanie Odpadami",
    "Roboty ziemne",
    "Kruszywa",
    "Generalni wykonawcy",
    "Projektanci",
    "Nadzór inwestycji",
    "OZE",
    "Telekomunikacja",
    "Studenci",
    "Edukacja i Stowarzyszenia",
    "Media branżowe",
    "Dystrybutorzy",
    "Inne.",
)

DEAL_STAGE_LEADS_RESEARCH = "1.0 Leads Research"
PIPELINE_SALES = "Sales Pipeline"


def claude_taxonomy_instructions() -> str:
    roles = "; ".join(SHEET_ROLES)
    branze = "; ".join(SHEET_BRANZE)
    return (
        "Also set sheet_role and sheet_branza using ONLY these exact allowed values.\n"
        f"sheet_role options: {roles}\n"
        f"sheet_branza options: {branze}\n"
        "Pick the closest match; use 'Inne.' when unsure."
    )

_ROLE_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"geodez|geodet", "Geodeta"),
    (r"biuro geodezyj", "Biuro geodezyjne"),
    (r"właściciel dsp|wlasciciel dsp", "Właściciel DSP"),
    (r"właściciel firmy geodezyj|wlasciciel firmy geodezyj", "Właściciel firmy geodyzyjnej"),
    (r"dyrektor kontrakt", "Dyrektor kontraktu"),
    (r"kierownik kontrakt", "Kierownik kontraktu"),
    (r"inżynier kontrakt|inzynier kontrakt", "Inżynier kontraktu"),
    (r"kierownik projekt", "Kierownik projektu"),
    (r"kierownik budow", "Kierownik budowy"),
    (r"robót ziemn|robot ziemn", "Kierownik robót ziemnych"),
    (r"branżow|branzow", "Kierownik do spraw branżowych"),
    (r"ofertow|dział ofert|dzial ofert|przetarg", "Specjalista do spraw ofertowania"),
    (r"przygotowan.*inwestyc|inwestycj.*przygotow", "Kierownik projektu"),
    (r"mas bitum", "Kierownik wytwarzania mas bitumicznych"),
    (r"koordynator.*geodez", "Koordynator do spraw geodezji"),
    (r"rozliczeni", "Rozliczeniowiec"),
    (r"inżynier budow|inzynier budow", "Inżynier budowy"),
    (r"\bbhp\b", "BHP"),
    (r"sekretariat", "Sekretariat"),
    (r"przedstawiciel.*wykonaw", "Przedstawiciel Wykonawcy"),
    (r"zamówien|zamowien|publiczn", "Sekretariat"),
)

_BRANZA_KEYWORDS: tuple[tuple[str, str], ...] = (
    (r"\bgw\b|generalny wykonaw", "Generalni wykonawcy"),
    (r"geodez", "Geodezja"),
    (r"samorząd|gmina|powiat|miasto|urząd|urzad", "Samorząd"),
    (r"projektant|architekt|pb \+ pw", "Projektanci"),
    (r"nadzór|nadzor", "Nadzór inwestycji"),
    (r"roboty ziemn", "Roboty ziemne"),
    (r"kruszyw", "Kruszywa"),
    (r"kopaln", "Kopalnie"),
    (r"odpad", "Zarządzanie Odpadami"),
    (r"oze|fotowolta|wiatr", "OZE"),
    (r"telekom|teleinformat", "Telekomunikacja"),
    (r"dsp|dron", "Dostawca Usług Dronowych (DSP)"),
    (r"ubezpiecz", "Ubezpieczenia"),
    (r"nieruchom", "Nieruchomości"),
    (r"dystrybut", "Dystrybutorzy"),
    (r"media", "Media branżowe"),
    (r"edukac|stowarzysz|student", "Edukacja i Stowarzyszenia"),
)


def _pick_allowed(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    if not value or not value.strip():
        return default
    cleaned = value.strip()
    for option in allowed:
        if cleaned.lower() == option.lower():
            return option
    return default


def map_role(raw_role: str | None, *, company_name: str | None = None) -> str:
    """Map a person's raw job title onto the sheet taxonomy.

    Keyword matching runs on ``raw_role`` ONLY. ``company_name`` is accepted
    for API compatibility but deliberately ignored: matching it turned
    'Prezes Zarządu' at 'Zakład Robót Ziemnych X' into 'Kierownik robót
    ziemnych' and any title at a '...Geodezja...' firm into 'Geodeta' —
    company-name tokens describe the firm (branża), never the person's title.
    """
    hay = (raw_role or "").lower()
    if hay.strip():
        for pattern, role in _ROLE_KEYWORDS:
            if re.search(pattern, hay, re.I):
                return role
    return _pick_allowed(raw_role, SHEET_ROLES, "Inne.")


def map_branza(
    raw: str | None,
    *,
    company_name: str | None = None,
    contact_role: str | None = None,
    project_name: str | None = None,
) -> str:
    hay = " ".join(p for p in (raw, company_name, contact_role, project_name) if p).lower()
    if hay:
        for pattern, branza in _BRANZA_KEYWORDS:
            if re.search(pattern, hay, re.I):
                return branza
    return _pick_allowed(raw, SHEET_BRANZE, "Inne.")


def apply_sheet_taxonomy(
    enrichment: EnrichmentResult | None,
    *,
    company_name: str | None,
    project_name: str | None,
) -> EnrichmentResult | None:
    if enrichment is None:
        return None
    contact_role = enrichment.contact.role if enrichment.contact else None
    sheet_role = enrichment.sheet_role or map_role(contact_role, company_name=company_name)
    sheet_branza = enrichment.sheet_branza or map_branza(
        enrichment.sheet_branza,
        company_name=company_name,
        contact_role=contact_role,
        project_name=project_name,
    )
    sheet_role = _pick_allowed(sheet_role, SHEET_ROLES, map_role(contact_role, company_name=company_name))
    sheet_branza = _pick_allowed(sheet_branza, SHEET_BRANZE, map_branza(None, company_name=company_name, project_name=project_name))
    return enrichment.model_copy(update={"sheet_role": sheet_role, "sheet_branza": sheet_branza})
