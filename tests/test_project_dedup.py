"""Tests for project-level HubSpot deal similarity."""

from skysnap.models import HubSpotDealCandidate, ProjectSimilarityDecision
from skysnap.project_dedup import (
    apply_lot_rules,
    deterministic_similarity,
    extract_lot_differentiator,
    format_deal_similarity_cell,
    identity_from_lead,
    merge_similarity_decision,
    normalize_project_identity,
    score_deal_candidate,
)
from skysnap.db import Lead, LeadStatus
from skysnap.sheet_rows import build_row_for_headers, cell_for_header


def test_lead_from_sheet_columns():
    from skysnap.engine import _lead_from_sheet_columns

    headers = ["Nazwa Inwestycji", "Company Name", "Orygin link"]
    column_texts = [
        ["Nazwa Inwestycji", "Projekt Alpha", ""],
        ["Company Name", "Budimex", ""],
        ["Orygin link", "https://kompasinwestycji.pl/alpha-1", ""],
    ]
    lead = _lead_from_sheet_columns(headers, column_texts, row_index=1)
    assert lead is not None
    assert lead.project_name == "Projekt Alpha"
    assert lead.company_name == "Budimex"
    assert "alpha-1" in (lead.project_url or "")


def _lead(**kwargs) -> Lead:
    base = dict(
        id=23,
        source="manual_kompass",
        source_message_id=None,
        source_received_at=None,
        project_name="Osiedle Test Część 1",
        company_name="Budimex S.A.",
        country="PL",
        city="Warszawa",
        project_value="35 mln",
        project_phase="Realizacja",
        project_url="https://www.kompasinwestycji.pl/osiedle-test-czesc-1-110250",
        raw_payload_json={},
        icp_score=75,
        icp_reason="test",
        status=LeadStatus.pending,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_error=None,
    )
    base.update(kwargs)
    return Lead(**base)


def test_extract_lot_differentiator_part_numbers():
    assert extract_lot_differentiator("Budynek wielorodzinny Część 1") == "1"
    assert extract_lot_differentiator("Investment Part 2 ul. Test") == "2"
    assert extract_lot_differentiator("Budynek A") == "a"


def test_part1_vs_part2_capped_low():
    a = normalize_project_identity(project_name="Osiedle X Część 1", city="Kraków")
    b = normalize_project_identity(project_name="Osiedle X Część 2", city="Kraków")
    pre = deterministic_similarity(a, b)
    score, forced = apply_lot_rules(pre, a, b)
    assert forced == "different_lot"
    assert score <= 35


def test_building_a_vs_b_different_lot():
    a = normalize_project_identity(project_name="Kompleks biurowy Budynek A")
    b = normalize_project_identity(project_name="Kompleks biurowy Budynek B")
    pre = deterministic_similarity(a, b)
    score, forced = apply_lot_rules(pre, a, b)
    assert forced == "different_lot"
    assert score <= 35


def test_identical_kompass_url_high_similarity():
    url = "https://www.kompasinwestycji.pl/same-project-110250"
    a = normalize_project_identity(project_name="Projekt A", project_url=url)
    b = normalize_project_identity(project_name="Projekt B renamed", project_url=url)
    pre = deterministic_similarity(a, b)
    score, forced = apply_lot_rules(pre, a, b)
    assert pre >= 90
    assert forced == "same_project"
    assert score >= 90


def test_merge_lot_conflict_overrides_claude():
    deal = HubSpotDealCandidate(id="99", dealname="Deal X Część 2")
    claude = ProjectSimilarityDecision(
        similarity_pct=92,
        match_class="same_project",
        matched_deal_id="99",
        matched_deal_name="Deal X Część 2",
        confidence=0.9,
        reasoning="looks similar",
    )
    merged = merge_similarity_decision(
        pre_score=30,
        forced_class="different_lot",
        claude_decision=claude,
        best_deal=deal,
    )
    assert merged.match_class == "different_lot"
    assert merged.similarity_pct <= 35


def test_score_deal_candidate_from_lead():
    lead = _lead()
    deal = HubSpotDealCandidate(
        id="1",
        dealname="KI: Budimex, Osiedle Test Część 2",
        project_url=lead.project_url,
    )
    scored = score_deal_candidate(identity_from_lead(lead), deal)
    assert scored.forced_class == "different_lot"
    assert scored.pre_score <= 35


def test_format_deal_similarity_cell():
    decision = ProjectSimilarityDecision(
        similarity_pct=23,
        match_class="different_lot",
        matched_deal_id="1",
        matched_deal_name="KI: Budimex, Hala",
        confidence=0.8,
        reasoning="part 1 vs part 2",
    )
    cell = format_deal_similarity_cell(decision)
    assert cell.startswith("23%")
    assert "different lot" in cell
    assert "Budimex" in cell


def test_format_deal_similarity_cell_zero_shows_label():
    decision = ProjectSimilarityDecision(
        similarity_pct=0,
        match_class="unrelated",
        matched_deal_id=None,
        matched_deal_name=None,
        confidence=0.0,
        reasoning=None,
    )
    cell = format_deal_similarity_cell(decision)
    assert cell == "0% — unrelated"


def test_deal_name_search_tokens_splits_phrase():
    from skysnap.hubspot import _deal_name_search_tokens, _deal_search_filter_groups

    tokens = _deal_name_search_tokens("Hala produkcyjna Radom Budimex")
    assert "hala" in tokens
    assert "produkcyjna" in tokens
    assert "radom" in tokens


def test_deal_name_search_tokens_max_five_for_hubspot():
    from skysnap.hubspot import _deal_name_search_tokens, _deal_search_filter_groups

    query = (
        "Wojewodzki Szpital Specjalistyczny w Lublinie "
        "Centrum Procedur Ambulatoryjnych 111588"
    )
    tokens = _deal_name_search_tokens(query)
    assert len(tokens) <= 5
    assert "111588" in tokens
    groups = _deal_search_filter_groups(tokens, pipeline_id="default")
    assert len(groups) <= 5


def test_sheet_deal_similarity_column():
    lead = _lead()
    decision = ProjectSimilarityDecision(
        similarity_pct=85,
        match_class="same_project",
        matched_deal_id="1",
        matched_deal_name="KI: Budimex, Osiedle",
        confidence=0.9,
        reasoning="same url",
    )
    row = build_row_for_headers(
        ["Nazwa Inwestycji", "Deal Similarity", "komentarz"],
        lead=lead,
        enrichment=None,
        decision=None,
        project_similarity=decision,
    )
    assert "85%" in row[1]
    assert "same project" in row[1]
    komentarz = cell_for_header(
        "komentarz",
        lead=lead,
        enrichment=None,
        decision=None,
        project_similarity=decision,
    )
    assert "Podobieństwo dealu: 85%" in komentarz
