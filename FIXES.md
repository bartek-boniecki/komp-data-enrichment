# Accuracy Fix Pack — 2026-08-08

Scope: bugs #1–#12, #14, #18–#19 from the code review. Every fix has a
regression test in `tests/test_bugfix_regressions.py` (21 new tests;
suite: **148 passed**). Each entry states the bug → the fix → why the fix
resolves it.

## ⚠️ Action required before deploying
`Credentials.json` in this repo contained a **live Google service-account
private key**. Rotate it in GCP Console (IAM → Service Accounts →
`kompas@kompass-498908...` → Keys), download a fresh JSON, and keep it out of
version control — a `.gitignore` is now included. The delivered zip
**excludes** `Credentials.json` on purpose.

---

## #1 Company-duplicate no longer blocks enrichment — `engine.py::_process_lead`
**Bug:** `if claude and not is_duplicate:` skipped ALL enrichment whenever the
*company* matched HubSpot. Recurring GWs (Budimex, PORR, Strabag…) are in
HubSpot after their first deal, so every subsequent project from your
best-fit companies exported with zero contact data.
**Fix:** Enrichment is now gated on `project_similarity.match_class ==
"same_project"` (a genuinely re-ingested deal) instead of the company match.
The company match is kept as a flag and later resolves the existing HubSpot
company so the new deal attaches to it.
**Why it works:** company duplication and project duplication are different
predicates; the pipeline already computes the correct one
(`ProjectSimilarityDecision`) before enrichment, so switching the gate to it
restores contact yield for repeat companies while still avoiding double-spend
on true re-ingests.

## #2 Pattern-guessed emails segregated — `models.py`, `contact_finalize.py`, `contact_search.py`, `sheet_rows.py`
**Bug:** a name+domain guess (`anna.nowak@firma.pl`) was written into
`email` **and** `direct_email` with a confidence bump — indistinguishable
from a found address in the sheet and HubSpot. MX validates the domain, not
the mailbox, so guess accuracy is far below your 90% bar.
**Fix:** new `WebsiteContact.guessed_email` field; guesses go there only,
confidence is untouched, the note now reads `NIEZWERYFIKOWANY email
(zgadnięty…)`, and an optional `Email Guessed` sheet column renders it.
`Email` / `Email Direct` never show guesses.
**Why it works:** the guess still reaches sales (visible, labeled) but can no
longer contaminate the columns your accuracy is measured on.

## #2b Trust anchor only from verified sources — `osint.py`
**Bug:** `restrict_domain` (which whitelists deterministic contacts AND is
the guessing domain) was derived from `_pick_best_website(evidence.urls)` =
the **first non-denylisted search hit** — often a tender portal → fabricated
emails @wrong-domain and misattributed contacts.
**Fix:** `restrict_domain` now comes only from the LLM's grounded website
pick or the domain of a found personal email. The SERP fallback fills the
Website column only when the host contains a distinctive company-name token
(`_company_host_token`), and is **never** used as the trust anchor.
**Why it works:** the anchor now requires positive evidence of belonging to
the company; "no anchor" degrades safely (deterministic candidates simply
aren't auto-trusted), whereas a wrong anchor actively poisoned data.

## #3 Invalid contacts dropped, not exported — `contact_finalize.py`
**Bug:** `email = chosen_email or base_contact.email` meant that when the
LLM's value failed validation (`jan.kowalski@firma`, `tel. 22 12`),
`chosen_*` was `None` and the **invalid original survived** to the sheet.
**Fix:** finalize now writes `chosen_*` directly; a present-but-invalid value
becomes `None` with a note (`odrzucono niepoprawny email/telefon: …`).
**Why it works:** the fallback was the only path by which unvalidated data
could pass the "final safety pass"; removing it makes validation total.

## #4 Generic channels can never become DIRECT channels
**a) `contact_finalize.py`** — `direct_phone`/`direct_email` were filled from
whatever won the *general* pick (a switchboard qualified). Fix: candidates
carry an `is_direct` provenance flag; `direct_*` accepts only direct-sourced
winners, with a validated fallback to the pre-existing direct value.
**b) `contact_search.py::_merge_contacts`** — the channels merge did
`direct_email = … or coalesce(email)` and the phone equivalent, promoting
`biuro@` / office numbers. Fix: `direct_*` merges only from `direct_*`; a
role mailbox is additionally scrubbed from `direct_email`; found generics
are routed into `company_generic_*` via `merge_gap_fill`.
**c) `claude.py` gap-fill prompt** — it literally instructed the model to put
`biuro@` in `email/direct_email` **and overwrite `role` with "Sekretariat"**,
destroying real job titles. Fix: generics are directed to
`company_generic_email/phone` (now declared in the schema hint), and the
prompt forbids changing `full_name` *and* `role`.
**Why it works:** the direct/general distinction is now enforced at every
layer that writes contact fields — model instruction, merge, and final
validation — so a switchboard cannot reach the "Direct Number"/"Email
Direct" columns by any path. `sheet_rows._contact_direct_email` gets a
matching guard (fallback only for non-role personal emails).

