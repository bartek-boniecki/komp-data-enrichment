from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re

from dotenv import load_dotenv


def _env(key: str, default: str | None = None) -> str | None:
    v = os.getenv(key)
    if v is None:
        return default
    v = v.strip()
    if v == "":
        return default
    return v


def _resolve_path(raw: str, *, base_dir: Path) -> str:
    path = Path(raw)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


@dataclass(frozen=True)
class Settings:
    db_path: str
    daily_limit: int
    min_score: int
    stakeholder_export_min_icp: int
    user_agent: str

    blocked_contact_emails: frozenset[str]
    blocked_contact_email_domains: frozenset[str]
    blocked_contact_phones: frozenset[str]
    blocked_contact_website_hosts: frozenset[str]

    anthropic_api_key: str
    claude_model: str
    nvidia_api_key: str | None
    nvidia_nim_model: str

    imap_host: str | None
    imap_port: int
    imap_username: str | None
    imap_password: str | None
    imap_folder: str
    imap_search_query: str

    hubspot_private_app_token: str | None
    hubspot_push_enabled: bool
    hubspot_deal_pipeline_id: str | None
    hubspot_deal_stage_id: str | None
    hubspot_prop_project_url: str | None
    hubspot_prop_project_name: str | None
    hubspot_prop_icp_score: str | None
    hubspot_prop_leads_origin: str | None
    hubspot_prop_stage_inwestycji: str | None
    hubspot_prop_deal_typ: str | None
    hubspot_prop_deal_source: str | None
    hubspot_prop_deal_branza: str | None
    hubspot_prop_deal_role: str | None
    hubspot_prop_nip: str | None
    hubspot_prop_opis: str | None
    hubspot_sync_company_fields: bool
    hubspot_update_existing_deals: bool
    hubspot_company_owner_id: str | None
    hubspot_prop_branza_skysnap: str | None
    hubspot_prop_branza_extrainfo: str | None
    hubspot_prop_leads_score: str | None
    hubspot_prop_ai_score: str | None
    hubspot_prop_company_notes: str | None
    hubspot_prop_uslugi: str | None
    hubspot_prop_typ: str | None
    hubspot_prop_konkurencja: str | None
    hubspot_prop_konkurencja_expiry: str | None
    hubspot_prop_voivodeship: str | None
    hubspot_prop_sektor_podsektor: str | None
    hubspot_prop_project_city: str | None
    hubspot_prop_project_voivodeship: str | None
    hubspot_prop_project_street: str | None
    hubspot_prop_project_building_number: str | None
    hubspot_create_analysis_note: bool
    hubspot_create_task: bool
    hubspot_task_when: str
    hubspot_task_owner_id: str | None
    hubspot_task_type: str
    hubspot_task_due_days: int
    hubspot_ticket_pipeline_id: str | None
    hubspot_ticket_stage_id: str | None

    google_service_account_json: str | None
    google_sheet_id: str | None
    google_sheet_tab_name: str

    kompass_username: str | None
    kompass_password: str | None
    kompass_base_url: str
    kompass_login_path: str
    kompass_browser_state_dir: str
    kompass_headless: bool

    timezone: str
    osint_daily_cap: int | None
    osint_max_subpages: int
    email_mx_check: bool
    email_pattern_guess: bool
    gus_bir_api_key: str | None

    claude_usage_log_dir: str
    claude_input_price_per_mtok: float
    claude_output_price_per_mtok: float

    project_similarity_enabled: bool
    project_similarity_min_score: int


