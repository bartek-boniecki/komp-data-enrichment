"""Tests for HubSpot export mapping and push selection (no live API)."""

import sqlite3
from unittest.mock import patch

from skysnap import db
from skysnap.db import Lead, LeadStatus
from skysnap.hubspot import HubSpotClient
from skysnap.hubspot_export import (
    ASSOC_TASK_TO_COMPANY,
    ASSOC_TASK_TO_CONTACT,
    ASSOC_TASK_TO_DEAL,
    HubSpotFollowUpConfig,
    HubSpotWriteConfig,
    build_company_properties,
    build_contact_properties,
    build_deal_properties,
    build_task_associations,
    build_task_properties,
    deal_name,
    hubspot_followup_config_from_settings,
    parse_polish_address,
    resolve_company_id,
    resolve_existing_deal_id,
    should_create_hubspot_followup,
)
from skysnap.models import EnrichmentResult, FuzzyDuplicateDecision, ProjectSimilarityDecision, WebsiteContact


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    return conn


def _sample_lead(**overrides) -> Lead:
    base = dict(
        id=1,
        source="manual_kompass",
        source_message_id=None,
        source_received_at=None,
        project_name="Hala produkcyjna Radom",
        company_name="Budimex S.A.",
        country="PL",
        city="Radom",
        project_value="35 mln",
        project_phase="Realizacja",
        project_url="https://kompasinwestycji.pl/example-123",
        raw_payload_json={},
        icp_score=75,
        icp_reason="test",
        status=LeadStatus.processed_success,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_error=None,
    )
    base.update(overrides)
    return Lead(**base)


def _write_config() -> HubSpotWriteConfig:
    return HubSpotWriteConfig(
        pipeline_id="pipeline-abc",
        stage_id="stage-xyz",
        sync_company_fields=True,
        company_owner_id="34040248",
        prop_project_url="strona_inwestycji",
        prop_project_name="nazwa_inwestycji",
        prop_icp_score="icp_score",
        prop_leads_origin="leads_orygin",
        prop_stage_inwestycji="etap_inwestycji",
        prop_deal_typ="typ_inwestycji",
        prop_deal_source="deal_source",
        prop_deal_branza="deal_branza_skysnap",
        prop_deal_role="rola_w_projekcie",
        prop_nip="nip",
        prop_opis="opis",
        prop_branza_skysnap="branza",
        prop_branza_extrainfo="branza_skysnap_extrainfo",
        prop_leads_score="leads_score",
        prop_ai_score="ai_score",
        prop_company_notes="komentarz_wewnetrzny",
        prop_uslugi="uslugi_swiadczone",
        prop_voivodeship="voivodship",
        prop_sektor_podsektor="sektor_podsektor",
        prop_project_city="wspol_miasto_budynku",
        prop_project_voivodeship="wspol_wojewodztwo",
        prop_project_street="wspol_ulica_budynku",
        prop_project_building_number="wspol_numer_budynku",
        create_analysis_note=True,
    )


def _followup_config(**overrides) -> HubSpotFollowUpConfig:
    base = dict(
        enabled=True,
        when="always",
        owner_id="12345",
        task_type="CALL",
        due_days=7,
        timezone="Europe/Warsaw",
    )
    base.update(overrides)
    return HubSpotFollowUpConfig(**base)


def test_build_task_properties_subject_owner_and_due():
    props = build_task_properties(
        _sample_lead(),
        None,
        None,
        followup_config=_followup_config(),
    )
    assert props is not None
    assert props["hs_task_subject"].startswith("Skontaktuj się: KI:")
    assert props["hubspot_owner_id"] == "12345"
    assert props["hs_task_type"] == "CALL"
    assert props["hs_task_status"] == "NOT_STARTED"
    assert props["hs_task_priority"] == "HIGH"
    assert props["hs_timestamp"].isdigit()


def test_build_task_properties_none_without_owner():
    assert (
        build_task_properties(
            _sample_lead(),
            None,
            None,
            followup_config=_followup_config(owner_id=None),
        )
        is None
    )


