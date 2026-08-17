from __future__ import annotations

from typing import Any

from skysnap.db import Lead
from skysnap.enrichment import (
    enrichment_source_label,
    has_identified_stakeholder,
    has_personal_contact_data,
    is_placeholder_contact_value,
    is_real_email,
    resolve_company_name,
    resolve_company_website,
)
from skysnap.validation import is_role_email
from skysnap.models import (
    EnrichmentResult,
    FuzzyDuplicateDecision,
    ProjectSimilarityDecision,
    is_confident_duplicate,
)
from skysnap.project_dedup import format_deal_similarity_cell
from skysnap.sheet_taxonomy import (
    DEAL_STAGE_LEADS_RESEARCH,
    PIPELINE_SALES,
    apply_sheet_taxonomy,
    map_branza,
    map_role,
)


def normalize_header(header: str) -> str:
    return " ".join(header.strip().lower().split())


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    if not full or not full.strip():
        return None, None
    parts = full.strip().split(None, 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else None
    return first, last


def _is_kompass_url(url: str | None) -> bool:
    if not url:
        return False
    lower = url.lower()
    return "kompas" in lower or "kompass" in lower


def _sheet_role_value(lead: Lead, enrichment: EnrichmentResult | None) -> str:
    if enrichment and enrichment.sheet_role:
        return enrichment.sheet_role
    contact_role = enrichment.contact.role if enrichment and enrichment.contact else None
    return map_role(contact_role, company_name=resolve_company_name(lead, enrichment))


def _sheet_branza_value(lead: Lead, enrichment: EnrichmentResult | None) -> str:
    if enrichment and enrichment.sheet_branza:
        return enrichment.sheet_branza
    contact_role = enrichment.contact.role if enrichment and enrichment.contact else None
    return map_branza(
        None,
        company_name=resolve_company_name(lead, enrichment),
        contact_role=contact_role,
        project_name=lead.project_name,
    )


def format_icp_score_cell(lead: Lead) -> str:
    """ICP Score column: numeric score plus concise rubric reason."""
    score = int(lead.icp_score)
    reason = (lead.icp_reason or "").strip()
    if reason:
        return f"{score} — {reason}"
    return str(score)


def _build_komentarz(
    lead: Lead,
    *,
    enrichment: EnrichmentResult | None,
    decision: FuzzyDuplicateDecision | None,
    is_duplicate: bool,
    project_similarity: ProjectSimilarityDecision | None = None,
    project_similarity_min_score: int = 60,
) -> str | None:
    parts: list[str] = []
    loc = ", ".join(p for p in (lead.city, lead.country) if p)
    if loc:
        parts.append(f"Lokalizacja: {loc}")
    if lead.project_value:
        parts.append(f"Wartość: {lead.project_value}")
    if enrichment:
        parts.append(f"Enrichment: {enrichment_source_label(enrichment.source)}")
    if enrichment and enrichment.notes:
        parts.append(enrichment.notes)
    if enrichment and not has_personal_contact_data(enrichment) and has_identified_stakeholder(
        lead, enrichment
    ):
        parts.append("Eksport bez osoby kontaktowej — zidentyfikowany GW/inwestor")
    elif enrichment and not has_personal_contact_data(enrichment) and (
        enrichment.company_generic_email or enrichment.company_generic_phone
    ):
        parts.append("Kontakt firmowy (ogólny) — brak osoby kontaktowej")
    elif (
        not has_personal_contact_data(enrichment)
        and not (
            enrichment
            and (enrichment.company_generic_email or enrichment.company_generic_phone)
        )
        and int(lead.icp_score) >= 60
    ):
        parts.append("Eksport bez kontaktu — ICP ≥ 60")
    if enrichment and enrichment.company_address:
        parts.append(f"Adres: {enrichment.company_address}")
    if enrichment and enrichment.company_nip:
        parts.append(f"NIP: {enrichment.company_nip}")
    if is_duplicate and decision:
        parts.append(
            f"Duplikat HubSpot: {decision.matched_company_name or decision.matched_company_id} "
            f"(pewność {decision.confidence:.0%})"
        )
        if decision.reasoning:
            parts.append(decision.reasoning)
    elif decision and decision.is_duplicate and decision.matched_company_id:
        # Fuzzy match below the confidence gate: not acted upon (no company
        # attach, no skipped_duplicate status) but surfaced for human review.
        parts.append(
            f"Możliwy duplikat HubSpot (niezatwierdzony): "
            f"{decision.matched_company_name or decision.matched_company_id} "
            f"(pewność {decision.confidence:.0%})"
        )
    note_threshold = int(project_similarity_min_score) if int(project_similarity_min_score) > 0 else 60
    if (
        project_similarity
        and project_similarity.similarity_pct >= note_threshold
        and project_similarity.matched_deal_id
    ):
        parts.append(
            f"Podobieństwo dealu: {project_similarity.similarity_pct}% "
            f"({project_similarity.match_class})"
        )
        if project_similarity.reasoning:
            parts.append(project_similarity.reasoning)
    parts.append(f"SkySnap lead_id={lead.id}")
    return " | ".join(parts) if parts else None


def _short_company_label(company_name: str | None) -> str:
    """Match manual sheet style: 'Budimex S.A.' -> 'Budimex', 'NDI S.A.' -> 'NDI'."""
    if not company_name or not company_name.strip():
        return ""
    name = company_name.strip()
    for suffix in (
        " Sp. z o.o.",
        " sp. z o.o.",
        " Spółka z o.o.",
        " S.A.",
        " SA",
        " Sp. j.",
    ):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
            break
    if " w " in name:
        name = name.split(" w ", 1)[0].strip()
    return name.split()[0] if name else ""


def _deal_dn_label(lead: Lead, enrichment: EnrichmentResult | None) -> str:
    """DN column: '{ShortCo}, {project}' (see existing sheet rows)."""
    short = _short_company_label(resolve_company_name(lead, enrichment))
    if short:
        return f"{short}, {lead.project_name}"
    return lead.project_name


def _contact_linkedin_url(contact) -> str:
    if contact.linkedin_url and contact.linkedin_url.strip():
        return contact.linkedin_url.strip()
    source = contact.source_url or ""
    if "linkedin.com" in source.lower():
        return source.strip()
    return ""


def _contact_direct_phone(contact) -> str:
    for value in (contact.direct_phone, contact.phone):
        if value and not is_placeholder_contact_value(value):
            return value.strip()
    return ""


def _contact_direct_email(contact) -> str:
    if is_real_email(contact.direct_email):
        return contact.direct_email.strip()
    # Fallback to the general email only when it is personal — a biuro@/kontakt@
    # mailbox in the "Email Direct" column would misattribute a generic channel.
    if is_real_email(contact.email) and not is_role_email(contact.email):
        return contact.email.strip()
    return ""


def _contact_phone(contact, enrichment: EnrichmentResult | None) -> str:
    if contact:
        for value in (contact.phone, contact.direct_phone):
            if value and not is_placeholder_contact_value(value):
                return value.strip()
    if enrichment and enrichment.company_generic_phone:
        if not has_personal_contact_data(enrichment):
            return enrichment.company_generic_phone.strip()
    return ""


def _contact_email(contact, enrichment: EnrichmentResult | None) -> str:
    if contact:
        for value in (contact.email, contact.direct_email):
            if is_real_email(value):
                return value.strip()
    if enrichment and enrichment.company_generic_email:
        if not has_personal_contact_data(enrichment):
            return enrichment.company_generic_email.strip()
    return ""


def _stage_inwestycji(lead: Lead, enrichment: EnrichmentResult | None) -> str:
    if enrichment and enrichment.project_phase and enrichment.project_phase.strip():
        return enrichment.project_phase.strip()
    if lead.project_phase and lead.project_phase.strip():
        return lead.project_phase.strip()
    return ""


def _sheet_value(value: Any) -> str | int | float:
    """Google Sheets append must use '' not None or later columns shift left."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TAK" if value else "NIE"
    return value


def cell_for_header(
    header: str,
    *,
    lead: Lead,
    enrichment: EnrichmentResult | None,
    decision: FuzzyDuplicateDecision | None,
    project_similarity: ProjectSimilarityDecision | None = None,
    project_similarity_min_score: int = 60,
) -> Any:
    """Map one sheet column header to a cell value."""
    company_name = resolve_company_name(lead, enrichment)
    enrichment = apply_sheet_taxonomy(
        enrichment,
        company_name=company_name,
        project_name=lead.project_name,
    )
    norm = normalize_header(header)
    contact = enrichment.contact if enrichment and enrichment.contact else None
    first_name, last_name = _split_name(contact.full_name if contact else None)
    is_duplicate = is_confident_duplicate(decision)
    kompass_url = lead.project_url if _is_kompass_url(lead.project_url) else None
    website = resolve_company_website(lead, enrichment)
    sheet_role = _sheet_role_value(lead, enrichment)
    sheet_branza = _sheet_branza_value(lead, enrichment)

    if not norm:
        return ""
    if norm == "adress pobrany przez kogos innego":
        return ""
    if norm == "orygin link":
        return kompass_url or lead.project_url or ""
    if norm == "nazwa inwestycji":
        return lead.project_name
    if norm == "company name":
        return company_name or ""
    if norm == "icp score":
        return format_icp_score_cell(lead)
    if norm == "deal similarity":
        return format_deal_similarity_cell(project_similarity)
    if norm == "ki:":
        return "KI:"
    if norm == "dn":
        return _deal_dn_label(lead, enrichment)
    if norm == "deal name":
        return f"KI: {_deal_dn_label(lead, enrichment)}"
    if norm == "website url":
        return website
    if norm in ("company address", "adres", "adres firmy", "address"):
        return (enrichment.company_address if enrichment else None) or ""
    if norm in ("nip", "tax identification number", "numer nip", "company nip"):
        return (enrichment.company_nip if enrichment else None) or ""
    if norm == "mobile phone number":
        return _contact_phone(contact, enrichment)
    if norm == "email":
        return _contact_email(contact, enrichment)
    if norm == "full name":
        return contact.full_name if contact else ""
    if norm == "first name":
        return first_name or ""
    if norm == "last name":
        return last_name or ""
    if norm == "job title":
        return sheet_role
    if norm == "branza":
        return sheet_branza
    if norm == "rola w projekcie":
        return sheet_role
    if norm == "rola według skysnap":
        return sheet_role
    if norm == "role":
        return sheet_role
    if norm == "deal stage":
        return DEAL_STAGE_LEADS_RESEARCH
    if norm == "leads orygin":
        return "Kompass Email" if lead.source == "kompass_email" else lead.source
    if norm == "pipeline":
        return PIPELINE_SALES
    if norm == "strona inwestycji":
        return kompass_url or lead.project_url or ""
    if norm == "komentarz":
        return _build_komentarz(
            lead,
            enrichment=enrichment,
            decision=decision,
            is_duplicate=is_duplicate,
            project_similarity=project_similarity,
            project_similarity_min_score=project_similarity_min_score,
        ) or ""
    if norm == "linkedin in":
        return _contact_linkedin_url(contact) if contact else ""
    if norm == "direct number":
        return _contact_direct_phone(contact) if contact else ""
    if norm == "email direct":
        return _contact_direct_email(contact) if contact else ""
    if norm in ("email guessed", "guessed email", "email (guessed)", "email zgadniety"):
        # Pattern-inferred addresses only — kept out of Email / Email Direct.
        return (contact.guessed_email or "") if contact else ""
    if norm in ("duplikat", "duplicate", "hubspot duplikat", "hubspot duplicate", "dup"):
        return is_duplicate  # rendered as TAK/NIE by _sheet_value
    if norm == "stage inwestycji":
        return _stage_inwestycji(lead, enrichment)
    return ""


def build_row_for_headers(
    headers: list[str],
    *,
    lead: Lead,
    enrichment: EnrichmentResult | None,
    decision: FuzzyDuplicateDecision | None,
    project_similarity: ProjectSimilarityDecision | None = None,
    project_similarity_min_score: int = 60,
) -> list[Any]:
    """Build one row aligned to the sheet's existing header row (column order preserved)."""
    return [
        _sheet_value(
            cell_for_header(
                h,
                lead=lead,
                enrichment=enrichment,
                decision=decision,
                project_similarity=project_similarity,
                project_similarity_min_score=project_similarity_min_score,
            )
        )
        for h in headers
    ]
