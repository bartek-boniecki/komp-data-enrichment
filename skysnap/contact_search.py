"""Web / LinkedIn dork searches to fill missing contact fields."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from skysnap.enrichment import (
    has_contact_name,
    is_placeholder_contact_value,
    is_real_email,
    needs_channel_gap_fill,
    needs_contact_gap_search,
    needs_name_gap_fill,
    resolve_company_name,
    sanitize_search_term,
    short_company_name_for_search,
)
from skysnap.contact_extract import candidates_summary
from skysnap.contact_finalize import finalize_enrichment_contact
from skysnap.kompass_session import close_kompass_session
from skysnap.models import EnrichmentResult, WebsiteContact
from skysnap.osint import find_company_website, gather_osint_evidence
from skysnap.progress import log_progress
from skysnap.sheet_taxonomy import SHEET_ROLES
from skysnap.validation import email_domain, is_real_email_deliverable, is_role_email

if TYPE_CHECKING:
    from skysnap.claude import ClaudeClient
    from skysnap.db import Lead

ContactGapPhase = Literal["no_name", "channels"]

# Roles to hunt on LinkedIn when no named contact exists (excludes sheet fallback).
LINKEDIN_TARGET_ROLES: tuple[str, ...] = tuple(r for r in SHEET_ROLES if r != "Inne.")

_LINKEDIN_ROLE_PRIORITY: tuple[str, ...] = (
    "Specjalista do spraw ofertowania",
    "Kierownik projektu",
    "Kierownik kontraktu",
    "Dyrektor kontraktu",
    "Inżynier kontraktu",
    "Kierownik do spraw branżowych",
    "Kierownik budowy",
    "Inżynier budowy",
    "Kierownik robót ziemnych",
    "Geodeta",
    "Sekretariat",
)


def _ordered_roles_for_search() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for role in _LINKEDIN_ROLE_PRIORITY + LINKEDIN_TARGET_ROLES:
        if role in seen:
            continue
        seen.add(role)
        ordered.append(role)
    return ordered


def _clean_company_for_search(company_name: str) -> str:
    return short_company_name_for_search(company_name)


def _clean_person_name(full_name: str) -> str:
    return sanitize_search_term(full_name, max_len=40)


# Tier 1: free Polish B2B directories that publish company phone/email directly.
_PL_DIRECTORY_SITES: tuple[str, ...] = (
    "panoramafirm.pl",
    "aleo.com",
    "pkt.pl",
)

# Company registries that list board / management people, searchable by NIP.
_PL_REGISTRY_SITES: tuple[str, ...] = (
    "rejestr.io",
    "aleo.com",
    "krs-pobierz.pl",
)


def _build_directory_queries(*, company_name: str, city: str | None) -> list[str]:
    company = _clean_company_for_search(company_name)
    if not company:
        return []
    city_part = f" {city}" if city else ""
    return [f'"{company}"{city_part} site:{site}' for site in _PL_DIRECTORY_SITES]


def _build_nip_queries(*, nip: str | None, company_name: str) -> list[str]:
    """Registry lookups keyed on NIP — surfaces board members and org contact."""
    clean_nip = "".join(ch for ch in (nip or "") if ch.isdigit())
    if len(clean_nip) != 10:
        return []
    company = _clean_company_for_search(company_name)
    queries = [f"NIP {clean_nip} site:{site}" for site in _PL_REGISTRY_SITES[:2]]
    if company:
        queries.append(f'"{company}" NIP {clean_nip} zarząd kontakt')
    return queries


def _project_hint(project_name: str | None) -> str | None:
    """Short project keyword phrase usable inside a search query."""
    if not project_name:
        return None
    hint = sanitize_search_term(project_name, max_len=50)
    # Strip trailing Kompass numeric ids ("... 111706") — noise in web search.
    hint = " ".join(w for w in hint.split() if not (w.isdigit() and len(w) >= 4))
    return hint.strip() or None


def _build_channel_search_queries(
    *,
    full_name: str,
    company_name: str,
    city: str | None,
    project_name: str | None = None,
    nip: str | None = None,
) -> list[str]:
    name = _clean_person_name(full_name)
    company = _clean_company_for_search(company_name)
    if not name or not company:
        return []
    queries = [
        f'"{name}" "{company}" email kontakt',
        f'"{name}" "{company}" telefon',
        f'site:linkedin.com/in "{name}" "{company}"',
        f'site:pl.linkedin.com/in "{name}" "{company}"',
        f'"{company}" kontakt email biuro telefon',
    ]
    if city:
        queries.append(f'"{name}" {city} {company} telefon')
    project = _project_hint(project_name)
    if project:
        queries.append(f'"{name}" "{project}" kontakt')
    queries.extend(_build_nip_queries(nip=nip, company_name=company_name))
    # Tier 1 directories often carry the company switchboard / generic inbox.
    queries.extend(_build_directory_queries(company_name=company_name, city=city))
    return queries[:9]


def _build_role_discovery_queries(
    *,
    company_name: str,
    city: str | None,
    project_name: str | None = None,
    nip: str | None = None,
) -> list[str]:
    company = _clean_company_for_search(company_name)
    if not company:
        return []
    roles = _ordered_roles_for_search()[:3]
    queries = [f'site:linkedin.com/in "{company}" "{role}"' for role in roles]
    queries.append(f'site:pl.linkedin.com/in "{company}" "{roles[0]}"')
    project = _project_hint(project_name)
    if project:
        # The person actually running THIS project is the best possible contact.
        queries.append(f'"{company}" "{project}" kierownik kontraktu')
    if city:
        queries.append(f'site:linkedin.com/in "{company}" {city} kierownik budowy')
    queries.append(f'"{company}" dział ofertowania kontakt')
    queries.append(f'"{company}" kontakt kierownik budowy')
    queries.extend(_build_nip_queries(nip=nip, company_name=company_name))
    queries.extend(_build_directory_queries(company_name=company_name, city=city))
    return queries[:10]


def _coalesce_text(preferred: str | None, fallback: str | None) -> str | None:
    if preferred and not is_placeholder_contact_value(preferred):
        return preferred.strip()
    if fallback and not is_placeholder_contact_value(fallback):
        return fallback.strip()
    return preferred or fallback


def _coalesce_email(preferred: str | None, fallback: str | None) -> str | None:
    if is_real_email(preferred):
        return preferred.strip()
    if is_real_email(fallback):
        return fallback.strip()
    return None


def _join_notes(base: EnrichmentResult, found: EnrichmentResult, *, phase: str) -> str | None:
    label = "name search" if phase == "no_name" else "email/phone search"
    found_note = f"OSINT {label}: {found.notes}" if found.notes else f"OSINT {label}"
    parts = [p for p in (base.notes, found_note) if p and p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} | {parts[1]}"


def _coalesce_linkedin(preferred: str | None, fallback: str | None) -> str | None:
    for value in (preferred, fallback):
        if value and "linkedin.com" in value.lower():
            return value.strip()
    return _coalesce_text(preferred, fallback)


def _merge_contacts(
    base: WebsiteContact | None,
    found: WebsiteContact,
    *,
    phase: ContactGapPhase,
) -> WebsiteContact:
    if base is None:
        if phase == "channels":
            return found
        return WebsiteContact(
            full_name=found.full_name,
            role=found.role,
            linkedin_url=found.linkedin_url,
            source_url=found.source_url,
            confidence=found.confidence,
        )
    if phase == "no_name":
        return WebsiteContact(
            full_name=_coalesce_text(base.full_name, found.full_name),
            role=_coalesce_text(base.role, found.role),
            email=base.email,
            phone=base.phone,
            direct_email=base.direct_email,
            direct_phone=base.direct_phone,
            linkedin_url=_coalesce_linkedin(base.linkedin_url, found.linkedin_url),
            source_url=found.source_url or base.source_url,
            confidence=max(base.confidence, found.confidence),
        )
    # direct_* is filled ONLY from direct_* sources. The previous
    # `or coalesce(email/phone)` fallback promoted whatever generic channel
    # the search found (biuro@, switchboard) into the person's DIRECT fields.
    direct_email = _coalesce_email(base.direct_email, found.direct_email)
    if direct_email and is_role_email(direct_email):
        direct_email = None  # a generic mailbox is never someone's direct email
    return WebsiteContact(
        full_name=base.full_name,
        role=_coalesce_text(base.role, found.role),
        email=_coalesce_email(base.email, found.email),
        phone=_coalesce_text(base.phone, found.phone),
        direct_email=direct_email,
        direct_phone=_coalesce_text(base.direct_phone, found.direct_phone),
        guessed_email=base.guessed_email or found.guessed_email,
        linkedin_url=_coalesce_linkedin(base.linkedin_url, found.linkedin_url),
        source_url=found.source_url or base.source_url,
        confidence=max(base.confidence, found.confidence),
    )


def merge_gap_fill(
    base: EnrichmentResult,
    found: EnrichmentResult,
    *,
    phase: ContactGapPhase,
) -> EnrichmentResult:
    """Keep existing non-placeholder fields; fill gaps from web search."""
    updates: dict[str, object] = {}
    if found.company_name and not base.company_name:
        updates["company_name"] = found.company_name
    if found.website and not base.website:
        updates["website"] = found.website
    if found.project_phase and not base.project_phase:
        updates["project_phase"] = found.project_phase
    if found.sheet_role and not base.sheet_role:
        updates["sheet_role"] = found.sheet_role
    if found.sheet_branza and not base.sheet_branza:
        updates["sheet_branza"] = found.sheet_branza
    # Generic company channels discovered by the search land in the dedicated
    # company_generic_* fields (the prompt directs them there), not on the person.
    if found.company_generic_email and not base.company_generic_email:
        updates["company_generic_email"] = found.company_generic_email
    if found.company_generic_phone and not base.company_generic_phone:
        updates["company_generic_phone"] = found.company_generic_phone

    if found.contact:
        updates["contact"] = _merge_contacts(base.contact, found.contact, phase=phase)

    notes = _join_notes(base, found, phase=phase)
    if notes:
        updates["notes"] = notes
    if not updates:
        return base
    return base.model_copy(update=updates)


def _has_complete_direct_contact(enrichment: EnrichmentResult) -> bool:
    """True when we already have a deliverable personal email and a phone."""
    if not enrichment.contact:
        return False
    c = enrichment.contact
    email = c.direct_email or c.email
    has_personal_email = is_real_email_deliverable(email) and not is_placeholder_contact_value(email)
    has_phone = bool((c.direct_phone or c.phone) and not is_placeholder_contact_value(c.direct_phone or c.phone))
    return has_personal_email and has_phone


def _restrict_domain_for(enrichment: EnrichmentResult) -> str | None:
    if enrichment.website:
        host = urlparse(
            enrichment.website
            if "//" in enrichment.website
            else f"//{enrichment.website}"
        ).netloc.lower()
        host = host.split("@")[-1].split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host:
            return host
    if enrichment.contact and enrichment.contact.email:
        return email_domain(enrichment.contact.email)
    return None


def _run_gap_phase(
    lead: Lead,
    enrichment: EnrichmentResult,
    claude: ClaudeClient,
    *,
    user_agent: str,
    company_name: str,
    phase: ContactGapPhase,
    queries: list[str],
    max_subpages: int = 2,
    check_mx: bool = True,
    pattern_guess: bool = True,
) -> EnrichmentResult:
    queries = [q for q in queries if q.strip()]
    if not queries:
        return enrichment

    label = "find contact name (LinkedIn)" if phase == "no_name" else "find email/phone"
    log_progress(f"  contact gap-fill phase: {label}")

    close_kompass_session()
    restrict = _restrict_domain_for(enrichment)
    try:
        evidence = gather_osint_evidence(
            queries,
            user_agent=user_agent,
            max_urls=6,
            max_subpages=max_subpages,
            restrict_email_domain=restrict,
        )
        if evidence.is_empty():
            log_progress("  contact gap-fill: no web results, keeping existing enrichment")
            return enrichment

        found = claude.fill_contact_gaps_from_osint_sources(
            company_name=_clean_company_for_search(company_name) or company_name,
            project_name=lead.project_name,
            existing=enrichment,
            gap=phase,
            target_roles=list(LINKEDIN_TARGET_ROLES),
            sources=evidence.sources,
            extracted_candidates=candidates_summary(evidence.extracted),
        )
        merged = merge_gap_fill(enrichment, found, phase=phase)
        # In the channels phase, reconcile against deterministic candidates + validate.
        if phase == "channels":
            merged = finalize_enrichment_contact(
                merged,
                evidence.extracted,
                restrict_domain=restrict,
                check_mx=check_mx,
                allow_pattern_guess=pattern_guess,
            )
        return merged
    except Exception as e:
        log_progress(f"  contact gap-fill skipped ({type(e).__name__}): {e}")
        return enrichment


def fill_contact_gaps(
    lead: Lead,
    enrichment: EnrichmentResult,
    claude: ClaudeClient,
    *,
    user_agent: str,
    company_name: str | None = None,
    max_subpages: int = 2,
    check_mx: bool = True,
    pattern_guess: bool = True,
) -> EnrichmentResult:
    """Two-step gap fill: (1) contact name, (2) email and phone."""
    resolved_company = company_name or resolve_company_name(lead, enrichment)
    if not needs_contact_gap_search(enrichment, company_name=resolved_company):
        return enrichment
    if not resolved_company:
        return enrichment

    result = enrichment

    if needs_name_gap_fill(result, company_name=resolved_company):
        result = _run_gap_phase(
            lead,
            result,
            claude,
            user_agent=user_agent,
            company_name=resolved_company,
            phase="no_name",
            queries=_build_role_discovery_queries(
                company_name=resolved_company,
                city=lead.city,
                project_name=lead.project_name,
                nip=result.company_nip,
            ),
            max_subpages=max_subpages,
            check_mx=check_mx,
            pattern_guess=pattern_guess,
        )

    if _has_complete_direct_contact(result):
        log_progress("  contact gap-fill: personal email + phone already present, skipping channels")
    elif needs_channel_gap_fill(result) and has_contact_name(result):
        assert result.contact and result.contact.full_name
        result = _run_gap_phase(
            lead,
            result,
            claude,
            user_agent=user_agent,
            company_name=resolved_company,
            phase="channels",
            queries=_build_channel_search_queries(
                full_name=result.contact.full_name.strip(),
                company_name=resolved_company,
                city=lead.city,
                project_name=lead.project_name,
                nip=result.company_nip,
            ),
            max_subpages=max_subpages,
            check_mx=check_mx,
            pattern_guess=pattern_guess,
        )

    if not result.website and resolved_company:
        try:
            website = find_company_website(
                lead,
                user_agent=user_agent,
                company_name=resolved_company,
            )
        except Exception as e:
            log_progress(f"  company website lookup skipped ({type(e).__name__})")
            website = None
        if website:
            result = result.model_copy(update={"website": website})
    return result