def test_build_task_associations_ids():
    assoc = build_task_associations(
        deal_id="d1",
        company_id="c1",
        contact_id="p1",
    )
    by_type = {
        a["types"][0]["associationTypeId"]: a["to"]["id"] for a in assoc
    }
    assert ASSOC_TASK_TO_COMPANY == 192
    assert ASSOC_TASK_TO_CONTACT == 204
    assert ASSOC_TASK_TO_DEAL == 216
    assert by_type[ASSOC_TASK_TO_DEAL] == "d1"
    assert by_type[ASSOC_TASK_TO_COMPANY] == "c1"
    assert by_type[ASSOC_TASK_TO_CONTACT] == "p1"


def test_create_hubspot_followup_task_company_assoc_192():
    from skysnap.hubspot import create_hubspot_followup_task

    class FakeResp:
        status_code = 201
        ok = True

        def json(self):
            return {"id": "task-9", "properties": {"hs_task_subject": "Follow up"}}

    with patch("skysnap.hubspot.requests.Session") as session_cls:
        session = session_cls.return_value
        session.post.return_value = FakeResp()
        out = create_hubspot_followup_task(
            access_token="pat-test",
            owner_id="34040248",
            record_id="co-1",
            record_type="company",
            task_details={
                "subject": "Follow up regarding new tier pricing",
                "body": "Call the lead",
                "priority": "HIGH",
                "due_date": "2030-01-15",
            },
        )
    assert out["id"] == "task-9"
    payload = session.post.call_args.kwargs["json"]
    assert payload["properties"]["hubspot_owner_id"] == "34040248"
    assert payload["properties"]["hs_task_subject"].startswith("Follow up")
    assert payload["properties"]["hs_timestamp"].isdigit()
    assert payload["associations"][0]["to"]["id"] == "co-1"
    assert payload["associations"][0]["types"][0]["associationTypeId"] == 192


def test_create_hubspot_followup_task_contact_assoc_204():
    from skysnap.hubspot import create_hubspot_followup_task

    class FakeResp:
        status_code = 201
        ok = True

        def json(self):
            return {"id": "task-8"}

    with patch("skysnap.hubspot.requests.Session") as session_cls:
        session = session_cls.return_value
        session.post.return_value = FakeResp()
        create_hubspot_followup_task(
            access_token="pat-test",
            owner_id="1",
            record_id="12345",
            record_type="contact",
            task_details={"subject": "Call", "due_date": "2030-01-15"},
        )
    payload = session.post.call_args.kwargs["json"]
    assert payload["associations"][0]["types"][0]["associationTypeId"] == 204


def test_create_hubspot_followup_task_401():
    from skysnap.hubspot import HubSpotWriteError, create_hubspot_followup_task

    class FakeResp:
        status_code = 401
        ok = False
        text = "unauthorized"

        def json(self):
            return {"message": "invalid token"}

    with patch("skysnap.hubspot.requests.Session") as session_cls:
        session_cls.return_value.post.return_value = FakeResp()
        try:
            create_hubspot_followup_task(
                access_token="bad",
                owner_id="1",
                record_id="1",
                record_type="company",
                task_details={"subject": "X", "due_date": "2030-01-15"},
            )
            assert False, "expected HubSpotWriteError"
        except HubSpotWriteError as e:
            assert e.status_code == 401
            assert "401" in str(e)


def test_create_hubspot_followup_task_400():
    from skysnap.hubspot import HubSpotWriteError, create_hubspot_followup_task

    class FakeResp:
        status_code = 400
        ok = False
        text = "bad request"

        def json(self):
            return {"message": "Property values were not valid"}

    with patch("skysnap.hubspot.requests.Session") as session_cls:
        session_cls.return_value.post.return_value = FakeResp()
        try:
            create_hubspot_followup_task(
                access_token="pat",
                owner_id="1",
                record_id="1",
                record_type="company",
                task_details={"subject": "X", "due_date": "2030-01-15"},
            )
            assert False, "expected HubSpotWriteError"
        except HubSpotWriteError as e:
            assert e.status_code == 400
            assert "400" in str(e)


