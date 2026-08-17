# SkySnap Lead Engine — User Guide

How to operate the lead automation agent day to day.

## Prerequisites

- Python 3.11+ with virtualenv activated
- `.env` configured (copy from `.env.example`)
- `python -m playwright install chromium` completed
- `python -m skysnap check-config` returns `"all_ok": true` for the services you need

---

## Daily workflow (production)

### 1. Verify configuration

```powershell
python -m skysnap check-config
```

Confirm:

- `anthropic.ok` — Claude API key and model
- `imap.ok` — email access (if ingesting)
- `google_sheets.ok` — service account can read/write the sheet tab
- `kompass.ok` — Kompass login works

Exit code `1` means at least one check failed.

### 2. Ingest new emails

```powershell
python -m skysnap ingest-email
```

Review JSON output:

- `emails_found` / `emails_to_process` — how many messages matched IMAP search
- `leads_upserted` — new projects added to SQLite
- `imap_search_matched` — if `0`, adjust `IMAP_SEARCH_QUERY` or folder

**Gmail tip:** If unread search returns nothing, use a body/subject fragment without `UNSEEN`, e.g.:

```env
IMAP_SEARCH_QUERY=X-GM-RAW:inwestycjach
```

**Re-ingest** the same messages (e.g. after prompt changes):

```powershell
python -m skysnap ingest-email --force
```

### 3. Run daily enrichment + export

```powershell
python -m skysnap run-daily
```

This runs the 3-step contact cascade per lead (highest ICP first):

- **Phase A (Kompass):**
  1. Opens the project page and follows the GW's firm profile link (`/v2/firma/NNNN`; Inwestor when no GW), clicks *Pokaż kontakt* and scrapes address, NIP, telefon firmowy, email firmowy — free, done for every lead.
  2. If the daily reveal quota is not exhausted, submits the *Skontaktuj się z uczestnikiem* modal to reveal the personal contact (name, email, phone). Reveals are logged in SQLite and capped at `SKYSNAP_DAILY_LIMIT` per **calendar day** (persisted across runs; a lead is never revealed twice).
  3. If the contact is still generic (no person name / role email only), OSINT runs to upgrade to a personal contact (LinkedIn dorks, company-site crawl, email-pattern inference). Generic company email/phone from Kompass still count as exportable contact if OSINT does not find better data.
- **Phase A export gate** (before writing a row): export when **any** of:
  - personal contact (name or non-role email),
  - generic company email/phone (`biuro@…`, switchboard),
  - post-enrichment ICP ≥ `SKYSNAP_STAKEHOLDER_EXPORT_MIN_ICP` (default **60**), even with no contact at all.
- **Phase B (OSINT):** remaining pending leads (the Kompass page prefetch never spends reveal credits here); exports all pending leads regardless of contact.

Watch stderr for `[skysnap]` progress lines.

### 4. Check queue status

```powershell
python -m skysnap status
```

Shows counts by status and top pending leads by ICP.

### One-shot: ingest + daily

```powershell
python -m skysnap run-pipeline
```

---

## Importing your own Kompass links (`import-kompass`)

Use this when you have a curated list of Kompass project URLs (not from email ingest).

1. Create a text file with **one URL per line** (lines starting with `#` are ignored):

   ```text
   https://www.kompasinwestycji.pl/dp1047-huta-krzeszowska-przebudowa-110250
   https://www.kompasinwestycji.pl/budynek-wielorodzinny-ul-wigury-stanislawa-110466
   ```

   Optional: add a custom project name after a tab or comma:

   ```text
   https://www.kompasinwestycji.pl/...	My custom project name
   ```

2. Import into SQLite:

   ```powershell
   python -m skysnap import-kompass my_leads.txt
   ```

   Options:

   - `--icp-score 60` — base ICP before Kompass re-scoring (default **60**, higher = processed first)
   - `--reset-pending` — if the URL was imported before and already processed, set it back to `pending`

