"""Test-wide safety net.

Why this file exists
--------------------
`save_config()` writes every secret field into the operating system's
credential store. A test that builds an `AppConfig(azure_speech_key="k")` and
saves it therefore OVERWRITES the developer's real Azure key with the string
"k" — silently, with no error, and permanently. The key cannot be recovered;
it has to be fetched from the vendor portal again.

This is not hypothetical. It happened: a run of this suite replaced a live
84-character Azure key with "k", and the only symptom was the app failing
with HTTP 401 afterwards, which looks exactly like a mistyped credential.

Per-file `isolated_config` fixtures were supposed to prevent it, but each test
module defined its OWN copy and only one of them disabled the keyring. That is
the failure mode of duplicated fixtures: fixing the bug in one copy leaves it
live in the others, and nothing tells you the other copies exist.

So the guard lives here instead, `autouse=True` at session scope: it applies
to every test in every file, including tests written later by someone who has
never read this comment. A test cannot opt out by forgetting a fixture,
because there is no fixture to forget.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_real_keyring():
    """Disable the OS credential store for the whole test session.

    `config._hydrate_secrets()` and `config.save_config()` both check
    `secrets_store.is_available()` before reading or writing. Returning False
    makes them take the same path the app already takes on a machine with no
    keyring — so this hides nothing that production depends on, it only
    removes the ability to reach the developer's real secrets.
    """
    try:
        import secrets_store
    except ImportError:
        yield
        return

    original = secrets_store.is_available
    secrets_store.is_available = lambda: False
    try:
        yield
    finally:
        secrets_store.is_available = original


@pytest.fixture(autouse=True)
def _keep_config_out_of_appdata(monkeypatch, tmp_path):
    """Point config.json at a temp dir for every test by default.

    Same reasoning as above: a test that calls `save_config()` without
    requesting an isolation fixture would otherwise rewrite the real
    config.json under %LOCALAPPDATA%. Tests that need a specific path still
    override this with their own fixture; this is only the floor.
    """
    import config as config_mod

    fallback = tmp_path / "conftest-config.json"
    monkeypatch.setattr(config_mod, "config_path", lambda: fallback, raising=False)
    monkeypatch.setattr(config_mod, "app_data_dir", lambda: tmp_path, raising=False)
    yield