def test_should_create_hubspot_followup_always_vs_personal():
    cfg = _followup_config(when="always")
    assert should_create_hubspot_followup(followup_config=cfg, created_contact=False)
    personal = _followup_config(when="personal_contact")
    assert not should_create_hubspot_followup(followup_config=personal, created_contact=False)
    assert should_create_hubspot_followup(followup_config=personal, created_contact=True)


def test_hubspot_followup_config_from_settings_defaults_due_7():
    class S:
        hubspot_create_task = True
        hubspot_task_when = "always"
        hubspot_task_owner_id = "99"
        hubspot_task_type = "EMAIL"
        hubspot_task_due_days = 7
        timezone = "Europe/Warsaw"

    cfg = hubspot_followup_config_from_settings(S())
    assert cfg.due_days == 7
    assert cfg.task_type == "EMAIL"
    assert cfg.owner_id == "99"


@patch.object(HubSpotClient, "create_company", return_value="co-1")
@patch.object(HubSpotClient, "create_deal", return_value="deal-1")
@patch.object(HubSpotClient, "create_task", return_value="task-1")
@patch.object(HubSpotClient, "associate_default")
def test_push_lead_export_creates_task_with_associations(
    _assoc, create_task, _create_deal, _create_company
):
    client = HubSpotClient(token="test-token")
    result = client.push_lead_export(
        _sample_lead(),
        EnrichmentResult(source="kompass", company_name="Budimex S.A."),
        None,
        write_config=_write_config(),
        followup_config=_followup_config(),
        dry_run=False,
    )
    assert result.created_task is True
    assert result.task_id == "task-1"
    create_task.assert_called_once()
    _kwargs = create_task.call_args
    associations = _kwargs.kwargs.get("associations") or _kwargs[1].get("associations")
    if associations is None and len(_kwargs[0]) > 1:
        associations = None
    # associations passed as keyword
    associations = create_task.call_args.kwargs["associations"]
    type_ids = {a["types"][0]["associationTypeId"] for a in associations}
    assert ASSOC_TASK_TO_DEAL in type_ids
    assert ASSOC_TASK_TO_COMPANY in type_ids
    assert ASSOC_TASK_TO_CONTACT not in type_ids  # no contact created


@patch.object(HubSpotClient, "create_company", return_value="co-1")
@patch.object(HubSpotClient, "create_deal", return_value="deal-1")
@patch.object(HubSpotClient, "create_task")
@patch.object(HubSpotClient, "associate_default")
def test_push_lead_export_skips_task_without_owner(
    _assoc, create_task, _create_deal, _create_company
):
    client = HubSpotClient(token="test-token")
    result = client.push_lead_export(
        _sample_lead(),
        None,
        None,
        write_config=_write_config(),
        followup_config=_followup_config(owner_id=None),
        dry_run=False,
    )
    assert result.created_task is False
    assert result.task_skipped_reason == "HUBSPOT_TASK_OWNER_ID not set"
    create_task.assert_not_called()


def test_deal_name_matches_sheet_format():
    assert deal_name(_sample_lead(), None).startswith("KI: Budimex")


def test_build_company_properties_includes_domain_and_nip():
    enrichment = EnrichmentResult(
        source="kompass",
        website="https://budimex.pl",
        company_nip="5261003187",
        project_description="Hala produkcyjna w Radomiu — opis z Kompass.",
        sheet_branza="Generalni wykonawcy",
        company_address="ul. Słoneczna 1, 26-600 Radom, mazowieckie",
        contact=WebsiteContact(
            full_name="Jan Kowalski",
            email="jan@budimex.pl",
            linkedin_url="https://www.linkedin.com/company/budimex",
            confidence=0.8,
        ),
    )
    props = build_company_properties(
        _sample_lead(),
        enrichment,
        write_config=_write_config(),
    )
    assert props["name"] == "Budimex S.A."
    assert props["domain"] == "budimex.pl"
    assert props["website"] == "https://budimex.pl"
    assert props["nip"] == "5261003187"
    assert "Hala produkcyjna" in props["opis"]
    assert props["branza"] == "Generalni wykonawcy"
    assert props["leads_score"] == "P2"
    assert props["linkedin_company_page"] == "https://www.linkedin.com/company/budimex"
    assert props["city"] == "Radom"
    assert props["zip"] == "26-600"
    assert "komentarz_wewnetrzny" in props
    assert "SkySnap lead_id=1" in props["komentarz_wewnetrzny"]
    assert props["hubspot_owner_id"] == "34040248"


