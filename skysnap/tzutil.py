"""Timezone helpers (Windows needs the ``tzdata`` PyPI package for ZoneInfo)."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_timezone(tz_name: str) -> ZoneInfo:
    """Return a ``ZoneInfo`` for *tz_name*, loading ``tzdata`` on Windows if needed."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        try:
            import tzdata  # noqa: F401 — registers IANA zones on Windows
        except ImportError as e:
            raise ZoneInfoNotFoundError(
                f"No time zone found with key {tz_name!r}. "
                "On Windows, run: pip install tzdata"
            ) from e
        return ZoneInfo(tz_name)
