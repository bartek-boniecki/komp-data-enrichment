# SkySnap AI Lead Automation Engine (MVP)

Headless, AI-driven lead triage + enrichment + Google Sheets export.

## Documentation

- [High level mechanism implemented](docs/HIGH_LEVEL_MECHANISM_IMPLEMENTED.md) — sales-friendly workflow summary (non-technical)
- [Pipeline reference](docs/PIPELINE.md) — step-by-step what each phase does
- [User guide](docs/USAGE.md) — commands and daily workflow
- [HubSpot setup](docs/HUBSPOT_SETUP.md) — private app, `.env` variables, pipeline IDs, tasks
- [VM installation](docs/VM_INSTALLATION.md) — production setup (anonymized credentials)

## What this repo does

- **Ingest & score** Kompass (or similar) email notifications into **SQLite**
- **Daily dual enrichment**:
  - **Phase A (Kompass)**: keeps searching pending leads (by ICP score) until **`SKYSNAP_DAILY_LIMIT` contacts are found** (default 5), not a fixed number of searches; leads without contact data do **not** count — the next ICP lead is tried
  - **Phase B (OSINT)**: all **remaining pending** leads (after Kompass exports) enriched via web search + scrape + Claude; exports even without contact but still sets Deal Stage, Pipeline, Role, Branża
- **Deduplicate** against HubSpot (read-only company search + fuzzy match via Claude)
- **Push** exported leads to HubSpot as Deal + Company + Contact + follow-up Task when write scopes are enabled (`push-hubspot` / `run-pipeline`)
- **Export** to **Google Sheets** as the sole destination (including duplicate-flagged rows for sales review)

## Quickstart

### 1) Prereqs

- Python 3.11+
- Playwright browsers installed (see below)

### 2) Install

