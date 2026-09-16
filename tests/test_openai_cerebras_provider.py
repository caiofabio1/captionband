"""Tests for OpenAICerebrasProvider — verifies STT goes to OpenAI default URL
while translation goes to Cerebras.
"""
from unittest.mock import patch

import pytest

from providers.cerebras import CEREBRAS_BASE_URL
from providers.openai_cerebras import OPENAI_BASE_URL, OpenAICerebrasProvider


def _make_provider():
    return OpenAICerebrasProvider(
        openai_api_key="sk-fake-openai",
        cerebras_api_key="cb-fake-cerebras",
        openai_stt_model="whisper-1",
        cerebras_translation_model="llama-3.3-70b",
        source_languages=["pt-BR", "en-US"],
        target_languages=["es"],
        on_event=lambda e: None,
        samplerate=16000,
        chunk_seconds=4.0,
    )


def test_init_requires_openai_key():
    with pytest.raises(ValueError, match="openai_api_key"):
        OpenAICerebrasProvider(
            openai_api_key="",
            cerebras_api_key="cb-key",
            openai_stt_model="whisper-1",
            cerebras_translation_model="llama-3.3-70b",
            source_languages=["en-US"],
            target_languages=["es"],
            on_event=lambda e: None,
        )


def test_init_requires_cerebras_key():
    with pytest.raises(ValueError, match="cerebras_api_key"):
        OpenAICerebrasProvider(
            openai_api_key="sk-key",
            cerebras_api_key="",
            openai_stt_model="whisper-1",
            cerebras_translation_model="llama-3.3-70b",
            source_languages=["en-US"],
            target_languages=["es"],
            on_event=lambda e: None,
        )


def test_stt_model_returns_openai_model():
    prov = _make_provider()
    assert prov._stt_model_for_call() == "whisper-1"


def test_translation_model_returns_cerebras_model():
    prov = _make_provider()
    assert prov._translation_model_for_call() == "llama-3.3-70b"


def test_clients_use_correct_base_urls():
    """STT client → OpenAI (default URL), translation client → Cerebras URL."""
    with patch("providers.groq.OpenAI") as mock_groq, \
         patch("providers.cerebras.OpenAI") as mock_cerebras, \
         patch("providers.openai_cerebras.OpenAI") as mock_openai:
        prov = _make_provider()
        prov.start()
        # STT client is built by openai_cerebras.py
        openai_call = mock_openai.call_args_list[0]
        assert openai_call.kwargs["api_key"] == "sk-fake-openai"
        # OpenAI default — NO base_url override
        assert "base_url" not in openai_call.kwargs or openai_call.kwargs.get("base_url") is None
        # Translation client is built by cerebras.py
        cerebras_call = mock_cerebras.call_args_list[0]
        assert cerebras_call.kwargs["base_url"] == CEREBRAS_BASE_URL
        assert cerebras_call.kwargs["api_key"] == "cb-fake-cerebras"
        prov.stop()
