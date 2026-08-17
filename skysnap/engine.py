from __future__ import annotations

import re
import socket
import sqlite3
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from tenacity import retry, stop_after_attempt, wait_exponential

from skysnap.claude import ClaudeClient
from skysnap.claude_usage import usage_tracker_from_settings
from skysnap.config import Settings
from skysnap import db
from skysnap.db import Lead, LeadStatus, utc_now_iso
from skysnap.contact_extract import ExtractedContacts
from skysnap.contact_finalize import finalize_enrichment_contact
from skysnap.contact_search import fill_contact_gaps
from skysnap.enrichment import (
    has_exportable_contact_data,
    has_generic_contact_data,
    qualifies_for_phase_a_export,
    has_personal_contact_data,
    infer_website_from_email,
    needs_contact_gap_search,
    resolve_company_name,
    scrub_platform_contacts,
    separate_generic_contact_channels,
)
from skysnap.sheet_taxonomy import apply_sheet_taxonomy
from skysnap.hubspot import HubSpotClient, HubSpotRateLimitError, HubSpotWriteError
from skysnap.hubspot_export import (
    deal_name,
    hubspot_followup_config_from_settings,
    hubspot_write_config_from_settings,
)
from skysnap.imap_ingest import fetch_unseen_html_emails
from skysnap.kompass import kompass_client_from_settings
from skysnap.kompass_firm import merge_firm_profile_into_updates
from skysnap.kompass_project import (
    merge_project_meta_into_enrichment_updates,
    parse_kompass_project_meta,
)
from skysnap.kompass_session import close_kompass_session, with_kompass_page
from skysnap.models import (
    EnrichmentResult,
    FuzzyDuplicateDecision,
    HubSpotDealCandidate,
    ProjectSimilarityDecision,
    is_confident_duplicate,
)
from skysnap import project_dedup
from skysnap import osint as osint_module
from skysnap.progress import log_progress
from skysnap.icp import (
    IcpAdjustment,
    apply_icp_rubric,
    base_icp_reason,
    coalesce_project_value,
    extract_project_value_from_text,
    refine_icp_from_enrichment,
    refine_icp_from_kompass_text,
    rubric_seed_score,
)
from skysnap.sheet_rows import build_row_for_headers, format_icp_score_cell, normalize_header
from skysnap.sheets import GoogleSheetsClient, _column_letter


def _company_name_tokens(name: str | None) -> set[str]:
    """ASCII-folded distinctive tokens of a company name (legal forms dropped)."""
    import unicodedata

    folded = (
        unicodedata.normalize("NFKD", (name or "").replace("ł", "l").replace("Ł", "L"))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    stop = {
        "sp", "z", "o", "oo", "sa", "spolka", "akcyjna", "firma", "grupa",
        "przedsiebiorstwo", "zaklad", "sp.k", "sp.j", "the",
    }
    return {t for t in re.split(r"[^a-z0-9]+", folded) if len(t) >= 3 and t not in stop}


def _companies_plausibly_match(a: str | None, b: str | None) -> bool:
    """Token-overlap check; unknowable (either side empty) -> True (don't block)."""
    ta, tb = _company_name_tokens(a), _company_name_tokens(b)
    if not ta or not tb:
        return True
    return bool(ta & tb)


def _apply_kompass_page_fetch(
    enrichment: EnrichmentResult,
    page_fetch: object,
    lead: Lead,
) -> EnrichmentResult:
    """Merge scraped Kompass firm profile + participant company into enrichment."""
    participant = getattr(page_fetch, "participant_company", None)
    firm_profile = getattr(page_fetch, "firm_profile", None)
    # The firm-profile scrape (step 1) and the contact modal (step 2) pick a
    # participant independently; when their picks disagree, merging the
    # profile's generic email/phone/NIP/website would staple company A's data
    # onto company B's contact. Skip the merge and say so.
    profile_mismatch = bool(
        firm_profile is not None
        and participant
        and firm_profile.company_name
        and not _companies_plausibly_match(participant, firm_profile.company_name)
    )
    if profile_mismatch:
        mismatch_note = (
            f"Pominięto dane z profilu firmy '{firm_profile.company_name}' — "
            f"kontakt dotyczy uczestnika '{participant}'"
        )
        joined = " | ".join(
            p for p in ((enrichment.notes or "").strip(), mismatch_note) if p
        )
        enrichment = enrichment.model_copy(update={"notes": joined})
    elif firm_profile is not None:
        updates = merge_firm_profile_into_updates(
            firm_profile,
            existing_company=enrichment.company_name or lead.company_name,
            existing_website=enrichment.website,
        )
        if updates:
            enrichment = enrichment.model_copy(update=updates)
    elif getattr(page_fetch, "generic_email", None) or getattr(page_fetch, "generic_phone", None):
        legacy: dict[str, str] = {}
        if page_fetch.generic_email:
            legacy["company_generic_email"] = page_fetch.generic_email
        if page_fetch.generic_phone:
            legacy["company_generic_phone"] = page_fetch.generic_phone
        enrichment = enrichment.model_copy(update=legacy)

    company = enrichment.company_name or participant or lead.company_name
    if company and not enrichment.company_name:
        enrichment = enrichment.model_copy(update={"company_name": company})

    page_text = getattr(page_fetch, "text", None) or ""
    if page_text.strip():
        meta = parse_kompass_project_meta(page_text)
        meta_updates = merge_project_meta_into_enrichment_updates(
            meta,
            existing=enrichment.model_dump(),
        )
        if meta_updates:
            enrichment = enrichment.model_copy(update=meta_updates)
    return enrichment


def _patch_lead_icp_from_kompass(
    conn,
    lead: Lead,
    kompass_text: str,
) -> Lead:
    """Re-score lead from Kompass page text; returns lead with updated ICP fields."""
    parsed_value = extract_project_value_from_text(kompass_text)
    project_value = lead.project_value or parsed_value
    adj = refine_icp_from_kompass_text(
        icp_score=lead.icp_score,
        icp_reason=lead.icp_reason,
        project_name=lead.project_name,
        project_value=project_value,
        project_phase=lead.project_phase,
        kompass_text=kompass_text,
        source=lead.source,
    )
    phase = adj.project_phase
    value_changed = bool(parsed_value and parsed_value != (lead.project_value or ""))
    if (
        adj.icp_score != lead.icp_score
        or (adj.icp_reason or "") != (lead.icp_reason or "")
        or (phase and phase != (lead.project_phase or ""))
        or value_changed
    ):
        db.patch_lead_icp(
            conn,
            lead.id,
            icp_score=adj.icp_score,
            icp_reason=adj.icp_reason,
            project_phase=phase,
            project_value=parsed_value if value_changed else None,
        )
        return replace(
            lead,
            icp_score=adj.icp_score,
            icp_reason=adj.icp_reason,
            project_phase=phase or lead.project_phase,
            project_value=parsed_value or lead.project_value,
        )
    return lead


def _authoritative_icp_adjustment(
    lead: Lead,
    enrichment: EnrichmentResult | None = None,
) -> IcpAdjustment:
    """Single ICP pass using stored lead fields and optional cached enrichment."""
    if enrichment is not None:
        return refine_icp_from_enrichment(
            icp_score=lead.icp_score,
            icp_reason=lead.icp_reason,
            project_name=lead.project_name,
            project_value=lead.project_value,
            project_phase=lead.project_phase,
            enrichment_phase=enrichment.project_phase,
            enrichment_notes=enrichment.notes,
            source=lead.source,
        )
    value_text = coalesce_project_value(
        project_value=lead.project_value,
        project_name=lead.project_name,
        project_phase=lead.project_phase,
        icp_reason=lead.icp_reason,
    )
    return apply_icp_rubric(
        icp_score=rubric_seed_score(
            source=lead.source,
            icp_reason=lead.icp_reason,
            icp_score=lead.icp_score,
        ),
        icp_reason=base_icp_reason(lead.icp_reason) or lead.icp_reason,
        project_name=lead.project_name,
        project_value=value_text,
        project_phase=lead.project_phase,
    )


def _apply_icp_adjustment_to_lead(
    conn,
    lead: Lead,
    adj: IcpAdjustment,
    *,
    dry_run: bool,
) -> Lead:
    phase = adj.project_phase or lead.project_phase
    value_text = coalesce_project_value(
        project_value=lead.project_value,
        project_name=lead.project_name,
        project_phase=phase,
        icp_reason=lead.icp_reason,
    )
    value_changed = bool(value_text and value_text != (lead.project_value or ""))
    if (
        adj.icp_score != lead.icp_score
        or (adj.icp_reason or "") != (lead.icp_reason or "")
        or (phase and phase != (lead.project_phase or ""))
        or value_changed
    ):
        if not dry_run:
            db.patch_lead_icp(
                conn,
                lead.id,
                icp_score=adj.icp_score,
                icp_reason=adj.icp_reason,
                project_phase=phase,
                project_value=value_text if value_changed else None,
            )
        return replace(
            lead,
            icp_score=adj.icp_score,
            icp_reason=adj.icp_reason,
            project_phase=phase,
            project_value=value_text or lead.project_value,
        )
    return lead


def _make_claude(settings: Settings, *, command: str) -> ClaudeClient:
    tracker = usage_tracker_from_settings(settings, command=command)
    return ClaudeClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
        usage_tracker=tracker,
        nvidia_api_key=settings.nvidia_api_key,
        nvidia_model=settings.nvidia_nim_model,
    )