def test_build_company_properties_never_uses_project_title_as_firm_name():
    props = build_company_properties(
        _sample_lead(company_name=None, project_name="Budynek wielofunkcyjny, ul. Klaudyny"),
        EnrichmentResult(source="osint", notes="No OSINT search results for query: ..."),
        write_config=_write_config(),
    )
    assert props["name"] != "Budynek wielofunkcyjny, ul. Klaudyny"
    assert "Nieznana firma" in props["name"]
    assert "opis" not in props  # OSINT failure notes must not become company opis


def test_build_company_properties_uses_dropdown_safe_values():
    """leads_score/leads_orygin are HubSpot dropdowns, not free text."""
    props = build_company_properties(
        _sample_lead(icp_score=72),
        EnrichmentResult(source="kompass"),
        write_config=_write_config(),
    )
    assert props["leads_score"] == "P2"
    assert props["leads_orygin"] == "Kompas Inwestycji"


def test_leads_score_bucket_thresholds():
    from skysnap.hubspot_export import leads_score_bucket

    assert leads_score_bucket(95) == "P1"
    assert leads_score_bucket(70) == "P2"
    assert leads_score_bucket(55) == "P3"
    assert leads_score_bucket(20) == "P4"


def test_leads_origin_label_maps_sources():
    from skysnap.hubspot_export import leads_origin_label

    assert leads_origin_label("kompass_email") == "Kompas Inwestycji"
    assert leads_origin_label("manual_kompass") == "Kompas Inwestycji"
    assert leads_origin_label("osint") == "Strona www inwestycji"
    assert leads_origin_label("something-else") == "Leads Research"


def test_investment_type_label_public_vs_private():
    from skysnap.hubspot_export import investment_type_label

    public = _sample_lead(company_name="Urząd Miasta i Gminy Starozreby")
    assert investment_type_label(public, None) == "publiczne"
    private = _sample_lead(company_name="Budimex Sp. z o.o.", project_name="Hala")
    assert investment_type_label(private, None) == "prywatne"


def test_investment_type_prefers_kompass_typ_over_company_name():
    from skysnap.hubspot_export import investment_type_label

    # Company looks private (Sp. z o.o.) but Kompass Typ is Publiczna.
    lead = _sample_lead(company_name="Amex Sp.z o.o.")
    enrichment = EnrichmentResult(source="kompass", investment_type="Publiczna")
    assert investment_type_label(lead, enrichment) == "publiczne"


def test_build_deal_opis_is_project_description_not_analysis():
    enrichment = EnrichmentResult(
        source="kompass",
        project_description="Budowa magazynu zasobów OL i OC w Miejscu Piastowym.",
        notes="Agent analysis that must NOT go into Opis.",
        investment_type="Publiczna",
        sector_subsector="niemieszkaniowy - budynki magazynowe, centra logistyczne",
        project_city="Miejsce Piastowe",
        project_voivodeship="podkarpackie",
        project_street="ul. Jaćmierz",
        sheet_branza="Generalni wykonawcy",
        sheet_role="Kierownik projektu",
        project_phase="Generalny Wykonawca wybrany",
    )
    props = build_deal_properties(
        _sample_lead(project_name="Magazyn Zasobów OL i OC, ul. Jaćmierz"),
        enrichment,
        None,
        write_config=_write_config(),
    )
    assert props["description"] == "Budowa magazynu zasobów OL i OC w Miejscu Piastowym."
    assert "Agent analysis" not in props["description"]
    assert props["typ_inwestycji"] == "publiczne"
    assert props["sektor_podsektor"] == "budynki magazynowe, centra logistyczne"
    assert props["wspol_miasto_budynku"] == "Miejsce Piastowe"
    assert props["wspol_wojewodztwo"] == "Podkarpackie"
    assert props["wspol_ulica_budynku"] == "ul. Jaćmierz"


