# SkySnap Lead Engine — VM Installation Guide

Step-by-step setup for a dedicated Windows or Linux VM. **All secrets below are placeholders** — replace with your own credentials. Never commit `.env` or service account JSON to git.

---

## 1. VM requirements


| Resource | Minimum                              | Recommended                           |
| -------- | ------------------------------------ | ------------------------------------- |
| OS       | Windows Server 2022 or Ubuntu 22.04+ | Same                                  |
| RAM      | 4 GB                                 | 8 GB (Playwright + Chromium)          |
| Disk     | 20 GB free                           | 40 GB                                 |
| Network  | Outbound HTTPS                       | Stable egress for IMAP, APIs, Kompass |


Outbound access needed:

- `api.anthropic.com` — Claude
- `imap.gmail.com` (or your provider) — email ingest
- `www.kompasinwestycji.pl` — Kompass
- `html.duckduckgo.com`, `www.google.com` — OSINT search
- `sheets.googleapis.com` — Google Sheets
- `api.hubapi.com` — HubSpot (optional)

---

## 2. Install system dependencies

### Windows

1. Install [Python 3.11+](https://www.python.org/downloads/) — check **“Add python to PATH”**
2. Install [Git for Windows](https://git-scm.com/download/win)
3. Install [Google Chrome](https://www.google.com/chrome/) — required for OSINT **Google fallback** when DuckDuckGo fails (`channel="chrome"` in Playwright)

### Ubuntu

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git \
  libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 libasound2
```

---

## 3. Clone the repository

```bash
cd /opt   # or C:\Apps on Windows
git clone https://github.com/YOUR_ORG/skysnap-lead-engine.git
cd skysnap-lead-engine
```

Use your actual repository URL. On Windows PowerShell:

```powershell
cd C:\Apps
git clone https://github.com/YOUR_ORG/skysnap-lead-engine.git
cd skysnap-lead-engine
```

---

## 4. Python virtual environment

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

### Linux

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python -m playwright install-deps chromium   # system libs on Linux
```

---

## 5. Create configuration (`.env`)

```bash
cp .env.example .env    # Linux
copy .env.example .env  # Windows
```

Edit `.env` with a text editor. **Example with anonymized values:**

```env
# --- Core ---
SKYSNAP_DB_PATH=./data/skysnap.sqlite
SKYSNAP_DAILY_LIMIT=5
SKYSNAP_MIN_SCORE=1
SKYSNAP_USER_AGENT=SkySnapLeadBot/1.0 (+https://your-company.example)
SKYSNAP_TIMEZONE=Europe/Warsaw

# --- Anthropic (https://console.anthropic.com/settings/keys) ---
ANTHROPIC_API_KEY=sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
SKYSNAP_CLAUDE_MODEL=claude-sonnet-4-20250514
SKYSNAP_CLAUDE_USAGE_LOG_DIR=./data/logs
CLAUDE_PRICE_INPUT_PER_MTOK=3.0
CLAUDE_PRICE_OUTPUT_PER_MTOK=15.0

# --- IMAP (example: Gmail app password) ---
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USERNAME=leads-inbox@your-company.example
IMAP_PASSWORD=xxxx-xxxx-xxxx-xxxx
IMAP_FOLDER=INBOX
IMAP_SEARCH_QUERY=X-GM-RAW:inwestycjach

# --- HubSpot (optional, read-only private app) ---
HUBSPOT_PRIVATE_APP_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# --- Google Sheets ---
GOOGLE_SERVICE_ACCOUNT_JSON=./secrets/google-service-account.json
GOOGLE_SHEET_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890abc
GOOGLE_SHEET_TAB_NAME=Arkusz1

# --- Kompass ---
KOMPASS_USERNAME=your-kompass-user@example.com
KOMPASS_PASSWORD=your-kompass-password-here
KOMPASS_BASE_URL=https://www.kompasinwestycji.pl
KOMPASS_LOGIN_PATH=/zaloguj
KOMPASS_BROWSER_STATE_DIR=./data/kompass_browser_state
KOMPASS_HEADLESS=true

# --- Optional OSINT cap ---
# SKYSNAP_OSINT_DAILY_CAP=10
```

### Security practices

- Store `.env` only on the VM; file permissions `600` on Linux (`chmod 600 .env`)
- Add to `.gitignore` (already ignored): `.env`, `credentials.json`, `secrets/`
- Use **app passwords** for Gmail, not your primary account password
- Rotate keys if they ever appear in logs or chat

---

## 6. Google Cloud / Sheets setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create or select project `**your-gcp-project-id`**
2. **Enable** [Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
3. **IAM → Service Accounts → Create**
  - Name: `skysnap-sheets-writer`
  - Role: none required for Sheets-only access
4. **Keys → Add key → JSON** → save as:
  ```
   ./secrets/google-service-account.json
  ```
   Example `client_email` inside the file (share this with the sheet):
5. Open your Google Sheet → **Share** → add that email as **Editor**
6. Copy spreadsheet ID from URL:
  ```
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890abc/edit
                                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                      GOOGLE_SHEET_ID
  ```
7. Set `GOOGLE_SHEET_TAB_NAME` to the exact tab label (e.g. `Arkusz1`)

---

## 7. HubSpot setup (optional)

1. HubSpot → **Settings → Integrations → Private Apps**
2. Create app with scope: `**crm.objects.companies.read`**
3. Copy token → `HUBSPOT_PRIVATE_APP_TOKEN=pat-...`

No write scopes are required or used.

---

## 8. Kompass credentials

Use a dedicated Kompass Inwestycji account with access to contact panels.

- Set `KOMPASS_USERNAME` / `KOMPASS_PASSWORD`
- First successful `check-config` or `run-daily` saves cookies to `data/kompass_browser_state/`
- If CAPTCHA blocks headless login:
  ```env
  KOMPASS_HEADLESS=false
  ```
  Run once interactively on the VM (RDP), then switch back to `true` if session persists.

---

## 9. Verify installation

```powershell
# Windows — from repo root with venv active
python -m skysnap check-config
```

Expected (when fully configured):

```json
{
  "all_ok": true,
  "anthropic": { "ok": true, ... },
  "imap": { "ok": true, ... },
  "google_sheets": { "ok": true, ... },
  "kompass": { "ok": true, ... }
}
```

Dry run without writing the sheet:

```powershell
python -m skysnap run-daily --dry-run
```

---

## 10. Directory layout after setup

```
skysnap-lead-engine/
  .env                          # secrets (not in git)
  .venv/                        # Python virtualenv
  secrets/
    google-service-account.json # GCP key (not in git)
  data/
    skysnap.sqlite              # created on first run
    kompass_browser_state/      # created after Kompass login
    logs/
      claude-usage-YYYY-MM-DD.log
  docs/
  skysnap/                      # application code
```

Create secrets directory:

```bash
mkdir -p secrets data/logs data/kompass_browser_statedir

```

---

## 11. Schedule automated runs

### Windows Task Scheduler

Full pipeline daily at **20:00** (local time). From the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_pipeline.ps1
```

Or create manually:

1. **Task Scheduler** → Create Task
2. **Triggers:** Daily at **20:00**
3. **Actions:**
  - Program: `C:\Apps\skysnap-lead-engine\.venv\Scripts\python.exe`
  - Arguments: `-m skysnap run-pipeline`
  - Start in: `C:\Apps\skysnap-lead-engine`
4. **Settings:** Run whether user is logged on or not; use service account with access to repo + `.env`

Alternative: two tasks — `ingest-email` hourly, `run-daily` once daily.

### Linux systemd timer (example)

`/etc/systemd/system/skysnap-pipeline.service`:

```ini
[Unit]
Description=SkySnap daily lead pipeline
After=network-online.target

[Service]
Type=oneshot
User=skysnap
WorkingDirectory=/opt/skysnap-lead-engine
EnvironmentFile=/opt/skysnap-lead-engine/.env
ExecStart=/opt/skysnap-lead-engine/.venv/bin/python -m skysnap run-pipeline
```

`/etc/systemd/system/skysnap-pipeline.timer`:

```ini
[Unit]
Description=Run SkySnap pipeline daily

[Timer]
OnCalendar=*-*-* 20:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now skysnap-pipeline.timer
```

---

## 12. Post-install checklist

- [ ] `check-config` → `all_ok: true`
- [ ] Sheet row 1 headers match your template (see `docs/PIPELINE.md`)
- [ ] `ingest-email` returns `leads_upserted > 0` when test mail present
- [ ] `run-daily --dry-run` shows Kompass/OSINT phases without errors
- [ ] `run-daily` appends one test row to sheet
- [ ] `.env` and JSON keys **not** in git (`git status` clean of secrets)
- [ ] VM firewall allows outbound HTTPS only (no inbound required)

---

## 13. Troubleshooting


| Issue                   | Action                                                                   |
| ----------------------- | ------------------------------------------------------------------------ |
| `401 invalid x-api-key` | Regenerate Anthropic key; paste into `.env` without quotes               |
| Google Sheets `403`     | Enable Sheets API; share sheet with service account **Editor**           |
| IMAP `0` matches        | Fix `IMAP_SEARCH_QUERY`; try `--imap-only`                               |
| Kompass login fails     | `KOMPASS_HEADLESS=false`; delete `data/kompass_browser_state/` and retry |
| `ZoneInfoNotFoundError: Europe/Warsaw` | Windows: `pip install tzdata` in `.venv`, then re-run `check-config` |
| `DLL load failed` importing `greenlet` | Install [VC++ Redistributable x64](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist); use `.venv` |
| DuckDuckGo timeouts     | Normal intermittently; pipeline continues with Google Chrome fallback    |
| Wrong sheet columns     | Do not rename row 1; fix `sheet_rows.py` if headers must change          |


---

## 14. Updating the application

Update code with **git only**. Never copy the project folder from a workstation
onto the VM: that overwrites `data/skysnap.sqlite` with the workstation's copy
and destroys the VM's leads, export snapshots, and HubSpot links. `data/` is
gitignored precisely so `git pull` cannot touch it.

```bash
cd /opt/skysnap-lead-engine   # or your path
git pull
source .venv/bin/activate      # or .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m skysnap check-config
```

**Never do this** — `scp -r` on the whole folder overwrites the VM's database
with the workstation's copy:

```powershell
scp -r "C:\Users\jmomp\skysnap-lead-engine" skysnapagent:"C:\Users\SkySnapAdmin\KompassInvest"
```

`scp` has no exclude option, so copy only the code instead:

```powershell
.\scripts\deploy_to_vm.ps1              # add -DryRun to preview
```

That sends `skysnap\`, `tests\`, `docs\`, `scripts\`, `requirements.txt`,
`README.md`, and `.env.example`, and never touches `data\`, `.env`, `.venv\`,
or `.git\`. In WinSCP, the equivalent is a **File mask → Exclude** entry of
`data/; .venv/; .git/; __pycache__/; .pytest_cache/; *.pyc`.

`python -m skysnap status` reports `db_created_on_host` and prints a warning
when the database file originated on a different machine, which is the signal
that a copy has clobbered the VM database.

Once you have confirmed the file on the VM is the authoritative database, claim
it so the warning stops:

```powershell
python -m skysnap adopt-db
```

After code changes affecting enrichment, re-export if needed:

```powershell
python -m skysnap requeue-leads
python -m skysnap run-daily
```

---

## Related documentation

- [PIPELINE.md](./PIPELINE.md) — what each phase does internally
- [USAGE.md](./USAGE.md) — day-to-day commands and workflows
- [../README.md](../README.md) — project overview and quickstart

