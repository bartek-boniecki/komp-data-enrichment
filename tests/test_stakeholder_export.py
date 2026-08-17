"""Tests for GW/investor export without personal contact."""

from skysnap.db import Lead, LeadStatus
from skysnap.enrichment import has_identified_stakeholder, qualifies_for_stakeholder_export
from skysnap.models import EnrichmentResult


def _lead(**kwargs) -> Lead:
    defaults = dict(
        id=1,
        source="kompass_email",
        source_message_id="m1",
        source_received_at=None,
        project_name="Budowa hali",
        company_name=None,
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


def test_company_name_qualifies_stakeholder():
    lead = _lead(company_name="Budimex S.A.")
    enrichment = EnrichmentResult(source="kompass", notes="brak panelu kontaktowego")
    assert has_identified_stakeholder(lead, enrichment)
    assert qualifies_for_stakeholder_export(lead, enrichment, min_icp=60)


def test_gw_branza_without_company_name():
    lead = _lead(icp_score=75)
    enrichment = EnrichmentResult(
        source="kompass",
        sheet_branza="Generalni wykonawcy",
        notes="GW wybrany",
    )
    assert has_identified_stakeholder(lead, enrichment)
    assert qualifies_for_stakeholder_export(lead, enrichment, min_icp=60)


def test_low_icp_does_not_qualify():
    lead = _lead(company_name="Inwestor Sp. z o.o.", icp_score=55)
    enrichment = EnrichmentResult(source="kompass")
    assert has_identified_stakeholder(lead, enrichment)
    assert not qualifies_for_stakeholder_export(lead, enrichment, min_icp=60)


def test_no_stakeholder_signal():
    lead = _lead(icp_score=80)
    enrichment = EnrichmentResult(source="kompass", notes="tylko opis projektu")
    assert not has_identified_stakeholder(lead, enrichment)
    assert not qualifies_for_stakeholder_export(lead, enrichment, min_icp=60)


def test_investor_signal_in_notes():
    lead = _lead(icp_score=70)
    enrichment = EnrichmentResult(
        source="kompass",
        company_name="ABC Development",
        notes="Inwestor: ABC Development",
    )
    assert has_identified_stakeholder(lead, enrichment)
    assert qualifies_for_stakeholder_export(lead, enrichment, min_icp=60)
