# High Level Mechanism Implemented

**Audience:** Sales team  
**Purpose:** Explain how SkySnap kompass Agent turns Kompass investment alerts into enriched rows in your Google Sheet, so you can confirm this matches how you want to work.

---

## What this system does (in one sentence)

SkySnap reads new investment emails from Kompass, scores and queues each project, enriches the best opportunities first with real contacts from Kompass, enriches the rest from the open web, checks HubSpot for possible duplicates, and appends qualified leads to your sales spreadsheet—automatically, every day.

---

## Where information comes from and where it goes


| Step              | What happens                                                                                       |
| ----------------- | -------------------------------------------------------------------------------------------------- |
| **In**            | Emails about new construction / investment projects (Kompass-style notifications)                  |
| **Working queue** | An internal list of projects waiting to be researched                                              |
| **Out**           | Your **Google Sheet** — one new row per processed project, aligned to your existing column headers |


HubSpot is used **only to read** whether a company might already exist. Nothing is created or updated in HubSpot by this tool.

---

## The daily workflow (big picture)

```
New emails arrive
       ↓
Each project is extracted and scored (1–100)
       ↓
Projects wait in a queue as "pending"
       ↓
┌──────────────────────────────────────┐
│  STEP A — Kompass (priority)         │  Up to 5 contacts with real details per day
│  Highest scores first                │
└──────────────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  STEP B — Web research (remainder)   │  All other pending projects
└──────────────────────────────────────┘
       ↓
Rows appended to Google Sheet
```

You can run email intake and the daily research as one scheduled job, or in two steps—intake first, then research.

---

## Step 0 — New projects from email

When new Kompass notification emails arrive:

1. The system collects messages it has not seen before.
2. For each email, it identifies **one or more investment projects** (name, location, phase, link to Kompass, company if known, etc.).
3. Each project is added to the internal queue with status **pending** (waiting for research).

No spreadsheet row is written at this stage—only the queue is updated.

---

## How leads are scored (ICP score)

**When:** Immediately when a project is first read from an email.  
**What you see:** A score from **1 to 100** plus a short **reason** (stored internally and reflected in the sheet’s comment field where relevant).

Scoring is done by AI using your ideal-customer logic for construction / geospatial sales:

**Higher scores (more interesting)** tend to go to projects that are:

- Larger or higher value
- In an active phase (design complete, tender, contractor selection, construction)
- In Poland or the EU with a clear location
- Linked to identifiable companies or stakeholders (especially general contractor / investor)

**Lower scores** tend to go to projects that are:

- Very early rumours or vague announcements
- Small or low commercial potential
- Missing company or location information

The score is **not a formula in a spreadsheet**—it is assigned once at intake and used to **order the queue**. The highest-scoring pending projects are researched first in Step A (Kompass). Lower-scoring projects are still processed later in Step B unless you change minimum-score settings.

**For sales:** Treat the ICP score as a **priority hint**. Step A always tries to deliver up to **five quality Kompass contacts per day** starting from the top of the queue.

---

## Step 1 — Kompass priority research (up to 5 real contacts per day)

**Goal:** Fill your sheet with the **best** leads that have **actionable contact details** from authenticated Kompass—not just project names.

**Order:** Pending projects are handled **highest ICP score first**.

**Daily limit:** The run stops adding Kompass-tier *quota* contacts after **5 successful contacts** (configurable). Searching a project without finding a contact **does not** count toward the five; the system moves to the next highest-scoring project until five contacts are found or the queue is exhausted.

### What the system does on each Kompass project

1. **Duplicate check (optional)**
  If HubSpot is connected and a company name exists, the system checks whether this company may already be in your CRM. Possible duplicates are **still written to the sheet** with a note so you can decide manually.
2. **Log in to Kompass** (session is reused during the run)
  Opens the project page from the email link.
3. **Contact panel on Kompass**
  Where available, it uses the official flow: *“skontaktuj się z uczestnikiem inwestycji”*, selects a participant, and **prefers Generalny Wykonawca (GW)** when listed.
4. **Extract details**
  Name, role, email, phone, company name, investment stage, and other fields mapped to your sheet columns (including LinkedIn, direct email/phone, and stage where found).
