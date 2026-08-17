"""Run Playwright sync API on a dedicated thread (safe when asyncio is active)."""

from __future__ import annotations

import atexit
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright-sync")
_playwright_thread_id: int | None = None


def _on_playwright_thread() -> bool:
    tid = threading.current_thread().ident
    return _playwright_thread_id is not None and tid == _playwright_thread_id


def run_playwright_isolated(func: Callable[[], T]) -> T:
    """Execute *func* on the Playwright worker thread unless already there."""
    global _playwright_thread_id

    if _on_playwright_thread():
        return func()

    def _wrapper() -> T:
        global _playwright_thread_id
        _playwright_thread_id = threading.current_thread().ident
        try:
            return func()
        finally:
            # Let Chromium subprocesses fully exit before the next job (Windows).
            time.sleep(0.3)

    return _executor.submit(_wrapper).result()


@atexit.register
def _shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
