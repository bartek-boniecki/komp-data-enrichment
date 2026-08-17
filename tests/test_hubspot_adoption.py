"""An unlinked export must adopt its existing HubSpot deal, never duplicate it."""

from skysnap.db import Lead, LeadStatus
from skysnap.engine import find_existing_hubspot_link
from skysnap.models import EnrichmentResult, HubSpotDealCandidate


def _lead(**overrides) -> Lead:
    base = dict(
        id=51,
        source="manual_kompass",
        source_message_id=None,
        source_received_at=None,
        project_name="Unknown Project 111647",
        company_name=None,
        country="PL",
        city=None,
        project_value=None,
        project_phase=None,
        project_url="https://kompasinwestycji.pl/-111647",
        raw_payload_json={},
        icp_score=40,
        icp_reason="test",
        status=LeadStatus.processed_success,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        last_error=None,
    )
    base.update(overrides)
    return Lead(**base)


class _FakeHubSpot:
    def __init__(
        self,
        deals: list[HubSpotDealCandidate],
        company_ids: list[str],
        *,
        url_deals: list[HubSpotDealCandidate] | None = None,
    ):
        self._deals = deals
        self._company_ids = company_ids
        self._url_deals = url_deals or []
        self.exact_name_queries: list[str] = []
        self.token_searches = 0
        self.url_searches: list[str] = []

    def find_deal_by_exact_name(self, name, *, pipeline_id=None, limit=3):
        self.exact_name_queries.append(name)
        return list(self._deals)

    def find_deals_by_project_url(
        self, project_url, *, prop_project_url, pipeline_id=None, limit=10
    ):
        self.url_searches.append(project_url)
        return list(self._url_deals)

    def search_deal_candidates(self, *, name_query, pipeline_id=None, limit=5, **_kwargs):
        self.token_searches += 1
        return []

    def list_deal_company_ids(self, deal_id):
        return list(self._company_ids)


def test_finds_existing_deal_by_exact_name():
    hubspot = _FakeHubSpot(
        [HubSpotDealCandidate(id="513655974107", dealname="KI: Unknown Project 111647")],
        ["440726073538"],
    )

    link = find_existing_hubspot_link(
        hubspot, _lead(), EnrichmentResult(source="kompass"), pipeline_id="default"
    )

    assert link == ("513655974107", "440726073538")
    assert hubspot.exact_name_queries == ["KI: Unknown Project 111647"]
    assert hubspot.token_searches == 0


def test_uses_company_id_already_on_the_deal():
    hubspot = _FakeHubSpot(
        [
            HubSpotDealCandidate(
                id="513655974107",
                dealname="KI: Unknown Project 111647",
                company_id="440726073538",
            )
        ],
        [],
    )

    link = find_existing_hubspot_link(
        hubspot, _lead(), EnrichmentResult(source="kompass"), pipeline_id="default"
    )

    assert link == ("513655974107", "440726073538")


def test_returns_none_when_no_deal_matches():
    hubspot = _FakeHubSpot([], [])

    link = find_existing_hubspot_link(
        hubspot, _lead(), EnrichmentResult(source="kompass"), pipeline_id="default"
    )

    assert link is None


def test_returns_none_when_deal_has_no_company():
    hubspot = _FakeHubSpot(
        [HubSpotDealCandidate(id="513655974107", dealname="KI: Unknown Project 111647")],
        [],
    )

    link = find_existing_hubspot_link(
        hubspot, _lead(), EnrichmentResult(source="kompass"), pipeline_id="default"
    )

    assert link is None


def test_adopts_existing_deal_by_kompass_project_url():
    """Same Kompass URL must adopt even when dealname differs (different firm prefixes)."""
    url = "https://www.kompasinwestycji.pl/budynek-wielofunkcyjny-ul-klaudyny-107486"
    hubspot = _FakeHubSpot(
        [],
        ["440705520859"],
        url_deals=[
            HubSpotDealCandidate(
                id="513905321162",
                dealname="KI: Fortuna, Budynek wielofunkcyjny, ul. Klaudyny",
                project_url=url,
                company_id="440705520859",
            )
        ],
    )
    lead = _lead(
        project_name="Budynek wielofunkcyjny, ul. Klaudyny",
        company_name=None,
        project_url=url,
    )

    link = find_existing_hubspot_link(
        hubspot,
        lead,
        EnrichmentResult(source="osint"),
        pipeline_id="default",
        prop_project_url="strona_inwestycji",
    )

    assert link == ("513905321162", "440705520859")
    assert hubspot.url_searches == [url]
    assert hubspot.exact_name_queries == []


def test_prefers_firm_prefixed_deal_over_bare_project_duplicate():
    url = "https://www.kompasinwestycji.pl/budynek-wielofunkcyjny-ul-klaudyny-107486"
    hubspot = _FakeHubSpot(
        [],
        [],
        url_deals=[
            HubSpotDealCandidate(
                id="514073601223",
                dealname="KI: Budynek wielofunkcyjny, ul. Klaudyny",
                project_url=url,
                company_id="440968380641",
            ),
            HubSpotDealCandidate(
                id="513905321162",
                dealname="KI: Fortuna, Budynek wielofunkcyjny, ul. Klaudyny",
                project_url=url,
                company_id="440705520859",
            ),
        ],
    )
    lead = _lead(
        project_name="Budynek wielofunkcyjny, ul. Klaudyny",
        company_name=None,
        project_url=url,
    )

    link = find_existing_hubspot_link(
        hubspot,
        lead,
        EnrichmentResult(source="osint"),
        pipeline_id="default",
        prop_project_url="strona_inwestycji",
    )

    assert link == ("513905321162", "440705520859")
