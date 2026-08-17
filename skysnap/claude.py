from __future__ import annotations

import json
import re
from typing import Any

from anthropic import Anthropic, AuthenticationError
from pydantic import BaseModel, ValidationError

from skysnap.claude_usage import ClaudeUsageTracker
from skysnap.icp import ICP_EMAIL_SYSTEM_PROMPT, apply_icp_rubric, coalesce_project_value
from skysnap.nim import DEFAULT_NIM_MODEL, is_anthropic_unavailable_error, make_nim_client
from skysnap.progress import log_progress
from skysnap.sheet_taxonomy import claude_taxonomy_instructions
from skysnap.models import (
    EmailExtraction,
    EnrichmentResult,
    FuzzyDuplicateDecision,
    HubSpotCompanyCandidate,
    HubSpotDealCandidate,
    ProjectSimilarityDecision,
)

_CONTACT_JSON_SCHEMA = (
    '"contact": {"full_name": "...", "role": "...", "email": "...", "phone": "...", '
    '"direct_email": "...", "direct_phone": "...", "linkedin_url": "...", '
    '"source_url": "...", "confidence": 0.0-1.0}'
)
_ENRICHMENT_JSON_SCHEMA = (
    '"company_name": "...", "website": "...", "project_phase": "...", '
    '"project_description": "...", '
    '"investment_type": "Publiczna|Prywatna|...", '
    '"sector_subsector": "...", '
    '"project_city": "...", "project_voivodeship": "...", '
    '"project_street": "...", "project_building_number": "...", '
    '"company_generic_email": "biuro@... or null", '
    '"company_generic_phone": "switchboard or null", '
    '"sheet_role": "...", "sheet_branza": "...", ' + _CONTACT_JSON_SCHEMA
)


def _salvage_projects_json(raw: str) -> dict[str, Any] | None:
    """Recover complete project objects from a truncated email-extraction response."""
    match = re.search(r'"projects"\s*:\s*\[', raw)
    if not match:
        return None
    rest = raw[match.end() :]
    projects: list[dict[str, Any]] = []
    depth = 0
    start: int | None = None
    in_string = False
    escape = False
    for i, ch in enumerate(rest):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                blob = rest[start : i + 1]
                try:
                    projects.append(json.loads(blob))
                except json.JSONDecodeError:
                    pass
                start = None
        elif ch == "]" and depth == 0:
            break
    if not projects:
        return None
    source = "kompass_email"
    src_match = re.search(r'"source"\s*:\s*"([^"]+)"', raw)
    if src_match:
        source = src_match.group(1)
    return {"source": source, "projects": projects}


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if not raw:
        raise ValueError("Claude returned empty text (expected JSON)")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, count=1, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            salvaged = _salvage_projects_json(raw[start : end + 1])
            if salvaged:
                return salvaged
    salvaged = _salvage_projects_json(raw)
    if salvaged:
        return salvaged
    raise ValueError(f"Claude response was not valid JSON (first 200 chars): {raw[:200]!r}")


class ClaudeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        usage_tracker: ClaudeUsageTracker | None = None,
        nvidia_api_key: str | None = None,
        nvidia_model: str = DEFAULT_NIM_MODEL,
    ) -> None:
        if not api_key and not nvidia_api_key:
            raise ValueError("ANTHROPIC_API_KEY or NVIDIA_API_KEY is required")
        self._anthropic_client = Anthropic(api_key=api_key) if api_key else None
        self._model = model
        self._usage = usage_tracker
        self._nvidia_api_key = nvidia_api_key
        self._nvidia_model = nvidia_model
        self._nvidia_client = make_nim_client(api_key=nvidia_api_key) if nvidia_api_key else None
        self._anthropic_disabled = not api_key
        self._logged_nim_fallback = False

    @property
    def usage_tracker(self) -> ClaudeUsageTracker | None:
        return self._usage

    @property
    def active_provider(self) -> str:
        if self._anthropic_disabled or self._anthropic_client is None:
            return "nvidia_nim"
        return "anthropic"

    def _nim_json_call(
        self,
        *,
        system: str,
        user: str,
        schema_hint: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        if self._nvidia_client is None:
            raise ValueError(
                "Claude is unavailable and NVIDIA NIM fallback is not configured. "
                "Set NVIDIA_API_KEY in .env (https://build.nvidia.com/)."
            )
        response = self._nvidia_client.chat.completions.create(
            model=self._nvidia_model,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": user
                    + "\n\nReturn ONLY JSON.\n"
                    + f"JSON schema hint (informal): {schema_hint}\n",
                },
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        text = response.choices[0].message.content or ""
        return _parse_json_object(text)

    def _anthropic_json_call(
        self,
        *,
        operation: str,
        system: str,
        user: str,
        schema_hint: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        if self._anthropic_client is None:
            raise RuntimeError("Anthropic client not configured")
        try:
            msg = self._anthropic_client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=0.2,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": user
                        + "\n\nReturn ONLY JSON.\n"
                        + f"JSON schema hint (informal): {schema_hint}\n",
                    }
                ],
            )
        except AuthenticationError as e:
            raise ValueError(
                "ANTHROPIC_API_KEY was rejected (401). Create a new key at "
                "https://console.anthropic.com/settings/keys and set it in .env "
                "(no quotes; no trailing spaces)."
            ) from e
        if self._usage is not None and getattr(msg, "usage", None) is not None:
            self._usage.record(
                operation,
                input_tokens=int(msg.usage.input_tokens),
                output_tokens=int(msg.usage.output_tokens),
            )
        text = "".join([p.text for p in msg.content if getattr(p, "type", None) == "text"])
        return _parse_json_object(text)

    def _json_call(
        self,
        *,
        operation: str,
        system: str,
        user: str,
        schema_hint: str,
        max_tokens: int = 1800,
    ) -> dict[str, Any]:
        if self._anthropic_disabled or self._anthropic_client is None:
            return self._nim_json_call(
                system=system,
                user=user,
                schema_hint=schema_hint,
                max_tokens=max_tokens,
            )
        try:
            return self._anthropic_json_call(
                operation=operation,
                system=system,
                user=user,
                schema_hint=schema_hint,
                max_tokens=max_tokens,
            )
        except Exception as e:
            if self._nvidia_client is None or not is_anthropic_unavailable_error(e):
                raise
            self._anthropic_disabled = True
            if not self._logged_nim_fallback:
                log_progress(
                    "Claude unavailable (quota/billing); using NVIDIA NIM fallback for this run"
                )
                self._logged_nim_fallback = True
            return self._nim_json_call(
                system=system,
                user=user,
                schema_hint=schema_hint,
                max_tokens=max_tokens,
            )

    def extract_projects_from_email(self, *, html: str, max_html_chars: int = 80_000) -> EmailExtraction:
        system = ICP_EMAIL_SYSTEM_PROMPT
        clipped = html[:max_html_chars]
        user = (
            "Parse this email HTML and extract a list of projects.\n"
            "For each project fill project_name, company_name (if any), country, city, project_value, project_phase, project_url.\n"
            "Also include icp_score and icp_reason. Keep raw={} unless needed.\n\n"
            f"EMAIL_HTML:\n{clipped}"
        )
        last_error: Exception | None = None
        for attempt, max_tokens in enumerate((8192, 16384)):
            try:
                data = self._json_call(
                    operation="extract_projects_from_email",
                    system=system,
                    user=user
                    + (
                        "\n\nIMPORTANT: Your previous response was truncated — return complete, valid JSON only."
                        if attempt > 0
                        else ""
                    ),
                    schema_hint='{"source": "kompass_email", "projects": [ExtractedProject]}',
                    max_tokens=max_tokens,
                )
                extraction = EmailExtraction.model_validate(data)
                adjusted_projects = []
                for p in extraction.projects:
                    value_text = coalesce_project_value(
                        project_value=p.project_value,
                        project_name=p.project_name,
                        project_phase=p.project_phase,
                        icp_reason=p.icp_reason,
                    )
                    adj = apply_icp_rubric(
                        icp_score=p.icp_score,
                        icp_reason=p.icp_reason,
                        project_name=p.project_name,
                        project_value=value_text,
                        project_phase=p.project_phase,
                    )
                    adjusted_projects.append(
                        p.model_copy(
                            update={
                                "icp_score": adj.icp_score,
                                "icp_reason": adj.icp_reason,
                                "project_value": value_text or p.project_value,
                            }
                        )
                    )
                return extraction.model_copy(update={"projects": adjusted_projects})
            except (ValueError, json.JSONDecodeError, ValidationError) as e:
                last_error = e
        assert last_error is not None
        raise last_error

    def decide_duplicate(
        self,
        *,
        new_company_name: str,
        candidates: list[HubSpotCompanyCandidate],
    ) -> FuzzyDuplicateDecision:
        system = (
            "You are a CRM deduplication assistant. Your job is to decide if a new company name matches an existing HubSpot company.\n"
            "Use fuzzy matching tolerant to suffixes (SA, sp. z o.o., LLC), punctuation, and whitespace.\n"
            "Return strict JSON only."
        )
        user = (
            f"New company name: {new_company_name}\n\n"
            "Candidates (JSON):\n"
            + json.dumps([c.model_dump() for c in candidates], ensure_ascii=False)
        )
        data = self._json_call(
            operation="decide_duplicate",
            system=system,
            user=user,
            schema_hint='{"is_duplicate": true, "matched_company_id": "123", "confidence": 0.0-1.0, "reasoning": "..."}',
        )
        return FuzzyDuplicateDecision.model_validate(data)

    def decide_project_similarity(
        self,
        *,
        new_project: dict[str, Any],
        candidates: list[HubSpotDealCandidate],
        pre_scores: list[int] | None = None,
    ) -> ProjectSimilarityDecision:
        system = (
            "You assess whether a new construction investment project matches an existing HubSpot deal.\n"
            "Return strict JSON only.\n\n"
            "match_class values:\n"
            "- same_project: same lot/tender/investment re-ingested (identical Kompass URL or clearly same scope)\n"
            "- addon: extension/phase-2/rozbudowa/dogbudowa on same site/object (related but distinct scope)\n"
            "- different_lot: same company or address but different lot/phase/building "
            "(e.g. Część 1 vs Część 2, Part 1 vs Part 2, Budynek A vs Budynek B) — LOW similarity\n"
            "- unrelated: no meaningful match\n\n"
            "CRITICAL: Part 1 vs Part 2, Część 1 vs Część 2, Budynek A vs B at the same address "
            "must be different_lot with similarity_pct <= 35.\n"
            "Identical Kompass project URL → same_project with similarity_pct >= 90.\n"
            "Rozbudowa/dogbudowa/etap II on same object → addon, typically 50-85%.\n"
            "similarity_pct is 0-100 vs the BEST matching candidate only."
        )
        cand_payload = []
        for i, c in enumerate(candidates):
            item = c.model_dump()
            if pre_scores and i < len(pre_scores):
                item["deterministic_pre_score"] = pre_scores[i]
            cand_payload.append(item)
        user = (
            "New project (JSON):\n"
            + json.dumps(new_project, ensure_ascii=False)
            + "\n\nExisting HubSpot deal candidates (JSON):\n"
            + json.dumps(cand_payload, ensure_ascii=False)
        )
        data = self._json_call(
            operation="decide_project_similarity",
            system=system,
            user=user,
            schema_hint=(
                '{"similarity_pct": 0, "match_class": "unrelated", '
                '"matched_deal_id": "123", "matched_deal_name": "...", '
                '"confidence": 0.0, "reasoning": "..."}'
            ),
        )
        return ProjectSimilarityDecision.model_validate(data)

    def extract_contact_from_website_text(
        self,
        *,
        website_url: str,
        text: str,
    ) -> EnrichmentResult:
        system = (
            "You extract contact details from website text for B2B sales outreach. Return JSON only.\n"
            "If no reliable contact is present, return contact=null and explain briefly in notes.\n"
            "Prefer: project manager / purchasing / construction director. Avoid generic info@ unless nothing else.\n"
        )
        user = (
            f"Website URL: {website_url}\n\n"
            "Website text (scraped, noisy):\n"
            f"{text}\n\n"
            "Extract best contact person (name, role, email, phone) if present, with confidence 0-1."
        )
        data = self._json_call(
            operation="extract_contact_from_website",
            system=system,
            user=user,
            schema_hint='{"source": "website", "website": "...", "project_phase": "...", ' + _CONTACT_JSON_SCHEMA + ', "notes": "..."}',
        )
        result = EnrichmentResult.model_validate(data)
        if result.source == "website":
            return result
        return result.model_copy(update={"source": "website"})

    def extract_contact_from_kompass_page(
        self,
        *,
        project_url: str,
        text: str,
        project_name: str,
        company_name: str | None,
    ) -> EnrichmentResult:
        system = (
            "You extract contact details from authenticated Kompass Inwestycji contact panels (Polish). "
            "Return JSON only.\n"
            "Input may include a KOMPASS CONTACT PANEL section after the user selected a project participant. "
            "Typical format: 'dane kontaktowe to: {role} - {full_name}' and 'email: ...' / phone lines.\n"
            "Look for: osoba kontaktowa, telefon, e-mail, dyrektor, kierownik.\n"
            "Primary contact targets at the winning Generalny Wykonawca (GW): bidding department "
            "(dział ofertowania, specjalista ds. ofertowania) and investment preparation "
            "(przygotowanie inwestycji, dział techniczny). Secondary: Inwestor, Projektant.\n"
            "The selected participant should be Generalny Wykonawca (GW) when present — this is the primary target role.\n"
            "Only use Inwestor or subcontractor/trade contacts if no GW contact is available.\n"
            "If no reliable *personal* contact is present, return contact=null and explain briefly in notes.\n"
            "Do NOT put company switchboard emails (biuro@, kontakt@, sekretariat@) or main office phones "
            "in contact fields — those are collected separately from the firm profile.\n"
            + claude_taxonomy_instructions()
        )
        company_line = f"Company: {company_name}\n" if company_name else ""
        user = (
            f"Project: {project_name}\n"
            f"{company_line}"
            f"Kompass URL: {project_url}\n\n"
            "Page text (scraped, noisy):\n"
            f"{text}\n\n"
            "Extract best contact person (name, role, email, phone) if present, with confidence 0-1. "
            "Set company_name to the organization the contact works for (GW preferred). "
            "Set project_phase from the Kompass investment stage on the page "
            "(e.g. Projektowanie, Projektowanie zakończone, Wybór Generalnego Wykonawcy, Realizacja). "
            "Set project_description to the main Kompass investment description "
            "(Polish 'Opis inwestycji' / 'Ogólne informacje'), in Polish when available. "
            "Set investment_type from Kompass Typ (Publiczna/Prywatna/publiczno-prawne). "
            "Set sector_subsector from 'Sektor, podsektor'. "
            "Set project_city, project_voivodeship, project_street, project_building_number "
            "from the project location block (Miasto, Województwo, Adres). "
            "Set direct_email/direct_phone for the person's direct channels when known; "
            "use email/phone for any published channel. "
            "Set linkedin_url when a LinkedIn profile URL is visible. "
            "Set website to the company homepage if visible (not kompassinwestycji.pl)."
        )
        data = self._json_call(
            operation="extract_contact_from_kompass_page",
            system=system,
            user=user,
            schema_hint='{"source": "kompass", ' + _ENRICHMENT_JSON_SCHEMA + ', "notes": "..."}',
            max_tokens=3000,  # full field set + Polish project_description; 1800 truncated → JSON error → lead failed
        )
        result = EnrichmentResult.model_validate(data)
        return result.model_copy(update={"source": "kompass"})

    def extract_contact_from_osint_sources(
        self,
        *,
        lead_label: str,
        sources: list[dict[str, str]],
        extracted_candidates: dict | None = None,
    ) -> EnrichmentResult:
        system = (
            "You extract B2B contact details from multiple OSINT web snippets (Polish construction market). "
            "Return JSON only.\n"
            "Merge the best single contact from all sources. Cite source URLs in notes.\n"
            "A PROGRAMMATICALLY EXTRACTED CONTACTS block (high trust) may be provided: these emails/phones "
            "were parsed deterministically from the pages (mailto:/tel:/structured data). Prefer them "
            "over values you infer from prose; pick the most personal (non-generic) one that matches the company.\n"
            "CRITICAL — relevance: only return a contact that plausibly belongs to the INVESTOR, the "
            "procuring authority (zamawiający/inwestor), or the (general) contractor/architect for THIS "
            "project. Prefer GW bidding (dział ofertowania, specjalista ds. ofertowania) or investment "
            "preparation (przygotowanie inwestycji) roles at the general contractor when available.\n"
            "If the only contacts available belong to NEWS outlets, journalists, press/media "
            "portals, business-news sites, or otherwise unrelated organizations, you MUST return "
            "contact=null (do not put a media/unrelated contact in the contact fields).\n"
            "Do NOT set website to a news article, press portal, directory, aggregator, or social-media "
            "URL — only the organization's own homepage; otherwise leave website null.\n"
            "If nothing reliable, return contact=null.\n"
            + claude_taxonomy_instructions()
        )
        candidates_block = (
            "PROGRAMMATICALLY EXTRACTED CONTACTS (high trust):\n"
            + json.dumps(extracted_candidates, ensure_ascii=False)
            + "\n\n"
            if extracted_candidates and (extracted_candidates.get("emails") or extracted_candidates.get("phones"))
            else ""
        )
        user = (
            f"Lead: {lead_label}\n\n"
            + candidates_block
            + "Sources (JSON list of {url, text}):\n"
            + json.dumps(sources, ensure_ascii=False)
            + "\n\nExtract best contact (name, role, email, phone) with confidence 0-1. "
            "Set company_name to the organization being contacted. "
            "Set project_phase when the investment stage is mentioned. "
            "Set project_description when sources describe the investment scope or project. "
            "Set direct_email/direct_phone for direct personal channels; email/phone for any published channel. "
            "Set linkedin_url when a linkedin.com/in/ profile URL appears. "
            "Set website to the best company homepage URL from sources."
        )
        data = self._json_call(
            operation="extract_contact_from_osint_sources",
            system=system,
            user=user,
            schema_hint='{"source": "osint", ' + _ENRICHMENT_JSON_SCHEMA + ', "notes": "..."}',
            max_tokens=2400,
        )
        result = EnrichmentResult.model_validate(data)
        return result.model_copy(update={"source": "osint"})

    def fill_contact_gaps_from_osint_sources(
        self,
        *,
        company_name: str | None,
        project_name: str,
        existing: EnrichmentResult,
        gap: str,
        target_roles: list[str],
        sources: list[dict[str, str]],
        extracted_candidates: dict | None = None,
    ) -> EnrichmentResult:
        existing_json = existing.model_dump(exclude_none=True)
        roles_block = "\n".join(f"- {role}" for role in target_roles)
        if gap == "channels":
            task = (
                "Phase 2 — channels only. A contact person name is already set; do NOT change full_name "
                "and do NOT change role — this person's job title was established earlier. "
                "Find the missing email and/or phone for THIS PERSON (Kompass placeholders like "
                "'widoczny email' count as missing). "
                "Personal channels only in contact fields: email/phone for any channel published for the "
                "person, direct_email/direct_phone ONLY when the source explicitly presents them as the "
                "person's own direct line/address. "
                "Generic company mailboxes and switchboards (biuro@, kontakt@, sekretariat@, recepcja@, "
                "main office numbers) go in company_generic_email / company_generic_phone — NEVER in "
                "email, direct_email, phone, or direct_phone. "
                "Also set linkedin_url when a linkedin.com/in/ URL is in sources. "
                "Leave full_name and role unchanged. Do not invent data."
            )
        else:
            task = (
                "Phase 1 — name only. No reliable contact person name exists yet. "
                "Search results include LinkedIn dorks. "
                "Find the best matching person at this company with one of the target job titles below. "
                "Return full_name, role, and linkedin_url (linkedin.com/in/...) when the profile URL is in sources. "
                "Set email, phone, direct_email, direct_phone to null — they are filled in a later step. "
                "Do not invent data."
            )
        system = (
            "You complete missing B2B contact details from OSINT web snippets (Polish construction). "
            "Return JSON only. Sources may include LinkedIn public snippets and company pages.\n"
            "A PROGRAMMATICALLY EXTRACTED CONTACTS block (high trust) may be provided: prefer those "
            "deterministically-parsed emails/phones over values inferred from prose.\n"
            "Only use a contact that plausibly belongs to the investor, procuring authority, or "
            "(general) contractor/architect for THIS project. Prefer GW bidding and investment-preparation "
            "roles at the general contractor when filling name/role gaps.\n"
            "Never return a contact that belongs to a "
            "news outlet, journalist, media/press portal, or unrelated organization — leave those fields "
            "null instead.\n"
            f"{task}\n"
            + claude_taxonomy_instructions()
        )
        candidates_block = (
            "PROGRAMMATICALLY EXTRACTED CONTACTS (high trust):\n"
            + json.dumps(extracted_candidates, ensure_ascii=False)
            + "\n\n"
            if extracted_candidates and (extracted_candidates.get("emails") or extracted_candidates.get("phones"))
            else ""
        )
        user = (
            f"Project: {project_name}\n"
            f"Company: {company_name or 'unknown'}\n"
            f"Gap type: {gap}\n"
            f"Existing enrichment (JSON):\n{json.dumps(existing_json, ensure_ascii=False)}\n\n"
            f"Target job titles (prefer in this order when gap=no_name / phase 1):\n{roles_block}\n\n"
            + candidates_block
            + "Sources (JSON list of {url, text}):\n"
            + json.dumps(sources, ensure_ascii=False)
            + "\n\nReturn updated contact fields. Keep existing good values; only fill gaps."
        )
        data = self._json_call(
            operation="fill_contact_gaps_from_osint",
            system=system,
            user=user,
            schema_hint='{"source": "osint", ' + _ENRICHMENT_JSON_SCHEMA + ', "notes": "..."}',
            max_tokens=2400,
        )
        result = EnrichmentResult.model_validate(data)
        return result.model_copy(update={"source": existing.source})

