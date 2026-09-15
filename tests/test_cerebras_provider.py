# tests/test_cerebras_provider.py
"""Tests for CerebrasProvider — verifies translation goes to Cerebras URL
while STT stays on Groq Whisper.
"""
from unittest.mock import MagicMock, patch

import pytest

from providers.cerebras import CerebrasProvider, CEREBRAS_BASE_URL


def _make_provider():
    return CerebrasProvider(
        api_key="groq-fake-key",                          # for STT
        cerebras_api_key="cb-fake-key",                   # for translation
        transcription_model="whisper-large-v3-turbo",
        cerebras_translation_model="llama-3.3-70b",
        source_languages=["pt-BR", "en-US"],
        target_languages=["es"],
        on_event=lambda e: None,
        samplerate=16000,
        chunk_seconds=4.0,
    )


def test_init_requires_cerebras_key():
    with pytest.raises(ValueError, match="cerebras_api_key"):
        CerebrasProvider(
            api_key="groq-key",
            cerebras_api_key="",
            transcription_model="whisper-large-v3-turbo",
            cerebras_translation_model="llama-3.3-70b",
            source_languages=["en-US"],
            target_languages=["es"],
            on_event=lambda e: None,
        )


def test_init_requires_groq_key_for_stt():
    with pytest.raises(ValueError, match="api_key"):
        CerebrasProvider(
            api_key="",
            cerebras_api_key="cb-key",
            transcription_model="whisper-large-v3-turbo",
            cerebras_translation_model="llama-3.3-70b",
            source_languages=["en-US"],
            target_languages=["es"],
            on_event=lambda e: None,
        )


def test_translation_client_uses_cerebras_url():
    with patch("providers.groq.OpenAI") as mock_openai_groq, \
         patch("providers.cerebras.OpenAI") as mock_openai_cerebras:
        prov = _make_provider()
        prov.start()
        # First OpenAI() call is STT (Groq), second is translation (Cerebras)
        cerebras_call = mock_openai_cerebras.call_args_list[0]
        assert cerebras_call.kwargs["base_url"] == CEREBRAS_BASE_URL
        assert cerebras_call.kwargs["api_key"] == "cb-fake-key"
        prov.stop()


def test_translation_uses_cerebras_model_name():
    prov = _make_provider()
    assert prov._translation_model_for_call() == "llama-3.3-70b"
