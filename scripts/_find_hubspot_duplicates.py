"""Find SkySnap HubSpot deal duplicates by shared Kompass URL / bare deal names."""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict

from skysnap.config import load_settings
from skysnap.hubspot import HUBSPOT_BASE, HubSpotClient

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    print(msg, flush=True)


def _norm_url(url: str | None) -> str:
    u = (url or "").strip().lower().rstrip("/")
    if "://" in u:
        scheme, rest = u.split("://", 1)
        if rest.startswith("www."):
            rest = rest[4:]
        return f"{scheme}://{rest}"
    return u


def _search_deals(hs: HubSpotClient, *, props: list[str], filters: list[dict], label: str) -> list[dict]:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals/search"
    after: str | None = None
    out: list[dict] = []
    page = 0
    while True:
        page += 1
        payload: dict = {
            "filterGroups": [{"filters": filters}],
            "properties": props,
            "limit": 100,
            "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
        }
        if after:
            payload["after"] = after
        r = hs._post(url, json=payload, timeout=60)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After") or "5")
            log(f"  rate-limited on {label}, sleep {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        batch = data.get("results") or []
        out.extend(batch)
        log(f"  {label} page {page}: +{len(batch)} (total {len(out)})")
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after or not batch:
            break
        if len(out) >= 5000:
            break
        time.sleep(0.12)
    return out


def _parse_deal(item: dict, *, prop_url: str, prop_name: str) -> dict | None:
    p = item.get("properties") or {}
    name = (p.get("dealname") or "").strip()
    if not name.upper().startswith("KI:"):
        return None
    return {
        "id": str(item.get("id")),
        "dealname": name,
        "createdate": p.get("createdate"),
        "project_url": (p.get(prop_url) or "").strip() or None,
        "project_name": (p.get(prop_name) or "").strip() or None,
    }


def _is_bare(deal: dict) -> bool:
    pname = (deal.get("project_name") or "").strip()
    if not pname:
        # Heuristic: "KI: Something" with no comma after KI prefix often means no firm
        dname = deal.get("dealname") or ""
        # Firm-prefixed: "KI: Fortuna, Project..." — has ", " after first token
        rest = dname[3:].strip() if dname.upper().startswith("KI:") else dname
        return "," not in rest
    bare = f"KI: {pname}".strip().lower()
    return (deal.get("dealname") or "").strip().lower() == bare


def main() -> None:
    s = load_settings()
    prop_url = (s.hubspot_prop_project_url or "strona_inwestycji").strip()
    prop_name = (s.hubspot_prop_project_name or "nazwa_inwestycji").strip()
    hs = HubSpotClient(token=s.hubspot_private_app_token)
    props = ["dealname", "createdate", prop_url, prop_name]

    log("Searching deals with Kompass URL...")
    raw = _search_deals(
        hs,
        props=props,
        filters=[
            {
                "propertyName": prop_url,
                "operator": "CONTAINS_TOKEN",
                "value": "kompasinwestycji",
            }
        ],
        label="kompass-url",
    )

    deals: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        parsed = _parse_deal(item, prop_url=prop_url, prop_name=prop_name)
        if parsed and parsed["id"] not in seen:
            deals.append(parsed)
            seen.add(parsed["id"])

    log(f"KI: deals with Kompass URL: {len(deals)}")

    by_url: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        nu = _norm_url(d.get("project_url"))
        if nu:
            by_url[nu].append(d)

    url_dup_groups = {u: rows for u, rows in by_url.items() if len(rows) > 1}
    log(f"Unique URLs: {len(by_url)}  |  duplicate groups: {len(url_dup_groups)}")

    report: list[dict] = []
    agent_like_bad: list[dict] = []  # bare deal next to firm-prefixed siblings

    for url, rows in sorted(url_dup_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        enriched = []
        for r in sorted(rows, key=lambda x: x.get("createdate") or ""):
            bare = _is_bare(r)
            enriched.append(
                {
                    "id": r["id"],
                    "dealname": r["dealname"],
                    "createdate": r.get("createdate"),
                    "project_name": r.get("project_name"),
                    "is_bare_dealname": bare,
                }
            )
        bare_n = sum(1 for d in enriched if d["is_bare_dealname"])
        firm_n = len(enriched) - bare_n
        entry = {
            "project_url": rows[0].get("project_url"),
            "norm_url": url,
            "deal_count": len(rows),
            "bare_deal_count": bare_n,
            "firm_prefixed_count": firm_n,
            "deals": enriched,
        }
        report.append(entry)
        # Classic agent bug: firm deal exists, then bare/project-named duplicate added
        if bare_n and firm_n:
            agent_like_bad.append(entry)
        elif bare_n >= 2:
            agent_like_bad.append(entry)
        elif len(enriched) >= 2 and firm_n >= 2:
            # Multiple firm-prefixed on same URL (Fortuna + Bednarska) — stakeholder variants
            entry["note"] = "multiple_stakeholders_same_url"
            # still a dup group but may be intentional
            pass

    # Single-URL bare deals (company likely named after project) — list top recent
    bare_singles = []
    for rows in by_url.values():
        if len(rows) != 1:
            continue
        r = rows[0]
        if _is_bare(r):
            bare_singles.append(r)
    bare_singles.sort(key=lambda x: x.get("createdate") or "", reverse=True)

    log("")
    log("=" * 72)
    log("LIKELY AGENT BAD DUPLICATES (bare deal + firm-prefixed sibling on same URL)")
    log("=" * 72)
    classic = [g for g in agent_like_bad if g["bare_deal_count"] and g["firm_prefixed_count"]]
    if not classic:
        log("(none)")
    for i, g in enumerate(classic, 1):
        log(f"\n[{i}] {g['deal_count']} deals — {g['project_url']}")
        for d in g["deals"]:
            flag = "  << BARE (likely bad)" if d["is_bare_dealname"] else "  (keep / firm-prefixed)"
            created = (d["createdate"] or "?")[:10]
            log(f"    - {d['id']}  {created}  {d['dealname']}{flag}")

    log("")
    log("=" * 72)
    log("OTHER URL DUPLICATES (multiple firm-prefixed / multiple bare only)")
    log("=" * 72)
    other = [g for g in report if g not in classic]
    multi_firm = [g for g in other if g["firm_prefixed_count"] >= 2 and g["bare_deal_count"] == 0]
    multi_bare = [g for g in other if g["bare_deal_count"] >= 2 and g["firm_prefixed_count"] == 0]
    log(f"Multiple firm-prefixed same URL (often different stakeholders): {len(multi_firm)}")
    log(f"Multiple bare same URL: {len(multi_bare)}")
    for i, g in enumerate(multi_bare[:30], 1):
        log(f"\n[bare-dup {i}] {g['deal_count']} — {g['project_url']}")
        for d in g["deals"]:
            created = (d["createdate"] or "?")[:10]
            log(f"    - {d['id']}  {created}  {d['dealname']}")
    if len(multi_bare) > 30:
        log(f"  ... and {len(multi_bare) - 30} more bare-only dup groups")

    log("")
    log("=" * 72)
    log("SAMPLE: recent single-URL bare KI deals (possible bad company name)")
    log("=" * 72)
    for r in bare_singles[:25]:
        created = (r.get("createdate") or "?")[:10]
        log(f"- {r['id']}  {created}  {r['dealname']}")
        log(f"  {r.get('project_url')}")
    if len(bare_singles) > 25:
        log(f"... and {len(bare_singles) - 25} more bare singles")

    log("")
    log("=" * 72)
    log("SUMMARY")
    log("=" * 72)
    log(f"KI deals with Kompass URL: {len(deals)}")
    log(f"URL duplicate groups (any): {len(report)}")
    log(f"Classic bad pattern (bare + firm sibling): {len(classic)}")
    bare_in_classic = sum(
        1 for g in classic for d in g["deals"] if d["is_bare_dealname"]
    )
    log(f"Bare deals to review/delete in classic groups: {bare_in_classic}")
    log(f"Multi firm-prefixed same URL: {len(multi_firm)}")
    log(f"Multi bare same URL: {len(multi_bare)}")
    log(f"Single-URL bare deals: {len(bare_singles)}")
    extra = sum(g["deal_count"] - 1 for g in report)
    log(f"Extra deals beyond 1st per URL (all patterns): {extra}")

    out_path = "data/hubspot_ki_duplicates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "ki_with_kompass_url": len(deals),
                "classic_bad_groups": classic,
                "multi_firm_groups": multi_firm,
                "multi_bare_groups": multi_bare,
                "bare_singles_count": len(bare_singles),
                "bare_singles_sample": bare_singles[:100],
                "all_url_duplicate_groups": report,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    log(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
