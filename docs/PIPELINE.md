# SkySnap Lead Engine — Pipeline Reference

This document describes what the system does today, step by step, from email ingest through Google Sheets export.

## Overview

SkySnap Kompass Agent is a headless Python pipeline that:

1. Reads Kompass-style investment notification emails over **IMAP**
2. Stores projects as **leads** in **SQLite** with an **ICP score** (1–100)
3. Enriches leads daily in two tiers: **Kompass** (authenticated browser, up to 5 contacts/day) then **OSINT** (web search for remaining `pending` only)
4. Defers high-ICP leads not attempted when Kompass quota is full (`enriched_manually` → Kompass retry next run)
5. Optionally deduplicates against **HubSpot** (read-only)
6. Appends rows to **Google Sheets** (sole CRM export destination)

All state lives in `data/skysnap.sqlite`. The sheet is append-only; headers in row 1 drive column mapping.

---

## Lead lifecycle (SQLite)


| Status              | Meaning                                                              |
| ------------------- | -------------------------------------------------------------------- |
| `pending`           | Waiting for enrichment / export                                      |
| `enriched_manually` | Kompass daily quota full; not attempted today — retry Kompass tomorrow (ICP order) |
| `in_progress`       | Currently being processed (recovered automatically if a run crashes) |
| `processed_success` | Exported to the sheet                                                |
| `processed_failed`  | Run failed; use `retry-failed` or `requeue-leads`                    |
| `skipped_duplicate` | Exported but flagged as HubSpot duplicate                            |


Leads are ordered for Kompass by **ICP score descending** (`iter_pending_by_icp`).

### Kompass quota deferral (`enriched_manually`)

When Phase A finds **5 contacts** (`SKYSNAP_DAILY_LIMIT`), any remaining `pending` leads that were **not attempted** in that Kompass pass are set to `enriched_manually`. They are excluded from Phase B OSINT the same day.

At the **start of the next** `run-daily` (before Phase A), `requeue_enriched_manually()` resets them to `pending` so they compete again by ICP—preserving Kompass priority for the best leads.

| After Phase A | Lead was… | Status | Phase B same day? | Next Kompass run |
| ------------- | --------- | ------ | ----------------- | ---------------- |
| Quota full | Not attempted | `enriched_manually` | No | Re-queued → `pending`, ICP order |
| Quota full or not | Attempted, no contact | `pending` | Yes | Tried again in Phase A |
| Any | Contact exported | `processed_success` | N/A | Done |

---

## Command map (high level)


| Command           | Role in pipeline                                 |
| ----------------- | ------------------------------------------------ |
| `ingest-email`    | Phase 0 — pull emails → leads                    |
| `import-kompass`  | Phase 0 alt — import Kompass URLs from a text file |
| `run-daily`       | Phases A + B — enrich + export                   |
| `run-pipeline`    | `ingest-email` then `run-daily`                  |
| `check-config`    | Validate credentials without ingesting           |
| `status`          | Queue snapshot (`by_status` includes `enriched_manually`) |
| `requeue-leads`   | Reset processed leads to `pending` for re-export |
| `retry-failed`    | Reset `processed_failed` → `pending`             |


---

## Phase 0 — Email ingest (`ingest-email`)

**Trigger:** `python -m skysnap ingest-email`

1. **Connect IMAP** using `IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD`, `IMAP_FOLDER`.
2. **Search** with `IMAP_SEARCH_QUERY` (e.g. Gmail `X-GM-RAW:inwestycjach`).
3. **Skip** messages already recorded by `Message-ID` (unless `--force`).
4. **Fetch** HTML (or plain text wrapped as HTML) for each new message.
5. **Claude** (`extract_projects_from_email`) parses the email into one or more projects:
  - `project_name`, `company_name`, `city`, `country`, `project_value`, `project_phase`, `project_url`
  - `icp_score` + `icp_reason`
6. **Upsert** each project into SQLite as `source=kompass_email`, `status=pending`.
7. **Mark seen** on IMAP (unless `--no-mark-seen`).

**Output:** JSON with counts (`emails_found`, `leads_upserted`, `lead_ids`, IMAP diagnostics).

---

## Phase 0b — Manual Kompass import (`import-kompass`)

**Trigger:** `python -m skysnap import-kompass my_leads.txt`

For curated Kompass project links (not from email). No Claude call at import time.

