from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import requests

from skysnap.db import Lead
from skysnap.enrichment import has_personal_contact_data
from skysnap.hubspot_export import (
    ASSOC_NOTE_TO_COMPANY,
    ASSOC_NOTE_TO_CONTACT,
    ASSOC_NOTE_TO_DEAL,
    ASSOC_TASK_TO_COMPANY,
    ASSOC_TASK_TO_CONTACT,
    ASSOC_TASK_TO_DEAL,
    HubSpotFollowUpConfig,
    HubSpotWriteConfig,
    analysis_note_hash,
    build_analysis_note_body,
    build_company_properties,
    build_contact_properties,
    build_deal_properties,
    build_task_associations,
    build_task_properties,
    resolve_company_id,
    resolve_existing_deal_id,
    should_create_hubspot_followup,
)
from skysnap.hubspot_props import DroppedProperty, PropertySchema
from skysnap.models import (
    EnrichmentResult,
    FuzzyDuplicateDecision,
    HubSpotCompanyCandidate,
    HubSpotDealCandidate,
    ProjectSimilarityDecision,
)
from skysnap.tzutil import get_timezone


HUBSPOT_BASE = "https://api.hubapi.com"


def _due_date_to_unix_ms(due_date: Any, *, timezone: str = "Europe/Warsaw") -> str:
    """Convert due_date (datetime/date/str/int) to HubSpot hs_timestamp (Unix ms)."""
    if due_date is None:
        raise ValueError("taskDetails.due_date is required")
    if isinstance(due_date, (int, float)):
        value = int(due_date)
        if value < 10_000_000_000:  # seconds → ms
            value *= 1000
        return str(value)
    if isinstance(due_date, str) and due_date.strip().isdigit():
        return _due_date_to_unix_ms(int(due_date.strip()), timezone=timezone)

    tz = get_timezone(timezone)
    if isinstance(due_date, datetime):
        dt = due_date if due_date.tzinfo else due_date.replace(tzinfo=tz)
        return str(int(dt.timestamp() * 1000))
    if isinstance(due_date, date):
        dt = datetime(due_date.year, due_date.month, due_date.day, 9, 0, 0, tzinfo=tz)
        return str(int(dt.timestamp() * 1000))
    if isinstance(due_date, str):
        raw = due_date.strip()
        try:
            if "T" in raw or raw.count(" ") == 1:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=tz)
                return str(int(parsed.timestamp() * 1000))
            d = datetime.strptime(raw[:10], "%Y-%m-%d").date()
            dt = datetime(d.year, d.month, d.day, 9, 0, 0, tzinfo=tz)
            return str(int(dt.timestamp() * 1000))
        except ValueError as e:
            raise ValueError(f"Unrecognized due_date format: {due_date!r}") from e
    raise ValueError(f"Unsupported due_date type: {type(due_date).__name__}")


HUBSPOT_STANDARD_COMPANY_PROPERTIES = frozenset(
    {
        "name",
        "domain",
        "website",
        "description",
        "country",
        "city",
        "zip",
        "state",
        "address",
        "phone",
        "linkedin_company_page",
        "hubspot_owner_id",
    }
)

HUBSPOT_STANDARD_DEAL_PROPERTIES = frozenset(
    {
        "dealname",
        "pipeline",
        "dealstage",
        "description",
    }
)


def _invalid_property_names(response: requests.Response) -> list[str]:
    try:
        data = response.json()
        errors = data.get("errors") if isinstance(data, dict) else None
        if not isinstance(errors, list):
            return []
        names: list[str] = []
        for err in errors:
            if not isinstance(err, dict):
                continue
            ctx = err.get("context") or {}
            prop = ctx.get("propertyName")
            if isinstance(prop, list) and prop:
                names.append(str(prop[0]))
            elif isinstance(prop, str) and prop:
                names.append(prop)
        return names
    except Exception:
        return []


def _existing_object_id(response: requests.Response) -> str | None:
    """Pull the id out of a 409 'already exists' response."""
    try:
        message = str((response.json() or {}).get("message") or "")
    except Exception:
        message = response.text[:300]
    match = re.search(r"(?:Existing ID|existing id)\s*:?\s*(\d+)", message)
    return match.group(1) if match else None


def _hubspot_error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error") or ""
            errors = data.get("errors")
            if errors:
                return f"{msg} errors={errors}".strip()
            return str(msg)
    except Exception:
        pass
    return (response.text or "")[:300]