def _claude_usage_payload(claude: ClaudeClient | None) -> dict[str, Any]:
    if claude is None or claude.usage_tracker is None:
        payload: dict[str, Any] = {}
    else:
        session = claude.usage_tracker.write_session_summary()
        daily = claude.usage_tracker.read_daily_totals()
        payload = {
            "claude_usage_session": session,
            "claude_usage_today": daily,
        }
    if claude is not None:
        payload["llm_provider"] = claude.active_provider
    return payload


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
def ingest_from_email(
    settings: Settings,
    *,
    mark_seen: bool = True,
    imap_only: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    if not (settings.imap_host and settings.imap_username and settings.imap_password):
        raise ValueError("IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD must be set to ingest emails")

    conn = db.connect(settings.db_path)
    backfilled = db.backfill_ingested_emails(conn)

    emails, imap_meta = fetch_unseen_html_emails(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        password=settings.imap_password,
        folder=settings.imap_folder,
        search_query=settings.imap_search_query,
        mark_seen=mark_seen,
    )

    skipped_already_ingested = 0
    to_process = []
    for em in emails:
        if (
            not force
            and em.message_id
            and db.is_email_ingested(conn, em.message_id)
        ):
            skipped_already_ingested += 1
            continue
        to_process.append(em)

    if imap_only:
        return {
            "emails_found": len(emails),
            "emails_to_process": len(to_process),
            "emails_skipped_already_ingested": skipped_already_ingested,
            "ingested_emails_backfilled": backfilled,
            "leads_upserted": 0,
            "lead_ids": [],
            "imap_only": True,
            "email_subjects": [em.subject for em in to_process],
            **imap_meta,
        }

    claude = _make_claude(settings, command="ingest-email")
    inserted: list[int] = []
    for em in to_process:
        extraction = claude.extract_projects_from_email(html=em.html)
        for p in extraction.projects:
            lead_id = db.upsert_lead(
                conn,
                source=extraction.source,
                source_message_id=em.message_id,
                source_received_at=em.received_at,
                project_name=p.project_name,
                company_name=p.company_name,
                country=p.country,
                city=p.city,
                project_value=p.project_value,
                project_phase=p.project_phase,
                project_url=p.project_url,
                raw_payload_json={"email_subject": em.subject, "raw": p.raw},
                icp_score=int(p.icp_score),
                icp_reason=p.icp_reason,
            )
            inserted.append(lead_id)
        if em.message_id:
            db.mark_email_ingested(
                conn,
                message_id=em.message_id,
                subject=em.subject,
                received_at=em.received_at,
            )

    return {
        "emails_found": len(emails),
        "emails_to_process": len(to_process),
        "emails_skipped_already_ingested": skipped_already_ingested,
        "ingested_emails_backfilled": backfilled,
        "leads_upserted": len(inserted),
        "lead_ids": inserted,
        **_claude_usage_payload(claude),
        **imap_meta,
    }


def lead_status(settings: Settings) -> dict[str, Any]:
    try:
        conn = db.connect(settings.db_path)
    except sqlite3.DatabaseError:
        conn, _repair = db.connect_or_repair(settings.db_path)
    stats = db.get_lead_stats(conn)
    stats["db_path"] = settings.db_path
    stats["db_path_resolved"] = str(Path(settings.db_path).resolve())
    resolved = Path(settings.db_path).resolve()
    wal = Path(f"{resolved}-wal")
    stats["db_wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    quarantined = sorted(
        str(p) for p in resolved.parent.glob(f"{resolved.name}*.corrupt-*")
    )
    if quarantined:
        stats["db_quarantined_files"] = quarantined
    origin_host = db.get_meta(conn, "created_on_host")
    this_host = socket.gethostname()
    stats["db_created_on_host"] = origin_host
    stats["db_host"] = this_host
    if origin_host and origin_host != this_host:
        stats["db_host_warning"] = (
            f"This database was created on '{origin_host}' but is being read on "
            f"'{this_host}'. A folder copy has probably overwritten the local "
            "database — exclude data/ from any file sync."
        )
    if resolved.exists():
        stats["db_file_modified_at"] = (
            datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        )
    return stats


def adopt_database(settings: Settings) -> dict[str, Any]:
    """Mark this machine as the owner of the local database file."""
    conn = db.connect(settings.db_path)
    result = db.adopt_database(conn)
    counts = db.lead_export_hubspot_counts(conn)
    db.checkpoint(conn)
    return {
        "db_path": str(Path(settings.db_path).resolve()),
        "previous_host": result["previous_host"],
        "host": result["host"],
        "lead_exports_total": counts["total"],
        "note": "Foreign-origin warning cleared for this database file.",
    }


def repair_db(settings: Settings) -> dict[str, Any]:
    """Fix malformed SQLite (WAL sidecars or full recreate)."""
    return db.repair_database(settings.db_path)


def retry_failed_leads(settings: Settings) -> dict[str, Any]:
    conn = db.connect(settings.db_path)
    recovered = db.recover_failed_leads(conn)
    return {"recovered_failed_to_pending": recovered}


def rescore_leads(
    settings: Settings,
    *,
    pending_only: bool = True,
) -> dict[str, Any]:
    """Re-apply ICP rubric to stored leads (fixes stale scores after rubric changes)."""
    conn = db.connect(settings.db_path)
    leads = db.iter_leads(
        conn,
        status=LeadStatus.pending if pending_only else None,
    )
    updated = 0
    samples: list[dict[str, Any]] = []
    for lead in leads:
        adj = _authoritative_icp_adjustment(lead)
        phase = adj.project_phase or lead.project_phase
        value_text = coalesce_project_value(
            project_value=lead.project_value,
            project_name=lead.project_name,
            project_phase=phase,
            icp_reason=lead.icp_reason,
        )
        value_changed = bool(value_text and value_text != (lead.project_value or ""))
        if (
            adj.icp_score != lead.icp_score
            or (adj.icp_reason or "") != (lead.icp_reason or "")
            or value_changed
            or (phase and phase != (lead.project_phase or ""))
        ):
            db.patch_lead_icp(
                conn,
                lead.id,
                icp_score=adj.icp_score,
                icp_reason=adj.icp_reason,
                project_phase=phase,
                project_value=value_text if value_changed else None,
            )
            updated += 1
            if len(samples) < 10:
                samples.append(
                    {
                        "lead_id": lead.id,
                        "project_name": lead.project_name[:80],
                        "icp_before": lead.icp_score,
                        "icp_after": adj.icp_score,
                        "project_value": value_text or lead.project_value,
                    }
                )
    return {
        "rescored": len(leads),
        "updated": updated,
        "pending_only": pending_only,
        "samples": samples,
    }


def requeue_leads(
    settings: Settings,
    *,
    include_success: bool = True,
    include_failed: bool = True,
) -> dict[str, Any]:
    conn = db.connect(settings.db_path)
    counts = db.requeue_processed_leads(
        conn,
        include_success=include_success,
        include_failed=include_failed,
    )
    return {
        "requeued_success_to_pending": counts["success"],
        "requeued_failed_to_pending": counts["failed"],
        "total_requeued": counts["success"] + counts["failed"],
    }


_LEAD_ID_IN_KOMENTARZ_RE = re.compile(r"SkySnap lead_id=(\d+)")


def sync_sheet_icp(settings: Settings, *, dry_run: bool = False) -> dict[str, Any]:
    """Update ICP Score cells for existing sheet rows (no Kompass re-scrape)."""
    if not (settings.google_service_account_json and settings.google_sheet_id):
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID are required")
    conn = db.connect(settings.db_path)
    sheets = GoogleSheetsClient(service_account_json_path=settings.google_service_account_json)
    spreadsheet_id = settings.google_sheet_id
    tab_name = settings.google_sheet_tab_name
    headers = sheets.ensure_header(spreadsheet_id=spreadsheet_id, tab_name=tab_name)

    icp_idx = next(
        (i for i, h in enumerate(headers) if normalize_header(h) == "icp score"),
        None,
    )
    if icp_idx is None:
        raise ValueError(
            "Add an 'ICP Score' column to row 1 of your sheet, then run sync-sheet-icp again."
        )
    kom_idx = next(
        (i for i, h in enumerate(headers) if normalize_header(h) == "komentarz"),
        None,
    )
    if kom_idx is None:
        raise ValueError(
            "Sheet must have a 'komentarz' column containing 'SkySnap lead_id=' tags."
        )

    kom_texts = sheets.read_column_text(
        spreadsheet_id=spreadsheet_id,
        tab_name=tab_name,
        column_letter=_column_letter(kom_idx + 1),
    )
    updates: list[tuple[int, int, Any]] = []
    matched = 0
    missing_leads = 0
    samples: list[dict[str, Any]] = []

    for row_i, text in enumerate(kom_texts):
        if row_i == 0:
            continue
        match = _LEAD_ID_IN_KOMENTARZ_RE.search(text)
        if not match:
            continue
        lead_id = int(match.group(1))
        lead = db.get_lead(conn, lead_id)
        if lead is None:
            missing_leads += 1
            continue
        export = db.get_lead_export(conn, lead_id)
        enrichment: EnrichmentResult | None = None
        if export and export.enrichment_json and export.enrichment_json.strip() not in ("", "{}"):
            enrichment = EnrichmentResult.model_validate_json(export.enrichment_json)
        adj = _authoritative_icp_adjustment(lead, enrichment)
        lead = replace(
            lead,
            icp_score=adj.icp_score,
            icp_reason=adj.icp_reason,
            project_phase=adj.project_phase or lead.project_phase,
        )
        if not dry_run:
            db.patch_lead_icp(
                conn,
                lead.id,
                icp_score=adj.icp_score,
                icp_reason=adj.icp_reason,
                project_phase=lead.project_phase,
            )
        updates.append((row_i + 1, icp_idx + 1, format_icp_score_cell(lead)))
        matched += 1
        if len(samples) < 10:
            samples.append(
                {
                    "lead_id": lead_id,
                    "row": row_i + 1,
                    "icp_score": lead.icp_score,
                    "icp_cell": format_icp_score_cell(lead)[:120],
                }
            )

    written = 0
    if not dry_run and updates:
        written = sheets.update_cells(
            spreadsheet_id=spreadsheet_id,
            tab_name=tab_name,
            updates=updates,
        )

    return {
        "matched_rows": matched,
        "updated_cells": written,
        "missing_leads": missing_leads,
        "dry_run": dry_run,
        "samples": samples,
    }


def _header_column_index(headers: list[str], *normalized_names: str) -> int | None:
    wanted = {normalize_header(n) for n in normalized_names}
    for i, h in enumerate(headers):
        if normalize_header(h) in wanted:
            return i
    return None


def _lead_from_sheet_columns(
    headers: list[str],
    column_texts: list[list[str]],
    row_index: int,
) -> Lead | None:
    """Build a minimal Lead from sheet row values (rows without SkySnap lead_id)."""
    values: dict[str, str] = {}
    for col_i, texts in enumerate(column_texts):
        if row_index >= len(texts):
            continue
        header = headers[col_i] if col_i < len(headers) else ""
        norm = normalize_header(header)
        if norm:
            values[norm] = texts[row_index].strip()

    project_name = values.get("nazwa inwestycji", "")
    if not project_name:
        return None

    project_url = (
        values.get("orygin link")
        or values.get("strona inwestycji")
        or ""
    ).strip() or None
    company_name = values.get("company name") or None
    now = utc_now_iso()
    return Lead(
        id=0,
        source="sheet_backfill",
        source_message_id=None,
        source_received_at=None,
        project_name=project_name,
        company_name=company_name,
        country="PL",
        city=None,
        project_value=None,
        project_phase=values.get("stage inwestycji") or None,
        project_url=project_url,
        raw_payload_json={},
        icp_score=0,
        icp_reason=None,
        status=LeadStatus.processed_success,
        created_at=now,
        updated_at=now,
        last_error=None,
    )


def sync_sheet_similarity(
    settings: Settings,
    *,
    dry_run: bool = False,
    use_ai: bool = True,
) -> dict[str, Any]:
    """Re-run HubSpot deal similarity for every existing sheet row."""
    if not settings.project_similarity_enabled:
        raise ValueError("SKYSNAP_PROJECT_SIMILARITY_ENABLED is false")
    if not settings.hubspot_private_app_token:
        raise ValueError("HUBSPOT_PRIVATE_APP_TOKEN is required for deal similarity")
    if not (settings.google_service_account_json and settings.google_sheet_id):
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID are required")

    conn = db.connect(settings.db_path)
    sheets = GoogleSheetsClient(service_account_json_path=settings.google_service_account_json)
    spreadsheet_id = settings.google_sheet_id
    tab_name = settings.google_sheet_tab_name
    headers = sheets.ensure_header(spreadsheet_id=spreadsheet_id, tab_name=tab_name)

    sim_idx = _header_column_index(headers, "deal similarity")
    if sim_idx is None:
        raise ValueError(
            "Add a 'Deal Similarity' column to row 1 of your sheet, then run sync-sheet-similarity again."
        )
    sim_col_letter = _column_letter(sim_idx + 1)
    log_progress(f"sync-sheet-similarity: writing column {sim_col_letter} ({headers[sim_idx]!r})")

    kom_idx = _header_column_index(headers, "komentarz")
    project_idx = _header_column_index(headers, "nazwa inwestycji")
    if kom_idx is None and project_idx is None:
        raise ValueError(
            "Sheet must have 'komentarz' (with SkySnap lead_id=) and/or 'Nazwa Inwestycji'."
        )

    read_cols = sorted({i for i in (kom_idx, project_idx, sim_idx) if i is not None})
    read_cols.extend(
        i
        for i in (
            _header_column_index(headers, "orygin link"),
            _header_column_index(headers, "strona inwestycji"),
            _header_column_index(headers, "company name"),
            _header_column_index(headers, "stage inwestycji"),
        )
        if i is not None and i not in read_cols
    )

    column_texts: list[list[str]] = []
    max_rows = 1
    for col_i in range(len(headers)):
        if col_i in read_cols:
            texts = sheets.read_column_text(
                spreadsheet_id=spreadsheet_id,
                tab_name=tab_name,
                column_letter=_column_letter(col_i + 1),
            )
            column_texts.append(texts)
            max_rows = max(max_rows, len(texts))
        else:
            column_texts.append([])

    hubspot = HubSpotClient(token=settings.hubspot_private_app_token)
    claude = _make_claude(settings, command="sync-sheet-similarity") if use_ai else None

    updates: list[tuple[int, int, Any]] = []
    matched = 0
    missing_leads = 0
    sheet_only = 0
    skipped_empty = 0
    db_patched = 0
    errors = 0
    nonzero_cells = 0
    samples: list[dict[str, Any]] = []

    for row_i in range(1, max_rows):
        kom_text = column_texts[kom_idx][row_i] if kom_idx is not None and kom_idx < len(column_texts) else ""
        lead: Lead | None = None
        enrichment: EnrichmentResult | None = None
        company_decision: FuzzyDuplicateDecision | None = None
        export = None

        match = _LEAD_ID_IN_KOMENTARZ_RE.search(kom_text)
        if match:
            lead_id = int(match.group(1))
            lead = db.get_lead(conn, lead_id)
            if lead is None:
                missing_leads += 1
                continue
            export = db.get_lead_export(conn, lead_id)
            if export and export.enrichment_json and export.enrichment_json.strip() not in ("", "{}"):
                enrichment = EnrichmentResult.model_validate_json(export.enrichment_json)
            if export and export.duplicate_decision_json:
                company_decision = FuzzyDuplicateDecision.model_validate_json(
                    export.duplicate_decision_json
                )
        else:
            lead = _lead_from_sheet_columns(headers, column_texts, row_i)
            if lead is None:
                skipped_empty += 1
                continue
            sheet_only += 1

        company = resolve_company_name(lead, enrichment)
        if company and lead.company_name != company:
            lead = replace(lead, company_name=company)

        if company_decision is None and hubspot and claude and lead.company_name:
            company_decision = _hubspot_decision(lead, hubspot=hubspot, claude=claude)

        try:
            similarity = _hubspot_project_similarity(
                lead,
                settings=settings,
                hubspot=hubspot,
                claude=claude,
                company_decision=company_decision,
                enrichment=enrichment,
            )
        except Exception as e:
            errors += 1
            log_progress(f"  sync-sheet-similarity row {row_i + 1} failed ({type(e).__name__})")
            continue

        cell = project_dedup.format_deal_similarity_cell(similarity)
        updates.append((row_i + 1, sim_idx + 1, cell))
        matched += 1
        if cell and similarity.similarity_pct > 0:
            nonzero_cells += 1

        if not dry_run and lead.id > 0:
            if db.patch_lead_export_similarity(conn, lead.id, similarity):
                db_patched += 1

        if len(samples) < 10:
            samples.append(
                {
                    "row": row_i + 1,
                    "lead_id": lead.id if lead.id > 0 else None,
                    "similarity_pct": similarity.similarity_pct,
                    "match_class": similarity.match_class,
                    "cell": cell[:120],
                }
            )

        if matched % 5 == 0:
            log_progress(f"  sync-sheet-similarity: {matched} row(s) scored…")

    written = 0
    if not dry_run and updates:
        written = sheets.update_cells(
            spreadsheet_id=spreadsheet_id,
            tab_name=tab_name,
            updates=updates,
        )

    result = {
        "matched_rows": matched,
        "updated_cells": written,
        "nonzero_cells": nonzero_cells,
        "deal_similarity_column": sim_col_letter,
        "deal_similarity_header": headers[sim_idx],
        "missing_leads": missing_leads,
        "sheet_only_rows": sheet_only,
        "skipped_empty_rows": skipped_empty,
        "db_patched": db_patched,
        "errors": errors,
        "dry_run": dry_run,
        "use_ai": use_ai,
        "samples": samples,
    }
    result.update(_claude_usage_payload(claude))
    return result


def seed_demo(settings: Settings, *, n: int = 10) -> dict[str, Any]:
    conn = db.connect(settings.db_path)
    ids: list[int] = []
    for i in range(int(n)):
        lead_id = db.upsert_lead(
            conn,
            source="demo",
            source_message_id=f"demo-{i}",
            source_received_at=None,
            project_name=f"Demo project {i}",
            company_name=f"Demo Company {i % 3}",
            country="PL",
            city="Warsaw",
            project_value=str(1_000_000 + i * 10_000),
            project_phase="tender",
            project_url="https://example.com",
            raw_payload_json={"demo": True, "i": i},
            icp_score=int(100 - i),
            icp_reason="demo seed",
        )
        ids.append(lead_id)
    return {"seeded": len(ids), "lead_ids": ids}


def purge_demo(settings: Settings) -> dict[str, Any]:
    conn = db.connect(settings.db_path)
    deleted = db.purge_demo_leads(conn)
    return {"deleted_demo_leads": deleted}


def reset_system(
    settings: Settings,
    *,
    keep_kompass_session: bool = False,
    keep_logs: bool = False,
) -> dict[str, Any]:
    """Wipe local pipeline state as if no leads or emails were ever processed."""
    import shutil
    from pathlib import Path

    counts = db.reset_or_recreate_database(settings.db_path)

    kompass_cleared = False
    if not keep_kompass_session:
        state_dir = Path(settings.kompass_browser_state_dir)
        if state_dir.exists():
            shutil.rmtree(state_dir)
            kompass_cleared = True
        state_dir.mkdir(parents=True, exist_ok=True)

    logs_deleted = 0
    if not keep_logs:
        log_dir = Path(settings.claude_usage_log_dir)
        if log_dir.is_dir():
            for path in log_dir.glob("claude-usage-*.log"):
                path.unlink(missing_ok=True)
                logs_deleted += 1

    return {
        **counts,
        "kompass_session_cleared": kompass_cleared,
        "claude_usage_logs_deleted": logs_deleted,
        "db_path": str(Path(settings.db_path).resolve()),
        "note": (
            "Google Sheet rows are not removed (append-only). "
            "IMAP mailbox read/unread flags are unchanged; with X-GM-RAW search "
            "ingest-email will pick messages up again. Use ingest-email --force "
            "if your search only matches UNSEEN mail."
        ),
    }


_MANUAL_SOURCE = "manual_kompass"
_KOMPASS_HOST_MARKERS = ("kompasinwestycji", "kompass")


def _is_kompass_project_url(url: str) -> bool:
    lower = url.strip().lower()
    if not lower.startswith("http"):
        return False
    host = urlparse(lower).netloc.lower()
    return any(m in host for m in _KOMPASS_HOST_MARKERS)


def _slug_from_kompass_url(url: str) -> str:
    path = urlparse(url.strip()).path.strip("/")
    return path.split("/")[-1] if path else "unknown"


def _project_name_from_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug


def _parse_kompass_import_line(line: str) -> tuple[str, str | None] | None:
    """Return (url, optional_project_name) or None if the line should be skipped."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "\t" in stripped:
        url, _, name = stripped.partition("\t")
        url, name = url.strip(), name.strip()
        return (url, name or None) if url else None
    if "," in stripped and stripped.lower().startswith("http"):
        url, _, name = stripped.partition(",")
        url, name = url.strip(), name.strip()
        return (url, name or None) if url else None
    return (stripped, None)


def import_kompass_leads(
    settings: Settings,
    file_path: str | Path,
    *,
    icp_score: int = 60,
    reset_pending: bool = False,
) -> dict[str, Any]:
    """Import Kompass project URLs from a text file into SQLite as pending leads."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Import file not found: {path}")

    conn = db.connect(settings.db_path)
    lead_ids: list[int] = []
    skipped_invalid = 0
    skipped_duplicate = 0
    requeued = 0
    seen_urls: set[str] = set()

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        parsed = _parse_kompass_import_line(line)
        if parsed is None:
            continue
        url, custom_name = parsed
        if not _is_kompass_project_url(url):
            skipped_invalid += 1
            continue
        norm_url = url.strip().rstrip("/")
        if norm_url in seen_urls:
            skipped_duplicate += 1
            continue
        seen_urls.add(norm_url)

        slug = _slug_from_kompass_url(norm_url)
        project_name = (custom_name or _project_name_from_slug(slug)).strip()
        icp_adj = apply_icp_rubric(
            icp_score=int(icp_score),
            icp_reason="Manual Kompass URL import",
            project_name=project_name,
        )
        lead_id = db.upsert_lead(
            conn,
            source=_MANUAL_SOURCE,
            source_message_id=f"manual-import-{slug}",
            source_received_at=None,
            project_name=project_name,
            company_name=None,
            country="PL",
            city=None,
            project_value=None,
            project_phase=None,
            project_url=norm_url,
            raw_payload_json={"import": "kompass_url", "url": norm_url},
            icp_score=icp_adj.icp_score,
            icp_reason=icp_adj.icp_reason,
        )
        lead_ids.append(lead_id)
        if reset_pending:
            row = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
            if row and row["status"] != LeadStatus.pending.value:
                db.set_status(conn, lead_id, LeadStatus.pending, last_error=None)
                requeued += 1

    return {
        "file": str(path.resolve()),
        "imported": len(lead_ids),
        "skipped_invalid": skipped_invalid,
        "skipped_duplicate": skipped_duplicate,
        "requeued_to_pending": requeued,
        "lead_ids": lead_ids,
        "icp_score": int(icp_score),
    }


