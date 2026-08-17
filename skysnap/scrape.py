from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Page, sync_playwright

from skysnap.playwright_runner import run_playwright_isolated
from skysnap.progress import log_progress

# Anchor text / href fragments that lead to a contact-rich subpage, grouped by
# priority (most likely to carry emails/phones first).
_CONTACT_LINK_HINT_TIERS: tuple[tuple[str, ...], ...] = (
    ("kontakt", "contact", "skontaktuj"),
    ("biuro", "dane-firmy", "dane kontaktowe", "impressum"),
    (
        "zespol", "zespół", "team", "ludzie", "people", "nasi-ludzie", "pracownicy",
        "kadra", "kierownictwo", "zarzad", "zarząd",
    ),
    ("ofertowanie", "przetargi", "dla-inwestora", "wspolpraca", "współpraca"),
    ("o-nas", "o nas", "about", "firma"),
)
_CONTACT_LINK_HINTS = tuple(h for tier in _CONTACT_LINK_HINT_TIERS for h in tier)


def _link_priority(haystack: str) -> int | None:
    """Lower number = higher priority; None if no hint matches."""
    for tier, hints in enumerate(_CONTACT_LINK_HINT_TIERS):
        if any(h in haystack for h in hints):
            return tier
    return None

_SKIP_LINK_FRAGMENTS = (
    "facebook.",
    "linkedin.",
    "instagram.",
    "youtube.",
    "twitter.",
    "x.com",
    "tiktok.",
    "mailto:",
    "tel:",
    ".pdf",
    ".jpg",
    ".png",
    ".zip",
)

# Whole-page fetch is unreliable or useless on these hosts (login walls, bot blocks).
# SERP snippets are still used; only Playwright navigation is skipped.
SCRAPE_BLOCKED_HOST_FRAGMENTS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
)


def is_scrape_blocked_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == frag or host.endswith("." + frag) for frag in SCRAPE_BLOCKED_HOST_FRAGMENTS)


@dataclass
class ScrapedPage:
    url: str
    text: str
    html: str


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch(page: Page, url: str, *, timeout_ms: int, max_chars: int) -> ScrapedPage | None:
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        html = page.content()
    except Exception as e:
        log_progress(f"  scrape failed {url} ({type(e).__name__})")
        return None
    text = html_to_text(html)
    if len(text) > max_chars:
        text = text[:max_chars]
    return ScrapedPage(url=url, text=text, html=html)


def _norm_host(host: str) -> str:
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _discover_contact_links(page: Page, base_url: str, *, limit: int) -> list[str]:
    base_host = _norm_host(urlparse(base_url).netloc)
    base_clean = base_url.split("#")[0].rstrip("/")
    try:
        anchors = page.locator("a[href]").all()
    except Exception:
        return []
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for anchor in anchors:
        try:
            href = anchor.get_attribute("href") or ""
            label = (anchor.inner_text(timeout=200) or "").strip().lower()
        except Exception:
            continue
        if not href:
            continue
        low = href.lower()
        if any(frag in low for frag in _SKIP_LINK_FRAGMENTS):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if _norm_host(parsed.netloc) != base_host:  # stay on the company domain
            continue
        priority = _link_priority(f"{low} {label}")
        if priority is None:
            continue
        clean = absolute.split("#")[0]
        if clean.rstrip("/") == base_clean or clean in seen:
            continue
        seen.add(clean)
        scored.append((priority, clean))
    scored.sort(key=lambda t: t[0])
    return [url for _, url in scored[:limit]]


def crawl_site_for_contacts_on_page(
    page: Page,
    url: str,
    *,
    timeout_ms: int = 25_000,
    max_chars: int = 40_000,
    max_subpages: int = 3,
) -> list[ScrapedPage]:
    """Fetch landing + contact subpages using an existing Playwright page."""
    if is_scrape_blocked_url(url):
        return []
    pages: list[ScrapedPage] = []
    page.set_default_navigation_timeout(timeout_ms)
    page.set_default_timeout(timeout_ms)

    landing = _fetch(page, url, timeout_ms=timeout_ms, max_chars=max_chars)
    if landing is None:
        return pages
    pages.append(landing)

    if max_subpages > 0:
        links = _discover_contact_links(page, url, limit=max_subpages)
        for link in links:
            sub = _fetch(page, link, timeout_ms=timeout_ms, max_chars=max_chars)
            if sub and sub.text.strip():
                pages.append(sub)
    return pages


def scrape_website_text(
    url: str,
    *,
    user_agent: str,
    timeout_ms: int = 25_000,
    max_chars: int = 80_000,
) -> str:
    """Back-compatible: scrape one page and return visible text only."""

    if is_scrape_blocked_url(url):
        return ""

    def _run() -> str:
        with sync_playwright() as p:
            browser: Browser = p.chromium.launch(headless=True)
            try:
                page: Page = browser.new_page(user_agent=user_agent)
                page.set_default_navigation_timeout(timeout_ms)
                page.set_default_timeout(timeout_ms)
                result = _fetch(page, url, timeout_ms=timeout_ms, max_chars=max_chars)
                return result.text if result else ""
            finally:
                browser.close()

    return run_playwright_isolated(_run)


def crawl_site_for_contacts(
    url: str,
    *,
    user_agent: str,
    timeout_ms: int = 25_000,
    max_chars: int = 40_000,
    max_subpages: int = 3,
) -> list[ScrapedPage]:
    """Fetch the landing page plus a few contact/about/team subpages.

    Returns ScrapedPage objects (url, text, html) so callers can run both
    deterministic extraction (html) and LLM extraction (text).
    """

    if is_scrape_blocked_url(url):
        return []

    def _run() -> list[ScrapedPage]:
        with sync_playwright() as p:
            browser: Browser = p.chromium.launch(headless=True)
            try:
                page: Page = browser.new_page(user_agent=user_agent)
                return crawl_site_for_contacts_on_page(
                    page,
                    url,
                    timeout_ms=timeout_ms,
                    max_chars=max_chars,
                    max_subpages=max_subpages,
                )
            finally:
                browser.close()

    return run_playwright_isolated(_run)