1. **Read** a text file: one `kompasinwestycji.pl` / Kompass URL per line (`#` comments allowed).
2. **Optional** custom name per line: `URL<TAB>project name` or `URL,project name`.
3. **Upsert** each URL into SQLite as `source=manual_kompass`, `status=pending`, with `project_url` set.
4. **Default ICP** score **90** (`--icp-score`) so manual picks are processed before lower-scored email leads.
5. **`--reset-pending`** — set already-imported leads back to `pending` for re-enrichment.

**Output:** JSON with `imported`, `skipped_invalid`, `skipped_duplicate`, `lead_ids`.

Then run `run-daily` (not `run-pipeline`) to enrich without touching IMAP.

---

## Phase 1 — Kompass enrichment (`run-daily`, default first)

**Goal:** Find up to `**SKYSNAP_DAILY_LIMIT`** leads (default **5**) with **real contact data**.

**Start of run:** `enriched_manually` leads from the previous day are reset to `pending` so they compete again by ICP.

**Selection:** Walk `pending` leads by ICP (≥ `SKYSNAP_MIN_SCORE`), skipping leads already tried in this run. Stop when the contact quota is met or the queue is exhausted.

**When quota is met:** Pending leads that were **not attempted** in this Kompass pass are set to `enriched_manually` (skipped for OSINT today; re-queued for Kompass next run).

### Per-lead flow (Kompass tier)

For each candidate lead:

1. **Mark `in_progress`** (unless `--dry-run`).
2. **HubSpot company dedupe** (if token + `company_name` present):
  - Search HubSpot companies by name
  - Claude decides fuzzy duplicate → `FuzzyDuplicateDecision`
  - Duplicates are still exported later with duplicate metadata in Komentarz
3. **HubSpot project similarity** (if token + `SKYSNAP_PROJECT_SIMILARITY_ENABLED`):
  - Fetch deal candidates (company-associated deals + deal name search)
  - Deterministic lot rules (Part 1 ≠ Part 2, Budynek A ≠ B) + Claude classification
  - Writes **Deal Similarity** sheet column (`23% — different lot (vs …)`)
  - Always exports and enriches (flag-only; does not block processing)
4. **Kompass browser fetch** (Playwright on a dedicated worker thread):
  - Reuse one logged-in session per run (`kompass_session.py`)
  - Open `project_url` on kompasinwestycji.pl
  - Dismiss Cookiebot if present
  - Try participant contact flow:
    - Click *“skontaktuj się z uczestnikiem inwestycji”*
    - Prefer **Generalny Wykonawca (GW)** radio when available
    - Submit modal → read `#modal_contact`
  - Capture **participant company name** from the selected radio / GW row
  - Return scraped text + metadata
5. **Claude** (`extract_contact_from_kompass_page`):
  - Extract contact name, role, email, phone
  - `company_name`, `website`, `project_phase` (investment stage)
  - Sheet taxonomy: `sheet_role`, `sheet_branza`
6. **Website lookup** (if contact found, no company URL yet):
  - Infer from email domain (`user@firma.pl` → `https://firma.pl`)
  - Else web search for company homepage
  - Kompass session is closed before opening a second browser (Playwright constraint)
