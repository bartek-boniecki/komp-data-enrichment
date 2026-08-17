from __future__ import annotations

import argparse
import json
import sys

from skysnap.check_config import check_config
from skysnap.config import load_settings
from skysnap.engine import (
    adopt_database,
    backfill_export_snapshots,
    hubspot_property_report,
    ingest_from_email,
    import_kompass_leads,
    lead_status,
    link_hubspot_exports,
    requeue_leads,
    rescore_leads,
    retry_failed_leads,
    purge_demo,
    push_hubspot_leads,
    repair_db,
    reset_system,
    run_daily,
    run_pipeline,
    seed_demo,
    sync_sheet_icp,
    sync_sheet_similarity,
)


def _write_json(data: object) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        # Windows consoles often default to cp1252; UTF-8 bytes still display correctly in modern terminals.
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skysnap", description="SkySnap lead automation engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest-email", help="Ingest & score projects from IMAP emails")
    p_ingest.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_ingest.add_argument("--no-mark-seen", action="store_true", help="Do not mark emails as seen")
    p_ingest.add_argument(
        "--imap-only",
        action="store_true",
        help="Fetch matching emails only; skip Claude extraction (no ANTHROPIC_API_KEY needed)",
    )
    p_ingest.add_argument(
        "--force",
        action="store_true",
        help="Re-extract emails even if already ingested (still upserts leads by message_id)",
    )

    p_daily = sub.add_parser("run-daily", help="Process top-N pending leads")
    p_daily.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_daily.add_argument("--dry-run", action="store_true", help="Do not append to Google Sheet")
    p_daily.add_argument("--no-ai", action="store_true", help="Skip Claude dedupe/enrichment (testing only)")
    p_daily.add_argument(
        "--include-demo",
        action="store_true",
        help="Include seed-demo leads in the top-N selection (default: real leads only)",
    )
    p_daily.add_argument(
        "--kompass-only",
        action="store_true",
        help="Run only Phase A (Kompass enrichment, up to daily limit with contacts)",
    )
    p_daily.add_argument(
        "--osint-only",
        action="store_true",
        help="Run only Phase B (OSINT enrichment for today's pending leads)",
    )
    p_daily.add_argument(
        "--no-hubspot",
        action="store_true",
        help="Skip HubSpot push after successful exports",
    )

    p_purge_demo = sub.add_parser("purge-demo", help="Delete seed-demo leads from SQLite")
    p_purge_demo.add_argument("--dotenv", default=".env", help="Path to .env file")

    p_reset = sub.add_parser(
        "reset-system",
        help="Delete all leads + ingested-email history (fresh start)",
    )
    p_reset.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_reset.add_argument(
        "--keep-kompass-session",
        action="store_true",
        help="Keep saved Kompass login cookies (default: clear and re-login next run)",
    )
    p_reset.add_argument(
        "--keep-logs",
        action="store_true",
        help="Keep data/logs/claude-usage-*.log files",
    )

    p_seed = sub.add_parser("seed-demo", help="Seed demo leads into SQLite (for local testing)")
    p_seed.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_seed.add_argument("--n", type=int, default=10, help="Number of leads to seed")

    p_import = sub.add_parser(
        "import-kompass",
        help="Import Kompass project URLs from a text file into SQLite as pending leads",
    )
    p_import.add_argument("file", help="Text file with one Kompass URL per line")
    p_import.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_import.add_argument(
        "--icp-score",
        type=int,
        default=60,
        help="Base ICP for imported leads before Kompass re-scoring (default 60)",
    )
    p_import.add_argument(
        "--reset-pending",
        action="store_true",
        help="Reset existing imported leads back to pending (re-run enrichment)",
    )

    p_check = sub.add_parser("check-config", help="Verify Anthropic API key and IMAP (no ingest)")
    p_check.add_argument("--dotenv", default=".env", help="Path to .env file")

    p_repair_db = sub.add_parser(
        "repair-db",
        help="Fix malformed SQLite (quarantines bad WAL files or recreates empty DB)",
    )
    p_repair_db.add_argument("--dotenv", default=".env", help="Path to .env file")

    p_adopt_db = sub.add_parser(
        "adopt-db",
        help="Mark this machine as owner of the database (clears foreign-origin warning)",
    )
    p_adopt_db.add_argument("--dotenv", default=".env", help="Path to .env file")

    p_status = sub.add_parser("status", help="Show lead counts and top pending projects")
    p_status.add_argument("--dotenv", default=".env", help="Path to .env file")

    p_retry = sub.add_parser("retry-failed", help="Reset processed_failed leads back to pending")
    p_retry.add_argument("--dotenv", default=".env", help="Path to .env file")

    p_rescore = sub.add_parser(
        "rescore-leads",
        help="Re-apply ICP rubric to pending leads (fixes stale scores)",
    )
    p_rescore.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_rescore.add_argument(
        "--all",
        action="store_true",
        help="Rescore every lead, not only pending",
    )

    p_requeue = sub.add_parser(
        "requeue-leads",
        help="Reset processed_success/failed leads to pending for re-export",
    )
    p_requeue.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_requeue.add_argument(
        "--success-only",
        action="store_true",
        help="Only requeue processed_success (default: success + failed)",
    )
    p_requeue.add_argument(
        "--failed-only",
        action="store_true",
        help="Only requeue processed_failed",
    )

    p_sync_icp = sub.add_parser(
        "sync-sheet-icp",
        help="Update ICP Score column on existing sheet rows (no Kompass re-scrape)",
    )
    p_sync_icp.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_sync_icp.add_argument("--dry-run", action="store_true", help="Preview without writing")

    p_sync_sim = sub.add_parser(
        "sync-sheet-similarity",
        help="Re-run HubSpot deal similarity for existing sheet rows (backfill Deal Similarity column)",
    )
    p_sync_sim.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_sync_sim.add_argument("--dry-run", action="store_true", help="Preview without writing")
    p_sync_sim.add_argument(
        "--no-ai",
        action="store_true",
        help="Deterministic scoring only (no Claude; faster, less accurate)",
    )

    p_pipeline = sub.add_parser("run-pipeline", help="Ingest emails then run daily top-N processing")
    p_pipeline.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_pipeline.add_argument("--no-mark-seen", action="store_true", help="Do not mark emails as seen")
    p_pipeline.add_argument("--dry-run", action="store_true", help="Do not append to Google Sheet")
    p_pipeline.add_argument("--no-ai", action="store_true", help="Skip Claude dedupe/enrichment")
    p_pipeline.add_argument(
        "--skip-hubspot",
        action="store_true",
        help="Do not push exported leads to HubSpot after daily run",
    )

    p_push_hs = sub.add_parser(
        "push-hubspot",
        help="Push exported leads to HubSpot (Deal + Company + Contact)",
    )
    p_push_hs.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_push_hs.add_argument("--dry-run", action="store_true", help="Build payloads without API writes")
    hs_group = p_push_hs.add_mutually_exclusive_group()
    hs_group.add_argument("--lead-id", type=int, help="Push a single lead by SQLite id")
    hs_group.add_argument(
        "--all",
        action="store_true",
        help="Push all exported leads not yet synced (default when no --lead-id)",
    )
    p_push_hs.add_argument(
        "--resync",
        action="store_true",
        help="Update HubSpot company+deal for all previously synced exports",
    )

    p_backfill_exports = sub.add_parser(
        "backfill-exports",
        help="Create lead_exports snapshots from processed leads (enables push-hubspot)",
    )
    p_backfill_exports.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_backfill_exports.add_argument("--dry-run", action="store_true", help="Preview only")
    p_backfill_exports.add_argument(
        "--include-failed",
        action="store_true",
        help="Also backfill processed_failed leads",
    )

    p_link_hs = sub.add_parser(
        "link-hubspot",
        help="Match lead_exports to existing HubSpot deals by name (for --resync)",
    )
    p_link_hs.add_argument("--dotenv", default=".env", help="Path to .env file")
    p_link_hs.add_argument("--dry-run", action="store_true", help="Preview only")
    p_link_hs.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-link even when hubspot_deal_id is already set",
    )
    p_link_hs.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max unlinked exports to process per run (0 = all)",
    )
    p_link_hs.add_argument(
        "--delay-ms",
        type=int,
        default=250,
        help="Pause between each export lookup (default 250ms)",
    )

    p_hs_props = sub.add_parser(
        "hubspot-props",
        help="Validate HUBSPOT_PROP_* names against the live HubSpot schema",
    )
    p_hs_props.add_argument("--dotenv", default=".env", help="Path to .env file")

    args = parser.parse_args(argv)

    settings = load_settings(args.dotenv)

    if args.cmd == "ingest-email":
        res = ingest_from_email(
            settings,
            mark_seen=not args.no_mark_seen,
            imap_only=bool(args.imap_only),
            force=bool(args.force),
        )
        _write_json(res)
        return 0
    if args.cmd == "run-daily":
        res = run_daily(
            settings,
            dry_run=bool(args.dry_run),
            use_ai=not bool(args.no_ai),
            include_demo=bool(args.include_demo),
            kompass_only=bool(args.kompass_only),
            osint_only=bool(args.osint_only),
            push_hubspot=not bool(args.no_hubspot),
        )
        _write_json(res)
        return 0
    if args.cmd == "seed-demo":
        res = seed_demo(settings, n=int(args.n))
        _write_json(res)
        return 0
    if args.cmd == "import-kompass":
        res = import_kompass_leads(
            settings,
            args.file,
            icp_score=int(args.icp_score),
            reset_pending=bool(args.reset_pending),
        )
        _write_json(res)
        return 0
    if args.cmd == "check-config":
        res = check_config(settings, dotenv_path=args.dotenv)
        _write_json(res)
        return 0 if res.get("all_ok") else 1
    if args.cmd == "repair-db":
        res = repair_db(settings)
        _write_json(res)
        return 0
    if args.cmd == "adopt-db":
        res = adopt_database(settings)
        _write_json(res)
        return 0
    if args.cmd == "status":
        res = lead_status(settings)
        _write_json(res)
        return 0
    if args.cmd == "retry-failed":
        res = retry_failed_leads(settings)
        _write_json(res)
        return 0
    if args.cmd == "rescore-leads":
        res = rescore_leads(settings, pending_only=not bool(args.all))
        _write_json(res)
        return 0
    if args.cmd == "requeue-leads":
        include_success = not args.failed_only
        include_failed = not args.success_only
        if args.success_only:
            include_failed = False
        if args.failed_only:
            include_success = False
        res = requeue_leads(
            settings,
            include_success=include_success,
            include_failed=include_failed,
        )
        _write_json(res)
        return 0
    if args.cmd == "sync-sheet-icp":
        res = sync_sheet_icp(settings, dry_run=bool(args.dry_run))
        _write_json(res)
        return 0
    if args.cmd == "sync-sheet-similarity":
        res = sync_sheet_similarity(
            settings,
            dry_run=bool(args.dry_run),
            use_ai=not bool(args.no_ai),
        )
        _write_json(res)
        return 0
    if args.cmd == "purge-demo":
        res = purge_demo(settings)
        _write_json(res)
        return 0
    if args.cmd == "reset-system":
        res = reset_system(
            settings,
            keep_kompass_session=bool(args.keep_kompass_session),
            keep_logs=bool(args.keep_logs),
        )
        _write_json(res)
        return 0
    if args.cmd == "run-pipeline":
        res = run_pipeline(
            settings,
            mark_seen=not args.no_mark_seen,
            dry_run=bool(args.dry_run),
            use_ai=not bool(args.no_ai),
            push_hubspot=not bool(args.skip_hubspot),
        )
        _write_json(res)
        return 0
    if args.cmd == "push-hubspot":
        res = push_hubspot_leads(
            settings,
            lead_id=args.lead_id,
            all_pending=(bool(args.all) or args.lead_id is None) and not bool(args.resync),
            resync_all=bool(args.resync),
            dry_run=bool(args.dry_run),
        )
        _write_json(res)
        return 0
    if args.cmd == "backfill-exports":
        res = backfill_export_snapshots(
            settings,
            dry_run=bool(args.dry_run),
            include_failed=bool(args.include_failed),
        )
        _write_json(res)
        return 0
    if args.cmd == "link-hubspot":
        res = link_hubspot_exports(
            settings,
            dry_run=bool(args.dry_run),
            overwrite=bool(args.overwrite),
            limit=int(args.limit or 0),
            delay_ms=int(args.delay_ms),
        )
        _write_json(res)
        return 0
    if args.cmd == "hubspot-props":
        res = hubspot_property_report(settings)
        _write_json(res)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