3. Verify and run enrichment (no email ingest needed):

   ```powershell
   python -m skysnap status
   python -m skysnap run-daily --dry-run
   python -m skysnap run-daily
   ```

Imported leads use `source=manual_kompass`, `status=pending`, and the Kompass URL as `project_url` (required for Phase A).

**Bulk import tips:**

- **Kompass reveal quota:** default `SKYSNAP_DAILY_LIMIT=5` — only **five get-contact reveals per calendar day** (persisted in SQLite, shared across runs; running `run-daily` twice does not spend extra credits). The free firm-page contact (address, NIP, company phone/email) is still scraped for every lead. Raise in `.env` when your Kompass plan allows more, e.g. `SKYSNAP_DAILY_LIMIT=15`.
- **Same-day OSINT:** manually imported leads are **not** deferred to tomorrow when the Kompass quota is full; Phase B OSINT still runs on them (using the Kompass project page + web search).
- **Re-run deferred backlog** (leads stuck in `enriched_manually` from an older run): run `run-daily` again — deferred leads are reset to `pending` automatically at the start of each non-dry run.

- **Multi-day import:** import all URLs once, then run `run-daily` each day until `status` shows no pending leads (or raise `SKYSNAP_DAILY_LIMIT` temporarily).

---

## Re-exporting leads

After fixing sheet mapping or enrichment logic:

```powershell
python -m skysnap requeue-leads
python -m skysnap run-daily
```

Options:

```powershell
# Only successful exports
python -m skysnap requeue-leads --success-only

# Only failed runs
python -m skysnap requeue-leads --failed-only
python -m skysnap retry-failed
```

## Full reset (fresh start)

Wipe **all** local pipeline state — every lead, every recorded `Message-ID`, Kompass browser cookies, and Claude usage logs:

```powershell
python -m skysnap reset-system
python -m skysnap status
```

If you see `database disk image is malformed`, run:

```powershell
python -m skysnap repair-db
python -m skysnap status
```

`repair-db` tries removing stale `-wal`/`-shm` sidecar files first (common on Windows). If the main file is still corrupt, it recreates an **empty** database — then re-import leads:

```powershell
python -m skysnap import-kompass my_leads.txt
```

Full wipe (also clears Kompass cookies unless `--keep-kompass-session`):

```powershell
python -m skysnap reset-system --keep-kompass-session
```

Manual fix:

```powershell
Remove-Item -Force .\data\skysnap.sqlite, .\data\skysnap.sqlite-wal, .\data\skysnap.sqlite-shm -ErrorAction SilentlyContinue
python -m skysnap reset-system --keep-kompass-session
```

Options:

- `--keep-kompass-session` — do not delete `data/kompass_browser_state/` (stay logged in)
- `--keep-logs` — keep `data/logs/claude-usage-*.log`

**What this does not reset:**

- **Google Sheet** — rows already appended stay (delete manually in Sheets if needed)
- **IMAP read/unread** — mailbox flags are unchanged. Your `X-GM-RAW:inwestycjach` query will match again on the next `ingest-email`. If you use an `UNSEEN`-only query, run `ingest-email --force` or mark messages unread in Gmail

Manual equivalent (PowerShell):

```powershell
Remove-Item -Recurse -Force .\data\skysnap.sqlite, .\data\kompass_browser_state -ErrorAction SilentlyContinue
Remove-Item .\data\logs\claude-usage-*.log -ErrorAction SilentlyContinue
mkdir data, data\logs, data\kompass_browser_state -Force
```

---

## Debug and testing

### Dry run (no sheet writes, no status changes for exports)

```powershell
python -m skysnap run-daily --dry-run
```

JSON includes `sheet_row` previews per lead.

### Single phase

```powershell
# Kompass only (top ICP, contact quota)
python -m skysnap run-daily --kompass-only --dry-run

# OSINT only (remaining pending)
python -m skysnap run-daily --osint-only --dry-run
```

### IMAP without Claude (connectivity test)

```powershell
python -m skysnap ingest-email --imap-only
```

### Demo data (local only)

