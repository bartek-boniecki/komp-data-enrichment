from __future__ import annotations

import sys


def log_progress(message: str) -> None:
    print(f"[skysnap] {message}", file=sys.stderr, flush=True)
