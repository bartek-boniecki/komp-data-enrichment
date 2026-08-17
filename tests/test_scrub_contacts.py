"""Tests for scrubbing SkySnap-login / Kompass-platform contact leakage."""

from skysnap.enrichment import (
    is_blocked_email,
    is_blocked_phone,
    is_blocked_website,
    phone_national_digits,
    scrub_platform_contacts,
)
from skysnap.models import EnrichmentResult, WebsiteContact


def test_phone_national_digits_strips_country_code():
    assert phone_national_digits("+4848226332685") == "226332685"
    assert phone_national_digits("+48226332685") == "226332685"
    assert phone_national_digits("48226332685") == "226332685"
    assert phone_national_digits("22 633 26 85") == "226332685"


def test_default_blocklist_matches_leaked_values():
    assert is_blocked_email("pawel.wojcik@skysnap.pl")
    assert is_blocked_email("anyone@skysnap.pl")
    assert not is_blocked_email("kontakt@realfirma.pl")
    assert is_blocked_phone("+4848226332685")
    assert not is_blocked_phone("+48600800250")
    assert is_blocked_website("https://skysnap.pl")
    assert is_blocked_website("http://www.skysnap.pl/kontakt")
    assert not is_blocked_website("https://silta.pl")


def test_scrub_removes_generic_channels_and_website():
    enr = EnrichmentResult(
        source="kompass",
        website="https://skysnap.pl",
        company_generic_email="pawel.wojcik@skysnap.pl",
        company_generic_phone="+4848226332685",
    )
    out = scrub_platform_contacts(enr)
    assert out.website is None
    assert out.company_generic_email is None
    assert out.company_generic_phone is None


def test_scrub_keeps_named_contact_but_drops_bad_channels():
    enr = EnrichmentResult(
        source="kompass",
        contact=WebsiteContact(confidence=0.5, 
            full_name="Jan Kowalski",
            email="pawel.wojcik@skysnap.pl",
            phone="+4848226332685",
        ),
    )
    out = scrub_platform_contacts(enr)
    assert out.contact is not None
    assert out.contact.full_name == "Jan Kowalski"
    assert out.contact.email is None
    assert out.contact.phone is None


def test_scrub_drops_contact_with_only_bad_channels():
    enr = EnrichmentResult(
        source="kompass",
        contact=WebsiteContact(confidence=0.5, email="pawel.wojcik@skysnap.pl"),
    )
    out = scrub_platform_contacts(enr)
    assert out.contact is None


def test_scrub_preserves_legit_contact():
    enr = EnrichmentResult(
        source="kompass",
        website="https://sainz.pl",
        contact=WebsiteContact(confidence=0.5, 
            full_name="Jarosław Król",
            email="jaroslaw.krol@sainz.pl",
            phone="+48600800250",
        ),
    )
    out = scrub_platform_contacts(enr)
    assert out.website == "https://sainz.pl"
    assert out.contact.email == "jaroslaw.krol@sainz.pl"
    assert out.contact.phone == "+48600800250"


def test_scrub_extended_blocklist_from_config():
    enr = EnrichmentResult(
        source="kompass",
        company_generic_email="biuro@platforma.example",
    )
    out = scrub_platform_contacts(enr, email_domains=frozenset({"platforma.example"}))
    assert out.company_generic_email is None