```bash
python -m venv .venv
./.venv/Scripts/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

### 3) Configure

Copy `.env.example` to `.env` and fill values:

```bash
copy .env.example .env
```

**HubSpot (optional):** Private app with `crm.objects.companies.read` for dedupe. For CRM sync, also add `crm.objects.companies.write`, `crm.objects.contacts.write`, `crm.objects.deals.write`, `crm.objects.tasks.write`, set `SKYSNAP_HUBSPOT_PUSH_ENABLED=true`, configure `HUBSPOT_DEAL_PIPELINE_ID` / `HUBSPOT_DEAL_STAGE_ID`, and `HUBSPOT_TASK_OWNER_ID` for contact tasks (due in `HUBSPOT_TASK_DUE_DAYS`, default 7). If omitted, deduplication and push are skipped.

**Google Sheets (required for `run-daily`):**

1. In [Google Cloud Console](https://console.cloud.google.com/), select the project tied to your service account
2. **Enable** [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com) (APIs & Services → Library → search “Google Sheets API” → Enable)
3. Create a service account (IAM → Service Accounts) and download JSON → `GOOGLE_SERVICE_ACCOUNT_JSON`
4. Share the target spreadsheet with the service account email as **Editor** (not Viewer — read-only share makes `check-config` pass but `run-daily` fail). Email is in the JSON field `client_email`.
5. Set `GOOGLE_SHEET_ID` and `GOOGLE_SHEET_TAB_NAME` (tab name must match exactly, e.g. `Arkusz1`)

**Kompass (required for Phase A enrichment):**

1. Set `KOMPASS_USERNAME` and `KOMPASS_PASSWORD` in `.env`
2. Optional: `KOMPASS_HEADLESS=false` if login is blocked in headless mode
3. Session is cached under `KOMPASS_BROWSER_STATE_DIR`

Verify with `python -m skysnap check-config` — `google_sheets.ok` and `kompass.ok` should be `true`.

### 4) Run

Ingest new emails (IMAP):

```bash
python -m skysnap ingest-email
```

Run the daily enrichment loop (Kompass top-5 with contacts + OSINT for today’s remainder):

```bash
python -m skysnap run-daily
```

Import your own Kompass project URLs (one per line in a text file):

```bash
python -m skysnap import-kompass my_leads.txt
python -m skysnap run-daily
```

Debug a single phase:

```bash
python -m skysnap run-daily --kompass-only --dry-run
python -m skysnap run-daily --osint-only --dry-run
```

Full pipeline (ingest + daily in one command):

```bash
python -m skysnap run-pipeline
```

Check configuration (Anthropic, IMAP, optional HubSpot/Sheets):

```bash
python -m skysnap check-config
```

View lead queue status:

```bash
python -m skysnap status
```

Re-export leads already marked processed (e.g. after fixing sheet mapping):

```bash
python -m skysnap requeue-leads
python -m skysnap run-daily
```

`run-daily --dry-run` previews rows **without** changing lead status or writing to the sheet.

Seed demo data (local testing only — **not exported** unless you pass `--include-demo`):

```bash
python -m skysnap seed-demo --n 10
python -m skysnap run-daily --dry-run --no-ai --include-demo
python -m skysnap purge-demo
```

## Google Sheet columns

`run-daily` **reads your existing header row** (row 1) and appends new projects at the **bottom**, matching column order. Expected headers include:

`Nazwa Inwestycji`, `Company name`, `Strona Inwestycji`, `Stage inwestycji`, `KI:`, `Komentarz`, `Leads Orygin`, contact fields (`Email`, `Full Name`, `Job Title`, …), etc.

Do not rename row 1 — SkySnap maps values by header name. `DN` carries the short deal label (`{Company}, {project}`). To surface the HubSpot duplicate flag (`TAK`/`NIE`), add a column headed `Duplikat` (or `Duplicate`). A column headed `Email Guessed` receives pattern-inferred addresses, which are never written into `Email` / `Email Direct`.

## Claude API usage log

Every Claude call (ingest, dedupe, Kompass/OSINT enrichment) appends a JSON line to a **daily log file**:

`data/logs/claude-usage-YYYY-MM-DD.log`

Each `run-daily` / `ingest-email` response also includes `claude_usage_session` and `claude_usage_today` (tokens + estimated USD).

Tune pricing estimates in `.env`:

```env
CLAUDE_PRICE_INPUT_PER_MTOK=3.0
CLAUDE_PRICE_OUTPUT_PER_MTOK=15.0
```

## Notes

- HubSpot is not written during dedupe (read-only search). Use `push-hubspot` or `run-pipeline` to create Deal + Company + Contact + Ticket when push is enabled.
- Duplicates are still exported to the sheet with `is_duplicate=true` for manual review.
- All lead state is persisted in `data/skysnap.sqlite`.
- Designed to run on a low-cost VM or workstation with a daily scheduled task.

### IMAP ingest troubleshooting

`ingest-email` prints JSON. Besides `emails_found`, check:

- **`imap_search_typ`**: must be `OK`. If not, the server rejected `IMAP_SEARCH_QUERY` (syntax or capability).
- **`imap_search_matched`**: how many messages matched the IMAP search. If this is `0`, relax or fix the query, or fix `IMAP_FOLDER`.
- **`imap_mailbox_total`** / **`imap_unseen_count`**: messages in the selected folder vs IMAP `UNSEEN` count.
- **`imap_hint`**: set on Gmail when the query uses unread filters but IMAP reports zero unseen (see below).

**Gmail note:** The web UI can show a message as unread while IMAP reports `\Seen` on every INBOX message (preview pane, categories, other clients). In that case `(UNSEEN)` and even `X-GM-RAW is:unread` return nothing. Use a subject/body fragment without unread, e.g. `IMAP_SEARCH_QUERY=X-GM-RAW:inwestycjach` (the colon form is required; a single string `X-GM-RAW inwestycjach` is invalid for Gmail).
- **`imap_skipped_empty_body`**: matched messages with no usable HTML or plain text body.
- **`imap_skipped_fetch_failed`**: FETCH failed for some IDs.

Messages with **plain text only** (no HTML part) are now ingested by wrapping the text for extraction. Non-ASCII strings inside `IMAP_SEARCH_QUERY` use `SEARCH CHARSET UTF-8` when the server allows it.

**Re-ingest:** Gmail queries like `X-GM-RAW:inwestycjach` match the same mail every run. Ingested `Message-ID`s are tracked in SQLite so Claude is not called again. Use `ingest-email --force` to re-extract. For only new mail, use Gmail syntax such as `X-GM-RAW:inwestycjach newer_than:1d`.