5. **Company website**
  If needed, the system looks up the company’s public website (from email domain or web search).
6. **Gap filling (if details are incomplete)**
  - **First:** find a **contact name** (e.g. LinkedIn-style search for roles such as Kierownik budowy, Geodeta, Sekretariat—aligned with your sheet role list).  
  - **Then:** find **email and phone** (or a generic company inbox such as biuro@ / sekretariat@ if no personal address is public).
7. **Export rule for Step A**
  - **Contact found** → row is appended to the Google Sheet; deal stage and pipeline are set to your research defaults.  
  - **No contact found** → project **stays in the queue** for a future run (not counted as one of the five). Some Kompass pages simply do not offer the participant contact button.

---

## Step 2 — Web research for all remaining pending projects

**Goal:** Do not lose projects that did not get a Kompass contact in Step A.

**Who is included:** Every project still **pending** after Step A—not only those received the same day.

**How it works (simpler than Kompass):**

1. Same optional HubSpot duplicate check.
2. Web search for the company / project + location + construction contact keywords.
3. AI reads the best public pages and pulls contact and company information.
4. Same two-step gap fill (name first, then email/phone) when needed.
5. **Export rule for Step B:** Rows are written **even if no person is found**, so the project still appears on the sheet with project name, link, stage, role/branża defaults, and comments—sales can pursue manually.

An optional daily cap can limit how many Step B projects run per day; if unset, all remaining pending projects are processed.

---

## What appears on the Google Sheet

The system **does not replace your template**. It reads **row 1 headers** and fills columns you already use, for example:

- Project name, company name, Kompass link  
- Website, email, phone, full name, job title  
- Role and branża (from your approved lists)  
- Deal stage **1.0 Leads Research**, pipeline **Sales Pipeline**, origin **Kompass Email**  
- LinkedIn, direct number, direct email, investment stage  
- **Komentarz** — location, ICP score and reason, enrichment source (Kompass vs web), duplicate notes, internal lead id

New rows are added at the **bottom** on the next free line.

---

## Duplicates and data quality


| Situation                          | What sales sees                                                              |
| ---------------------------------- | ---------------------------------------------------------------------------- |
| Likely HubSpot duplicate           | Row still exported; comment explains match and confidence                    |
| Kompass page has no contact button | No row from Step A; project stays queued                                     |
| Web search temporarily fails       | Logged internally; Kompass contact may still export; gap-fill may be partial |
| Wrong or old row in sheet          | Operations can re-queue and re-run (technical step—not needed day to day)    |


---

## What we need from sales to confirm

Please review whether this matches your intended process:

1. **Priority:** Highest ICP scores get Kompass contact research first; up to **five Kompass contacts per day** is the default quota.
2. **Strictness on Step 1:** No contact on Kompass = no export from Step A (project waits for step 2 even if it is a high score and a high value). Is that correct, or should we export “project only” rows from Kompass too when the ICP score is very high?
3. **Step 2:** All other pending projects are exported after web research, **with or without** a named contact. Is that the right safety net?
4. **Duplicates:** Suspected duplicates still land on the sheet for human review—not auto-dropped.
5. **Scoring:** AI scoring at email intake drives order, not a manual sales filter. Should any score band be excluded entirely (e.g. below 50)? this would reduce token cost
6. **Roles:** Gap-fill searches target your standard role list (Kierownik budowy, Geodeta, Sekretariat, etc.) . what are the right personas? Looking for all the personna listed in the googlesheet might be expensive in token.
  If you confirm or want changes, note them for product/ops—settings such as daily Kompass limit, minimum score, and OSINT cap can be adjusted without changing the overall mechanism.

---

## Summary for sales team

- **Intake:** Emails → scored project queue.  
- **Step 1 (Kompass):** Best scores first; up to five **verified contacts** per day; GW preferred.  
- **Step 2 (Web):** Everyone else still pending gets a sheet row after open-web enrichment.  
- **Output:** One Google Sheet, append-only, your columns.  
- **CRM:** HubSpot read-only duplicate hint; sales owns final qualification.

This document describes the mechanism **as implemented today** for alignment with the sales workflow.