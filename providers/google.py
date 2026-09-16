"""Google provider — Speech-to-Text v2 streaming + Cloud Translation v3.

Pipeline:
  audio → StreamingRecognize (with alternative_language_codes for auto-detect)
        → emits transcript per utterance (final results)
        → submit translation to thread pool (NON-blocking)
        → translation worker emits TranslationEvent when ready

Concurrency design:
- Recognition thread reads STT responses and immediately emits an event with
  ONLY the original transcript (no translations yet). This gets to the overlay
  in <100ms after Google emits.
- At each STT final the utterance gets a monotonically increasing `seq` and
  the translation is submitted to a thread pool (NON-blocking). Translations
  CAN complete out of order — a short utterance B submitted after A finishes
  first — so the final event carries that `seq` and this provider declares
  ordered_by_protocol=False: the controller's reorder gate publishes the
  finals in utterance order.
- This avoids the previous serialization where 3 STT results in a row would
  wait for 3 sequential translation API calls, WITHOUT pretending the
  translation fan-out preserves order (it does not — see the seq above).

Note: Google Speech v2 supports up to 4 source languages (1 main +
3 alternatives). For 5+ languages prefer Azure.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .base import (
    OnTranslationCallback,
    ProviderCapabilities,
    TranslationEvent,
    TranslationProvider,
)

log = logging.getLogger(__name__)


class GoogleProvider(TranslationProvider):
    # Recognition runs over one bidirectional StreamingRecognize call, so
    # recognition results arrive in order — but the per-utterance translation
    # fan-out is PARALLEL, and the final (translated) event is emitted when
    # the translation finishes, not when the utterance was recognized. Two
    # utterances A→B routinely complete B before A, so finals need the
    # controller's reorder gate, fed by the seq assigned at STT-final time.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False,
        translates=True,
        streaming=True,
        label="Google Speech v2 + Translate",
    )

    def __init__(
        self,
        credentials_json: str,
        project_id: str,
        location: str,
        recognizer_id: str,
        source_languages: list[str],
        target_languages: list[str],
        on_event: OnTranslationCallback,
        samplerate: int = 16000,
    ):
        if not credentials_json:
            raise ValueError("Google credentials_json required (file path or inline JSON)")
        if not project_id:
            raise ValueError("Google project_id required")
        if len(source_languages) > 4:
            raise ValueError("Google Speech v2 supports at most 4 source languages (1 + 3 alts)")

        self.credentials_json = credentials_json
        self.project_id = project_id
        self.location = location or "global"
        self.recognizer_id = recognizer_id or "_"
        self.source_languages = source_languages
        self.target_languages = target_languages
        self.on_event = on_event
        self.samplerate = samplerate

        self._audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=400)
        self._thread: threading.Thread | None = None
        self._translation_pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._running = False
        self._creds_temp_path: str | None = None
        # Save/restore for the process-wide GOOGLE_APPLICATION_CREDENTIALS we
        # overwrite in _setup_credentials (see stop()).
        self._creds_env_prev: str | None = None
        self._creds_env_set = False
        self._speech_client = None
        self._translate_client = None
        # Monotonic utterance index, assigned at STT-final time and carried
        # by the final (translated) event so the controller's reorder gate
        # can undo the translation pool's completion order. Only the
        # recognition thread touches it.
        self._sequence = 0

    def _set_credentials_env(self, value: str) -> None:
        """Point GOOGLE_APPLICATION_CREDENTIALS at our credentials, remembering
        what was there before.

        The variable is PROCESS-wide: leaving it overwritten (or pointing at
        a temp file we delete in stop()) breaks every other Google client in
        this process after we stop.
        """
        if not self._creds_env_set:
            self._creds_env_prev = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            self._creds_env_set = True
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = value

    def _setup_credentials(self) -> None:
        if os.path.isfile(self.credentials_json):
            self._set_credentials_env(self.credentials_json)
            return
        try:
            json.loads(self.credentials_json)
        except Exception as exc:
            raise ValueError(f"credentials_json is neither a path nor valid JSON: {exc}") from exc
        fd, path = tempfile.mkstemp(suffix=".json", prefix="tlt-google-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.credentials_json)
        self._set_credentials_env(path)
        self._creds_temp_path = path

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._setup_credentials()
            from google.cloud import speech_v2
            from google.cloud import translate_v3 as translate

            self._speech_client = speech_v2.SpeechClient()
            self._translate_client = translate.TranslationServiceClient()
            self._translation_pool = ThreadPoolExecutor(
                max_workers=max(2, len(self.target_languages) * 2),
                thread_name_prefix="google-translate",
            )

            self._running = True
            self._thread = threading.Thread(target=self._run, name="google-stt", daemon=True)
            self._thread.start()
            log.info("google provider started (translation pool size=%s)", self._translation_pool._max_workers)

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            try:
                self._audio_queue.put_nowait(None)
            except queue.Full:
                pass
            if self._thread is not None:
                self._thread.join(timeout=3)
                self._thread = None
            if self._translation_pool is not None:
                self._translation_pool.shutdown(wait=False, cancel_futures=True)
                self._translation_pool = None
            self._speech_client = None
            self._translate_client = None
            if self._creds_temp_path and os.path.exists(self._creds_temp_path):
                try:
                    os.remove(self._creds_temp_path)
                except Exception:
                    pass
            self._creds_temp_path = None
            # Restore the process-wide env var we overwrote at start: with
            # inline JSON it pointed at the temp file we just deleted, and
            # with a file path it hid whatever the operator had before.
            if self._creds_env_set:
                if self._creds_env_prev is None:
                    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
                else:
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self._creds_env_prev
                self._creds_env_set = False
                self._creds_env_prev = None
            # drain queue
            try:
                while True:
                    self._audio_queue.get_nowait()
            except queue.Empty:
                pass
            log.info("google provider stopped")

    def push_audio(self, audio_bytes: bytes) -> None:
        if not self._running:
            return
        try:
            self._audio_queue.put_nowait(audio_bytes)
        except queue.Full:
            # drop oldest to keep latency bounded
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.put_nowait(audio_bytes)
                log.warning("google audio queue full; dropped oldest frame")
            except (queue.Empty, queue.Full):
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    # --- internals -----------------------------------------------------

    def _request_generator(self):
        from google.cloud.speech_v2 import types as speech_types

        primary = self.source_languages[0]
        alternates = self.source_languages[1:]

        recognition_config = speech_types.RecognitionConfig(
            explicit_decoding_config=speech_types.ExplicitDecodingConfig(
                encoding=speech_types.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=self.samplerate,
                audio_channel_count=1,
            ),
            language_codes=[primary, *alternates],
            model="latest_long",
            features=speech_types.RecognitionFeatures(
                enable_automatic_punctuation=True,
            ),
        )
        streaming_config = speech_types.StreamingRecognitionConfig(
            config=recognition_config,
            streaming_features=speech_types.StreamingRecognitionFeatures(
                # Interim results give us the "typing effect" — partial transcripts
                # arrive as the speaker talks. We forward them to the overlay so the
                # original text appears in real-time, then replace with the final.
                interim_results=True,
            ),
        )
        recognizer_path = (
            f"projects/{self.project_id}/locations/{self.location}/recognizers/{self.recognizer_id}"
        )
        yield speech_types.StreamingRecognizeRequest(
            recognizer=recognizer_path,
            streaming_config=streaming_config,
        )

        while self._running:
            chunk = self._audio_queue.get()
            if chunk is None:
                break
            yield speech_types.StreamingRecognizeRequest(audio=chunk)

    def _run(self) -> None:
        # Google Cloud Speech-to-Text v2 streaming has a hard limit per stream
        # (~5 minutes of audio). After that, the server closes the stream.
        # We auto-reconnect: when the response generator raises or finishes
        # while still _running, we restart the stream. This is transparent
        # to the user — the audio queue keeps buffering.
        backoff_s = 1.0
        while self._running:
            try:
                log.info("google streaming session starting")
                responses = self._speech_client.streaming_recognize(
                    requests=self._request_generator()
                )
                for response in responses:
                    if not self._running:
                        break
                    for result in response.results:
                        if not result.alternatives:
                            continue
                        transcript = result.alternatives[0].transcript.strip()
                        detected = getattr(result, "language_code", None)
                        if not transcript:
                            continue

                        if not result.is_final:
                            # Interim/partial result — show typing effect, no translation
                            self.on_event(TranslationEvent(
                                detected_language=detected,
                                original_text=transcript,
                                translations={},
                                is_final=False,
                            ))
                            continue

                        # Sequence at STT-FINAL time, before the translation
                        # pool gets the utterance: the pool emits in
                        # COMPLETION order, which is not utterance order.
                        seq = self._sequence
                        self._sequence += 1

                        if self._is_likely_hallucination(transcript):
                            log.info("google dropping hallucination: %r", transcript[:80])
                            # The gate holds every later caption hostage to a
                            # slot that never arrives unless we release it.
                            self._release_empty(seq, detected)
                            continue

                        log.info("google STT final [seq=%s]: lang=%s text=%r",
                                 seq, detected, transcript[:80])
                        # Emit final original (will replace any interim with same text).
                        # Cosmetic and seq-less on purpose: partials bypass the
                        # reorder gate so the original shows without gate latency.
                        self.on_event(TranslationEvent(
                            detected_language=detected,
                            original_text=transcript,
                            translations={},
                            is_final=False,  # not final until translation arrives
                        ))
                        if self._translation_pool is not None:
                            self._translation_pool.submit(
                                self._translate_and_emit, seq, transcript, detected
                            )

                # Stream ended normally — restart immediately if still running
                backoff_s = 1.0
                if self._running:
                    log.info("google streaming session ended; reconnecting")
                    continue
            except Exception as exc:
                if self._running:
                    log.exception("google streaming recognize failed; reconnecting in %.1fs", backoff_s)
                    # Reconnecting silently is right for a one-off blip, but a
                    # stream that keeps dying (bad credentials, revoked
                    # service account, no quota) would retry forever behind a
                    # green tray icon and a blank overlay. Report it: the
                    # controller de-duplicates repeats, and the operator sees
                    # the reason instead of an apparently quiet room.
                    self.report_exception(exc, "streaming recognize")
                    # exponential backoff up to 8s
                    self._sleep_interruptible(backoff_s)
                    backoff_s = min(backoff_s * 2, 8.0)
                else:
                    return

    def _sleep_interruptible(self, seconds: float) -> None:
        # Sleep but wake up early if stop() is called
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(0.1)

    @staticmethod
    def _is_likely_hallucination(text: str) -> bool:
        common = {".", "..", "...", "you", "thank you.", "thanks.", "bye."}
        return text.strip().lower() in common

    def _translate_and_emit(self, seq: int, transcript: str, detected: str | None) -> None:
        """Every exit path must emit an event carrying `seq` — a translation
        that dies silently stalls the controller's reorder gate until its
        deadline and swallows every caption behind this one."""
        try:
            t0 = time.time()
            translations = self._translate_all(transcript, detected)
            dt = (time.time() - t0) * 1000
            log.info("google translate done in %.0fms: %s", dt, list(translations.keys()))
            self.on_event(TranslationEvent(
                detected_language=detected,
                original_text=transcript,
                translations=translations,
                is_final=True,
                seq=seq,
            ))
            self.report_ok()
        except Exception as exc:
            log.exception("google translation worker crashed")
            self.report_exception(exc, "translation worker")
            self._release_empty(seq, detected)

    def _release_empty(self, seq: int, detected: str | None = None) -> None:
        """Free a reorder-gate slot that produced no translatable text."""
        try:
            self.on_event(TranslationEvent(
                detected_language=detected,
                original_text="",
                translations={},
                is_final=True,
                seq=seq,
            ))
        except Exception:
            log.exception("failed to release empty seq=%s", seq)

    def _translate_all(self, text: str, source_lang: str | None) -> dict[str, str]:
        out: dict[str, str] = {}
        if self._translate_client is None:
            return out
        loc = self.location if self.location not in ("_", "") else "global"
        parent = f"projects/{self.project_id}/locations/{loc}"

        def one(tgt: str) -> tuple[str, str]:
            if source_lang and source_lang.lower().startswith(tgt.lower()):
                return tgt, text
            try:
                resp = self._translate_client.translate_text(
                    parent=parent,
                    contents=[text],
                    target_language_code=tgt,
                    source_language_code=(source_lang.split("-")[0] if source_lang else None),
                    mime_type="text/plain",
                )
                if resp.translations:
                    return tgt, resp.translations[0].translated_text
            except Exception as exc:
                log.exception("google translate failed for target=%s", tgt)
                self.report_exception(exc, f"translate->{tgt}")
            return tgt, ""

        if len(self.target_languages) == 1:
            tgt, txt = one(self.target_languages[0])
            if txt:
                out[tgt] = txt
            return out

        # Fan out over the provider's PERSISTENT pool. A fresh
        # ThreadPoolExecutor per utterance paid thread-spawn cost on every
        # sentence of the event. Sizing note: the pool has
        # max(2, 2 * len(targets)) workers and _translate_and_emit occupies
        # one, so the fan-out always fits without starving.
        pool = self._translation_pool
        if pool is None:
            # stop() ran mid-utterance: finish sequentially rather than die.
            for t in self.target_languages:
                tgt, txt = one(t)
                if txt:
                    out[tgt] = txt
            return out
        futures = [pool.submit(one, t) for t in self.target_languages]
        for fut in as_completed(futures):
            tgt, txt = fut.result()
            if txt:
                out[tgt] = txt
        return out