def load_settings(dotenv_path: str | None = ".env") -> Settings:
    if dotenv_path:
        # Prefer values from the project's .env over stale shell env vars.
        load_dotenv(dotenv_path=dotenv_path, override=True)

    base_dir = Path(dotenv_path).resolve().parent if dotenv_path else Path.cwd()
    db_path = _env("SKYSNAP_DB_PATH", "./data/skysnap.sqlite") or "./data/skysnap.sqlite"
    db_path = _resolve_path(db_path, base_dir=base_dir)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _csv_lower(raw: str | None) -> set[str]:
        return {p.strip().lower() for p in (raw or "").split(",") if p.strip()}

    def _csv_phones(raw: str | None) -> set[str]:
        out: set[str] = set()
        for p in (raw or "").split(","):
            digits = re.sub(r"\D", "", p)
            if digits:
                out.add(digits[-9:] if len(digits) > 9 else digits)
        return out

    # Auto-block the logged-in Kompass account (it renders on every page).
    kompass_username = _env("KOMPASS_USERNAME")
    self_emails: set[str] = set()
    self_domains: set[str] = set()
    if kompass_username and "@" in kompass_username:
        self_emails.add(kompass_username.strip().lower())
        self_domains.add(kompass_username.split("@", 1)[-1].strip().lower())

    return Settings(
        db_path=db_path,
        daily_limit=int(_env("SKYSNAP_DAILY_LIMIT", "5") or "5"),
        min_score=int(_env("SKYSNAP_MIN_SCORE", "40") or "40"),
        stakeholder_export_min_icp=int(
            _env("SKYSNAP_STAKEHOLDER_EXPORT_MIN_ICP", "60") or "60"
        ),
        user_agent=_env("SKYSNAP_USER_AGENT", "SkySnapLeadBot/1.0") or "SkySnapLeadBot/1.0",
        blocked_contact_emails=frozenset(
            self_emails | _csv_lower(_env("SKYSNAP_BLOCKED_EMAILS"))
        ),
        blocked_contact_email_domains=frozenset(
            self_domains | _csv_lower(_env("SKYSNAP_BLOCKED_EMAIL_DOMAINS"))
        ),
        blocked_contact_phones=frozenset(_csv_phones(_env("SKYSNAP_BLOCKED_PHONES"))),
        blocked_contact_website_hosts=frozenset(
            self_domains | _csv_lower(_env("SKYSNAP_BLOCKED_WEBSITE_HOSTS"))
        ),
        anthropic_api_key=_env("ANTHROPIC_API_KEY", "") or "",
        claude_model=_env("SKYSNAP_CLAUDE_MODEL", "claude-sonnet-4-20250514") or "claude-sonnet-4-20250514",
        nvidia_api_key=_env("NVIDIA_API_KEY"),
        nvidia_nim_model=_env("NVIDIA_NIM_MODEL", "meta/llama-3.3-70b-instruct")
        or "meta/llama-3.3-70b-instruct",
        imap_host=_env("IMAP_HOST"),
        imap_port=int(_env("IMAP_PORT", "993") or "993"),
        imap_username=_env("IMAP_USERNAME"),
        imap_password=_env("IMAP_PASSWORD"),
        imap_folder=_env("IMAP_FOLDER", "INBOX") or "INBOX",
        imap_search_query=_env("IMAP_SEARCH_QUERY", '(UNSEEN SUBJECT "Kompass")')
        or '(UNSEEN SUBJECT "Kompass")',
        hubspot_private_app_token=_env("HUBSPOT_PRIVATE_APP_TOKEN"),
        hubspot_push_enabled=(_env("SKYSNAP_HUBSPOT_PUSH_ENABLED", "false") or "false").lower()
        not in ("0", "false", "no"),
        hubspot_deal_pipeline_id=_env("HUBSPOT_DEAL_PIPELINE_ID"),
        hubspot_deal_stage_id=_env("HUBSPOT_DEAL_STAGE_ID"),
        hubspot_prop_project_url=_env("HUBSPOT_PROP_PROJECT_URL"),
        hubspot_prop_project_name=_env("HUBSPOT_PROP_PROJECT_NAME"),
        hubspot_prop_icp_score=_env("HUBSPOT_PROP_ICP_SCORE"),
        hubspot_prop_leads_origin=_env("HUBSPOT_PROP_LEADS_ORIGIN"),
        hubspot_prop_stage_inwestycji=_env("HUBSPOT_PROP_STAGE_INWESTYCJI"),
        hubspot_prop_deal_typ=_env("HUBSPOT_PROP_DEAL_TYP"),
        hubspot_prop_deal_source=_env("HUBSPOT_PROP_DEAL_SOURCE"),
        hubspot_prop_deal_branza=_env("HUBSPOT_PROP_DEAL_BRANZA"),
        hubspot_prop_deal_role=_env("HUBSPOT_PROP_DEAL_ROLE"),
        hubspot_prop_nip=_env("HUBSPOT_PROP_NIP"),
        hubspot_prop_opis=_env("HUBSPOT_PROP_OPIS"),
        hubspot_sync_company_fields=(
            _env("SKYSNAP_HUBSPOT_SYNC_COMPANY_FIELDS", "true") or "true"
        ).lower()
        not in ("0", "false", "no"),
        hubspot_update_existing_deals=(
            _env("SKYSNAP_HUBSPOT_UPDATE_EXISTING_DEALS", "true") or "true"
        ).lower()
        not in ("0", "false", "no"),
        hubspot_company_owner_id=_env("HUBSPOT_COMPANY_OWNER_ID"),
        hubspot_prop_branza_skysnap=_env("HUBSPOT_PROP_BRANZA_SKYSNAP"),
        hubspot_prop_branza_extrainfo=_env("HUBSPOT_PROP_BRANZA_EXTRAINFO"),
        hubspot_prop_leads_score=_env("HUBSPOT_PROP_LEADS_SCORE"),
        hubspot_prop_ai_score=_env("HUBSPOT_PROP_AI_SCORE"),
        hubspot_prop_company_notes=_env("HUBSPOT_PROP_COMPANY_NOTES"),
        hubspot_prop_uslugi=_env("HUBSPOT_PROP_USLUGI"),
        hubspot_prop_typ=_env("HUBSPOT_PROP_TYP"),
        hubspot_prop_konkurencja=_env("HUBSPOT_PROP_KONKURENCJA"),
        hubspot_prop_konkurencja_expiry=_env("HUBSPOT_PROP_KONKURENCJA_EXPIRY"),
        hubspot_prop_voivodeship=_env("HUBSPOT_PROP_VOIVODSHIP"),
        hubspot_prop_sektor_podsektor=_env(
            "HUBSPOT_PROP_SEKTOR_PODSEKTOR", "sektor_podsektor"
        ),
        hubspot_prop_project_city=_env(
            "HUBSPOT_PROP_PROJECT_CITY", "wspol_miasto_budynku"
        ),
        hubspot_prop_project_voivodeship=_env(
            "HUBSPOT_PROP_PROJECT_VOIVODSHIP", "wspol_wojewodztwo"
        ),
        hubspot_prop_project_street=_env(
            "HUBSPOT_PROP_PROJECT_STREET", "wspol_ulica_budynku"
        ),
        hubspot_prop_project_building_number=_env(
            "HUBSPOT_PROP_PROJECT_BUILDING_NUMBER", "wspol_numer_budynku"
        ),
        hubspot_create_analysis_note=(
            _env("SKYSNAP_HUBSPOT_CREATE_ANALYSIS_NOTE", "true") or "true"
        ).lower()
        not in ("0", "false", "no"),
        hubspot_create_task=(_env("SKYSNAP_HUBSPOT_CREATE_TASK", "true") or "true").lower()
        not in ("0", "false", "no"),
        hubspot_task_when=(_env("SKYSNAP_HUBSPOT_TASK_WHEN", "always") or "always").lower(),
        hubspot_task_owner_id=_env("HUBSPOT_TASK_OWNER_ID"),
        hubspot_task_type=(_env("HUBSPOT_TASK_TYPE", "CALL") or "CALL").upper(),
        hubspot_task_due_days=int(_env("HUBSPOT_TASK_DUE_DAYS", "7") or "7"),
        hubspot_ticket_pipeline_id=_env("HUBSPOT_TICKET_PIPELINE_ID"),
        hubspot_ticket_stage_id=_env("HUBSPOT_TICKET_STAGE_ID"),
        google_service_account_json=_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
        google_sheet_id=_env("GOOGLE_SHEET_ID"),
        google_sheet_tab_name=_env("GOOGLE_SHEET_TAB_NAME", "Leads") or "Leads",
        kompass_username=_env("KOMPASS_USERNAME"),
        kompass_password=_env("KOMPASS_PASSWORD"),
        kompass_base_url=_env("KOMPASS_BASE_URL", "https://www.kompasinwestycji.pl")
        or "https://www.kompasinwestycji.pl",
        kompass_login_path=_env("KOMPASS_LOGIN_PATH", "/zaloguj") or "/zaloguj",
        kompass_browser_state_dir=_env("KOMPASS_BROWSER_STATE_DIR", "./data/kompass_browser_state")
        or "./data/kompass_browser_state",
        kompass_headless=(_env("KOMPASS_HEADLESS", "true") or "true").lower()
        not in ("0", "false", "no"),
        timezone=_env("SKYSNAP_TIMEZONE", "Europe/Warsaw") or "Europe/Warsaw",
        osint_daily_cap=int(v)
        if (v := _env("SKYSNAP_OSINT_DAILY_CAP")) and v.strip().isdigit()
        else None,
        osint_max_subpages=int(_env("SKYSNAP_OSINT_MAX_SUBPAGES", "4") or "4"),
        email_mx_check=(_env("SKYSNAP_EMAIL_MX_CHECK", "true") or "true").lower()
        not in ("0", "false", "no"),
        email_pattern_guess=(_env("SKYSNAP_EMAIL_PATTERN_GUESS", "true") or "true").lower()
        not in ("0", "false", "no"),
        gus_bir_api_key=_env("GUS_BIR_API_KEY"),
        claude_usage_log_dir=_env("SKYSNAP_CLAUDE_USAGE_LOG_DIR", "./data/logs") or "./data/logs",
        claude_input_price_per_mtok=float(_env("CLAUDE_PRICE_INPUT_PER_MTOK", "3.0") or "3.0"),
        claude_output_price_per_mtok=float(_env("CLAUDE_PRICE_OUTPUT_PER_MTOK", "15.0") or "15.0"),
        project_similarity_enabled=(_env("SKYSNAP_PROJECT_SIMILARITY_ENABLED", "true") or "true").lower()
        not in ("0", "false", "no"),
        project_similarity_min_score=int(
            _env("SKYSNAP_PROJECT_SIMILARITY_MIN_SCORE", "0") or "0"
        ),
    )

