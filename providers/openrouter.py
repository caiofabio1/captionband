"""OpenRouter all-in-one provider — single API key for STT + translation.

OpenRouter (https://openrouter.ai) is an OpenAI-compatible aggregator that
routes to 200+ models from multiple providers behind a single API key. This
provider uses OpenRouter for both:
- STT: Whisper variants via /chat/completions with input_audio content type
       (OpenRouter does NOT accept multipart audio uploads — they reject the
        OpenAI SDK's audio.transcriptions.create() with a JSON parse error.
        Their documented audio path is base64-in-message via chat completions.)
- Translation: openai/gpt-oss-* / meta-llama/* via /chat/completions

Trade-off vs going direct to providers:
+ 1 API key replaces several accounts
+ Built-in failover (OpenRouter routes to fallback if primary down)
+ Easy to swap models without re-cadastrar contas
- ~25-40ms added latency per call (proxy hop)
- Single point of failure (OpenRouter outage = total outage)
- Slight cost markup (~5-10%) on some routes
- Audio uses chat-completions trick — slightly more verbose than direct STT API

Recommended for users who prefer simplicity over absolute lowest latency.
"""
from __future__ import annotations

import base64
import logging

from .base import (
    CODE_AUTH,
    STATUS_FATAL,
    OnTranslationCallback,
    ProviderCapabilities,
)
from .chunked_rest import ChunkedRestProvider

log = logging.getLogger(__name__)


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# Module-level import so tests can patch it, and so a broken install degrades
# to "provider unavailable" instead of taking the module down.
#
# `except ImportError` is NOT enough, and this is not hypothetical: on
# 2026-09-21 something upgraded pydantic-core to 2.49.0 in the user's global
# site-packages while pydantic 2.13.5 still required 2.46.5, and `import
# openai` started raising SystemError. ImportError does not catch that, so
# importing this module died outright -- which is worse than the missing
# dependency the guard was written for. providers/__init__.py already caught
# BaseException at the factory for exactly this reason; the per-module guard
# had stayed narrow.
try:
    from openai import OpenAI
except Exception:      # SystemError from a dependency clash included
    OpenAI = None      # type: ignore[assignment,misc]


