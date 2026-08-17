"""Tests for SkySnap ICP master-list rubric."""

from skysnap.icp import (
    apply_icp_rubric,
    base_icp_reason,
    parse_value_pln_millions,
    refine_icp_from_enrichment,
)
from skysnap.sheet_rows import format_icp_score_cell


def test_wizja_stage_no_value_capped():
    """Wizja (concept) leads must not sit at 98 even without a parsed value."""
    adj = apply_icp_rubric(
        icp_score=98,
        icp_reason="Manual Kompass URL import",
        project_name="Plac Moniuszki Przed Opera Zagospodarowanie",
        project_value=None,
        project_phase="Wizja",
    )
    assert adj.icp_score <= 45
    assert "design_or_preconstruction_phase" in adj.flags


def test_designer_selection_high_value_capped():
    """35 mln but only 'Wybór głównego projektanta' (design) → capped, not 100."""
    adj = apply_icp_rubric(
        icp_score=100,
        icp_reason="Manual Kompass URL import",
        project_name="Teren LKS Gierałtowice Zagospodarowanie",
        project_value="35 mln",
        project_phase="Wybór głównego projektanta",
    )
    assert adj.icp_score <= 55
    assert "design_or_preconstruction_phase" in adj.flags


def test_projektowanie_zakonczone_capped():
    adj = apply_icp_rubric(
        icp_score=98,
        icp_reason="Manual Kompass URL import",
        project_name="Budynek wielorodzinny",
        project_value=None,
        project_phase="Projektowanie zakończone",
    )
    assert adj.icp_score <= 45
    assert "design_or_preconstruction_phase" in adj.flags


def test_gw_selection_tender_not_capped_as_design():
    """GW tender in progress is good timing — not a design cap."""
    adj = apply_icp_rubric(
        icp_score=80,
        icp_reason="",
        project_name="Rozbudowa zakładu przemysłowego",
        project_value="30 mln",
        project_phase="Wybór Generalnego Wykonawcy",
    )
    assert "design_or_preconstruction_phase" not in adj.flags
    assert adj.icp_score >= 75


def test_construction_started_overrides_design_words():
    adj = apply_icp_rubric(
        icp_score=90,
        icp_reason="",
        project_name="Hala produkcyjna",
        project_value="42 mln",
        project_phase="Realizacja - Stan zero",
        extra_text="dokumentacja projektowa gotowa, roboty ziemne w toku",
    )
    assert "design_or_preconstruction_phase" not in adj.flags
    assert adj.icp_score >= 80


def test_refine_from_enrichment_uses_claude_phase():
    """Raw import score 98 + Claude phase 'Wizja' in enrichment → low score."""
    adj = refine_icp_from_enrichment(
        icp_score=98,
        icp_reason="Manual Kompass URL import",
        project_name="Stacja uzdatniania wody",
        project_value=None,
        project_phase=None,
        enrichment_phase="Wizja",
        enrichment_notes="Brak Generalnego Wykonawcy — inwestycja na etapie Wizja.",
    )
    assert adj.icp_score <= 45
    assert "design_or_preconstruction_phase" in adj.flags


def test_parse_value_mln():
    assert parse_value_pln_millions("25 mln PLN") == 25.0
    assert parse_value_pln_millions("15 million zł") == 15.0
    assert parse_value_pln_millions("8 mln") == 8.0


def test_interior_renovation_disqualified():
    adj = apply_icp_rubric(
        icp_score=80,
        icp_reason="test",
        project_name="Modernizacja wnętrz biura",
        project_value="2 mln PLN",
    )
    assert adj.icp_score <= 20
    assert "avoid_scope" in "_".join(adj.flags)


def test_hala_high_value_boosted():
    adj = apply_icp_rubric(
        icp_score=60,
        icp_reason="",
        project_name="Budowa hali magazynowej",
        project_value="22 mln PLN",
        project_phase="Realizacja — roboty ziemne",
    )
    assert adj.icp_score >= 75
    assert "value_ideal_20m_plus" in adj.flags


def test_early_phase_penalized():
    adj = apply_icp_rubric(
        icp_score=70,
        icp_reason="",
        project_name="Osiedle mieszkaniowe",
        project_phase="Projektowanie koncepcyjne, brak GW",
        project_value="30 mln PLN",
    )
    assert adj.icp_score < 70
    assert "too_early_phase" in "_".join(adj.flags)


def test_sub10m_cannot_rebound_to_90s_after_bonuses():
    """Regression: value penalty then +scope must not land at 91-98."""
    adj = apply_icp_rubric(
        icp_score=98,
        icp_reason="infrastruktury drogowe",
        project_name="Przebudowa infrastruktury",
        project_value="8 mln PLN",
        project_phase="Realizacja",
    )
    assert adj.icp_score <= 52
    assert "value_below_10m" in adj.flags
    assert "value_band_ceiling" in adj.flags


def test_value_parsed_from_icp_reason_when_field_empty():
    adj = apply_icp_rubric(
        icp_score=98,
        icp_reason="wartość ok. 7 mln PLN, etap wizja",
        project_name="Inwestycja testowa",
        project_value=None,
        project_phase="Wizja",
    )
    assert adj.icp_score <= 45
    assert "value_below_10m" in adj.flags


def test_tarnogaj_wbo_greening_low_score():
    """Regression: 'realizacji zadania' must not boost; 7 mln Wizja WBO should score low."""
    name = "Zagospodarowanie zielenią terenów na Tarnogaju - zadanie II"
    extra = (
        "w ramach realizacji zadania II w ramach Wrocławskiego Budżetu Obywatelskiego. "
        "Wartość szacunkowa 7 mln. Etap Wizja. "
        "Zapowiedź przetargu na wykonanie dokumentacji projektowej. "
        "Inwestor publiczny Zarząd Zieleni Miejskiej"
    )
    adj = apply_icp_rubric(
        icp_score=100,
        icp_reason="",
        project_name=name,
        project_value="7 mln",
        project_phase="Wizja",
        extra_text=extra,
    )
    assert adj.icp_score <= 45
    assert "good_timing_gw_or_execution" not in adj.flags
    assert "value_below_10m" in adj.flags
    assert "too_early_phase" in "_".join(adj.flags)
    assert "low_priority_scope" in "_".join(adj.flags)


def test_stale_reason_does_not_reinflate_when_project_value_set():
    """Regression: export must not jump back to 100 from stale ~40 mln in icp_reason."""
    stale = (
        "Manual Kompass URL import | wartość ~40 mln PLN (idealna); "
        "timing: GW wybrany / przetarg / start robót"
    )
    adj = refine_icp_from_enrichment(
        icp_score=95,
        icp_reason=stale,
        project_name="Dp1047 Huta Krzeszowska Przebudowa",
        project_value="8.16 mln",
        project_phase="Realizacja",
        enrichment_phase="Generalny Wykonawca wybrany",
        enrichment_notes="Project is a road reconstruction, value ~8.16M PLN, GW selected.",
        source="manual_kompass",
    )
    assert adj.icp_score <= 52
    assert "40 mln" not in (adj.icp_reason or "")


def test_base_icp_reason_strips_rubric_clauses():
    merged = "Manual Kompass URL import | wartość ~40 mln PLN (idealna)"
    assert base_icp_reason(merged) == "Manual Kompass URL import"


def test_format_icp_score_cell():
    lead = type("Lead", (), {"icp_score": 95, "icp_reason": "Manual Kompass URL import | wartość ~8 mln"})()
    cell = format_icp_score_cell(lead)  # type: ignore[arg-type]
    assert cell.startswith("95 —")
    assert "Manual Kompass" in cell