## #5 Job titles from the role text only — `sheet_taxonomy.py::map_role`
**Bug:** keywords matched `role + company_name`, so "Prezes Zarządu" at
"Zakład **Robót Ziemnych**" exported as "Kierownik robót ziemnych" and any
title at "…**Geodezja**…" became "Geodeta".
**Fix:** keyword scan runs on `raw_role` only (`company_name` kept for API
compatibility, ignored).
**Why it works:** company-name tokens describe the firm; `map_branza`
already consumes them for the branża column, which is where they belong.

## #6 Phase B Kompass prefetch actually fires — `engine.py`
**Bug:** `"kompass" in url.lower()` — the real domain is
`kompas**inwestycji**.pl` (one *s*), so the condition was always `False` and
OSINT-tier leads never received the free Kompass meta (Typ, sektor,
city/voivodeship/street, participant GW).
**Fix:** reuse `_is_kompass_project_url()` which checks host markers
`("kompasinwestycji", "kompass")`.
**Why it works:** the helper encodes both spellings against the parsed host;
the dead branch now runs for every real Kompass URL.

## #7 Phase A iteration can no longer skip leads — `db.py::iter_pending_by_icp`
**Bug (repro: 10 of 30 leads silently skipped):** `LIMIT/OFFSET` paging over
`status='pending'` while the caller flips statuses mid-loop → the filtered
set shrinks and OFFSET jumps over never-attempted leads. The `skip_ids` set
was also copied at generator start, ignoring live additions.
**Fix:** snapshot the ordered id list once, re-fetch each lead and re-check
`status == pending` at yield time; when a set is passed, `skip_ids` is
consulted **by reference** (live).
**Why it works:** the snapshot is immune to result-set shrinkage (ids don't
move), the status re-check drops anything processed meanwhile, and the live
skip view restores the intended in-run dedupe.

## #8 No more fabricated project values — `icp.py::parse_value_pln_millions`
**Bug (repro):** the fallback concatenated **every digit in the text**;
20k chars of Kompass page (dates, postal codes, plot numbers, NIP) became
"wartość ~19 mln PLN", stored into `project_value` and granting ICP bonuses.
**Fix:** the bare-amount fallback now applies only when the **entire input**
is an amount (`compact.isdigit()`), preserving `"15000000"` / `"15 000 000"`
in the value field while returning `None` for prose.
**Why it works:** unit-anchored matches (`25 mln PLN`) still parse from
prose; only the pathological concatenation path is closed, so no legitimate
signal is lost (unit tests cover both directions).

## #9 Scrub before gap-fill (and re-scrub after) — `engine.py::_process_lead`
**Bug:** platform-contact scrubbing ran *after* gap-fill, so a leaked
`…@skysnap.pl` login satisfied `needs_contact_gap_search`, the search was
skipped, and the scrub then left the lead empty.
**Fix:** scrub → gap-fill → scrub → separate → finalize. The second scrub
catches platform contacts re-introduced by web results.
**Why it works:** gap detection now measures the cleaned state, so a leak
triggers (rather than suppresses) the recovery search.

## #10 Reveal detection is delta-based — `kompass.py`
**Bug (repro):** the signal regex matched NIP (`5213017228`) and amounts
(`12500000`), so `_wait_for_contact_signal` returned instantly on the firm
block (NIP always present) — the page could be captured **before** the
"Pokaż kontakt" XHR landed — and `reveal_succeeded` was mislogged.
**Fix:** `_extract_contact_signals()` accepts emails and separator-anchored
phone shapes only (`+48 …`, `502 713 692`, `(81) 746 22 94`,
`22 623 60 00`), skips matches preceded by NIP/REGON/KRS/IBAN labels; the
wait compares against a **pre-click baseline** and succeeds only on *new*
signals. Modal checks and `reveal_succeeded` use the same extractor.
**Why it works:** a signal that pre-existed the click cannot terminate the
wait, and identifier-shaped digit runs no longer count as contacts.

## #11 `best_email(prefer_personal=True)` implemented — `contact_extract.py`
**Bug:** both branches returned `emails[0]`; a `mailto: biuro@` (score 0.75)
beat a regex-found personal address (0.70).
**Fix:** with `prefer_personal`, the first non-role, non-free-domain
candidate in score order wins; fallback to `[0]`.

## #12 Sheets writes use RAW — `sheets.py::append_row`
**Bug:** `USER_ENTERED` makes Sheets parse a leading `+` as a unary-plus
formula: `+48501234567` → `48501234567` (plus stripped) or `#ERROR!`. Your
legacy rows' manual backtick prefixes (`` `+48 …``) were the workaround.
**Fix:** `valueInputOption="RAW"`.
**Why it works:** RAW stores strings verbatim — E.164 phones keep the `+`.

## #13 Gap rows must be fully empty before reuse — `sheets.py`
**Bug:** a mid-sheet row with blank marker columns but stray content (your
`align_check.json` row 473 held a single space) was overwritten A→end.
**Fix:** each candidate gap is verified across **all** header columns
(`_row_is_fully_empty`) before writing; otherwise scanning continues.

