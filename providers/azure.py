"""Azure Speech Translation provider.

Two operating modes:

1. **Multilingual (default)** — automatic language identification over the
   configured source languages. Uses AT-START LID (the service default) for up
   to 4 candidates: it decides once in the first seconds and holds for the
   session. Above 4 candidates it falls back to CONTINUOUS LID, which is the
   only mode that accepts up to 10 — at the cost of re-deciding mid-session.
   Continuous LID was the default here until 2026-09-15 and produced competing
   hypotheses in different languages for the same audio, because Azure
   "returns one of the candidate languages provided even if those languages
   weren't in the audio". Emits `recognizing` partials too —
   MEASURED against the real service (2026-09-15): 8 partials carrying
   translations over three utterances. An earlier version of this docstring
   claimed the opposite and the code never connected the event, so the
   default mode showed only finals, 5–15 s apart on a talk with few pauses.

2. **Streaming single-language** — fixed source language, but emits both
   partials (`recognizing` event, ~300ms) AND finals (`recognized`). Each
   partial carries the translation already, so the overlay can render
   incremental "word-by-word with correction" captions like Microsoft Live
   Captions. Source language can be swapped at runtime via
   `set_source_language()` (rebuilds the recognizer in place).

The mode is selected via the `streaming_mode` constructor flag.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import azure.cognitiveservices.speech as speechsdk

from constants import (
    AZURE_AT_START_LID_MAX_LANGUAGES,
    AZURE_SEGMENTATION_SILENCE_MS,
    AZURE_STABLE_PARTIAL_THRESHOLD,
)
from .base import (
    CODE_NETWORK,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_FATAL,
    OnTranslationCallback,
    ProviderCapabilities,
    TranslationEvent,
    TranslationProvider,
    classify_exception,
)


log = logging.getLogger(__name__)

# Backoff between reconnection attempts, in seconds. Mirrors the pattern the
# Google provider already used; Azure had none.
RECONNECT_DELAYS_S = (1, 2, 5, 10, 20)


class AzureProvider(TranslationProvider):
    # Both modes run ONE continuous TranslationRecognizer over a single
    # PushAudioInputStream, and the SDK delivers that session's callbacks
    # sequentially. Ordering is therefore a property of the wire protocol,
    # not something the controller has to reconstruct — which is why Azure
    # is the right default for a live event.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=True,
        translates=True,
        streaming=True,
        label="Azure Speech Translation",
    )

    def __init__(
        self,
        speech_key: str,
        region: str,
        source_languages: list[str],
        target_languages: list[str],
        on_event: OnTranslationCallback,
        samplerate: int = 16000,
        streaming_mode: bool = False,
        streaming_language: str = "pt-BR",
    ):
        if not speech_key:
            raise ValueError("Azure speech_key required")
        if not region:
            raise ValueError("Azure region required")
        if not target_languages:
            raise ValueError("at least one target language required")
        if not streaming_mode:
            if not source_languages:
                raise ValueError("at least one source language required")
            if len(source_languages) > 10:
                raise ValueError("Continuous LID supports at most 10 source languages")
        else:
            if not streaming_language:
                raise ValueError("streaming_language required when streaming_mode=True")

        self.speech_key = speech_key
        self.region = region
        self.source_languages = source_languages
        self.target_languages = target_languages
        self.on_event = on_event
        self.samplerate = samplerate
        self.streaming_mode = streaming_mode
        self.streaming_language = streaming_language

        self._push_stream: Optional[speechsdk.audio.PushAudioInputStream] = None
        self._recognizer: Optional[speechsdk.translation.TranslationRecognizer] = None
        self._lock = threading.Lock()
        self._running = False
        # True while stop() is tearing down: suppresses the reconnect that
        # a deliberate shutdown would otherwise trigger.
        self._stopping = False
        self._reconnecting = False

    def _build_recognizer(self) -> speechsdk.translation.TranslationRecognizer:
        translation_config = speechsdk.translation.SpeechTranslationConfig(
            subscription=self.speech_key,
            region=self.region,
        )
        for tgt in self.target_languages:
            translation_config.add_target_language(tgt)
        # Close a phrase after this much silence instead of the service
        # default. Microsoft's own guidance for "a recorded presenter fast
        # enough that several sentences get combined" is 300 ms; we stay a
        # little above so a breath inside a sentence does not split it.
        translation_config.set_property(
            speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
            str(AZURE_SEGMENTATION_SILENCE_MS),
        )
        # Ask the SERVICE for steadier partials instead of guessing at
        # stabilisation in the overlay. Microsoft's captioning guidance names
        # this as the knob for exactly our symptom: "Requesting more stable
        # partial results reduce the 'flickering' or changing text, but it can
        # increase latency."
        # learn.microsoft.com/azure/ai-services/speech-service/captioning-concepts
        # Measured symptom it addresses: a projected translation was rewritten
        # whole on every partial — 'Exposición Exa' -> 'Te mostraré exactamente
        # lo que…' -> 'Te muestro exactamente…' for one sentence.
        translation_config.set_property(
            speechsdk.PropertyId.SpeechServiceResponse_StablePartialResultThreshold,
            str(AZURE_STABLE_PARTIAL_THRESHOLD),
        )

        audio_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=self.samplerate, bits_per_sample=16, channels=1
        )
        self._push_stream = speechsdk.audio.PushAudioInputStream(stream_format=audio_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self._push_stream)

        if self.streaming_mode:
            # Single-language: enables `recognizing` partials with translation.
            translation_config.speech_recognition_language = self.streaming_language
            recognizer = speechsdk.translation.TranslationRecognizer(
                translation_config=translation_config,
                audio_config=audio_config,
            )
            recognizer.recognizing.connect(self._on_recognizing)
            recognizer.recognized.connect(self._on_recognized)
            log.info(
                "azure recognizer (streaming): lang=%s targets=%s",
                self.streaming_language, self.target_languages,
            )
        else:
            # AT-START vs CONTINUOUS language identification.
            #
            # Continuous LID re-decides the language mid-session, and Azure
            # documents that it "returns one of the candidate languages
            # provided EVEN IF those languages weren't in the audio". Measured
            # consequence on a Portuguese talk with pt-BR/en-US/es-ES as
            # candidates: the service emitted two competing hypotheses
            # milliseconds apart — 'bom dia a todos' (pt-BR) interleaved with
            # 'bongiatos' (en-US, phonetic garbage) — and both reached the
            # band. That is the "sometimes Spanish, sometimes English" the
            # operator reported.
            #
            # At-start LID decides once in the first seconds and holds for the
            # session, which removes the competition at the source. It is also
            # the service default: the mode property "is only required for
            # continuous LID. Without it, the Speech service defaults to
            # at-start LID."
            # learn.microsoft.com/azure/ai-services/speech-service/language-identification
            #
            # The catch is the candidate cap: at-start allows up to 4
            # languages, continuous up to 10. So an operator who really wants
            # 5+ candidates still gets continuous — they asked for a scenario
            # at-start cannot express.
            continuous = len(self.source_languages) > AZURE_AT_START_LID_MAX_LANGUAGES
            if continuous:
                translation_config.set_property(
                    property_id=speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode,
                    value="Continuous",
                )
            auto_detect_config = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
                languages=self.source_languages
            )
            recognizer = speechsdk.translation.TranslationRecognizer(
                translation_config=translation_config,
                auto_detect_source_language_config=auto_detect_config,
                audio_config=audio_config,
            )
            # Partials work under LID as well (see module doc).
            recognizer.recognizing.connect(self._on_recognizing)
            recognizer.recognized.connect(self._on_recognized)
            log.info(
                "azure recognizer (multilingual, lid=%s): sources=%s targets=%s",
                "continuous" if continuous else "at-start",
                self.source_languages, self.target_languages,
            )

        recognizer.canceled.connect(self._on_canceled)
        recognizer.session_started.connect(lambda _: log.info("azure session started"))
        recognizer.session_stopped.connect(self._on_session_stopped)
        return recognizer

    # ------------------------------------------------------------ event handlers

    def _is_defunct(self) -> bool:
        """True once stop() has begun. The SDK dispatches results on its own
        callback thread and they keep arriving after stop_continuous_recognition()
        returns, so without this guard a torn-down session still pushes captions
        into a controller that believes it is stopped — and, at quit, into an
        overlay Qt has already destroyed."""
        return self._stopping or not self._running

    def _on_recognizing(self, evt) -> None:
        """Streaming partial — original + translation grow incrementally."""
        if self._is_defunct():
            return
        try:
            text = evt.result.text or ""
            if not text.strip():
                return
            translations: dict[str, str] = {}
            for lang in self.target_languages:
                txt = evt.result.translations.get(lang, "")
                if txt:
                    translations[lang] = txt
            self.on_event(TranslationEvent(
                detected_language=self._detected_language(evt),
                original_text=text,
                translations=translations,
                is_final=False,
                result_id=str(evt.result.result_id or ""),
            ))
        except Exception:
            log.exception("error in azure recognizing handler")

    def _detected_language(self, evt) -> Optional[str]:
        """Pinned mode: the pin. LID mode: what the service says (may be
        empty on the first partials of an utterance)."""
        if self.streaming_mode:
            return self.streaming_language
        try:
            return evt.result.properties.get(
                speechsdk.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult
            ) or None
        except Exception:
            return None

    def _on_recognized(self, evt) -> None:
        if self._is_defunct():
            return
        try:
            if evt.result.reason != speechsdk.ResultReason.TranslatedSpeech:
                return
            detected = self._detected_language(evt)
            translations: dict[str, str] = {}
            for lang in self.target_languages:
                txt = evt.result.translations.get(lang, "")
                if txt:
                    translations[lang] = txt
            self.on_event(TranslationEvent(
                detected_language=detected,
                original_text=evt.result.text or "",
                translations=translations,
                is_final=True,
                result_id=str(evt.result.result_id or ""),
            ))
            # A completed translation clears any earlier degraded state.
            self.report_ok()
        except Exception as exc:
            log.exception("error in azure recognized handler")
            self.report_exception(exc, "recognized handler")

    def _on_canceled(self, evt) -> None:
        """Azure ends the recognition session. THIS is the silent-death path.

        The SDK reports a cancellation and then simply stops producing
        results. Logging it is not enough: to the operator the tray icon stays
        green and the overlay stays blank, which looks exactly like a room
        where nobody is talking. An end-to-end test with a deliberately wrong
        key caught this — the failure was only ever visible in the log file.
        """
        details = str(getattr(evt, "error_details", "") or "")
        log.error(
            "azure recognition canceled: reason=%s details=%s",
            evt.reason, details,
        )

        # EndOfStream is the normal close when we stop the session ourselves.
        try:
            if evt.reason == speechsdk.CancellationReason.EndOfStream:
                return
        except Exception:
            pass

        kind, code = classify_exception(RuntimeError(details or str(evt.reason)))
        # Azure spells auth failures out in the details string; classify_exception
        # already keys on "401"/"authentication", so this mostly just works.
        # What it cannot see is that a cancellation is ALWAYS terminal for this
        # session: the recognizer will not resume on its own.
        if kind == STATUS_DEGRADED:
            kind = STATUS_FAILING
        self.emit_status(
            kind,
            code,
            "Azure encerrou o reconhecimento: {}".format(
                details[:160] or str(evt.reason)),
        )

    def _on_session_stopped(self, evt) -> None:
        """The session ended while we still want to be recognizing.

        Distinct from `canceled`: a dropped WebSocket, a server-side session
        cap, or a network blip can simply STOP the session with no error at
        all. Measured: the recognizer went quiet, no cancellation fired, the
        health channel kept reporting 'ok', and captions never came back for
        the rest of the session. Silent and green — the worst combination.
        """
        log.warning("azure session stopped (running=%s)", self._running)
        if self._running and not self._stopping:
            self._schedule_reconnect("a sessão do Azure caiu")

    # ------------------------------------------------------- reconnection

    def _schedule_reconnect(self, why: str) -> None:
        """Rebuild the recognizer on a background thread, with backoff.

        Azure had no reconnection at all, while the Google provider has had it
        since day one. Over a two-hour event a single blip is close to
        certain, so this is the difference between 'captions stopped at
        minute 40' and 'captions paused for two seconds at minute 40'.
        """
        with self._lock:
            if self._reconnecting or self._stopping or not self._running:
                return
            self._reconnecting = True

        def worker() -> None:
            try:
                for delay in RECONNECT_DELAYS_S:
                    if self._stopping or not self._running:
                        return
                    self.emit_status(
                        STATUS_FAILING, CODE_NETWORK,
                        "{} — reconectando em {}s…".format(why, delay),
                    )
                    time.sleep(delay)
                    if self._stopping or not self._running:
                        return
                    try:
                        with self._lock:
                            # Re-check INSIDE the lock. The checks above race
                            # with stop(): it could run to completion between
                            # them and here, and we would then build and start
                            # a recognizer that nothing will ever stop, feeding
                            # a provider whose is_running() says False.
                            if self._stopping or not self._running:
                                return
                            old = self._recognizer
                            old_stream = self._push_stream
                            self._recognizer = self._build_recognizer()
                            self._recognizer.start_continuous_recognition()
                        if old is not None:
                            try:
                                # Disconnect EVERYTHING on the replaced
                                # recognizer, not just the lifecycle signals.
                                # Leaving recognizing/recognized connected let
                                # the dying session keep emitting captions that
                                # interleaved with the new one's — two sessions
                                # writing the same band.
                                old.recognizing.disconnect_all()
                                old.recognized.disconnect_all()
                                old.session_stopped.disconnect_all()
                                old.canceled.disconnect_all()
                                old.stop_continuous_recognition()
                            except Exception:
                                pass
                        if old_stream is not None:
                            try:
                                old_stream.close()
                            except Exception:
                                pass
                        log.info("azure recognizer reconnected")
                        self.report_ok()
                        return
                    except Exception:
                        log.exception("azure reconnect attempt failed")
                # Every attempt failed: stop pretending and let the controller
                # escalate to the fallback chain.
                self.emit_status(
                    STATUS_FATAL, CODE_NETWORK,
                    "Não foi possível reconectar ao Azure. Verifique a internet.",
                )
            finally:
                with self._lock:
                    self._reconnecting = False

        threading.Thread(target=worker, name="azure-reconnect", daemon=True).start()

    # ------------------------------------------------------------ lifecycle

    @staticmethod
    def _disconnect_all(recognizer) -> None:
        """Detach every signal of a recognizer we are done with."""
        if recognizer is None:
            return
        for signal in ("recognizing", "recognized", "canceled",
                       "session_started", "session_stopped"):
            try:
                getattr(recognizer, signal).disconnect_all()
            except Exception:
                pass

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stopping = False
            self._reconnecting = False
            self._recognizer = self._build_recognizer()
            # Mark running BEFORE the session opens. The content handlers now
            # drop anything that arrives while the provider is not running, so
            # setting this afterwards would discard the first results of the
            # session if the SDK dispatched one during the handshake. Reset it
            # if the start itself fails, or a failed provider would look alive.
            self._running = True
            try:
                self._recognizer.start_continuous_recognition()
            except Exception:
                self._running = False
                self._recognizer = None
                raise
            log.info("azure provider started")

    def stop(self) -> None:
        with self._lock:
            if not self._running or self._recognizer is None:
                return
            # INVARIANT — do not reorder these two steps.
            # `_stopping = True` must be set BEFORE stop_continuous_recognition(),
            # and stop_continuous_recognition() is deliberately called while
            # holding self._lock. That works only because _on_session_stopped
            # checks `not self._stopping` and returns BEFORE it touches the
            # lock: the SDK fires session_stopped on its callback thread during
            # the blocking stop, so a handler that took the lock there would
            # deadlock against us. Setting the flag first is what makes the
            # handler short-circuit. tests/test_review_ux.py guards this.
            self._stopping = True
            # Disconnect before stopping. stop_continuous_recognition() blocks
            # until the session ends, but results already dispatched onto the
            # SDK's callback thread are still in flight and used to land in a
            # provider that had already dropped its recognizer.
            self._disconnect_all(self._recognizer)
            try:
                self._recognizer.stop_continuous_recognition()
            except Exception:
                log.exception("error stopping recognizer")
            try:
                if self._push_stream is not None:
                    self._push_stream.close()
            except Exception:
                pass
            self._recognizer = None
            self._push_stream = None
            self._running = False
            log.info("azure provider stopped")

    def set_source_language(self, language: str) -> None:
        """Swap the streaming source language at runtime.

        Stops the current recognizer (if running), updates streaming_language,
        and restarts. No-op when not in streaming_mode (multilingual mode uses
        Continuous LID and detects automatically).
        """
        if not self.streaming_mode:
            log.warning("set_source_language ignored: provider not in streaming_mode")
            return
        if not language:
            return
        was_running = self._running
        log.info("azure switching source language: %s -> %s", self.streaming_language, language)
        if was_running:
            self.stop()
        self.streaming_language = language
        if was_running:
            self.start()

    def push_audio(self, audio_bytes: bytes) -> None:
        if self._push_stream is None:
            return
        try:
            self._push_stream.write(audio_bytes)
        except Exception:
            log.exception("error pushing audio to azure")

    @property
    def is_running(self) -> bool:
        return self._running