def test_build_analysis_note_body_contains_agent_notes():
    from skysnap.hubspot_export import build_analysis_note_body

    enrichment = EnrichmentResult(
        source="kompass",
        project_description="Project only",
        notes="Full agent analysis here.",
    )
    body = build_analysis_note_body(_sample_lead(), enrichment, None)
    assert body is not None
    assert "Full agent analysis here." in body
    assert "Project only" not in body or "Enrichment" in body


def test_build_deal_properties_custom_investment_fields():
    enrichment = EnrichmentResult(
        source="kompass",
        sheet_branza="Generalni wykonawcy",
        sheet_role="Kierownik budowy",
        project_phase="Realizacja",
        project_description="Opis projektu z Kompass.",
    )
    props = build_deal_properties(
        _sample_lead(),
        enrichment,
        None,
        write_config=_write_config(),
    )
    assert props["strona_inwestycji"] == "https://kompasinwestycji.pl/example-123"
    assert props["nazwa_inwestycji"] == "Hala produkcyjna Radom"
    assert props["etap_inwestycji"] == "Realizacja"
    assert props["deal_branza_skysnap"] == "Generalni wykonawcy"
    assert props["rola_w_projekcie"] == "Kierownik budowy"
    assert props["ai_score"] == "75 — test"
    assert props["deal_source"] == "Kompas Inwestycji"
    assert props["description"] == "Opis projektu z Kompass."
    # Typ inwestycji is public/private, never the branża taxonomy
    assert props.get("typ_inwestycji") != "Generalni wykonawcy"


def test_parse_polish_address():
    parsed = parse_polish_address("ul. Test 1, 00-001 Warszawa, mazowieckie")
    assert parsed["zip"] == "00-001"
    assert parsed["city"] == "Warszawa"
    assert parsed["voivodeship"] == "Mazowieckie"


def test_build_contact_skipped_for_generic_email_only():
    enrichment = EnrichmentResult(
        source="kompass",
        company_generic_email="biuro@firma.pl",
    )
    assert build_contact_properties(_sample_lead(), enrichment) is None


def test_build_contact_with_personal_email():
    enrichment = EnrichmentResult(
        source="kompass",
        contact=WebsiteContact(
            full_name="Jan Kowalski",
            email="jan.kowalski@budimex.pl",
            phone="+48123456789",
            confidence=0.8,
        ),
        sheet_role="Kierownik projektu",
    )
    props = build_contact_properties(_sample_lead(), enrichment)
    assert props is not None
    assert props["email"] == "jan.kowalski@budimex.pl"


def test_build_deal_properties_pipeline_stage():
    props = build_deal_properties(
        _sample_lead(),
        None,
        None,
        write_config=_write_config(),
    )
    assert props["pipeline"] == "pipeline-abc"
    assert props["dealstage"] == "stage-xyz"


def test_resolve_existing_deal_id_same_project():
    sim = ProjectSimilarityDecision(
        similarity_pct=95,
        match_class="same_project",
        matched_deal_id="deal-99",
        matched_deal_name="KI: Budimex, Hala",
        confidence=0.9,
    )
    assert resolve_existing_deal_id(sim) == "deal-99"


def test_resolve_existing_deal_id_addon_requires_min_score():
    sim = ProjectSimilarityDecision(
        similarity_pct=55,
        match_class="addon",
        matched_deal_id="deal-1",
        matched_deal_name="x",
        confidence=0.7,
    )
    assert resolve_existing_deal_id(sim, min_score=60) is None
    sim_high = sim.model_copy(update={"similarity_pct": 72})
    assert resolve_existing_deal_id(sim_high, min_score=60) == "deal-1"


