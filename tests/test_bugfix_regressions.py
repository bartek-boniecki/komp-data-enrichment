"""Regression tests for the accuracy-review bug fixes.

Each test mirrors a reproduction from the code review; the pre-fix code fails
every one of them.
"""

from __future__ import annotations

import sqlite3

from skysnap import db
from skysnap.contact_extract import ExtractedContacts, extract_contacts_from_html
from skysnap.contact_finalize import finalize_enrichment_contact
from skysnap.contact_search import _merge_contacts, merge_gap_fill
from skysnap.db import LeadStatus
from skysnap.engine import _is_kompass_project_url
from skysnap.icp import apply_icp_rubric, parse_value_pln_millions
from skysnap.kompass import _extract_contact_signals
from skysnap.models import EnrichmentResult, WebsiteContact
from skysnap.osint import _pick_best_website
from skysnap.sheet_rows import _contact_direct_email, cell_for_header
from skysnap.sheet_taxonomy import map_role


# --- #6: Phase B Kompass prefetch URL matching ------------------------------ #


def test_kompass_url_matcher_accepts_real_domain():
    # 'kompass' (double s) is NOT a substring of 'kompasinwestycji.pl'; the
    # helper must be used everywhere instead of ad-hoc substring checks.
    assert _is_kompass_project_url(
        "https://www.kompasinwestycji.pl/s16-barczewo-biskupiec-80699"
    )
    assert _is_kompass_project_url("https://kompasinwestycji.pl/-28575")
    assert not _is_kompass_project_url("https://www.budimex.pl/kontakt")


# --- #8: fabricated project values from digit concatenation ----------------- #


KOMPASS_PAGE_PROSE = """
Typ Publiczna
Sektor, podsektor niemieszkaniowy - drogi
Miasto Bierun
Kod pocztowy 43-150
Data aktualizacji 2026-08-01
Numery dzialek 1017/25, 1016/12
NIP 5213017228
Etap Realizacja
"""


def test_value_parser_ignores_prose_digit_soup():
    assert parse_value_pln_millions(KOMPASS_PAGE_PROSE) is None


def test_value_parser_still_accepts_bare_amounts_and_units():
    assert parse_value_pln_millions("15000000") == 15.0
    assert parse_value_pln_millions("15 000 000") == 15.0
    assert parse_value_pln_millions("25 mln PLN") == 25.0
    assert parse_value_pln_millions("8 mln") == 8.0


def test_icp_rubric_not_inflated_by_page_prose():
    adj = apply_icp_rubric(
        icp_score=50,
        icp_reason="Kompass Email; test",
        project_name="S1 wezel",
        project_value=None,
        project_phase="Realizacja",
        extra_text=KOMPASS_PAGE_PROSE,
    )
    assert "value_ideal_20m_plus" not in adj.flags
    assert "value_viable_10_17m" not in adj.flags
    assert "warto" not in (adj.icp_reason or "").lower() or "mln" not in (
        adj.icp_reason or ""
    ), f"fabricated value leaked into reason: {adj.icp_reason!r}"


# --- #3: invalid contacts must be dropped, not exported --------------------- #


def test_finalize_drops_invalid_email_and_phone():
    e = EnrichmentResult(
        source="osint",
        contact=WebsiteContact(
            full_name="Jan Kowalski",
            role="Kierownik budowy",
            email="jan.kowalski@firma",  # no TLD
            phone="tel. 22 12",  # not a number
            confidence=0.6,
        ),
    )
    out = finalize_enrichment_contact(
        e, ExtractedContacts(), restrict_domain=None, check_mx=False, allow_pattern_guess=False
    )
    assert out.contact.email is None
    assert out.contact.phone is None
    assert "odrzucono" in (out.notes or "")


def test_finalize_keeps_valid_email_and_phone():
    e = EnrichmentResult(
        source="osint",
        contact=WebsiteContact(
            full_name="Jan Kowalski",
            email="jan.kowalski@firma.pl",
            phone="+48 502 713 692",
            confidence=0.6,
        ),
    )
    out = finalize_enrichment_contact(
        e, ExtractedContacts(), restrict_domain=None, check_mx=False, allow_pattern_guess=False
    )
    assert out.contact.email == "jan.kowalski@firma.pl"
    assert out.contact.phone == "+48502713692"


# --- #4a: generic channel must never become the DIRECT channel -------------- #


def test_finalize_does_not_promote_generic_phone_to_direct():
    e = EnrichmentResult(
        source="kompass",
        contact=WebsiteContact(
            full_name="Jan Kowalski",
            phone="+48 22 623 60 00",  # switchboard in the general field
            direct_phone=None,
            confidence=0.6,
        ),
    )
    out = finalize_enrichment_contact(
        e, ExtractedContacts(), restrict_domain=None, check_mx=False, allow_pattern_guess=False
    )
    assert out.contact.phone == "+48226236000"  # kept as the general channel
    assert out.contact.direct_phone is None  # NOT promoted


