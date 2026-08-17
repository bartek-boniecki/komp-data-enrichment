"""SkySnap ICP rubric — master qualification criteria (deterministic + Claude prompt)."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

# --- Prompt block for Claude email triage ----------------------------------- #

ICP_EMAIL_SYSTEM_PROMPT = """You are a B2B lead triage engine for SkySnap (drone/geospatial services for construction).
Extract construction projects from Kompass-style email HTML. Output strict JSON only.

Score each project icp_score 1-100 using this rubric (score-first, be strict on disqualifiers):

## 1. Project scope & type (drone must be usable on site)
HIGH score (75-100 band contribution):
- Cubature: industrial halls, warehouses, logistics, hotels, large multi-family residential, major engineering/infrastructure.
- Large commercial or public buildings where exterior/aerial survey applies.

LOW score / cap below 35 when dominant signal:
- Small single-family residential plots, individual houses, garage additions.
- Strictly interior renovations or modernizations only (fit-out, interior finishing) where drones cannot be used on site.
- Pure design/concept announcements with no construction site.

## 2. Project value (PLN)
- Ideal: above 20 million PLN → strong positive.
- Viable band: 10-17 million PLN → moderate positive (case-by-case).
- Below 10 million PLN → strong negative unless clearly cubature/engineering at scale.
Parse project_value text; if value unknown, do not assume high value.

## 3. Project status & timing
HIGH when:
- Generalny Wykonawca (GW) already chosen / named on the project, OR
- Public tender: offers opened / contractor selection in progress (przetarg, oferty otwarte, wybór GW).
- Very beginning of physical execution: site entry, earthworks, roboty ziemne, rozpoczęcie budowy.

LOW when:
- Early design only, no GW, no tender activity, or vague future announcement.

## 4. Stakeholder fit (for icp_reason only at ingest; contacts come later)
Primary targets: GW bidding department (dział ofertowania) and investment preparation (przygotowanie inwestycji).
Secondary: private investor, projektant — note in icp_reason when relevant.