def _hubspot_decision(
    lead: Lead,
    *,
    hubspot: HubSpotClient | None,
    claude: ClaudeClient | None,
) -> FuzzyDuplicateDecision | None:
    if not hubspot or not claude or not lead.company_name:
        return None
    candidates = hubspot.search_company_candidates(name_query=lead.company_name, limit=5)
    return claude.decide_duplicate(new_company_name=lead.company_name, candidates=candidates)


def _deal_search_query(lead: Lead) -> str:
    """Tokens for HubSpot deal name search."""
    parts = [lead.project_name]
    if lead.city:
        parts.append(lead.city)
    if lead.company_name:
        parts.append(lead.company_name)
    return " ".join(p for p in parts if p and str(p).strip())[:120]


def _deal_search_queries(lead: Lead) -> list[str]:
    """Ordered search phrases — project name first (best match for KI: deals)."""
    out: list[str] = []
    if lead.project_name and str(lead.project_name).strip():
        out.append(str(lead.project_name).strip())
    full = _deal_search_query(lead)
    if full and full not in out:
        out.append(full)
    if lead.company_name and str(lead.company_name).strip():
        company = str(lead.company_name).strip()
        if company not in out:
            out.append(company)
    return out


def _hubspot_project_similarity(
    lead: Lead,
    *,
    settings: Settings,
    hubspot: HubSpotClient | None,
    claude: ClaudeClient | None,
    company_decision: FuzzyDuplicateDecision | None,
    enrichment: EnrichmentResult | None = None,
) -> ProjectSimilarityDecision:
    if not settings.project_similarity_enabled or not hubspot:
        return project_dedup.empty_similarity_decision()

    write_cfg = hubspot_write_config_from_settings(settings)
    pipeline_id = write_cfg.pipeline_id if write_cfg else settings.hubspot_deal_pipeline_id
    prop_url = settings.hubspot_prop_project_url
    prop_stage = settings.hubspot_prop_stage_inwestycji

    deals_by_id: dict[str, HubSpotDealCandidate] = {}

    if company_decision and company_decision.matched_company_id:
        for deal in hubspot.list_company_deals(
            company_decision.matched_company_id,
            limit=20,
            prop_project_url=prop_url,
            prop_stage=prop_stage,
        ):
            deals_by_id[deal.id] = deal

    search_q = _deal_search_query(lead)
    if search_q:
        for query in _deal_search_queries(lead):
            for deal in hubspot.search_deal_candidates(
                name_query=query,
                pipeline_id=pipeline_id,
                limit=10,
                prop_project_url=prop_url,
                prop_stage=prop_stage,
            ):
                deals_by_id.setdefault(deal.id, deal)
            if len(deals_by_id) >= 15:
                break

    if not deals_by_id:
        return project_dedup.empty_similarity_decision()

    new_identity = project_dedup.identity_from_lead(lead, enrichment)
    scored = [
        project_dedup.score_deal_candidate(new_identity, deal)
        for deal in deals_by_id.values()
    ]
    top = project_dedup.pick_top_candidates(scored, limit=5)
    if not top:
        return project_dedup.empty_similarity_decision()

    best = top[0]
    if not claude:
        return ProjectSimilarityDecision(
            similarity_pct=best.pre_score,
            match_class=best.forced_class or "unrelated",
            matched_deal_id=best.deal.id,
            matched_deal_name=best.deal.dealname,
            confidence=0.5,
            reasoning="Deterministic score only (Claude disabled)",
        )

    try:
        claude_decision = claude.decide_project_similarity(
            new_project={
                "project_name": new_identity.project_name,
                "base_name": new_identity.base_name,
                "lot_token": new_identity.lot_token,
                "kompass_slug": new_identity.kompass_slug,
                "project_url": new_identity.project_url,
                "city": new_identity.city,
                "company_name": new_identity.company_name,
            },
            candidates=[s.deal for s in top],
            pre_scores=[s.pre_score for s in top],
        )
    except Exception as e:
        log_progress(f"  project similarity skipped ({type(e).__name__})")
        if best.pre_score <= 0:
            return project_dedup.empty_similarity_decision()
        return ProjectSimilarityDecision(
            similarity_pct=best.pre_score,
            match_class=best.forced_class or "unrelated",
            matched_deal_id=best.deal.id,
            matched_deal_name=best.deal.dealname,
            confidence=0.5,
            reasoning="Claude unavailable; deterministic score only",
        )

    return project_dedup.merge_similarity_decision(
        pre_score=best.pre_score,
        forced_class=best.forced_class,
        claude_decision=claude_decision,
        best_deal=best.deal,
    )


