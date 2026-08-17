"""Project-level similarity vs HubSpot deals (lot-aware, construction industry)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from skysnap.models import HubSpotDealCandidate, ProjectSimilarityDecision

if TYPE_CHECKING:
    from skysnap.db import Lead
    from skysnap.models import EnrichmentResult

MatchClass = Literal["same_project", "addon", "different_lot", "unrelated"]

_LOT_DIFF_RE = re.compile(
    r"(?:"
    r"(?:część|czesc|etap|faza|part|phase|segment|blok|block|budynek|bud\.?|building|lot|lokal)"
    r"\s*([ivxlc\d]+|[a-z])"
    r"|"
    r"(?:część|czesc|etap|faza|part|phase)\s*([ivxlc]+)"
    r")",
    re.I,
)

_ADDON_RE = re.compile(
    r"\b(rozbudow\w*|dogbudow\w*|przebudow\w*|modernizacj\w*|etap\s*(?:ii|2|drugi)|"
    r"phase\s*(?:ii|2|two)|extension|add-?on)\b",
    re.I,
)

_STOPWORDS = frozenset(
    {
        "ul",
        "al",
        "pl",
        "projekt",
        "inwestycja",
        "budowa",
        "przebudowa",
        "rozbudowa",
        "zagospodarowanie",
        "realizacja",
        "etap",
        "faza",
        "część",
        "czesc",
        "part",
    }
)


@dataclass(frozen=True)
class ProjectIdentity:
    project_name: str
    base_name: str
    lot_token: str | None
    kompass_slug: str | None
    project_url: str | None
    city: str | None
    address_hint: str | None
    company_name: str | None


@dataclass(frozen=True)
class ScoredDealCandidate:
    deal: HubSpotDealCandidate
    identity: ProjectIdentity
    pre_score: int
    forced_class: MatchClass | None = None


def _normalize_text(text: str | None) -> str:
    if not text or not str(text).strip():
        return ""
    t = str(text).lower().strip()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return " ".join(t.split())


def _kompass_slug(url: str | None) -> str | None:
    if not url or not str(url).strip():
        return None
    path = urlparse(str(url).strip()).path.strip("/")
    if not path:
        return None
    slug = path.split("/")[-1].strip()
    return slug.lower() if slug else None


def extract_lot_differentiator(text: str | None) -> str | None:
    """Extract lot/phase/building differentiator (e.g. '1', '2', 'a', 'ii')."""
    if not text or not str(text).strip():
        return None
    hay = str(text)
    best: str | None = None
    for match in _LOT_DIFF_RE.finditer(hay):
        token = next((g for g in match.groups() if g), None)
        if not token:
            continue
        norm = token.strip().lower()
        if norm in _STOPWORDS:
            continue
        best = norm
    return best


def _strip_lot_from_name(name: str) -> str:
    base = _LOT_DIFF_RE.sub(" ", name)
    return " ".join(base.split())


def _token_set(text: str) -> set[str]:
    return {t for t in _normalize_text(text).split() if len(t) > 2 and t not in _STOPWORDS}


def normalize_project_identity(
    *,
    project_name: str,
    project_url: str | None = None,
    city: str | None = None,
    company_name: str | None = None,
    address_hint: str | None = None,
    dealname: str | None = None,
) -> ProjectIdentity:
    """Build comparable project identity from lead or HubSpot deal fields."""
    raw_name = (project_name or dealname or "").strip()
    hay = " ".join(p for p in (raw_name, dealname, address_hint, city) if p)
    lot = extract_lot_differentiator(hay)
    base = _normalize_text(_strip_lot_from_name(raw_name or (dealname or "")))
    return ProjectIdentity(
        project_name=raw_name or (dealname or ""),
        base_name=base,
        lot_token=lot,
        kompass_slug=_kompass_slug(project_url),
        project_url=(project_url or "").strip() or None,
        city=(city or "").strip() or None,
        address_hint=(address_hint or "").strip() or None,
        company_name=(company_name or "").strip() or None,
    )


def identity_from_lead(
    lead: Lead,
    enrichment: EnrichmentResult | None = None,
) -> ProjectIdentity:
    address = None
    if enrichment and enrichment.company_address:
        address = enrichment.company_address
    company = lead.company_name
    if enrichment and enrichment.company_name:
        company = enrichment.company_name
    return normalize_project_identity(
        project_name=lead.project_name,
        project_url=lead.project_url,
        city=lead.city,
        company_name=company,
        address_hint=address,
    )


def identity_from_deal(deal: HubSpotDealCandidate) -> ProjectIdentity:
    return normalize_project_identity(
        project_name=deal.dealname or "",
        project_url=deal.project_url,
        city=None,
        company_name=None,
        address_hint=deal.description,
        dealname=deal.dealname,
    )


def lot_tokens_conflict(a: str | None, b: str | None) -> bool:
    """True when both sides have a lot token and they differ."""
    if not a or not b:
        return False
    return a.strip().lower() != b.strip().lower()


def deterministic_similarity(new: ProjectIdentity, candidate: ProjectIdentity) -> int:
    """0-100 pre-score before Claude refinement."""
    if new.kompass_slug and candidate.kompass_slug and new.kompass_slug == candidate.kompass_slug:
        return 98
    if new.project_url and candidate.project_url:
        if new.project_url.rstrip("/") == candidate.project_url.rstrip("/"):
            return 98

    new_tokens = _token_set(new.base_name)
    cand_tokens = _token_set(candidate.base_name)
    if not new_tokens or not cand_tokens:
        return 0
    overlap = len(new_tokens & cand_tokens)
    union = len(new_tokens | cand_tokens)
    jaccard = overlap / union if union else 0.0
    score = int(round(jaccard * 100))

    if new.city and candidate.city and _normalize_text(new.city) == _normalize_text(candidate.city):
        score = min(100, score + 8)
    return max(0, min(100, score))


def apply_lot_rules(
    pre_score: int,
    new: ProjectIdentity,
    candidate: ProjectIdentity,
) -> tuple[int, MatchClass | None]:
    """Enforce hard caps/boosts; returns (adjusted_score, forced_class or None)."""
    if lot_tokens_conflict(new.lot_token, candidate.lot_token):
        return min(pre_score, 35), "different_lot"

    if new.kompass_slug and candidate.kompass_slug and new.kompass_slug == candidate.kompass_slug:
        return 98, "same_project"

    if _ADDON_RE.search(new.project_name) or _ADDON_RE.search(candidate.project_name):
        if pre_score >= 50:
            return min(pre_score, 85), "addon"

    return pre_score, None


def score_deal_candidate(
    new: ProjectIdentity,
    deal: HubSpotDealCandidate,
) -> ScoredDealCandidate:
    cand_identity = identity_from_deal(deal)
    pre = deterministic_similarity(new, cand_identity)
    adjusted, forced = apply_lot_rules(pre, new, cand_identity)
    return ScoredDealCandidate(
        deal=deal,
        identity=cand_identity,
        pre_score=adjusted,
        forced_class=forced,
    )


def pick_top_candidates(
    scored: list[ScoredDealCandidate],
    *,
    limit: int = 5,
) -> list[ScoredDealCandidate]:
    ranked = sorted(scored, key=lambda s: s.pre_score, reverse=True)
    return ranked[: int(limit)]


def merge_similarity_decision(
    *,
    pre_score: int,
    forced_class: MatchClass | None,
    claude_decision: ProjectSimilarityDecision,
    best_deal: HubSpotDealCandidate | None,
) -> ProjectSimilarityDecision:
    """Blend Claude output with deterministic rules (lot conflicts win)."""
    if forced_class == "different_lot":
        pct = min(claude_decision.similarity_pct, pre_score, 35)
        return ProjectSimilarityDecision(
            similarity_pct=max(0, pct),
            match_class="different_lot",
            matched_deal_id=best_deal.id if best_deal else claude_decision.matched_deal_id,
            matched_deal_name=(best_deal.dealname if best_deal else claude_decision.matched_deal_name),
            confidence=claude_decision.confidence,
            reasoning=claude_decision.reasoning,
        )
    if forced_class == "same_project":
        pct = max(claude_decision.similarity_pct, pre_score, 90)
        return ProjectSimilarityDecision(
            similarity_pct=min(100, pct),
            match_class="same_project",
            matched_deal_id=best_deal.id if best_deal else claude_decision.matched_deal_id,
            matched_deal_name=(best_deal.dealname if best_deal else claude_decision.matched_deal_name),
            confidence=claude_decision.confidence,
            reasoning=claude_decision.reasoning,
        )

    pct = max(0, min(100, int(round((pre_score + claude_decision.similarity_pct) / 2))))
    match_class = claude_decision.match_class
    if pct < 25:
        match_class = "unrelated"
    return ProjectSimilarityDecision(
        similarity_pct=pct,
        match_class=match_class,
        matched_deal_id=claude_decision.matched_deal_id,
        matched_deal_name=claude_decision.matched_deal_name,
        confidence=claude_decision.confidence,
        reasoning=claude_decision.reasoning,
    )


def empty_similarity_decision() -> ProjectSimilarityDecision:
    return ProjectSimilarityDecision(
        similarity_pct=0,
        match_class="unrelated",
        matched_deal_id=None,
        matched_deal_name=None,
        confidence=0.0,
        reasoning=None,
    )


_MATCH_CLASS_LABELS = {
    "same_project": "same project",
    "addon": "add-on",
    "different_lot": "different lot",
    "unrelated": "unrelated",
}


def format_deal_similarity_cell(decision: ProjectSimilarityDecision | None) -> str:
    """Google Sheet 'Deal Similarity' column value."""
    if decision is None:
        return ""
    label = _MATCH_CLASS_LABELS.get(decision.match_class, decision.match_class)
    vs = ""
    if decision.matched_deal_name:
        vs = f" (vs {decision.matched_deal_name[:80]})"
    return f"{decision.similarity_pct}% — {label}{vs}"
