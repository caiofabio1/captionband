"""Lightweight local usage tracker.

We don't have access to provider billing APIs without each customer
configuring scopes/permissions, so instead we track usage *locally* —
seconds of audio sent per provider per day. Stored as JSON in
%LOCALAPPDATA%\\CaptionBand\\usage.json.

The dashboard reads this and shows:
- Hours used this month per provider
- Estimated cost based on published rates (Apr 2026)
- Warnings when approaching free-tier limits

This is approximate — real billing comes from provider portals.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import app_data_dir

log = logging.getLogger(__name__)


# Published rates (USD per hour of audio) — adjust as providers change pricing
RATES_USD_PER_HOUR = {
    "azure": 2.50,             # S0 tier
    "cerebras": 0.0,           # free tier 1M tokens/day = ~33h webinars/day
    "google": 1.54,            # Speech v2 + Translate v3 combined
    "groq": 0.10,              # whisper-large-v3-turbo + Llama 3.3 70B
    "openai_cerebras": 0.36,   # OpenAI Whisper-1 (~$0.006/min) + Cerebras free
    "openrouter": 0.40,        # ~$0.36/h Whisper + ~$0.04/h Llama via OR markup
    "whisper_local": 0.0,      # local — no marginal cost
}

FREE_TIER_HOURS_MONTH = {
    "azure": 5.0,              # F0 tier
    "cerebras": float("inf"),
    "google": 1.0,             # 60 free minutes/month
    "groq": 0.0,               # rate-limited but no free hour quota
    "openai_cerebras": 0.0,    # OpenAI is paid from minute 1
    "openrouter": 0.0,
    "whisper_local": float("inf"),
}


def _usage_path() -> Path:
    return app_data_dir() / "usage.json"


@dataclass
class _Tracker:
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_seconds(self, provider: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            data = self._load()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            day = data.setdefault(today, {})
            day[provider] = day.get(provider, 0.0) + seconds
            self._save(data)

    def hours_for_month(self, provider: str, year_month: Optional[str] = None) -> float:
        with self._lock:
            data = self._load()
        ym = year_month or datetime.now(timezone.utc).strftime("%Y-%m")
        total = 0.0
        for day, providers in data.items():
            if day.startswith(ym):
                total += providers.get(provider, 0.0)
        return total / 3600.0

    def all_providers_summary(self) -> dict[str, dict]:
        """Return a dict per provider with hours, cost estimate, free remaining."""
        out: dict[str, dict] = {}
        ym = datetime.now(timezone.utc).strftime("%Y-%m")
        for prov in RATES_USD_PER_HOUR:
            hours = self.hours_for_month(prov, year_month=ym)
            free_limit = FREE_TIER_HOURS_MONTH.get(prov, 0.0)
            paid_hours = max(0.0, hours - free_limit) if free_limit != float("inf") else 0.0
            out[prov] = {
                "hours_month": hours,
                "free_remaining_hours": (
                    max(0.0, free_limit - hours) if free_limit != float("inf") else float("inf")
                ),
                "paid_hours": paid_hours,
                "estimated_cost_usd": paid_hours * RATES_USD_PER_HOUR[prov],
            }
        return out

    def prune_older_than_days(self, days: int = 90) -> None:
        with self._lock:
            data = self._load()
        cutoff = datetime.now(timezone.utc).date()
        keep: dict[str, dict] = {}
        for day_str, providers in data.items():
            try:
                d = datetime.strptime(day_str, "%Y-%m-%d").date()
                if (cutoff - d).days <= days:
                    keep[day_str] = providers
            except ValueError:
                continue
        with self._lock:
            self._save(keep)

    def _load(self) -> dict:
        p = _usage_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        try:
            _usage_path().write_text(
                json.dumps(data, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            log.exception("failed to save usage tracker")


_GLOBAL_TRACKER = _Tracker()


def add_seconds(provider: str, seconds: float) -> None:
    _GLOBAL_TRACKER.add_seconds(provider, seconds)


def summary() -> dict[str, dict]:
    return _GLOBAL_TRACKER.all_providers_summary()