def _maybe_fill_contact_gaps(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    *,
    settings: Settings,
    claude: ClaudeClient,
    enrichment_tier: str,
) -> EnrichmentResult | None:
    if enrichment is None:
        return None
    company = resolve_company_name(lead, enrichment)
    if not needs_contact_gap_search(enrichment, company_name=company):
        return enrichment
    if enrichment_tier == "kompass":
        close_kompass_session()
    try:
        return fill_contact_gaps(
            lead,
            enrichment,
            claude,
            user_agent=settings.user_agent,
            company_name=company,
            max_subpages=settings.osint_max_subpages,
            check_mx=settings.email_mx_check,
            pattern_guess=settings.email_pattern_guess,
        )
    except Exception as e:
        log_progress(f"  contact gap-fill skipped ({type(e).__name__}): {e}")
        return enrichment


def _process_lead(
    conn,
    lead: Lead,
    *,
    settings: Settings,
    claude: ClaudeClient | None,
    hubspot: HubSpotClient | None,
    sheets: GoogleSheetsClient | None,
    sheet_headers: list[str],
    dry_run: bool,
    enrich_fn: Callable[[Lead, FuzzyDuplicateDecision | None], EnrichmentResult | None],
    enrichment_tier: str,
    require_contact_to_export: bool,
) -> dict[str, Any]:
    if not dry_run:
        db.mark_in_progress_bulk(conn, [lead.id])

    decision = _hubspot_decision(lead, hubspot=hubspot, claude=claude)
    is_duplicate = is_confident_duplicate(decision)

    project_similarity = _hubspot_project_similarity(
        lead,
        settings=settings,
        hubspot=hubspot,
        claude=claude,
        company_decision=decision,
    )

    # A duplicate COMPANY is not a duplicate PROJECT. Recurring GWs (Budimex,
    # PORR, Strabag, ...) exist in HubSpot after their first deal — blocking
    # enrichment on the company match starved exactly the highest-value leads
    # of contacts. Enrichment is skipped only when project similarity confirms
    # the SAME project (re-ingested deal); the company match is kept as a flag
    # and later resolves the existing HubSpot company to attach the new deal.
    same_project_duplicate = bool(
        project_similarity
        and project_similarity.match_class == "same_project"
        and project_similarity.matched_deal_id
    )

    enrichment: EnrichmentResult | None = None
    if claude and not same_project_duplicate:
        enrichment = enrich_fn(lead, decision)
        if enrichment is not None:
            # Scrub BEFORE gap-fill: a leaked SkySnap/Kompass platform contact
            # otherwise satisfies needs_contact_gap_search, the search is
            # skipped, and the scrub below then leaves the lead with nothing.
            enrichment = scrub_platform_contacts(
                enrichment,
                emails=settings.blocked_contact_emails,
                email_domains=settings.blocked_contact_email_domains,
                phone_nationals=settings.blocked_contact_phones,
                website_hosts=settings.blocked_contact_website_hosts,
            )
        enrichment = _maybe_fill_contact_gaps(
            lead,
            enrichment,
            settings=settings,
            claude=claude,
            enrichment_tier=enrichment_tier,
        )
        if enrichment is not None:
            # Re-scrub: gap-fill web results can re-introduce platform contacts.
            enrichment = scrub_platform_contacts(
                enrichment,
                emails=settings.blocked_contact_emails,
                email_domains=settings.blocked_contact_email_domains,
                phone_nationals=settings.blocked_contact_phones,
                website_hosts=settings.blocked_contact_website_hosts,
            )
            enrichment = separate_generic_contact_channels(enrichment)
            # Final safety pass: normalize phones to E.164 and validate emails
            # across every tier (Kompass included), without inventing data.
            enrichment = finalize_enrichment_contact(
                enrichment,
                ExtractedContacts(),
                restrict_domain=None,
                check_mx=settings.email_mx_check,
                allow_pattern_guess=False,
            )
        enrichment = apply_sheet_taxonomy(
            enrichment,
            company_name=resolve_company_name(lead, enrichment),
            project_name=lead.project_name,
        )

        fresh = db.get_lead(conn, lead.id)
        if fresh is not None:
            lead = fresh

        icp_adj = _authoritative_icp_adjustment(lead, enrichment)
        lead = _apply_icp_adjustment_to_lead(conn, lead, icp_adj, dry_run=dry_run)

    has_personal = has_personal_contact_data(enrichment)
    has_generic = has_generic_contact_data(enrichment)
    has_exportable = has_exportable_contact_data(enrichment)
    min_icp = settings.stakeholder_export_min_icp
    icp_export = int(lead.icp_score) >= int(min_icp)
    phase_a_export = qualifies_for_phase_a_export(
        lead, enrichment, min_icp=min_icp
    )
    exported_without_personal = (
        not has_personal and not is_duplicate and phase_a_export
    )
    if require_contact_to_export and not is_duplicate and not phase_a_export:
        if not dry_run:
            db.set_status(conn, lead.id, LeadStatus.pending, last_error=None)
        return {
            "lead_id": lead.id,
            "status": "skipped_no_contact",
            "enrichment_tier": enrichment_tier,
            "is_duplicate": False,
            "has_contact": has_exportable,
            "has_personal_contact": has_personal,
            "has_generic_contact": has_generic,
            "has_icp_export": icp_export,
            "has_stakeholder_export": False,
            "icp_score": lead.icp_score,
            "enrichment": enrichment.model_dump() if enrichment else None,
        }

    row = build_row_for_headers(
        sheet_headers,
        lead=lead,
        enrichment=enrichment,
        decision=decision,
        project_similarity=project_similarity,
        project_similarity_min_score=settings.project_similarity_min_score,
    )

    if dry_run:
        status = "skipped_duplicate" if is_duplicate else "dry_run_success"
        return {
            "lead_id": lead.id,
            "status": status,
            "enrichment_tier": enrichment_tier,
            "is_duplicate": is_duplicate,
            "has_contact": has_exportable,
            "has_personal_contact": has_personal,
            "has_generic_contact": has_generic,
            "has_icp_export": icp_export and not has_exportable,
            "has_stakeholder_export": exported_without_personal,
            "sheet_row": row,
            "icp_score": lead.icp_score,
            "enrichment": enrichment.model_dump() if enrichment else None,
        }

    assert sheets is not None
    sheets.append_row(
        spreadsheet_id=settings.google_sheet_id or "",
        tab_name=settings.google_sheet_tab_name,
        row_values=row,
        headers=sheet_headers,
    )
    db.save_lead_export(
        conn,
        lead.id,
        enrichment=enrichment,
        decision=decision,
        project_similarity=project_similarity,
    )

    if is_duplicate:
        db.set_status(conn, lead.id, LeadStatus.skipped_duplicate, last_error=None)
        return {
            "lead_id": lead.id,
            "status": "skipped_duplicate",
            "enrichment_tier": enrichment_tier,
            "is_duplicate": True,
            "matched_company_id": decision.matched_company_id if decision else None,
            "confidence": decision.confidence if decision else None,
            "icp_score": lead.icp_score,
            "enrichment": enrichment.model_dump() if enrichment else None,
        }

    db.set_status(conn, lead.id, LeadStatus.processed_success, last_error=None)
    return {
        "lead_id": lead.id,
        "status": "success",
        "enrichment_tier": enrichment_tier,
        "is_duplicate": False,
        "has_contact": has_exportable,
        "has_personal_contact": has_personal,
        "has_generic_contact": has_generic,
        "has_icp_export": icp_export and not has_exportable,
        "has_stakeholder_export": exported_without_personal,
        "icp_score": lead.icp_score,
        "enrichment": enrichment.model_dump() if enrichment else None,
    }