def test_resolve_existing_deal_id_different_lot_creates_new():
    sim = ProjectSimilarityDecision(
        similarity_pct=30,
        match_class="different_lot",
        matched_deal_id="deal-2",
        matched_deal_name="x",
        confidence=0.8,
    )
    assert resolve_existing_deal_id(sim) is None


@patch.object(HubSpotClient, "create_company", return_value="co-1")
@patch.object(HubSpotClient, "update_deal")
@patch.object(HubSpotClient, "create_deal")
@patch.object(HubSpotClient, "create_task")
@patch.object(HubSpotClient, "associate_default")
def test_push_lead_export_updates_existing_deal_on_same_project(
    _assoc, create_task, create_deal, update_deal, _create_company
):
    client = HubSpotClient(token="test-token")
    similarity = ProjectSimilarityDecision(
        similarity_pct=98,
        match_class="same_project",
        matched_deal_id="existing-deal-42",
        matched_deal_name="KI: Budimex, Hala",
        confidence=0.95,
    )
    result = client.push_lead_export(
        _sample_lead(),
        EnrichmentResult(source="kompass", company_name="Budimex S.A."),
        None,
        write_config=_write_config(),
        followup_config=_followup_config(owner_id=None),
        project_similarity=similarity,
        dry_run=False,
    )
    assert result.updated_deal is True
    assert result.created_deal is False
    assert result.deal_id == "existing-deal-42"
    update_deal.assert_called_once()
    create_deal.assert_not_called()
    props = update_deal.call_args[0][1]
    # Opis is project description only; this enrichment has none, so description omitted.
    assert "description" not in props or not props["description"].startswith("[SkySnap update]")
    assert "pipeline" not in props
    create_task.assert_not_called()


def test_create_company_retries_without_invalid_custom_properties():
    from skysnap.hubspot import HubSpotClient, HubSpotWriteError

    class FakeResp:
        def __init__(self, status_code: int, payload: dict, text: str = ""):
            self.status_code = status_code
            self.ok = status_code < 400
            self.text = text
            self._payload = payload

        def json(self):
            return self._payload

    bad = FakeResp(
        400,
        {
            "message": "Property values were not valid",
            "errors": [
                {
                    "message": "Property does not exist",
                    "context": {"propertyName": ["notes"]},
                }
            ],
        },
    )
    ok = FakeResp(200, {"id": "co-99"})

    client = HubSpotClient(token="test-token")
    with patch.object(client, "_post", side_effect=[bad, ok]) as post:
        company_id = client.create_company(
            {"name": "Test Co", "notes": "meta", "domain": "test.pl"}
        )
    assert company_id == "co-99"
    assert post.call_count == 2
    second_props = post.call_args_list[1].kwargs["json"]["properties"]
    assert "notes" not in second_props
    assert second_props["name"] == "Test Co"


def test_create_company_400_raises_hubspot_write_error_with_detail():
    from skysnap.hubspot import HubSpotClient, HubSpotWriteError

    class FakeResp:
        status_code = 400
        ok = False
        text = "bad"

        def json(self):
            return {"message": "Property values were not valid", "errors": []}

    client = HubSpotClient(token="test-token")
    with patch.object(client, "_post", return_value=FakeResp()):
        try:
            client.create_company({"name": "Only Name"})
            assert False, "expected HubSpotWriteError"
        except HubSpotWriteError as e:
            assert e.status_code == 400
            assert "400" in str(e)


@patch.object(HubSpotClient, "update_company")
@patch.object(HubSpotClient, "update_deal")
@patch.object(HubSpotClient, "create_company")
@patch.object(HubSpotClient, "create_deal")
def test_push_lead_export_resync_updates_existing_records(
    create_deal, create_company, update_deal, update_company
):
    client = HubSpotClient(token="test-token")
    result = client.push_lead_export(
        _sample_lead(),
        EnrichmentResult(source="kompass", company_name="Budimex S.A."),
        None,
        write_config=_write_config(),
        resync_company_id="co-existing",
        resync_deal_id="deal-existing",
        dry_run=False,
    )
    assert result.updated_company is True
    assert result.updated_deal is True
    assert result.created_deal is False
    assert result.deal_id == "deal-existing"
    update_company.assert_called_once()
    update_deal.assert_called_once()
    create_company.assert_not_called()
    create_deal.assert_not_called()


