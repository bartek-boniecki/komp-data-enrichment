"""Reuse one Kompass browser session on the Playwright worker thread."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from playwright.sync_api import Page

from skysnap.playwright_runner import run_playwright_isolated

if TYPE_CHECKING:
    from skysnap.kompass import KompassClient

T = TypeVar("T")

_holder: dict[str, Any] = {}


def _enter_session(client: KompassClient) -> Page:
    page = _holder.get("page")
    if page is not None:
        return page
    cm = client.session()
    page = cm.__enter__()
    _holder["cm"] = cm
    _holder["page"] = page
    return page


def _exit_session() -> None:
    cm = _holder.pop("cm", None)
    _holder.pop("page", None)
    if cm is not None:
        cm.__exit__(None, None, None)


def with_kompass_page(client: KompassClient, fn: Callable[[Page], T]) -> T:
    """Run *fn(page)* using a shared session on the Playwright worker thread."""

    def _run() -> T:
        page = _enter_session(client)
        return fn(page)

    return run_playwright_isolated(_run)


def close_kompass_session() -> None:
    run_playwright_isolated(_exit_session)