def test_finalize_direct_phone_survives_when_actually_direct():
    e = EnrichmentResult(
        source="kompass",
        contact=WebsiteContact(
            full_name="Jan Kowalski",
            direct_phone="+48 502 713 692",
            confidence=0.6,
        ),
    )
    out = finalize_enrichment_contact(
        e, ExtractedContacts(), restrict_domain=None, check_mx=False, allow_pattern_guess=False
    )
    assert out.contact.direct_phone == "+48502713692"


# --- #2: pattern guesses stay segregated ------------------------------------ #


def test_pattern_guess_goes_to_guessed_email_only():
    e = EnrichmentResult(
        source="osint",
        website="https://firma.pl",
        contact=WebsiteContact(
            full_name="Anna Nowak", role="Specjalista ds. ofertowania", confidence=0.5
        ),
    )
    out = finalize_enrichment_contact(
        e, ExtractedContacts(), restrict_domain="firma.pl", check_mx=False, allow_pattern_guess=True
    )
    assert out.contact.email is None
    assert out.contact.direct_email is None
    assert out.contact.guessed_email == "anna.nowak@firma.pl"
    assert out.contact.confidence == 0.5  # a guess earns no confidence bump
    assert "NIEZWERYFIKOWANY" in (out.notes or "")


def test_guessed_email_not_exported_in_email_columns():
    from skysnap.db import Lead, utc_now_iso

    now = utc_now_iso()
    lead = Lead(
        id=1, source="manual_kompass", source_message_id=None, source_received_at=None,
        project_name="P", company_name="Firma", country="PL", city=None,
        project_value=None, project_phase=None, project_url=None, raw_payload_json={},
        icp_score=70, icp_reason=None, status=LeadStatus.pending,
        created_at=now, updated_at=now, last_error=None,
    )
    enrichment = EnrichmentResult(
        source="osint",
        contact=WebsiteContact(full_name="Anna Nowak", guessed_email="anna.nowak@firma.pl", confidence=0.5),
    )
    assert cell_for_header("Email ", lead=lead, enrichment=enrichment, decision=None) == ""
    assert cell_for_header("Email Direct", lead=lead, enrichment=enrichment, decision=None) == ""
    assert (
        cell_for_header("Email Guessed", lead=lead, enrichment=enrichment, decision=None)
        == "anna.nowak@firma.pl"
    )


# --- #4b: channels merge no longer promotes generics / erases role ---------- #


def test_channels_merge_keeps_generic_out_of_direct_fields():
    base = WebsiteContact(full_name="Jan Kowalski", role=None, confidence=0.6)
    found = WebsiteContact(
        full_name="Jan Kowalski",
        role="Sekretariat",
        email="biuro@firma.pl",
        direct_email="biuro@firma.pl",
        phone="+48 22 623 60 00",
        confidence=0.5,
    )
    merged = _merge_contacts(base, found, phase="channels")
    assert merged.direct_email is None  # role mailbox never a direct email
    assert merged.direct_phone is None  # general phone not promoted
    assert merged.email == "biuro@firma.pl"  # still available as general channel


def test_gap_fill_routes_generic_channels_to_company_fields():
    base = EnrichmentResult(source="osint")
    found = EnrichmentResult(
        source="osint",
        company_generic_email="biuro@firma.pl",
        company_generic_phone="+48 22 623 60 00",
    )
    merged = merge_gap_fill(base, found, phase="channels")
    assert merged.company_generic_email == "biuro@firma.pl"
    assert merged.company_generic_phone == "+48 22 623 60 00"


# --- #5: job title never derived from company name -------------------------- #


def test_map_role_ignores_company_name_keywords():
    assert (
        map_role("Prezes Zarządu", company_name="Zakład Robót Ziemnych KOP-EX") == "Inne."
    )
    assert map_role("Dyrektor Handlowy", company_name="OPGK Geodezja Sp. z o.o.") == "Inne."
    assert map_role(None, company_name="Szpital Publiczny w Radomiu") == "Inne."


def test_map_role_still_maps_real_titles():
    assert map_role("Kierownik budowy") == "Kierownik budowy"
    assert map_role("specjalista ds. ofertowania") == "Specjalista do spraw ofertowania"
    assert map_role("Główny Geodeta") == "Geodeta"


# --- #7: Phase A iteration must not skip or duplicate leads ----------------- #