def run_daily(
    settings: Settings,
    *,
    dry_run: bool = False,
    use_ai: bool = True,
    recover_stale: bool = True,
    include_demo: bool = False,
    kompass_only: bool = False,
    osint_only: bool = False,
    push_hubspot: bool = True,
) -> dict[str, Any]:
    db_repair: dict[str, Any] | None = None
    try:
        conn = db.connect(settings.db_path)
    except sqlite3.DatabaseError:
        conn, repair_info = db.connect_or_repair(settings.db_path)
        if repair_info.get("action") != "none":
            db_repair = repair_info
    recovered = db.recover_stale_in_progress(conn) if recover_stale else 0

    claude: ClaudeClient | None = None
    if use_ai:
        if not settings.anthropic_api_key and not settings.nvidia_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY or NVIDIA_API_KEY is required unless --no-ai is used"
            )
        claude = _make_claude(settings, command="run-daily")

    hubspot = HubSpotClient(token=settings.hubspot_private_app_token or "") if settings.hubspot_private_app_token else None

    sheets: GoogleSheetsClient | None = None
    sheet_headers: list[str] | None = None
    if settings.google_service_account_json and settings.google_sheet_id:
        sheets = GoogleSheetsClient(service_account_json_path=settings.google_service_account_json)
        sheet_headers = sheets.ensure_header(
            spreadsheet_id=settings.google_sheet_id,
            tab_name=settings.google_sheet_tab_name,
        )
    elif not dry_run:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID are required for run-daily (use --dry-run to skip)"
        )

    if not sheet_headers:
        raise ValueError(
            "Sheet headers unavailable. Set GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID "
            "to match your existing spreadsheet."
        )

    kompass_client = None
    if kompass_only and osint_only:
        raise ValueError("Use only one of --kompass-only or --osint-only")
    if not use_ai:
        raise ValueError("run-daily enrichment requires AI (omit --no-ai)")
    if not osint_only:
        kompass_client = kompass_client_from_settings(settings)

    results: list[dict[str, Any]] = []
    exported_ids: set[int] = set()
    kompass_attempted = 0
    kompass_contacts_found = 0
    kompass_skipped_no_contact = 0
    kompass_deferred = 0
    kompass_requeued = 0
    osint_processed = 0

    if not dry_run:
        kompass_requeued = db.requeue_enriched_manually(conn)
        if kompass_requeued:
            log_progress(
                f"Re-queued {kompass_requeued} lead(s) from enriched_manually for Kompass (ICP order)"
            )

    def _record(result: dict[str, Any]) -> None:
        nonlocal kompass_contacts_found, kompass_skipped_no_contact, osint_processed
        results.append(result)
        status = result.get("status")
        tier = result.get("enrichment_tier")
        lead_id = int(result["lead_id"])
        if status == "skipped_no_contact" and tier == "kompass":
            kompass_skipped_no_contact += 1
        elif status in ("success", "skipped_duplicate", "dry_run_success"):
            exported_ids.add(lead_id)
            if tier == "kompass" and status in ("success", "dry_run_success"):
                if result.get("has_contact"):
                    kompass_contacts_found += 1
            if tier == "osint":
                osint_processed += 1

    # --- Phase A: Kompass — spend the daily contact-reveal quota on the
    # highest-ICP leads (firm page data is free; the get-contact modal costs
    # one reveal credit and is gated by the persisted per-day ledger). ---
    reveals_used_today = 0
    try:
        reveals_used_today = db.count_reveals_today(conn, tz_name=settings.timezone)
    except sqlite3.DatabaseError:
        conn, repair_info = db.connect_or_repair(settings.db_path)
        if repair_info.get("action") != "none":
            db_repair = repair_info
        reveals_used_today = db.count_reveals_today(conn, tz_name=settings.timezone)
    if not osint_only and kompass_client and claude:
        log_progress(
            f"Phase A Kompass: {reveals_used_today}/{settings.daily_limit} contact reveals "
            f"already used today (ledger persists across runs)"
        )

        attempted_kompass_ids: set[int] = set()

        def kompass_enrich(lead: Lead, _decision: FuzzyDuplicateDecision | None) -> EnrichmentResult:
            nonlocal reveals_used_today
            assert kompass_client is not None
            assert claude is not None
            if not lead.project_url:
                return EnrichmentResult(
                    source="kompass",
                    notes="No project_url for Kompass enrichment",
                )

            allow_reveal = (
                not dry_run
                and reveals_used_today < settings.daily_limit
                and not db.lead_already_revealed(conn, lead.id)
            )

            def _fetch_page_context(page):
                return kompass_client.fetch_project_context(
                    lead.project_url, page=page, allow_contact_reveal=allow_reveal
                )

            page_fetch = with_kompass_page(kompass_client, _fetch_page_context)
            if page_fetch.reveal_submitted:
                db.log_reveal(
                    conn,
                    lead.id,
                    success=page_fetch.reveal_succeeded,
                    tz_name=settings.timezone,
                )
                reveals_used_today += 1
                log_progress(
                    f"  contact reveal used ({reveals_used_today}/{settings.daily_limit} today, "
                    f"{'contact data returned' if page_fetch.reveal_succeeded else 'no contact data'})"
                )
            lead = _patch_lead_icp_from_kompass(conn, lead, page_fetch.text)
            # Hint the PAGE-verified participant first: the email-derived
            # lead.company_name is often the investor, and the hint anchors
            # which organization the model attributes the contact to.
            company_hint = page_fetch.participant_company or lead.company_name
            enrichment = claude.extract_contact_from_kompass_page(
                project_url=lead.project_url,
                text=page_fetch.text,
                project_name=lead.project_name,
                company_name=company_hint,
            )
            enrichment = _apply_kompass_page_fetch(enrichment, page_fetch, lead)
            resolved_company = (
                enrichment.company_name or page_fetch.participant_company or lead.company_name
            )
            profile_website = (
                page_fetch.firm_profile.website if page_fetch.firm_profile else None
            )
            if (
                has_personal_contact_data(enrichment)
                or enrichment.company_generic_email
                or enrichment.company_generic_phone
                or profile_website
            ):
                website = enrichment.website or profile_website
                if not website or "kompas" in (website or "").lower():
                    personal_email = (
                        enrichment.contact.email if enrichment.contact else None
                    )
                    website = infer_website_from_email(
                        personal_email or enrichment.company_generic_email
                    )
                if not website:
                    # Kompass session holds a Playwright loop on the worker thread; close
                    # before opening a second browser for DuckDuckGo website lookup.
                    close_kompass_session()
                    try:
                        website = osint_module.find_company_website(
                            lead,
                            user_agent=settings.user_agent,
                            company_name=resolved_company,
                        )
                    except Exception as e:
                        log_progress(f"  company website lookup skipped ({type(e).__name__})")
                        website = None
                if website:
                    enrichment = enrichment.model_copy(update={"website": website})
            return enrichment

        try:
            quota_exhausted_logged = False
            for lead in db.iter_pending_by_icp(
                conn,
                min_score=settings.min_score,
                include_demo=include_demo,
                skip_ids=attempted_kompass_ids,
            ):
                # Contact reveals stop at the daily quota, but free project-page scrape
                # (Typ, sektor, location, description) still runs so requeues update HubSpot.
                if (
                    not dry_run
                    and reveals_used_today >= settings.daily_limit
                    and not quota_exhausted_logged
                ):
                    log_progress(
                        f"Kompass reveal quota ({settings.daily_limit}/day) reached — "
                        "continuing with free page scrape (no contact reveal)"
                    )
                    quota_exhausted_logged = True
                if dry_run and kompass_attempted >= settings.daily_limit:
                    break  # dry-run never spends reveals; bound the walk instead
                attempted_kompass_ids.add(lead.id)
                kompass_attempted += 1
                log_progress(
                    f"Kompass search #{kompass_attempted}: "
                    f"reveals {reveals_used_today}/{settings.daily_limit} — "
                    f"lead {lead.id} ICP={lead.icp_score} ({lead.project_name[:60]})"
                )
                try:
                    result = _process_lead(
                        conn,
                        lead,
                        settings=settings,
                        claude=claude,
                        hubspot=hubspot,
                        sheets=sheets,
                        sheet_headers=sheet_headers,
                        dry_run=dry_run,
                        enrich_fn=kompass_enrich,
                        enrichment_tier="kompass",
                        require_contact_to_export=True,
                    )
                    _record(result)
                    if result.get("icp_score") is not None:
                        log_progress(
                            f"  -> {result.get('status')} ICP={result.get('icp_score')}"
                        )
                    else:
                        log_progress(f"  -> {result.get('status')}")
                except Exception as e:
                    log_progress(f"  -> failed: {e}")
                    if not dry_run:
                        db.set_status(conn, lead.id, LeadStatus.processed_failed, last_error=str(e))
                    results.append(
                        {
                            "lead_id": lead.id,
                            "status": "failed",
                            "enrichment_tier": "kompass",
                            "error": str(e),
                        }
                    )
        finally:
            close_kompass_session()

        # Only defer leads we never attempted this run (free scrapes continue after quota).
        if reveals_used_today >= settings.daily_limit and not dry_run:
            kompass_deferred = db.defer_pending_for_kompass_quota(
                conn,
                min_score=settings.min_score,
                include_demo=include_demo,
                exclude_ids=attempted_kompass_ids | exported_ids,
                exclude_sources=("manual_kompass",),
            )
            if kompass_deferred:
                log_progress(
                    f"Kompass reveal quota ({settings.daily_limit}/day) reached; "
                    f"{kompass_deferred} lead(s) deferred to tomorrow (enriched_manually)"
                )

    # --- Phase B: OSINT for all remaining pending leads (not Kompass-exported) ---
    if not kompass_only and claude:
        log_progress("Phase B OSINT: remaining pending leads after Kompass tier")
        osint_leads = db.get_pending_excluding(
            conn,
            exclude_ids=exported_ids,
            min_score=settings.min_score,
            include_demo=include_demo,
        )
        cap = settings.osint_daily_cap
        if cap is not None:
            osint_leads = osint_leads[: int(cap)]
        log_progress(f"OSINT processing {len(osint_leads)} lead(s) (cap={cap or 'none'})")

        def osint_enrich(lead: Lead, _decision: FuzzyDuplicateDecision | None) -> EnrichmentResult:
            kompass_page = None
            if (
                kompass_client
                and lead.project_url
                and _is_kompass_project_url(lead.project_url)
            ):
                try:

                    def _fetch_kompass(page):
                        # Never spend a reveal credit in the OSINT tier — firm
                        # profile data (free) is still collected.
                        return kompass_client.fetch_project_context(
                            lead.project_url, page=page, allow_contact_reveal=False
                        )

                    kompass_page = with_kompass_page(kompass_client, _fetch_kompass)
                except Exception as e:
                    log_progress(f"  kompass page prefetch skipped ({type(e).__name__})")
                finally:
                    close_kompass_session()
            enrichment = osint_module.enrich_lead_osint(
                lead,
                claude,
                user_agent=settings.user_agent,
                max_subpages=settings.osint_max_subpages,
                check_mx=settings.email_mx_check,
                pattern_guess=settings.email_pattern_guess,
                kompass_page=kompass_page,
            )
            if kompass_page is not None:
                lead = _patch_lead_icp_from_kompass(conn, lead, kompass_page.text)
                enrichment = _apply_kompass_page_fetch(enrichment, kompass_page, lead)
            return enrichment

        for i, lead in enumerate(osint_leads, start=1):
            log_progress(f"OSINT {i}/{len(osint_leads)}: lead {lead.id} ({lead.project_name[:60]})")
            try:
                result = _process_lead(
                    conn,
                    lead,
                    settings=settings,
                    claude=claude,
                    hubspot=hubspot,
                    sheets=sheets,
                    sheet_headers=sheet_headers,
                    dry_run=dry_run,
                    enrich_fn=osint_enrich,
                    enrichment_tier="osint",
                    require_contact_to_export=False,
                )
                _record(result)
                if result.get("icp_score") is not None:
                    log_progress(
                        f"  -> {result.get('status')} ICP={result.get('icp_score')}"
                    )
                else:
                    log_progress(f"  -> {result.get('status')}")
            except Exception as e:
                log_progress(f"  -> failed: {e}")
                if not dry_run:
                    db.set_status(conn, lead.id, LeadStatus.processed_failed, last_error=str(e))
                results.append(
                    {
                        "lead_id": lead.id,
                        "status": "failed",
                        "enrichment_tier": "osint",
                        "error": str(e),
                    }
                )

    log_progress("run-daily finished")
    db.checkpoint(conn)

    hubspot_result: dict[str, Any] | None = None
    if push_hubspot and not dry_run:
        daily_lead_ids = [
            int(r["lead_id"])
            for r in results
            if r.get("status") in ("success", "skipped_duplicate")
        ]
        if daily_lead_ids:
            log_progress(f"HubSpot push: {len(daily_lead_ids)} lead(s) from this run")
            hubspot_result = push_hubspot_leads(settings, lead_ids=daily_lead_ids)
        else:
            hubspot_result = {
                "skipped": True,
                "reason": "no leads exported in daily run",
                "results": [],
            }
    elif push_hubspot and dry_run:
        hubspot_result = {"skipped": True, "reason": "dry_run", "results": []}

    payload: dict[str, Any] = {
        "kompass_searches": kompass_attempted,
        "kompass_contacts_found": kompass_contacts_found,
        "kompass_reveals_used_today": reveals_used_today,
        "kompass_reveal_quota": settings.daily_limit,
        "kompass_skipped_no_contact": kompass_skipped_no_contact,
        "kompass_deferred": kompass_deferred,
        "kompass_requeued": kompass_requeued,
        "osint_processed": osint_processed,
        "processed": len(results),
        "recovered_in_progress": recovered,
        "dry_run": dry_run,
        "use_ai": use_ai,
        "kompass_only": kompass_only,
        "osint_only": osint_only,
        "results": results,
        "hubspot": hubspot_result,
        **_claude_usage_payload(claude),
    }
    if db_repair is not None:
        payload["db_repair"] = db_repair
    return payload


