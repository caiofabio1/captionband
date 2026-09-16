"""WASAPI loopback audio capture for Windows.

Captures system audio (whatever is playing through the speakers/Teams) without
needing a virtual cable, via the `soundcard` library which has native WASAPI
loopback support.

Uses `sounddevice` only to enumerate output devices (lighter API for that).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import numpy as np
import soundcard as sc

log = logging.getLogger(__name__)

# COM apartment helpers. Every thread that touches `soundcard` needs COM
# initialised on it, because the process is no longer blanket-MTA: the entry
# point claims STA for the GUI thread so Qt works properly. See the comment at
# the top of translator.py for the measurement behind this.
_COINIT_MULTITHREADED = 0x0
# RPC_E_CHANGED_MODE: this thread already has a different apartment. Harmless —
# it means somebody initialised COM here first, so we must NOT uninitialise it.
_RPC_E_CHANGED_MODE = 0x80010106


def _com_initialize_mta() -> bool:
    """Put the CALLING thread in the MTA apartment. True if we must balance it."""
    try:
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) & 0xFFFFFFFF
    except Exception:
        return False
    if hr == _RPC_E_CHANGED_MODE:
        log.debug("com: thread already in another apartment; not claiming it")
        return False
    return True


def _com_uninitialize() -> None:
    try:
        import ctypes
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


def list_output_devices() -> list[dict]:
    """List output devices visible to soundcard (these support loopback)."""
    out: list[dict] = []
    try:
        speakers = sc.all_speakers()
    except Exception:
        log.exception("could not list speakers")
        return out
    for spk in speakers:
        out.append({
            "id": spk.id,
            "name": spk.name,
            "channels": spk.channels,
            "default_samplerate": 48000,
        })
    return out


def find_device(name_substring: str | None):
    """Resolve a speaker by partial name match. Returns a soundcard speaker or None for default."""
    if not name_substring:
        try:
            return sc.default_speaker()
        except Exception:
            log.exception("could not get default speaker")
            return None
    try:
        speakers = sc.all_speakers()
    except Exception:
        log.exception("could not enumerate speakers")
        return None
    needle = name_substring.lower()
    for spk in speakers:
        if needle in spk.name.lower():
            return spk
    return None


class AudioCapture:
    """Captures system audio via WASAPI loopback and forwards PCM 16-bit bytes to a callback.

    Uses soundcard.Microphone(include_loopback=True) on the chosen output device.
    """

    def __init__(
        self,
        on_audio: Callable[[bytes], None],
        device_index=None,
        samplerate: int = 16000,
        channels: int = 1,
        blocksize_ms: int = 50,
        on_died: Callable[[str], None] | None = None,
    ):
        # device_index can be: None (default), a soundcard speaker, or a name substring
        self.on_audio = on_audio
        self.device_arg = device_index
        self.samplerate = samplerate
        self.channels = max(1, channels)
        self.blocksize = max(1, int(samplerate * blocksize_ms / 1000))
        # Called (from the capture thread) when the loop exits for any reason
        # other than a requested stop. Without it, unplugging the headphones
        # mid-event kills this thread while the tray icon stays green and the
        # overlay stays blank — indistinguishable from nobody speaking.
        self.on_died = on_died

        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        # Monotonic timestamp of the last audio block actually delivered.
        # The controller's watchdog reads this: a live thread that has stopped
        # producing audio is just as fatal as a dead one, and only this
        # distinguishes "silence in the room" from "capture is wedged".
        self._last_audio_at: float = 0.0
        self._died_reason: str = ""

    def _resolve_speaker(self):
        if self.device_arg is None or self.device_arg == "":
            return sc.default_speaker()
        if hasattr(self.device_arg, "id") and hasattr(self.device_arg, "name"):
            return self.device_arg
        if isinstance(self.device_arg, str):
            return find_device(self.device_arg)
        return sc.default_speaker()

    def _run(self) -> None:
        """Capture loop.

        Every exit path other than a requested stop reports a reason through
        `on_died`. Returning quietly here is what let a disconnected audio
        device silently end the captions for the rest of an event.
        """
        reason = ""
        # Claim MTA for THIS thread. The entry point puts the GUI thread in STA
        # so Qt's OleInitialize() succeeds, which means soundcard's module-level
        # CoInitializeEx(MTA) got RPC_E_CHANGED_MODE and set com_loaded=False.
        # Without this call the capture thread has no COM at all and
        # sc.default_speaker() dies with 0x800401F0 CO_E_NOTINITIALIZED — the
        # exact failure that made the previous attempt at this fix get reverted.
        com_ready = _com_initialize_mta()
        try:
            speaker = self._resolve_speaker()
            if speaker is None:
                reason = "Nenhum dispositivo de saída de áudio encontrado."
                log.error("audio capture: no output device resolved")
                return
            log.info("audio capture using speaker: %s", speaker.name)
            loopback_mic = sc.get_microphone(speaker.name, include_loopback=True)

            with loopback_mic.recorder(
                samplerate=self.samplerate,
                channels=self.channels,
                blocksize=self.blocksize,
            ) as recorder:
                self._last_audio_at = time.monotonic()
                while not self._stop_evt.is_set():
                    try:
                        data = recorder.record(numframes=self.blocksize)
                    except Exception as exc:
                        # The common live-event trigger: headphones unplugged,
                        # Bluetooth dropped, or Windows switched the default
                        # output device out from under us.
                        reason = f"A captura de áudio parou: {exc}"
                        log.exception("recorder.record failed")
                        break
                    # Mark liveness BEFORE inspecting the payload. This
                    # timestamp answers "is the recorder still responding?",
                    # not "was there sound?" — a driver that returns empty
                    # buffers during a pause is alive, and skipping the
                    # update there would let the watchdog kill a healthy
                    # session the moment the speaker stopped to breathe.
                    self._last_audio_at = time.monotonic()
                    if data is None or data.size == 0:
                        continue
                    if data.ndim > 1 and data.shape[1] > 1 and self.channels == 1:
                        data = data.mean(axis=1, keepdims=True)
                    pcm16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
                    try:
                        self.on_audio(pcm16.tobytes())
                    except Exception:
                        log.exception("on_audio callback failed")
        except Exception as exc:
            reason = f"A captura de áudio caiu: {exc}"
            log.exception("audio capture thread crashed")
        finally:
            if com_ready:
                _com_uninitialize()
            self._running = False
            if reason and not self._stop_evt.is_set():
                self._died_reason = reason
                if self.on_died is not None:
                    try:
                        self.on_died(reason)
                    except Exception:
                        log.exception("on_died callback raised")

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stop_evt.clear()
            # Start the liveness clock NOW, not when the recorder opens. A
            # thread that hangs opening the WASAPI endpoint never sets it in
            # _run, and seconds_since_audio() would report 0.0 forever —
            # invisible to the watchdog.
            self._last_audio_at = time.monotonic()
            self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
            self._thread.start()
            self._running = True
            log.info("audio capture started: sr=%s ch=%s block=%s", self.samplerate, self.channels, self.blocksize)

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._stop_evt.set()
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None
            self._running = False
            log.info("audio capture stopped")

    def is_running(self) -> bool:
        return self._running

    def is_alive(self) -> bool:
        """True only if the capture THREAD is actually still running.

        `is_running()` reports intent (start() was called and stop() was not);
        this reports reality. They diverge exactly when something went wrong,
        which is the case the watchdog exists for.
        """
        t = self._thread
        return bool(t is not None and t.is_alive())

    def seconds_since_audio(self) -> float:
        """How long since the last delivered audio block.

        A thread can be alive and still deliver nothing (a wedged WASAPI
        endpoint does exactly this). Loopback capture yields blocks even in a
        silent room — digital silence is still samples — so a long gap here
        means the capture is broken, not that the room went quiet.
        """
        if self._last_audio_at <= 0:
            return 0.0
        return time.monotonic() - self._last_audio_at

    def died_reason(self) -> str:
        return self._died_reason
