"""Tests for config load/save round-trip and validation."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import config as config_mod
from config import AppConfig, AudioConfig, OverlayConfig, load_config, save_config


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Isolate BOTH stores a config is assembled from: the JSON file and the
    OS keyring.

    Redirecting config_path() alone is not isolation. load_config() then calls
    _hydrate_secrets(), which reads the real Windows Credential Manager, so a
    test on a machine that has actually used the app gets that machine's live
    API keys — the test outcome depends on the developer's credential store,
    and worse, a failing assertion prints the real secret into the pytest
    output.

    Disabling the keyring here exercises the same code path the app already
    takes on a machine without one.
    """
    fake_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "config_path", lambda: fake_path)
    monkeypatch.setattr(config_mod, "app_data_dir", lambda: tmp_path)
    try:
        import secrets_store
        monkeypatch.setattr(secrets_store, "is_available", lambda: False)
    except ImportError:
        pass
    return fake_path


class TestRoundTrip:
    def test_default_config_is_invalid(self, isolated_config):
        cfg = load_config()
        # Empty default keys → cannot start any provider
        assert not cfg.is_valid()

    def test_save_then_load_preserves_provider_choice(self, isolated_config):
        cfg = AppConfig(
            provider="openrouter",
            openrouter_api_key="sk-or-test",
            source_languages=["pt-BR", "en-US"],
            target_languages=["es"],
        )
        save_config(cfg)
        loaded = load_config()
        assert loaded.provider == "openrouter"
        assert loaded.openrouter_api_key == "sk-or-test"
        assert loaded.source_languages == ["pt-BR", "en-US"]
        assert loaded.target_languages == ["es"]

    def test_overlay_config_round_trip(self, isolated_config):
        cfg = AppConfig(
            provider="azure",
            azure_speech_key="abc",
            overlay=OverlayConfig(
                position="top",
                primary_font_size=44,
                primary_color="#FF0000",
                max_history=3,
            ),
        )
        save_config(cfg)
        loaded = load_config()
        assert loaded.overlay.position == "top"
        assert loaded.overlay.primary_font_size == 44
        assert loaded.overlay.primary_color == "#FF0000"
        assert loaded.overlay.max_history == 3

    def test_audio_config_round_trip(self, isolated_config):
        cfg = AppConfig(
            provider="azure",
            azure_speech_key="abc",
            audio=AudioConfig(device_name="My Headphones", samplerate=24000, channels=2),
        )
        save_config(cfg)
        loaded = load_config()
        assert loaded.audio.device_name == "My Headphones"
        assert loaded.audio.samplerate == 24000
        assert loaded.audio.channels == 2


class TestValidation:
    def test_azure_requires_key_and_region(self):
        assert not AppConfig(provider="azure").is_valid()
        assert AppConfig(
            provider="azure",
            azure_speech_key="k",
            azure_speech_region="brazilsouth",
        ).is_valid()


    def test_google_requires_creds_and_project(self):
        assert not AppConfig(provider="google").is_valid()
        assert not AppConfig(provider="google", google_credentials_json="{}").is_valid()
        assert AppConfig(
            provider="google",
            google_credentials_json="{}",
            google_project_id="proj-1",
        ).is_valid()

    def test_whisper_local_only_requires_model(self):
        cfg = AppConfig(provider="whisper_local", whisper_model="small")
        assert cfg.is_valid()


class TestForwardCompatibility:
    def test_unknown_keys_are_silently_ignored(self, isolated_config):
        # Simulate config saved by a future version with extra fields
        isolated_config.write_text(
            json.dumps(
                {
                    "provider": "azure",
                    "azure_speech_key": "k",
                    "azure_speech_region": "brazilsouth",
                    "this_is_a_future_field": True,
                    "overlay": {"position": "bottom", "future_overlay_field": 42},
                }
            ),
            encoding="utf-8",
        )
        # Should load without raising
        cfg = load_config()
        assert cfg.provider == "azure"
        assert cfg.overlay.position == "bottom"




# test_overlay_config_new_fields_default removed in v0.5 — those fields
# (caption_layout, toggle_hotkey, visual_scheme, text_align, streaming_indicator,
# side_panel_width_ratio) were the v0.3 caption-redesign visual fields, which
# v0.5 reverts back to the v0.2.0 simpler overlay.
