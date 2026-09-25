"""Abstract translation provider interface.

All providers implement this contract:
- Accept PCM 16-bit mono audio chunks via push_audio()
- Detect source language automatically (or accept a hint)
- Translate to one or more target languages
- Emit TranslationEvent via the on_event callback
- **Report every failure via the on_status callback** — a provider that fails
  silently is indistinguishable from a room where nobody is speaking, which is
  the worst possible outcome during a live event.

Two invariants the rest of the app relies on:

1. `capabilities().ordered_by_protocol` tells the controller whether events
   already arrive in chronological order (streaming providers: the wire
   protocol guarantees it) or whether they need the controller's reorder gate
   (chunk providers: results race each other through a thread pool).
2. Every error path calls `emit_status()`. Returning None from an internal
   helper without emitting a status is a bug, not a style choice.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- events


@dataclass
class TranslationEvent:
    detected_language: str | None
    original_text: str
    translations: dict[str, str]
    is_final: bool
    # Monotonic ms when the source audio chunk was finalized/emitted by the
    # capture/buffer. Used by the overlay to compute end-to-end latency.
    audio_emitted_at_ms: float | None = None
    # Stable identifier shared by all events belonging to the same utterance.
    # Streaming providers (Azure recognizing → recognized) use this so the
    # overlay can replace partials in-place instead of stacking new lines.
    # Empty string ⇒ chunk-final providers; overlay falls back to original-text
    # heuristics for those.
    result_id: str = ""
    # Monotonically increasing index assigned at AUDIO CHUNK time (not at
    # result time). The controller's reorder gate releases events in this
    # order for providers whose results can overtake each other.
    # None ⇒ provider does not sequence (ordered_by_protocol providers).
    seq: int | None = None


# ---------------------------------------------------------------- status


# What kind of trouble the provider is in. The operator UI maps these to
# colors; the controller maps failing/fatal to the fallback chain.
STATUS_OK = "ok"              # working normally
STATUS_DEGRADED = "degraded"  # recoverable hiccup, one chunk lost
STATUS_FAILING = "failing"    # repeated errors, fallback should consider switching
STATUS_FATAL = "fatal"        # cannot continue at all (bad key, no quota)

# Machine-readable cause. Kept deliberately small — the operator needs to know
# what to DO, and there are only a handful of distinct actions.
CODE_AUTH = "auth"              # bad/missing key -> fix credentials
CODE_QUOTA = "quota"            # free tier or billing exhausted -> switch provider
CODE_RATE_LIMIT = "rate_limit"  # 429 -> back off, usually self-healing
CODE_NETWORK = "network"        # timeout/DNS/connection -> check the venue wifi
CODE_DEVICE = "device"          # audio device vanished -> re-select output
CODE_UNKNOWN = "unknown"


@dataclass
class ProviderStatus:
    """A single health report from a provider. User-facing text is PT-BR."""

    kind: str                 # STATUS_*
    code: str = CODE_UNKNOWN  # CODE_*
    message: str = ""         # short, actionable, Portuguese
    provider: str = ""        # filled in by the provider

    @property
    def is_trouble(self) -> bool:
        return self.kind in (STATUS_FAILING, STATUS_FATAL)


OnTranslationCallback = Callable[[TranslationEvent], None]
OnStatusCallback = Callable[[ProviderStatus], None]


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """Map an arbitrary SDK/HTTP exception to (status_kind, code).

    Providers wrap heterogeneous SDKs (openai, azure-cognitiveservices,
    google-cloud). Rather than teach every provider about every SDK exception
    hierarchy, we sniff the string form — crude, but it degrades to
    (FAILING, UNKNOWN), which still reaches the operator. Silence is the only
    unacceptable outcome.
    """
    text = f"{type(exc).__name__}: {exc}".lower()

    # Order matters: 'quota exceeded' also contains '429' in some SDKs, and
    # quota is the more actionable diagnosis (switch provider, not wait).
    if any(s in text for s in ("quota", "insufficient_quota", "billing",
                               "exceeded your current")):
        return STATUS_FATAL, CODE_QUOTA
    if any(s in text for s in ("401", "403", "unauthorized", "forbidden",
                               "invalid api key", "invalid_api_key",
                               "authentication")):
        return STATUS_FATAL, CODE_AUTH
    if any(s in text for s in ("429", "rate limit", "rate_limit",
                               "too many requests")):
        return STATUS_DEGRADED, CODE_RATE_LIMIT
    if any(s in text for s in ("timeout", "timed out", "connection", "dns",
                               "unreachable", "network", "ssl", "econnreset",
                               "getaddrinfo")):
        return STATUS_FAILING, CODE_NETWORK
    return STATUS_FAILING, CODE_UNKNOWN


MESSAGES = {
    CODE_AUTH: "Chave de API recusada. Confira em Configurações → Credenciais.",
    CODE_QUOTA: "Cota/créditos do provedor esgotados. Configure uma reserva em Configurações → Provedor.",
    CODE_RATE_LIMIT: "Provedor limitando requisições (429). A legenda pode atrasar.",
    CODE_NETWORK: "Sem resposta do provedor. Verifique a internet do local.",
    CODE_DEVICE: "Dispositivo de áudio indisponível. Reselecione em Configurações → Áudio.",
    CODE_UNKNOWN: "Falha no provedor. Veja os logs para o detalhe.",
}


# ---------------------------------------------------------------- capabilities


@dataclass(frozen=True)
class ProviderCapabilities:
    """What the controller needs to know to drive this provider correctly.

    Declared per provider CLASS, never inferred from the provider name — name
    checks are how the reorder gate ends up applied to a streaming provider
    and silently dropping live results.
    """

    # True ⇒ results arrive chronologically because the wire protocol says so.
    # The controller MUST NOT apply its reorder gate to these (the gate's
    # bounded wait would delay and then drop perfectly good live results).
    ordered_by_protocol: bool
    # True ⇒ produces target-language text itself (possibly via a 2nd hop).
    translates: bool
    # True ⇒ long-lived connection with interim results; False ⇒ chunked REST.
    streaming: bool
    # Human label for the operator UI.
    label: str = ""


class TranslationProvider(ABC):
    """Common interface for all translation backends."""

    # Subclasses override. Conservative default: assume results can race, so
    # a provider that forgets to declare still gets the (safe) reorder gate.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False, translates=True, streaming=False
    )

    # Set by build_provider so status reports identify themselves.
    provider_name: str = ""
    on_status: OnStatusCallback | None = None

    @classmethod
    def capabilities(cls) -> ProviderCapabilities:
        return cls.CAPABILITIES

    # -- status reporting ------------------------------------------------

    def emit_status(
        self,
        kind: str,
        code: str = CODE_UNKNOWN,
        message: str = "",
    ) -> None:
        """Report health upward. Never raises — a broken status callback must
        not take down the audio pipeline."""
        status = ProviderStatus(
            kind=kind,
            code=code,
            message=message or MESSAGES.get(code, MESSAGES[CODE_UNKNOWN]),
            provider=self.provider_name or type(self).__name__,
        )
        cb = self.on_status
        if cb is None:
            # Not wired (unit tests, standalone use) — still leave a trace.
            log.warning("provider status (unrouted): %s/%s %s",
                        status.kind, status.code, status.message)
            return
        try:
            cb(status)
        except Exception:
            log.exception("on_status callback raised")

    def report_exception(self, exc: BaseException, context: str = "") -> ProviderStatus:
        """Classify and emit an exception as status. Returns what was emitted."""
        kind, code = classify_exception(exc)
        log.error("provider error%s: %s",
                  f" ({context})" if context else "", exc)
        self.emit_status(kind, code)
        return ProviderStatus(kind=kind, code=code, provider=self.provider_name)

    def report_ok(self) -> None:
        """Signal a successful round-trip (clears a prior degraded state)."""
        self.emit_status(STATUS_OK, CODE_UNKNOWN, "")

    # -- lifecycle -------------------------------------------------------

    @abstractmethod
    def start(self) -> None:
        """Start the recognition pipeline. Idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the recognition pipeline. Idempotent."""

    @abstractmethod
    def push_audio(self, audio_bytes: bytes) -> None:
        """Push PCM 16-bit mono audio bytes (16 kHz default)."""

    @property
    @abstractmethod
    def is_running(self) -> bool: ...
