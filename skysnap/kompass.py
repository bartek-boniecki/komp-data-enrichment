from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from skysnap.kompass_firm import (
    KompassFirmProfile,
    parse_firm_profile_from_page,
    profile_url_from_href,
)
from skysnap.models import EnrichmentResult
from skysnap.playwright_runner import run_playwright_isolated
from skysnap.scrape import html_to_text

if TYPE_CHECKING:
    from skysnap.claude import ClaudeClient
    from skysnap.config import Settings
    from skysnap.db import Lead

_STORAGE_FILENAME = "storage_state.json"
_LOGIN_INDICATORS = ("zaloguj", "logowanie", "login")
_PROJECT_READY_SELECTORS = (
    "main",
    "[class*='investment']",
    "[class*='project']",
    "article",
    "body",
)
_OPEN_CONTACT_PATTERN = re.compile(r"skontaktuj\s+si[eę]\s+z\s+uczestnikiem\s+inwestycji", re.I)
_SHOW_CONTACT_PATTERN = re.compile(
    r"poka[żz]\s+(?:kontakt|dane(?:\s+kontaktowe)?)|"
    r"wy[śs]wietl\s+(?:kontakt|dane(?:\s+kontaktowe)?)|"
    r"pokaz\s+kontakt|show\s+contact|zobacz\s+kontakt",
    re.I,
)
_GW_LABEL_PATTERN = re.compile(
    r"\bgw\b|generalny\s+wykonaw|generalnego\s+wykonawcy|główny\s+wykonaw|glowny\s+wykonaw",
    re.I,
)
_TRADE_RADIO_PATTERN = re.compile(
    r"instalacj|podwykonaw|branż|branz|stan surow|stolark|pokryć|pokryc|elektryczn|teletechn|"
    r"wentylac|klimatyz|kanalizac|grzewcz|wykonanie stanu",
    re.I,
)
# Contact signal = an email, or a phone WITH separators / +48 prefix. A bare
# digit run must NOT count: the previous pattern matched NIP ("5213017228")
# and money amounts ("12500000"), so reveal_succeeded fired on every firm
# block and the post-click wait returned before the contact XHR landed.
_CONTACT_SIGNAL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"          # email
    r"|(?:\+|00)\s?48[\s\-.]?\d{2,3}[\s\-.]?\d{2,3}[\s\-.]?\d{2,3}(?:[\s\-.]?\d{2,3})?\b"  # +48 …
    r"|\b\d{3}[\s\-]\d{3}[\s\-]\d{3}\b"                          # 502 713 692
    r"|\(\d{2,3}\)\s?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}\b"            # (81) 746 22 94
    r"|\b\d{2}[\s\-]\d{3}[\s\-]\d{2}[\s\-]\d{2}\b"               # 22 623 60 00
)
_ID_LABEL_BEFORE_RE = re.compile(r"(nip|regon|krs|iban|konto|kod\s*poczt)[\s:.\-]*$", re.I)


def _extract_contact_signals(text: str) -> set[str]:
    """Distinct email/phone-shaped strings in *text* (ID-labelled numbers excluded)."""
    signals: set[str] = set()
    for m in _CONTACT_SIGNAL_RE.finditer(text or ""):
        prefix = (text[max(0, m.start() - 14) : m.start()] or "").strip()
        if _ID_LABEL_BEFORE_RE.search(prefix):
            continue  # NIP: 526-100-31-87 etc. — an identifier, not a phone
        signals.add(re.sub(r"\s+", " ", m.group(0).strip()))
    return signals


