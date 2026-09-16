"""Groq provider — Whisper-large-v3-turbo + Llama for translation.

Groq exposes an OpenAI-compatible API at https://api.groq.com/openai/v1.
We use the official OpenAI Python SDK (with base_url override) to avoid
Cloudflare's bot/browser-fingerprint filter that blocks raw urllib requests.

Pipeline:
  audio chunk (VAD-segmented) → /audio/transcriptions (whisper-large-v3-turbo)
                              → text + detected language
                              → /chat/completions (llama-3.3-70b) prompt: "Translate to {target}"
                              → translated text per target language

Latency: ~700ms–1.5s per utterance
Cost (Apr 2026): ~$0.04/h Whisper turbo + ~$0.06/h Llama → ~$0.10/h total
"""
from __future__ import annotations

import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

from ._audio_buffer import ChunkedAudioBuffer, pcm16_to_wav_bytes
from .base import (
    CODE_UNKNOWN,
    STATUS_DEGRADED,
    OnTranslationCallback,
    ProviderCapabilities,
    TranslationEvent,
    TranslationProvider,
)

log = logging.getLogger(__name__)


GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# How much of the previous transcript to hand Whisper as context. The REST
# transcription endpoint is stateless, so the 300 ms of audio the buffer
# carries across a forced flush gets transcribed twice unless we tell the
# model what just came before. Whisper uses `prompt` to condition decoding,
# which both de-duplicates the seam and improves proper nouns.
STT_CONTEXT_CHARS = 200