Set project_phase to the Kompass investment stage when visible (e.g. Realizacja, Wybór GW, Projektowanie).
Keep icp_reason to one concise sentence citing scope, value band, and timing.
Always set raw to {}. JSON must be complete — never truncate mid-object.
"""

# --- Deterministic patterns ------------------------------------------------- #

_AVOID_SCOPE_RE = re.compile(
    r"modernizacj[ai]\s+wnętrz|modernizacj[ai]\s+wnetrz|remont\s+wnętrz|wykończenie\s+wnętrz|"
    r"fit-?out|tylko\s+wnętrza|jednorodzinny\s+dom|bliźniak|szeregówk|"
    r"mała\s+działk|mieszkanie\s+na\s+sprzedaż|adaptacja\s+lokalu\s+mieszkal",
    re.I,
)

_TARGET_SCOPE_RE = re.compile(
    r"\bhala\b|magazyn|logistyk|centrum\s+dystrybucj|hotel|kubatur|"
    r"osiedle|wielorodzinn|biurowiec|fabryk|zakład\s+produkcyj|infrastruktur|"
    r"most\b|droga\s+szybkiego|lotnisk|szpital|centrum\s+handlow|galeria\s+handlow|"
    r"elektrowni|oczyszczalni|stoczni|port\b|kolej\b|metro\b|tunel\b|"
    r"przebudow[ay]\s+(hali|obiektu|zakładu)|budow[ay]\s+hali",
    re.I,
)

# Municipal greening / WBO / small public realm — weak SkySnap fit vs cubature GW work.
_LOW_PRIORITY_SCOPE_RE = re.compile(
    r"zagospodarowan\w*\s+zieleni|budżet\s+obywatelsk|budzet\s+obywatelsk|"
    r"rewitalizacj\w*\s+przestrzeni\s+miejsk|\bWBO\b",
    re.I,
)

_GOOD_PHASE_RE = re.compile(
    r"etap\s+realizacji|w\s+trakcie\s+realizacji|"
    r"realizacj[ai]\s+(?:budow|inwestycj|prac|robót|robot|obiektu)\b|"
    r"rozpoczęci[ae]\s+budow|rozpoczecie\s+budow|roboty\s+ziemn|"
    r"wejście\s+na\s+budow|wejscie\s+na\s+budow|wybór\s+generalnego|wybor\s+generalnego|"
    r"wybrano\s+gw|generalny\s+wykonawca\s+(wybran|wyłonion)|"
    r"oferty\s+otwart|przetarg\s+(?:na\s+)?(?:wybór|wybor|wykonawc|rozstrzygn|zakończ)|"
    r"zakończon[ay]\s+przetarg|"
    r"w\s+trakcie\s+budowy",
    re.I,
)

_EARLY_PHASE_RE = re.compile(
    r"\bwizja\b|projektowanie\s+koncept|koncepcj[ay]|przedprojekt|bez\s+generalnego\s+wykonawcy|"
    r"brak\s+gw\b|poszukiwanie\s+wykonawcy|planowana\s+inwestycj|zamiar\s+budowy|"
    r"projekt\s+budowlany\s+bez\s+realizacji|"
    r"dokumentacj[ai]\s+projektow|zapowiedź\s+przetargu|zapowiedz\s+przetargu|"
    r"przetargu\s+na\s+wykonanie\s+dokumentacji",
    re.I,
)

_GW_ABSENT_RE = re.compile(
    r"brak\s+gw\b|bez\s+gw\b|brak\s+generalnego\s+wykonawcy|bez\s+generalnego\s+wykonawcy",
    re.I,
)

_GW_PRESENT_RE = re.compile(
    r"generalny\s+wykonawca|generalnego\s+wykonawcy|wykonawca\s+wybran|wybrano\s+gw|"
    r"wybór\s+generalnego|wybor\s+generalnego",
    re.I,
)

# Design / pre-construction Kompass stages — no GW, no site work yet → weak fit.
# NOTE: "wybór generalnego wykonawcy" (GW tender in progress) is intentionally NOT
# here — that is good timing and stays in _GOOD_PHASE_RE.
_DESIGN_PHASE_RE = re.compile(
    r"\bwizja\b|projektowani|koncepcj|przedprojekt|studium\s+wykonalno|"
    r"wybór\s+(?:głównego\s+)?projektant|wybor\s+(?:głównego\s+|glownego\s+)?projektant|"
    r"\bpfu\b|program\s+funkcjonalno|dokumentacj[ai]\s+projektow|"
    r"decyzj[aęi]\s+środowiskow|decyzj[aęi]\s+srodowiskow|"
    r"wniosek\s+o\s+pozwolenie|oczekuje\s+na\s+(?:weryfikacj|zatwierdz)",
    re.I,
)

# Construction genuinely started or GW chosen — overrides the design cap.
_CONSTRUCTION_STARTED_RE = re.compile(
    r"stan\s+zero|stan\s+surow|roboty\s+ziemn|w\s+trakcie\s+budowy|"
    r"rozpoczęci[ae]\s+budow|rozpoczecie\s+budow|"
    r"generalny\s+wykonawca\s+(?:wybran|wyłonion|wylonion)|wybrano\s+gw|\bgw\s+wybran",
    re.I,
)

_VALUE_PLN_RE = re.compile(
    r"(\d[\d\s.,]*)\s*(mln|milion(?:y|ów)?|million|mld|tys\.?|tysiąc|tysiac|pln|zł|zl)\b",
    re.I,
)


@dataclass(frozen=True)
class IcpAdjustment:
    icp_score: int
    icp_reason: str
    flags: tuple[str, ...] = ()
    project_phase: str | None = None


def parse_value_pln_millions(text: str | None) -> float | None:
    """Best-effort parse of project value to millions PLN."""
    if not text or not text.strip():
        return None
    hay = text.replace("\u00a0", " ").strip().lower()
    best: float | None = None
    for match in _VALUE_PLN_RE.finditer(hay):
        raw_num = match.group(1).replace(" ", "").replace(",", ".")
        try:
            num = float(raw_num)
        except ValueError:
            continue
        unit = match.group(2).lower()
        if unit in ("mld",):
            millions = num * 1000.0
        elif unit in ("mln", "milion", "million") or unit.startswith("milion"):
            millions = num
        elif unit.startswith("tys"):
            millions = num / 1000.0
        elif unit in ("pln", "zł", "zl"):
            millions = num / 1_000_000.0
        else:
            millions = num
        if best is None or millions > best:
            best = millions
    # Bare "15000000" / "15 000 000" style — ONLY when the whole input is an
    # amount. Never applied to prose: concatenating every digit in a Kompass
    # page (dates, postal codes, plot numbers, NIP) previously fabricated
    # astronomical "values" that inflated ICP and were stored as project_value.
    if best is None:
        compact = re.sub(r"[\s.,\u00a0]", "", hay)
        if compact.isdigit() and len(compact) >= 8:
            best = float(compact) / 1_000_000.0
    return best


_KNOWN_ICP_SOURCES = (
    "Manual Kompass URL import",
    "Kompass Email",
)


def base_icp_reason(icp_reason: str | None) -> str:
    """Source label only — strip prior rubric clauses so re-scoring stays idempotent."""
    if not icp_reason or not icp_reason.strip():
        return ""
    text = icp_reason.strip().split(" | ", 1)[0].strip()
    for prefix in _KNOWN_ICP_SOURCES:
        if text.startswith(prefix):
            return prefix
    if ";" in text:
        return text.split(";", 1)[0].strip()
    return text


def coalesce_project_value(
    *,
    project_value: str | None,
    project_name: str | None = None,
    project_phase: str | None = None,
    icp_reason: str | None = None,
    extra_text: str | None = None,
) -> str | None:
    """Best project_value for rubric — structured field first, then parse from prose."""
    if project_value and str(project_value).strip():
        return str(project_value).strip()
    base = base_icp_reason(icp_reason)
    hay = " ".join(
        p
        for p in (project_name, project_phase, extra_text, base)
        if p and str(p).strip()
    )
    return extract_project_value_from_text(hay)


def _value_band_ceiling(value_mln: float | None, hay: str) -> int | None:
    """Hard max ICP after bonuses — sub-10M must not creep back to ~90s."""
    if value_mln is None:
        return None
    if value_mln >= 20:
        return None
    if value_mln >= 10:
        return 72
    if _LOW_PRIORITY_SCOPE_RE.search(hay):
        return 38
    if _EARLY_PHASE_RE.search(hay):
        return 40
    if _TARGET_SCOPE_RE.search(hay):
        return 52
    return 45


def apply_icp_rubric(
    *,
    icp_score: int,
    icp_reason: str | None,
    project_name: str | None,
    project_value: str | None = None,
    project_phase: str | None = None,
    extra_text: str | None = None,
) -> IcpAdjustment:
    """Blend Claude score with deterministic master-list rules."""
    score = max(1, min(100, int(icp_score)))
    reasons: list[str] = []
    flags: list[str] = []
    source_reason = base_icp_reason(icp_reason)
    resolved_value = coalesce_project_value(
        project_value=project_value,
        project_name=project_name,
        project_phase=project_phase,
        icp_reason=icp_reason,
        extra_text=extra_text,
    )
    hay = " ".join(
        p
        for p in (project_name, resolved_value, project_phase, source_reason, extra_text)
        if p and str(p).strip()
    )

    if _AVOID_SCOPE_RE.search(hay):
        score = min(score, 20)
        flags.append("avoid_scope_interior_or_small_residential")
        reasons.append("wykluczenie: mała inwestycja / wyłącznie wnętrza")

    value_mln = parse_value_pln_millions(resolved_value or hay)

    if _LOW_PRIORITY_SCOPE_RE.search(hay):
        cap = 40 if value_mln is not None and value_mln < 10 else 50
        score = min(score, cap)
        flags.append("low_priority_scope_municipal_green")
        reasons.append("niski priorytet: zieleń miejska / WBO / mała skala publiczna")
    if value_mln is not None:
        if value_mln >= 20:
            score = min(100, score + 12)
            flags.append("value_ideal_20m_plus")
            reasons.append(f"wartość ~{value_mln:.0f} mln PLN (idealna)")
        elif value_mln >= 10:
            score = min(100, score + 5)
            flags.append("value_viable_10_17m")
            reasons.append(f"wartość ~{value_mln:.0f} mln PLN (pasuje case-by-case)")
        elif value_mln < 10:
            score = min(score, max(25, score - 15))
            flags.append("value_below_10m")
            reasons.append(f"wartość ~{value_mln:.1f} mln PLN (poniżej progu)")

    if _TARGET_SCOPE_RE.search(hay):
        score = min(100, score + 8)
        flags.append("target_scope_cubature_engineering")

    if (_GOOD_PHASE_RE.search(hay) or _GW_PRESENT_RE.search(hay)) and not _GW_ABSENT_RE.search(hay):
        score = min(100, score + 10)
        flags.append("good_timing_gw_or_execution")
        if not any("GW" in r or "realizac" in r.lower() for r in reasons):
            reasons.append("timing: GW wybrany / przetarg / start robót")

    if _EARLY_PHASE_RE.search(hay) and not (
        _GOOD_PHASE_RE.search(hay) or (_GW_PRESENT_RE.search(hay) and not _GW_ABSENT_RE.search(hay))
    ):
        if _GW_ABSENT_RE.search(hay):
            score = min(score, 55)
            flags.append("too_early_phase_no_gw")
            reasons.append("za wczesny etap (brak GW)")
        else:
            score = min(score, max(30, score - 12))
            flags.append("too_early_phase")
            reasons.append("za wczesny etap (bez GW / bez startu budowy)")

    if score < 25 and "avoid_scope" not in "_".join(flags):
        if not _TARGET_SCOPE_RE.search(hay) and value_mln is None:
            score = min(score, 35)
            reasons.append("brak sygnału kubaturowego/inżynieryjnego")

    ceiling = _value_band_ceiling(value_mln, hay)
    if ceiling is not None and score > ceiling:
        score = ceiling
        flags.append("value_band_ceiling")
        reasons.append(f"limit ICP dla pasma wartości (~{value_mln:g} mln PLN)")

    # Design / pre-construction stage is an authoritative ceiling: no GW, no site
    # work → cannot sit at 90s regardless of value/scope bonuses.
    if _DESIGN_PHASE_RE.search(hay) and not _CONSTRUCTION_STARTED_RE.search(hay):
        if value_mln is not None and value_mln < 10:
            design_cap = 35
        elif value_mln is not None and value_mln >= 20:
            design_cap = 55
        else:
            design_cap = 45
        if score > design_cap:
            score = design_cap
        flags.append("design_or_preconstruction_phase")
        reasons.append("etap projektowy/przedrealizacyjny (bez GW, bez budowy)")

    reason_parts = [p for p in (source_reason, "; ".join(reasons)) if p and p.strip()]
    merged_reason = " | ".join(dict.fromkeys(reason_parts))[:280]
    return IcpAdjustment(
        icp_score=max(1, min(100, score)),
        icp_reason=merged_reason or source_reason or icp_reason or "",
        flags=tuple(flags),
        project_phase=project_phase,
    )


def extract_project_value_from_text(text: str | None) -> str | None:
    """Pull a value snippet from Kompass page / email prose for rubric + storage."""
    if not text or not text.strip():
        return None
    labeled = re.search(
        r"wartość\s+szacunkow[ae]\s*[:.]?\s*(\d[\d\s.,]*\s*(?:mln|milion\w*|million|mld|tys\.?|pln|zł|zl))",
        text,
        re.I,
    )
    if labeled:
        return labeled.group(1).strip()
    mln = parse_value_pln_millions(text)
    if mln is not None:
        return f"{mln:g} mln"
    return None


def rubric_seed_score(
    *,
    source: str,
    icp_reason: str | None,
    icp_score: int,
) -> int:
    """Starting score before rubric bonuses — avoids double-counting on re-score."""
    base = base_icp_reason(icp_reason)
    if base == "Manual Kompass URL import":
        return 60
    if source == "kompass_email":
        return min(int(icp_score), 85)
    return int(icp_score)


def refine_icp_from_kompass_text(
    *,
    icp_score: int,
    icp_reason: str | None,
    project_name: str,
    project_value: str | None,
    project_phase: str | None,
    kompass_text: str,
    source: str = "manual_kompass",
) -> IcpAdjustment:
    """Re-evaluate ICP after authenticated Kompass page context is available."""
    value_hint = project_value or extract_project_value_from_text(kompass_text)
    phase_hint = project_phase
    if not phase_hint:
        for label in (
            "Realizacja",
            "Wybór Generalnego Wykonawcy",
            "Wybór głównego projektanta",
            "Projektowanie zakończone",
            "Projektowanie",
            "Wizja",
        ):
            if label.lower() in kompass_text.lower():
                phase_hint = label
                break
    adj = apply_icp_rubric(
        icp_score=rubric_seed_score(
            source=source,
            icp_reason=icp_reason,
            icp_score=icp_score,
        ),
        icp_reason=base_icp_reason(icp_reason) or icp_reason,
        project_name=project_name,
        project_value=value_hint,
        project_phase=phase_hint,
        extra_text=kompass_text[:20_000],
    )
    return replace(
        adj,
        project_phase=phase_hint or adj.project_phase,
    )


def refine_icp_from_enrichment(
    *,
    icp_score: int,
    icp_reason: str | None,
    project_name: str,
    project_value: str | None,
    project_phase: str | None,
    enrichment_phase: str | None = None,
    enrichment_notes: str | None = None,
    source: str = "",
) -> IcpAdjustment:
    """Authoritative re-score using Claude's structured phase + notes.

    The raw Kompass page text often lacks clean stage labels, but Claude reliably
    resolves ``project_phase`` (e.g. "Wizja", "Projektowanie", "Wybór głównego
    projektanta") and explains GW presence in ``notes`` — feed those back in.
    """
    phase_hint = enrichment_phase or project_phase
    extra = " ".join(
        p for p in (enrichment_phase, enrichment_notes) if p and str(p).strip()
    )
    value_hint = coalesce_project_value(
        project_value=project_value,
        project_name=project_name,
        project_phase=phase_hint,
        icp_reason=icp_reason,
        extra_text=extra,
    )
    adj = apply_icp_rubric(
        icp_score=rubric_seed_score(
            source=source,
            icp_reason=icp_reason,
            icp_score=icp_score,
        ),
        icp_reason=base_icp_reason(icp_reason) or icp_reason,
        project_name=project_name,
        project_value=value_hint,
        project_phase=phase_hint,
        extra_text=extra or None,
    )
    return replace(adj, project_phase=phase_hint or adj.project_phase)


def apply_icp_to_project_dict(project: dict[str, Any]) -> dict[str, Any]:
    adj = apply_icp_rubric(
        icp_score=int(project.get("icp_score") or 50),
        icp_reason=project.get("icp_reason"),
        project_name=project.get("project_name"),
        project_value=project.get("project_value"),
        project_phase=project.get("project_phase"),
    )
    out = dict(project)
    out["icp_score"] = adj.icp_score
    out["icp_reason"] = adj.icp_reason
    return out
