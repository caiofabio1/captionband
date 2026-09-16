"""OpenAI Realtime Translation provider — `gpt-realtime-translate`.

Streaming speech-to-speech translation over a WebSocket: source audio goes in,
translated transcript deltas come out while the speaker is still talking. The
model auto-detects the source language, so only the OUTPUT language is
configured.

API shape verified against developers.openai.com on 2026-09-15:

  endpoint  wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
  headers   Authorization: Bearer <key>
            OpenAI-Safety-Identifier: <hashed user id>
  configure {"type": "session.update",
             "session": {"audio": {"output": {"language": "es"}}}}
  audio in  {"type": "session.input_audio_buffer.append", "audio": "<b64 pcm16>"}
  audio out session.output_audio.delta      (ignored here — we want text)
  text out  session.output_transcript.delta (the translation)
            session.input_transcript.delta  (what the speaker said)
  close     {"type": "session.close"} then wait for "session.closed"

Three consequences of that design worth knowing before choosing this provider:

1. **One session per target language.** The API is configured around a single
   output language, so N targets means N WebSocket sessions, N× the audio
   uploaded and N× the cost. At US$0.034/min (2026-09-15), two targets is
   ~US$4.08/h. Azure does multi-target in ONE session and has a free tier —
   for a two-language event it is both cheaper and simpler.

2. **24 kHz input, and the app captures 16 kHz.** Audio is resampled here.
   Sending 16 kHz samples while claiming 24 kHz does not error — it just
   makes everyone sound chipmunk-fast to the model and quietly wrecks
   recognition, which is the kind of failure this app exists to not have.

3. **No documented end-of-utterance event.** The published docs list only
   `.delta` events; nothing marks a turn as finished. So utterance boundaries
   are decided HERE, by a gap timer. If OpenAI later documents a completion
   event, `_handle_event` already forwards anything ending in `.done` to the
   same finaliser — but nothing depends on that event existing.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

import numpy as np

from .base import (
    CODE_AUTH,
    CODE_NETWORK,
    CODE_UNKNOWN,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_FATAL,
    OnTranslationCallback,
    ProviderCapabilities,
    TranslationEvent,
    TranslationProvider,
)

log = logging.getLogger(__name__)


REALTIME_URL = (
    "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"
)

# The API takes PCM16 at 24 kHz. Non-negotiable, and different from the
# 16 kHz the capture pipeline uses everywhere else.
TARGET_SAMPLERATE = 24_000

# Silence gap after which the accumulated deltas are treated as a finished
# utterance. The API documents no completion event (see module docstring), so
# this is our boundary. Long enough to survive a breath, short enough that the
# transcript file gets sensible lines.
UTTERANCE_GAP_S = 1.2

# How often the finaliser thread checks for that gap.
TICK_S = 0.2

# Reconnect backoff. A venue wifi hiccup should not end the session.
RECONNECT_DELAYS_S = (1, 2, 5, 10)


def _resample_array(samples: np.ndarray, src_rate: int) -> np.ndarray:
    """One-shot polyphase resample of an int16 array to 24 kHz (float out).

    scipy when available (16k→24k is an exact 3:2 ratio, so it is cheap and
    clean); falls back to linear interpolation, which is audibly worse but
    still far better than sending the wrong rate.
    """
    try:
        from math import gcd

        from scipy.signal import resample_poly
        g = gcd(TARGET_SAMPLERATE, src_rate)
        return resample_poly(samples.astype(np.float32),
                             TARGET_SAMPLERATE // g, src_rate // g)
    except Exception:
        n_out = round(samples.size * TARGET_SAMPLERATE / src_rate)
        return np.interp(
            np.linspace(0.0, samples.size - 1, n_out, dtype=np.float64),
            np.arange(samples.size, dtype=np.float64),
            samples.astype(np.float64),
        )


class _StreamResampler:
    """Stateful mono PCM16 resampler for a continuous audio stream.

    Resampling each 50 ms block in isolation redesigns the FIR filter for
    every block and applies its edge transient to BOTH ends of every block —
    audible boundary clicks that degrade recognition. Carrying the tail of
    the previous block into the next call lets the filter see the same
    continuous signal it would see in a one-shot resample, and the carry
    buffer is reused instead of allocating three temporaries per block.
    """

    # Input samples carried across blocks. resample_poly's default FIR for
    # 3:2 is ~60 taps, so 32 samples of history covers its one-sided tail.
    CARRY_SAMPLES = 32

    def __init__(self, src_rate: int):
        self.src_rate = src_rate
        self._carry = np.zeros(0, dtype=np.int16)

    def process(self, pcm16: bytes) -> bytes:
        if self.src_rate == TARGET_SAMPLERATE:
            return pcm16
        samples = np.frombuffer(pcm16, dtype=np.int16)
        if samples.size == 0:
            return b""
        joined = np.concatenate([self._carry, samples])
        out = _resample_array(joined, self.src_rate)
        # Drop the outputs that correspond to the carried tail — they were
        # already emitted with the previous block. The first surviving
        # outputs still benefit from the filter history, which is the whole
        # point: no edge transient at the seam.
        skip = round(self._carry.size * TARGET_SAMPLERATE / self.src_rate)
        self._carry = joined[-self.CARRY_SAMPLES:].copy()
        out = out[skip:]
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def _resample_to_24k(pcm16: bytes, src_rate: int) -> bytes:
    """One-shot resample of mono PCM16 to 24 kHz.

    Correct for isolated buffers (tests, tools). The provider's live audio
    path uses _StreamResampler instead, which keeps filter state across
    blocks — see its docstring.
    """
    if src_rate == TARGET_SAMPLERATE:
        return pcm16
    return _StreamResampler(src_rate).process(pcm16)


class _TranslationSession:
    """One WebSocket session, producing ONE target language."""

    def __init__(
        self,
        api_key: str,
        target_language: str,
        on_text,           # (target, source_text, translated_text, final, result_id)
        on_error,          # (exception | str)
        safety_identifier: str = "",
    ):
        self.api_key = api_key
        self.target = target_language
        self.on_text = on_text
        self.on_error = on_error
        self.safety_identifier = safety_identifier or "teams-live-translation"

        self._ws = None
        self._thread: threading.Thread | None = None
        self._finaliser: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connected = threading.Event()

        # Accumulated deltas for the utterance in progress.
        self._src_buf = ""
        self._out_buf = ""
        self._last_delta_at = 0.0
        self._utterance_id = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"oai-rt-{self.target}", daemon=True
        )
        self._thread.start()
        self._finaliser = threading.Thread(
            target=self._finalise_loop,
            name=f"oai-rt-fin-{self.target}",
            daemon=True,
        )
        self._finaliser.start()

    def stop(self) -> None:
        self._stop.set()
        self._flush_utterance()
        ws = self._ws
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "session.close"}))
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
        self._ws = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -- audio ----------------------------------------------------------

    def push_audio_24k(self, pcm24k: bytes) -> None:
        """Send already-resampled audio. No-op until the socket is up."""
        ws = self._ws
        if ws is None or not self._connected.is_set() or not pcm24k:
            return
        payload = {
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(pcm24k).decode("ascii"),
        }
        try:
            ws.send(json.dumps(payload))
        except Exception as exc:
            # Do not spam the operator per audio block; the reader thread
            # reports the disconnect once and triggers the reconnect.
            log.debug("send failed on %s session: %s", self.target, exc)

    # -- socket ---------------------------------------------------------

    def _run(self) -> None:
        import websocket  # websocket-client

        attempt = 0
        while not self._stop.is_set():
            try:
                self._connected.clear()
                ws = websocket.create_connection(
                    REALTIME_URL,
                    header=[
                        f"Authorization: Bearer {self.api_key}",
                        f"OpenAI-Safety-Identifier: {self.safety_identifier}",
                    ],
                    timeout=10,
                )
                ws.settimeout(1.0)
                self._ws = ws
                ws.send(json.dumps({
                    "type": "session.update",
                    "session": {"audio": {"output": {"language": self.target}}},
                }))
                self._connected.set()
                attempt = 0
                log.info("openai realtime session open: target=%s", self.target)
                self._read_loop(ws)
            except Exception as exc:
                self._connected.clear()
                if self._stop.is_set():
                    return
                self.on_error(exc)
                delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
                attempt += 1
                log.warning("openai realtime (%s) reconnecting in %ss: %s",
                            self.target, delay, exc)
                self._stop.wait(delay)

    def _read_loop(self, ws) -> None:
        import websocket as _ws_mod

        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except _ws_mod.WebSocketTimeoutException:
                continue          # idle socket, not an error
            except Exception:
                if self._stop.is_set():
                    return
                raise             # let _run reconnect
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except Exception:
                log.debug("openai realtime: non-JSON frame ignored")
                continue
            self._handle_event(event)

    def _handle_event(self, event: dict) -> None:
        etype = event.get("type", "")

        if etype == "session.output_transcript.delta":
            with self._lock:
                self._out_buf += event.get("delta", "") or ""
                self._last_delta_at = time.monotonic()
            self._emit(final=False)
            return

        if etype == "session.input_transcript.delta":
            with self._lock:
                self._src_buf += event.get("delta", "") or ""
                self._last_delta_at = time.monotonic()
            self._emit(final=False)
            return

        # Translated audio: we render captions, not sound. Ignored on purpose.
        if etype == "session.output_audio.delta":
            return

        if etype == "error" or etype.endswith(".error"):
            detail = event.get("error") or event
            self.on_error(f"API: {detail}")
            return

        # Forward-compatible: the published docs list no completion event, so
        # nothing DEPENDS on this. If one is introduced, it finalises the
        # utterance earlier and more accurately than the gap timer.
        if etype.endswith(".done") or etype.endswith(".completed"):
            self._flush_utterance()
            return

        if etype == "session.closed":
            self._connected.clear()
            return

    # -- utterance assembly ---------------------------------------------

    def _emit(self, final: bool) -> None:
        with self._lock:
            src = self._src_buf.strip()
            out = self._out_buf.strip()
            rid = f"{self.target}:{self._utterance_id}"
        if not src and not out:
            return
        self.on_text(self.target, src, out, final, rid)

    def _flush_utterance(self) -> None:
        with self._lock:
            has_content = bool(self._src_buf.strip() or self._out_buf.strip())
        if not has_content:
            return
        self._emit(final=True)
        with self._lock:
            self._src_buf = ""
            self._out_buf = ""
            self._utterance_id += 1
            self._last_delta_at = 0.0

    def _finalise_loop(self) -> None:
        """Close an utterance after a gap in the delta stream.

        This exists because the API documents no end-of-turn event. It is a
        heuristic on our side, not a protocol guarantee.
        """
        while not self._stop.wait(TICK_S):
            with self._lock:
                last = self._last_delta_at
                pending = bool(self._src_buf.strip() or self._out_buf.strip())
            if pending and last and (time.monotonic() - last) >= UTTERANCE_GAP_S:
                self._flush_utterance()


class OpenAIRealtimeProvider(TranslationProvider):
    """`gpt-realtime-translate`, one WebSocket session per target language."""

    # Each session is a single ordered stream, so results arrive in order and
    # the controller's reorder gate is not needed. (With several targets there
    # are several such streams, but each carries a different language and the
    # overlay keys them independently — no cross-stream ordering to fix.)
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=True,
        translates=True,
        streaming=True,
        label="OpenAI Realtime Translate",
    )

    def __init__(
        self,
        api_key: str,
        target_languages: list[str],
        on_event: OnTranslationCallback,
        source_languages: list[str] | None = None,
        samplerate: int = 16_000,
        safety_identifier: str = "",
    ):
        if not api_key:
            raise ValueError("OpenAI api_key required")
        if not target_languages:
            raise ValueError("at least one target language required")

        self.api_key = api_key
        # The model auto-detects the source, so source_languages is accepted
        # for interface symmetry and deliberately unused.
        self.source_languages = source_languages or []
        self.target_languages = [t.split("-")[0] for t in target_languages]
        self.on_event = on_event
        self.samplerate = samplerate
        self.safety_identifier = safety_identifier

        self._sessions: list[_TranslationSession] = []
        self._lock = threading.Lock()
        self._running = False
        self._reported_cost_hint = False
        # Stateful resampler: per-block resampling put an FIR edge transient
        # on every 50 ms boundary (audible clicks, worse recognition).
        self._resampler: _StreamResampler | None = None
        # Monotonic ms of the last audio block we received. Stamped at
        # push_audio time — NOT at result time, which made the overlay's
        # end-to-end latency read ~0 for this provider.
        self._last_audio_at_ms = 0.0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            try:
                import websocket  # noqa: F401  (presence check)
            except ImportError as exc:
                raise RuntimeError(
                    "Pacote 'websocket-client' não instalado. "
                    "Instale com: pip install websocket-client"
                ) from exc

            self._sessions = [
                _TranslationSession(
                    api_key=self.api_key,
                    target_language=t,
                    on_text=self._on_session_text,
                    on_error=self._on_session_error,
                    safety_identifier=self.safety_identifier,
                )
                for t in self.target_languages
            ]
            for s in self._sessions:
                s.start()
            self._resampler = _StreamResampler(self.samplerate)
            self._last_audio_at_ms = 0.0
            self._running = True
            log.info("openai realtime provider started: targets=%s (%d sessions)",
                     self.target_languages, len(self._sessions))

        if len(self._sessions) > 1 and not self._reported_cost_hint:
            # Say this out loud once: the per-target session model means the
            # bill scales with the number of target languages, which is not
            # how any other provider here behaves.
            self._reported_cost_hint = True
            self.emit_status(
                STATUS_DEGRADED,
                CODE_UNKNOWN,
                f"{len(self._sessions)} idiomas = {len(self._sessions)} sessões simultâneas neste provedor; o custo "
                "por hora multiplica.",
            )

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            for s in self._sessions:
                try:
                    s.stop()
                except Exception:
                    log.exception("error stopping session %s", s.target)
            self._sessions = []
            self._resampler = None
            self._last_audio_at_ms = 0.0
            self._running = False
            log.info("openai realtime provider stopped")

    def push_audio(self, audio_bytes: bytes) -> None:
        if not self._running or not audio_bytes:
            return
        # Stamp arrival NOW: results come back seconds later, and the
        # overlay's latency metric measures audio-arrival → caption-out.
        self._last_audio_at_ms = time.monotonic() * 1000
        # Resample ONCE for all sessions rather than per session, through the
        # stateful stream resampler (no FIR edge transient per block).
        resampler = self._resampler
        try:
            if resampler is not None:
                pcm24 = resampler.process(audio_bytes)
            else:
                pcm24 = _resample_to_24k(audio_bytes, self.samplerate)
        except Exception as exc:
            self.report_exception(exc, "resample")
            return
        for s in self._sessions:
            s.push_audio_24k(pcm24)

    @property
    def is_running(self) -> bool:
        return self._running

    # -- callbacks ------------------------------------------------------

    def _on_session_text(
        self,
        target: str,
        source_text: str,
        translated_text: str,
        final: bool,
        result_id: str,
    ) -> None:
        if not source_text and not translated_text:
            return
        self.on_event(TranslationEvent(
            detected_language=None,     # the API does not report it per delta
            original_text=source_text,
            translations={target: translated_text} if translated_text else {},
            is_final=final,
            audio_emitted_at_ms=self._last_audio_at_ms or None,
            # Stable per (target, utterance): lets the overlay REPLACE the
            # partial in place instead of stacking a new line per delta.
            result_id=result_id,
            seq=None,                   # ordered by the socket, not by us
        ))
        if final and translated_text:
            self.report_ok()

    def _on_session_error(self, err) -> None:
        if isinstance(err, BaseException):
            self.report_exception(err, "realtime session")
            return
        text = str(err).lower()
        if "401" in text or "invalid_api_key" in text or "unauthorized" in text:
            self.emit_status(STATUS_FATAL, CODE_AUTH)
        elif "timeout" in text or "connection" in text:
            self.emit_status(STATUS_FAILING, CODE_NETWORK)
        else:
            self.emit_status(STATUS_FAILING, CODE_UNKNOWN, str(err)[:200])