def backfill_export_snapshots(
    settings: Settings,
    *,
    dry_run: bool = False,
    include_failed: bool = False,
) -> dict[str, Any]:
    """Create lead_exports rows from processed leads (for HubSpot push without re-scraping)."""
    conn = db.connect(settings.db_path)
    statuses = {LeadStatus.processed_success, LeadStatus.skipped_duplicate}
    if include_failed:
        statuses.add(LeadStatus.processed_failed)

    created = 0
    already = 0
    samples: list[dict[str, Any]] = []
    for lead in db.iter_leads(conn):
        if lead.status not in statuses:
            continue
        if db.get_lead_export(conn, lead.id):
            already += 1
            continue
        enrichment_source: Literal["kompass", "osint", "website"] = "kompass"
        if lead.source in ("osint", "website"):
            enrichment_source = lead.source  # type: ignore[assignment]
        enrichment = EnrichmentResult(
            source=enrichment_source,
            company_name=lead.company_name,
            project_phase=lead.project_phase,
            notes=lead.icp_reason,
        )
        enrichment = apply_sheet_taxonomy(
            enrichment,
            company_name=lead.company_name,
            project_name=lead.project_name,
        )
        if not dry_run:
            db.save_lead_export(conn, lead.id, enrichment=enrichment)
        created += 1
        if len(samples) < 10:
            samples.append(
                {
                    "lead_id": lead.id,
                    "status": lead.status.value,
                    "project_name": lead.project_name[:80],
                }
            )

    pending = len(db.iter_leads_pending_hubspot_sync(conn)) if not dry_run else None
    export_counts = db.lead_export_hubspot_counts(conn) if not dry_run else None
    db.checkpoint(conn)
    return {
        "created": created,
        "already_had_export": already,
        "dry_run": dry_run,
        "include_failed": include_failed,
        "hubspot_push_pending": pending,
        "db_path": settings.db_path,
        "db_path_resolved": str(Path(settings.db_path).resolve()),
        "lead_exports_total": export_counts["total"] if export_counts else None,
        "samples": samples,
    }