@dataclass(frozen=True)
class KompassPageFetch:
    text: str
    participant_company: str | None = None
    firm_profile: KompassFirmProfile | None = None
    # True when the get-contact modal was actually submitted (consumes a Kompass
    # reveal credit) / when the returned panel contained an email or phone.
    reveal_submitted: bool = False
    reveal_succeeded: bool = False
    # Back-compat fields derived from firm_profile
    generic_email: str | None = None
    generic_phone: str | None = None
    generic_profile_url: str | None = None

    @classmethod
    def build(
        cls,
        *,
        text: str,
        participant_company: str | None,
        firm_profile: KompassFirmProfile | None,
        reveal_submitted: bool = False,
        reveal_succeeded: bool = False,
    ) -> KompassPageFetch:
        return cls(
            text=text,
            participant_company=participant_company,
            firm_profile=firm_profile,
            reveal_submitted=reveal_submitted,
            reveal_succeeded=reveal_succeeded,
            generic_email=firm_profile.email if firm_profile else None,
            generic_phone=firm_profile.phones if firm_profile else None,
            generic_profile_url=firm_profile.profile_url if firm_profile else None,
        )


_COOKIE_ACCEPT_SELECTORS = (
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyLevelButtonAccept",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowall",
    'button[id*="CybotCookiebotDialogBodyLevelButtonLevelOptinAllow"]',
    'button[id*="CybotCookiebotDialogBodyButtonAccept"]',
    'a[id*="CybotCookiebotDialogBodyLevelButtonLevelOptinAllow"]',
    'button:has-text("Zezwól na wszystkie")',
    'button:has-text("Zezwól na wszystkie pliki")',
    'button:has-text("Akceptuj wszystkie")',
    'button:has-text("Akceptuj")',
    'button:has-text("Zgadzam")',
    'button:has-text("Potwierdź wybór")',
    'button:has-text("Accept all")',
    'button:has-text("Accept")',
)

_COOKIE_DISMISS_JS = """() => {
    const dialog = document.querySelector('#CybotCookiebotDialog');
    if (!dialog) return false;
    const ids = [
        'CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
        'CybotCookiebotDialogBodyLevelButtonAccept',
        'CybotCookiebotDialogBodyButtonAccept',
    ];
    for (const id of ids) {
        const el = document.getElementById(id);
        if (el) { el.click(); return true; }
    }
    for (const btn of dialog.querySelectorAll('button, a[role="button"], a.CybotCookiebotDialogBodyButton')) {
        const t = (btn.textContent || '').toLowerCase();
        if (
            t.includes('zezwól') || t.includes('zezwol') || t.includes('akceptuj') ||
            t.includes('accept') || t.includes('wszystkie') || t.includes('zgadzam')
        ) {
            btn.click();
            return true;
        }
    }
    return false;
}"""


