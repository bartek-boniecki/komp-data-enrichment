"""Clear HubSpot link/sync columns on lead_exports (keeps enrichment snapshots)."""
from __future__ import annotations

import argparse
import sys

from skysnap import db
from skysnap.config import load_settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="Override DB path")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = load_settings()
    path = args.db or settings.db_path
    conn = db.connect(path)

    before = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN hubspot_deal_id IS NOT NULL THEN 1 ELSE 0 END) AS with_deal,
          SUM(CASE WHEN hubspot_company_id IS NOT NULL THEN 1 ELSE 0 END) AS with_company,
          SUM(CASE WHEN hubspot_synced_at IS NOT NULL THEN 1 ELSE 0 END) AS synced,
          SUM(CASE WHEN hubspot_task_id IS NOT NULL THEN 1 ELSE 0 END) AS with_task
        FROM lead_exports
        """
    ).fetchone()
    print(f"DB: {path}")
    print(
        f"Before: total={before['total']} deal={before['with_deal']} "
        f"company={before['with_company']} synced={before['synced']} task={before['with_task']}"
    )

    if not args.execute:
        print("Dry-run. Pass --execute to clear HubSpot columns.")
        return 0

    conn.execute(
        """
        UPDATE lead_exports SET
          hubspot_deal_id=NULL,
          hubspot_company_id=NULL,
          hubspot_contact_id=NULL,
          hubspot_task_id=NULL,
          hubspot_note_hash=NULL,
          hubspot_synced_at=NULL,
          hubspot_last_error=NULL
        """
    )
    conn.commit()
    db.checkpoint(conn)

    after = conn.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN hubspot_deal_id IS NOT NULL THEN 1 ELSE 0 END) AS with_deal,
          SUM(CASE WHEN hubspot_company_id IS NOT NULL THEN 1 ELSE 0 END) AS with_company,
          SUM(CASE WHEN hubspot_synced_at IS NOT NULL THEN 1 ELSE 0 END) AS synced,
          SUM(CASE WHEN hubspot_task_id IS NOT NULL THEN 1 ELSE 0 END) AS with_task
        FROM lead_exports
        """
    ).fetchone()
    print(
        f"After:  total={after['total']} deal={after['with_deal']} "
        f"company={after['with_company']} synced={after['synced']} task={after['with_task']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
