from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import quote_plus, unquote, urlparse

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from skysnap.contact_extract import (
    ExtractedContacts,
    extract_contacts_from_html,
    merge_extracted,
)
from skysnap.contact_finalize import finalize_enrichment_contact
from skysnap.enrichment import (
    infer_website_from_email,
    sanitize_search_term,
    short_company_name_for_search,
)
from skysnap.models import EnrichmentResult
from skysnap.playwright_runner import run_playwright_isolated
from skysnap.progress import log_progress
from skysnap.scrape import (
    ScrapedPage,
    _fetch,
    crawl_site_for_contacts_on_page,
    is_scrape_blocked_url,
    scrape_website_text,
)

if TYPE_CHECKING:
    from skysnap.claude import ClaudeClient
    from skysnap.db import Lead

_KOMPASS_HOST_FRAGMENTS = ("kompas", "kompass")
# Hosts that are never a company's own website (news, press, aggregators, social,
# directories). Used to avoid storing an article URL as the website and—more
# importantly—to avoid making a portal the "company domain" for contact trust.
_NON_COMPANY_WEBSITE_HOSTS = (
    "wnp.pl",
    "leliwa.pl",
    "carpatiabiznes.pl",
    "money.pl",
    "bankier.pl",
    "onet.pl",
    "wp.pl",
    "interia.pl",
    "gazeta.pl",
    "rynekinfrastruktury.pl",
    "portalsamorzadowy.pl",
    "muratorplus.pl",
    "rynek-kolejowy.pl",
    "bizglob.pl",
    "moj.powiat.pl",
    "nuzle.pl",
    "panoramafirm.pl",
    "aleo.com",
    "pkt.pl",
    "gowork.pl",
    "facebook.com",
    "linkedin.com",
    "youtube.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "wikipedia.org",
)
# Search engines block obvious bot UAs; use a realistic desktop UA for SERPs.
_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_MAX_SEARCH_RESULTS = 3
_SNIPPET_MAX_CHARS = 25_000
_SEARCH_NAV_TIMEOUT_MS = 35_000
_QUERY_DELAY_SEC = 1.25
_DDG_RETRIES = 2

# Engine resilience: a keyless SERP engine that is being rate-limited typically
# returns 0 results (or throws) for every query. To avoid hammering (and wasting
# time on) a throttled engine across a multi-lead run, we put an engine on a
# cooldown after it errors once, or after it returns empty several times in a
# row. A genuinely-working engine that simply has no hits for one niche query
# resets its empty-streak as soon as it returns anything, so it is never
# penalised for legitimate misses.
_ENGINE_ERROR_COOLDOWN_SEC = 300.0  # hard block / anti-bot challenge
_ENGINE_EMPTY_COOLDOWN_SEC = 180.0  # likely throttled, but recover sooner
_ENGINE_EMPTY_STREAK_LIMIT = 3
_engine_cooldown_until: dict[str, float] = {}
_engine_empty_streak: dict[str, int] = {}


def _engine_ready(name: str) -> bool:
    return time.monotonic() >= _engine_cooldown_until.get(name, 0.0)


def _cooldown_engine(name: str, *, seconds: float = _ENGINE_ERROR_COOLDOWN_SEC) -> None:
    _engine_cooldown_until[name] = time.monotonic() + seconds
    _engine_empty_streak[name] = 0


def _note_engine_empty(name: str) -> None:
    streak = _engine_empty_streak.get(name, 0) + 1
    _engine_empty_streak[name] = streak
    if streak >= _ENGINE_EMPTY_STREAK_LIMIT:
        _cooldown_engine(name, seconds=_ENGINE_EMPTY_COOLDOWN_SEC)


def _note_engine_hit(name: str) -> None:
    _engine_empty_streak[name] = 0


@dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""


@dataclass
class OsintEvidence:
    """Everything gathered for one OSINT pass."""

    urls: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)  # {url, text} for the LLM
    extracted: ExtractedContacts = field(default_factory=ExtractedContacts)

    def is_empty(self) -> bool:
        return not self.sources and self.extracted.is_empty()


