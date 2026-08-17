"""HubSpot SkySnap cleanup: verify counts, then delete in safe order.

Default is dry-run. Pass --execute to actually delete.

Steps:
  1. Tasks owned by Jean-luc (expect ~13)
  2. Deals with name starting KI: and/or Kompass URL
  3. Companies owned by Jean-luc with SkySnap markers (expect ~137)
  4. Orphan contacts that were associated to those deals/companies

Usage:
  python scripts/_cleanup_hubspot_skysnap.py
  python scripts/_cleanup_hubspot_skysnap.py --execute
  python scripts/_cleanup_hubspot_skysnap.py --execute --step tasks
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

from skysnap.config import load_settings
from skysnap.hubspot import HUBSPOT_BASE, HubSpotClient

OWNER_ID = "34040248"  # Jean-luc Momprive
PROP_NOTES = "komentarz_wewnetrzny"
PROP_ORIGIN = "leads_orygin"
PROP_URL = "strona_inwestycji"


def log(msg: str) -> None:
    print(msg, flush=True)


def _search_all(
    hs: HubSpotClient,
    object_type: str,
    *,
    filters: list[dict],
    properties: list[str],
    label: str,
) -> list[dict]:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}/search"
    after: str | None = None
    out: list[dict] = []
    page = 0
    while True:
        page += 1
        body: dict = {
            "filterGroups": [{"filters": filters}],
            "properties": properties,
            "limit": 100,
        }
        if after:
            body["after"] = after
        r = hs._post(url, json=body, timeout=60)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After") or "3")
            log(f"  rate-limited {label}, sleep {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 400:
            # Some portals reject multi-filter combos; surface detail.
            raise RuntimeError(f"{label} search 400: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        batch = data.get("results") or []
        out.extend(batch)
        log(f"  {label} page {page}: +{len(batch)} (total {len(out)})")
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after or not batch:
            break
        time.sleep(0.12)
    return out


def _delete_ids(
    token: str,
    object_type: str,
    ids: list[str],
    *,
    label: str,
    execute: bool,
) -> tuple[int, list[tuple[str, str]]]:
    if not ids:
        return 0, []
    if not execute:
        log(f"[dry-run] would delete {len(ids)} {label}")
        return 0, []
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    deleted = 0
    failed: list[tuple[str, str]] = []
    # HubSpot batch archive: max 100
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        r = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}/batch/archive",
            headers=headers,
            json={"inputs": [{"id": x} for x in chunk]},
            timeout=60,
        )
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After") or "3"))
            r = requests.post(
                f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}/batch/archive",
                headers=headers,
                json={"inputs": [{"id": x} for x in chunk]},
                timeout=60,
            )
        if r.status_code in (200, 204):
            deleted += len(chunk)
            log(f"  deleted {deleted}/{len(ids)} {label}")
        else:
            # Fallback per-id
            for oid in chunk:
                dr = requests.delete(
                    f"{HUBSPOT_BASE}/crm/v3/objects/{object_type}/{oid}",
                    headers=headers,
                    timeout=30,
                )
                if dr.status_code in (200, 204):
                    deleted += 1
                else:
                    failed.append((oid, f"{dr.status_code} {dr.text[:160]}"))
            log(f"  deleted {deleted}/{len(ids)} {label} (fallback)")
        time.sleep(0.2)
    return deleted, failed


def collect_tasks(hs: HubSpotClient) -> list[dict]:
    rows = _search_all(
        hs,
        "tasks",
        filters=[{"propertyName": "hubspot_owner_id", "operator": "EQ", "value": OWNER_ID}],
        properties=["hs_task_subject", "hubspot_owner_id", "hs_task_status", "hs_createdate"],
        label="tasks-owner",
    )
    out = []
    for row in rows:
        p = row.get("properties") or {}
        out.append(
            {
                "id": str(row["id"]),
                "subject": p.get("hs_task_subject"),
                "status": p.get("hs_task_status"),
                "created": p.get("hs_createdate"),
            }
        )
    return out


def collect_deals(
    hs: HubSpotClient,
    *,
    company_ids: list[str] | None = None,
    created_after: str | None = "2026-07-01T00:00:00.000Z",
) -> list[dict]:
    """SkySnap-Agent deals only — NOT every historical KI: deal in the portal.

    Collects:
      A) deals associated to Jean-luc's SkySnap companies
      B) deals with Kompass URL created on/after created_after (agent era)
      C) deals whose description contains 'SkySnap lead_id='
    """
    props = ["dealname", "createdate", PROP_URL, "description"]
    by_id: dict[str, dict] = {}

    def _add(row: dict, *, reason: str) -> None:
        p = row.get("properties") or {}
        did = str(row["id"])
        if did in by_id:
            by_id[did].setdefault("reasons", [])
            if reason not in by_id[did]["reasons"]:
                by_id[did]["reasons"].append(reason)
            return
        by_id[did] = {
            "id": did,
            "dealname": (p.get("dealname") or "").strip(),
            "created": p.get("createdate"),
            "project_url": (p.get(PROP_URL) or "").strip() or None,
            "reasons": [reason],
        }

    # A) deals on JL companies
    company_ids = company_ids or []
    log(f"  collecting deals linked to {len(company_ids)} companies...")
    for i, cid in enumerate(company_ids, 1):
        for deal in hs.list_company_deals(cid, limit=50, prop_project_url=PROP_URL):
            # list_company_deals returns HubSpotDealCandidate — re-fetch props lightly via stub
            by_id[deal.id] = {
                "id": deal.id,
                "dealname": (deal.dealname or "").strip(),
                "created": None,
                "project_url": deal.project_url,
                "reasons": ["assoc_company"],
            }
        if i % 25 == 0:
            log(f"  company→deals {i}/{len(company_ids)} (unique deals {len(by_id)})")
            time.sleep(0.05)
        else:
            time.sleep(0.03)

    # Batch-fill createdate for assoc deals missing it
    missing = [d for d in by_id.values() if not d.get("created")]
    for i in range(0, len(missing), 100):
        chunk = missing[i : i + 100]
        r = hs._post(
            f"{HUBSPOT_BASE}/crm/v3/objects/deals/batch/read",
            json={
                "properties": props,
                "inputs": [{"id": d["id"]} for d in chunk],
            },
            timeout=60,
        )
        r.raise_for_status()
        for item in r.json().get("results") or []:
            p = item.get("properties") or {}
            did = str(item["id"])
            if did in by_id:
                by_id[did]["created"] = p.get("createdate")
                by_id[did]["dealname"] = (p.get("dealname") or by_id[did]["dealname"]).strip()
                by_id[did]["project_url"] = (p.get(PROP_URL) or "").strip() or by_id[did].get(
                    "project_url"
                )
                desc = p.get("description") or ""
                if "SkySnap lead_id=" in desc and "skysnap_desc" not in by_id[did]["reasons"]:
                    by_id[did]["reasons"].append("skysnap_desc")
        time.sleep(0.15)

    # B) Kompass URL deals created in agent era
    filters = [
        {
            "propertyName": PROP_URL,
            "operator": "CONTAINS_TOKEN",
            "value": "kompasinwestycji",
        }
    ]
    if created_after:
        filters.append(
            {
                "propertyName": "createdate",
                "operator": "GTE",
                "value": created_after,
            }
        )
    try:
        for row in _search_all(
            hs,
            "deals",
            filters=filters,
            properties=props,
            label="deals-kompass-recent",
        ):
            _add(row, reason="kompass_recent")
    except RuntimeError as exc:
        log(f"  warn kompass+date search failed ({exc}); trying without date filter + client filter")
        for row in _search_all(
            hs,
            "deals",
            filters=[
                {
                    "propertyName": PROP_URL,
                    "operator": "CONTAINS_TOKEN",
                    "value": "kompasinwestycji",
                }
            ],
            properties=props,
            label="deals-kompass-all",
        ):
            p = row.get("properties") or {}
            created = p.get("createdate") or ""
            if created_after and created < created_after:
                continue
            _add(row, reason="kompass_recent")

    # C) description contains SkySnap lead_id (CONTAINS_TOKEN on lead_id may be weak;
    #    search KI: recent and filter client-side)
    ki_filters = [
        {"propertyName": "dealname", "operator": "CONTAINS_TOKEN", "value": "KI"},
    ]
    if created_after:
        ki_filters.append(
            {
                "propertyName": "createdate",
                "operator": "GTE",
                "value": created_after,
            }
        )
    try:
        for row in _search_all(
            hs,
            "deals",
            filters=ki_filters,
            properties=props,
            label="deals-ki-recent",
        ):
            p = row.get("properties") or {}
            name = (p.get("dealname") or "").strip()
            desc = p.get("description") or ""
            if not name.upper().startswith("KI:"):
                continue
            if "SkySnap lead_id=" in desc or (p.get(PROP_URL) or ""):
                _add(row, reason="ki_recent_skysnap")
            elif created_after and (p.get("createdate") or "") >= created_after:
                # Recent KI: without URL still likely agent if after cutover
                _add(row, reason="ki_recent")
    except RuntimeError as exc:
        log(f"  warn KI+date search failed: {exc}")

    # Keep only deals that are clearly agent-linked (not every historical KI: name).
    filtered: list[dict] = []
    for d in by_id.values():
        reasons = set(d.get("reasons") or [])
        if reasons & {"assoc_company", "kompass_recent", "skysnap_desc", "ki_recent_skysnap"}:
            filtered.append(d)
        elif "ki_recent" in reasons:
            # Recent KI: without Kompass URL / company link — only if created with JL companies era
            created = d.get("created") or ""
            if created >= "2026-07-20T00:00:00.000Z":
                filtered.append(d)
    return sorted(filtered, key=lambda d: d.get("created") or "")


def collect_companies(hs: HubSpotClient) -> list[dict]:
    """Companies owned by Jean-luc. User asserts all are agent-created."""
    rows = _search_all(
        hs,
        "companies",
        filters=[{"propertyName": "hubspot_owner_id", "operator": "EQ", "value": OWNER_ID}],
        properties=["name", "hubspot_owner_id", "createdate", PROP_NOTES, PROP_ORIGIN],
        label="companies-owner",
    )
    out = []
    for row in rows:
        p = row.get("properties") or {}
        notes = p.get(PROP_NOTES) or ""
        out.append(
            {
                "id": str(row["id"]),
                "name": p.get("name"),
                "created": p.get("createdate"),
                "origin": p.get(PROP_ORIGIN),
                "has_skysnap_lead_id": "SkySnap lead_id=" in notes,
                "notes_prefix": notes[:80],
            }
        )
    return out


def _assoc_ids(hs: HubSpotClient, from_type: str, from_id: str, to_type: str) -> list[str]:
    r = hs._get(
        f"{HUBSPOT_BASE}/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}",
        params={"limit": 100},
        timeout=30,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [
        str(item.get("toObjectId"))
        for item in (r.json().get("results") or [])
        if item.get("toObjectId")
    ]


def collect_orphan_contacts(
    hs: HubSpotClient,
    *,
    deal_ids: list[str],
    company_ids: list[str],
    created_after: str = "2026-07-20T00:00:00.000Z",
) -> list[dict]:
    """Contacts associated to deals/companies we are deleting.

    Only keeps contacts created on/after created_after so we do not delete
    pre-existing CRM people who were merely associated to a SkySnap deal.
    """
    contact_ids: set[str] = set()
    sources = [("deals", deal_ids), ("companies", company_ids)]
    for obj_type, ids in sources:
        for i, oid in enumerate(ids, 1):
            for cid in _assoc_ids(hs, obj_type, oid, "contacts"):
                contact_ids.add(cid)
            if i % 50 == 0:
                log(f"  scanned {i}/{len(ids)} {obj_type} for contacts")
                time.sleep(0.05)
            else:
                time.sleep(0.03)
    out: list[dict] = []
    skipped_old = 0
    ids_list = sorted(contact_ids)
    for i in range(0, len(ids_list), 100):
        chunk = ids_list[i : i + 100]
        if not chunk:
            continue
        r = hs._post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/batch/read",
            json={
                "properties": ["email", "firstname", "lastname", "createdate"],
                "inputs": [{"id": x} for x in chunk],
            },
            timeout=60,
        )
        r.raise_for_status()
        for item in r.json().get("results") or []:
            p = item.get("properties") or {}
            created = p.get("createdate") or ""
            if created_after and created < created_after:
                skipped_old += 1
                continue
            out.append(
                {
                    "id": str(item["id"]),
                    "email": p.get("email"),
                    "name": f"{p.get('firstname') or ''} {p.get('lastname') or ''}".strip(),
                    "created": created,
                }
            )
        time.sleep(0.15)
    log(f"  contacts associated total={len(ids_list)}; agent-era kept={len(out)}; skipped older={skipped_old}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete (default: dry-run verify only)",
    )
    parser.add_argument(
        "--step",
        choices=["all", "tasks", "deals", "companies", "contacts"],
        default="all",
        help="Which step to run",
    )
    parser.add_argument(
        "--expect-tasks",
        type=int,
        default=13,
        help="Abort execute if task count != this (default 13)",
    )
    parser.add_argument(
        "--expect-companies",
        type=int,
        default=137,
        help="Abort execute if company count != this (default 137)",
    )
    parser.add_argument(
        "--allow-count-mismatch",
        action="store_true",
        help="Execute even if task/company counts differ from expected",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = load_settings()
    if not settings.hubspot_private_app_token:
        log("HUBSPOT_PRIVATE_APP_TOKEN missing")
        return 1

    hs = HubSpotClient(token=settings.hubspot_private_app_token)
    token = settings.hubspot_private_app_token
    execute = args.execute
    step = args.step

    report: dict = {"mode": "EXECUTE" if execute else "DRY-RUN", "owner_id": OWNER_ID}

    need_tasks = step in ("all", "tasks")
    need_deals = step in ("all", "deals", "contacts")
    need_companies = step in ("all", "companies", "contacts", "deals")
    need_contacts = step in ("all", "contacts")

    tasks: list[dict] = []
    deals: list[dict] = []
    companies: list[dict] = []
    contacts: list[dict] = []

    if need_tasks:
        log("=" * 60)
        log("1) TASKS owned by Jean-luc")
        tasks = collect_tasks(hs)
        report["tasks"] = {"count": len(tasks), "items": tasks}
        log(f"Found {len(tasks)} task(s) (expected {args.expect_tasks})")
        for t in tasks[:20]:
            log(f"  {t['id']}  [{t['status']}]  {(t['subject'] or '')[:100]}")
        if len(tasks) > 20:
            log(f"  ... +{len(tasks) - 20} more")

    if need_companies:
        log("=" * 60)
        log("3) COMPANIES owned by Jean-luc")
        companies = collect_companies(hs)
        report["companies"] = {
            "count": len(companies),
            "with_skysnap_lead_id": sum(1 for c in companies if c["has_skysnap_lead_id"]),
            "sample": companies[:30],
        }
        log(
            f"Found {len(companies)} company(ies) (expected {args.expect_companies}); "
            f"with SkySnap lead_id in notes: {report['companies']['with_skysnap_lead_id']}"
        )
        for c in companies[:15]:
            mark = " [SkySnap]" if c["has_skysnap_lead_id"] else ""
            log(f"  {c['id']}  {(c.get('created') or '')[:10]}  {c['name']}{mark}")
        if len(companies) > 15:
            log(f"  ... +{len(companies) - 15} more")

    if need_deals:
        log("=" * 60)
        log("2) DEALS (linked to JL companies / recent Kompass / SkySnap markers)")
        log("   NOTE: refusing to delete all historical KI: deals (1000+ pre-agent).")
        deals = collect_deals(hs, company_ids=[c["id"] for c in companies])
        report["deals"] = {"count": len(deals), "sample": deals[:30]}
        log(f"Found {len(deals)} agent deal(s)")
        for d in deals[:15]:
            reasons = ",".join(d.get("reasons") or [])
            log(
                f"  {d['id']}  {(d.get('created') or '')[:10]}  "
                f"{d['dealname'][:80]}  [{reasons}]"
            )
        if len(deals) > 15:
            log(f"  ... +{len(deals) - 15} more")

    if need_contacts:
        log("=" * 60)
        log("4) CONTACTS linked to those deals/companies (collect BEFORE delete)")
        contacts = collect_orphan_contacts(
            hs,
            deal_ids=[d["id"] for d in deals],
            company_ids=[c["id"] for c in companies],
        )
        report["contacts"] = {"count": len(contacts), "sample": contacts[:40]}
        log(f"Found {len(contacts)} associated contact(s)")
        for c in contacts[:20]:
            log(f"  {c['id']}  {c.get('email') or '-'}  {c.get('name') or ''}")
        if len(contacts) > 20:
            log(f"  ... +{len(contacts) - 20} more")

    # Persist inventory before any delete
    out = Path("data/_hubspot_cleanup_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    slim = {
        "mode": report["mode"],
        "owner_id": OWNER_ID,
        "tasks": {
            "count": len(tasks),
            "ids": [t["id"] for t in tasks],
            "items": tasks[:50],
        },
        "deals": {
            "count": len(deals),
            "ids": [d["id"] for d in deals],
            "sample": deals[:30],
        },
        "companies": {
            "count": len(companies),
            "with_skysnap_lead_id": sum(1 for c in companies if c.get("has_skysnap_lead_id")),
            "ids": [c["id"] for c in companies],
            "sample": companies[:30],
        },
        "contacts": {
            "count": len(contacts),
            "ids": [c["id"] for c in contacts],
            "sample": contacts[:40],
        },
    }
    out.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    log("")
    log(f"Inventory written to {out}")

    if not execute:
        log("")
        log("SUMMARY (dry-run — nothing deleted)")
        log(f"  tasks:     {len(tasks)} (expect {args.expect_tasks})")
        log(f"  deals:     {len(deals)}")
        log(f"  companies: {len(companies)} (expect {args.expect_companies})")
        log(f"  contacts:  {len(contacts)}")
        log("")
        log("If counts look right, run:")
        log("  python scripts/_cleanup_hubspot_skysnap.py --execute")
        return 0

    # --- execute deletes in order: tasks → contacts → deals → companies ---
    if need_tasks:
        if len(tasks) != args.expect_tasks and not args.allow_count_mismatch:
            log(
                f"ABORT: task count {len(tasks)} != expected {args.expect_tasks}. "
                "Pass --allow-count-mismatch if intentional."
            )
            return 2
        log("=" * 60)
        log("DELETE tasks")
        deleted, failed = _delete_ids(
            token, "tasks", [t["id"] for t in tasks], label="tasks", execute=True
        )
        slim["tasks"]["deleted"] = deleted
        slim["tasks"]["failed"] = failed
        log(f"Deleted tasks: {deleted}; failed: {len(failed)}")

    if need_contacts:
        log("=" * 60)
        log("DELETE contacts")
        deleted, failed = _delete_ids(
            token, "contacts", [c["id"] for c in contacts], label="contacts", execute=True
        )
        slim["contacts"]["deleted"] = deleted
        slim["contacts"]["failed"] = failed
        log(f"Deleted contacts: {deleted}; failed: {len(failed)}")

    if step in ("all", "deals"):
        log("=" * 60)
        log("DELETE deals")
        deleted, failed = _delete_ids(
            token, "deals", [d["id"] for d in deals], label="deals", execute=True
        )
        slim["deals"]["deleted"] = deleted
        slim["deals"]["failed"] = failed
        log(f"Deleted deals: {deleted}; failed: {len(failed)}")

    if step in ("all", "companies"):
        if len(companies) != args.expect_companies and not args.allow_count_mismatch:
            log(
                f"ABORT: company count {len(companies)} != expected {args.expect_companies}. "
                "Pass --allow-count-mismatch if intentional."
            )
            return 2
        log("=" * 60)
        log("DELETE companies")
        deleted, failed = _delete_ids(
            token,
            "companies",
            [c["id"] for c in companies],
            label="companies",
            execute=True,
        )
        slim["companies"]["deleted"] = deleted
        slim["companies"]["failed"] = failed
        log(f"Deleted companies: {deleted}; failed: {len(failed)}")

    out.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    log("")
    log(f"Updated report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
