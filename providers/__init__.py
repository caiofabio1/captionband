"""Provider factory.

Usage:
    from providers import build_provider
    provider = build_provider(app_config, on_event=callback)
    provider.start()
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .base import (
    OnStatusCallback,
    OnTranslationCallback,
    ProviderCapabilities,
    ProviderStatus,
    TranslationEvent,
    TranslationProvider,
)


if TYPE_CHECKING:
    from config import AppConfig


log = logging.getLogger(__name__)


PROVIDER_LABELS = {
    "azure": "Azure Speech Translation",
    "groq": "Groq (Whisper turbo + Llama)",
    "google": "Google Speech v2 + Translate",
    "whisper_local": "Whisper local (offline)",
    "cerebras": "Cerebras (Llama 3.3 70B + Groq Whisper)",
    "openai_cerebras": "OpenAI Whisper + Cerebras (paid SLA — recomendado para produção)",
    "openrouter": "OpenRouter (1 chave para tudo — recomendado simples)",
    "openai_realtime": "OpenAI Realtime Translate (streaming, 1 sessão POR idioma)",
}

# Which third-party package each provider needs, so a missing or broken
# dependency produces an instruction instead of a stack trace.
PROVIDER_REQUIREMENTS = {
    "azure": ("azure-cognitiveservices-speech", "azure.cognitiveservices.speech"),
    "groq": ("openai", "openai"),
    "google": ("google-cloud-speech", "google.cloud.speech"),
    "whisper_local": ("faster-whisper", "faster_whisper"),
    "cerebras": ("openai", "openai"),
    "openai_cerebras": ("openai", "openai"),
    "openrouter": ("openai", "openai"),
    "openai_realtime": ("websocket-client", "websocket"),
}


class ProviderUnavailable(RuntimeError):
    """The provider cannot be built — missing/broken dependency or bad config.

    Carries a message written for the operator, not for a developer.
    """


def _import_failure_message(name: str, exc: BaseException) -> str:
    package, _module = PROVIDER_REQUIREMENTS.get(name, (name, name))
    label = PROVIDER_LABELS.get(name, name)
    return (
        "O provedor '{label}' não pôde ser carregado.\n\n"
        "Causa: {kind}: {exc}\n\n"
        "Isto normalmente significa que o pacote '{pkg}' não está instalado "
        "ou está com versão incompatível neste Python.\n\n"
        "Conserto: pip install --upgrade --force-reinstall {pkg}"
    ).format(label=label, kind=type(exc).__name__, exc=exc, pkg=package)


def provider_capabilities(name: str) -> Optional[ProviderCapabilities]:
    """Capabilities of a provider WITHOUT instantiating or configuring it.

    The controller needs `ordered_by_protocol` before it has a live provider,
    and asking must never have the side effect of importing a broken SDK.
    """
    try:
        cls = _provider_class(name)
    except Exception:
        return None
    return cls.capabilities()


def _provider_class(name: str):
    """Import and return the provider class.

    Every provider module is imported lazily and behind ONE guard here rather
    than a `try/except ImportError` inside each module. Those per-module
    guards only caught ImportError, so a dependency that is installed but
    broken (e.g. a pydantic/pydantic-core version clash) raised SystemError
    straight through them.
    """
    try:
        if name == "azure":
            from .azure import AzureProvider
            return AzureProvider
        if name == "groq":
            from .groq import GroqProvider
            return GroqProvider
        if name == "google":
            from .google import GoogleProvider
            return GoogleProvider
        if name == "whisper_local":
            from .whisper_local import WhisperLocalProvider
            return WhisperLocalProvider
        if name == "cerebras":
            from .cerebras import CerebrasProvider
            return CerebrasProvider
        if name == "openai_cerebras":
            from .openai_cerebras import OpenAICerebrasProvider
            return OpenAICerebrasProvider
        if name == "openrouter":
            from .openrouter import OpenRouterProvider
            return OpenRouterProvider
        if name == "openai_realtime":
            from .openai_realtime import OpenAIRealtimeProvider
            return OpenAIRealtimeProvider
    except ProviderUnavailable:
        raise
    except BaseException as exc:  # SystemError included, deliberately
        log.exception("failed to import provider %r", name)
        raise ProviderUnavailable(_import_failure_message(name, exc)) from exc
    raise ProviderUnavailable("Provedor desconhecido: {!r}".format(name))


def build_provider(
    app_config: "AppConfig",
    on_event: OnTranslationCallback,
    on_status: Optional[OnStatusCallback] = None,
) -> TranslationProvider:
    """Build a configured provider with its status channel wired.

    `on_status` is how every failure inside the provider reaches the operator
    and the fallback chain. Passing None is supported (tests, scripts) but in
    the app it is always supplied — a provider with no status channel can only
    fail silently.
    """
    name = app_config.provider
    # Surface a broken dependency before touching credentials, so the operator
    # gets "install this package", not "missing API key".
    _provider_class(name)
    provider = _construct(app_config, on_event)
    provider.provider_name = name
    provider.on_status = on_status
    return provider


def _construct(
    app_config: "AppConfig",
    on_event: OnTranslationCallback,
) -> TranslationProvider:
    name = app_config.provider
    if name == "azure":
        from .azure import AzureProvider

        return AzureProvider(
            speech_key=app_config.azure_speech_key,
            region=app_config.azure_speech_region,
            source_languages=app_config.source_languages,
            target_languages=app_config.target_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
            streaming_mode=app_config.azure_streaming_mode,
            streaming_language=app_config.azure_streaming_language,
        )
    if name == "groq":
        from .groq import GroqProvider

        return GroqProvider(
            api_key=app_config.groq_api_key,
            transcription_model=app_config.groq_transcription_model,
            translation_model=app_config.groq_translation_model,
            source_languages=app_config.source_languages,
            target_languages=app_config.target_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
            chunk_seconds=app_config.chunk_seconds,
        )
    if name == "google":
        from .google import GoogleProvider

        return GoogleProvider(
            credentials_json=app_config.google_credentials_json,
            project_id=app_config.google_project_id,
            location=app_config.google_location,
            recognizer_id=app_config.google_recognizer_id,
            source_languages=app_config.source_languages,
            target_languages=app_config.target_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
        )
    if name == "whisper_local":
        from .whisper_local import WhisperLocalProvider

        return WhisperLocalProvider(
            whisper_model=app_config.whisper_model,
            whisper_device=app_config.whisper_device,
            whisper_compute_type=app_config.whisper_compute_type,
            source_languages=app_config.source_languages,
            target_languages=app_config.target_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
            chunk_seconds=app_config.chunk_seconds,
        )
    if name == "cerebras":
        from .cerebras import CerebrasProvider

        return CerebrasProvider(
            api_key=app_config.groq_api_key,
            cerebras_api_key=app_config.cerebras_api_key,
            transcription_model=app_config.groq_transcription_model,
            cerebras_translation_model=app_config.cerebras_translation_model,
            source_languages=app_config.source_languages,
            target_languages=app_config.target_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
            chunk_seconds=app_config.chunk_seconds,
        )
    if name == "openai_cerebras":
        from .openai_cerebras import OpenAICerebrasProvider

        return OpenAICerebrasProvider(
            openai_api_key=app_config.openai_api_key,
            cerebras_api_key=app_config.cerebras_api_key,
            openai_stt_model=app_config.openai_stt_model,
            cerebras_translation_model=app_config.cerebras_translation_model,
            source_languages=app_config.source_languages,
            target_languages=app_config.target_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
            chunk_seconds=app_config.chunk_seconds,
        )
    if name == "openrouter":
        from .openrouter import OpenRouterProvider

        return OpenRouterProvider(
            openrouter_api_key=app_config.openrouter_api_key,
            openrouter_stt_model=app_config.openrouter_stt_model,
            openrouter_translation_model=app_config.openrouter_translation_model,
            source_languages=app_config.source_languages,
            target_languages=app_config.target_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
            chunk_seconds=app_config.chunk_seconds,
        )
    if name == "openai_realtime":
        from .openai_realtime import OpenAIRealtimeProvider

        return OpenAIRealtimeProvider(
            api_key=app_config.openai_api_key,
            target_languages=app_config.target_languages,
            source_languages=app_config.source_languages,
            on_event=on_event,
            samplerate=app_config.audio.samplerate,
        )
    raise ProviderUnavailable("Provedor desconhecido: {!r}".format(name))


__all__ = [
    "TranslationProvider",
    "TranslationEvent",
    "ProviderStatus",
    "ProviderCapabilities",
    "ProviderUnavailable",
    "OnTranslationCallback",
    "OnStatusCallback",
    "PROVIDER_LABELS",
    "PROVIDER_REQUIREMENTS",
    "build_provider",
    "provider_capabilities",
]