class KompassClient:
    def __init__(
        self,
        *,
        base_url: str,
        login_path: str,
        username: str,
        password: str,
        browser_state_dir: str,
        user_agent: str,
        headless: bool = True,
        timeout_ms: int = 30_000,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._login_url = f"{self._base_url}{login_path}"
        self._username = username
        self._password = password
        self._state_dir = Path(browser_state_dir)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._storage_path = self._state_dir / _STORAGE_FILENAME
        self._user_agent = user_agent
        self._headless = headless
        self._timeout_ms = timeout_ms

    def verify_login(self) -> None:
        """Raise on failed login (for check-config)."""
        run_playwright_isolated(self._verify_login_impl)

    def _verify_login_impl(self) -> None:
        with self.session() as page:
            if not self._looks_logged_in(page):
                raise RuntimeError(
                    self._login_page_error(page)
                    or "Kompass login failed: still on login page after credentials"
                )

    @contextmanager
    def session(self) -> Iterator[Page]:
        """One browser session for multiple project fetches (much faster than per-lead launch)."""
        playwright: Playwright = sync_playwright().start()
        browser: Browser = playwright.chromium.launch(headless=self._headless)
        context: BrowserContext | None = None
        try:
            storage = str(self._storage_path) if self._storage_path.exists() else None
            context = browser.new_context(user_agent=self._user_agent, storage_state=storage)
            context.set_default_navigation_timeout(self._timeout_ms)
            context.set_default_timeout(self._timeout_ms)
            page = context.new_page()
            self._ensure_logged_in(page)
            yield page
        finally:
            if context:
                try:
                    context.storage_state(path=str(self._storage_path))
                except Exception:
                    pass
                context.close()
            browser.close()
            playwright.stop()

    def fetch_project_text(self, project_url: str, *, page: Page | None = None) -> str:
        return self.fetch_project_context(project_url, page=page).text

    def fetch_project_context(
        self,
        project_url: str,
        *,
        page: Page | None = None,
        allow_contact_reveal: bool = True,
    ) -> KompassPageFetch:
        if page is not None:
            return self._fetch_project_context_on_page(
                page, project_url, allow_contact_reveal=allow_contact_reveal
            )
        return run_playwright_isolated(
            lambda: self._fetch_project_context_with_session(
                project_url, allow_contact_reveal=allow_contact_reveal
            )
        )

    def _fetch_project_context_with_session(
        self, project_url: str, *, allow_contact_reveal: bool = True
    ) -> KompassPageFetch:
        with self.session() as session_page:
            return self._fetch_project_context_on_page(
                session_page, project_url, allow_contact_reveal=allow_contact_reveal
            )

    def enrich_lead_kompass(
        self,
        lead: Lead,
        claude: ClaudeClient,
        *,
        page: Page | None = None,
    ) -> EnrichmentResult:
        if not lead.project_url:
            return EnrichmentResult(
                source="kompass",
                notes="No project_url for Kompass enrichment",
            )
        text = self.fetch_project_text(lead.project_url, page=page)
        return claude.extract_contact_from_kompass_page(
            project_url=lead.project_url,
            text=text,
            project_name=lead.project_name,
            company_name=lead.company_name,
        )

    def _fetch_project_context_on_page(
        self,
        page: Page,
        project_url: str,
        *,
        allow_contact_reveal: bool = True,
    ) -> KompassPageFetch:
        page.goto(project_url, wait_until="domcontentloaded")
        self._dismiss_cookie_banner(page)
        self._wait_for_project_shell(page)
        if self._looks_like_login_page(page):
            self._clear_storage()
            self._login(page)
            page.goto(project_url, wait_until="domcontentloaded")
            self._dismiss_cookie_banner(page)
            self._wait_for_project_shell(page)

        # --- Step 1: participant firm page (GW first, Inwestor fallback) — free --- #
        self._scroll_to_participants(page)
        gw_company_hint = self._extract_gw_company_hint(page)
        profile_url, profile_company = self._pick_participant_profile_url(
            page, gw_company_hint=gw_company_hint
        )
        firm_profile: KompassFirmProfile | None = None
        if profile_url:
            firm_profile = self._fetch_firm_profile_on_page(
                page, profile_url, company_name_hint=profile_company
            )
            page.goto(project_url, wait_until="domcontentloaded")
            self._dismiss_cookie_banner(page)
            self._wait_for_project_shell(page)

        # --- Step 2: get-contact modal (consumes a Kompass reveal credit) ------- #
        contact_text: str | None = None
        participant_company: str | None = None
        reveal_submitted = False
        if allow_contact_reveal:
            contact_text, participant_company, reveal_submitted = (
                self._fetch_contact_via_participant_flow(page)
            )
        participant_company = (
            participant_company
            or (firm_profile.company_name if firm_profile else None)
            or profile_company
            or gw_company_hint
        )
        reveal_succeeded = bool(contact_text and _extract_contact_signals(contact_text))
        profile_block = self._format_firm_profile_block(firm_profile)
        if contact_text:
            page_text = html_to_text(page.content())
            combined = (
                "=== KOMPASS CONTACT PANEL ===\n"
                f"{contact_text}\n\n"
                "=== PROJECT PAGE (summary) ===\n"
                f"{page_text[:12_000]}"
            )
            if participant_company:
                combined = (
                    "=== SELECTED PARTICIPANT COMPANY ===\n"
                    f"{participant_company}\n\n"
                    f"{combined}"
                )
            if profile_block:
                combined = f"{profile_block}\n\n{combined}"
            return KompassPageFetch.build(
                text=combined[:80_000],
                participant_company=participant_company,
                firm_profile=firm_profile,
                reveal_submitted=reveal_submitted,
                reveal_succeeded=reveal_succeeded,
            )

        page_text = html_to_text(page.content())
        text = page_text[:80_000]
        if profile_block:
            text = f"{profile_block}\n\n{text}"
        return KompassPageFetch.build(
            text=text,
            participant_company=participant_company,
            firm_profile=firm_profile,
            reveal_submitted=reveal_submitted,
            reveal_succeeded=False,
        )

    @staticmethod
    def _format_firm_profile_block(profile: KompassFirmProfile | None) -> str:
        if not profile:
            return ""
        lines = [
            "=== KOMPASS COMPANY PROFILE (firm page — generic / org contact) ===",
            f"profile: {profile.profile_url}",
        ]
        if profile.company_name:
            lines.append(f"company_name: {profile.company_name}")
        if profile.website:
            lines.append(f"website: {profile.website}")
        if profile.address:
            lines.append(f"address: {profile.address}")
        if profile.nip:
            lines.append(f"nip: {profile.nip}")
        if profile.phones:
            lines.append(f"phones: {profile.phones}")
        if profile.email:
            lines.append(f"email: {profile.email}")
        return "\n".join(lines)

    def _wait_for_project_shell(self, page: Page) -> None:
        for selector in _PROJECT_READY_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=5_000)
                break
            except Exception:
                continue
        page.wait_for_timeout(500)

    def _scroll_to_participants(self, page: Page) -> None:
        section = page.get_by_text(re.compile(r"firmy bior[aą]ce udzia[lł]", re.I))
        if section.count():
            section.first.scroll_into_view_if_needed()
            page.wait_for_timeout(600)
            return
        for _ in range(8):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(200)

    def _pick_participant_profile_url(
        self,
        page: Page,
        *,
        gw_company_hint: str | None,
    ) -> tuple[str | None, str | None]:
        """Return (profile_url, company_name) for GW, else investor, else best participant."""
        data = page.evaluate(
            """(gwHint) => {
                const scoreRole = (role) => {
                    const low = (role || '').toLowerCase();
                    if (/\\bgw\\b|generalny\\s+wykonaw/.test(low)) return 100;
                    if (/inwestor/.test(low)) return 80;
                    if (/zamawiaj|inwestycj/.test(low)) return 70;
                    return 10;
                };
                const isProfile = (href) => /\\/firma\\/\\d+/i.test(href || '');
                const normCompany = (text) => (text || '').trim().split(/\\n/)[0].trim();
                const candidates = [];

                const add = (href, company, role) => {
                    if (!href || !isProfile(href)) return;
                    candidates.push({ href, company: normCompany(company), role: role || '' });
                };

                for (const row of document.querySelectorAll('.row')) {
                    const strong = row.querySelector('strong');
                    const role = strong ? (strong.innerText || '') : '';
                    const link = row.querySelector('a[href*="/firma/"]');
                    if (!link) continue;
                    const brand = row.querySelector('.cl--brand, a .cl--brand, span.cl--brand');
                    add(link.href, brand?.innerText || link.innerText, role);
                }

                document.querySelectorAll('a[href*="/firma/"]').forEach((link) => {
                    let role = '';
                    let el = link.parentElement;
                    for (let i = 0; i < 8 && el; i++) {
                        const t = (el.innerText || '').slice(0, 300);
                        if (/inwestor|generalny|\\bgw\\b|zamawiaj/i.test(t)) {
                            role = t;
                            break;
                        }
                        el = el.parentElement;
                    }
                    const brand = link.querySelector('.cl--brand, span.cl--brand');
                    add(link.href, brand?.innerText || link.innerText, role);
                });

                let best = null;
                let bestScore = -1;
                for (const c of candidates) {
                    let score = scoreRole(c.role);
                    if (gwHint && c.company && c.company.toLowerCase().includes(gwHint.toLowerCase())) {
                        score += 40;
                    }
                    if (score > bestScore) {
                        bestScore = score;
                        best = c;
                    }
                }
                return best;
            }""",
            gw_company_hint or "",
        )
        if not data or not data.get("href"):
            return None, None
        href = profile_url_from_href(str(data["href"]).strip()) or str(data["href"]).strip()
        company = str(data.get("company") or "").strip() or None
        return href, company

    def _fetch_firm_profile_on_page(
        self,
        page: Page,
        profile_url: str,
        *,
        company_name_hint: str | None,
    ) -> KompassFirmProfile:
        """Open participant firm profile and reveal org contact details."""
        page.goto(profile_url, wait_until="domcontentloaded")
        self._dismiss_cookie_banner(page)
        page.wait_for_timeout(800)

        name_hint = company_name_hint
        if not name_hint:
            try:
                h1 = page.locator("h1").first
                if h1.count() and h1.is_visible():
                    name_hint = (h1.inner_text() or "").strip() or None
            except Exception:
                pass

        # Click "Pokaż kontakt" and wait for a NEW email/phone to appear
        # relative to the pre-click page (contact data is injected via XHR).
        # Comparing against a baseline matters: the firm block always contains
        # a NIP, which previously satisfied the signal check instantly and the
        # snapshot was taken before the contact ever loaded.
        try:
            baseline = _extract_contact_signals(page.inner_text("body"))
        except Exception:
            baseline = set()
        for attempt in range(2):
            clicked = self._click_show_contact(page)
            if not clicked:
                break
            if self._wait_for_contact_signal(page, baseline=baseline, timeout_ms=6_000):
                break
            if attempt == 0:
                page.wait_for_timeout(500)

        html = page.content()
        visible = ""
        try:
            visible = page.inner_text("body")
        except Exception:
            pass
        return parse_firm_profile_from_page(
            html=html,
            visible_text=visible,
            profile_url=profile_url,
            company_name_hint=name_hint,
        )

    def _click_show_contact(self, page: Page) -> bool:
        show_btn = page.locator("a, button").filter(has_text=_SHOW_CONTACT_PATTERN)
        for i in range(show_btn.count()):
            candidate = show_btn.nth(i)
            try:
                if candidate.is_visible():
                    candidate.scroll_into_view_if_needed()
                    candidate.click()
                    return True
            except Exception:
                continue
        return False

    def _wait_for_contact_signal(
        self,
        page: Page,
        *,
        baseline: set[str] | None = None,
        timeout_ms: int = 6_000,
    ) -> bool:
        """Poll for an email/phone that was NOT on the page before the click."""
        base = baseline or set()
        elapsed = 0
        step = 400
        while elapsed <= timeout_ms:
            try:
                body = page.inner_text("body")
            except Exception:
                body = ""
            if _extract_contact_signals(body) - base:
                return True
            page.wait_for_timeout(step)
            elapsed += step
        return False

    def _fetch_contact_via_participant_flow(
        self, page: Page
    ) -> tuple[str | None, str | None, bool]:
        """Click through Kompass contact modal: pick participant radio, then Skontaktuj się.

        Returns (panel_text, participant_company, reveal_submitted). reveal_submitted
        is True once the modal form was submitted — that is what consumes a Kompass
        contact-reveal credit, regardless of whether useful data came back.
        """
        self._scroll_to_participants(page)
        gw_company_hint = self._extract_gw_company_hint(page)
        open_btn = page.locator("a, button").filter(has_text=_OPEN_CONTACT_PATTERN)
        if open_btn.count() == 0:
            return None, gw_company_hint, False

        clicked = False
        for i in range(open_btn.count()):
            candidate = open_btn.nth(i)
            try:
                if candidate.is_visible():
                    candidate.scroll_into_view_if_needed()
                    candidate.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return None, gw_company_hint, False

        page.wait_for_timeout(800)
        radios = page.locator('#modal_ask_question input[type="radio"]')
        try:
            radios.first.wait_for(state="visible", timeout=self._timeout_ms)
        except Exception:
            return None, gw_company_hint, False
        if radios.count() == 0:
            return None, gw_company_hint, False

        best_idx = self._pick_best_radio_index(page, radios, gw_company_hint=gw_company_hint)
        participant_company = self._company_from_radio_label(
            self._radio_label(page, radios, best_idx)
        ) or gw_company_hint
        radios.nth(best_idx).check(force=True)
        page.wait_for_timeout(400)

        submit = page.locator('#modal_ask_question button[type="submit"]')
        if submit.count() == 0:
            return None, participant_company, False
        try:
            with page.expect_response(
                lambda r: "zapytaj-o-kontakt" in r.url,
                timeout=self._timeout_ms,
            ):
                submit.first.click(force=True)
        except Exception:
            submit.first.click(force=True)
        reveal_submitted = True
        page.wait_for_timeout(1_000)

        contact_body = page.locator("#modal_contact .modal-body")
        try:
            contact_body.wait_for(state="visible", timeout=self._timeout_ms)
            text = contact_body.inner_text().strip()
            if text and _extract_contact_signals(text):
                return text, participant_company, reveal_submitted
            if text:
                # Panel opened but no email/phone — still return the text for the
                # LLM (may contain a name/role) but callers can see it via
                # reveal_succeeded=False on the fetch result.
                return text, participant_company, reveal_submitted
        except Exception:
            pass

        ask_body = page.locator("#modal_ask_question .modal-body")
        if ask_body.count() and ask_body.first.is_visible():
            text = ask_body.first.inner_text().strip()
            return (text or None), participant_company, reveal_submitted
        return None, participant_company, reveal_submitted

    @staticmethod
    def _company_from_radio_label(label: str) -> str | None:
        if not label or not label.strip():
            return None
        cleaned = " ".join(label.split())
        for sep in (" - ", " – ", ": "):
            if sep in cleaned:
                _role, company = cleaned.split(sep, 1)
                company = company.strip()
                if company and len(company) > 2:
                    return company
        if _GW_LABEL_PATTERN.search(cleaned) or _TRADE_RADIO_PATTERN.search(cleaned):
            return None
        return cleaned

    def _radio_label(self, page: Page, radios, index: int) -> str:
        return str(
            radios.nth(index).evaluate(
                """el => {
                    const id = el.id;
                    if (id) {
                        const lab = document.querySelector('label[for="' + id + '"]');
                        if (lab) return lab.innerText || '';
                    }
                    return el.closest('label')?.innerText || el.parentElement?.innerText || '';
                }"""
            )
        ).strip()

    def _extract_gw_company_hint(self, page: Page) -> str | None:
        """Company name listed as GW on the project page (used to match the right radio)."""
        hint = page.evaluate(
            """() => {
                const rows = Array.from(document.querySelectorAll('.row'));
                for (const row of rows) {
                    const strong = row.querySelector('strong');
                    if (!strong) continue;
                    const role = (strong.innerText || '').toLowerCase();
                    if (!/\\bgw\\b|generalny\\s+wykonaw/.test(role)) continue;
                    const brand = row.querySelector('a .cl--brand, a span.cl--brand, .cl--brand');
                    if (brand) return (brand.innerText || '').trim();
                }
                return null;
            }"""
        )
        if hint and str(hint).strip():
            return str(hint).strip()
        return None

    def _is_gw_radio_label(self, label: str) -> bool:
        return bool(_GW_LABEL_PATTERN.search(label))

    def _score_radio_label(self, label: str, *, gw_company_hint: str | None) -> int:
        """Prefer Generalny Wykonawca (GW); deprioritize Inwestor and trade/subcontractor rows."""
        if self._is_gw_radio_label(label):
            score = 100
            if gw_company_hint and gw_company_hint.lower() in label.lower():
                score += 40
            return score
        if _TRADE_RADIO_PATTERN.search(label):
            return 1
        low = label.lower()
        if "inwestor" in low:
            return 12
        if any(kw in low for kw in ("kierownik", "dyrektor", "zarząd", "zarzad")):
            return 25
        if "email" in low or "telefon" in low or "@" in label:
            return 18
        return 10

    def _pick_best_radio_index(
        self,
        page: Page,
        radios,
        *,
        gw_company_hint: str | None,
    ) -> int:
        count = radios.count()
        if count == 0:
            return 0
        labels = [self._radio_label(page, radios, i) for i in range(count)]

        gw_indices = [i for i, label in enumerate(labels) if self._is_gw_radio_label(label)]
        if gw_indices:
            if gw_company_hint:
                hint_low = gw_company_hint.lower()
                for i in gw_indices:
                    if hint_low in labels[i].lower():
                        return i
            return gw_indices[0]

        if gw_company_hint:
            hint_low = gw_company_hint.lower()
            for i, label in enumerate(labels):
                if hint_low in label.lower():
                    return i

        best_idx = 0
        best_score = -1
        for i, label in enumerate(labels):
            score = self._score_radio_label(label, gw_company_hint=gw_company_hint)
            if score > best_score:
                best_score = score
                best_idx = i
        return best_idx

    def _cookie_banner_open(self, page: Page) -> bool:
        try:
            dialog = page.locator("#CybotCookiebotDialog")
            if dialog.count() == 0:
                return False
            return dialog.first.is_visible(timeout=800)
        except Exception:
            return False

    def _try_accept_cookies(self, page: Page) -> bool:
        for selector in _COOKIE_ACCEPT_SELECTORS:
            try:
                btn = page.locator(selector).first
                if btn.count() and btn.is_visible(timeout=400):
                    btn.click(force=True, timeout=3_000)
                    return True
            except Exception:
                continue
        try:
            return bool(page.evaluate(_COOKIE_DISMISS_JS))
        except Exception:
            return False

    def _wait_cookie_banner_gone(self, page: Page, *, timeout_ms: int = 5_000) -> None:
        try:
            page.locator("#CybotCookiebotDialog").wait_for(state="hidden", timeout=timeout_ms)
        except Exception:
            pass

    def _dismiss_cookie_banner(self, page: Page) -> None:
        """Dismiss Cookiebot overlay so login and project clicks are not intercepted."""
        page.wait_for_timeout(400)
        for _ in range(5):
            if not self._cookie_banner_open(page):
                return
            if self._try_accept_cookies(page):
                page.wait_for_timeout(600)
                self._wait_cookie_banner_gone(page)
                if not self._cookie_banner_open(page):
                    return
            page.wait_for_timeout(500)

    def _ensure_logged_in(self, page: Page) -> None:
        page.goto(self._base_url, wait_until="domcontentloaded")
        self._dismiss_cookie_banner(page)
        if self._looks_like_login_page(page):
            self._login(page)

    def _login_form(self, page: Page):
        return page.locator('form#login-form, form[action*="zaloguj"]').first

    def _visible_login_password(self, page: Page) -> bool:
        try:
            form = self._login_form(page)
            if form.count() == 0:
                pwd = page.locator('input[type="password"]').first
                return pwd.count() > 0 and pwd.is_visible()
            pwd = form.locator('input[type="password"]').first
            return pwd.count() > 0 and pwd.is_visible()
        except Exception:
            return False

    def _looks_like_login_page(self, page: Page) -> bool:
        url = page.url.lower()
        on_login_url = any(token in url for token in _LOGIN_INDICATORS)
        if on_login_url and self._visible_login_password(page):
            return True
        return self._visible_login_password(page)

    def _looks_logged_in(self, page: Page) -> bool:
        url = page.url.lower()
        if "/zaloguj" in url or url.rstrip("/").endswith("/login"):
            return False
        if self._visible_login_password(page):
            return False
        if self._login_page_error(page):
            return False
        return True

    def _login_page_error(self, page: Page) -> str | None:
        selectors = (
            '[role="alert"]',
            '[class*="error" i]',
            '[class*="invalid" i]',
            '[data-testid*="error" i]',
        )
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() and loc.is_visible():
                    text = (loc.inner_text() or "").strip()
                    if text and len(text) < 300:
                        return text
            except Exception:
                continue
        try:
            body = page.inner_text("body").lower()
        except Exception:
            return None
        for phrase in (
            "nieprawidłowy adres e-mail",
            "nieprawidlowy adres e-mail",
            "nieprawidłowe hasło",
            "nieprawidlowe haslo",
            "błędny login",
            "bledny login",
        ):
            if phrase in body:
                return phrase
        return None

    def _login(self, page: Page) -> None:
        page.goto(self._login_url, wait_until="domcontentloaded")
        self._dismiss_cookie_banner(page)
        form = self._login_form(page)
        if form.count() > 0:
            email_input = form.locator(
                'input[type="email"], input[name*="email" i], input[name*="login" i], '
                'input[name*="user" i], input[placeholder*="mail" i]'
            ).first
            password_input = form.locator('input[type="password"]').first
            submit = form.locator(
                'button[type="submit"], input[type="submit"], button:has-text("Zaloguj")'
            ).first
        else:
            email_input = page.locator(
                'input[type="email"], input[name*="email" i], input[name*="login" i], '
                'input[name*="user" i], input[placeholder*="mail" i]'
            ).first
            password_input = page.locator('input[type="password"]').first
            submit = page.locator(
                'button:has-text("Zaloguj"), input[type="submit"], button[type="submit"]'
            ).first
        email_input.wait_for(state="visible", timeout=self._timeout_ms)
        password_input.wait_for(state="visible", timeout=self._timeout_ms)
        email_input.click()
        email_input.fill(self._username)
        password_input.click()
        password_input.fill(self._password)
        submit.wait_for(state="visible", timeout=self._timeout_ms)
        last_error: Exception | None = None
        for attempt in range(4):
            self._dismiss_cookie_banner(page)
            try:
                try:
                    with page.expect_navigation(
                        timeout=20_000, wait_until="domcontentloaded"
                    ):
                        submit.click(timeout=8_000)
                except PlaywrightTimeoutError:
                    password_input.press("Enter")
                    page.wait_for_timeout(2_500)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1_000)
                if self._looks_logged_in(page):
                    last_error = None
                    break
                last_error = RuntimeError(self._login_page_error(page) or "still on login page")
            except Exception as e:
                last_error = e
            if attempt >= 3:
                break
            page.wait_for_timeout(800)
        self._dismiss_cookie_banner(page)
        if not self._looks_logged_in(page):
            detail = self._login_page_error(page)
            if last_error and not detail:
                detail = str(last_error)
            raise RuntimeError(
                f"Kompass login failed: {detail or 'check KOMPASS_USERNAME / KOMPASS_PASSWORD'}"
            )
        try:
            context = page.context
            context.storage_state(path=str(self._storage_path))
        except Exception:
            pass

    def _clear_storage(self) -> None:
        if self._storage_path.exists():
            self._storage_path.unlink()


def kompass_client_from_settings(settings: Settings) -> KompassClient:
    if not settings.kompass_username or not settings.kompass_password:
        raise ValueError("KOMPASS_USERNAME and KOMPASS_PASSWORD are required for Kompass enrichment")
    return KompassClient(
        base_url=settings.kompass_base_url,
        login_path=settings.kompass_login_path,
        username=settings.kompass_username,
        password=settings.kompass_password,
        browser_state_dir=settings.kompass_browser_state_dir,
        user_agent=settings.user_agent,
        headless=settings.kompass_headless,
    )