def _seed(conn: sqlite3.Connection, n: int) -> None:
    for i in range(n):
        db.upsert_lead(
            conn, source="manual_kompass", source_message_id=None, source_received_at=None,
            project_name=f"P{i:02d}", company_name=None, country="PL", city=None,
            project_value=None, project_phase=None, project_url=None,
            raw_payload_json={}, icp_score=100 - i, icp_reason=None,
        )


def test_iter_pending_survives_mid_iteration_status_changes(tmp_path):
    conn = db.connect(str(tmp_path / "a.sqlite"))
    _seed(conn, 30)
    attempted: set[int] = set()
    yielded: list[str] = []
    for lead in db.iter_pending_by_icp(conn, min_score=0, skip_ids=attempted, batch_size=20):
        attempted.add(lead.id)
        yielded.append(lead.project_name)
        db.set_status(conn, lead.id, LeadStatus.processed_success)
    assert len(yielded) == 30, f"leads silently skipped: {sorted(set(f'P{i:02d}' for i in range(30)) - set(yielded))}"
    assert yielded == [f"P{i:02d}" for i in range(30)]  # ICP order preserved


def test_iter_pending_honors_live_skip_set(tmp_path):
    conn = db.connect(str(tmp_path / "b.sqlite"))
    _seed(conn, 10)
    attempted: set[int] = set()
    yielded: list[int] = []
    for lead in db.iter_pending_by_icp(conn, min_score=0, skip_ids=attempted):
        yielded.append(lead.id)
        attempted.add(lead.id)  # stays pending (skipped_no_contact path)
    assert len(yielded) == len(set(yielded)) == 10  # no duplicates, no misses


# --- #10: reveal signal — IDs and amounts are not contacts ------------------ #


def test_contact_signal_ignores_nip_and_amounts():
    assert _extract_contact_signals("NIP 5213017228") == set()
    assert _extract_contact_signals("NIP: 526-100-31-87") == set()
    assert _extract_contact_signals("Wartosc 12500000 PLN") == set()
    assert _extract_contact_signals("Data aktualizacji: 2026-08-08") == set()


def test_contact_signal_detects_real_contacts():
    assert _extract_contact_signals("email: jan@firma.pl")
    assert _extract_contact_signals("tel. +48 502 713 692")
    assert _extract_contact_signals("kom. 502 713 692")
    assert _extract_contact_signals("(81) 746 22 94")
    assert _extract_contact_signals("22 623 60 00")


# --- #11: best_email prefers personal over role mailboxes ------------------- #


def test_best_email_prefers_personal_over_role():
    html = (
        '<a href="mailto:biuro@firma.pl">biuro@firma.pl</a>'
        "<p>Kierownik budowy Jan Kowalski, jan.kowalski@firma.pl</p>"
    )
    extracted = extract_contacts_from_html(html, url="https://firma.pl/kontakt")
    assert extracted.best_email(prefer_personal=True).value == "jan.kowalski@firma.pl"
    assert extracted.best_email(prefer_personal=False).value == "biuro@firma.pl"


# --- #2b: SERP website fallback requires a company token -------------------- #


def test_pick_best_website_requires_company_token():
    urls = [
        "https://ebudownictwo.pl/przetarg/12345",
        "https://www.budimex.pl/pl/kontakt",
    ]
    assert _pick_best_website(urls, company_name="Budimex S.A.") == "https://www.budimex.pl/pl/kontakt"
    assert _pick_best_website(urls, company_name="Mota-Engil Central Europe") is None
    assert _pick_best_website(urls, company_name=None) is None


# --- direct-email sheet fallback guard -------------------------------------- #


def test_sheet_direct_email_fallback_excludes_role_mailboxes():
    personal = WebsiteContact(full_name="Jan", email="jan.kowalski@firma.pl", confidence=0.6)
    generic = WebsiteContact(full_name="Jan", email="biuro@firma.pl", confidence=0.6)
    assert _contact_direct_email(personal) == "jan.kowalski@firma.pl"
    assert _contact_direct_email(generic) == ""


# ============================================================================ #
# Round 2 — "wrong company attributed to the contact" root causes (W1–W4)
# ============================================================================ #

from skysnap.engine import _apply_kompass_page_fetch, _companies_plausibly_match
from skysnap.enrichment import resolve_company_name
from skysnap.hubspot_export import resolve_company_id
from skysnap.kompass_firm import KompassFirmProfile
from skysnap.models import FuzzyDuplicateDecision, is_confident_duplicate


def _lead(company: str | None):
    from skysnap.db import Lead, utc_now_iso

    now = utc_now_iso()
    return Lead(
        id=1, source="kompass_email", source_message_id=None, source_received_at=None,
        project_name="S16 Barczewo-Biskupiec", company_name=company, country="PL",
        city=None, project_value=None, project_phase=None, project_url=None,
        raw_payload_json={}, icp_score=80, icp_reason=None, status=LeadStatus.pending,
        created_at=now, updated_at=now, last_error=None,
    )


