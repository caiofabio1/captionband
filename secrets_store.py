"""Secure storage for API keys and credentials.

Uses the OS-native credential manager via the `keyring` library:
- Windows: Windows Credential Manager (DPAPI-encrypted with user's login)
- macOS:   Keychain
- Linux:   Secret Service / KWallet

Secrets stored: groq_api_key, azure_speech_key, google_credentials_json.
The path to the Google service account JSON is stored as a separate, less
sensitive value (config.json keeps the path; the actual JSON contents stay
in keyring or on disk in user-only readable mode).

When `keyring` is unavailable (rare on Windows but possible on stripped
images), every get_secret() call returns None and set_secret()/delete_secret()
return False. The app keeps working because config.py then falls back to
keeping the API keys in config.json — writing them in PLAIN TEXT and logging
a loud warning. This module itself never touches that file; it only talks to
the OS credential store.
"""
from __future__ import annotations

import logging

from constants import APP_NAME

log = logging.getLogger(__name__)


SERVICE = APP_NAME  # appears as "CaptionBand" in Credential Manager

# The app was called "Teams Live Translation" until 2026-09-15, and the service
# name is what Credential Manager keys the entry on. Without this the rename
# would silently hide the operator's Azure key, and the app would come up
# asking for a credential it already had — on the morning of an event.
LEGACY_SERVICES = ("TeamsLiveTranslation",)

SECRET_KEYS = (
    "groq_api_key",
    "azure_speech_key",
    "google_credentials_json",
)


def _backend():
    """Return the keyring module if available, else None."""
    try:
        import keyring
        # On Windows make sure a real backend is set (not the null backend)
        backend = keyring.get_keyring()
        if backend is None or "null" in type(backend).__name__.lower():
            log.warning("keyring has no backend; falling back to plaintext")
            return None
        return keyring
    except ImportError:
        log.warning("keyring not installed; falling back to plaintext")
        return None
    except Exception:
        log.exception("keyring init failed; falling back to plaintext")
        return None


def get_secret(key: str) -> str | None:
    """Return the secret stored under SERVICE/key, or None if unset/unavailable."""
    kr = _backend()
    if kr is None:
        return None
    try:
        value = kr.get_password(SERVICE, key)
    except Exception:
        log.exception("failed to read secret %s from keyring", key)
        return None
    if value:
        return value
    # Nothing under the current name: look under the names this app used to
    # have and move it across, so this happens at most once.
    for legacy in LEGACY_SERVICES:
        try:
            old_value = kr.get_password(legacy, key)
        except Exception:
            continue
        if old_value:
            log.info("migrating secret %s from the previous app name %r", key, legacy)
            try:
                kr.set_password(SERVICE, key, old_value)
            except Exception:
                log.exception("could not copy secret %s to the new name", key)
            return old_value
    return None


def set_secret(key: str, value: str) -> bool:
    """Store the secret. Returns True on success, False if no backend."""
    if not value:
        return delete_secret(key)
    kr = _backend()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE, key, value)
        return True
    except Exception:
        log.exception("failed to write secret %s to keyring", key)
        return False


def delete_secret(key: str) -> bool:
    kr = _backend()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE, key)
        return True
    except Exception:
        # delete_password raises PasswordDeleteError if the key doesn't exist;
        # treat that as success (idempotent)
        return True


def is_available() -> bool:
    """True if a real keyring backend is configured."""
    return _backend() is not None
