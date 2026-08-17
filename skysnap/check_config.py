from __future__ import annotations

from pathlib import Path
from typing import Any

from anthropic import Anthropic, AuthenticationError, NotFoundError
from dotenv import dotenv_values

from skysnap.claude_usage import usage_tracker_from_settings
from skysnap.config import Settings
from skysnap.hubspot import HubSpotClient
from skysnap.imap_ingest import fetch_unseen_html_emails
from skysnap.kompass import kompass_client_from_settings
from skysnap.nim import DEFAULT_NIM_MODEL, is_anthropic_unavailable_error, nim_ping
from skysnap.sheets import GoogleSheetsClient


def check_config(settings: Settings, *, dotenv_path: str = ".env") -> dict[str, Any]:
    report: dict[str, Any] = {}

    env_file = Path(dotenv_path).resolve()
    report["env_file"] = str(env_file)
    file_key = (dotenv_values(env_file).get("ANTHROPIC_API_KEY") or "").strip()
    if file_key:
        report["env_file_key_prefix"] = file_key[:12]
        report["env_file_key_length"] = len(file_key)

    key = settings.anthropic_api_key
    if not key:
        report["anthropic"] = {"ok": False, "error": "ANTHROPIC_API_KEY is empty in .env"}
    else:
        report["anthropic"] = {
            "key_present": True,
            "key_length": len(key),
            "key_prefix": key[:12],
            "model": settings.claude_model,
        }
        try:
            client = Anthropic(api_key=key)
            msg = client.messages.create(
                model=settings.claude_model,
                max_tokens=8,
                messages=[{"role": "user", "content": "ping"}],
            )
            report["anthropic"]["ok"] = True
            if getattr(msg, "usage", None) is not None:
                tracker = usage_tracker_from_settings(settings, command="check-config")
                tracker.record(
                    "check_config_ping",
                    input_tokens=int(msg.usage.input_tokens),
                    output_tokens=int(msg.usage.output_tokens),
                )
                report["claude_usage_session"] = tracker.write_session_summary()
                report["claude_usage_today"] = tracker.read_daily_totals()
        except AuthenticationError:
            report["anthropic"]["ok"] = False
            report["anthropic"]["error"] = (
                "Key rejected (401 invalid x-api-key). Create a new key at "
                "https://console.anthropic.com/settings/keys - copy once, paste into .env "
                "with no quotes. Revoke old keys if this one was exposed."
            )
        except NotFoundError:
            report["anthropic"]["ok"] = False
            report["anthropic"]["error"] = (
                f"Model not found: {settings.claude_model!r}. "
                "Set SKYSNAP_CLAUDE_MODEL=claude-sonnet-4-20250514 in .env and save."
            )
        except Exception as e:
            report["anthropic"]["ok"] = False
            report["anthropic"]["error"] = f"{type(e).__name__}: {e}"
            if is_anthropic_unavailable_error(e):
                report["anthropic"]["fallback_eligible"] = True
                report["anthropic"]["hint"] = (
                    "Claude quota/billing limit reached. Set NVIDIA_API_KEY in .env to "
                    "run the pipeline on NVIDIA NIM until Claude access is restored."
                )

    nim_key = (settings.nvidia_api_key or "").strip()
    if not nim_key:
        report["nvidia_nim"] = {"ok": None, "skipped": "NVIDIA_API_KEY not set"}
    else:
        nim_model = settings.nvidia_nim_model or DEFAULT_NIM_MODEL
        report["nvidia_nim"] = {
            "key_present": True,
            "key_length": len(nim_key),
            "key_prefix": nim_key[:12],
            "model": nim_model,
        }
        try:
            nim_ping(api_key=nim_key, model=nim_model)
            report["nvidia_nim"]["ok"] = True
        except Exception as e:
            report["nvidia_nim"]["ok"] = False
            report["nvidia_nim"]["error"] = f"{type(e).__name__}: {e}"

    llm_ok = bool(report.get("anthropic", {}).get("ok")) or bool(
        report.get("nvidia_nim", {}).get("ok")
    )
    if report.get("anthropic", {}).get("ok"):
        report["llm_provider"] = "anthropic"
    elif report.get("nvidia_nim", {}).get("ok"):
        report["llm_provider"] = "nvidia_nim"
    else:
        report["llm_provider"] = None
    report["llm_ok"] = llm_ok

    if settings.imap_host and settings.imap_username and settings.imap_password:
        try:
            _, imap_meta = fetch_unseen_html_emails(
                host=settings.imap_host,
                port=settings.imap_port,
                username=settings.imap_username,
                password=settings.imap_password,
                folder=settings.imap_folder,
                search_query=settings.imap_search_query,
                mark_seen=False,
            )
            report["imap"] = {"ok": True, **imap_meta}
        except Exception as e:
            report["imap"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        report["imap"] = {"ok": False, "error": "IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD not fully set"}

    if settings.hubspot_private_app_token:
        try:
            hs = HubSpotClient(token=settings.hubspot_private_app_token)
            candidates = hs.search_company_candidates(name_query="test", limit=1)
            hubspot_report: dict[str, Any] = {
                "ok": True,
                "sample_company_results": len(candidates),
            }
            if settings.project_similarity_enabled:
                recent = hs.list_recent_deals(
                    limit=5,
                    pipeline_id=settings.hubspot_deal_pipeline_id or None,
                    prop_project_url=settings.hubspot_prop_project_url,
                    prop_stage=settings.hubspot_prop_stage_inwestycji,
                )
                hubspot_report["deals_read_ok"] = True
                hubspot_report["deal_corpus_sample"] = len(recent)
                hubspot_report["sample_deal_names"] = [
                    d.dealname for d in recent if d.dealname
                ][:3]
                search_hits = hs.search_deal_candidates(
                    name_query="KI",
                    pipeline_id=settings.hubspot_deal_pipeline_id or None,
                    limit=3,
                    prop_project_url=settings.hubspot_prop_project_url,
                    prop_stage=settings.hubspot_prop_stage_inwestycji,
                )
                hubspot_report["sample_deal_search_results"] = len(search_hits)
                if not recent:
                    hubspot_report["deal_similarity_note"] = (
                        "No deals found in HubSpot. Deal Similarity will show 0% until "
                        "deals exist (run push-hubspot on exported leads, or create deals manually)."
                    )
                elif not search_hits:
                    hubspot_report["deal_similarity_note"] = (
                        "Deals exist but name search returned 0 for token 'KI'. "
                        "Check deal naming (expected prefix 'KI:') or HUBSPOT_DEAL_PIPELINE_ID."
                    )
                if settings.hubspot_deal_pipeline_id:
                    hubspot_report["configured_pipeline_id"] = settings.hubspot_deal_pipeline_id
            report["hubspot"] = hubspot_report
        except Exception as e:
            report["hubspot"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        report["hubspot"] = {"ok": None, "skipped": "HUBSPOT_PRIVATE_APP_TOKEN not set"}

    if settings.hubspot_push_enabled:
        missing: list[str] = []
        if not settings.hubspot_private_app_token:
            missing.append("HUBSPOT_PRIVATE_APP_TOKEN")
        if not settings.hubspot_deal_pipeline_id:
            missing.append("HUBSPOT_DEAL_PIPELINE_ID")
        if not settings.hubspot_deal_stage_id:
            missing.append("HUBSPOT_DEAL_STAGE_ID")
        report["hubspot_push"] = {
            "enabled": True,
            "ready": not missing,
            "missing": missing,
            "create_task": settings.hubspot_create_task,
            "tasks_ready": (
                not settings.hubspot_create_task
                or bool((settings.hubspot_task_owner_id or "").strip())
            ),
            "task_when": settings.hubspot_task_when,
            "task_due_days": settings.hubspot_task_due_days,
            "note": (
                "Write scopes cannot be verified without a live create call; "
                "ensure crm.objects.companies.read, companies.write, contacts.write, "
                "deals.read, deals.write, crm.objects.tasks.write"
            ),
        }
        if settings.hubspot_create_task and not (settings.hubspot_task_owner_id or "").strip():
            report["hubspot_push"]["task_warning"] = (
                "HUBSPOT_TASK_OWNER_ID not set; follow-up tasks will be skipped"
            )
    else:
        report["hubspot_push"] = {"enabled": False, "ready": None}

    if settings.google_service_account_json and settings.google_sheet_id:
        try:
            sheets = GoogleSheetsClient(service_account_json_path=settings.google_service_account_json)
            sheets.ensure_header(
                spreadsheet_id=settings.google_sheet_id,
                tab_name=settings.google_sheet_tab_name,
            )
            sheets.verify_write_access(
                spreadsheet_id=settings.google_sheet_id,
                tab_name=settings.google_sheet_tab_name,
            )
            report["google_sheets"] = {
                "ok": True,
                "spreadsheet_id": settings.google_sheet_id,
                "tab_name": settings.google_sheet_tab_name,
                "service_account_email": sheets.service_account_email,
            }
        except ValueError as e:
            report["google_sheets"] = {"ok": False, "error": str(e)}
        except Exception as e:
            report["google_sheets"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        report["google_sheets"] = {
            "ok": None,
            "skipped": "GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SHEET_ID not set",
        }

    if settings.kompass_username and settings.kompass_password:
        try:
            client = kompass_client_from_settings(settings)
            client.verify_login()
            report["kompass"] = {
                "ok": True,
                "base_url": settings.kompass_base_url,
                "login_path": settings.kompass_login_path,
            }
        except Exception as e:
            report["kompass"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        report["kompass"] = {"ok": None, "skipped": "KOMPASS_USERNAME/KOMPASS_PASSWORD not set"}

    required_ok = [
        llm_ok,
        bool(report.get("imap", {}).get("ok")),
    ]
    if settings.kompass_username and settings.kompass_password:
        required_ok.append(bool(report.get("kompass", {}).get("ok")))
    gs = report.get("google_sheets", {})
    if settings.google_service_account_json and settings.google_sheet_id:
        required_ok.append(bool(gs.get("ok")))
    report["all_ok"] = all(required_ok)
    return report