class OpenRouterProvider(ChunkedRestProvider):
    """Single-key provider: STT + translation both via OpenRouter."""

    # Inherits ChunkedRestProvider's chunked REST pipeline, so results can
    # race: the reorder gate stays on. Declared explicitly rather than
    # inherited so the operator UI shows THIS provider, not Groq.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False,
        translates=True,
        streaming=False,
        label="OpenRouter (1 chave para tudo)",
    )

    def __init__(
        self,
        openrouter_api_key: str,
        openrouter_stt_model: str,           # e.g. "openai/whisper-1"
        openrouter_translation_model: str,   # e.g. "openai/gpt-oss-120b"
        source_languages: list[str],
        target_languages: list[str],
        on_event: OnTranslationCallback,
        samplerate: int = 16000,
        chunk_seconds: float = 4.0,
    ):
        if not openrouter_api_key:
            raise ValueError("openrouter_api_key required")
        # Pass openrouter_api_key as api_key — ChunkedRestProvider's start() will call
        # our _build_stt_client which uses the OpenRouter URL. The transcription_model
        # and translation_model on the parent class are unused here because we
        # override the model-name hooks too.
        super().__init__(
            api_key=openrouter_api_key,
            transcription_model=openrouter_stt_model,
            translation_model=openrouter_translation_model,
            source_languages=source_languages,
            target_languages=target_languages,
            on_event=on_event,
            samplerate=samplerate,
            chunk_seconds=chunk_seconds,
        )
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_stt_model = openrouter_stt_model
        self.openrouter_translation_model = openrouter_translation_model

    def _build_stt_client(self):
        if OpenAI is None:
            raise RuntimeError("openai package required for OpenRouter")
        log.info("openrouter provider: stt_model=%s", self.openrouter_stt_model)
        return OpenAI(
            api_key=self.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=30.0,
            max_retries=2,
        )

    def _build_translation_client(self):
        if OpenAI is None:
            raise RuntimeError("openai package required for OpenRouter")
        log.info("openrouter provider: translation_model=%s", self.openrouter_translation_model)
        return OpenAI(
            api_key=self.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=30.0,
            max_retries=2,
        )

    def _stt_model_for_call(self) -> str:
        return self.openrouter_stt_model

    def _translation_model_for_call(self) -> str:
        return self.openrouter_translation_model

    # ----------------------------------------------------------- STT override
    def _call_transcription(self, wav_bytes: bytes) -> tuple[str, str | None] | None:
        """Override parent's multipart upload.

        OpenRouter's audio is messy:
        - /audio/transcriptions rejects multipart with JSON parse error.
        - /chat/completions with input_audio returns 500 for whisper-only
          models (only multimodal chat models like gpt-4o-audio accept it).

        Strategy: route the call based on model type.
          - If model contains "whisper" or "transcribe": hit /audio/transcriptions
            with a JSON body (base64-encoded file) using raw HTTP. Try OpenAI's
            documented file param shape.
          - Otherwise: assume multimodal chat model (gpt-4o-audio-preview etc.)
            and use chat.completions with input_audio content.
        """
        if self._stt_client is None:
            return None
        model = self.openrouter_stt_model.lower()
        if "whisper" in model or "transcribe" in model:
            return self._stt_via_audio_endpoint(wav_bytes)
        return self._stt_via_chat_audio(wav_bytes)

    def _stt_via_audio_endpoint(self, wav_bytes: bytes) -> tuple[str, str | None] | None:
        """Hit /audio/transcriptions directly with JSON body containing base64."""
        try:
            import requests
        except ImportError as exc:
            log.error("`requests` package required for OpenRouter /audio/transcriptions")
            self.report_exception(exc, "stt (missing requests)")
            return None
        try:
            b64 = base64.b64encode(wav_bytes).decode("ascii")
            url = OPENROUTER_BASE_URL + "/audio/transcriptions"
            headers = {
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "Content-Type": "application/json",
            }
            # OpenRouter's schema (discovered via 400 ZodError): the audio goes
            # under `input_audio` as an object with `data` (base64) + `format`.
            # Same shape as the chat-completions input_audio content type, just
            # hoisted to the top level of the body.
            payload = {
                "model": self.openrouter_stt_model,
                "input_audio": {"data": b64, "format": "wav"},
                "response_format": "json",
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                log.error(
                    "openrouter /audio/transcriptions %d: %s",
                    resp.status_code, resp.text[:300],
                )
                # A silent None here is how a dead key becomes a "quiet room"
                # behind a green tray icon. Report every failure; a 401/403
                # will never heal by retrying, so it is FATAL.
                if resp.status_code in (401, 403):
                    self.emit_status(STATUS_FATAL, CODE_AUTH)
                else:
                    self.report_exception(
                        RuntimeError(
                            f"/audio/transcriptions HTTP {resp.status_code}: "
                            f"{resp.text[:300]}"),
                        "stt",
                    )
                return None
            data = resp.json()
            text = (data.get("text") or "").strip()
            lang = data.get("language") or self._fallback_source_lang()
            return text, lang
        except Exception as exc:
            log.error("openrouter /audio/transcriptions error: %s", exc)
            self.report_exception(exc, "stt")
            return None

    def _fallback_source_lang(self) -> str | None:
        """When the API doesn't return a detected language (OpenRouter often
        omits it), fall back to the first configured source language so the
        overlay can color-tint the caption correctly. Returns just the
        2-letter primary subtag (e.g. "pt" from "pt-BR")."""
        if self.source_languages:
            first = self.source_languages[0]
            return first.split("-")[0].lower() if first else None
        return None

    def _stt_via_chat_audio(self, wav_bytes: bytes) -> tuple[str, str | None] | None:
        """Use /chat/completions with input_audio for multimodal chat models."""
        try:
            b64 = base64.b64encode(wav_bytes).decode("ascii")
            resp = self._stt_client.chat.completions.create(
                model=self.openrouter_stt_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": b64, "format": "wav"},
                            },
                            {"type": "text", "text": "Transcribe verbatim."},
                        ],
                    }
                ],
                temperature=0.0,
                max_tokens=600,
            )
            text = (resp.choices[0].message.content or "").strip()
            return text, None
        except Exception as exc:
            log.error("openrouter /chat/completions audio error: %s", exc)
            # Same contract as every other provider: a failure that only
            # reaches the log is a silent "quiet room" for the operator.
            self.report_exception(exc, "stt (chat audio)")
            return None
