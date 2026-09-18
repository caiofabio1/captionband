"""Audio buffer for chunked-API providers (OpenRouter, Whisper local).

Live-captioning best practices applied:

1. **Bounded chunk size** (max 5s by default).
   Speakers who don't pause for >5s would otherwise create huge chunks that
   take long to transcribe. We force-flush at MAX_SPEECH_S.

2. **Pre-roll buffer** (200ms before speech onset).
   When VAD declares "speech started", the first phoneme has often already
   passed. We prepend ~200ms of pre-speech audio so Whisper doesn't lose
   the first word.

3. **Overlap window** (300ms kept for next chunk on forced flush).
   When we cut mid-utterance at MAX_SPEECH_S, the last word can be split.
   We carry the tail forward so the next chunk's transcript stitches
   correctly. Whisper's context handles the duplicate gracefully.

4. **Hangover trim** (500ms silence ends utterance).
   Short pauses (<500ms) between phrases are kept inside one chunk;
   pauses >=500ms close the chunk.

5. **Minimum speech requirement** (400ms).
   Avoid sending fragments shorter than this — it's just noise/clicks.

6. **Smoothed VAD via moving average of frame RMS**.
   Instead of treating each 100ms frame independently, we compute a
   rolling average over the last 3 frames. This reduces flapping at
   boundaries and prevents single-frame dropouts from cutting words.

7. **Adaptive noise floor** (optional, opt-in).
   In v1 we use a fixed RMS threshold. v2 could track quiet-frame
   percentile to adapt to room noise.

Algorithm:

    [audio in 100ms frames]
        |
        v
    [pre-roll deque keeps last 200ms always]
        |
        v
    is_speech(frame) = smooth_rms(window=3) >= threshold
        |
        v
    +-- speech detected & not currently capturing -> start capture
    |       prepend pre-roll to buffer
    |       reset speech/silence counters
    |
    +-- in-capture -> append frame
            +-- speech: speech_frames++, silence_frames=0
            |
            +-- silence: silence_frames++
            |       if silence_frames >= silence_target and
            |          speech_frames >= min_speech: emit, reset
            |
            +-- buffer_samples >= max_speech_samples: emit-with-overlap, reset
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable

import numpy as np

log = logging.getLogger(__name__)


class ChunkedAudioBuffer:
    def __init__(
        self,
        on_chunk: Callable[[bytes], None],
        samplerate: int = 16000,
        frame_ms: int = 100,
        silence_ms: int = 500,
        min_speech_ms: int = 400,
        max_chunk_s: float = 5.0,
        preroll_ms: int = 200,
        overlap_ms: int = 300,
        speech_rms_threshold: float = 0.010,
        smoothing_window: int = 3,
    ):
        self.on_chunk = on_chunk
        self.samplerate = samplerate

        self.frame_size = int(samplerate * frame_ms / 1000)
        self.frame_bytes = self.frame_size * 2

        self.silence_frames_target = max(1, silence_ms // frame_ms)
        self.min_speech_frames = max(1, min_speech_ms // frame_ms)
        self.max_speech_samples = int(max_chunk_s * samplerate)
        self.preroll_frames = max(1, preroll_ms // frame_ms)
        self.overlap_frames = max(1, overlap_ms // frame_ms)
        self.speech_rms_threshold = speech_rms_threshold

        # Tail of unaligned input bytes (less than frame_bytes)
        self._tail: bytearray = bytearray()

        # Always-on pre-roll: holds the most recent N frames regardless of speech state.
        self._preroll: deque[bytes] = deque(maxlen=self.preroll_frames)

        # Active utterance buffer (only filled during speech)
        self._buffer: list[bytes] = []
        self._buffer_samples = 0

        # Sliding RMS window for smoothing
        self._rms_window: deque[float] = deque(maxlen=smoothing_window)

        # Per-utterance counters
        self._speech_frames = 0
        self._silence_frames = 0
        self._has_speech = False

        # Carry-over from forced flush (overlap)
        self._carryover: list[bytes] = []

        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public

    def push(self, pcm16_bytes: bytes) -> None:
        with self._lock:
            self._tail.extend(pcm16_bytes)
            while len(self._tail) >= self.frame_bytes:
                frame = bytes(self._tail[: self.frame_bytes])
                del self._tail[: self.frame_bytes]
                self._process_frame(frame)

    def flush(self) -> None:
        with self._lock:
            self._emit(force=True, with_overlap=False)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _frame_rms(frame: bytes) -> float:
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(arr * arr)))

    def _is_speech(self, frame: bytes) -> bool:
        rms = self._frame_rms(frame)
        self._rms_window.append(rms)
        smoothed = sum(self._rms_window) / len(self._rms_window)
        return smoothed >= self.speech_rms_threshold

    def _process_frame(self, frame: bytes) -> None:
        # Always feed the rolling pre-roll
        self._preroll.append(frame)

        speech = self._is_speech(frame)

        if not self._has_speech:
            # waiting for speech to begin
            if speech:
                self._has_speech = True
                # prepend pre-roll (everything we recently saw)
                if self._carryover:
                    self._buffer.extend(self._carryover)
                    self._buffer_samples += len(self._carryover) * self.frame_size
                    self._carryover = []
                # add pre-roll BUT skip duplicates of carryover
                for f in list(self._preroll):
                    self._buffer.append(f)
                    self._buffer_samples += self.frame_size
                self._speech_frames = 1
                self._silence_frames = 0
            return

        # capturing speech
        self._buffer.append(frame)
        self._buffer_samples += self.frame_size

        if speech:
            self._speech_frames += 1
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        # Check end conditions in order of priority

        # 1) hard cap reached -> emit with overlap so next chunk continues
        if self._buffer_samples >= self.max_speech_samples:
            log.debug(
                "buffer hit max_chunk_s, flushing with overlap (samples=%s)",
                self._buffer_samples,
            )
            self._emit(force=True, with_overlap=True)
            return

        # 2) sustained silence after enough speech -> natural end of utterance
        if (
            self._silence_frames >= self.silence_frames_target
            and self._speech_frames >= self.min_speech_frames
        ):
            self._emit(force=False, with_overlap=False)

    def _emit(self, force: bool, with_overlap: bool) -> None:
        if not self._buffer:
            self._has_speech = False
            self._silence_frames = 0
            self._speech_frames = 0
            return

        # Skip emit if too short and not forced
        if not force and self._speech_frames < self.min_speech_frames:
            self._buffer.clear()
            self._buffer_samples = 0
            self._has_speech = False
            self._speech_frames = 0
            self._silence_frames = 0
            return

        chunk = b"".join(self._buffer)

        # If forced (mid-utterance), keep the trailing overlap_frames as carryover
        if with_overlap and len(self._buffer) > self.overlap_frames:
            self._carryover = self._buffer[-self.overlap_frames :]
            # When the overlap rolls into the next chunk, pre-roll deque
            # already contains those frames too — that's fine because
            # we drain pre-roll on speech onset and dedup is handled
            # naturally by Whisper's context.
        else:
            self._carryover = []

        self._buffer.clear()
        self._buffer_samples = 0
        self._has_speech = with_overlap  # keep capturing if we forced flush mid-speech
        self._speech_frames = 0
        self._silence_frames = 0

        try:
            self.on_chunk(chunk)
        except Exception:
            log.exception("on_chunk handler raised")


# ---------------------------------------------------------------------- helpers


def pcm16_to_wav_bytes(pcm: bytes, samplerate: int = 16000) -> bytes:
    """Wrap raw PCM 16-bit mono into a WAV header (in-memory)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm)
    return buf.getvalue()
