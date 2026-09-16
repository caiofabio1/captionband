"""Version sync guard.

The app version lives in three places with no single source of truth:

- constants.py:      APP_VERSION = "x.y.z"   (compared against GitHub tags
                                              by updater.py for auto-update)
- installer.iss:     #define AppVersion "x.y.z"
- pyproject.toml:    [project] version = "x.y.z"

If they drift, the auto-updater silently compares the wrong version against
release tags and the installer stamps a different version than the app
reports. This test fails the moment any of the three diverges.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _app_version_from_constants() -> str:
    text = (ROOT / "constants.py").read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "APP_VERSION assignment not found in constants.py"
    return m.group(1)


def _app_version_from_installer() -> str:
    text = (ROOT / "installer.iss").read_text(encoding="utf-8")
    m = re.search(r'^#define\s+AppVersion\s+"([^"]+)"', text, re.MULTILINE)
    assert m, "#define AppVersion not found in installer.iss"
    return m.group(1)


def _app_version_from_pyproject() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    assert version, "project.version not found in pyproject.toml"
    return version


def test_version_synced_across_app_installer_and_package() -> None:
    versions = {
        "constants.py APP_VERSION": _app_version_from_constants(),
        "installer.iss AppVersion": _app_version_from_installer(),
        "pyproject.toml version": _app_version_from_pyproject(),
    }
    distinct = set(versions.values())
    assert len(distinct) == 1, (
        "version drift — updater.py compares constants.APP_VERSION against "
        "GitHub release tags, so all three must match:\n"
        + "\n".join(f"  {where} = {v}" for where, v in versions.items())
    )