## #14 Kompass extraction `max_tokens` 1800 → 3000 — `claude.py`
**Bug:** the prompt requests ~15 fields incl. a full Polish
`project_description`; 1800 output tokens truncated long pages → JSON parse
error → whole lead marked `processed_failed`.
**Fix:** 3000 tokens (matches the OSINT calls' headroom).

## #18 Removed `Radom` debug artifact — `kompass_firm.py::_ADDRESS_RE`
Replaced the test-specific city name in the address-terminator lookahead
with `Poland|Polska`.

## #19 Duplicate flag + docs — `sheet_rows.py`, `README.md`
**Bug:** README claimed the TAK/NIE duplicate flag lands in `DN`, but `DN`
renders the deal label and no header emitted the flag (`is_duplicate` was
computed and unused).
**Fix:** headers `Duplikat` / `Duplicate` / `Hubspot Duplicate` / `Dup` now
render TAK/NIE; README corrected and documents the `Email Guessed` column.

---

## Not changed (deliberate, flagged for follow-up)
- **MX-fail emails are kept** (no deliverability bonus, not dropped): DNS is
  flaky and `dnspython` may be absent; dropping on MX-fail risks discarding
  good data. Revisit if bounce rates justify it.
- **#15** `resolve_company_name` still prefers the email-derived name over
  the Kompass-scraped GW — changing precedence needs validation against your
  data (which entity do you sell to when they differ?).
- **#16** IMAP marks `\Seen` before extraction succeeds (loss risk only with
  UNSEEN-based queries + transient Claude failure); **#17** `check-config`
  probe rows; **#20** Phase A walks the whole queue daily (cost, not
  correctness) — see the review for details.

---

# Round 2 — "wrong company attributed to the contact" (W1–W4)

Confirmed against the reported production symptom. 10 new regression tests;
suite now **158 passed**.

## W1 Company follows the contact's employer — `enrichment.py::resolve_company_name`
**Bug:** `lead.company_name` (from the notification EMAIL — frequently the
investor, e.g. GDDKiA) always beat `enrichment.company_name`, which both
extraction prompts define as *"the organization the contact works for"*. A GW
employee (Budimex) was exported under the investor's company — the literal
"wrong company attributed to the contact" symptom. This name also drives the
gap-fill searches, so people were being *searched for* at the wrong firm.
**Fix:** the enriched name wins when it is trustworthy: `source == "kompass"`
(read off the authenticated page) OR the enrichment carries a NAMED contact.
Contact-less OSINT keeps the email name (an unanchored OSINT company could be
a hallucination).
**Why it works:** person and employer now travel together whenever a person
exists; the email name only fills the gap when nothing better is known.

## W2 Page participant beats the email guess in prompts — `engine.py`, `osint.py`
**Bug:** `company_hint = lead.company_name or participant_company` fed the
extraction prompt "Company: {investor}" even when the Kompass modal had just
revealed a GW contact — anchoring the model to attribute the person to the
wrong org (and steering OSINT queries to the wrong firm).
**Fix:** flipped to `participant_company or lead.company_name` in both tiers.
**Why it works:** the participant name is read off the authenticated page for
THIS project; the email name is a summary that may name any party.

## W3 Mismatched firm profile is never merged — `engine.py::_apply_kompass_page_fetch`
**Bug:** step 1 (firm-profile scrape) and step 2 (contact modal) each pick a
participant with independent heuristics. When they disagreed, company A's
generic email/phone/NIP/website was stapled onto company B's contact —
mixed-entity rows.
**Fix:** token-overlap check (`_companies_plausibly_match`, ASCII-folded,
legal forms dropped). On mismatch the profile merge is skipped entirely and a
Polish note explains what was dropped; unknowable cases (either name missing)
still merge.
**Why it works:** cross-entity data can only enter via that merge; blocking it
on a name mismatch removes the path while a visible note preserves auditability.

## W4 Duplicate decisions gated on confidence ≥ 0.8 — `models.py` + consumers
**Bug:** `is_duplicate` had NO confidence gate: a 0.35-confidence fuzzy match
("Budimex" ≈ "Budimet") set `skipped_duplicate` AND `resolve_company_id`
attached the deal + contact to the wrong HubSpot company.
**Fix:** `is_confident_duplicate(decision)` (threshold
`DUPLICATE_MIN_CONFIDENCE = 0.8`) now gates `engine._process_lead`,
`sheet_rows.cell_for_header`, and `hubspot_export.resolve_company_id`.
Below-threshold matches are not acted upon but surface in Komentarz as
"Możliwy duplikat HubSpot (niezatwierdzony) … (pewność N%)".
**Why it works:** the model already emits calibrated confidence; acting only
above the threshold converts wrong-company merges into human-review notes.

## Residual (flagged, not changed)
- HubSpot dedupe still *searches* by the pre-enrichment `lead.company_name`;
  after W1 the exported company can differ. The confidence gate protects the
  attach step; re-running the dedupe search post-enrichment would tighten it
  further.
- `_company_from_radio_label` splits "role - company" on the first dash; a
  company name containing " - " could truncate. Low frequency, monitor.
