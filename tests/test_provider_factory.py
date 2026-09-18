"""Tests for the provider factory and TranslationEvent dataclass."""
from __future__ import annotations

import pytest

from config import AppConfig
from providers import ProviderUnavailable, build_provider, provider_capabilities
from providers.base import TranslationEvent, TranslationProvider

from .conftest import requires_azure


def noop_callback(event: TranslationEvent) -> None:
    pass


class TestBuildProvider:
    def test_unknown_provider_raises(self):
        cfg = AppConfig(provider="invalid_xyz")
        # ProviderUnavailable rather than a bare ValueError, so the tray can
        # catch exactly this case and show an actionable message instead of
        # generic "failed to start" boilerplate.
        with pytest.raises(ProviderUnavailable, match="desconhecido"):
            build_provider(cfg, on_event=noop_callback)

    @requires_azure
    def test_azure_built_with_required_fields(self):
        cfg = AppConfig(
            provider="azure",
            azure_speech_key="key",
            azure_speech_region="brazilsouth",
        )
        prov = build_provider(cfg, on_event=noop_callback)
        assert isinstance(prov, TranslationProvider)
        assert not prov.is_running

    @requires_azure
    def test_azure_missing_key_raises(self):
        cfg = AppConfig(provider="azure", azure_speech_region="brazilsouth")
        with pytest.raises(ValueError, match="speech_key"):
            build_provider(cfg, on_event=noop_callback)

    def test_openrouter_built_with_api_key(self):
        cfg = AppConfig(provider="openrouter", openrouter_api_key="sk-or-test")
        prov = build_provider(cfg, on_event=noop_callback)
        assert isinstance(prov, TranslationProvider)
        assert not prov.is_running

    def test_openrouter_missing_key_raises(self):
        cfg = AppConfig(provider="openrouter")
        with pytest.raises(ValueError, match="api_key"):
            build_provider(cfg, on_event=noop_callback)

    def test_google_supports_max_4_source_languages(self):
        cfg = AppConfig(
            provider="google",
            google_credentials_json='{"type":"service_account"}',
            google_project_id="proj",
            source_languages=["pt-BR", "en-US", "es-ES", "fr-FR", "it-IT"],
        )
        with pytest.raises(ValueError, match="at most 4"):
            build_provider(cfg, on_event=noop_callback)

    @requires_azure
    def test_azure_supports_max_10_source_languages(self):
        cfg = AppConfig(
            provider="azure",
            azure_speech_key="k",
            source_languages=[f"x{i}-XX" for i in range(11)],
        )
        with pytest.raises(ValueError, match="at most 10"):
            build_provider(cfg, on_event=noop_callback)


class TestTranslationEvent:
    def test_optional_fields_default_to_none_or_empty(self):
        e = TranslationEvent(
            detected_language="pt-BR",
            original_text="hello",
            translations={},
            is_final=False,
        )
        assert e.audio_emitted_at_ms is None
        assert e.translations == {}

    def test_final_event_with_translations(self):
        e = TranslationEvent(
            detected_language="pt-BR",
            original_text="bom dia",
            translations={"es": "buenos días", "en": "good morning"},
            is_final=True,
            audio_emitted_at_ms=1234.5,
        )
        assert e.is_final
        assert e.audio_emitted_at_ms == 1234.5
        assert e.translations["es"] == "buenos días"




def test_factory_builds_openrouter():
    from config import AppConfig
    cfg = AppConfig(
        provider="openrouter",
        openrouter_api_key="or-fake",
    )
    cfg.target_languages = ["es"]
    from providers import build_provider
    from providers.openrouter import OpenRouterProvider
    prov = build_provider(cfg, on_event=lambda e: None)
    assert isinstance(prov, OpenRouterProvider)


class TestCapabilities:
    """The controller picks its reorder strategy from these, so a wrong value
    silently scrambles captions (a chunk provider marked ordered) or adds
    latency for nothing (a streaming provider marked unordered)."""

    def test_unknown_provider_has_no_capabilities(self):
        assert provider_capabilities("invalid_xyz") is None

    def test_undeclared_capabilities_default_to_gated(self):
        # A provider that forgets to declare must inherit the SAFE default:
        # assume its results can race, so the reorder gate stays on.
        assert TranslationProvider.CAPABILITIES.ordered_by_protocol is False
