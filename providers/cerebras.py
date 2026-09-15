# providers/cerebras.py
"""Cerebras provider — Groq Whisper for STT + Cerebras Llama for translation.

Cerebras Inference (https://inference.cerebras.ai) offers an
OpenAI-compatible API with Llama-3.3-70b at ~1800 tokens/sec, faster
than Groq. They DO NOT host Whisper, so we still use Groq's free-tier
Whisper Turbo for the STT step.

Same code path as GroqProvider — just overrides translation to point at
Cerebras's base URL.

Free tier (May 2026): 30 RPM, 60K TPM, 1M tokens/day, no waitlist.
Paid: $0.60/Mtok input — sign up at https://inference.cerebras.ai.
"""
from __future__ import annotations

import logging

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

from .groq import GroqProvider
from .base import ProviderCapabilities, OnTranslationCallback


log = logging.getLogger(__name__)


CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"


class CerebrasProvider(GroqProvider):
    """STT via Groq Whisper (inherited) + translation via Cerebras Llama."""

    # Inherits GroqProvider's chunked REST pipeline, so results can
    # race: the reorder gate stays on. Declared explicitly rather than
    # inherited so the operator UI shows THIS provider, not Groq.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False,
        translates=True,
        streaming=False,
        label="Cerebras (Llama 3.3 70B + Groq Whisper)",
    )

    def __init__(
        self,
        api_key: str,                       # Groq key (used for Whisper STT only)
        cerebras_api_key: str,              # Cerebras key (used for translation)
        transcription_model: str,
        cerebras_translation_model: str,
        source_languages: list[str],
        target_languages: list[str],
        on_event: OnTranslationCallback,
        samplerate: int = 16000,
        chunk_seconds: float = 4.0,
    ):
        if not cerebras_api_key:
            raise ValueError("cerebras_api_key required")
        # GroqProvider validates api_key
        super().__init__(
            api_key=api_key,
            transcription_model=transcription_model,
            translation_model=cerebras_translation_model,  # stored on parent for logging
            source_languages=source_languages,
            target_languages=target_languages,
            on_event=on_event,
            samplerate=samplerate,
            chunk_seconds=chunk_seconds,
        )
        self.cerebras_api_key = cerebras_api_key
        self.cerebras_translation_model = cerebras_translation_model

    def _build_translation_client(self):
        log.info("cerebras provider: translation_model=%s", self.cerebras_translation_model)
        return OpenAI(
            api_key=self.cerebras_api_key,
            base_url=CEREBRAS_BASE_URL,
            timeout=30.0,
            max_retries=2,
        )

    def _translation_model_for_call(self) -> str:
        return self.cerebras_translation_model
