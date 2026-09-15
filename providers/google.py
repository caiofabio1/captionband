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
- Translation pool runs translations in parallel. When done, emits another
  event with translations filled in. Overlay merges by sequence id.
- This avoids the previous serialization where 3 STT results in a row would
  wait for 3 sequential translation API calls.

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
from typing import Optional

from .base import (
    OnTranslationCallback,
    ProviderCapabilities,
    TranslationEvent,
    TranslationProvider,
)


log = logging.getLogger(__name__)


class GoogleProvider(TranslationProvider):
    # Recognition runs over one bidirectional StreamingRecognize call, so
    # recognition results arrive in order. (The per-utterance translation
    # fan-out is parallel, but it happens AFTER the ordered recognition
    # result and never reorders utterances relative to each other.)
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=True,
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

        self._audio_queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=400)
        self._thread: Optional[threading.Thread] = None
        self._translation_pool: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()
        self._running = False
        self._creds_temp_path: Optional[str] = None
        self._speech_client = None
        self._translate_client = None

    def _setup_credentials(self) -> None:
        if os.path.isfile(self.credentials_json):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.credentials_json
            return
        try:
            json.loads(self.credentials_json)
        except Exception as exc:
            raise ValueError(f"credentials_json is neither a path nor valid JSON: {exc}")
        fd, path = tempfile.mkstemp(suffix=".json", prefix="tlt-google-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.credentials_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
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
            language_codes=[primary] + alternates,
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

                        if self._is_likely_hallucination(transcript):
                            log.info("google dropping hallucination: %r", transcript[:80])
                            continue

                        log.info("google STT final: lang=%s text=%r", detected, transcript[:80])
                        # Emit final original (will replace any interim with same text)
                        self.on_event(TranslationEvent(
                            detected_language=detected,
                            original_text=transcript,
                            translations={},
                            is_final=False,  # not final until translation arrives
                        ))
                        if self._translation_pool is not None:
                            self._translation_pool.submit(
                                self._translate_and_emit, transcript, detected
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

    def _translate_and_emit(self, transcript: str, detected: Optional[str]) -> None:
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
            ))
            self.report_ok()
        except Exception as exc:
            log.exception("google translation worker crashed")
            self.report_exception(exc, "translation worker")

    def _translate_all(self, text: str, source_lang: Optional[str]) -> dict[str, str]:
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
                self.report_exception(exc, "translate->{}".format(tgt))
            return tgt, ""

        if len(self.target_languages) == 1:
            tgt, txt = one(self.target_languages[0])
            if txt:
                out[tgt] = txt
            return out

        with ThreadPoolExecutor(max_workers=len(self.target_languages)) as exe:
            futures = [exe.submit(one, t) for t in self.target_languages]
            for fut in as_completed(futures):
                tgt, txt = fut.result()
                if txt:
                    out[tgt] = txt
        return out
