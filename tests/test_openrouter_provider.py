"""Tests for OpenRouterProvider — verifies both STT and translation route to OpenRouter."""
from unittest.mock import patch

import pytest

from providers.openrouter import OPENROUTER_BASE_URL, OpenRouterProvider


def _make_provider():
    return OpenRouterProvider(
        openrouter_api_key="or-fake-key",
        openrouter_stt_model="openai/whisper-1",
        openrouter_translation_model="openai/gpt-oss-120b",
        source_languages=["pt-BR", "en-US"],
        target_languages=["es"],
        on_event=lambda e: None,
        samplerate=16000,
        chunk_seconds=4.0,
    )


def test_init_requires_api_key():
    with pytest.raises(ValueError, match="openrouter_api_key"):
        OpenRouterProvider(
            openrouter_api_key="",
            openrouter_stt_model="openai/whisper-1",
            openrouter_translation_model="openai/gpt-oss-120b",
            source_languages=["en-US"],
            target_languages=["es"],
            on_event=lambda e: None,
        )


def test_stt_model_returns_openrouter_model():
    prov = _make_provider()
    assert prov._stt_model_for_call() == "openai/whisper-1"


def test_translation_model_returns_openrouter_model():
    prov = _make_provider()
    assert prov._translation_model_for_call() == "openai/gpt-oss-120b"


def test_both_clients_use_openrouter_url():
    """Both STT and translation clients must point at OpenRouter."""
    with patch("providers.groq.OpenAI") as mock_groq, \
         patch("providers.openrouter.OpenAI") as mock_or:
        prov = _make_provider()
        prov.start()
        # Both calls should go through openrouter module's OpenAI patch
        # (not groq module's, since we override BOTH hooks)
        assert mock_or.call_count == 2  # STT + translation
        for call in mock_or.call_args_list:
            assert call.kwargs["base_url"] == OPENROUTER_BASE_URL
            assert call.kwargs["api_key"] == "or-fake-key"
        prov.stop()
