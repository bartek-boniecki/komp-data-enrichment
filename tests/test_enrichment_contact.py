"""Unit tests for personal vs generic contact classification."""

from skysnap.enrichment import (
    has_exportable_contact_data,
    has_generic_contact_data,
    has_personal_contact_data,
    separate_generic_contact_channels,
)
from skysnap.models import EnrichmentResult, WebsiteContact


def test_personal_contact_with_name():
    enrichment = EnrichmentResult(
        source="kompass",
        contact=WebsiteContact(full_name="Jan Kowalski", confidence=0.8),
    )
    assert has_personal_contact_data(enrichment)
    assert not has_generic_contact_data(enrichment)


def test_generic_company_email_not_personal():
    enrichment = EnrichmentResult(
        source="kompass",
        company_generic_email="biuro@firma.pl",
        company_generic_phone="+48123456789",
    )
    assert not has_personal_contact_data(enrichment)
    assert has_generic_contact_data(enrichment)
    assert has_exportable_contact_data(enrichment)


def test_role_email_without_name_demoted_to_generic():
    enrichment = EnrichmentResult(
        source="osint",
        contact=WebsiteContact(
            email="kontakt@firma.pl",
            phone="+48111222333",
            confidence=0.5,
        ),
    )
    normalized = separate_generic_contact_channels(enrichment)
    assert normalized is not None
    assert normalized.contact is None
    assert normalized.company_generic_email == "kontakt@firma.pl"
    assert normalized.company_generic_phone == "+48111222333"
    assert not has_personal_contact_data(normalized)


def test_personal_email_without_name_counts_as_personal():
    enrichment = EnrichmentResult(
        source="osint",
        contact=WebsiteContact(email="jan.kowalski@firma.pl", confidence=0.7),
    )
    assert has_personal_contact_data(enrichment)
