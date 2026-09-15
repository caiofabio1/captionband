"""Lightweight self-update checker.

Polls GitHub's public releases API at most once every UPDATE_CHECK_INTERVAL_S
and compares `tag_name` against APP_VERSION. Returns the URL of the latest
release if newer than installed; None otherwise.

Runs in a background thread on app startup so the UI is never blocked.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from config import app_data_dir
from constants import (
    APP_GITHUB_REPO,
    APP_VERSION,
    UPDATE_CHECK_INTERVAL_S,
    UPDATE_CHECK_TIMEOUT_S,
)

log = logging.getLogger(__name__)


@dataclass
class ReleaseInfo:
    tag: str
    name: str
    url: str
    published_at: str
    body: str


def _last_check_path():
    return app_data_dir() / "update_check.json"


def _read_last_check() -> float:
    try:
        data = json.loads(_last_check_path().read_text(encoding="utf-8"))
        return float(data.get("timestamp", 0))
    except Exception:
        return 0.0


def _write_last_check() -> None:
    try:
        _last_check_path().write_text(
            json.dumps({"timestamp": time.time(), "version": APP_VERSION}),
            encoding="utf-8",
        )
    except Exception:
        log.exception("failed to record update check timestamp")


def _parse_version(tag: str) -> tuple[int, ...]:
    """Convert 'v1.2.3' or '1.2.3' into (1, 2, 3) for comparison."""
    s = tag.lstrip("v").lstrip("V").strip()
    parts = []
    for p in s.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _fetch_latest_release() -> Optional[ReleaseInfo]:
    url = f"https://api.github.com/repos/{APP_GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"CaptionBand/{APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_CHECK_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ReleaseInfo(
            tag=data.get("tag_name", ""),
            name=data.get("name", ""),
            url=data.get("html_url", f"https://github.com/{APP_GITHUB_REPO}/releases"),
            published_at=data.get("published_at", ""),
            body=data.get("body", ""),
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log.debug("no releases yet on %s", APP_GITHUB_REPO)
        else:
            log.warning("update check HTTP %s", exc.code)
        return None
    except Exception:
        log.debug("update check failed (offline?)", exc_info=True)
        return None


def check_for_update_async(on_result: Callable[[Optional[ReleaseInfo]], None]) -> None:
    """Run the update check in a background daemon thread.

    Calls on_result(release_info) on the calling thread's event loop ONLY
    if a newer release is found. Otherwise calls on_result(None).
    Caller is responsible for marshaling back to the UI thread (e.g. via
    Qt signals if needed).
    """

    def worker():
        # Throttle by interval since last check
        last = _read_last_check()
        if UPDATE_CHECK_INTERVAL_S > 0 and (time.time() - last) < UPDATE_CHECK_INTERVAL_S:
            log.debug("skipping update check; last was %.0fs ago", time.time() - last)
            on_result(None)
            return

        info = _fetch_latest_release()
        _write_last_check()
        if info is None or not info.tag:
            on_result(None)
            return

        try:
            if _parse_version(info.tag) > _parse_version(APP_VERSION):
                log.info("update available: %s -> %s", APP_VERSION, info.tag)
                on_result(info)
            else:
                on_result(None)
        except Exception:
            log.exception("version comparison failed")
            on_result(None)

    threading.Thread(target=worker, name="update-checker", daemon=True).start()
