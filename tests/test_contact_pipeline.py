"""Tests for the 3-step contact pipeline: reveal ledger, query building, pattern inference."""

from skysnap import db
from skysnap.contact_search import (
    _build_channel_search_queries,
    _build_nip_queries,
    _build_role_discovery_queries,
)
from skysnap.kompass_firm import parse_firm_profile_from_page
from skysnap.validation import guess_email_patterns, infer_email_pattern


# --- Reveal ledger ---------------------------------------------------------- #


def _mem_conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    return conn


def test_reveal_ledger_counts_today():
    conn = _mem_conn()
    assert db.count_reveals_today(conn) == 0
    db.log_reveal(conn, 1, success=True)
    db.log_reveal(conn, 2, success=False)
    assert db.count_reveals_today(conn) == 2


def test_reveal_ledger_prevents_double_spend():
    conn = _mem_conn()
    assert not db.lead_already_revealed(conn, 7)
    db.log_reveal(conn, 7, success=True)
    assert db.lead_already_revealed(conn, 7)
    assert not db.lead_already_revealed(conn, 8)


def test_reveal_ledger_older_days_do_not_count():
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO contact_reveals (lead_id, revealed_on, success, created_at) "
        "VALUES (1, '2020-01-01', 1, '2020-01-01T10:00:00+00:00')"
    )
    conn.commit()
    assert db.count_reveals_today(conn) == 0
    assert db.lead_already_revealed(conn, 1)


# --- Firm profile reveal flag ----------------------------------------------- #


def test_firm_profile_contact_revealed_flag():
    text = (
        "Budimex S.A.\n"
        "Adres firmy: ul. Siedmiogrodzka 9, Warszawa\n"
        "NIP: 5261003187\n"
        "Telefon firmy: 22 623 60 00\n"
        "E-mail firmy: info@budimex.pl\n"
    )
    profile = parse_firm_profile_from_page(
        html=f"<html><body><h1>Budimex S.A.</h1><p>{text}</p></body></html>",
        visible_text=text,
        profile_url="https://www.kompasinwestycji.pl/v2/firma/1234",
    )
    assert profile.contact_revealed
    assert profile.nip == "5261003187"
    assert profile.email == "info@budimex.pl"


def test_firm_profile_not_revealed_without_channels():
    text = "Budimex S.A.\nProfil firmy w serwisie Kompas Inwestycji\n"
    profile = parse_firm_profile_from_page(
        html="<html><body><h1>Budimex S.A.</h1></body></html>",
        visible_text=text,
        profile_url="https://www.kompasinwestycji.pl/v2/firma/1234",
    )
    assert not profile.contact_revealed


# --- OSINT query building ---------------------------------------------------- #


def test_nip_queries_only_for_valid_nip():
    assert _build_nip_queries(nip=None, company_name="Firma") == []
    assert _build_nip_queries(nip="123", company_name="Firma") == []
    queries = _build_nip_queries(nip="526-100-31-87", company_name="Budimex S.A.")
    assert any("rejestr.io" in q for q in queries)
    assert any("5261003187" in q for q in queries)


def test_role_discovery_queries_include_project_and_nip():
    queries = _build_role_discovery_queries(
        company_name="Konstrukcje Żywiec Sp. z o.o.",
        city="Żywiec",
        project_name="Zaklad Electris Rozbudowa Ii 111653",
        nip="5532511382",
    )
    joined = " | ".join(queries)
    assert "linkedin.com/in" in joined
    assert "5532511382" in joined
    # Kompass numeric id must be stripped from the project hint.
    assert "111653" not in joined
    assert "Zaklad Electris Rozbudowa" in joined


def test_channel_queries_include_pl_linkedin_variant():
    queries = _build_channel_search_queries(
        full_name="Jan Kowalski",
        company_name="Budimex",
        city="Warszawa",
        project_name="Hala produkcyjna Radom",
        nip="5261003187",
    )
    joined = " | ".join(queries)
    assert "site:pl.linkedin.com/in" in joined
    assert '"Jan Kowalski"' in joined
    assert "Hala produkcyjna Radom" in joined


# --- Email pattern inference -------------------------------------------------- #


def test_infer_email_pattern_fi_dot_last():
    pattern = infer_email_pattern(
        ["j.kowalski@firma.pl", "a.nowak@firma.pl", "biuro@firma.pl"],
        "firma.pl",
    )
    assert pattern == "{fi}.{last}"


def test_infer_email_pattern_first_dot_last():
    pattern = infer_email_pattern(
        ["jan.kowalski@firma.pl", "anna.nowak@firma.pl"],
        "firma.pl",
    )
    assert pattern == "{first}.{last}"


def test_infer_email_pattern_ignores_other_domains_and_role():
    assert infer_email_pattern(["jan.kowalski@inna.pl", "biuro@firma.pl"], "firma.pl") is None


def test_guess_email_patterns_prefers_inferred_pattern():
    guesses = guess_email_patterns(
        "Piotr Zieliński", "firma.pl", preferred_pattern="{fi}.{last}"
    )
    assert guesses[0] == "p.zielinski@firma.pl"
    # Without a preferred pattern, first.last leads.
    default = guess_email_patterns("Piotr Zieliński", "firma.pl")
    assert default[0] == "piotr.zielinski@firma.pl"
