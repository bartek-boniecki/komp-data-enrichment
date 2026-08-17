"""Map SkySnap leads/exports to HubSpot CRM property payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from skysnap.db import Lead
from skysnap.enrichment import (
    has_personal_contact_data,
    is_real_email,
    resolve_company_name,
    resolve_company_website,
)
from skysnap.models import (
    EnrichmentResult,
    FuzzyDuplicateDecision,
    ProjectSimilarityDecision,
    is_confident_duplicate,
)
from skysnap.kompass_project import (
    map_kompass_typ_to_hubspot,
    sector_hubspot_value,
)
from skysnap.sheet_rows import (
    _build_komentarz,
    _contact_direct_email,
    _contact_direct_phone,
    _contact_linkedin_url,
    _deal_dn_label,
    _split_name,
    _stage_inwestycji,
    format_icp_score_cell,
)
from skysnap.tzutil import get_timezone

HubSpotFollowUpWhen = Literal["always", "personal_contact"]
HubSpotTaskWhen = HubSpotFollowUpWhen
_VALID_TASK_TYPES = frozenset({"CALL", "EMAIL", "TODO"})

# HubSpot-defined association type IDs for tasks (CRM v3 create associations array)
ASSOC_TASK_TO_CONTACT = 204
ASSOC_TASK_TO_COMPANY = 192
ASSOC_TASK_TO_DEAL = 216
# Note → Deal
ASSOC_NOTE_TO_DEAL = 214
ASSOC_NOTE_TO_COMPANY = 190
ASSOC_NOTE_TO_CONTACT = 202

_PL_ZIP_RE = re.compile(r"\b(\d{2}-\d{3})\b")
_PL_VOIVODSHIPS = (
    "dolnośląskie",
    "kujawsko-pomorskie",
    "lubelskie",
    "lubuskie",
    "łódzkie",
    "małopolskie",
    "mazowieckie",
    "opolskie",
    "podkarpackie",
    "podlaskie",
    "pomorskie",
    "śląskie",
    "świętokrzyskie",
    "warmińsko-mazurskie",
    "wielkopolskie",
    "zachodniopomorskie",
)


@dataclass(frozen=True)
class HubSpotWriteConfig:
    pipeline_id: str
    stage_id: str
    sync_company_fields: bool = True
    update_existing_deals: bool = True
    company_owner_id: str | None = None
    prop_project_url: str | None = None
    prop_project_name: str | None = None
    prop_icp_score: str | None = None
    prop_leads_origin: str | None = None
    prop_stage_inwestycji: str | None = None
    prop_deal_typ: str | None = None
    prop_deal_source: str | None = None
    prop_deal_branza: str | None = None
    prop_deal_role: str | None = None
    prop_nip: str | None = None
    prop_opis: str | None = None
    prop_branza_skysnap: str | None = None
    prop_branza_extrainfo: str | None = None
    prop_leads_score: str | None = None
    prop_ai_score: str | None = None
    prop_company_notes: str | None = None
    prop_uslugi: str | None = None
    prop_typ: str | None = None
    prop_konkurencja: str | None = None
    prop_konkurencja_expiry: str | None = None
    prop_voivodeship: str | None = None
    prop_sektor_podsektor: str | None = None
    prop_project_city: str | None = None
    prop_project_voivodeship: str | None = None
    prop_project_street: str | None = None
    prop_project_building_number: str | None = None
    create_analysis_note: bool = True


@dataclass(frozen=True)
class HubSpotFollowUpConfig:
    """Follow-up HubSpot Task created on push."""

    enabled: bool
    when: HubSpotFollowUpWhen
    owner_id: str | None
    task_type: str
    due_days: int
    timezone: str


HubSpotTaskConfig = HubSpotFollowUpConfig


def hubspot_write_config_from_settings(settings: Any) -> HubSpotWriteConfig | None:
    """Return write config when pipeline + stage IDs are configured."""
    pipeline = (settings.hubspot_deal_pipeline_id or "").strip()
    stage = (settings.hubspot_deal_stage_id or "").strip()
    if not pipeline or not stage:
        return None
    sync = (getattr(settings, "hubspot_sync_company_fields", True))
    owner = _strip(getattr(settings, "hubspot_company_owner_id", None)) or _strip(
        getattr(settings, "hubspot_task_owner_id", None)
    )
    return HubSpotWriteConfig(
        pipeline_id=pipeline,
        stage_id=stage,
        sync_company_fields=bool(sync),
        update_existing_deals=(
            getattr(settings, "hubspot_update_existing_deals", True)
        ),
        company_owner_id=owner,
        prop_project_url=_strip(settings.hubspot_prop_project_url),
        prop_project_name=_strip(settings.hubspot_prop_project_name),
        prop_icp_score=_strip(settings.hubspot_prop_icp_score),
        prop_leads_origin=_strip(settings.hubspot_prop_leads_origin),
        prop_stage_inwestycji=_strip(settings.hubspot_prop_stage_inwestycji),
        prop_deal_typ=_strip(settings.hubspot_prop_deal_typ),
        prop_deal_source=_strip(settings.hubspot_prop_deal_source),
        prop_deal_branza=_strip(settings.hubspot_prop_deal_branza),
        prop_deal_role=_strip(getattr(settings, "hubspot_prop_deal_role", None)),
        prop_nip=_strip(settings.hubspot_prop_nip),
        prop_opis=_strip(settings.hubspot_prop_opis),
        prop_branza_skysnap=_strip(settings.hubspot_prop_branza_skysnap),
        prop_branza_extrainfo=_strip(settings.hubspot_prop_branza_extrainfo),
        prop_leads_score=_strip(settings.hubspot_prop_leads_score),
        prop_ai_score=_strip(settings.hubspot_prop_ai_score),
        prop_company_notes=_strip(settings.hubspot_prop_company_notes),
        prop_uslugi=_strip(settings.hubspot_prop_uslugi),
        prop_typ=_strip(settings.hubspot_prop_typ),
        prop_konkurencja=_strip(settings.hubspot_prop_konkurencja),
        prop_konkurencja_expiry=_strip(settings.hubspot_prop_konkurencja_expiry),
        prop_voivodeship=_strip(settings.hubspot_prop_voivodeship),
        prop_sektor_podsektor=_strip(getattr(settings, "hubspot_prop_sektor_podsektor", None)),
        prop_project_city=_strip(getattr(settings, "hubspot_prop_project_city", None)),
        prop_project_voivodeship=_strip(
            getattr(settings, "hubspot_prop_project_voivodeship", None)
        ),
        prop_project_street=_strip(getattr(settings, "hubspot_prop_project_street", None)),
        prop_project_building_number=_strip(
            getattr(settings, "hubspot_prop_project_building_number", None)
        ),
        create_analysis_note=bool(
            getattr(settings, "hubspot_create_analysis_note", True)
        ),
    )


def hubspot_followup_config_from_settings(settings: Any) -> HubSpotFollowUpConfig:
    when_raw = (getattr(settings, "hubspot_task_when", None) or "always").strip().lower()
    when: HubSpotFollowUpWhen = (
        "personal_contact" if when_raw == "personal_contact" else "always"
    )
    task_type = (getattr(settings, "hubspot_task_type", None) or "CALL").strip().upper()
    if task_type not in _VALID_TASK_TYPES:
        task_type = "CALL"
    owner = _strip(getattr(settings, "hubspot_task_owner_id", None))
    due_days = int(getattr(settings, "hubspot_task_due_days", 7) or 7)
    if due_days < 0:
        due_days = 0
    return HubSpotFollowUpConfig(
        enabled=bool(getattr(settings, "hubspot_create_task", True)),
        when=when,
        owner_id=owner,
        task_type=task_type,
        due_days=due_days,
        timezone=getattr(settings, "timezone", None) or "Europe/Warsaw",
    )


def hubspot_task_config_from_settings(settings: Any) -> HubSpotFollowUpConfig:
    return hubspot_followup_config_from_settings(settings)


def should_create_hubspot_followup(
    *,
    followup_config: HubSpotFollowUpConfig,
    created_contact: bool,
) -> bool:
    if not followup_config.enabled:
        return False
    if followup_config.when == "personal_contact":
        return created_contact
    return True


def should_create_hubspot_task(
    *,
    task_config: HubSpotFollowUpConfig,
    created_contact: bool,
) -> bool:
    return should_create_hubspot_followup(
        followup_config=task_config,
        created_contact=created_contact,
    )


def task_due_timestamp_ms(*, timezone: str, due_days: int) -> str:
    """HubSpot hs_timestamp: due datetime as Unix ms (09:00 local)."""
    tz = get_timezone(timezone)
    today = datetime.now(tz).date()
    due_date = today + timedelta(days=int(due_days))
    due_dt = datetime(due_date.year, due_date.month, due_date.day, 9, 0, 0, tzinfo=tz)
    return str(int(due_dt.timestamp() * 1000))


followup_due_timestamp_ms = task_due_timestamp_ms


def build_task_properties(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    decision: FuzzyDuplicateDecision | None,
    *,
    followup_config: HubSpotFollowUpConfig | None = None,
    task_config: HubSpotFollowUpConfig | None = None,
    is_duplicate: bool = False,
    project_similarity: ProjectSimilarityDecision | None = None,
    project_similarity_min_score: int = 60,
) -> dict[str, str] | None:
    """Build HubSpot task properties for a contact-lead follow-up."""
    cfg = followup_config or task_config
    if cfg is None or not cfg.owner_id:
        return None
    subject = f"Skontaktuj się: {deal_name(lead, enrichment)}"[:255]
    body = _build_komentarz(
        lead,
        enrichment=enrichment,
        decision=decision,
        is_duplicate=is_duplicate,
        project_similarity=project_similarity,
        project_similarity_min_score=project_similarity_min_score,
    )
    props: dict[str, str] = {
        "hs_task_subject": subject,
        "hs_task_type": cfg.task_type,
        "hs_task_status": "NOT_STARTED",
        "hs_task_priority": "HIGH",
        "hs_timestamp": task_due_timestamp_ms(
            timezone=cfg.timezone,
            due_days=cfg.due_days,
        ),
        "hubspot_owner_id": cfg.owner_id,
    }
    if body:
        props["hs_task_body"] = body[:65_000]
    return props


def build_task_associations(
    *,
    deal_id: str,
    company_id: str,
    contact_id: str | None = None,
) -> list[dict[str, Any]]:
    """CRM v3 associations payload for task → deal/company/contact."""
    associations: list[dict[str, Any]] = [
        {
            "to": {"id": str(deal_id)},
            "types": [
                {
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": ASSOC_TASK_TO_DEAL,
                }
            ],
        },
        {
            "to": {"id": str(company_id)},
            "types": [
                {
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": ASSOC_TASK_TO_COMPANY,
                }
            ],
        },
    ]
    if contact_id:
        associations.append(
            {
                "to": {"id": str(contact_id)},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": ASSOC_TASK_TO_CONTACT,
                    }
                ],
            }
        )
    return associations


def _strip(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    return str(value).strip()


def _set_prop(props: dict[str, str], key: str | None, value: str | None) -> None:
    if not key:
        return
    text = (value or "").strip()
    if text:
        props[key] = text[:65_000]


def _domain_from_url(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    host = urlparse(url if "://" in url else f"//{url}").netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def parse_polish_address(address: str | None) -> dict[str, str]:
    """Best-effort parse of Kompass firm address into HubSpot address fields."""
    if not address or not address.strip():
        return {}
    text = " ".join(address.replace("\n", ", ").split())
    out: dict[str, str] = {"address": text[:255]}
    zip_match = _PL_ZIP_RE.search(text)
    if zip_match:
        out["zip"] = zip_match.group(1)
        after = text[zip_match.end() :].strip(" ,")
        if after and len(after) < 80 and not after[0].isdigit():
            out["city"] = after.split(",")[0].strip()[:100]
    lower = text.lower()
    for voiv in _PL_VOIVODSHIPS:
        if voiv in lower:
            out["voivodeship"] = voiv.title()
            break
    return out


def _company_linkedin_url(enrichment: EnrichmentResult | None) -> str | None:
    if not enrichment or not enrichment.contact:
        return None
    url = _contact_linkedin_url(enrichment.contact)
    if not url:
        return None
    low = url.lower()
    if "/company/" in low:
        return url
    return None


def _company_phone(enrichment: EnrichmentResult | None) -> str | None:
    if not enrichment:
        return None
    if enrichment.contact:
        phone = _contact_direct_phone(enrichment.contact)
        if phone:
            return phone
    if enrichment.company_generic_phone:
        return enrichment.company_generic_phone.strip()
    return None


def _company_description(enrichment: EnrichmentResult | None, lead: Lead) -> str | None:
    if enrichment and enrichment.project_description and enrichment.project_description.strip():
        return enrichment.project_description.strip()
    # Never put agent/OSINT working notes or bare project titles into company opis.
    return None


def hubspot_company_name(lead: Lead, enrichment: EnrichmentResult | None) -> str:
    """HubSpot company name — never the investment/project title."""
    project = (lead.project_name or "").strip()
    for value in (
        resolve_company_name(lead, enrichment),
        lead.company_name,
        enrichment.company_name if enrichment else None,
    ):
        name = (value or "").strip()
        if not name:
            continue
        if project and name.casefold() == project.casefold():
            continue
        return name
    return "Nieznana firma (KI)"


def _branza_extrainfo(lead: Lead, enrichment: EnrichmentResult | None) -> str | None:
    parts: list[str] = []
    phase = _stage_inwestycji(lead, enrichment)
    if phase:
        parts.append(phase)
    if lead.project_value:
        parts.append(f"Wartość: {lead.project_value}")
    if lead.icp_reason:
        parts.append(lead.icp_reason)
    if enrichment and enrichment.sheet_role:
        parts.append(enrichment.sheet_role)
    return " | ".join(parts) if parts else None


def _uslugi_swiadczone(enrichment: EnrichmentResult | None) -> str | None:
    if enrichment and enrichment.sheet_role:
        return enrichment.sheet_role.strip()
    return None


def leads_score_bucket(icp_score: int) -> str:
    """HubSpot 'Leads Score' is a P1-P4 dropdown, not the raw ICP number."""
    score = int(icp_score or 0)
    if score >= 80:
        return "P1"
    if score >= 65:
        return "P2"
    if score >= 50:
        return "P3"
    return "P4"


def leads_origin_label(source: str | None) -> str:
    """HubSpot 'Leads orygin' dropdown label for a SkySnap lead source."""
    raw = (source or "").strip().lower()
    if "kompas" in raw:
        return "Kompas Inwestycji"
    if raw in ("osint", "website"):
        return "Strona www inwestycji"
    if "gov" in raw or "rzad" in raw or "rząd" in raw:
        return "Strona Rządowa"
    return "Leads Research"


_PUBLIC_ENTITY_RE = re.compile(
    r"\b(gmin|miast|urz[ąa]d|powiat|wojew[óo]dz|starost|szpital|przychodni|szko[łl]|liceum|"
    r"przedszkol|uniwersytet|politechnik|akademi|zarz[ąa]d dr[óo]g|gddkia|nadle[śs]nictw|"
    r"komend|policj|stra[żz]|s[ąa]d |muzeum|bibliotek|samorz[ąa]d|wodoci[ąa]g|kanalizacj|"
    r"skarb pa[ńn]stwa|instytut|centrum kultury|zak[łl]ad komunaln|pkp|zus)",
    re.IGNORECASE,
)
_PRIVATE_ENTITY_RE = re.compile(
    r"(sp\.\s*z\s*o\.?\s*o|sp[óo][łl]ka z ograniczon|\bs\.?a\.?\b|\bsa\b|holding|"
    r"deweloper|invest|development|group|grupa)",
    re.IGNORECASE,
)
_COOPERATIVE_RE = re.compile(r"(sp[óo][łl]dzielni|wsp[óo]lnota)", re.IGNORECASE)


def investment_type_label(lead: Lead, enrichment: EnrichmentResult | None) -> str | None:
    """HubSpot 'Typ inwestycji' dropdown: publiczne / prywatne / publiczno-prawne.

    Prefer Kompass Typ from the project page; fall back to company-name heuristics.
    """
    if enrichment and enrichment.investment_type:
        mapped = map_kompass_typ_to_hubspot(enrichment.investment_type)
        if mapped:
            return mapped
    haystack = " ".join(
        part
        for part in (
            resolve_company_name(lead, enrichment),
            lead.company_name,
            lead.project_name,
        )
        if part
    )
    if not haystack.strip():
        return None
    if _COOPERATIVE_RE.search(haystack):
        return "publiczno-prawne"
    if _PUBLIC_ENTITY_RE.search(haystack):
        return "publiczne"
    if _PRIVATE_ENTITY_RE.search(haystack):
        return "prywatne"
    return None


def build_analysis_note_body(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    decision: FuzzyDuplicateDecision | None,
    *,
    is_duplicate: bool = False,
    project_similarity: ProjectSimilarityDecision | None = None,
    project_similarity_min_score: int = 60,
) -> str | None:
    """Full SkySnap agent analysis for a HubSpot timeline Note."""
    body = _build_komentarz(
        lead,
        enrichment=enrichment,
        decision=decision,
        is_duplicate=is_duplicate,
        project_similarity=project_similarity,
        project_similarity_min_score=project_similarity_min_score,
    )
    return body.strip() if body and body.strip() else None


def analysis_note_hash(body: str) -> str:
    """Stable fingerprint so resync skips duplicate timeline Notes."""
    import hashlib

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def capitalize_voivodeship(raw: str | None) -> str | None:
    """Normalize 'podkarpackie' → 'Podkarpackie' for HubSpot enum options."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # Already title-ish from parse_polish_address helpers
    lower = text.casefold()
    for name in _PL_VOIVODSHIPS:
        if name.casefold() == lower:
            return name[0].upper() + name[1:] if name else name
    # Fallback: capitalize first letter, keep Polish chars
    return text[0].upper() + text[1:] if text else None