@patch.object(HubSpotClient, "update_company")
@patch.object(HubSpotClient, "update_deal")
@patch.object(HubSpotClient, "create_task", return_value="task-resync")
@patch.object(HubSpotClient, "create_company")
@patch.object(HubSpotClient, "create_deal")
def test_push_lead_export_resync_creates_task_when_missing(
    create_deal, create_company, create_task, update_deal, update_company
):
    """Adopt/resync must still create a follow-up when the deal never got a SkySnap task."""
    client = HubSpotClient(token="test-token")
    result = client.push_lead_export(
        _sample_lead(),
        EnrichmentResult(source="kompass", company_name="Budimex S.A."),
        None,
        write_config=_write_config(),
        followup_config=_followup_config(),
        resync_company_id="co-existing",
        resync_deal_id="deal-existing",
        previous_task_id=None,
        dry_run=False,
    )
    assert result.created_task is True
    assert result.task_id == "task-resync"
    create_task.assert_called_once()
    create_company.assert_not_called()
    create_deal.assert_not_called()
    associations = create_task.call_args.kwargs["associations"]
    type_ids = {a["types"][0]["associationTypeId"] for a in associations}
    assert ASSOC_TASK_TO_DEAL in type_ids
    assert ASSOC_TASK_TO_COMPANY in type_ids


@patch.object(HubSpotClient, "update_company")
@patch.object(HubSpotClient, "update_deal")
@patch.object(HubSpotClient, "create_task")
@patch.object(HubSpotClient, "create_company")
@patch.object(HubSpotClient, "create_deal")
def test_push_lead_export_resync_skips_task_when_already_linked(
    create_deal, create_company, create_task, update_deal, update_company
):
    client = HubSpotClient(token="test-token")
    result = client.push_lead_export(
        _sample_lead(),
        EnrichmentResult(source="kompass", company_name="Budimex S.A."),
        None,
        write_config=_write_config(),
        followup_config=_followup_config(),
        resync_company_id="co-existing",
        resync_deal_id="deal-existing",
        previous_task_id="task-already",
        dry_run=False,
    )
    assert result.created_task is False
    assert result.task_id is None
    create_task.assert_not_called()


def test_resolve_company_id_from_duplicate_decision():
    decision = FuzzyDuplicateDecision(
        is_duplicate=True,
        matched_company_id="999",
        matched_company_name="Existing Co",
        confidence=0.9,
    )
    assert resolve_company_id(decision) == "999"


def test_save_and_pending_hubspot_sync():
    conn = _mem_conn()
    now = db.utc_now_iso()
    conn.execute(
        """
        INSERT INTO leads (
            source, project_name, raw_payload_json, icp_score, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("manual_kompass", "Proj", "{}", 60, LeadStatus.processed_success.value, now, now),
    )
    conn.commit()
    lead_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    enrichment = EnrichmentResult(source="kompass", company_name="Firma X")
    db.save_lead_export(conn, lead_id, enrichment=enrichment, decision=None)
    pending = db.iter_leads_pending_hubspot_sync(conn)
    assert lead_id in pending
    db.mark_hubspot_synced(
        conn, lead_id, deal_id="d1", company_id="c1", contact_id=None, task_id="t1"
    )
    row = db.get_lead_export(conn, lead_id)
    assert row is not None
    assert row.hubspot_task_id == "t1"
    assert lead_id not in db.iter_leads_pending_hubspot_sync(conn)


def test_push_hubspot_leads_skipped_when_disabled():
    from skysnap.engine import push_hubspot_leads

    class FakeSettings:
        hubspot_push_enabled = False
        hubspot_private_app_token = "token"
        hubspot_deal_pipeline_id = "p"
        hubspot_deal_stage_id = "s"
        db_path = ":memory:"

    res = push_hubspot_leads(FakeSettings())
    assert res["skipped"] is True