class HubSpotWriteError(RuntimeError):
    """HubSpot token lacks write scopes or request payload is invalid."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HubSpotRateLimitError(HubSpotWriteError):
    """HubSpot API rate limit (HTTP 429)."""


REQUIRED_WRITE_SCOPES = (
    "crm.objects.companies.read",
    "crm.objects.companies.write",
    "crm.objects.contacts.write",
    "crm.objects.deals.read",
    "crm.objects.deals.write",
    "crm.objects.tasks.write",
)


def create_hubspot_followup_task(
    *,
    access_token: str,
    owner_id: str,
    record_id: str,
    record_type: Literal["contact", "company"],
    task_details: dict[str, Any],
    timezone: str = "Europe/Warsaw",
) -> dict[str, Any]:
    """Create an assigned HubSpot task linked to a contact or company (CRM v3).

    Parameters
    ----------
    access_token:
        HubSpot private app token.
    owner_id:
        HubSpot user id for ``hubspot_owner_id``.
    record_id:
        Contact or company object id to associate.
    record_type:
        ``\"contact\"`` (associationTypeId 204) or ``\"company\"`` (192).
    task_details:
        Dict with ``subject``, optional ``body`` / ``priority`` / ``status``,
        and ``due_date`` (datetime, date, ISO string, or Unix seconds/ms).
    timezone:
        Used when ``due_date`` is date-only (default 09:00 local).

    Returns
    -------
    dict
        Parsed JSON body from HubSpot.

    Raises
    ------
    HubSpotWriteError
        On 401/403 (token/scopes), 400 (validation), or other HTTP errors.
    ValueError
        On invalid arguments.
    """
    if not access_token or not str(access_token).strip():
        raise ValueError("accessToken is required")
    if not owner_id or not str(owner_id).strip():
        raise ValueError("ownerId is required")
    if not record_id or not str(record_id).strip():
        raise ValueError("recordId is required")
    record_type_norm = (record_type or "").strip().lower()
    if record_type_norm not in ("contact", "company"):
        raise ValueError('recordType must be "contact" or "company"')
    if not isinstance(task_details, dict):
        raise ValueError("taskDetails must be an object/dict")

    subject = str(task_details.get("subject") or "").strip()
    if not subject:
        raise ValueError("taskDetails.subject is required")
    body = task_details.get("body")
    priority = str(task_details.get("priority") or "HIGH").strip().upper()
    status = str(task_details.get("status") or "NOT_STARTED").strip().upper()
    due_raw = task_details.get("due_date", task_details.get("dueDate"))
    hs_timestamp = _due_date_to_unix_ms(due_raw, timezone=timezone)

    assoc_type = (
        ASSOC_TASK_TO_CONTACT if record_type_norm == "contact" else ASSOC_TASK_TO_COMPANY
    )
    properties: dict[str, str] = {
        "hs_task_subject": subject[:255],
        "hs_task_status": status,
        "hs_task_priority": priority,
        "hs_timestamp": hs_timestamp,
        "hubspot_owner_id": str(owner_id).strip(),
    }
    if body is not None and str(body).strip():
        properties["hs_task_body"] = str(body)[:65_000]

    payload: dict[str, Any] = {
        "properties": properties,
        "associations": [
            {
                "to": {"id": str(record_id).strip()},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": assoc_type,
                    }
                ],
            }
        ],
    }

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {str(access_token).strip()}",
            "Content-Type": "application/json",
        }
    )
    url = f"{HUBSPOT_BASE}/crm/v3/objects/tasks"
    try:
        response = session.post(url, json=payload, timeout=30)
    except requests.RequestException as e:
        raise HubSpotWriteError(f"HubSpot task request failed: {e}") from e

    if response.status_code == 401:
        detail = _hubspot_error_detail(response)
        raise HubSpotWriteError(
            f"HubSpot authentication failed (401). Check HUBSPOT_PRIVATE_APP_TOKEN "
            f"and private-app scopes (crm.objects.tasks.write). {detail}".strip(),
            status_code=401,
        )
    if response.status_code == 403:
        detail = _hubspot_error_detail(response)
        scopes = ", ".join(REQUIRED_WRITE_SCOPES)
        raise HubSpotWriteError(
            f"HubSpot write denied (403). Add scopes: {scopes}. {detail}".strip(),
            status_code=403,
        )
    if response.status_code == 400:
        detail = _hubspot_error_detail(response)
        raise HubSpotWriteError(
            f"HubSpot task validation error (400). Check subject/owner/associations/"
            f"hs_timestamp. {detail}".strip(),
            status_code=400,
        )
    if not response.ok:
        detail = _hubspot_error_detail(response)
        raise HubSpotWriteError(
            f"HubSpot task create failed ({response.status_code}). {detail}".strip(),
            status_code=response.status_code,
        )
    return response.json()


_DEAL_SEARCH_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "projekt",
        "inwestycja",
        "budowa",
        "realizacja",
        "etap",
        "faza",
        "część",
        "czesc",
        "part",
    }
)


def _deal_name_search_tokens(query: str) -> list[str]:
    """HubSpot CONTAINS_TOKEN expects single tokens, not full phrases.

    HubSpot deal search allows at most 5 filterGroups (OR branches); each token
    uses one group when combined with an optional pipeline filter.
    """
    raw = (query or "").strip()
    if not raw:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for match in re.findall(r"[\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+", raw, flags=re.UNICODE):
        t = match.strip().lower()
        if len(t) < 3 or t in _DEAL_SEARCH_STOPWORDS or t in seen:
            continue
        seen.add(t)
        tokens.append(t)
    if not tokens:
        fallback = re.sub(r"[^\w\s]", " ", raw, flags=re.UNICODE).split()
        if fallback:
            tokens = [fallback[0][:40].lower()]
    if not tokens:
        return []

    def _rank(token: str) -> tuple[int, int, str]:
        has_digits = 1 if re.search(r"\d", token) else 0
        return (has_digits, len(token), token)

    tokens.sort(key=_rank, reverse=True)
    return tokens[:5]


def _deal_search_filter_groups(
    tokens: list[str],
    *,
    pipeline_id: str | None,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for token in tokens:
        filters: list[dict[str, str]] = [
            {
                "propertyName": "dealname",
                "operator": "CONTAINS_TOKEN",
                "value": token,
            }
        ]
        if pipeline_id:
            filters.append(
                {
                    "propertyName": "pipeline",
                    "operator": "EQ",
                    "value": pipeline_id,
                }
            )
        groups.append({"filters": filters})
    return groups


@dataclass(frozen=True)
class HubSpotPushResult:
    deal_id: str
    company_id: str
    contact_id: str | None
    created_company: bool
    created_contact: bool
    created_deal: bool
    updated_company: bool = False
    updated_deal: bool = False
    task_id: str | None = None
    created_task: bool = False
    task_skipped_reason: str | None = None
    note_id: str | None = None
    created_note: bool = False
    note_hash: str | None = None
    dry_run: bool = False
    dropped_properties: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "deal_id": self.deal_id,
            "company_id": self.company_id,
            "contact_id": self.contact_id,
            "created_company": self.created_company,
            "updated_company": self.updated_company,
            "created_contact": self.created_contact,
            "created_deal": self.created_deal,
            "updated_deal": self.updated_deal,
            "task_id": self.task_id,
            "created_task": self.created_task,
            "task_skipped_reason": self.task_skipped_reason,
            "note_id": self.note_id,
            "created_note": self.created_note,
            "dropped_properties": list(self.dropped_properties),
            "dry_run": self.dry_run,
        }


class HubSpotClient:
    """HubSpot CRM client — company search (read) and lead push (write)."""

    def __init__(self, *, token: str) -> None:
        if not token:
            raise ValueError("HUBSPOT_PRIVATE_APP_TOKEN is required")
        self._s = requests.Session()
        self._s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        self._schemas: dict[str, PropertySchema] = {}

    def property_schema(self, object_type: str) -> PropertySchema:
        """Property definitions for an object type (cached, best-effort)."""
        cached = self._schemas.get(object_type)
        if cached is not None:
            return cached
        try:
            r = self._get(
                f"{HUBSPOT_BASE}/crm/v3/properties/{object_type}",
                params={"limit": 400},
                timeout=30,
            )
            r.raise_for_status()
            schema = PropertySchema.from_api_results(object_type, r.json().get("results", []))
        except Exception:
            schema = PropertySchema.unavailable(object_type)
        self._schemas[object_type] = schema
        return schema

    def _coerce_properties(
        self,
        object_type: str,
        props: dict[str, str],
    ) -> tuple[dict[str, str], list[DroppedProperty]]:
        return self.property_schema(object_type).coerce(props)

    @staticmethod
    def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(max(float(raw), 1.0), 60.0)
            except ValueError:
                pass
        return min(2**attempt, 30.0)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", 30)
        last: requests.Response | None = None
        for attempt in range(8):
            last = self._s.request(method, url, timeout=timeout, **kwargs)
            if last.status_code != 429:
                return last
            time.sleep(self._retry_after_seconds(last, attempt))
        assert last is not None
        return last

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        r = self._request("POST", url, **kwargs)
        if r.status_code == 429:
            raise HubSpotRateLimitError(
                "HubSpot rate limit (429) on POST. Wait and retry, or use link-hubspot --limit.",
                status_code=429,
            )
        return r

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        r = self._request("GET", url, **kwargs)
        if r.status_code == 429:
            raise HubSpotRateLimitError(
                "HubSpot rate limit (429) on GET. Wait and retry, or use link-hubspot --limit.",
                status_code=429,
            )
        return r

    def _patch(self, url: str, **kwargs: Any) -> requests.Response:
        r = self._request("PATCH", url, **kwargs)
        if r.status_code == 429:
            raise HubSpotRateLimitError(
                "HubSpot rate limit (429) on PATCH. Wait and retry.",
                status_code=429,
            )
        return r

    def _put(self, url: str, **kwargs: Any) -> requests.Response:
        r = self._request("PUT", url, **kwargs)
        if r.status_code == 429:
            raise HubSpotRateLimitError(
                "HubSpot rate limit (429) on PUT. Wait and retry.",
                status_code=429,
            )
        return r

    def search_company_candidates(self, *, name_query: str, limit: int = 5) -> list[HubSpotCompanyCandidate]:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/companies/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "name",
                            "operator": "CONTAINS_TOKEN",
                            "value": name_query,
                        }
                    ]
                }
            ],
            "properties": ["name", "domain", "country"],
            "limit": int(limit),
        }
        r = self._post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        out: list[HubSpotCompanyCandidate] = []
        for item in data.get("results", []):
            props = item.get("properties", {}) or {}
            out.append(
                HubSpotCompanyCandidate(
                    id=str(item.get("id")),
                    name=props.get("name"),
                    domain=props.get("domain"),
                    country=props.get("country"),
                )
            )
        return out

    def find_deal_by_exact_name(
        self,
        dealname: str,
        *,
        pipeline_id: str | None = None,
        limit: int = 5,
    ) -> list[HubSpotDealCandidate]:
        """Find deals whose dealname exactly matches (best for link-hubspot)."""
        name = (dealname or "").strip()
        if not name:
            return []
        filters: list[dict[str, str]] = [
            {
                "propertyName": "dealname",
                "operator": "EQ",
                "value": name[:255],
            }
        ]
        if pipeline_id:
            filters.append(
                {
                    "propertyName": "pipeline",
                    "operator": "EQ",
                    "value": pipeline_id,
                }
            )
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/search"
        payload = {
            "filterGroups": [{"filters": filters}],
            "properties": ["dealname", "description", "pipeline"],
            "limit": int(limit),
        }
        r = self._post(url, json=payload, timeout=30)
        if r.status_code == 400:
            detail = _hubspot_error_detail(r)
            raise HubSpotWriteError(
                f"HubSpot deal search validation error (400). {detail}".strip(),
                status_code=400,
            )
        r.raise_for_status()
        return self._parse_deal_results(r.json())

    def find_deals_by_project_url(
        self,
        project_url: str,
        *,
        prop_project_url: str,
        pipeline_id: str | None = None,
        limit: int = 10,
    ) -> list[HubSpotDealCandidate]:
        """Find deals with the same investment URL (Kompass / project page)."""
        prop = (prop_project_url or "").strip()
        raw = (project_url or "").strip()
        if not prop or not raw:
            return []

        # Try common URL variants (www / trailing slash) so adopt still works.
        variants: list[str] = []
        for candidate in (raw, raw.rstrip("/")):
            if candidate and candidate not in variants:
                variants.append(candidate)
            no_www = (
                candidate.replace("://www.", "://", 1)
                if "://www." in candidate
                else candidate
            )
            with_www = (
                candidate.replace("://", "://www.", 1)
                if "://" in candidate and "://www." not in candidate
                else candidate
            )
            for alt in (no_www, with_www, no_www.rstrip("/"), with_www.rstrip("/")):
                if alt and alt not in variants:
                    variants.append(alt)

        seen: set[str] = set()
        out: list[HubSpotDealCandidate] = []
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/search"
        pipe = pipeline_id
        for value in variants:
            filters: list[dict[str, str]] = [
                {
                    "propertyName": prop,
                    "operator": "EQ",
                    "value": value[:500],
                }
            ]
            if pipe:
                filters.append(
                    {
                        "propertyName": "pipeline",
                        "operator": "EQ",
                        "value": pipe,
                    }
                )
            payload = {
                "filterGroups": [{"filters": filters}],
                "properties": ["dealname", "description", "pipeline", prop],
                "limit": int(limit),
            }
            r = self._post(url, json=payload, timeout=30)
            if r.status_code == 400:
                if pipe:
                    pipe = None
                    continue
                detail = _hubspot_error_detail(r)
                raise HubSpotWriteError(
                    f"HubSpot deal URL search validation error (400). {detail}".strip(),
                    status_code=400,
                )
            self._raise_write_errors(r)
            r.raise_for_status()
            for deal in self._parse_deal_results(r.json(), prop_project_url=prop):
                if deal.id not in seen:
                    seen.add(deal.id)
                    out.append(deal)
            if out:
                break
        return out[: int(limit)]

    def search_deal_candidates(
        self,
        *,
        name_query: str,
        pipeline_id: str | None = None,
        limit: int = 10,
        prop_project_url: str | None = None,
        prop_stage: str | None = None,
    ) -> list[HubSpotDealCandidate]:
        """Search HubSpot deals by deal name tokens (OR across significant words)."""
        tokens = _deal_name_search_tokens(name_query)
        if not tokens:
            return []

        props = ["dealname", "description"]
        if prop_project_url:
            props.append(prop_project_url)
        if prop_stage:
            props.append(prop_stage)

        def _run_search(*, pipe: str | None) -> list[HubSpotDealCandidate]:
            filter_groups = _deal_search_filter_groups(tokens, pipeline_id=pipe)
            if not filter_groups:
                return []
            url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/search"
            payload = {
                "filterGroups": filter_groups,
                "properties": props,
                "limit": int(limit),
            }
            r = self._post(url, json=payload, timeout=30)
            if r.status_code == 400 and "too many filterGroups" in r.text:
                reduced_groups = filter_groups[:5]
                if len(reduced_groups) < len(filter_groups):
                    payload = {**payload, "filterGroups": reduced_groups}
                    r = self._post(url, json=payload, timeout=30)
            if r.status_code == 400:
                detail = _hubspot_error_detail(r)
                raise HubSpotWriteError(
                    f"HubSpot deal search validation error (400). {detail}".strip(),
                    status_code=400,
                )
            r.raise_for_status()
            return self._parse_deal_results(
                r.json(),
                prop_project_url=prop_project_url,
                prop_stage=prop_stage,
            )

        results = _run_search(pipe=pipeline_id)
        if not results and pipeline_id:
            results = _run_search(pipe=None)
        return results

    def list_recent_deals(
        self,
        *,
        limit: int = 5,
        pipeline_id: str | None = None,
        prop_project_url: str | None = None,
        prop_stage: str | None = None,
    ) -> list[HubSpotDealCandidate]:
        """List recent deals (no name filter) — verifies the deal corpus exists."""
        props = ["dealname", "description", "pipeline"]
        if prop_project_url:
            props.append(prop_project_url)
        if prop_stage:
            props.append(prop_stage)
        params: dict[str, Any] = {
            "limit": int(limit),
            "properties": ",".join(props),
        }
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals"
        r = self._get(url, params=params, timeout=30)
        r.raise_for_status()
        deals = self._parse_deal_results(
            r.json(),
            prop_project_url=prop_project_url,
            prop_stage=prop_stage,
        )
        if pipeline_id:
            in_pipeline = [d for d in deals if d.pipeline_id == pipeline_id]
            if in_pipeline:
                return in_pipeline
        return deals

    def list_deal_company_ids(self, deal_id: str, *, limit: int = 5) -> list[str]:
        """Company IDs associated with a HubSpot deal."""
        did = str(deal_id).strip()
        if not did:
            return []
        assoc_url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{did}/associations/companies"
        r = self._get(assoc_url, params={"limit": int(limit)}, timeout=30)
        r.raise_for_status()
        return [
            str(item.get("toObjectId"))
            for item in r.json().get("results", [])
            if item.get("toObjectId")
        ]

    def list_company_deals(
        self,
        company_id: str,
        *,
        limit: int = 20,
        prop_project_url: str | None = None,
        prop_stage: str | None = None,
    ) -> list[HubSpotDealCandidate]:
        """Deals associated with a HubSpot company."""
        cid = str(company_id).strip()
        if not cid:
            return []
        assoc_url = (
            f"{HUBSPOT_BASE}/crm/v4/objects/companies/{cid}/associations/deals"
        )
        r = self._get(assoc_url, params={"limit": int(limit)}, timeout=30)
        r.raise_for_status()
        deal_ids = [
            str(item.get("toObjectId"))
            for item in r.json().get("results", [])
            if item.get("toObjectId")
        ]
        if not deal_ids:
            return []
        return self._batch_read_deals(
            deal_ids[: int(limit)],
            company_id=cid,
            prop_project_url=prop_project_url,
            prop_stage=prop_stage,
        )

    def _batch_read_deals(
        self,
        deal_ids: list[str],
        *,
        company_id: str | None = None,
        prop_project_url: str | None = None,
        prop_stage: str | None = None,
    ) -> list[HubSpotDealCandidate]:
        props = ["dealname", "description"]
        if prop_project_url:
            props.append(prop_project_url)
        if prop_stage:
            props.append(prop_stage)
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/batch/read"
        payload = {
            "properties": props,
            "inputs": [{"id": did} for did in deal_ids],
        }
        r = self._post(url, json=payload, timeout=30)
        r.raise_for_status()
        return self._parse_deal_results(
            r.json(),
            company_id=company_id,
            prop_project_url=prop_project_url,
            prop_stage=prop_stage,
        )

    @staticmethod
    def _parse_deal_results(
        data: dict,
        *,
        company_id: str | None = None,
        prop_project_url: str | None = None,
        prop_stage: str | None = None,
    ) -> list[HubSpotDealCandidate]:
        out: list[HubSpotDealCandidate] = []
        for item in data.get("results", []):
            props = item.get("properties", {}) or {}
            out.append(
                HubSpotDealCandidate(
                    id=str(item.get("id")),
                    dealname=props.get("dealname"),
                    project_url=props.get(prop_project_url) if prop_project_url else None,
                    stage=props.get(prop_stage) if prop_stage else None,
                    description=props.get("description"),
                    company_id=company_id,
                    pipeline_id=props.get("pipeline"),
                )
            )
        return out

    def _ensure_hubspot_ok(self, response: requests.Response, *, action: str) -> None:
        if response.status_code in (401, 403):
            self._raise_write_errors(response)
        if response.status_code == 400:
            detail = _hubspot_error_detail(response)
            raise HubSpotWriteError(
                f"HubSpot {action} validation error (400). {detail}".strip(),
                status_code=400,
            )
        if not response.ok:
            detail = _hubspot_error_detail(response)
            raise HubSpotWriteError(
                f"HubSpot {action} failed ({response.status_code}). {detail}".strip(),
                status_code=response.status_code,
            )

    def create_company(self, properties: dict[str, str]) -> str:
        return self._create_object("companies", properties, standard_fallback=True)

    def update_company(self, company_id: str, properties: dict[str, str]) -> None:
        if not properties:
            return
        url = f"{HUBSPOT_BASE}/crm/v3/objects/companies/{company_id}"
        r = self._patch(url, json={"properties": properties}, timeout=30)
        if r.status_code == 400:
            bad = set(_invalid_property_names(r))
            if bad:
                reduced = {k: v for k, v in properties.items() if k not in bad}
                if reduced and reduced != properties:
                    r = self._patch(url, json={"properties": reduced}, timeout=30)
        self._ensure_hubspot_ok(r, action="company update")

    def create_contact(self, properties: dict[str, str]) -> str:
        """Create a contact, or update the existing one when the email is taken."""
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
        r = self._post(url, json={"properties": properties}, timeout=30)
        if r.status_code == 409:
            existing_id = _existing_object_id(r)
            if existing_id:
                self._patch(
                    f"{url}/{existing_id}",
                    json={"properties": properties},
                    timeout=30,
                )
                return existing_id
        if r.status_code == 400:
            bad = set(_invalid_property_names(r))
            if bad:
                reduced = {k: v for k, v in properties.items() if k not in bad}
                if reduced and reduced != properties:
                    r = self._post(url, json={"properties": reduced}, timeout=30)
        self._ensure_hubspot_ok(r, action="contacts create")
        obj_id = r.json().get("id")
        if not obj_id:
            raise HubSpotWriteError("HubSpot contacts create returned no id")
        return str(obj_id)

    def create_deal(self, properties: dict[str, str]) -> str:
        return self._create_object("deals", properties, standard_fallback=True)

    def update_deal(self, deal_id: str, properties: dict[str, str]) -> None:
        if not properties:
            return
        url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}"
        r = self._patch(url, json={"properties": properties}, timeout=30)
        if r.status_code == 400:
            bad = set(_invalid_property_names(r))
            if bad:
                reduced = {k: v for k, v in properties.items() if k not in bad}
                if reduced and reduced != properties:
                    r = self._patch(url, json={"properties": reduced}, timeout=30)
        self._ensure_hubspot_ok(r, action="deal update")

    def create_task(
        self,
        properties: dict[str, str],
        *,
        associations: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create a task; optionally associate deal/company/contact in the same request."""
        url = f"{HUBSPOT_BASE}/crm/v3/objects/tasks"
        payload: dict[str, Any] = {"properties": properties}
        if associations:
            payload["associations"] = associations
        r = self._post(url, json=payload, timeout=30)
        if r.status_code == 400:
            raise HubSpotWriteError(
                f"HubSpot task validation error (400). {_hubspot_error_detail(r)}".strip(),
                status_code=400,
            )
        if r.status_code == 401:
            raise HubSpotWriteError(
                f"HubSpot authentication failed (401). {_hubspot_error_detail(r)}".strip(),
                status_code=401,
            )
        self._raise_write_errors(r)
        r.raise_for_status()
        data = r.json()
        obj_id = data.get("id")
        if not obj_id:
            raise HubSpotWriteError("HubSpot tasks create returned no id")
        return str(obj_id)

    def create_note(
        self,
        body: str,
        *,
        deal_id: str,
        company_id: str | None = None,
        contact_id: str | None = None,
    ) -> str:
        """Create a timeline Note and associate it with the deal (and optionally company/contact)."""
        from datetime import timezone as dt_timezone

        url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
        timestamp = datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        associations: list[dict[str, Any]] = [
            {
                "to": {"id": str(deal_id)},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": ASSOC_NOTE_TO_DEAL,
                    }
                ],
            }
        ]
        if company_id:
            associations.append(
                {
                    "to": {"id": str(company_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": ASSOC_NOTE_TO_COMPANY,
                        }
                    ],
                }
            )
        if contact_id:
            associations.append(
                {
                    "to": {"id": str(contact_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": ASSOC_NOTE_TO_CONTACT,
                        }
                    ],
                }
            )
        payload = {
            "properties": {
                "hs_timestamp": timestamp,
                "hs_note_body": body[:65_000],
            },
            "associations": associations,
        }
        r = self._post(url, json=payload, timeout=30)
        if r.status_code in (400, 401, 403):
            raise HubSpotWriteError(
                f"HubSpot notes create failed ({r.status_code}). {_hubspot_error_detail(r)}".strip(),
                status_code=r.status_code,
            )
        self._raise_write_errors(r)
        r.raise_for_status()
        obj_id = r.json().get("id")
        if not obj_id:
            raise HubSpotWriteError("HubSpot notes create returned no id")
        return str(obj_id)

    def associate_default(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
    ) -> None:
        url = (
            f"{HUBSPOT_BASE}/crm/v4/objects/{from_type}/{from_id}"
            f"/associations/default/{to_type}/{to_id}"
        )
        r = self._put(url, timeout=30)
        self._raise_write_errors(r)
        r.raise_for_status()

    def push_lead_export(
        self,
        lead: Lead,
        enrichment: EnrichmentResult | None,
        decision: FuzzyDuplicateDecision | None,
        *,
        write_config: HubSpotWriteConfig,
        followup_config: HubSpotFollowUpConfig | None = None,
        task_config: HubSpotFollowUpConfig | None = None,
        project_similarity: ProjectSimilarityDecision | None = None,
        project_similarity_min_score: int = 60,
        dry_run: bool = False,
        resync_company_id: str | None = None,
        resync_deal_id: str | None = None,
        previous_note_hash: str | None = None,
        previous_task_id: str | None = None,
    ) -> HubSpotPushResult:
        """Create/link Company, optional Contact, Deal, and optional Task for one export."""
        is_duplicate = bool(decision and decision.is_duplicate)
        company_props = build_company_properties(
            lead,
            enrichment,
            write_config=write_config,
            decision=decision,
            is_duplicate=is_duplicate,
            project_similarity=project_similarity,
            project_similarity_min_score=project_similarity_min_score,
        )
        contact_props = build_contact_properties(lead, enrichment)
        deal_props = build_deal_properties(
            lead,
            enrichment,
            decision,
            write_config=write_config,
            is_duplicate=is_duplicate,
            project_similarity=project_similarity,
            project_similarity_min_score=project_similarity_min_score,
        )
        existing_deal_id = resolve_existing_deal_id(
            project_similarity,
            min_score=project_similarity_min_score,
            update_enabled=write_config.update_existing_deals,
        )
        deal_update_props = (
            build_deal_properties(
                lead,
                enrichment,
                decision,
                write_config=write_config,
                is_duplicate=is_duplicate,
                project_similarity=project_similarity,
                project_similarity_min_score=project_similarity_min_score,
                for_update=True,
            )
            if existing_deal_id or resync_deal_id
            else None
        )

        dropped: list[DroppedProperty] = []
        if not dry_run:
            company_props, company_dropped = self._coerce_properties("companies", company_props)
            dropped.extend(company_dropped)
            deal_props, deal_dropped = self._coerce_properties("deals", deal_props)
            dropped.extend(deal_dropped)
            if deal_update_props is not None:
                deal_update_props, update_dropped = self._coerce_properties(
                    "deals", deal_update_props
                )
                for item in update_dropped:
                    if item.property_name not in {d.property_name for d in dropped}:
                        dropped.append(item)
        dropped_payload = tuple(item.as_dict() for item in dropped)

        note_body = build_analysis_note_body(
            lead,
            enrichment,
            decision,
            is_duplicate=is_duplicate,
            project_similarity=project_similarity,
            project_similarity_min_score=project_similarity_min_score,
        )
        note_hash = analysis_note_hash(note_body) if note_body else None
        want_note = bool(
            write_config.create_analysis_note
            and note_body
            and note_hash
            and note_hash != (previous_note_hash or "").strip()
        )

        followup_cfg = followup_config or task_config or HubSpotFollowUpConfig(
            enabled=False,
            when="always",
            owner_id=None,
            task_type="CALL",
            due_days=7,
            timezone="Europe/Warsaw",
        )
        would_create_contact = bool(contact_props and has_personal_contact_data(enrichment))
        # Create a follow-up once: on first push, or when adopting/resyncing a deal
        # that never received a SkySnap task yet.
        want_task = should_create_hubspot_followup(
            followup_config=followup_cfg,
            created_contact=would_create_contact,
        ) and not (previous_task_id or "").strip()
        task_props = (
            build_task_properties(
                lead,
                enrichment,
                decision,
                followup_config=followup_cfg,
                is_duplicate=is_duplicate,
                project_similarity=project_similarity,
                project_similarity_min_score=project_similarity_min_score,
            )
            if want_task
            else None
        )
        task_skipped_reason: str | None = None
        if want_task and not task_props:
            task_skipped_reason = "HUBSPOT_TASK_OWNER_ID not set"

        if resync_company_id and resync_deal_id:
            if dry_run:
                return HubSpotPushResult(
                    deal_id=resync_deal_id,
                    company_id=resync_company_id,
                    contact_id="dry-run-contact" if would_create_contact else None,
                    created_company=False,
                    updated_company=True,
                    created_contact=would_create_contact,
                    created_deal=False,
                    updated_deal=True,
                    task_id="dry-run-task" if task_props else None,
                    created_task=bool(task_props),
                    task_skipped_reason=task_skipped_reason,
                    note_id="dry-run-note" if want_note else None,
                    created_note=want_note,
                    note_hash=note_hash if want_note else previous_note_hash,
                    dry_run=True,
                )
            self.update_company(resync_company_id, company_props)
            if deal_update_props:
                self.update_deal(resync_deal_id, deal_update_props)
            contact_id: str | None = None
            if contact_props and has_personal_contact_data(enrichment):
                contact_id = self.create_contact(contact_props)
                self.associate_default("contacts", contact_id, "companies", resync_company_id)
                self.associate_default("deals", resync_deal_id, "contacts", contact_id)
            task_id: str | None = None
            created_task = False
            if task_props:
                associations = build_task_associations(
                    deal_id=resync_deal_id,
                    company_id=resync_company_id,
                    contact_id=contact_id,
                )
                task_id = self.create_task(task_props, associations=associations)
                created_task = True
            note_id: str | None = None
            created_note = False
            if want_note and note_body:
                try:
                    note_id = self.create_note(
                        note_body,
                        deal_id=resync_deal_id,
                        company_id=resync_company_id,
                        contact_id=contact_id,
                    )
                    created_note = True
                except HubSpotWriteError:
                    # Missing crm.objects.notes.write must not fail the whole push.
                    note_id = None
                    created_note = False
            return HubSpotPushResult(
                deal_id=resync_deal_id,
                company_id=resync_company_id,
                contact_id=contact_id,
                created_company=False,
                updated_company=True,
                created_contact=bool(contact_id),
                created_deal=False,
                updated_deal=True,
                task_id=task_id,
                created_task=created_task,
                task_skipped_reason=task_skipped_reason,
                note_id=note_id,
                created_note=created_note,
                note_hash=note_hash if created_note else previous_note_hash,
                dry_run=False,
                dropped_properties=dropped_payload,
            )

        existing_company_id = resolve_company_id(decision)

        if dry_run:
            return HubSpotPushResult(
                deal_id=existing_deal_id or "dry-run-deal",
                company_id=existing_company_id or "dry-run-company",
                contact_id="dry-run-contact" if contact_props else None,
                created_company=not existing_company_id,
                updated_company=bool(existing_company_id),
                created_contact=bool(contact_props),
                created_deal=not existing_deal_id,
                updated_deal=bool(existing_deal_id),
                task_id="dry-run-task" if task_props else None,
                created_task=bool(task_props),
                task_skipped_reason=task_skipped_reason,
                note_id="dry-run-note" if want_note else None,
                created_note=want_note,
                note_hash=note_hash if want_note else previous_note_hash,
                dry_run=True,
            )

        created_company = False
        updated_company = False
        if existing_company_id:
            company_id = existing_company_id
            self.update_company(company_id, company_props)
            updated_company = True
        else:
            company_id = self.create_company(company_props)
            created_company = True

        contact_id = None
        created_contact = False
        if contact_props and has_personal_contact_data(enrichment):
            contact_id = self.create_contact(contact_props)
            created_contact = True
            self.associate_default("contacts", contact_id, "companies", company_id)

        created_deal = False
        updated_deal = False
        if existing_deal_id and deal_update_props is not None:
            deal_id = existing_deal_id
            self.update_deal(deal_id, deal_update_props)
            updated_deal = True
        else:
            deal_id = self.create_deal(deal_props)
            created_deal = True

        self.associate_default("deals", deal_id, "companies", company_id)
        if contact_id:
            self.associate_default("deals", deal_id, "contacts", contact_id)

        task_id: str | None = None
        created_task = False
        if task_props:
            associations = build_task_associations(
                deal_id=deal_id,
                company_id=company_id,
                contact_id=contact_id,
            )
            task_id = self.create_task(task_props, associations=associations)
            created_task = True

        note_id: str | None = None
        created_note = False
        if want_note and note_body:
            try:
                note_id = self.create_note(
                    note_body,
                    deal_id=deal_id,
                    company_id=company_id,
                    contact_id=contact_id,
                )
                created_note = True
            except HubSpotWriteError:
                note_id = None
                created_note = False

        return HubSpotPushResult(
            deal_id=deal_id,
            company_id=company_id,
            contact_id=contact_id,
            created_company=created_company,
            updated_company=updated_company,
            created_contact=created_contact,
            created_deal=created_deal,
            updated_deal=updated_deal,
            task_id=task_id,
            created_task=created_task,
            task_skipped_reason=task_skipped_reason,
            note_id=note_id,
            created_note=created_note,
            note_hash=note_hash if created_note else previous_note_hash,
            dry_run=False,
            dropped_properties=dropped_payload,
        )

    def _create_object(
        self,
        object_type: str,
        properties: dict[str, str],
        *,
        standard_fallback: bool = False,
    ) -> str:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}"
        r = self._post(url, json={"properties": properties}, timeout=30)
        if r.status_code == 400:
            bad = set(_invalid_property_names(r))
            if bad:
                reduced = {k: v for k, v in properties.items() if k not in bad}
                if reduced and reduced != properties:
                    r = self._post(url, json={"properties": reduced}, timeout=30)
            elif standard_fallback:
                allowed = (
                    HUBSPOT_STANDARD_COMPANY_PROPERTIES
                    if object_type == "companies"
                    else HUBSPOT_STANDARD_DEAL_PROPERTIES
                    if object_type == "deals"
                    else frozenset(properties)
                )
                reduced = {k: v for k, v in properties.items() if k in allowed}
                if reduced and reduced != properties:
                    r = self._post(url, json={"properties": reduced}, timeout=30)
        self._ensure_hubspot_ok(r, action=f"{object_type} create")
        data = r.json()
        obj_id = data.get("id")
        if not obj_id:
            raise HubSpotWriteError(f"HubSpot {object_type} create returned no id")
        return str(obj_id)

    def _raise_write_errors(self, response: requests.Response) -> None:
        if response.status_code not in (401, 403):
            return
        scopes = ", ".join(REQUIRED_WRITE_SCOPES)
        detail = ""
        try:
            detail = response.json().get("message", "")
        except Exception:
            detail = response.text[:200]
        raise HubSpotWriteError(
            f"HubSpot write denied ({response.status_code}). "
            f"Add scopes to your private app token: {scopes}. {detail}".strip(),
            status_code=response.status_code,
        )