def deal_name(lead: Lead, enrichment: EnrichmentResult | None) -> str:
    return f"KI: {_deal_dn_label(lead, enrichment)}"


def build_company_properties(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    *,
    write_config: HubSpotWriteConfig,
    decision: FuzzyDuplicateDecision | None = None,
    is_duplicate: bool = False,
    project_similarity: ProjectSimilarityDecision | None = None,
    project_similarity_min_score: int = 60,
) -> dict[str, str]:
    name = hubspot_company_name(lead, enrichment)
    props: dict[str, str] = {"name": name[:255]}
    website = resolve_company_website(lead, enrichment)
    domain = _domain_from_url(website)
    if domain:
        props["domain"] = domain
    if website:
        props["website"] = website[:500]
    if lead.country and lead.country.strip():
        props["country"] = lead.country.strip()[:100]

    if not write_config.sync_company_fields:
        if enrichment and enrichment.company_nip and write_config.prop_nip:
            props[write_config.prop_nip] = enrichment.company_nip.strip()
        return props

    parsed: dict[str, str] = {}
    if enrichment and enrichment.company_address:
        parsed = parse_polish_address(enrichment.company_address)

    city = lead.city or parsed.get("city")
    if city:
        props["city"] = city[:100]
    if parsed.get("zip"):
        props["zip"] = parsed["zip"][:20]
    if parsed.get("address"):
        props["address"] = parsed["address"][:255]

    voivodeship = parsed.get("voivodeship")
    if voivodeship:
        props["state"] = voivodeship[:100]
        _set_prop(props, write_config.prop_voivodeship, voivodeship)

    description = _company_description(enrichment, lead)
    if description:
        if write_config.prop_opis:
            props[write_config.prop_opis] = description[:65_000]
        else:
            props["description"] = description[:65_000]

    notes = _build_komentarz(
        lead,
        enrichment=enrichment,
        decision=decision,
        is_duplicate=is_duplicate,
        project_similarity=project_similarity,
        project_similarity_min_score=project_similarity_min_score,
    )
    _set_prop(props, write_config.prop_company_notes, notes)

    linkedin = _company_linkedin_url(enrichment)
    if linkedin:
        props["linkedin_company_page"] = linkedin[:500]

    phone = _company_phone(enrichment)
    if phone:
        props["phone"] = phone[:50]

    if enrichment and enrichment.company_nip and write_config.prop_nip:
        props[write_config.prop_nip] = enrichment.company_nip.strip()

    if enrichment and enrichment.sheet_branza:
        _set_prop(props, write_config.prop_branza_skysnap, enrichment.sheet_branza)

    _set_prop(props, write_config.prop_branza_extrainfo, _branza_extrainfo(lead, enrichment))

    _set_prop(props, write_config.prop_leads_score, leads_score_bucket(lead.icp_score))

    _set_prop(props, write_config.prop_uslugi, _uslugi_swiadczone(enrichment))

    _set_prop(props, write_config.prop_leads_origin, leads_origin_label(lead.source))

    if write_config.company_owner_id:
        props["hubspot_owner_id"] = write_config.company_owner_id

    return props