```powershell
python -m skysnap seed-demo --n 10
python -m skysnap run-daily --dry-run --include-demo
python -m skysnap purge-demo
```

Demo leads are excluded from `run-daily` unless `--include-demo`.

---

## Understanding run-daily results

Each lead in `results` has a `status`:

| Status | Meaning |
|--------|---------|
| `success` | Row appended; lead marked `processed_success` |
| `skipped_no_contact` | Kompass tier: ICP &lt; 60 and no personal/generic contact; lead stays `pending` |
| `skipped_duplicate` | Exported with HubSpot duplicate flag |
| `dry_run_success` | Would export (`--dry-run`) |
| `failed` | Exception during processing; lead → `processed_failed` |

Summary fields:

- `kompass_reveals_used_today` / `kompass_reveal_quota` — get-contact reveals spent today (persisted ledger) vs `SKYSNAP_DAILY_LIMIT`
- `kompass_contacts_found` — exported leads with a personal contact
- `kompass_searches` — Kompass attempts this run
- `kompass_skipped_no_contact` — tried but no contact panel
- `osint_processed` — OSINT exports
- `claude_usage_session` / `claude_usage_today` — token and cost estimates

---

## Google Sheet requirements

1. **Row 1** must contain your column headers (see `docs/PIPELINE.md` for mapping).
   - Optional but recommended: **`ICP Score`**, **`Deal Similarity`** (engine writes these when headers exist).
2. Tab name must match `GOOGLE_SHEET_TAB_NAME` exactly (case-sensitive).
3. Service account email must be **Editor** on the spreadsheet.
4. New rows append at the first empty line (columns C+D used to detect used rows).

---

## HubSpot deduplication and push

Full setup guide (private app, scopes, pipeline IDs, tasks, custom properties): **[HUBSPOT_SETUP.md](HUBSPOT_SETUP.md)**.

### Deduplication (read-only)

- **Company dedupe** runs when `HUBSPOT_PRIVATE_APP_TOKEN` is set and lead has `company_name`
  - Requires scope `crm.objects.companies.read`
  - Duplicates are still written to the sheet for manual review
- **Project similarity** (deal-level, flag-only) runs when `SKYSNAP_PROJECT_SIMILARITY_ENABLED=true` (default)
  - Requires scope `crm.objects.deals.read`
  - Populates the **Deal Similarity** column (`23% — different lot (vs …)`)
  - Add header `Deal Similarity` to row 1 of the sheet (same pattern as `ICP Score`)
  - Does not skip enrichment or export — for human review only
  - Backfill existing rows: `python -m skysnap sync-sheet-similarity` (add `--dry-run` to preview)
- If token is omitted, dedupe and similarity checks are skipped

### Push to HubSpot (write-ready)

When `SKYSNAP_HUBSPOT_PUSH_ENABLED=true` and pipeline/stage IDs are set, exported leads can be synced to HubSpot as **Deal + Company + Contact** (contact only when a personal email exists) and an assigned **Task** to contact the lead.

```powershell
# Push one lead (must have been exported to the sheet first)
python -m skysnap push-hubspot --lead-id 286

# Push all exported leads not yet synced
python -m skysnap push-hubspot --all

# Dry-run (no API writes)
python -m skysnap push-hubspot --all --dry-run
```

`run-pipeline` calls HubSpot push automatically for leads exported in that run (use `--skip-hubspot` to disable).

Required private-app **write** scopes: `crm.objects.companies.write`, `crm.objects.contacts.write`, `crm.objects.deals.write`, `crm.objects.tasks.write` (plus existing `crm.objects.companies.read` for dedupe).

Configure in `.env`:

