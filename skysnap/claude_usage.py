from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from skysnap.tzutil import get_timezone

# USD per million tokens (override via env in Settings). Sonnet 4 list price as of 2025.
_DEFAULT_INPUT_PER_MTOK = 3.0
_DEFAULT_OUTPUT_PER_MTOK = 15.0


@dataclass
class ClaudeUsageTracker:
    """Append Claude API usage to a per-day log file with cost estimates."""

    log_dir: Path
    model: str
    timezone: str
    command: str
    input_price_per_mtok: float = _DEFAULT_INPUT_PER_MTOK
    output_price_per_mtok: float = _DEFAULT_OUTPUT_PER_MTOK
    _session_input: int = field(default=0, init=False, repr=False)
    _session_output: int = field(default=0, init=False, repr=False)
    _session_calls: int = field(default=0, init=False, repr=False)
    _session_cost_usd: float = field(default=0.0, init=False, repr=False)

    def record(self, operation: str, *, input_tokens: int, output_tokens: int) -> None:
        cost = estimate_cost_usd(
            input_tokens,
            output_tokens,
            input_price_per_mtok=self.input_price_per_mtok,
            output_price_per_mtok=self.output_price_per_mtok,
        )
        self._session_input += input_tokens
        self._session_output += output_tokens
        self._session_calls += 1
        self._session_cost_usd += cost
        self._append(
            {
                "type": "api_call",
                "operation": operation,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost, 6),
            }
        )

    def write_session_summary(self) -> dict[str, Any]:
        if self._session_calls == 0:
            return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
        summary = {
            "type": "session_summary",
            "calls": self._session_calls,
            "input_tokens": self._session_input,
            "output_tokens": self._session_output,
            "estimated_cost_usd": round(self._session_cost_usd, 6),
        }
        self._append(summary)
        return {
            "calls": self._session_calls,
            "input_tokens": self._session_input,
            "output_tokens": self._session_output,
            "estimated_cost_usd": round(self._session_cost_usd, 6),
            "log_file": str(self._log_path()),
        }

    def read_daily_totals(self) -> dict[str, Any]:
        """Sum api_call rows for today from the daily log (all sessions)."""
        path = self._log_path()
        if not path.exists():
            return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
        calls = 0
        input_tokens = 0
        output_tokens = 0
        cost = 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "api_call":
                continue
            calls += 1
            input_tokens += int(row.get("input_tokens") or 0)
            output_tokens += int(row.get("output_tokens") or 0)
            cost += float(row.get("cost_usd") or 0.0)
        return {
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(cost, 6),
            "log_file": str(path),
        }

    def _log_path(self) -> Path:
        day = datetime.now(get_timezone(self.timezone)).date().isoformat()
        return self.log_dir / f"claude-usage-{day}.log"

    def _append(self, payload: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.now(get_timezone(self.timezone)).isoformat(),
            "date": datetime.now(get_timezone(self.timezone)).date().isoformat(),
            "command": self.command,
            "model": self.model,
            **payload,
        }
        with self._log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    *,
    input_price_per_mtok: float,
    output_price_per_mtok: float,
) -> float:
    return (input_tokens / 1_000_000) * input_price_per_mtok + (output_tokens / 1_000_000) * output_price_per_mtok


def usage_tracker_from_settings(settings: Any, *, command: str) -> ClaudeUsageTracker:
    log_dir = Path(getattr(settings, "claude_usage_log_dir", "./data/logs"))
    return ClaudeUsageTracker(
        log_dir=log_dir,
        model=settings.claude_model,
        timezone=settings.timezone,
        command=command,
        input_price_per_mtok=float(getattr(settings, "claude_input_price_per_mtok", _DEFAULT_INPUT_PER_MTOK)),
        output_price_per_mtok=float(getattr(settings, "claude_output_price_per_mtok", _DEFAULT_OUTPUT_PER_MTOK)),
    )