class GroqProvider(TranslationProvider):
    # Chunked REST: results race each other through the thread pool, so the
    # controller must run them through the reorder gate.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False,
        translates=True,      # via a second hop to the chat model
        streaming=False,
        label="Groq (Whisper turbo + Llama)",
    )

    # Backpressure cap: how many chunks may sit QUEUED (submitted but not yet
    # started) before we drop the oldest one. The executor's own queue is
    # unbounded, and when the network makes a chunk take longer than the
    # speech that produced it, that queue becomes a latency accumulator:
    # every caption still arrives, just later and later — tens of seconds
    # behind by mid-event — pinning hundreds of MB of PCM over a long event.
    # A live caption 30 s late is worse than a dropped one.
    MAX_PENDING_CHUNKS = 10
    # Throttle for the DEGRADED warning so a slow stretch does not spam it.
    BACKPRESSURE_WARN_INTERVAL_S = 30.0

    def __init__(
        self,
        api_key: str,
        transcription_model: str,
        translation_model: str,
        source_languages: list[str],
        target_languages: list[str],
        on_event: OnTranslationCallback,
        samplerate: int = 16000,
        chunk_seconds: float = 4.0,
    ):
        if not api_key:
            raise ValueError("Groq api_key required")
        self.api_key = api_key
        self.transcription_model = transcription_model or "whisper-large-v3-turbo"
        self.translation_model = translation_model or "llama-3.3-70b-versatile"
        self.source_languages = source_languages
        self.target_languages = target_languages
        self.on_event = on_event
        self.samplerate = samplerate
        self.chunk_seconds = chunk_seconds

        self._client = None
        self._stt_client = None
        self._translation_client = None
        self._executor: ThreadPoolExecutor | None = None
        # Persistent pool for the per-utterance multi-target translation
        # fan-out (a fresh ThreadPoolExecutor per utterance paid thread-spawn
        # cost on every sentence of the event).
        self._translation_pool: ThreadPoolExecutor | None = None
        self._buffer: ChunkedAudioBuffer | None = None
        # (seq, future) of chunks submitted to _executor and not known to be
        # finished. Backlog accounting for the drop-oldest cap above.
        self._pending: list = []
        self._pending_lock = threading.Lock()
        self._last_backpressure_warning = 0.0
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            if OpenAI is None:
                raise RuntimeError(
                    "Pacote 'openai' não instalado. Instale com `pip install openai`."
                )

            # Split: STT client and translation client (both override-able in subclasses)
            self._stt_client = self._build_stt_client()
            self._translation_client = self._build_translation_client()
            # Keep _client for backwards compat in case external code reads it
            self._client = self._stt_client
            # 5 workers handle ~2s chunks at ~1.5s each => can sustain
            # continuous speech without backlog.
            self._executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="groq-worker")
            # Separate pool for the translation fan-out: a _process_chunk
            # worker must never wait on the SAME pool it runs in (a 5-worker
            # pool with 5 chunks waiting on their own fan-outs deadlocks).
            self._translation_pool = ThreadPoolExecutor(
                max_workers=max(2, len(self.target_languages)),
                thread_name_prefix="groq-translate",
            )
            with self._pending_lock:
                self._pending = []
            self._last_backpressure_warning = 0.0
            # ChunkedAudioBuffer enforces max_chunk_s itself (default 5s).
            # We pass user's chunk_seconds as the cap.
            self._buffer = ChunkedAudioBuffer(
                on_chunk=self._on_chunk,
                samplerate=self.samplerate,
                max_chunk_s=max(2.0, min(self.chunk_seconds, 5.0)),
            )
            self._sequence = 0
            # Tail of the last transcript, fed to Whisper as `prompt` so the
            # overlap audio carried across a forced flush is not transcribed
            # as a duplicated word. Guarded by _context_lock: worker threads
            # read and write it concurrently.
            self._last_transcript = ""
            self._context_lock = threading.Lock()
            self._running = True
            log.info(
                "groq provider started: stt_model=%s translate_model=%s max_chunk=%.1fs",
                self.transcription_model,
                self.translation_model,
                self._buffer.max_speech_samples / self.samplerate,
            )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            try:
                if self._buffer is not None:
                    self._buffer.flush()
            except Exception:
                pass
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None
            if self._translation_pool is not None:
                self._translation_pool.shutdown(wait=False, cancel_futures=True)
                self._translation_pool = None
            with self._pending_lock:
                self._pending = []
            self._buffer = None
            self._stt_client = None
            self._translation_client = None
            self._client = None
            self._running = False
            log.info("groq provider stopped")

    def push_audio(self, audio_bytes: bytes) -> None:
        if self._buffer is not None:
            self._buffer.push(audio_bytes)

    @property
    def is_running(self) -> bool:
        return self._running

    # --- internals -----------------------------------------------------

    def _on_chunk(self, pcm: bytes) -> None:
        if self._executor is None:
            return
        emitted_at_ms = time.monotonic() * 1000
        seq = self._sequence
        self._sequence += 1
        fut = self._executor.submit(self._process_chunk, seq, pcm, emitted_at_ms)
        self._enforce_backlog_cap(seq, fut)

    def _enforce_backlog_cap(self, seq: int, fut) -> None:
        """Drop the OLDEST queued chunk when the backlog passes the cap.

        Only queued (not-yet-started) chunks are droppable; one already
        running leaves the accounting and releases its own seq when it
        finishes. A dropped chunk MUST release its reorder-gate slot, or
        every later caption waits on a chunk that will never arrive.
        """
        dropped: list[int] = []
        with self._pending_lock:
            self._pending = [(s, f) for s, f in self._pending if not f.done()]
            self._pending.append((seq, fut))
            while len(self._pending) > self.MAX_PENDING_CHUNKS:
                old_seq, old_fut = self._pending.pop(0)
                if old_fut.cancel():
                    dropped.append(old_seq)
        for old_seq in dropped:
            log.warning(
                "groq backlog: dropping queued chunk seq=%s "
                "(processing is slower than speech)", old_seq)
            self._release_empty(old_seq)
        if dropped:
            self._report_backpressure()

    def _report_backpressure(self) -> None:
        """Tell the operator chunks are being dropped — throttled.

        Without this, a slow stretch presents as mysteriously incomplete
        captions behind a green tray icon.
        """
        now = time.monotonic()
        if now - self._last_backpressure_warning < self.BACKPRESSURE_WARN_INTERVAL_S:
            return
        self._last_backpressure_warning = now
        self.emit_status(
            STATUS_DEGRADED,
            CODE_UNKNOWN,
            "Processando mais devagar que a fala — descartando chunks para a "
            "legenda não atrasar.",
        )

    def _process_chunk(self, seq: int, pcm: bytes, emitted_at_ms: float) -> None:
        """Transcribe + translate one chunk.

        Every exit path must either emit an event carrying `seq` or emit a
        status. A chunk that leaves without doing one of the two stalls the
        controller's reorder gate until its deadline and shows the operator
        nothing — the exact silent failure this app was losing events to.
        """
        try:
            wav = pcm16_to_wav_bytes(pcm, self.samplerate)
            t0 = time.time()
            transcribed = self._call_transcription(wav)
            stt_dt = (time.time() - t0) * 1000
            if not transcribed:
                # _call_transcription already emitted a status describing why.
                self._release_empty(seq)
                return
            text, detected = transcribed
            text = (text or "").strip()
            if not text:
                # Genuine silence — not a failure, but the gate still needs
                # this slot released or every later caption waits on it.
                self._release_empty(seq)
                return
            if self._is_likely_hallucination(text):
                log.info("groq dropping likely hallucination: %r", text[:80])
                self._release_empty(seq)
                return

            log.info("groq STT [seq=%s] in %.0fms: lang=%s text=%r",
                     seq, stt_dt, detected, text[:80])

            with self._context_lock:
                self._last_transcript = text[-STT_CONTEXT_CHARS:]

            # NO partial event here, deliberately.
            #
            # A partial would carry seq=None to skip the reorder gate and show
            # the original text a second earlier. But partials from a thread
            # pool race each other exactly like finals do: the worker handling
            # a short chunk 2 can publish its partial before the worker still
            # grinding on chunk 1 publishes anything. Since these partials
            # carry no result_id, the overlay treats each as a NEW line rather
            # than replacing one — so the audience sees the out-of-order text
            # this whole design exists to prevent.
            #
            # Streaming providers (Azure, Google) keep their partials: theirs
            # come from one ordered session and do carry a result_id.

            translations = self._translate_all(text, detected)

            self.on_event(TranslationEvent(
                detected_language=detected,
                original_text=text,
                translations=translations,
                is_final=True,
                audio_emitted_at_ms=emitted_at_ms,
                seq=seq,
            ))
            self.report_ok()
        except Exception as exc:
            log.exception("groq processing failed (seq=%s)", seq)
            self.report_exception(exc, "process_chunk")
            self._release_empty(seq)

    def _release_empty(self, seq: int) -> None:
        """Release a sequence slot that produced no text.

        Without this the reorder gate holds every later caption hostage to a
        chunk that will never arrive, until the deadline expires.
        """
        try:
            self.on_event(TranslationEvent(
                detected_language=None,
                original_text="",
                translations={},
                is_final=True,
                audio_emitted_at_ms=None,
                seq=seq,
            ))
        except Exception:
            log.exception("failed to release empty seq=%s", seq)

    @staticmethod
    def _is_likely_hallucination(text: str) -> bool:
        """Drop Whisper's known near-silence artifacts — and nothing else.

        The previous denylist included "thank you." and "obrigado.", which are
        things speakers genuinely say, most often while CLOSING an event. The
        closing line of a webinar vanishing from the captions is a
        worse failure than letting one artifact through, so the list now holds
        only strings that carry no meaning even when the speaker did say them:
        bracketed annotations and music glyphs Whisper emits for non-speech.

        Filler words ("uh", "um") are deliberately NOT filtered. They are real
        speech, and stripping them mid-sentence makes the caption read as if
        words were lost.
        """
        normalized = text.strip().lower()
        if not normalized:
            return True
        # Pure punctuation / music glyphs: never meaningful.
        if all(c in ".,!?-–—…♪♫ \t" for c in normalized):
            return True
        # Whisper's non-speech annotations, e.g. "[music]", "(applause)".
        if (normalized.startswith("[") and normalized.endswith("]")) or (
            normalized.startswith("(") and normalized.endswith(")")
        ):
            return True
        # "Thanks for watching" / "subtitles by ..." — Whisper trained on
        # YouTube captions emits these verbatim over silence. Kept narrow:
        # the full phrase, not the words "thank you" on their own.
        youtube_artifacts = {
            "thanks for watching!", "thanks for watching.", "thanks for watching",
            "subscribe!", "legendas pela comunidade amara.org",
            "subtitles by the amara.org community",
        }
        if normalized in youtube_artifacts:
            return True
        return False

    def _build_stt_client(self):
        """Override in subclasses to route STT to a different API.

        Uses the module-level OpenAI global (not a local import) so tests
        can patch providers.groq.OpenAI and so start()'s
        `OpenAI is None` guard actually protects this call.
        """
        return OpenAI(
            api_key=self.api_key,
            base_url=GROQ_BASE_URL,
            timeout=30.0,
            max_retries=2,
        )

    def _stt_model_for_call(self) -> str:
        """Override in subclasses to use a different STT model name."""
        return self.transcription_model

    def _build_translation_client(self):
        """Override in subclasses to route translation to a different API.

        Same reasoning as _build_stt_client: module-level global, not a
        local import.
        """
        return OpenAI(
            api_key=self.api_key,
            base_url=GROQ_BASE_URL,
            timeout=30.0,
            max_retries=2,
        )

    def _translation_model_for_call(self) -> str:
        """Override in subclasses to use a different model name."""
        return self.translation_model

    def _call_transcription(self, wav_bytes: bytes) -> tuple[str, str | None] | None:
        if self._stt_client is None:
            return None
        try:
            buf = io.BytesIO(wav_bytes)
            buf.name = "audio.wav"  # OpenAI SDK needs a filename attribute
            kwargs = {
                "file": buf,
                "model": self._stt_model_for_call(),
                "response_format": "verbose_json",
                "temperature": 0.0,
            }
            # Condition decoding on what was just said. The transcription
            # endpoint is stateless, so without this the overlap audio the
            # buffer carries across a forced flush comes back as a repeated
            # word at the seam.
            with self._context_lock:
                context = self._last_transcript
            if context:
                kwargs["prompt"] = context
            resp = self._stt_client.audio.transcriptions.create(**kwargs)
            text = (resp.text or "").strip()
            lang = getattr(resp, "language", None)
            return text, lang
        except Exception as exc:
            self.report_exception(exc, "transcription")
            return None

    def _translate_all(self, text: str, source_lang: str | None) -> dict[str, str]:
        out: dict[str, str] = {}
        if self._translation_client is None:
            return out

        def one(tgt: str) -> tuple[str, str]:
            if source_lang and source_lang.lower().startswith(tgt.lower()):
                return tgt, text
            translated = self._call_translation(text, source_lang, tgt)
            return tgt, (translated or "")

        if len(self.target_languages) == 1:
            # The single-target path is the common default, so it must reach
            # the degraded-status check below rather than returning early.
            tgt, txt = one(self.target_languages[0])
            if txt:
                out[tgt] = txt
        else:
            # Persistent fan-out pool (created in start()). A fresh
            # ThreadPoolExecutor per utterance paid thread-spawn cost on
            # every sentence of the event.
            pool = self._translation_pool
            if pool is None:
                # stop() ran mid-utterance: finish sequentially.
                for t in self.target_languages:
                    tgt, txt = one(t)
                    if txt:
                        out[tgt] = txt
            else:
                futures = [pool.submit(one, t) for t in self.target_languages]
                for fut in as_completed(futures):
                    tgt, txt = fut.result()
                    if txt:
                        out[tgt] = txt

        if not out:
            # We have the original but no translation. The overlay will show
            # source-language text, which looks like it is working while the
            # audience gets nothing they can read. Say so out loud.
            self.emit_status(
                STATUS_DEGRADED,
                CODE_UNKNOWN,
                "Transcrevendo, mas a tradução está falhando.",
            )
        return out

    def _call_translation(self, text: str, source_lang: str | None, target_lang: str) -> str | None:
        if self._translation_client is None:
            return None
        prompt = (
            f"You are a professional translator. Translate the following text to {target_lang}. "
            "Output ONLY the translation, no quotes, no explanations, no notes."
        )
        if source_lang:
            prompt += f" The source language is {source_lang}."
        try:
            resp = self._translation_client.chat.completions.create(
                model=self._translation_model_for_call(),
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=600,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            self.report_exception(exc, f"translation->{target_lang}")
            return None