# (HubSpot object type, ((Settings attribute, .env variable), ...))
_HUBSPOT_PROP_OBJECTS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "deals",
        (
            ("hubspot_prop_project_url", "HUBSPOT_PROP_PROJECT_URL"),
            ("hubspot_prop_project_name", "HUBSPOT_PROP_PROJECT_NAME"),
            ("hubspot_prop_icp_score", "HUBSPOT_PROP_ICP_SCORE"),
            ("hubspot_prop_stage_inwestycji", "HUBSPOT_PROP_STAGE_INWESTYCJI"),
            ("hubspot_prop_deal_typ", "HUBSPOT_PROP_DEAL_TYP"),
            ("hubspot_prop_deal_source", "HUBSPOT_PROP_DEAL_SOURCE"),
            ("hubspot_prop_deal_branza", "HUBSPOT_PROP_DEAL_BRANZA"),
            ("hubspot_prop_deal_role", "HUBSPOT_PROP_DEAL_ROLE"),
            ("hubspot_prop_ai_score", "HUBSPOT_PROP_AI_SCORE"),
            ("hubspot_prop_sektor_podsektor", "HUBSPOT_PROP_SEKTOR_PODSEKTOR"),
            ("hubspot_prop_project_city", "HUBSPOT_PROP_PROJECT_CITY"),
            ("hubspot_prop_project_voivodeship", "HUBSPOT_PROP_PROJECT_VOIVODSHIP"),
            ("hubspot_prop_project_street", "HUBSPOT_PROP_PROJECT_STREET"),
            (
                "hubspot_prop_project_building_number",
                "HUBSPOT_PROP_PROJECT_BUILDING_NUMBER",
            ),
        ),
    ),
    (
        "companies",
        (
            ("hubspot_prop_nip", "HUBSPOT_PROP_NIP"),
            ("hubspot_prop_opis", "HUBSPOT_PROP_OPIS"),
            ("hubspot_prop_company_notes", "HUBSPOT_PROP_COMPANY_NOTES"),
            ("hubspot_prop_branza_skysnap", "HUBSPOT_PROP_BRANZA_SKYSNAP"),
            ("hubspot_prop_branza_extrainfo", "HUBSPOT_PROP_BRANZA_EXTRAINFO"),
            ("hubspot_prop_leads_score", "HUBSPOT_PROP_LEADS_SCORE"),
            ("hubspot_prop_leads_origin", "HUBSPOT_PROP_LEADS_ORIGIN"),
            ("hubspot_prop_uslugi", "HUBSPOT_PROP_USLUGI"),
            ("hubspot_prop_konkurencja", "HUBSPOT_PROP_KONKURENCJA"),
            ("hubspot_prop_konkurencja_expiry", "HUBSPOT_PROP_KONKURENCJA_EXPIRY"),
            ("hubspot_prop_voivodeship", "HUBSPOT_PROP_VOIVODSHIP"),
        ),
    ),
)


def hubspot_property_report(settings: Settings) -> dict[str, Any]:
    """Validate configured HUBSPOT_PROP_* names against the live HubSpot schema."""
    if not settings.hubspot_private_app_token:
        return {"skipped": True, "reason": "HUBSPOT_PRIVATE_APP_TOKEN not set"}

    hubspot = HubSpotClient(token=settings.hubspot_private_app_token)
    report: dict[str, Any] = {"objects": {}}
    problems: list[str] = []

    for object_type, setting_names in _HUBSPOT_PROP_OBJECTS:
        schema = hubspot.property_schema(object_type)
        entries: list[dict[str, Any]] = []
        for setting_name, env_name in setting_names:
            configured = (getattr(settings, setting_name, None) or "").strip()
            if not configured:
                continue
            definition = schema.properties.get(configured)
            entry: dict[str, Any] = {
                "env": env_name,
                "property": configured,
                "exists": definition is not None,
            }
            if definition is None:
                entry["problem"] = "property does not exist in HubSpot"
                problems.append(f"{object_type}.{configured} does not exist")
            else:
                entry["type"] = definition.type
                entry["field_type"] = definition.field_type
                if definition.is_enumeration:
                    entry["options"] = list(definition.options)
                if definition.read_only:
                    entry["problem"] = "read-only in HubSpot"
                    problems.append(f"{object_type}.{configured} is read-only")
            entries.append(entry)
        report["objects"][object_type] = {
            "schema_available": schema.available,
            "properties": entries,
        }

    report["problems"] = problems
    report["ok"] = not problems
    return report


def _normalize_project_url(url: str | None) -> str:
    u = (url or "").strip().lower().rstrip("/")
    if "://" in u:
        scheme, rest = u.split("://", 1)
        if rest.startswith("www."):
            rest = rest[4:]
        return f"{scheme}://{rest}"
    return u


def _project_urls_match(a: str | None, b: str | None) -> bool:
    na = _normalize_project_url(a)
    nb = _normalize_project_url(b)
    return bool(na and nb and na == nb)


def _bare_ki_dealname(lead: Lead) -> str:
    project = (lead.project_name or "").strip()
    return f"KI: {project}".strip().lower() if project else ""


def _match_hubspot_deal(
    lead: Lead,
    enrichment: EnrichmentResult | None,
    candidates: list[HubSpotDealCandidate],
) -> HubSpotDealCandidate | None:
    if not candidates:
        return None

    url_matches = [
        deal
        for deal in candidates
        if _project_urls_match(deal.project_url, lead.project_url)
    ]
    pool = list(url_matches or candidates)

    target = deal_name(lead, enrichment).strip().lower()
    bare = _bare_ki_dealname(lead)

    # When several deals share the Kompass URL, prefer a firm-prefixed dealname
    # over the bare "KI: {project}" duplicate (created when company was unknown).
    if url_matches and bare and len(url_matches) > 1:
        preferred = [
            deal
            for deal in url_matches
            if (deal.dealname or "").strip().lower() != bare
        ]
        if preferred:
            pool = preferred

    if target:
        for deal in pool:
            name = (deal.dealname or "").strip().lower()
            if name == target:
                return deal
        for deal in pool:
            name = (deal.dealname or "").strip().lower()
            if target in name or name in target:
                return deal
    return pool[0]


def _collect_hubspot_link_candidates(
    hubspot: HubSpotClient,
    lead: Lead,
    enrichment: EnrichmentResult | None,
    *,
    pipeline_id: str | None,
    prop_project_url: str | None = None,
) -> list[HubSpotDealCandidate]:
    """Find HubSpot deals for linking — URL first, then exact name, then token search."""
    target_dealname = deal_name(lead, enrichment)
    candidates: list[HubSpotDealCandidate] = []
    seen: set[str] = set()

    def _add(deals: list[HubSpotDealCandidate]) -> None:
        for deal in deals:
            if deal.id not in seen:
                seen.add(deal.id)
                candidates.append(deal)

    prop_url = (prop_project_url or "").strip()
    project_url = (lead.project_url or "").strip()
    if prop_url and project_url:
        _add(
            hubspot.find_deals_by_project_url(
                project_url,
                prop_project_url=prop_url,
                pipeline_id=pipeline_id,
                limit=10,
            )
        )
        if not candidates:
            _add(
                hubspot.find_deals_by_project_url(
                    project_url,
                    prop_project_url=prop_url,
                    pipeline_id=None,
                    limit=10,
                )
            )
    if candidates:
        return candidates

    _add(hubspot.find_deal_by_exact_name(target_dealname, pipeline_id=pipeline_id, limit=3))
    if not candidates:
        _add(hubspot.find_deal_by_exact_name(target_dealname, pipeline_id=None, limit=3))
    if not candidates:
        fallback = (lead.project_name or "").strip() or (lead.company_name or "").strip()
        if fallback:
            _add(
                hubspot.search_deal_candidates(
                    name_query=fallback,
                    pipeline_id=pipeline_id,
                    limit=5,
                    prop_project_url=prop_url or None,
                )
            )
    return candidates


def find_existing_hubspot_link(
    hubspot: HubSpotClient,
    lead: Lead,
    enrichment: EnrichmentResult | None,
    *,
    pipeline_id: str | None,
    prop_project_url: str | None = None,
) -> tuple[str, str] | None:
    """Locate the HubSpot deal+company already representing this lead, if any.

    Used before creating records so a lead whose link is unknown (e.g. a
    backfilled snapshot) updates its existing deal instead of duplicating it.
    """
    candidates = _collect_hubspot_link_candidates(
        hubspot,
        lead,
        enrichment,
        pipeline_id=pipeline_id,
        prop_project_url=prop_project_url,
    )
    match = _match_hubspot_deal(lead, enrichment, candidates)
    if match is None:
        return None
    company_id = (match.company_id or "").strip()
    if not company_id:
        company_ids = hubspot.list_deal_company_ids(match.id)
        company_id = company_ids[0] if company_ids else ""
    if not company_id:
        return None
    return match.id, company_id


