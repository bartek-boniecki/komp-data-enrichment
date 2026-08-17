"""Phase A (Kompass) export gate: personal, generic, or ICP threshold."""

from skysnap.db import Lead, LeadStatus
from skysnap.enrichment import (
    needs_contact_gap_search,
    qualifies_for_phase_a_export,
)
from skysnap.models import EnrichmentResult, WebsiteContact


def _lead(**kwargs) -> Lead:
    defaults = dict(
        id=1,
        source="manual_kompass",
        source_message_id=None,
        source_received_at=None,
        project_name="Budowa hali",
        company_name="Firma Sp. z o.o.",
        country="PL",
        city=None,
        project_value="25 mln PLN",
        project_phase=None,
        project_url="https://kompasinwestycji.pl/x",
        raw_payload_json={},
        icp_score=80,
        icp_reason="test",
        status=LeadStatus.pending,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        last_error=None,
    )
    defaults.update(kwargs)
    return Lead(**defaults)


def test_phase_a_export_personal_contact():
    lead = _lead(icp_score=40)
    enrichment = EnrichmentResult(
        source="kompass",
        contact=WebsiteContact(full_name="Jan Kowalski", confidence=0.8),
    )
    assert qualifies_for_phase_a_export(lead, enrichment, min_icp=60)


def test_phase_a_export_generic_contact_below_icp():
    lead = _lead(icp_score=55)
    enrichment = EnrichmentResult(
        source="kompass",
        company_generic_email="biuro@firma.pl",
        company_generic_phone="+48123456789",
    )
    assert qualifies_for_phase_a_export(lead, enrichment, min_icp=60)


def test_phase_a_export_icp_threshold_without_contact():
    lead = _lead(icp_score=65, company_name=None)
    enrichment = EnrichmentResult(source="kompass", notes="brak kontaktu")
    assert qualifies_for_phase_a_export(lead, enrichment, min_icp=60)


def test_phase_a_export_blocked_low_icp_no_contact():
    lead = _lead(icp_score=55, company_name=None)
    enrichment = EnrichmentResult(source="kompass")
    assert not qualifies_for_phase_a_export(lead, enrichment, min_icp=60)


def test_generic_contact_triggers_osint_gap_search():
    enrichment = EnrichmentResult(
        source="kompass",
        company_generic_email="biuro@firma.pl",
    )
    assert needs_contact_gap_search(enrichment, company_name="Firma Sp. z o.o.")