# --- W1: contact and displayed company travel together ---------------------- #


def test_company_resolution_prefers_kompass_verified_org():
    lead = _lead("GDDKiA w Olsztynie")  # investor from the notification email
    enr = EnrichmentResult(
        source="kompass", company_name="Budimex S.A.",
        contact=WebsiteContact(full_name="Jan Kowalski", role="Kierownik kontraktu",
                               email="jan.kowalski@budimex.pl", confidence=0.8),
    )
    assert resolve_company_name(lead, enr) == "Budimex S.A."


def test_company_resolution_prefers_contact_anchored_org_in_osint():
    lead = _lead("GDDKiA w Olsztynie")
    enr = EnrichmentResult(
        source="osint", company_name="PORR S.A.",
        contact=WebsiteContact(full_name="Anna Nowak", confidence=0.6),
    )
    assert resolve_company_name(lead, enr) == "PORR S.A."


def test_company_resolution_keeps_lead_for_contactless_osint():
    lead = _lead("GDDKiA w Olsztynie")
    enr = EnrichmentResult(source="osint", company_name="Some Portal Sp. z o.o.")
    assert resolve_company_name(lead, enr) == "GDDKiA w Olsztynie"


def test_company_resolution_falls_back_when_lead_empty():
    lead = _lead(None)
    enr = EnrichmentResult(source="osint", company_name="PORR S.A.")
    assert resolve_company_name(lead, enr) == "PORR S.A."


# --- W3: mismatched firm profile is not merged onto the contact ------------- #


class _Fetch:
    def __init__(self, participant, profile):
        self.participant_company = participant
        self.firm_profile = profile
        self.generic_email = profile.email if profile else None
        self.generic_phone = profile.phones if profile else None
        self.text = ""


def test_companies_plausibly_match_tokens():
    assert _companies_plausibly_match("Budimex S.A.", "BUDIMEX Spółka Akcyjna")
    assert not _companies_plausibly_match("Budimex S.A.", "PORR S.A.")
    assert _companies_plausibly_match("Budimex S.A.", None)  # unknowable -> allow


def test_mismatched_firm_profile_not_merged():
    profile = KompassFirmProfile(
        profile_url="https://www.kompasinwestycji.pl/v2/firma/1",
        company_name="PORR S.A.", email="biuro@porr.pl", phones="22 111 22 33",
        nip="1111111111",
    )
    fetch = _Fetch("Budimex S.A.", profile)  # modal picked Budimex, profile is PORR
    enr = _apply_kompass_page_fetch(EnrichmentResult(source="kompass"), fetch, _lead(None))
    assert enr.company_generic_email is None
    assert enr.company_generic_phone is None
    assert enr.company_nip is None
    assert "Pominięto dane z profilu" in (enr.notes or "")
    assert enr.company_name == "Budimex S.A."  # participant still resolves company


def test_matching_firm_profile_still_merges():
    profile = KompassFirmProfile(
        profile_url="https://www.kompasinwestycji.pl/v2/firma/1",
        company_name="Budimex S.A.", email="info@budimex.pl", phones="22 623 60 00",
    )
    fetch = _Fetch("Budimex S.A.", profile)
    enr = _apply_kompass_page_fetch(EnrichmentResult(source="kompass"), fetch, _lead(None))
    assert enr.company_generic_email == "info@budimex.pl"


# --- W4: duplicate decisions gated on confidence ---------------------------- #


def test_low_confidence_duplicate_not_acted_upon():
    weak = FuzzyDuplicateDecision(
        is_duplicate=True, matched_company_id="123",
        matched_company_name="Budimet Sp. z o.o.", confidence=0.35,
    )
    assert not is_confident_duplicate(weak)
    assert resolve_company_id(weak) is None  # no attach to the wrong company


def test_confident_duplicate_still_acted_upon():
    strong = FuzzyDuplicateDecision(
        is_duplicate=True, matched_company_id="123",
        matched_company_name="Budimex S.A.", confidence=0.92,
    )
    assert is_confident_duplicate(strong)
    assert resolve_company_id(strong) == "123"


def test_weak_duplicate_surfaces_as_review_note():
    from skysnap.sheet_rows import _build_komentarz

    weak = FuzzyDuplicateDecision(
        is_duplicate=True, matched_company_id="123",
        matched_company_name="Budimet Sp. z o.o.", confidence=0.35,
    )
    note = _build_komentarz(
        _lead("Budimex S.A."), enrichment=None, decision=weak, is_duplicate=False
    )
    assert "Możliwy duplikat" in note
    assert "35%" in note