def link_hubspot_exports(
    settings: Settings,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    limit: int = 0,
    delay_ms: int = 250,
) -> dict[str, Any]:
    """Match lead_exports to existing HubSpot deals by name (enables --resync)."""
    if not settings.hubspot_private_app_token:
        return {"skipped": True, "reason": "HUBSPOT_PRIVATE_APP_TOKEN not set", "linked": 0}

    write_config = hubspot_write_config_from_settings(settings)
    pipeline_id = write_config.pipeline_id if write_config else settings.hubspot_deal_pipeline_id
    conn = db.connect(settings.db_path)
    export_counts = db.lead_export_hubspot_counts(conn)
    hubspot = HubSpotClient(token=settings.hubspot_private_app_token)

    linked = 0
    not_found = 0
    skipped = 0
    rate_limited = False
    results: list[dict[str, Any]] = []
    pause_s = max(int(delay_ms), 0) / 1000.0
    attempt_limit = int(limit) if limit and limit > 0 else None
    attempted = 0

    export_rows = db.iter_lead_exports(conn)
    if not export_rows and export_counts["total"] == 0:
        return {
            "linked": 0,
            "not_found": 0,
            "skipped": 0,
            "dry_run": dry_run,
            "overwrite": overwrite,
            "db_path": settings.db_path,
            "exports_total": 0,
            "exports_unlinked": 0,
            "exports_already_linked": 0,
            "hubspot_resync_available": 0,
            "hint": "Run: python -m skysnap backfill-exports",
            "results": [],
        }

    for export_row in export_rows:
        if attempt_limit is not None and attempted >= attempt_limit:
            break
        if export_row.hubspot_deal_id and export_row.hubspot_company_id and not overwrite:
            skipped += 1
            continue
        lead = db.get_lead(conn, export_row.lead_id)
        if lead is None:
            skipped += 1
            continue
        attempted += 1

        enrichment: EnrichmentResult | None = None
        if export_row.enrichment_json and export_row.enrichment_json.strip() not in ("", "{}"):
            enrichment = EnrichmentResult.model_validate_json(export_row.enrichment_json)

        try:
            candidates = _collect_hubspot_link_candidates(
                hubspot,
                lead,
                enrichment,
                pipeline_id=pipeline_id,
                prop_project_url=write_config.prop_project_url if write_config else None,
            )
        except HubSpotRateLimitError as exc:
            rate_limited = True
            if len(results) < 20:
                results.append(
                    {
                        "lead_id": lead.id,
                        "status": "rate_limited",
                        "error": str(exc),
                    }
                )
            break

        match = _match_hubspot_deal(lead, enrichment, candidates)
        if match is None:
            not_found += 1
            if len(results) < 20:
                results.append(
                    {
                        "lead_id": lead.id,
                        "status": "not_found",
                        "dealname": deal_name(lead, enrichment),
                    }
                )
            continue

        company_id = (match.company_id or "").strip()
        if not company_id:
            try:
                company_ids = hubspot.list_deal_company_ids(match.id)
            except HubSpotRateLimitError as exc:
                rate_limited = True
                if len(results) < 20:
                    results.append(
                        {
                            "lead_id": lead.id,
                            "status": "rate_limited",
                            "error": str(exc),
                        }
                    )
                break
            company_id = company_ids[0] if company_ids else ""

        if not company_id:
            not_found += 1
            if len(results) < 20:
                results.append(
                    {
                        "lead_id": lead.id,
                        "status": "no_company",
                        "hubspot_deal_id": match.id,
                        "dealname": match.dealname,
                    }
                )
            continue

        if not dry_run:
            db.link_lead_export_hubspot(
                conn,
                lead.id,
                deal_id=match.id,
                company_id=company_id,
            )
        linked += 1
        if len(results) < 20:
            results.append(
                {
                    "lead_id": lead.id,
                    "status": "linked" if not dry_run else "would_link",
                    "hubspot_deal_id": match.id,
                    "hubspot_company_id": company_id,
                    "dealname": match.dealname,
                }
            )

        if pause_s > 0:
            time.sleep(pause_s)

    resync_available = len(db.iter_leads_hubspot_resync(conn)) if not dry_run else None
    db.checkpoint(conn)
    return {
        "linked": linked,
        "not_found": not_found,
        "skipped": skipped,
        "rate_limited": rate_limited,
        "attempted": attempted,
        "dry_run": dry_run,
        "overwrite": overwrite,
        "limit": attempt_limit,
        "delay_ms": delay_ms,
        "db_path": settings.db_path,
        "exports_total": export_counts["total"],
        "exports_unlinked": export_counts["unlinked"],
        "exports_already_linked": export_counts["linked"],
        "exports_processed": len(export_rows),
        "hubspot_resync_available": resync_available,
        "results": results,
        "hint": (
            "Re-run link-hubspot to continue after rate limit."
            if rate_limited
            else None
        ),
    }


def push_hubspot_leads(
    settings: Settings,
    *,
    lead_id: int | None = None,
    lead_ids: list[int] | None = None,
    all_pending: bool = False,
    resync_all: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Push exported leads to HubSpot (Deal + Company + Contact)."""
    if not settings.hubspot_push_enabled:
        return {
            "skipped": True,
            "reason": "SKYSNAP_HUBSPOT_PUSH_ENABLED=false",
            "results": [],
        }
    if not settings.hubspot_private_app_token:
        return {
            "skipped": True,
            "reason": "HUBSPOT_PRIVATE_APP_TOKEN not set",
            "results": [],
        }
    write_config = hubspot_write_config_from_settings(settings)
    if write_config is None:
        return {
            "skipped": True,
            "reason": "HUBSPOT_DEAL_PIPELINE_ID and HUBSPOT_DEAL_STAGE_ID required",
            "results": [],
        }
    task_config = hubspot_followup_config_from_settings(settings)

    conn = db.connect(settings.db_path)
    # Explicitly named leads are always pushed, even if synced before.
    explicit_targets = lead_id is not None or lead_ids is not None
    if lead_id is not None:
        target_ids = [int(lead_id)]
    elif lead_ids is not None:
        target_ids = [int(i) for i in lead_ids]
    elif resync_all:
        target_ids = db.iter_leads_hubspot_resync(conn)
    elif all_pending or (lead_id is None and lead_ids is None):
        target_ids = db.iter_leads_pending_hubspot_sync(conn)
    else:
        target_ids = []

    hubspot = HubSpotClient(token=settings.hubspot_private_app_token)
    results: list[dict[str, Any]] = []
    pushed = 0
    failed = 0

    for lid in target_ids:
        export_row = db.get_lead_export(conn, lid)
        if export_row is None:
            results.append({"lead_id": lid, "status": "skipped", "reason": "no export snapshot"})
            continue
        if (
            export_row.hubspot_synced_at
            and not dry_run
            and not resync_all
            and not explicit_targets
        ):
            results.append(
                {
                    "lead_id": lid,
                    "status": "skipped",
                    "reason": "already synced",
                    "hubspot_deal_id": export_row.hubspot_deal_id,
                }
            )
            continue
        if resync_all and not (export_row.hubspot_deal_id and export_row.hubspot_company_id):
            results.append(
                {
                    "lead_id": lid,
                    "status": "skipped",
                    "reason": "not previously synced",
                }
            )
            continue
        lead = db.get_lead(conn, lid)
        if lead is None:
            results.append({"lead_id": lid, "status": "skipped", "reason": "lead not found"})
            continue

        enrichment: EnrichmentResult | None = None
        decision: FuzzyDuplicateDecision | None = None
        project_similarity: ProjectSimilarityDecision | None = None
        try:
            if export_row.enrichment_json and export_row.enrichment_json.strip() not in ("", "{}"):
                enrichment = EnrichmentResult.model_validate_json(export_row.enrichment_json)
            if export_row.duplicate_decision_json:
                decision = FuzzyDuplicateDecision.model_validate_json(
                    export_row.duplicate_decision_json
                )
            if export_row.project_similarity_json:
                project_similarity = ProjectSimilarityDecision.model_validate_json(
                    export_row.project_similarity_json
                )

            link_deal_id = export_row.hubspot_deal_id
            link_company_id = export_row.hubspot_company_id
            adopted = False
            if not (link_deal_id and link_company_id) and not dry_run:
                # Never create a second deal for a lead HubSpot already knows.
                existing = find_existing_hubspot_link(
                    hubspot,
                    lead,
                    enrichment,
                    pipeline_id=write_config.pipeline_id,
                    prop_project_url=write_config.prop_project_url,
                )
                if existing:
                    link_deal_id, link_company_id = existing
                    adopted = True
                    db.link_lead_export_hubspot(
                        conn, lid, deal_id=link_deal_id, company_id=link_company_id
                    )

            push_result = hubspot.push_lead_export(
                lead,
                enrichment,
                decision,
                write_config=write_config,
                followup_config=task_config,
                project_similarity=project_similarity,
                project_similarity_min_score=settings.project_similarity_min_score,
                dry_run=dry_run,
                resync_company_id=link_company_id,
                resync_deal_id=link_deal_id,
                previous_note_hash=export_row.hubspot_note_hash,
                previous_task_id=export_row.hubspot_task_id,
            )
            if not dry_run:
                db.mark_hubspot_synced(
                    conn,
                    lid,
                    deal_id=push_result.deal_id,
                    company_id=push_result.company_id,
                    contact_id=push_result.contact_id or export_row.hubspot_contact_id,
                    task_id=push_result.task_id or export_row.hubspot_task_id,
                    note_hash=push_result.note_hash or export_row.hubspot_note_hash,
                )
            pushed += 1
            entry: dict[str, Any] = {
                "lead_id": lid,
                "status": "dry_run" if dry_run else ("resynced" if resync_all else "pushed"),
                **push_result.as_dict(),
            }
            if adopted:
                entry["adopted_existing_deal"] = True
            results.append(entry)
            log_progress(
                f"HubSpot {'dry-run' if dry_run else 'push'} lead {lid}: "
                f"deal={push_result.deal_id}"
            )
        except HubSpotWriteError as e:
            failed += 1
            if not dry_run:
                db.set_hubspot_sync_error(conn, lid, str(e))
            results.append({"lead_id": lid, "status": "failed", "error": str(e)})
            log_progress(f"HubSpot push failed lead {lid}: {e}")
        except Exception as e:
            failed += 1
            if not dry_run:
                db.set_hubspot_sync_error(conn, lid, str(e))
            results.append({"lead_id": lid, "status": "failed", "error": str(e)})
            log_progress(f"HubSpot push failed lead {lid}: {type(e).__name__}: {e}")

    export_counts = db.lead_export_hubspot_counts(conn)
    db.checkpoint(conn)
    out: dict[str, Any] = {
        "skipped": False,
        "dry_run": dry_run,
        "resync_all": resync_all,
        "target_count": len(target_ids),
        "pushed": pushed,
        "failed": failed,
        "db_path": str(Path(settings.db_path).resolve()),
        "exports_total": export_counts["total"],
        "exports_linked_to_hubspot": export_counts["linked"],
        "results": results,
    }
    if not target_ids:
        if export_counts["total"] == 0:
            out["hint"] = "No export snapshots. Run: python -m skysnap backfill-exports"
        elif resync_all and export_counts["linked"] == 0:
            out["hint"] = "No exports linked to HubSpot. Run: python -m skysnap link-hubspot"
        else:
            out["hint"] = "Every export snapshot is already synced; nothing to push."
    dropped_summary = _summarize_dropped_properties(results)
    if dropped_summary:
        out["dropped_properties_summary"] = dropped_summary
    return out


def _summarize_dropped_properties(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-lead dropped properties so problems are visible at a glance."""
    counts: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for item in results:
        for dropped in item.get("dropped_properties") or ():
            name = str(dropped.get("property"))
            counts[name] = counts.get(name, 0) + 1
            reasons.setdefault(name, str(dropped.get("reason", "")))
    return {
        name: {"leads": count, "reason": reasons.get(name, "")}
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1])
    }


def run_pipeline(
    settings: Settings,
    *,
    mark_seen: bool = True,
    dry_run: bool = False,
    use_ai: bool = True,
    push_hubspot: bool = True,
) -> dict[str, Any]:
    """Ingest new emails, then process top-N pending leads (HubSpot push included)."""
    ingest = ingest_from_email(settings, mark_seen=mark_seen)
    daily = run_daily(
        settings,
        dry_run=dry_run,
        use_ai=use_ai,
        push_hubspot=push_hubspot,
    )
    return {"ingest": ingest, "daily": daily, "hubspot": daily.get("hubspot")}
