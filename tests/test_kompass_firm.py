"""Tests for Kompass firm profile parsing."""

from skysnap.kompass_firm import parse_firm_profile_from_page

SAMPLE_PROFILE_TEXT = """
Komenda Wojewódzka Policji w Radomiu
Company website
http://www.kwp.radom.pl
Company address :
ul.11 Listopada 37/59,
Radom 26-605

Radom, Masovian Voivodeship
Poland
Tax Identification Number :
7962234609
Company phone number :
(48)3453103,(48)3629191
Company email :
prasowy.kwp@ra.policja.gov.pl
"""


def test_parse_kwp_firm_profile_fields():
    profile = parse_firm_profile_from_page(
        html="<h1>Komenda Wojewódzka Policji w Radomiu</h1>",
        visible_text=SAMPLE_PROFILE_TEXT,
        profile_url="https://www.kompasinwestycji.pl/v2/firma/5751",
        company_name_hint="Komenda Wojewódzka Policji w Radomiu",
    )
    assert profile.company_name == "Komenda Wojewódzka Policji w Radomiu"
    assert profile.website == "http://www.kwp.radom.pl"
    assert "11 Listopada" in (profile.address or "")
    assert profile.nip == "7962234609"
    assert "3453103" in (profile.phones or "")
    assert profile.email == "prasowy.kwp@ra.policja.gov.pl"
