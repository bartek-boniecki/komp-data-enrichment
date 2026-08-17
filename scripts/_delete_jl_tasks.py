"""Dry-run / delete HubSpot tasks owned by Jean-luc (SkySnap follow-ups only).

Usage:
  python scripts/_delete_jl_tasks.py              # dry-run (default)
  python scripts/_delete_jl_tasks.py --delete     # delete matched tasks
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from skysnap.config import load_settings
from skysnap.hubspot import HUBSPOT_BASE, HubSpotClient

OWNER_ID = "34040248"  # Jean-luc Momprive (HUBSPOT_TASK_OWNER_ID)
SUBJECT_PREFIX = "Skontaktuj się:"


def _search_owner_tasks(hs: HubSpotClient, owner_id: str) -> list[dict]:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/tasks/search"
    rows: list[dict] = []
    after: str | None = None
    while True:
        body: dict = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hubspot_owner_id",
                            "operator": "EQ",
                            "value": owner_id,
                        }
                    ]
                }
            ],
            "properties": [
                "hs_task_subject",
                "hubspot_owner_id",
                "hs_task_status",
                "hs_task_type",
                "hs_createdate",
            ],
            "limit": 100,
        }
        if after:
            body["after"] = after
        r = hs._post(url, json=body, timeout=60)
        r.raise_for_status()
        data = r.json()
        rows.extend(data.get("results", []))
        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete matched tasks (default is dry-run)",
    )
    parser.add_argument(
        "--owner-id",
        default=OWNER_ID,
        help=f"HubSpot owner id (default {OWNER_ID})",
    )
    parser.add_argument(
        "--subject-prefix",
        default=SUBJECT_PREFIX,
        help="Only match subjects with this prefix (default SkySnap follow-up)",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = load_settings()
    if not settings.hubspot_private_app_token:
        print("HUBSPOT_PRIVATE_APP_TOKEN missing", file=sys.stderr)
        return 1

    hs = HubSpotClient(token=settings.hubspot_private_app_token)
    raw = _search_owner_tasks(hs, args.owner_id)

    matched: list[dict] = []
    other: list[dict] = []
    for row in raw:
        props = row.get("properties") or {}
        subj = props.get("hs_task_subject") or ""
        item = {
            "id": str(row["id"]),
            "subject": subj,
            "status": props.get("hs_task_status"),
            "type": props.get("hs_task_type"),
            "created": props.get("hs_createdate"),
            "owner": props.get("hubspot_owner_id"),
        }
        if args.subject_prefix and not subj.startswith(args.subject_prefix):
            other.append(item)
        else:
            matched.append(item)

    print("=== HubSpot task cleanup ===")
    print(f"Owner ID: {args.owner_id}")
    print(f"Subject prefix: {args.subject_prefix!r}")
    print(f"Mode: {'DELETE' if args.delete else 'DRY-RUN (nothing deleted)'}")
    print()
    print(f"Total tasks owned by this user: {len(raw)}")
    print(f"Matched (would delete):         {len(matched)}")
    print(f"Other under same owner (kept):  {len(other)}")
    print()
    print("Matched status:", dict(Counter(t["status"] for t in matched)))
    print("Matched type:  ", dict(Counter(t["type"] for t in matched)))
    print()
    print("Sample matched (first 12):")
    for t in matched[:12]:
        print(f"  {t['id']}  [{t['status']}]  {t['subject'][:110]}")
    if other:
        print()
        print("Sample NON-matched kept (first 8):")
        for t in other[:8]:
            print(f"  {t['id']}  [{t['status']}]  {t['subject'][:110]}")

    out = Path("data/_jl_tasks_dryrun.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "owner_id": args.owner_id,
                "subject_prefix": args.subject_prefix,
                "matched_count": len(matched),
                "other_count": len(other),
                "matched": matched,
                "other": other,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"Saved details to {out}")

    if not args.delete:
        print()
        print("Re-run with --delete after confirming the matched count.")
        return 0

    token = settings.hubspot_private_app_token
    headers = {"Authorization": f"Bearer {token}"}
    deleted = 0
    failed: list[tuple[str, str]] = []
    for t in matched:
        tid = t["id"]
        r = requests.delete(
            f"{HUBSPOT_BASE}/crm/v3/objects/tasks/{tid}",
            headers=headers,
            timeout=30,
        )
        if r.status_code in (200, 204):
            deleted += 1
        elif r.status_code == 429:
            time.sleep(2.0)
            r = requests.delete(
                f"{HUBSPOT_BASE}/crm/v3/objects/tasks/{tid}",
                headers=headers,
                timeout=30,
            )
            if r.status_code in (200, 204):
                deleted += 1
            else:
                failed.append((tid, f"{r.status_code} {r.text[:200]}"))
        else:
            failed.append((tid, f"{r.status_code} {r.text[:200]}"))
        if deleted and deleted % 25 == 0:
            print(f"  deleted {deleted}/{len(matched)}")
            time.sleep(0.25)

    print()
    print(f"Deleted {deleted}/{len(matched)} task(s).")
    if failed:
        print(f"Failed {len(failed)}:")
        for tid, err in failed[:10]:
            print(f"  {tid}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
