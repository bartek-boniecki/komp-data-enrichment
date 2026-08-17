from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ExtractedProject(BaseModel):
    project_name: str = Field(..., description="Project/deal name")
    company_name: str | None = Field(None, description="Company/investor/general contractor, if present")
    country: str | None = None
    city: str | None = None
    project_value: str | None = Field(None, description="Value/budget/size, as text if ambiguous")
    project_phase: str | None = Field(None, description="Planning/tender/construction/etc")
    project_url: str | None = Field(None, description="A URL to project detail page if present")
    icp_score: int = Field(..., ge=1, le=100)
    icp_reason: str | None = None
    raw: dict = Field(default_factory=dict, description="Any extra fields extracted from source")


class EmailExtraction(BaseModel):
    source: str = "kompass_email"
    projects: list[ExtractedProject]


class HubSpotCompanyCandidate(BaseModel):
    id: str
    name: str | None = None
    domain: str | None = None
    country: str | None = None


class FuzzyDuplicateDecision(BaseModel):
    is_duplicate: bool
    matched_company_id: str | None = None
    matched_company_name: str | None = None
    confidence: float = Field(..., ge=0, le=1)
    reasoning: str | None = None


# Fuzzy company matching mislabels similar-but-different firms (Budimex vs
# "Budimet"); acting on a low-confidence match attaches deals and contacts to
# the WRONG HubSpot company. Only confident matches count as duplicates —
# weaker ones are surfaced as a "possible duplicate" note for humans instead.
DUPLICATE_MIN_CONFIDENCE = 0.8


def is_confident_duplicate(
    decision: FuzzyDuplicateDecision | None,
    *,
    min_confidence: float = DUPLICATE_MIN_CONFIDENCE,
) -> bool:
    return bool(
        decision
        and decision.is_duplicate
        and decision.matched_company_id
        and float(decision.confidence) >= float(min_confidence)
    )


class HubSpotDealCandidate(BaseModel):
    id: str
    dealname: str | None = None
    project_url: str | None = None
    stage: str | None = None
    description: str | None = None
    company_id: str | None = None
    pipeline_id: str | None = None


class ProjectSimilarityDecision(BaseModel):
    similarity_pct: int = Field(..., ge=0, le=100)
    match_class: Literal["same_project", "addon", "different_lot", "unrelated"]
    matched_deal_id: str | None = None
    matched_deal_name: str | None = None
    confidence: float = Field(0.0, ge=0, le=1)
    reasoning: str | None = None


class WebsiteContact(BaseModel):
    full_name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    direct_email: str | None = None
    direct_phone: str | None = None
    # Pattern-inferred address (name + company domain). NEVER copied into
    # email/direct_email: a guess is not a found contact and must stay
    # visibly separate all the way to the sheet/HubSpot.
    guessed_email: str | None = None
    linkedin_url: str | None = None
    source_url: str | None = None
    confidence: float = Field(..., ge=0, le=1)


class EnrichmentResult(BaseModel):
    source: Literal["kompass", "osint", "website"] = "website"
    company_name: str | None = None
    website: str | None = None
    project_phase: str | None = None
    project_description: str | None = None
    # Kompass project-page fields (Typ / Sektor / investment location).
    investment_type: str | None = None  # raw Kompass Typ, e.g. Publiczna
    sector_subsector: str | None = None
    project_city: str | None = None
    project_voivodeship: str | None = None
    project_street: str | None = None
    project_building_number: str | None = None
    contact: WebsiteContact | None = None
    # Company switchboard from Kompass firm profile ("Pokaż kontakt") — fallback only.
    company_generic_email: str | None = None
    company_generic_phone: str | None = None
    company_address: str | None = None
    company_nip: str | None = None
    sheet_role: str | None = None
    sheet_branza: str | None = None
    notes: str | None = None

