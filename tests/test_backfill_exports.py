"""Tests for backfill-export snapshots."""

import sqlite3

from skysnap import db
from skysnap.config import Settings
from skysnap.db import LeadStatus
from skysnap.engine import backfill_export_snapshots


def _settings(tmp_path) -> Settings:
    db_path = str(tmp_path / "test.sqlite")
    return Settings(
        db_path=db_path,
        daily_limit=5,
        min_score=40,
        stakeholder_export_min_icp=60,
        user_agent="test",
        blocked_contact_emails=frozenset(),
        blocked_contact_email_domains=frozenset(),
        blocked_contact_phones=frozenset(),
        blocked_contact_website_hosts=frozenset(),
        anthropic_api_key="",
        claude_model="claude-sonnet-4-20250514",
        nvidia_api_key=None,
        nvidia_nim_model="meta/llama-3.3-70b-instruct",
        imap_host=None,
        imap_port=993,
        imap_username=None,
        imap_password=None,
        imap_folder="INBOX",
        imap_search_query="UNSEEN",
        hubspot_private_app_token=None,
        hubspot_push_enabled=False,
        hubspot_deal_pipeline_id=None,
        hubspot_deal_stage_id=None,
        hubspot_prop_project_url=None,
        hubspot_prop_project_name=None,
        hubspot_prop_icp_score=None,
        hubspot_prop_leads_origin=None,
        hubspot_prop_stage_inwestycji=None,
        hubspot_prop_deal_typ=None,
        hubspot_prop_deal_source=None,
        hubspot_prop_deal_branza=None,
        hubspot_prop_deal_role=None,
        hubspot_prop_nip=None,
        hubspot_prop_opis=None,
        hubspot_sync_company_fields=True,
        hubspot_update_existing_deals=True,
        hubspot_company_owner_id=None,
        hubspot_prop_branza_skysnap=None,
        hubspot_prop_branza_extrainfo=None,
        hubspot_prop_leads_score=None,
        hubspot_prop_ai_score=None,
        hubspot_prop_company_notes=None,
        hubspot_prop_uslugi=None,
        hubspot_prop_typ=None,
        hubspot_prop_konkurencja=None,
        hubspot_prop_konkurencja_expiry=None,
        hubspot_prop_voivodeship=None,
        hubspot_prop_sektor_podsektor=None,
        hubspot_prop_project_city=None,
        hubspot_prop_project_voivodeship=None,
        hubspot_prop_project_street=None,
        hubspot_prop_project_building_number=None,
        hubspot_create_analysis_note=True,
        hubspot_create_task=True,
        hubspot_task_when="always",
        hubspot_task_owner_id=None,
        hubspot_task_type="CALL",
        hubspot_task_due_days=7,
        hubspot_ticket_pipeline_id=None,
        hubspot_ticket_stage_id=None,
        google_service_account_json=None,
        google_sheet_id=None,
        google_sheet_tab_name="Leads",
        kompass_username=None,
        kompass_password=None,
        kompass_base_url="https://www.kompasinwestycji.pl",
        kompass_login_path="/zaloguj",
        kompass_browser_state_dir=str(tmp_path / "kompass"),
        kompass_headless=True,
        timezone="Europe/Warsaw",
        osint_daily_cap=None,
        osint_max_subpages=4,
        email_mx_check=False,
        email_pattern_guess=False,
        gus_bir_api_key=None,
        claude_usage_log_dir=str(tmp_path / "logs"),
        claude_input_price_per_mtok=3.0,
        claude_output_price_per_mtok=15.0,
        project_similarity_enabled=True,
        project_similarity_min_score=0,
    )


def test_backfill_export_snapshots_creates_rows(tmp_path):
    settings = _settings(tmp_path)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    lead_id = db.upsert_lead(
        conn,
        source="kompass_email",
        source_message_id="m1",
        source_received_at=None,
        project_name="Test project",
        company_name="Acme",
        country="PL",
        city="Warsaw",
        project_value=None,
        project_phase="Realizacja",
        project_url="https://example.com/p",
        raw_payload_json={},
        icp_score=70,
        icp_reason="Good fit",
    )
    db.set_status(conn, lead_id, LeadStatus.processed_success, last_error=None)

    result = backfill_export_snapshots(settings)
    assert result["created"] == 1
    assert result["hubspot_push_pending"] == 1
    export = db.get_lead_export(conn, lead_id)
    assert export is not None
    assert "Acme" in export.enrichment_json


def test_link_does_not_mark_export_as_synced(tmp_path):
    """Linking records ids only; the push must still write the properties."""
    settings = _settings(tmp_path)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    lead_id = db.upsert_lead(
        conn,
        source="kompass_email",
        source_message_id="m1",
        source_received_at=None,
        project_name="Test project",
        company_name="Acme",
        country="PL",
        city="Warsaw",
        project_value=None,
        project_phase="Realizacja",
        project_url="https://example.com/p",
        raw_payload_json={},
        icp_score=70,
        icp_reason="Good fit",
    )
    db.set_status(conn, lead_id, LeadStatus.processed_success, last_error=None)
    db.save_lead_export(conn, lead_id, enrichment=None)

    db.link_lead_export_hubspot(conn, lead_id, deal_id="111", company_id="222")

    export = db.get_lead_export(conn, lead_id)
    assert export is not None
    assert export.hubspot_deal_id == "111"
    assert export.hubspot_synced_at is None
    # Still queued, so `push-hubspot --all` updates the linked records
    assert db.iter_leads_pending_hubspot_sync(conn) == [lead_id]
    assert db.iter_leads_hubspot_resync(conn) == [lead_id]


def test_re_export_keeps_hubspot_links_and_requeues_push(tmp_path):
    """Re-exporting a synced lead must not orphan its HubSpot records."""
    settings = _settings(tmp_path)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    lead_id = db.upsert_lead(
        conn,
        source="kompass_email",
        source_message_id="m1",
        source_received_at=None,
        project_name="Test project",
        company_name="Acme",
        country="PL",
        city="Warsaw",
        project_value=None,
        project_phase="Realizacja",
        project_url="https://example.com/p",
        raw_payload_json={},
        icp_score=70,
        icp_reason="Good fit",
    )
    db.set_status(conn, lead_id, LeadStatus.processed_success, last_error=None)
    db.save_lead_export(conn, lead_id, enrichment=None)
    db.mark_hubspot_synced(conn, lead_id, deal_id="111", company_id="222")

    assert db.iter_leads_pending_hubspot_sync(conn) == []
    assert db.iter_leads_hubspot_resync(conn) == [lead_id]

    db.save_lead_export(conn, lead_id, enrichment=None)

    export = db.get_lead_export(conn, lead_id)
    assert export is not None
    assert export.hubspot_deal_id == "111"
    assert export.hubspot_company_id == "222"
    assert export.hubspot_synced_at is None
    # Fresh snapshot is queued for push, and resync still finds the linked deal
    assert db.iter_leads_pending_hubspot_sync(conn) == [lead_id]
    assert db.iter_leads_hubspot_resync(conn) == [lead_id]
