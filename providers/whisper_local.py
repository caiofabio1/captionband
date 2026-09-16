"""Whisper local provider — faster-whisper + Argos Translate (offline).

Pipeline:
  audio chunk (VAD-segmented) → faster-whisper transcribe (auto-detect language)
                              → argostranslate.translate (per target language)
                              → emit TranslationEvent

100% offline after model download. Models live at:
  %LOCALAPPDATA%\\CaptionBand\\models\\
  - faster-whisper: cached by Hugging Face transformers (Systran/faster-whisper-*)
  - argos-translate: ~/.local/share/argos-translate/packages/

Latency: 3–6s per utterance (CPU, base model)
         1–3s with GPU and small model
Cost: zero recurring; one-time download ~150MB-3GB depending on model.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ._audio_buffer import ChunkedAudioBuffer
from .base import (
    CODE_UNKNOWN,
    STATUS_DEGRADED,
    OnTranslationCallback,
    ProviderCapabilities,
    TranslationEvent,
    TranslationProvider,
)

log = logging.getLogger(__name__)


WHISPER_MODELS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "Systran/faster-whisper-large-v3-turbo",
}


class WhisperLocalProvider(TranslationProvider):
    # Chunked local inference across a 2-worker pool: a short chunk finishes
    # before a longer earlier one, so results need the reorder gate.
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False,
        translates=True,      # via Argos Translate, offline
        streaming=False,
        label="Whisper local (offline)",
    )

    # Backpressure cap: chunks QUEUED (submitted, not yet started) before we
    # drop the oldest. CPU transcription takes 3–6 s per chunk against chunks
    # arriving every ~2 s, so an unbounded executor queue becomes a latency
    # accumulator: every caption arrives, just later and later — and ~230 MB
    # of PCM piles up over a 2 h event. A caption 30 s late is worse than a
    # dropped one.
    MAX_PENDING_CHUNKS = 4          # 2 workers × 2
    # Throttle for the DEGRADED warning so a slow stretch does not spam it.
    BACKPRESSURE_WARN_INTERVAL_S = 30.0

    def __init__(
        self,
        whisper_model: str,
        whisper_device: str,
        whisper_compute_type: str,
        source_languages: list[str],
        target_languages: list[str],
        on_event: OnTranslationCallback,
        samplerate: int = 16000,
        chunk_seconds: float = 4.0,
    ):
        self.whisper_model = whisper_model or "small"
        self.whisper_device = whisper_device or "cpu"
        self.whisper_compute_type = whisper_compute_type or "int8"
        self.source_languages = source_languages
        self.target_languages = target_languages
        self.on_event = on_event
        self.samplerate = samplerate
        self.chunk_seconds = chunk_seconds

        self._model = None
        self._executor: ThreadPoolExecutor | None = None
        self._buffer: ChunkedAudioBuffer | None = None
        # (seq, future) of submitted chunks not known to be finished —
        # backlog accounting for the drop-oldest cap above.
        self._pending: list = []
        self._pending_lock = threading.Lock()
        self._last_backpressure_warning = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._argos_loaded: dict[tuple[str, str], object] = {}

    def _model_dir(self) -> str:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        target = os.path.join(base, "CaptionBand", "models")
        os.makedirs(target, exist_ok=True)
        return target

    def _load_model(self):
        from faster_whisper import WhisperModel

        repo_id = WHISPER_MODELS.get(self.whisper_model, self.whisper_model)
        log.info(
            "loading faster-whisper model=%s device=%s compute_type=%s",
            repo_id, self.whisper_device, self.whisper_compute_type,
        )
        return WhisperModel(
            repo_id,
            device=self.whisper_device,
            compute_type=self.whisper_compute_type,
            download_root=self._model_dir(),
        )

    def _ensure_argos(self) -> None:
        try:
            import argostranslate.package as ap
            import argostranslate.translate  # noqa: F401  (availability probe)
        except ImportError:
            log.warning("argostranslate not available — translation disabled")
            return

        ap.update_package_index()
        installed = {(p.from_code, p.to_code) for p in ap.get_installed_packages()}
        wanted: set[tuple[str, str]] = set()
        for src in self.source_languages:
            src_short = src.split("-")[0]
            for tgt in self.target_languages:
                tgt_short = tgt.split("-")[0]
                if src_short == tgt_short:
                    continue
                wanted.add((src_short, tgt_short))

        for from_code, to_code in wanted:
            if (from_code, to_code) in installed:
                continue
            try:
                available = ap.get_available_packages()
                pkg = next((p for p in available if p.from_code == from_code and p.to_code == to_code), None)
                if pkg is None:
                    log.warning("no argos package %s->%s", from_code, to_code)
                    continue
                log.info("downloading argos %s->%s", from_code, to_code)
                ap.install_from_path(pkg.download())
            except Exception:
                log.exception("failed to install argos %s->%s", from_code, to_code)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._model = self._load_model()
            self._ensure_argos()
            self._executor = ThreadPoolExecutor(max_workers=2)
            self._buffer = ChunkedAudioBuffer(
                on_chunk=self._on_chunk,
                samplerate=self.samplerate,
                max_chunk_s=max(self.chunk_seconds * 2, 8.0),
            )
            self._sequence = 0
            with self._pending_lock:
                self._pending = []
            self._last_backpressure_warning = 0.0
            self._running = True
            log.info("whisper-local provider started")

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
            with self._pending_lock:
                self._pending = []
            self._buffer = None
            self._model = None
            self._running = False
            log.info("whisper-local provider stopped")

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
        # Sequence at CHUNK time. Two workers transcribe in parallel and a
        # short chunk routinely finishes before a longer earlier one, so
        # without this the captions arrive shuffled.
        seq = self._sequence
        self._sequence += 1
        emitted_at_ms = time.monotonic() * 1000
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
                "whisper-local backlog: dropping queued chunk seq=%s "
                "(transcription is slower than speech)", old_seq)
            self._release_empty(old_seq)
        if dropped:
            self._report_backpressure()

    def _report_backpressure(self) -> None:
        """Tell the operator chunks are being dropped — throttled."""
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
        """Every exit path must release `seq`, or the reorder gate holds all
        later captions until its deadline expires."""
        try:
            import numpy as np

            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            if audio.size == 0:
                self._release_empty(seq)
                return

            language_hint: str | None = None
            if len(self.source_languages) == 1:
                language_hint = self.source_languages[0].split("-")[0]

            segments, info = self._model.transcribe(
                audio,
                language=language_hint,
                vad_filter=True,
                beam_size=1,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if not text:
                self._release_empty(seq)
                return
            detected = info.language

            translations: dict[str, str] = {}
            failed_targets = 0
            for tgt in self.target_languages:
                tgt_short = tgt.split("-")[0]
                if detected == tgt_short:
                    translations[tgt] = text
                    continue
                translated = self._translate_argos(text, detected, tgt_short)
                if translated:
                    translations[tgt] = translated
                else:
                    failed_targets += 1

            if failed_targets and not translations:
                # Transcribing fine, translating nothing. The overlay would
                # show source-language text and look healthy while the
                # audience gets nothing they can read.
                self.emit_status(
                    STATUS_DEGRADED,
                    CODE_UNKNOWN,
                    "Transcrevendo, mas o modelo de tradução local falhou. "
                    "Verifique os pacotes do Argos Translate.",
                )

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
            log.exception("whisper-local processing failed (seq=%s)", seq)
            self.report_exception(exc, "process_chunk")
            self._release_empty(seq)

    def _release_empty(self, seq: int) -> None:
        """Free a gate slot that produced no text."""
        try:
            self.on_event(TranslationEvent(
                detected_language=None,
                original_text="",
                translations={},
                is_final=True,
                seq=seq,
            ))
        except Exception:
            log.exception("failed to release empty seq=%s", seq)

    def _translate_argos(self, text: str, src: str | None, tgt: str) -> str | None:
        try:
            import argostranslate.translate as at
        except ImportError:
            return None
        if not src:
            return None
        try:
            return at.translate(text, src, tgt)
        except Exception:
            log.exception("argos translate failed src=%s tgt=%s", src, tgt)
            return None