def _is_kompass_url(url: str) -> bool:
    lower = url.lower()
    return any(f in lower for f in _KOMPASS_HOST_FRAGMENTS)


def _resolve_duckduckgo_redirect(href: str) -> str | None:
    if href.startswith("http"):
        return href
    match = re.search(r"uddg=([^&]+)", href)
    if match:
        return unquote(match.group(1))
    if href.startswith("//"):
        return "https:" + href
    return None


# --------------------------------------------------------------------------- #
# SERP parsers (capture title + snippet, not just URLs)
# --------------------------------------------------------------------------- #
def _dedup_append(results: list[SearchResult], item: SearchResult) -> None:
    if item.url and item.url.startswith("http") and not _is_kompass_url(item.url):
        if item.url not in [r.url for r in results]:
            results.append(item)


def _brave_search(page: Page, query: str, *, max_results: int) -> list[SearchResult]:
    encoded = quote_plus(query)
    page.goto(f"https://search.brave.com/search?q={encoded}&source=web", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    results: list[SearchResult] = []
    items = page.locator('div.snippet[data-type="web"]').all() or page.locator("div.snippet").all()
    for it in items:
        if len(results) >= max_results:
            break
        try:
            anchor = it.locator("a[href^='http']").first
            href = anchor.get_attribute("href")
            if not href:
                continue
            blob = " ".join((it.inner_text(timeout=400) or "").split())
            title = ""
            t = it.locator(".title, .url, .snippet-title")
            if t.count():
                title = (t.first.inner_text(timeout=300) or "").strip()
            _dedup_append(results, SearchResult(url=href, title=title, snippet=blob[:400]))
        except Exception:
            continue
    return results


def _mojeek_search(page: Page, query: str, *, max_results: int) -> list[SearchResult]:
    encoded = quote_plus(query)
    page.goto(f"https://www.mojeek.com/search?q={encoded}", wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    results: list[SearchResult] = []
    items = page.locator("ul.results-standard li").all()
    for it in items:
        if len(results) >= max_results:
            break
        try:
            anchor = (
                it.locator("a.title").first
                if it.locator("a.title").count()
                else it.locator("a[href^='http']").first
            )
            href = anchor.get_attribute("href")
            if not href:
                continue
            title = (anchor.inner_text(timeout=300) or "").strip()
            blob = " ".join((it.inner_text(timeout=400) or "").split())
            _dedup_append(results, SearchResult(url=href, title=title, snippet=blob[:400]))
        except Exception:
            continue
    return results


def _duckduckgo_search(page: Page, query: str, *, max_results: int) -> list[SearchResult]:
    encoded = quote_plus(query)
    page.goto(f"https://html.duckduckgo.com/html/?q={encoded}", wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    results: list[SearchResult] = []
    rows = page.locator("div.result").all()
    for row in rows:
        if len(results) >= max_results:
            break
        try:
            link = row.locator("a.result__a").first
            href = link.get_attribute("href")
            if not href:
                continue
            real = _resolve_duckduckgo_redirect(href)
            if not real or _is_kompass_url(real):
                continue
            title = (link.inner_text(timeout=300) or "").strip()
            snippet = ""
            snip = row.locator(".result__snippet")
            if snip.count():
                snippet = (snip.first.inner_text(timeout=300) or "").strip()
            if real not in [r.url for r in results]:
                results.append(SearchResult(url=real, title=title, snippet=snippet))
        except Exception:
            continue
    return results


def _duckduckgo_lite_search(page: Page, query: str, *, max_results: int) -> list[SearchResult]:
    encoded = quote_plus(query)
    page.goto(f"https://lite.duckduckgo.com/lite/?q={encoded}", wait_until="domcontentloaded")
    page.wait_for_timeout(500)
    results: list[SearchResult] = []
    anchors = page.locator('a.result-link, a[href^="http"]').all()
    for anchor in anchors:
        if len(results) >= max_results:
            break
        try:
            href = anchor.get_attribute("href")
            if not href:
                continue
            real = _resolve_duckduckgo_redirect(href)
            if not real or _is_kompass_url(real) or "duckduckgo.com" in real:
                continue
            title = (anchor.inner_text(timeout=300) or "").strip()
            if real not in [r.url for r in results]:
                results.append(SearchResult(url=real, title=title))
        except Exception:
            continue
    return results


def _ecosia_search(page: Page, query: str, *, max_results: int) -> list[SearchResult]:
    encoded = quote_plus(query)
    page.goto(f"https://www.ecosia.org/search?q={encoded}", wait_until="domcontentloaded")
    page.wait_for_timeout(1300)
    results: list[SearchResult] = []
    anchors = (
        page.locator("a.result__link").all()
        or page.locator("a[data-test-id='result-link']").all()
    )
    for anchor in anchors:
        if len(results) >= max_results:
            break
        try:
            href = anchor.get_attribute("href")
            if not href or not href.startswith("http"):
                continue
            host = urlparse(href).netloc.lower()
            if "ecosia.org" in host or "google.com" in host:
                continue  # skip attribution / consent links
            title = (anchor.inner_text(timeout=300) or "").strip()
            _dedup_append(results, SearchResult(url=href, title=title))
        except Exception:
            continue
    return results


def _startpage_search(page: Page, query: str, *, max_results: int) -> list[SearchResult]:
    encoded = quote_plus(query)
    page.goto(
        f"https://www.startpage.com/sp/search?query={encoded}",
        wait_until="domcontentloaded",
    )
    page.wait_for_timeout(1300)
    results: list[SearchResult] = []
    items = page.locator("div.w-gl__result").all() or page.locator("div.result").all()
    for it in items:
        if len(results) >= max_results:
            break
        try:
            anchor = (
                it.locator("a.w-gl__result-title").first
                if it.locator("a.w-gl__result-title").count()
                else it.locator("a[href^='http']").first
            )
            href = anchor.get_attribute("href")
            if not href or "startpage.com" in urlparse(href).netloc.lower():
                continue
            title = (anchor.inner_text(timeout=300) or "").strip()
            blob = " ".join((it.inner_text(timeout=400) or "").split())
            _dedup_append(results, SearchResult(url=href, title=title, snippet=blob[:400]))
        except Exception:
            continue
    return results


def _is_google_serp_noise(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if not host:
        return True
    return any(
        fragment in host
        for fragment in (
            "google.",
            "googleusercontent.",
            "gstatic.com",
            "youtube.com",
            "webcache.googleusercontent.",
        )
    )


def _dismiss_google_consent(page: Page) -> None:
    for selector in (
        "button#L2AGLb",
        'button:has-text("Accept all")',
        'button:has-text("Zaakceptuj wszystko")',
        'button:has-text("Akceptuję")',
    ):
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=800):
                btn.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _google_search(page: Page, query: str, *, max_results: int) -> list[SearchResult]:
    encoded = quote_plus(query)
    page.goto(f"https://www.google.com/search?q={encoded}&hl=pl&num=10", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    _dismiss_google_consent(page)

    results: list[SearchResult] = []
    blocks = page.locator("div.g, div.MjjYud").all()
    for block in blocks:
        if len(results) >= max_results:
            break
        try:
            link = block.locator('a[href^="http"]').first
            href = link.get_attribute("href")
            if not href or _is_google_serp_noise(href) or _is_kompass_url(href):
                continue
            title = ""
            h3 = block.locator("h3")
            if h3.count():
                title = (h3.first.inner_text(timeout=300) or "").strip()
            snippet = ""
            sn = block.locator("div.VwiC3b, div[data-sncf]")
            if sn.count():
                snippet = (sn.first.inner_text(timeout=300) or "").strip()
            if href not in [r.url for r in results]:
                results.append(SearchResult(url=href, title=title, snippet=snippet))
        except Exception:
            continue
    return results


def _launch_chrome_browser(playwright: Playwright, *, headless: bool = True) -> Browser:
    """Prefer installed Google Chrome; fall back to bundled Chromium."""
    try:
        return playwright.chromium.launch(channel="chrome", headless=headless)
    except Exception:
        return playwright.chromium.launch(headless=headless)


def _new_search_page(browser: Browser, *, user_agent: str) -> Page:
    page = browser.new_page(user_agent=user_agent)
    page.set_default_navigation_timeout(_SEARCH_NAV_TIMEOUT_MS)
    page.set_default_timeout(_SEARCH_NAV_TIMEOUT_MS)
    return page


# Engines tried in order on a single Chromium browser. All are keyless and
# free. Brave + Mojeek are currently the most scraper-tolerant; Ecosia and
# Startpage add diversity so a single throttled engine cannot zero out a run;
# DuckDuckGo frequently serves anti-bot challenges and Google needs the Chrome
# channel (tried last, separately).
_CHROMIUM_ENGINES: tuple[tuple[str, object], ...] = (
    ("Brave", _brave_search),
    ("Mojeek", _mojeek_search),
    ("Ecosia", _ecosia_search),
    ("Startpage", _startpage_search),
    ("DuckDuckGo", _duckduckgo_search),
    ("DuckDuckGo Lite", _duckduckgo_lite_search),
)
_PRIMARY_ENGINE = _CHROMIUM_ENGINES[0][0]


def _search_engines_on_page(
    page: Page,
    query: str,
    *,
    max_results: int,
    playwright: Playwright | None = None,
) -> list[SearchResult]:
    """Try each SERP engine on an existing page; optional Playwright for Google fallback."""
    last_error: Exception | None = None
    for name, engine in _CHROMIUM_ENGINES:
        if not _engine_ready(name):
            continue
        try:
            results = engine(page, query, max_results=max_results)  # type: ignore[operator]
            if results:
                _note_engine_hit(name)
                if name != _PRIMARY_ENGINE:
                    log_progress(f"  web search: used {name}")
                return results
            _note_engine_empty(name)
        except Exception as e:
            last_error = e
            _cooldown_engine(name)
        time.sleep(0.4 + random.uniform(0.1, 0.6))

    if playwright is not None:
        time.sleep(_QUERY_DELAY_SEC)
        chrome_browser = _launch_chrome_browser(playwright, headless=True)
        try:
            page = _new_search_page(chrome_browser, user_agent=_SEARCH_USER_AGENT)
            results = _google_search(page, query, max_results=max_results)
            if results:
                log_progress("  web search: used Google Chrome fallback")
                return results
        except Exception as e:
            last_error = e
        finally:
            chrome_browser.close()

    if last_error is not None:
        raise last_error
    return []


def _search_with_browser(query: str, *, user_agent: str, max_results: int) -> list[SearchResult]:
    serp_ua = _SEARCH_USER_AGENT
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = _new_search_page(browser, user_agent=serp_ua)
            return _search_engines_on_page(
                page, query, max_results=max_results, playwright=p
            )
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# Public search helpers (back-compatible)
# --------------------------------------------------------------------------- #
def search_web_detailed(
    query: str, *, user_agent: str, max_results: int = _MAX_SEARCH_RESULTS
) -> list[SearchResult]:
    """Search; returns SearchResult(url, title, snippet). [] when engines fail."""

    def _run() -> list[SearchResult]:
        cleaned = sanitize_search_term(query, max_len=200) or query.strip()
        if not cleaned:
            return []
        try:
            return _search_with_browser(cleaned, user_agent=user_agent, max_results=max_results)
        except Exception as e:
            short = cleaned if len(cleaned) <= 90 else cleaned[:87] + "..."
            log_progress(f"  web search failed ({type(e).__name__}): {short}")
            return []

    return run_playwright_isolated(_run)


def search_web(query: str, *, user_agent: str, max_results: int = _MAX_SEARCH_RESULTS) -> list[str]:
    """Back-compatible: return result URLs only."""
    return [r.url for r in search_web_detailed(query, user_agent=user_agent, max_results=max_results)]


def build_company_website_query(lead: Lead, *, company_name: str | None = None) -> str:
    parts: list[str] = []
    name = short_company_name_for_search(company_name or lead.company_name or "")
    if name:
        parts.append(name)
    elif lead.project_name:
        parts.append(sanitize_search_term(lead.project_name, max_len=60))
    if lead.city:
        parts.append(lead.city.strip())
    parts.append("strona www oficjalna")
    return " ".join(parts)


def find_company_website(
    lead: Lead,
    *,
    user_agent: str,
    company_name: str | None = None,
) -> str | None:
    """Lightweight web search for the company homepage (no Claude)."""
    query = build_company_website_query(lead, company_name=company_name)
    urls = search_web(query, user_agent=user_agent, max_results=5)
    return _pick_best_website(urls, company_name=company_name or lead.company_name)


def build_search_query(lead: Lead, *, company_name: str | None = None) -> str:
    parts: list[str] = []
    company = short_company_name_for_search(company_name or lead.company_name)
    if company:
        parts.append(company)
    elif lead.project_name:
        parts.append(sanitize_search_term(lead.project_name, max_len=60))
    if lead.city:
        parts.append(lead.city.strip())
    parts.append("kontakt inwestycja budowa")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Evidence gathering: snippets + crawled pages + deterministic extraction
# --------------------------------------------------------------------------- #
# Per-run cache: company domain -> crawled pages, so the same GW site is not
# re-fetched across the name and channel gap-fill phases. Cleared by callers
# between runs is unnecessary (process is short-lived per run-daily).
_CRAWL_CACHE: dict[str, list[ScrapedPage]] = {}
_CRAWL_CACHE_MAX = 256


def _crawl_cached_on_page(
    page: Page,
    url: str,
    *,
    max_subpages: int,
) -> list[ScrapedPage]:
    host = urlparse(url).netloc.lower()
    if host and host in _CRAWL_CACHE:
        return _CRAWL_CACHE[host]
    if is_scrape_blocked_url(url):
        return []
    pages = crawl_site_for_contacts_on_page(
        page,
        url,
        max_chars=_SNIPPET_MAX_CHARS,
        max_subpages=max_subpages,
    )
    if host:
        if len(_CRAWL_CACHE) >= _CRAWL_CACHE_MAX:
            _CRAWL_CACHE.clear()
        _CRAWL_CACHE[host] = pages
    return pages


def gather_osint_evidence(
    queries: list[str],
    *,
    user_agent: str,
    max_urls: int = 6,
    results_per_query: int = 3,
    crawl: bool = True,
    max_subpages: int = 2,
    restrict_email_domain: str | None = None,
) -> OsintEvidence:
    """Run searches, keep snippets, crawl top hits, extract contacts deterministically.

    Uses one Playwright browser per call (search + scrape) to avoid socket errors from
    launching many Chromium instances back-to-back on the worker thread.
    """

    def _run() -> OsintEvidence:
        evidence = OsintEvidence()
        extracts: list[ExtractedContacts] = []
        detailed: list[SearchResult] = []
        seen_urls: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                search_page = _new_search_page(browser, user_agent=_SEARCH_USER_AGENT)
                scrape_page = browser.new_page(user_agent=user_agent)
                scrape_page.set_default_navigation_timeout(_SEARCH_NAV_TIMEOUT_MS)
                scrape_page.set_default_timeout(_SEARCH_NAV_TIMEOUT_MS)

                for query in queries:
                    if not query.strip():
                        continue
                    try:
                        found = _search_engines_on_page(
                            search_page,
                            query,
                            max_results=results_per_query,
                            playwright=p,
                        )
                    except Exception as e:
                        log_progress(f"  web search skipped ({type(e).__name__})")
                        found = []
                    for r in found:
                        if r.url in seen_urls or _is_kompass_url(r.url):
                            continue
                        seen_urls.add(r.url)
                        detailed.append(r)
                        snippet_text = " ".join(part for part in (r.title, r.snippet) if part).strip()
                        if snippet_text:
                            evidence.sources.append(
                                {"url": r.url, "text": f"[search snippet] {snippet_text}"}
                            )
                            extracts.append(
                                extract_contacts_from_html(
                                    f"<p>{snippet_text}</p>",
                                    url=r.url,
                                    restrict_email_domain=restrict_email_domain,
                                )
                            )
                        if len(detailed) >= max_urls:
                            break
                    if len(detailed) >= max_urls:
                        break
                    time.sleep(_QUERY_DELAY_SEC)

                evidence.urls = [r.url for r in detailed]

                for r in detailed:
                    try:
                        if crawl:
                            pages = _crawl_cached_on_page(
                                scrape_page, r.url, max_subpages=max_subpages
                            )
                        elif is_scrape_blocked_url(r.url):
                            pages = []
                        else:
                            fetched = _fetch(
                                scrape_page,
                                r.url,
                                timeout_ms=_SEARCH_NAV_TIMEOUT_MS,
                                max_chars=_SNIPPET_MAX_CHARS,
                            )
                            pages = [fetched] if fetched and fetched.text.strip() else []
                    except Exception as e:
                        log_progress(f"  scrape skipped {r.url} ({type(e).__name__})")
                        pages = []
                    for page in pages:
                        if page.text.strip():
                            evidence.sources.append({"url": page.url, "text": page.text})
                        if page.html:
                            extracts.append(
                                extract_contacts_from_html(
                                    page.html,
                                    url=page.url,
                                    restrict_email_domain=restrict_email_domain,
                                )
                            )
            finally:
                browser.close()

        evidence.extracted = merge_extracted(extracts) if extracts else ExtractedContacts()
        return evidence

    return run_playwright_isolated(_run)


def search_and_scrape(
    queries: list[str],
    *,
    user_agent: str,
    max_urls: int = 8,
    results_per_query: int = 3,
) -> list[dict[str, str]]:
    """Back-compatible: snippet + crawled page sources for the LLM."""
    evidence = gather_osint_evidence(
        queries,
        user_agent=user_agent,
        max_urls=max_urls,
        results_per_query=results_per_query,
    )
    return evidence.sources


def scrape_snippets(urls: list[str], *, user_agent: str) -> list[dict[str, str]]:
    snippets: list[dict[str, str]] = []
    for url in urls:
        try:
            text = scrape_website_text(url, user_agent=user_agent, max_chars=_SNIPPET_MAX_CHARS)
            if text.strip():
                snippets.append({"url": url, "text": text})
        except Exception as e:
            snippets.append({"url": url, "text": f"(scrape failed: {e})"})
    return snippets


def enrich_lead_osint(
    lead: Lead,
    claude: ClaudeClient,
    *,
    user_agent: str,
    max_subpages: int = 2,
    check_mx: bool = True,
    pattern_guess: bool = True,
    kompass_page: object | None = None,
) -> EnrichmentResult:
    # Page-verified participant first (usually the GW); the email-derived
    # lead.company_name is often the investor and would steer the searches —
    # and the contact attribution — to the wrong organization.
    company_hint = (
        kompass_page.participant_company if kompass_page else None
    ) or lead.company_name
    queries = [build_search_query(lead, company_name=company_hint)]
    if company_hint:
        short = short_company_name_for_search(company_hint) or company_hint
        queries.append(f"{short} kontakt email telefon")
    query = queries[0]
    evidence = gather_osint_evidence(
        queries, user_agent=user_agent, max_urls=6, max_subpages=max_subpages
    )

    if kompass_page and kompass_page.text.strip():
        kurl = lead.project_url or ""
        evidence.sources.insert(
            0,
            {"url": kurl, "text": f"[kompass project page]\n{kompass_page.text[:50_000]}"},
        )
        if kurl and kurl not in evidence.urls:
            evidence.urls.insert(0, kurl)
        if kompass_page.text:
            evidence.extracted = merge_extracted(
                [
                    evidence.extracted,
                    extract_contacts_from_html(
                        f"<pre>{kompass_page.text}</pre>",
                        url=kurl or None,
                        restrict_email_domain=None,
                    ),
                ]
            )

    if evidence.is_empty() and lead.project_url and not _is_kompass_url(lead.project_url):
        text = scrape_website_text(lead.project_url, user_agent=user_agent, max_chars=_SNIPPET_MAX_CHARS)
        if text.strip():
            evidence.sources.append({"url": lead.project_url, "text": text})
            evidence.urls.append(lead.project_url)

    if not evidence.sources and evidence.extracted.is_empty():
        return EnrichmentResult(
            source="osint",
            company_name=lead.company_name,
            notes=f"No OSINT search results for query: {query}",
        )

    label = f"{lead.project_name} / {company_hint or 'unknown company'}"
    enrichment = claude.extract_contact_from_osint_sources(
        lead_label=label,
        sources=evidence.sources,
        extracted_candidates=_candidates_payload(evidence),
    )

    updates: dict[str, str] = {}
    if not enrichment.company_name and company_hint:
        updates["company_name"] = company_hint
    elif not enrichment.company_name and lead.company_name:
        updates["company_name"] = lead.company_name
    resolved_company = enrichment.company_name or company_hint or lead.company_name
    # VERIFIED website sources only: the LLM's grounded pick (told to return the
    # org's own homepage) or the domain of a found personal email. These may
    # anchor contact trust and email-pattern guessing.
    verified_website = enrichment.website or infer_website_from_email(
        enrichment.contact.email if enrichment.contact else None
    )
    if verified_website and _is_non_company_host(verified_website):
        verified_website = None  # never store a news/portal/aggregator URL
    website = verified_website
    if not website:
        # SERP fallback fills the Website column only when the host matches a
        # company-name token — and it NEVER becomes the trust anchor below.
        website = _pick_best_website(evidence.urls, company_name=resolved_company)
    if website:
        updates["website"] = website
    elif enrichment.website:  # drop a denylisted website the LLM may have set
        updates["website"] = None
    if updates:
        enrichment = enrichment.model_copy(update=updates)

    restrict = _host_of_url(verified_website) if verified_website else None
    return finalize_enrichment_contact(
        enrichment,
        evidence.extracted,
        restrict_domain=restrict,
        check_mx=check_mx,
        allow_pattern_guess=pattern_guess,
    )


def _candidates_payload(evidence: OsintEvidence) -> dict:
    from skysnap.contact_extract import candidates_summary

    return candidates_summary(evidence.extracted)


def _is_non_company_host(url: str | None) -> bool:
    host = _host_of_url(url)
    if not host:
        return True
    return any(host == d or host.endswith("." + d) for d in _NON_COMPANY_WEBSITE_HOSTS)


def _host_of_url(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "//" in url else f"//{url}").netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _company_host_token(company_name: str | None) -> str | None:
    """Longest distinctive company token (ASCII-folded, len>=4) for host matching."""
    import unicodedata

    short = short_company_name_for_search(company_name) if company_name else ""
    if not short:
        return None
    folded = (
        unicodedata.normalize("NFKD", short.replace("ł", "l").replace("Ł", "L"))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    tokens = sorted(
        (t for t in re.split(r"[^a-z0-9]+", folded) if len(t) >= 4),
        key=len,
        reverse=True,
    )
    return tokens[0] if tokens else None


def _pick_best_website(urls: list[str], *, company_name: str | None = None) -> str | None:
    """First plausible company homepage among *urls*.

    A URL only qualifies when the host contains a distinctive token of the
    company name. Previously the first non-denylisted search hit was returned
    — a tender portal or random article became the 'Website URL' AND the
    contact-trust / email-guess domain, producing confident wrong data.
    Unverifiable (no usable token) → None; an empty cell beats a wrong one.
    """
    token = _company_host_token(company_name)
    if not token:
        return None
    for url in urls:
        host = urlparse(url).netloc.lower()
        if not host or host.endswith("duckduckgo.com") or _is_non_company_host(url):
            continue
        if token in host:
            return url
    return None