7. **Contact gap-fill** (two phases, if company name known) — see [OSINT contact engine](#osint-contact-engine-tier-0--tier-1).
8. **Contact finalization** (every tier): phones normalized to **E.164** (`+48…`), emails validated (syntax + domain **MX**), generic vs personal classified. Non-destructive — never invents data here.
9. **Sheet taxonomy** — map role/branża to allowed sheet lists.
10. **Export decision:**
  - `**require_contact_to_export=true`** for Kompass: if no usable contact (name, email, or phone), status → `skipped_no_contact`, lead stays `**pending**` (does not count toward the 5-contact quota)
  - If contact exists → build row → append to Google Sheet (unless `--dry-run`)
  - Status → `processed_success` or `skipped_duplicate`

**Progress logs** look like:

```
[skysnap] Re-queued N lead(s) from enriched_manually for Kompass (ICP order)
[skysnap] Kompass search #N: contacts X/5 — lead ID ICP=…
[skysnap]   contact gap-fill phase: find contact name (LinkedIn)
[skysnap]   contact gap-fill phase: find email/phone
[skysnap]   -> success | skipped_no_contact | failed
[skysnap] Kompass daily quota (5) reached; N lead(s) deferred to tomorrow (enriched_manually)
```

**`run-daily` JSON fields:** `kompass_requeued` (deferrals reset at run start), `kompass_deferred` (set at end of Phase A when quota met).

---

## Phase B — OSINT enrichment (`run-daily`, after Kompass)

**Goal:** Enrich **all remaining `pending` leads** not exported in Phase A.

**Selection:** `get_pending_excluding(exported_ids)` — not limited to “today’s” ingest date. Leads in `enriched_manually` (Kompass quota deferral) are **excluded** until the next day’s Kompass pass.

**Optional cap:** `SKYSNAP_OSINT_DAILY_CAP` limits how many OSINT leads run per day (unset = no cap).

### Per-lead flow (OSINT tier)

1. HubSpot dedupe (same as Phase A)
2. **Evidence gathering** (`gather_osint_evidence`) — see [OSINT contact engine](#osint-contact-engine-tier-0--tier-1)
3. **Claude** (`extract_contact_from_osint_sources`) merges snippets + crawled pages into one contact, **preferring the deterministically extracted candidates**
4. **Contact gap-fill** (two-phase name → channels) + **finalization** (E.164 phone, MX-validated email, pattern inference)
5. **Export** — `require_contact_to_export=false`: exports even without contact, but still fills Deal Stage, Pipeline, Role, Branża, etc.
6. Status → `processed_success`

---

## OSINT contact engine (Tier 0 + Tier 1)

Shared by Phase B and by the gap-fill step of **both** tiers. The goal is
state-of-the-art recall and quality for **name / email / phone** using only
free sources (no paid SERP or data providers).

### 1. Search (`osint.py`)

- Keyless, free engines tried in order on one Chromium browser: **Brave** → **Mojeek** → **Ecosia** → **Startpage** → **DuckDuckGo HTML** → **DuckDuckGo Lite**, with a final **Google Chrome** SERP scrape fallback (`channel="chrome"`, Chromium fallback). A realistic desktop User-Agent is used for all SERP requests.
- **Engine resilience / backoff**: a throttled engine usually returns 0 results (or throws) for *every* query. To avoid hammering it across a multi-lead run, an engine is put on a **cooldown** after it errors once (5 min) or returns empty several times in a row (3 → 3 min); cooled-down engines are skipped until the TTL expires. A working engine that simply has no hits for one niche query resets its empty-streak on its next hit, so it is never penalised for legitimate misses. Jittered delays between engines reduce bot-like patterns.
- Results keep **title + snippet**, not just URLs — the SERP snippet is itself a free, high-signal source (often contains the email/phone) and is fed to extraction even if the page fails to load.

### 2. Crawl (`scrape.py`, `crawl_site_for_contacts`)

- Fetches the landing page **plus up to `SKYSNAP_OSINT_MAX_SUBPAGES`** same-domain subpages, **prioritized**: `kontakt`/`contact` → `biuro`/`dane-firmy`/`impressum` → `zespol`/`team`/`ludzie` → `o-nas`/`about`.
- `www.` vs apex host treated as the same domain.
- Per-run **domain cache** so the same GW company site is not re-crawled across the name and channel phases.

### 3. Deterministic extraction (`contact_extract.py`) — runs *before* the LLM

| Method        | Signal | Notes |
| ------------- | ------ | ----- |
| `mailto:` / `tel:` links | highest | E.164-normalized phones, direct emails |
| schema.org **JSON-LD** | high | `email`, `telephone`, `contactPoint` |
| de-obfuscated regex | medium | `jan [at] firma [dot] pl`, `(małpa)` |
| plain regex | medium | rejects NIP/REGON/IBAN/postcode false positives |

Each candidate is scored (method, role-vs-personal, free-domain, role keywords in context) and cross-source corroboration adds confidence. The ranked list is passed to Claude as a **high-trust "PROGRAMMATICALLY EXTRACTED CONTACTS"** block.

### 4. Tier-1 Polish sources

Targeted directory dorks added to gap-fill queries: `site:panoramafirm.pl`, `site:aleo.com`, `site:pkt.pl` (company switchboard / generic inbox). Optional **GUS BIR (REGON)** registry hook is enabled only when `GUS_BIR_API_KEY` is set.

### 5. Finalization (`contact_finalize.py`)

- **Phones** → E.164 (`phonenumbers`, region PL).
- **Emails** → syntax + **domain MX** validation (`dnspython`, cached); generic mailboxes (`biuro@`, `kontakt@`) ranked below personal addresses.
- Reconciles LLM output with deterministic candidates, preferring the strongest deliverable personal contact.
- **Email-pattern inference**: when a name + a verified company domain are known but no email was found, generates `imie.nazwisko@domena.pl`-style candidates (tagged "inferred" in notes). Toggle with `SKYSNAP_EMAIL_PATTERN_GUESS`.
- **Early exit**: if a deliverable personal email **and** a phone already exist, the channel search phase is skipped (saves time + LLM cost).

Graceful degradation: if `phonenumbers` / `dnspython` are missing or DNS is unreachable, validation falls back to syntax checks and never drops otherwise-valid data.

---

## Per-lead processing (`_process_lead`)

Shared by both tiers:

```
HubSpot dedupe
    → enrich_fn (Kompass or OSINT)
    → contact gap-fill (optional)
    → apply_sheet_taxonomy
    → skip if Kompass + no contact (stay pending)
    → build_row_for_headers
    → sheets.append_row (or dry-run preview)
    → update SQLite status
```

After Phase A only (not per-lead): if Kompass contact quota met → `defer_pending_for_kompass_quota()` → `enriched_manually` for unattempted `pending` leads.

---

## Google Sheet mapping

Headers are read from **row 1** of the configured tab. Values are mapped by normalized header name (`sheet_rows.py`).

Key columns (example layout):


| Column | Header              | Source                                             |
| ------ | ------------------- | -------------------------------------------------- |
| C      | Orygin Link         | Kompass project URL                                |
| D      | Nazwa Inwestycji    | `lead.project_name`                                |
| E      | Company name        | `lead.company_name` or `enrichment.company_name`   |
| I      | Website URL         | Company site (not Kompass)                         |
| L      | Mobile Phone Number | Contact phone                                      |
| M      | Email               | Contact email                                      |
| U      | Deal Stage          | `1.0 Leads Research`                               |
| V      | Leads Orygin        | `Kompass Email`                                    |
| W      | Pipeline            | `Sales Pipeline`                                   |
| Z      | linkedin in         | `contact.linkedin_url`                             |
| AA     | Direct Number       | `contact.direct_phone` or phone                    |
| AB     | email direct        | `contact.direct_email` or email                    |
| AC     | Stage inwestycji    | `enrichment.project_phase` or `lead.project_phase` |


Do not rename row 1 headers without updating `sheet_rows.py` mappings.

---

## Claude usage logging

Every Claude call appends a JSON line to:

`data/logs/claude-usage-YYYY-MM-DD.log`

`run-daily` / `ingest-email` JSON output includes `claude_usage_session` and `claude_usage_today` (tokens + estimated USD).

---

## Playwright architecture notes

- **Kompass** and **web search** use Playwright **sync** API on a **single worker thread** (`playwright_runner.py`) to avoid asyncio conflicts.
- **Kompass session** is shared across leads in Phase A; it is closed before nested searches (website lookup, gap-fill).
- Browser state (cookies) is cached under `KOMPASS_BROWSER_STATE_DIR`.

---

## Failure and recovery


| Situation                      | Behavior                                                                  |
| ------------------------------ | ------------------------------------------------------------------------- |
| Kompass daily quota full       | Unattempted pending → `enriched_manually`; OSINT skipped; Kompass retry next run |
| Kompass: no participant button | `skipped_no_contact`, stays pending                                       |
| Web search timeout / reset     | Logged; gap-fill skipped; lead may still export if Kompass contact exists |
| Run interrupted mid-lead       | `recover_stale_in_progress` on next `run-daily`                           |
| Wrong sheet data exported      | `requeue-leads` then `run-daily`                                          |
| Failed leads                   | `retry-failed` or `requeue-leads --failed-only`                           |


---

## Configuration knobs (see `.env.example`)


| Variable                  | Effect                                      |
| ------------------------- | ------------------------------------------- |
| `SKYSNAP_DAILY_LIMIT`        | Kompass contact quota per run (default 5)        |
| `SKYSNAP_MIN_SCORE`          | Minimum ICP for processing                       |
| `SKYSNAP_OSINT_DAILY_CAP`    | Max OSINT leads per run (optional)               |
| `SKYSNAP_OSINT_MAX_SUBPAGES` | Contact/about/team subpages crawled per site (default 2; 0 = landing only) |
| `SKYSNAP_EMAIL_MX_CHECK`     | Validate email domain accepts mail via MX (default true) |
| `SKYSNAP_EMAIL_PATTERN_GUESS`| Infer `imie.nazwisko@domena` from name + verified domain (default true) |
| `GUS_BIR_API_KEY`            | Optional free key for GUS BIR (REGON) registry lookups |
| `KOMPASS_HEADLESS`           | `false` if login/CAPTCHA blocks headless         |
| `SKYSNAP_TIMEZONE`           | Used for date-scoped logic where applicable      |


---

## Data directories

```
data/
  skysnap.sqlite              # Lead queue + ingest tracking
  kompass_browser_state/      # Playwright storage_state (session)
  logs/
    claude-usage-YYYY-MM-DD.log
```

