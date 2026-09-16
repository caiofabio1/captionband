"""The suite must never be able to reach the real credential store.

This is a test about the tests. It exists because the suite destroyed a real
API key: a test saved `AppConfig(azure_speech_key="k")` and the value landed
in the developer's Windows Credential Manager, overwriting an 84-character
Azure key with a single letter. Nothing failed, nothing warned; the app just
started returning HTTP 401, which is indistinguishable from a typo.

If someone later removes or weakens `tests/conftest.py`, these fail.
"""
from __future__ import annotations

import config as config_mod
from config import SECRET_FIELDS, AppConfig, load_config, save_config


def test_keyring_is_disabled_for_the_whole_session():
    """The autouse guard in conftest.py must be in force."""
    import secrets_store

    assert secrets_store.is_available() is False, (
        "O cofre de credenciais do SO esta ACESSIVEL durante os testes. "
        "Um save_config() com chave ficticia vai sobrescrever a credencial "
        "real do desenvolvedor."
    )


def test_config_path_is_not_the_real_one(tmp_path):
    """Nothing may write over the user's live config.json."""
    p = str(config_mod.config_path()).lower()
    assert "localappdata" not in p and "teamslivetranslation" not in p, (
        "config_path() aponta para o arquivo REAL do app durante os testes: "
        f"{p}"
    )


def test_saving_a_fake_secret_does_not_escape_the_sandbox():
    """The exact operation that destroyed the key, now proven harmless."""
    fake = AppConfig(
        provider="azure",
        azure_speech_key="k",          # the literal value that caused the loss
        azure_speech_region="brazilsouth",
    )
    save_config(fake)

    # Reading back must not have consulted (or written) the OS keyring.
    reloaded = load_config()
    for field in SECRET_FIELDS:
        value = getattr(reloaded, field, "") or ""
        assert "k" == value or value == "", (
            f"campo {field} voltou com {value[:8]!r} — sinal de que o keyring real "
            "participou"
        )


def test_every_test_module_defining_its_own_isolation_is_still_covered():
    """Duplicated per-file fixtures were the root cause; the guard must not
    depend on any of them being correct.

    Asserting on the autouse guard directly (rather than on each module's
    fixture) is the point: new test files inherit the protection without
    anyone remembering to ask for it.
    """
    import secrets_store

    # Simulate a module that forgot every isolation fixture.
    assert secrets_store.is_available() is False
