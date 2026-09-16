"""Tests for Azure streaming mode + result_id partial dedup.

Covers:
- Config: streaming fields round-trip through save/load
- providers/__init__.py: factory passes streaming flags to AzureProvider
- AzureProvider: __init__ validates streaming inputs and selects the right
  recognizer-build path (without actually calling speechsdk)
- Overlay: partials with same result_id replace the last utterance in-place
  rather than stacking new lines

We don't hit the real Azure SDK — those branches require network + a key.
We mock just enough to verify branching.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import config as config_mod
from config import AppConfig, load_config, save_config

from .conftest import requires_azure

# ---------------------------------------------------------------------- config


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    fake_path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "config_path", lambda: fake_path)
    monkeypatch.setattr(config_mod, "app_data_dir", lambda: tmp_path)
    return fake_path


class TestStreamingConfigRoundTrip:
    def test_streaming_fields_persist(self, isolated_config):
        cfg = AppConfig(
            provider="azure",
            azure_speech_key="k",
            azure_streaming_mode=True,
            azure_streaming_language="en-US",
            azure_quick_languages=["pt-BR", "en-US"],
            azure_switch_hotkey="ctrl+f9",
        )
        save_config(cfg)
        loaded = load_config()
        assert loaded.azure_streaming_mode is True
        assert loaded.azure_streaming_language == "en-US"
        assert loaded.azure_quick_languages == ["pt-BR", "en-US"]
        assert loaded.azure_switch_hotkey == "ctrl+f9"

    def test_streaming_defaults(self, isolated_config):
        cfg = AppConfig()
        assert cfg.azure_streaming_mode is False
        assert cfg.azure_streaming_language == "pt-BR"
        assert "pt-BR" in cfg.azure_quick_languages
        assert cfg.azure_switch_hotkey == "f9"

    def test_unknown_streaming_fields_ignored(self, isolated_config):
        # Forward compat: a future field shouldn't break load
        isolated_config.write_text(
            json.dumps({
                "provider": "azure",
                "azure_speech_key": "k",
                "azure_streaming_mode": True,
                "azure_future_streaming_thing": 42,
            }),
            encoding="utf-8",
        )
        cfg = load_config()
        assert cfg.azure_streaming_mode is True


# ---------------------------------------------------------------------- factory


@requires_azure
class TestAzureFactoryWiring:
    def test_factory_passes_streaming_flags(self):
        from providers import build_provider

        cfg = AppConfig(
            provider="azure",
            azure_speech_key="k",
            azure_speech_region="brazilsouth",
            azure_streaming_mode=True,
            azure_streaming_language="en-US",
            source_languages=["pt-BR"],
            target_languages=["es"],
        )
        with patch("providers.azure.speechsdk") as mock_sdk:
            # Make minimal mocks so __init__ doesn't blow up
            mock_sdk.translation.SpeechTranslationConfig.return_value = MagicMock()
            mock_sdk.audio.AudioStreamFormat.return_value = MagicMock()
            mock_sdk.audio.PushAudioInputStream.return_value = MagicMock()
            mock_sdk.audio.AudioConfig.return_value = MagicMock()
            mock_sdk.translation.TranslationRecognizer.return_value = MagicMock()
            provider = build_provider(cfg, on_event=lambda e: None)

        assert provider.streaming_mode is True
        assert provider.streaming_language == "en-US"

    def test_factory_defaults_to_multilingual(self):
        from providers import build_provider

        cfg = AppConfig(
            provider="azure",
            azure_speech_key="k",
            azure_speech_region="brazilsouth",
            source_languages=["pt-BR", "en-US"],
            target_languages=["es"],
        )
        with patch("providers.azure.speechsdk") as mock_sdk:
            mock_sdk.translation.SpeechTranslationConfig.return_value = MagicMock()
            mock_sdk.audio.AudioStreamFormat.return_value = MagicMock()
            mock_sdk.audio.PushAudioInputStream.return_value = MagicMock()
            mock_sdk.audio.AudioConfig.return_value = MagicMock()
            mock_sdk.translation.TranslationRecognizer.return_value = MagicMock()
            provider = build_provider(cfg, on_event=lambda e: None)

        assert provider.streaming_mode is False


# ---------------------------------------------------------------------- provider


@requires_azure
class TestAzureProviderValidation:
    def test_streaming_mode_requires_streaming_language(self):
        from providers.azure import AzureProvider
        with pytest.raises(ValueError, match="streaming_language"):
            AzureProvider(
                speech_key="k", region="r",
                source_languages=[], target_languages=["es"],
                on_event=lambda e: None,
                streaming_mode=True,
                streaming_language="",
            )

    def test_multilingual_requires_source_languages(self):
        from providers.azure import AzureProvider
        with pytest.raises(ValueError, match="source language"):
            AzureProvider(
                speech_key="k", region="r",
                source_languages=[], target_languages=["es"],
                on_event=lambda e: None,
                streaming_mode=False,
            )

    def test_multilingual_caps_at_10_languages(self):
        from providers.azure import AzureProvider
        with pytest.raises(ValueError, match="10"):
            AzureProvider(
                speech_key="k", region="r",
                source_languages=[f"lang-{i}" for i in range(11)],
                target_languages=["es"],
                on_event=lambda e: None,
            )

    def test_set_source_language_noop_in_multilingual(self):
        from providers.azure import AzureProvider
        provider = AzureProvider(
            speech_key="k", region="r",
            source_languages=["pt-BR"], target_languages=["es"],
            on_event=lambda e: None,
            streaming_mode=False,
        )
        provider.set_source_language("en-US")
        # streaming_language is preserved at its default; provider didn't switch
        assert provider.streaming_language == "pt-BR"


# ---------------------------------------------------------------------- overlay


class TestOverlayPartialDedup:
    """Result_id partials must replace last utterance, not stack."""

    @pytest.fixture
    def overlay(self, qapp):
        from overlay_qt import CaptionOverlay
        cfg = AppConfig(
            provider="azure",
            azure_speech_key="k",
            target_languages=["es"],
            display_mode="original_plus_translation",
        )
        ov = CaptionOverlay(cfg)
        yield ov
        ov.close()

    def test_same_result_id_replaces_in_place(self, overlay):
        # Three partials growing word-by-word, then a final.
        overlay._on_update("Olá", {"es": "Hola"}, "pt-BR", False, 0.0, "rid-1")
        assert len(overlay._history) == 1
        assert overlay._history[-1].original == "Olá"
        assert overlay._history[-1].is_final is False

        overlay._on_update("Olá como", {"es": "Hola cómo"}, "pt-BR", False, 0.0, "rid-1")
        assert len(overlay._history) == 1, "partial must NOT add a new line"
        assert overlay._history[-1].original == "Olá como"
        assert overlay._history[-1].translations["es"] == "Hola cómo"

        overlay._on_update("Olá como vai você", {"es": "Hola cómo estás tú"}, "pt-BR", True, 0.0, "rid-1")
        assert len(overlay._history) == 1
        assert overlay._history[-1].is_final is True
        assert overlay._history[-1].original == "Olá como vai você"

    def test_real_azure_partials_have_distinct_ids_and_still_replace(self, overlay):
        # MEASURED against the real service (2026-09-15): every recognizing
        # and recognized event carries its own result_id — 6 ids for the 6
        # events of one sentence. Keying the in-place replace on the id
        # therefore never fired, and each partial became a new caption line.
        overlay._on_update("bom dia", {"es": "buenos días"}, "pt-BR", False, 0.0, "b21e629d")
        overlay._on_update("bom dia a todos", {"es": "buenos días a todos"}, "pt-BR", False, 0.0, "e260de18")
        overlay._on_update("bom dia a todos sejam bem", {"es": "buenos días a todos sean"}, "pt-BR", False, 0.0, "ba3dd7b7")
        overlay._on_update("Bom dia a todos, sejam bem-vindos.", {"es": "Buenos días a todos, bienvenidos."}, "pt-BR", True, 0.0, "55e39e6a")
        assert len(overlay._history) == 1, [u.original for u in overlay._history]
        assert overlay._history[-1].original == "Bom dia a todos, sejam bem-vindos."
        assert overlay._history[-1].is_final is True
        # The NEXT sentence, after a final, does start a new line.
        overlay._on_update("a primeira", {"es": "la primera"}, "pt-BR", False, 0.0, "7a46613e")
        assert len(overlay._history) == 2

    def test_partial_translation_grows_instead_of_flickering(self, overlay):
        overlay._on_update("esse atendimento ao cliente", {"es": "Este servicio al cliente"}, "pt-BR", False, 0.0, "1")
        # A shorter re-wording of the same partial must not replace it…
        overlay._on_update("esse atendimento ao cliente quem", {"es": "Ese servicio"}, "pt-BR", False, 0.0, "2")
        assert overlay._history[-1].translations["es"] == "Este servicio al cliente"
        assert overlay._history[-1].original == "esse atendimento ao cliente quem"
        # …but the final always wins.
        overlay._on_update("Esse atendimento ao cliente, quem?", {"es": "¿Ese?"}, "pt-BR", True, 0.0, "3")
        assert overlay._history[-1].translations["es"] == "¿Ese?"

    def test_different_result_id_does_not_replace(self, overlay):
        # We use different languages so the existing Case C auto-concat
        # (same-language continuous speech) doesn't muddy the test — we only
        # care that result_id mismatch does NOT trigger Case A0 in-place
        # replace. Two utterances must be tracked separately.
        overlay._on_update("First utterance.", {"es": "Primera."}, "pt-BR", True, 0.0, "rid-A")
        overlay._on_update("Second one.", {"es": "Segunda."}, "en-US", True, 0.0, "rid-B")
        assert len(overlay._history) == 2
        assert overlay._history[0].result_id == "rid-A"
        assert overlay._history[1].result_id == "rid-B"

    def test_empty_result_id_falls_back_to_text_match(self, overlay):
        # Chunk-final providers send result_id="" — must use the existing
        # same-original Case A merge logic.
        overlay._on_update("Hello.", {}, "en-US", False, 0.0, "")
        overlay._on_update("Hello.", {"es": "Hola."}, "en-US", True, 0.0, "")
        assert len(overlay._history) == 1
        assert overlay._history[-1].translations["es"] == "Hola."

    def test_streaming_does_not_auto_concat_consecutive_utterances(self, overlay):
        # Bug repro: in streaming mode, two distinct Azure utterances
        # (different result_ids) emitted back-to-back must NOT be merged into
        # one ever-growing line by Case C. Each utterance gets its own line.
        # Same language, both final, fired within concat_gap_ms — chunk-final
        # providers WOULD concat here, but streaming must not.
        overlay._on_update("Olá como vai?", {"es": "Hola cómo estás?"}, "pt-BR", True, 0.0, "rid-A")
        overlay._on_update("Tudo bem, obrigado.", {"es": "Bien, gracias."}, "pt-BR", True, 0.0, "rid-B")
        assert len(overlay._history) == 2
        assert overlay._history[0].original == "Olá como vai?"
        assert overlay._history[1].original == "Tudo bem, obrigado."

    def test_chunk_final_still_auto_concats_when_no_result_id(self, overlay):
        # Regression guard: chunk-final providers (Groq, Whisper local) must
        # keep their auto-concat behavior. They send result_id="".
        overlay._on_update("Olá como vai", {"es": "Hola cómo estás"}, "pt-BR", True, 0.0, "")
        overlay._on_update("tudo bem.", {"es": "todo bien."}, "pt-BR", True, 0.0, "")
        assert len(overlay._history) == 1
        assert "Olá como vai" in overlay._history[-1].original
        assert "tudo bem" in overlay._history[-1].original


@pytest.fixture(scope="session")
def qapp():
    import sys

    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