- `HUBSPOT_DEAL_PIPELINE_ID` / `HUBSPOT_DEAL_STAGE_ID` — internal IDs from HubSpot → Settings → Objects → Deals → Pipelines
- `HUBSPOT_TASK_OWNER_ID` — HubSpot user ID for task assignment
- `SKYSNAP_HUBSPOT_CREATE_TASK=true` — create a follow-up task on each push (default: true)
- `SKYSNAP_HUBSPOT_TASK_WHEN=always` — `always` or `personal_contact`
- `HUBSPOT_TASK_TYPE=CALL` — `CALL`, `EMAIL`, or `TODO`
- `HUBSPOT_TASK_DUE_DAYS=7` — due date offset in `SKYSNAP_TIMEZONE` (default 7)

`check-config` reports `hubspot_push.tasks_ready` when task owner is configured.

---

## Kompass tips

- First run logs in via Playwright; session saved under `data/kompass_browser_state/`
- If login fails headless: set `KOMPASS_HEADLESS=false` and run on a machine with display (or VM with GUI)
- Projects without *“skontaktuj się z uczestnikiem inwestycji”* will not yield Kompass contacts → `skipped_no_contact`

---

## Web search / gap-fill notes

When contact name or email/phone is incomplete, the agent:

1. Searches for a **name** (LinkedIn dorks + target job titles)
2. Then searches for **email/phone**

Search uses DuckDuckGo with Google Chrome fallback. Intermittent `web search failed` in logs is normal; the run continues.

---

## Scheduling on a VM

Full pipeline (`run-pipeline` = ingest + daily enrichment + HubSpot push) should run **daily at 20:00** (machine local time; use `Europe/Warsaw` on the VM).

Register with the helper script (from repo root):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_pipeline.ps1
```

Or create the task manually:

1. Action: `C:\path\to\.venv\Scripts\python.exe`
2. Arguments: `-m skysnap run-pipeline`
3. Start in: `C:\path\to\skysnap-lead-engine`
4. Trigger: Daily at **20:00**

Or split tasks: ingest every hour, `run-daily` once per day.

See `docs/VM_INSTALLATION.md` for full VM setup.

---

## Environment variables (quick reference)

| Variable | Required for | Purpose |
|----------|--------------|---------|
| `ANTHROPIC_API_KEY` | ingest + run-daily | Claude extraction/enrichment |
| `IMAP_*` | ingest-email | Email source |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | run-daily | Sheet auth |
| `GOOGLE_SHEET_ID` | run-daily | Target spreadsheet |
| `KOMPASS_USERNAME` / `KOMPASS_PASSWORD` | Phase A | Authenticated enrichment |
| `HUBSPOT_PRIVATE_APP_TOKEN` | Optional | Dedupe + push |
| `SKYSNAP_HUBSPOT_PUSH_ENABLED` | Optional | Enable HubSpot write sync |
| `HUBSPOT_DEAL_PIPELINE_ID` / `HUBSPOT_DEAL_STAGE_ID` | Push | Deal pipeline/stage internal IDs |
| `HUBSPOT_TASK_OWNER_ID` | Push (tickets) | HubSpot user ID assigned to follow-up tickets |
| `SKYSNAP_HUBSPOT_CREATE_TASK` | Push (tickets) | Create follow-up ticket on push (default true) |
| `HUBSPOT_TICKET_PIPELINE_ID` | Push (tickets) | Ticket pipeline internal ID |
| `HUBSPOT_TICKET_STAGE_ID` | Push (tickets) | Ticket stage internal ID |

Full list: `.env.example`

---

## Getting help from logs

| Log / output | Location |
|--------------|----------|
| Progress | stderr `[skysnap] …` |
| Claude usage | `data/logs/claude-usage-*.log` |
| Command JSON | stdout from each CLI command |
| SQLite | `data/skysnap.sqlite` (use `status` or DB browser) |

---

## Command cheat sheet

```powershell
python -m skysnap check-config
python -m skysnap ingest-email
python -m skysnap import-kompass my_leads.txt
python -m skysnap run-daily
python -m skysnap run-pipeline
python -m skysnap status
python -m skysnap requeue-leads
python -m skysnap retry-failed
python -m skysnap run-daily --dry-run
python -m skysnap run-daily --kompass-only
python -m skysnap run-daily --osint-only
```