def build_contact_properties(
    lead: Lead,
    enrichment: EnrichmentResult | None,
) -> dict[str, str] | None:
    if not enrichment or not has_personal_contact_data(enrichment) or not enrichment.contact:
        return None
    contact = enrichment.contact
    email = _contact_direct_email(contact)
    if not is_real_email(email):
        return None
    first, last = _split_name(contact.full_name)
    props: dict[str, str] = {"email": email.strip().lower()}
    if first:
        props["firstname"] = first[:100]
    if last:
        props["lastname"] = last[:100]
    phone = _contact_direct_phone(contact)
    if phone:
        props["phone"] = phone[:50]
    role = enrichment.sheet_role or contact.role
    if role:
        props["jobtitle"] = role[:100]
    linkedin = _contact_linkedin_url(contact)
    if linkedin:
        props["hs_linkedin_url"] = linkedin[:500]
    return props


def build_deal_properties(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    decision: FuzzyDuplicateDecision | None,
    *,
    write_config: HubSpotWriteConfig,
    is_duplicate: bool = False,
    project_similarity: ProjectSimilarityDecision | None = None,
    project_similarity_min_score: int = 60,
    for_update: bool = False,
) -> dict[str, str]:
    props: dict[str, str] = {
        "dealname": deal_name(lead, enrichment)[:255],
    }
    if not for_update:
        props["pipeline"] = write_config.pipeline_id
        props["dealstage"] = write_config.stage_id
    # Opis transakcji = Kompass project description only (agent analysis → timeline Note).
    project_desc = (
        enrichment.project_description.strip()
        if enrichment and enrichment.project_description and enrichment.project_description.strip()
        else None
    )
    if project_desc:
        props["description"] = project_desc[:65_000]
    project_url = lead.project_url or ""
    if project_url and write_config.prop_project_url:
        props[write_config.prop_project_url] = project_url[:500]
    if lead.project_name and write_config.prop_project_name:
        props[write_config.prop_project_name] = lead.project_name[:255]
    if write_config.prop_icp_score:
        props[write_config.prop_icp_score] = str(int(lead.icp_score))
    _set_prop(props, write_config.prop_deal_source, leads_origin_label(lead.source))
    _set_prop(props, write_config.prop_stage_inwestycji, _stage_inwestycji(lead, enrichment))
    _set_prop(props, write_config.prop_deal_typ, investment_type_label(lead, enrichment))
    if enrichment and enrichment.sheet_branza:
        _set_prop(props, write_config.prop_deal_branza, enrichment.sheet_branza)
    if enrichment and enrichment.sheet_role:
        _set_prop(props, write_config.prop_deal_role, enrichment.sheet_role)
    _set_prop(props, write_config.prop_ai_score, format_icp_score_cell(lead))
    if enrichment:
        _set_prop(
            props,
            write_config.prop_sektor_podsektor,
            sector_hubspot_value(enrichment.sector_subsector),
        )
        _set_prop(props, write_config.prop_project_city, enrichment.project_city)
        _set_prop(
            props,
            write_config.prop_project_voivodeship,
            capitalize_voivodeship(enrichment.project_voivodeship),
        )
        _set_prop(props, write_config.prop_project_street, enrichment.project_street)
        _set_prop(
            props,
            write_config.prop_project_building_number,
            enrichment.project_building_number,
        )
    return props


def resolve_company_id(
    decision: FuzzyDuplicateDecision | None,
) -> str | None:
    """Existing HubSpot company to attach to — confident matches only.

    A fuzzy low-confidence "match" previously attached the deal AND its
    contact to a similarly-named but different company.
    """
    if is_confident_duplicate(decision):
        return str(decision.matched_company_id).strip() or None
    return None


def resolve_existing_deal_id(
    project_similarity: ProjectSimilarityDecision | None,
    *,
    min_score: int = 60,
    update_enabled: bool = True,
) -> str | None:
    """Return HubSpot deal ID to update when project similarity indicates same opportunity."""
    if not update_enabled or project_similarity is None:
        return None
    deal_id = (project_similarity.matched_deal_id or "").strip()
    if not deal_id:
        return None
    match_class = project_similarity.match_class
    if match_class == "same_project":
        return deal_id
    if match_class == "addon" and int(project_similarity.similarity_pct) >= int(min_score):
        return deal_id
    return None
