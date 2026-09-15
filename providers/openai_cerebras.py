"""OpenAI Whisper + Cerebras Llama provider — paid SLA, no Groq dependency.

Combines:
- OpenAI Whisper API (whisper-1 / gpt-4o-transcribe) for STT — paid, ~$0.36/h, SLA
- Cerebras Llama 3.3 70B for translation — free 1M tokens/day or paid $10 min

Targets users who want production reliability without Groq's free-tier risk.

Both APIs are OpenAI-compatible (Cerebras uses base_url override, OpenAI Whisper
is the original). The pipeline is identical to GroqProvider — VAD chunking,
push to STT, push transcript to translation, emit caption events — only the
clients differ.
"""
from __future__ import annotations

import logging

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

from .cerebras import CerebrasProvider
from .base import ProviderCapabilities, OnTranslationCallback


log = logging.getLogger(__name__)


# OpenAI default base URL (used by SDK when not overridden)
OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAICerebrasProvider(CerebrasProvider):
    """OpenAI Whisper STT + Cerebras Llama translation."""

    # Inherits GroqProvider's chunked REST pipeline, so results can
    # race: the reorder gate stays on. Declared explicitly rather than
    # inherited so the operator UI shows THIS provider, not Groq.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False,
        translates=True,
        streaming=False,
        label="OpenAI Whisper + Cerebras",
    )

    def __init__(
        self,
        openai_api_key: str,                # for STT (OpenAI Whisper)
        cerebras_api_key: str,              # for translation (Cerebras Llama)
        openai_stt_model: str,              # "whisper-1" or "gpt-4o-transcribe"
        cerebras_translation_model: str,
        source_languages: list[str],
        target_languages: list[str],
        on_event: OnTranslationCallback,
        samplerate: int = 16000,
        chunk_seconds: float = 4.0,
    ):
        if not openai_api_key:
            raise ValueError("openai_api_key required")
        # Pass openai_api_key as api_key to GroqProvider — it would normally
        # build a Groq STT client with this, but we override _build_stt_client
        # below so the value is reused as the OpenAI key.
        super().__init__(
            api_key=openai_api_key,         # not actually sent to Groq — see override
            cerebras_api_key=cerebras_api_key,
            transcription_model=openai_stt_model,
            cerebras_translation_model=cerebras_translation_model,
            source_languages=source_languages,
            target_languages=target_languages,
            on_event=on_event,
            samplerate=samplerate,
            chunk_seconds=chunk_seconds,
        )
        self.openai_api_key = openai_api_key
        self.openai_stt_model = openai_stt_model

    def _build_stt_client(self):
        log.info("openai_cerebras provider: stt_model=%s", self.openai_stt_model)
        return OpenAI(
            api_key=self.openai_api_key,
            timeout=30.0,
            max_retries=2,
        )

    def _stt_model_for_call(self) -> str:
        return self.openai_stt_model
