"""CaptionBand — main Windows app entry.

Provides a system-tray-driven app that:
1. Captures system audio (Teams) via WASAPI loopback
2. Streams to selected provider (Azure / Groq / Google / Whisper local)
3. Renders captions on a transparent always-on-top overlay
4. Lets the user configure everything through a settings GUI

Tray UX:
- Single-click on icon → opens menu (Windows native behavior)
- Double-click on icon → opens Settings
- Right-click → full menu
- Icon color changes: gray = stopped, green = running, red = error
"""
from __future__ import annotations

# ---------------------------------------------------------------- COM apartment
# MUST run before anything imports `soundcard`, which calls
# CoInitializeEx(MULTITHREADED) at import time on the importing thread. When that
# thread is this one, the Qt GUI thread ends up in the MTA apartment and
# QWindowsContext::OleInitialize() fails with RPC_E_CHANGED_MODE on every single
# launch (measured: every one of the 154 launches in the operator's app.log).
# A Qt GUI thread on Windows is supposed to be STA; the state we had is the
# anomalous one.
#
# Claiming STA here is safe for soundcard: its _COMLibrary.__init__ explicitly
# catches RPC_E_CHANGED_MODE and carries on with com_loaded=False
# (site-packages/soundcard/mediafoundation.py:58-69). The catch is that it then
# relies on the process being MTA so ANY thread can use COM without
# initialising — which is why `AudioCapture._run` now claims MTA for itself.
# Measured: STA alone breaks capture with 0x800401F0 CO_E_NOTINITIALIZED;
# STA here + MTA on the capture thread captures normally.
def _init_sta() -> None:
    # Escape hatch: TLT_COM_APARTMENT=mta restores the old (broken) behaviour.
    # It exists so the apartment can be A/B tested against a real session
    # without rebuilding, and so a support call has something to try. Measured
    # difference on the Qt test suite: 1.4 s and 21 passed in STA, versus
    # 115 s and 6 failed in MTA.
    import os
    if os.environ.get("TLT_COM_APARTMENT", "").lower() == "mta":
        return
    try:
        import ctypes
        COINIT_APARTMENTTHREADED = 0x2
        ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    except Exception:
        pass        # non-Windows or ole32 missing: nothing to claim


_init_sta()

import argparse
import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QAction, QIcon, QPainter, QColor, QPixmap, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from config import AppConfig, KNOWN_LANGUAGES, load_config, save_config, log_path
from audio_capture import AudioCapture, find_device
from providers import (
    build_provider,
    provider_capabilities,
    ProviderStatus,
    ProviderUnavailable,
    TranslationEvent,
    TranslationProvider,
    PROVIDER_LABELS,
)
from providers.base import (
    CODE_DEVICE,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_FATAL,
    STATUS_OK,
)
from ordering import ReorderGate
from overlay_qt import CaptionOverlay
from settings_window import SettingsWindow
from transcript import TranscriptWriter
from updater import check_for_update_async, ReleaseInfo
from constants import APP_VERSION


log = logging.getLogger("captionband")


# ---------------------------------------------------------------------- logging


def _setup_logging() -> None:
    if getattr(_setup_logging, "_done", False):
        return  # already configured
    _setup_logging._done = True  # type: ignore[attr-defined]
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    # Handlers on the ROOT only. Attaching them to this logger as well made
    # every line appear twice in app.log (child → root propagation).
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    if sys.stderr is not None and not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)


# ---------------------------------------------------------------------- icons


def _make_icon(state: str = "stopped") -> QIcon:
    """Generate a state-colored icon. state = 'stopped' | 'running' | 'error'."""
    colors = {
        "stopped": QColor(96, 96, 96),
        "running": QColor(46, 160, 67),
        "degraded": QColor(191, 135, 0),  # working, but losing chunks
        "error": QColor(207, 34, 46),
        "config": QColor(0, 120, 215),
    }
    base = colors.get(state, colors["stopped"])
    pix = QPixmap(64, 64)
    pix.fill(base)
    painter = QPainter(pix)
    painter.setPen(QColor(255, 255, 255))
    font = painter.font()
    font.setBold(True)
    font.setPointSize(28)
    painter.setFont(font)
    painter.drawText(pix.rect(), 0x0084, "ES")
    painter.end()
    return QIcon(pix)


# ---------------------------------------------------------------------- overlays


def split_overlay_configs(cfg: AppConfig) -> tuple[AppConfig, Optional[AppConfig]]:
    """Configs for the primary band and, in two-box mode, the second box.

    Pure function so it can be tested without Qt. The CONTROLLER keeps the
    full config (the provider still translates to every target); only what
    each window displays is narrowed.
    """
    from dataclasses import replace
    targets = list(cfg.target_languages or [])
    if not cfg.overlay.split_languages or len(targets) < 2:
        return cfg, None
    keep_original = cfg.display_mode == "original_plus_translation"
    # One language per box: a band two lines tall (one line + its wrap) is
    # enough; the stacked layout's taller reservation would leave a large
    # empty bar above the text.
    first = replace(
        cfg, target_languages=[targets[0]],
        display_mode="original_plus_translation" if keep_original else "translations_only",
        overlay=replace(cfg.overlay, reserved_lines=3 if keep_original else 2),
    )
    second = replace(
        cfg, target_languages=targets[1:],
        display_mode="translations_only_multi" if len(targets) > 2 else "translations_only",
        overlay=replace(cfg.overlay, position=cfg.overlay.second_position or "top",
                        reserved_lines=2 * max(1, len(targets) - 1)),
    )
    return first, second


# ---------------------------------------------------------------------- controller


class TranslationController(QObject):
    """Wires audio capture + translation provider + overlay together.

    Includes a fallback chain: when the primary provider fails repeatedly
    (within FAILURE_WINDOW_S), the controller transparently switches to
    the next configured fallback and emits provider_changed so the tray
    can show a notification.
    """

    caption_ready = pyqtSignal(str, dict, str, bool, float, str)
    state_changed = pyqtSignal(str)  # 'stopped' | 'running' | 'error'
    provider_changed = pyqtSignal(str, str)  # (from_provider, to_provider)
    # (kind, code, message) — operator-facing health. Deliberately NOT routed
    # to the overlay: the overlay is the audience's projection screen, and
    # telemetry blinking in front of a room is noise for them and useless for
    # the operator, who is not looking at the projection.
    health_changed = pyqtSignal(str, str, str)
    # Internal, thread-hopping signals. Provider workers and the capture
    # thread emit these; because this QObject lives in the GUI thread, Qt
    # delivers them there via a queued connection.
    #
    # QTimer.singleShot() does NOT work for this: called from a worker it
    # creates a timer in the CALLING thread, which has no event loop, so the
    # callback silently never runs.
    _fallback_requested = pyqtSignal()
    _capture_lost = pyqtSignal(str)
    # (ok, new_cfg, provider, error) from the provider-swap worker thread.
    _swap_result = pyqtSignal(bool, object, object, str)
    # Public: the operator-initiated source-language change finished.
    # (ok, message). Emitted on the GUI thread.
    source_mode_changed = pyqtSignal(bool, str)

    # A pinned language that hears speech but recognizes nothing for this
    # long is almost always the WRONG language pinned (Portuguese in the
    # room, English on the pin). Say so instead of staying quietly blank —
    # measured: the operator read that silence as "the switch failed".
    PIN_HINT_S = 12.0

    # Minimum time between two provider switches. A dying provider does not
    # emit one error — an HTTP pool collapsing emits a burst of them, and each
    # one queues a fallback request. Without this floor the burst walks the
    # entire fallback list in milliseconds and lands on "no provider left"
    # while the second provider in the chain was never actually tried.
    FALLBACK_COOLDOWN_S = 20.0

    # How long the capture may deliver no audio at all before we call it dead.
    # Loopback capture yields blocks even in a silent room (digital silence is
    # still samples), so this only trips when capture is genuinely broken.
    CAPTURE_STALL_S = 5.0

    # After the capture dies, re-open it against whatever output device Windows
    # has NOW, with this backoff, before giving up. A projector being
    # (re)plugged or a Bluetooth headset dropping switches the default output
    # — routine at a live event — and the device is back within seconds.
    # Giving up leaves a CONSISTENT state (stopped), so "Iniciar" works again.
    CAPTURE_RETRY_DELAYS_S = (2.0, 5.0, 10.0)

    # Audio is arriving with real speech in it, but NO result has come back for
    # this long. Provider-agnostic: it does not care whether the socket
    # dropped, the session hit a server-side cap, or the SDK simply went quiet
    # without raising. That is the point — it catches failure modes nobody
    # anticipated, which over a two-hour event is most of them.
    #
    # Generous on purpose: a genuine pause between talks must not trip it, so
    # it only counts time during which the capture heard actual SPEECH.
    RESULT_STALL_S = 45.0
    # Below this RMS we treat a block as silence and do not count it towards
    # the stall. Matches the VAD threshold used by the chunk buffer.
    SPEECH_RMS = 0.010
    # How often the watchdog and the reorder gate are serviced.
    TICK_MS = 250

    def __init__(self, config: AppConfig, overlay: CaptionOverlay):
        super().__init__()
        self.config = config
        self.overlay = overlay
        self._capture: Optional[AudioCapture] = None
        self._translator: Optional[TranslationProvider] = None
        self._transcript: Optional[TranscriptWriter] = None
        self._running = False
        self._failure_timestamps: list[float] = []
        self._tried_fallbacks: set[str] = set()
        self._session_start: Optional[float] = None

        # Reorder gate — created per session, only for providers whose results
        # can overtake each other. See ordering.py.
        self._gate: Optional[ReorderGate] = None
        self._last_health: tuple[str, str, str] = (STATUS_OK, "", "")
        self._capture_died = False
        self._capture_retry = 0
        self._capture_recovering = False
        self._last_fallback_at: float = 0.0
        self._fallback_in_progress = False
        # Identifies the CURRENT provider's callbacks. A provider that was
        # replaced (fallback, language pin, stall recovery) may still have
        # workers in flight; their late results and — worse — their late FATAL
        # statuses must not reach the controller as if they came from the
        # provider that is now live.
        self._event_token: object = object()
        self._swap_in_progress = False
        # Last source-mode the operator asked for while a swap was running.
        # Requests used to be dropped on the floor in that window.
        self._pending_source_mode: Optional[AppConfig] = None
        self._pin_hinted = False
        # Bumped by stop(). A pending capture-reopen timer carries the value it
        # was scheduled with and does nothing if the session has moved on.
        self._capture_session = 0
        # Guards the two pieces of state that provider worker threads and the
        # GUI thread both read-modify-write: _failure_timestamps and
        # _last_health. Everything else crosses threads by signal.
        self._state_lock = threading.Lock()
        # Result-stall watchdog state.
        self._last_speech_at: float = 0.0
        self._last_result_at: float = 0.0
        self._stall_recoveries = 0
        self._last_stall_recovery_at: float = 0.0

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)

        self.caption_ready.connect(overlay.push_caption)
        self._fallback_requested.connect(self._attempt_fallback)
        self._capture_lost.connect(self._handle_capture_loss)
        self._swap_result.connect(self._on_swap_result)

    def record_failure(self) -> bool:
        """Record a provider failure. Returns True if threshold reached and
        fallback should be triggered."""
        import time as _t
        from constants import PROVIDER_FAILURE_THRESHOLD, PROVIDER_FAILURE_WINDOW_S

        now = _t.monotonic()
        cutoff = now - PROVIDER_FAILURE_WINDOW_S
        # Called from PROVIDER WORKER threads, while the GUI thread clears the
        # same list in _attempt_fallback. Unsynchronised, a failure could be
        # dropped and the fallback delayed at exactly the wrong moment.
        with self._state_lock:
            self._failure_timestamps = [t for t in self._failure_timestamps if t >= cutoff]
            self._failure_timestamps.append(now)
            return len(self._failure_timestamps) >= PROVIDER_FAILURE_THRESHOLD

    def trigger_fallback(self) -> bool:
        """Switch to the next provider in fallback_providers that is valid
        and hasn't been tried this session. Returns True on switch."""
        from dataclasses import replace
        candidates = [p for p in self.config.fallback_providers if p != self.config.provider]
        for cand in candidates:
            if cand in self._tried_fallbacks:
                continue
            test_cfg = replace(self.config, provider=cand)
            if not test_cfg.is_valid():
                continue
            old = self.config.provider
            log.warning("triggering provider fallback: %s -> %s", old, cand)
            self._tried_fallbacks.add(old)
            # Swap ONLY the provider. stop()/start() would close the
            # transcript and open a second file for the same talk, and flash
            # the tray stopped→running in the middle of a failure.
            if self._swap_provider(test_cfg):
                self.provider_changed.emit(old, cand)
                return True
            log.error("fallback provider %s also failed to start", cand)
            # Mark the FAILED candidate as tried too. Without this, only the
            # provider we switched AWAY from is recorded, so a candidate that
            # cannot start gets retried on every later fallback — burning the
            # cooldown on a door we know is shut.
            self._tried_fallbacks.add(cand)
        return False

    def is_running(self) -> bool:
        return self._running

    def update_config(self, config: AppConfig) -> None:
        # The tray owns what each overlay window displays (see
        # TrayApp._apply_overlays); the controller only keeps the full config.
        self.config = config

    def _push_audio(self, data: bytes) -> None:
        """Forward captured audio to whichever provider is active right now,
        and note whether the room is actually speaking.

        The speech timestamp is what makes the result-stall watchdog usable:
        without it, a quiet twenty minutes between sessions would look
        identical to a provider that stopped answering.
        """
        provider = self._translator
        if provider is not None:
            provider.push_audio(data)
        if self._capture_retry:
            # Audio flowing again is the proof a reopened capture works —
            # not the mere fact that start() returned.
            self._capture_retry = 0

        try:
            import numpy as _np
            arr = _np.frombuffer(data, dtype=_np.int16)
            if arr.size:
                rms = float(_np.sqrt(_np.mean((arr.astype(_np.float32) / 32768.0) ** 2)))
                if rms >= self.SPEECH_RMS:
                    import time as _t
                    self._last_speech_at = _t.monotonic()
        except Exception:
            pass          # never let metering break the audio path

    # ------------------------------------------------- source language

    def source_mode(self) -> Optional[str]:
        """Current source setting: a language code, or None for auto-detect."""
        if self.config.provider != "azure":
            return None
        if not self.config.azure_streaming_mode:
            return None
        return self.config.azure_streaming_language

    def set_source_mode(self, language: Optional[str]) -> bool:
        """Pin the source language, or pass None to go back to auto-detect.

        Three cases, cheapest first:
          - already pinned, different language  -> the provider swaps the
            recognizer in place (~200 ms, capture untouched);
          - changing between auto and pinned    -> the PROVIDER is rebuilt,
            but capture and the transcript file survive;
          - not Azure                           -> refused, nothing to do.

        Returns True if the mode is now what was asked for.
        """
        from dataclasses import replace

        if self.config.provider != "azure":
            return False

        want_streaming = language is not None
        if want_streaming and self.config.azure_streaming_mode \
                and language == self.config.azure_streaming_language:
            return True

        new_cfg = replace(
            self.config,
            azure_streaming_mode=want_streaming,
            azure_streaming_language=language or self.config.azure_streaming_language,
        )
        if not self._running:
            # Not live: remember the choice, it applies on the next start.
            self.config = new_cfg
            return True
        if self._swap_in_progress:
            # QUEUE it, do not drop it. A swap takes seconds of network I/O,
            # and an operator whose captions are in the wrong language jabs F9
            # repeatedly — every press during that window used to vanish with
            # only a log line, which is exactly the reported "não consigo
            # trocar o idioma". Keeping only the LAST request is deliberate:
            # four quick presses should land on the fourth language, not walk
            # through all of them.
            self._pending_source_mode = new_cfg
            log.info("source mode change queued: a swap is already running")
            return True
        # Never on the GUI thread: stopping and starting an Azure recognizer
        # is 1.3–1.6 s of blocking network I/O (measured in the log of a real
        # session), during which the tray menu and the overlay freeze — the
        # operator read that as "the app hung".
        self._swap_provider_async(new_cfg)
        return True

    def _swap_provider_async(self, new_cfg: AppConfig) -> None:
        """Same contract as _swap_provider, but the blocking part runs on a
        worker thread; _on_swap_result finishes the job on the GUI thread."""
        self._swap_in_progress = True
        old, self._translator = self._translator, None   # audio drops meanwhile
        on_event, on_status = self._bind_provider_callbacks()

        def worker() -> None:
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    log.exception("error stopping provider during swap")
            try:
                provider = build_provider(new_cfg, on_event=on_event, on_status=on_status)
                provider.start()
            except Exception as exc:
                log.exception("provider swap failed")
                self._swap_result.emit(False, new_cfg, None, str(exc))
                return
            self._swap_result.emit(True, new_cfg, provider, "")

        threading.Thread(target=worker, name="provider-swap", daemon=True).start()

    def _on_swap_result(self, ok: bool, new_cfg: object, provider: object, error: str) -> None:
        """GUI thread. Install the new provider (or report the failure)."""
        import time as _t
        self._swap_in_progress = False
        assert isinstance(new_cfg, AppConfig)
        if not ok or provider is None:
            msg = "Falha ao iniciar o provedor {}: {}".format(
                PROVIDER_LABELS.get(new_cfg.provider, new_cfg.provider), error)
            self._set_health(STATUS_FATAL, "", msg)
            self._pending_source_mode = None
            self.source_mode_changed.emit(False, msg)
            # The result-stall watchdog will retry the CURRENT config; nothing
            # else to do here.
            return
        # The swap ran on a worker while the GUI stayed live, so the operator
        # may have pressed "Parar" (or quit) in the meantime. Installing here
        # would attach a LIVE, already-started provider to a stopped
        # controller: stop() would never see it again, it would keep billing
        # and emitting, and the tray would announce "rodando" on a stopped app.
        if not self._running:
            log.info("swap finished after stop; discarding the new provider")
            try:
                provider.stop()
            except Exception:
                log.exception("error stopping provider orphaned by stop()")
            return
        self._install_provider(new_cfg, provider)
        self._last_result_at = _t.monotonic()
        self._pin_hinted = False
        # A successful swap clears whatever DEGRADED/FAILING got us here.
        self._set_health(STATUS_OK, "", "")
        log.info("provider swapped: streaming=%s language=%s",
                 new_cfg.azure_streaming_mode, new_cfg.azure_streaming_language)
        self.source_mode_changed.emit(True, "")
        # Apply whatever the operator asked for WHILE this swap was running.
        pending, self._pending_source_mode = self._pending_source_mode, None
        if pending is not None and self._running:
            log.info("applying the source mode queued during the swap")
            self._swap_provider_async(pending)

    def _install_provider(self, new_cfg: AppConfig, provider: TranslationProvider) -> None:
        caps = provider_capabilities(new_cfg.provider)
        needs_gate = bool(caps is None or not caps.ordered_by_protocol)
        if self._gate is not None:
            try:
                self._gate.flush()
            except Exception:
                log.exception("error flushing gate during swap")
        self._gate = ReorderGate(on_release=self._release_event) if needs_gate else None
        self.config = new_cfg
        self._translator = provider

    def _swap_provider(self, new_cfg: AppConfig) -> bool:
        """Rebuild ONLY the provider, keeping capture and transcript alive.

        Mid-event this is the difference between a ~1 s gap in the captions
        and a torn session: stop()/start() would also close the transcript
        file and start a second one for the same talk.
        """
        old = self._translator
        self._translator = None          # _push_audio drops audio meanwhile
        if old is not None:
            try:
                old.stop()
            except Exception:
                log.exception("error stopping provider during swap")

        on_event, on_status = self._bind_provider_callbacks()
        try:
            provider = build_provider(new_cfg, on_event=on_event, on_status=on_status)
            provider.start()
        except Exception as exc:
            log.exception("provider swap failed")
            self._set_health(STATUS_FATAL, "", "Falha ao iniciar o provedor {}: {}".format(
                PROVIDER_LABELS.get(new_cfg.provider, new_cfg.provider), exc))
            return False

        self._install_provider(new_cfg, provider)
        self._pin_hinted = False
        log.info("provider swapped: streaming=%s language=%s",
                 new_cfg.azure_streaming_mode, new_cfg.azure_streaming_language)
        return True

    def start(self) -> None:
        if self._running:
            return
        if not self.config.is_valid():
            raise RuntimeError("Configuração incompleta — abra Configurações.")

        device = find_device(self.config.audio.device_name)
        log.info(
            "starting translation: provider=%s device=%s",
            self.config.provider,
            getattr(device, "name", None),
        )

        # Decide ordering strategy BEFORE building the provider, from the
        # class's declared capabilities — never from its name.
        caps = provider_capabilities(self.config.provider)
        needs_gate = bool(caps is None or not caps.ordered_by_protocol)
        if needs_gate:
            self._gate = ReorderGate(on_release=self._release_event)
            log.info("reorder gate ENABLED for provider=%s", self.config.provider)
        else:
            self._gate = None
            log.info("reorder gate bypassed: %s orders results by protocol",
                     self.config.provider)

        self._capture_died = False
        self._capture_retry = 0
        self._capture_recovering = False
        on_event, on_status = self._bind_provider_callbacks()
        self._translator = build_provider(
            self.config, on_event=on_event, on_status=on_status,
        )
        self._translator.start()

        try:
            self._capture = self._build_capture(device)
            self._capture.start()
        except Exception:
            # Don't leave a provider session running (and billing) with no
            # audio behind a "failed to start" dialog.
            try:
                self._translator.stop()
            finally:
                self._translator = None
            raise

        # Start transcript writer
        transcript_failed = False
        try:
            self._transcript = TranscriptWriter(
                provider_name=self.config.provider,
                target_languages=self.config.target_languages,
            )
            self._transcript.start()
        except Exception:
            log.exception("failed to start transcript writer")
            self._transcript = None
            transcript_failed = True

        import time as _t
        self._session_start = _t.monotonic()
        # Start the stall clock now: a provider that never produces a single
        # result must trip the watchdog too, not only one that stops later.
        self._last_result_at = _t.monotonic()
        self._last_speech_at = 0.0
        self._stall_recoveries = 0
        self._last_stall_recovery_at = 0.0
        self._running = True
        self._set_health(STATUS_OK, "", "")
        if transcript_failed:
            # The operator would otherwise discover at the end of a two-hour
            # event that nothing was recorded.
            self._set_health(STATUS_DEGRADED, "",
                             "A transcrição NÃO está sendo gravada (veja os logs).")
        self._tick_timer.start()
        self.state_changed.emit("running")
        log.info("translation pipeline running")

    def _build_capture(self, device=None) -> AudioCapture:
        return AudioCapture(
            # Indirection, NOT self._translator.push_audio: binding the
            # provider's method here would freeze the capture to the provider
            # that existed at start(). Going through _push_audio lets the
            # provider be swapped underneath — for a source-language change
            # or a fallback — without tearing down capture and cutting the
            # transcript file in the middle of an event.
            on_audio=self._push_audio,
            device_index=device,
            samplerate=self.config.audio.samplerate,
            channels=self.config.audio.channels,
            on_died=self._on_capture_died,
        )

    def _bind_provider_callbacks(self):
        """Callbacks that only act while THEIR provider is the current one.

        Measured consequence without this: a chunk provider replaced by the
        stall watchdog kept delivering seq=57, 58… from its old workers into
        the FRESH reorder gate, which then treated the new provider's seq
        0…56 as stragglers and dropped them.
        """
        token = object()
        self._event_token = token

        def on_event(event, _t=token):
            if self._event_token is _t:
                self._on_translation_event(event)

        def on_status(status, _t=token):
            if self._event_token is _t:
                self._on_provider_status(status)
            else:
                log.info("ignoring status from replaced provider: %s/%s",
                         status.kind, status.code)

        return on_event, on_status

    def stop(self) -> None:
        if not self._running:
            return
        log.info("stopping translation pipeline")
        self._tick_timer.stop()
        # Anything still in flight from this session is now stale.
        self._event_token = object()
        # A manual stop is a fresh start for the fallback chain: otherwise a
        # provider tried an hour ago stays "burned" for the rest of the day.
        self._tried_fallbacks.clear()
        self._failure_timestamps.clear()
        # ...and so is the cooldown. Leaving it set meant a stop/start inside
        # FALLBACK_COOLDOWN_S silently swallowed the new session's first
        # fallback — precisely when the operator had just restarted to recover.
        self._last_fallback_at = 0.0
        # A swap worker may never report back (it is a bare daemon thread).
        # This flag was only ever cleared in _on_swap_result, so one lost
        # worker disabled language switching AND the stall watchdog for the
        # rest of the session, with no way out short of restarting the app.
        self._swap_in_progress = False
        self._pending_source_mode = None
        # Invalidate any pending capture-reopen timer from this session.
        self._capture_session += 1

        # Release anything still held by the reorder gate BEFORE tearing the
        # provider down, or the last utterance of the session is lost.
        if self._gate is not None:
            try:
                self._gate.flush()
                log.info("reorder gate: released=%s skipped=%s deadline=%.2fs",
                         self._gate.released, self._gate.skipped,
                         self._gate.deadline_s())
            except Exception:
                log.exception("error flushing reorder gate")
            self._gate = None

        # Record session duration before tearing down
        try:
            import usage_tracker
            import time as _t
            if self._session_start is not None:
                seconds = _t.monotonic() - self._session_start
                usage_tracker.add_seconds(self.config.provider, seconds)
            self._session_start = None
        except Exception:
            log.exception("failed to record usage")

        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                log.exception("error stopping capture")
            self._capture = None
        if self._translator is not None:
            try:
                self._translator.stop()
            except Exception:
                log.exception("error stopping translator")
            self._translator = None
        if self._transcript is not None:
            try:
                self._transcript.stop()
                log.info(
                    "transcript saved: %s + %s",
                    self._transcript.txt_path,
                    self._transcript.srt_path,
                )
            except Exception:
                log.exception("error closing transcript")
            self._transcript = None
        self._running = False
        self.state_changed.emit("stopped")

    def _on_translation_event(self, event: TranslationEvent) -> None:
        """Provider callback. Runs on a provider WORKER thread, not the GUI.

        Sequenced events go through the reorder gate; everything else is
        released immediately. Qt signals are queued across threads, so the
        overlay update still lands on the GUI thread either way.
        """
        # Any result at all — even a partial, even an empty slot-release —
        # proves the provider is still answering. Recorded before any
        # filtering, because the watchdog asks "is it alive?", not "was the
        # text useful?".
        import time as _tt
        self._last_result_at = _tt.monotonic()

        seq = getattr(event, "seq", None)
        # Feed the gate's adaptive deadline with a real observed latency.
        emitted_at = getattr(event, "audio_emitted_at_ms", None) or 0.0
        if self._gate is not None and seq is not None and emitted_at > 0:
            import time as _t
            latency_s = (_t.monotonic() * 1000 - emitted_at) / 1000.0
            self._gate.observe_latency(latency_s)

        if self._gate is not None and seq is not None:
            self._gate.submit(seq, event)
        else:
            self._release_event(event)

    def _release_event(self, event: object) -> None:
        """Emit one event to the overlay + transcript, in released order."""
        assert isinstance(event, TranslationEvent)
        # Empty-text events exist only to free a slot in the reorder gate
        # (a chunk that produced no speech). Nothing to display or persist.
        if not event.original_text and not event.translations:
            return

        log.info(
            "translation event: seq=%s lang=%s original=%r translations=%s final=%s",
            getattr(event, "seq", None),
            event.detected_language,
            event.original_text[:80],
            {k: v[:80] for k, v in event.translations.items()},
            event.is_final,
        )
        emitted_at = getattr(event, "audio_emitted_at_ms", None) or 0.0
        result_id = getattr(event, "result_id", "") or ""
        self.caption_ready.emit(
            event.original_text,
            event.translations,
            event.detected_language or "",
            bool(event.is_final),
            float(emitted_at),
            result_id,
        )
        # Persist to transcript
        if self._transcript is not None:
            try:
                self._transcript.append(
                    original_text=event.original_text,
                    translations=event.translations,
                    detected_language=event.detected_language,
                    is_final=event.is_final and bool(event.translations),
                )
            except Exception:
                log.exception("transcript append failed")

    # ------------------------------------------------------------- health

    def _set_health(self, kind: str, code: str, message: str) -> None:
        """Publish health, de-duplicated so a flapping provider does not spam.

        Reached from both the GUI thread and provider worker threads, so the
        read-modify-write of _last_health is serialised: without the lock two
        threads could both pass the de-dup check and double-emit, or one could
        overwrite the other's update and swallow it.
        """
        new = (kind, code, message)
        with self._state_lock:
            if new == self._last_health:
                return
            self._last_health = new
        self.health_changed.emit(kind, code, message)

    def _on_provider_status(self, status: ProviderStatus) -> None:
        """Provider health callback — THIS is what revives the fallback chain.

        Before this existed, record_failure() and trigger_fallback() had no
        caller anywhere in the app: the provider contract had no error channel,
        so a provider that failed simply went quiet and the controller waited
        forever for an event that was never coming.
        """
        self._set_health(status.kind, status.code, status.message)

        if not status.is_trouble:
            return

        log.warning("provider status: %s/%s %s",
                    status.kind, status.code, status.message)

        # A fatal status (bad key, exhausted quota) will not fix itself by
        # waiting, so it counts as the full failure threshold immediately.
        should_fallback = self.record_failure()
        if status.kind == STATUS_FATAL:
            should_fallback = True

        if should_fallback and self._running:
            # Marshal to the GUI thread: trigger_fallback() tears down and
            # rebuilds the whole pipeline, which touches Qt objects.
            self._fallback_requested.emit()

    def _attempt_fallback(self) -> None:
        """Switch providers at most once per cooldown window.

        Runs on the GUI thread. Several fallback requests can already be
        queued behind this one by the time it executes — that is the normal
        shape of a provider failing, not an anomaly — so the guard has to be
        here rather than at the emit site.
        """
        import time as _t

        if not self._running or self._fallback_in_progress:
            return
        now = _t.monotonic()
        since = now - self._last_fallback_at
        if self._last_fallback_at and since < self.FALLBACK_COOLDOWN_S:
            log.info("fallback suppressed: only %.1fs since the last switch "
                     "(cooldown %.0fs)", since, self.FALLBACK_COOLDOWN_S)
            return

        self._fallback_in_progress = True
        self._failure_timestamps.clear()
        try:
            switched = self.trigger_fallback()
        finally:
            self._fallback_in_progress = False

        if switched:
            self._last_fallback_at = _t.monotonic()
            return

        log.error("no usable fallback provider remains")
        # Stop for real. "error" with _running still True made "Iniciar" a
        # no-op — the operator's only way out was to quit the app.
        self.stop()
        self._set_health(
            STATUS_FATAL,
            "",
            "Nenhum provedor alternativo disponível. Veja Configurações.",
        )
        self.state_changed.emit("error")

    # ------------------------------------------------------------ watchdog

    def _on_capture_died(self, reason: str) -> None:
        """Called from the CAPTURE thread when its loop exits unexpectedly."""
        log.error("audio capture died: %s", reason)
        self._capture_died = True
        self._capture_lost.emit(reason)

    def _handle_capture_loss(self, reason: str) -> None:
        """GUI thread. Try to get the audio back before declaring defeat."""
        if not self._running:
            return
        if self._capture_retry >= len(self.CAPTURE_RETRY_DELAYS_S):
            log.error("audio capture could not be recovered: %s", reason)
            # Every retry died too. Leave a CONSISTENT state — stopped — so
            # the tray's "Iniciar" really starts again once the device is
            # back, instead of returning early on `_running`.
            self.stop()
            self._set_health(
                STATUS_FATAL, CODE_DEVICE,
                "Sem áudio do sistema: {} Confira o dispositivo de saída e "
                "inicie de novo.".format(reason))
            self.state_changed.emit("error")
            return
        delay = self.CAPTURE_RETRY_DELAYS_S[self._capture_retry]
        self._capture_retry += 1
        self._capture_recovering = True
        self._set_health(
            STATUS_FAILING, CODE_DEVICE,
            "{} Reconectando o áudio em {:.0f}s…".format(reason, delay))
        # Slot on the GUI thread, so a QTimer here does fire (unlike from a
        # worker — see the note on the internal signals above).
        # The timer carries the session number: stop() bumps it, so a pending
        # reopen from a dead session cannot tear down the capture of a session
        # the operator has since restarted. Without this, a stop/start inside
        # the 2/5/10 s window rebuilt the brand-new capture for no reason and
        # punched a ~1 s hole in the audio right after the restart.
        session = self._capture_session
        QTimer.singleShot(int(delay * 1000),
                          lambda: self._reopen_capture(session))

    def _reopen_capture(self, session: Optional[int] = None) -> None:
        """Rebuild the capture against the output device Windows has NOW.

        A pinned device that vanished resolves to None → default speaker,
        which is what the operator wants when the projector took the audio
        with it.
        """
        if not self._running:
            return
        if session is not None and session != self._capture_session:
            log.info("ignoring capture reopen from a previous session")
            return
        old, self._capture = self._capture, None
        if old is not None:
            try:
                old.stop()
            except Exception:
                log.exception("error stopping dead capture")
        self._capture_died = False
        try:
            self._capture = self._build_capture(find_device(self.config.audio.device_name))
            self._capture.start()
        except Exception as exc:
            log.exception("capture reopen failed")
            self._handle_capture_loss(
                "A captura de áudio falhou ao reabrir: {}.".format(exc))

    def _on_tick(self) -> None:
        """Serviced every TICK_MS on the GUI thread."""
        if not self._running:
            return
        # 1. Let the reorder gate give up on a chunk that will never arrive.
        if self._gate is not None:
            self._gate.tick()

        # 2. Watch the capture. A thread that exited, or one that is alive but
        #    has delivered nothing for CAPTURE_STALL_S, is equally fatal — and
        #    only this check distinguishes either from a quiet room.
        cap = self._capture
        if cap is None or self._capture_died:
            return
        if not cap.is_alive():
            self._capture_died = True
            self._handle_capture_loss(
                cap.died_reason() or "A captura de áudio parou inesperadamente."
            )
            return
        stalled = cap.seconds_since_audio()
        if stalled > self.CAPTURE_STALL_S:
            self._capture_died = True
            self._handle_capture_loss(
                "Sem áudio do sistema há {:.0f}s. O dispositivo de saída pode "
                "ter mudado.".format(stalled)
            )
            return

        if self._capture_recovering and self._capture_retry == 0:
            # _push_audio zeroed the retry counter: the reopened device is
            # actually delivering. Tell the operator the scare is over.
            self._capture_recovering = False
            log.info("audio capture recovered")
            self._set_health(STATUS_OK, "", "")

        # 3. The provider-agnostic safety net: speech is going in, nothing is
        #    coming out.
        self._check_result_stall()

    def _check_result_stall(self) -> None:
        """Recover from a provider that went quiet, whatever the reason.

        Measured before this existed: dropping the Azure session mid-event
        produced no cancellation, no error status and no further captions —
        the app stayed green and mute for the rest of the talk. Per-provider
        reconnection fixes the causes we know about; this catches the ones we
        do not, because it only looks at the observable symptom.
        """
        import time as _t

        if not self._last_speech_at or not self._last_result_at:
            return
        if self._swap_in_progress:
            return
        now = _t.monotonic()
        # Only count the stall while the room is (or was just) speaking.
        if now - self._last_speech_at > 5.0:
            return
        quiet_for = now - self._last_result_at
        if (quiet_for >= self.PIN_HINT_S and not self._pin_hinted
                and self.source_mode() is not None):
            self._pin_hinted = True
            self._set_health(
                STATUS_DEGRADED, "",
                "Fixado em {}, mas nada reconhecido há {:.0f}s com áudio "
                "entrando. O idioma falado é outro? Volte para Auto-detectar "
                "(F9).".format(KNOWN_LANGUAGES.get(self.source_mode(), self.source_mode()),
                               quiet_for))
        if quiet_for < self.RESULT_STALL_S:
            return
        # Don't thrash: one recovery attempt per stall window.
        if now - self._last_stall_recovery_at < self.RESULT_STALL_S:
            return

        self._last_stall_recovery_at = now
        self._stall_recoveries += 1
        log.warning(
            "result stall: speech for %.0fs with no result (recovery #%d)",
            quiet_for, self._stall_recoveries,
        )
        self._set_health(
            STATUS_FAILING, "",
            "Há {:.0f}s recebendo áudio sem legenda. Reiniciando o "
            "reconhecimento…".format(quiet_for),
        )
        # Rebuild the provider in place: capture and the transcript file
        # survive, so the event keeps its single recording.
        if self._swap_provider(self.config):
            self._last_result_at = _t.monotonic()
            # Say so. Health only ever returned to OK from start() and from
            # capture recovery, so a single stall left the tray amber and the
            # tooltip accusing for the rest of the event even though captions
            # were flowing again.
            self._set_health(STATUS_OK, "", "")
        else:
            # Rebuilding failed too — escalate to the fallback chain.
            self._fallback_requested.emit()


# ---------------------------------------------------------------------- tray


class TrayApp(QObject):
    # Minimum gap between two FAILING balloons (FATAL is never throttled).
    BALLOON_MIN_GAP_S = 30.0

    # Emitted from the update-check worker thread; queued onto the GUI thread.
    _update_available = pyqtSignal(str, str)  # (tag, url)
    # Emitted from the global-hotkey thread.
    _language_cycle_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._update_available.connect(self._notify_update)
        self._language_cycle_requested.connect(self._apply_pending_language)
        self._pending_language: Optional[str] = None
        self._latest_release_url = ""
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setApplicationName("CaptionBand")
        # A session that ends WITHOUT this line died abnormally.
        self.app.aboutToQuit.connect(lambda: log.info("app quit normally"))

        self._icons = {
            "stopped": _make_icon("stopped"),
            "running": _make_icon("running"),
            "degraded": _make_icon("degraded"),
            "error": _make_icon("error"),
            "config": _make_icon("config"),
        }
        self.app.setWindowIcon(self._icons["config"])

        self.config = load_config()
        first_cfg, _second = split_overlay_configs(self.config)
        self.overlay = CaptionOverlay(first_cfg)
        self.overlay.close_requested.connect(self._on_overlay_close_requested)
        # Second box for two-box bilingual mode; created lazily.
        self.overlay2: Optional[CaptionOverlay] = None
        self.controller = TranslationController(self.config, self.overlay)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.provider_changed.connect(self._on_provider_changed)
        self.controller.health_changed.connect(self._on_health_changed)
        self.controller.source_mode_changed.connect(self._on_source_mode_changed)
        self._pending_mode_label = ""
        self._health = (STATUS_OK, "", "")
        self._last_balloon_at = 0.0
        self.settings_window: Optional[SettingsWindow] = None
        self._presentation_mode_active = False
        self._saved_overlay_config = None
        self._saved_display_mode = ""

        self.tray = QSystemTrayIcon(self._icons["stopped"])
        self._update_tooltip()
        self.tray.activated.connect(self._on_tray_activated)
        self._build_menu()
        self._apply_overlays(self.config)
        self.tray.show()

        # Global hotkey for cycling source language. We register lazily at
        # start_translation() so the hotkey is only active while a session is
        # running (avoids stealing keys when the user isn't translating).
        self._hotkey_handle = None

        if not self.config.is_valid():
            QTimer.singleShot(500, self.open_settings)
        elif self.config.auto_start_translation:
            QTimer.singleShot(800, self.start_translation)
        else:
            QTimer.singleShot(1000, self._show_welcome_notification)

        # Async update check on launch (does not block UI)
        QTimer.singleShot(3000, self._check_updates)

    def _check_updates(self) -> None:
        # The check runs on a worker thread. Marshalling back with
        # QTimer.singleShot does not work from there (the timer would be
        # created in a thread with no event loop and never fire), so we hop
        # threads with a signal, which Qt queues onto the GUI thread.
        def on_result(info):
            if info is None:
                return
            self._update_available.emit(info.tag or "", info.url or "")
        check_for_update_async(on_result)

    def _notify_update(self, tag: str, url: str) -> None:
        self._latest_release_url = url
        # The old text told the operator to use a tray menu item called
        # 'Sobre'. No such item exists — 'Sobre' is a TAB inside Settings.
        self.tray.showMessage(
            "Atualização disponível ({})".format(tag),
            "Versão atual: {}. Abra Configurações → aba 'Sobre' para baixar.".format(
                APP_VERSION
            ),
            QSystemTrayIcon.MessageIcon.Information,
            8000,
        )

    # ------------------------------------------------------------------ menu

    def _build_menu(self) -> None:
        menu = QMenu()

        self.status_action = QAction("Status: parado", menu)
        self.status_action.setEnabled(False)
        font = self.status_action.font()
        font.setBold(True)
        self.status_action.setFont(font)
        menu.addAction(self.status_action)

        self.provider_action = QAction(menu)
        self.provider_action.setEnabled(False)
        menu.addAction(self.provider_action)

        menu.addSeparator()

        self.action_start = QAction("▶  Iniciar tradução", menu)
        self.action_start.triggered.connect(self.start_translation)
        menu.addAction(self.action_start)

        self.action_stop = QAction("■  Parar tradução", menu)
        self.action_stop.triggered.connect(self.stop_translation)
        self.action_stop.setEnabled(False)
        menu.addAction(self.action_stop)

        menu.addSeparator()

        # "Idioma de origem ▸" submenu — only meaningful when Azure streaming
        # mode is on. We rebuild the items every time the menu opens so that
        # the active language always shows the ✓.
        self.lang_menu = QMenu("🌐  Idioma de origem")
        menu.addMenu(self.lang_menu)
        self.lang_menu.aboutToShow.connect(self._rebuild_lang_menu)

        menu.addSeparator()

        self.action_overlay_show = QAction("Mostrar legenda", menu)
        self.action_overlay_show.triggered.connect(self._show_overlays)
        menu.addAction(self.action_overlay_show)

        self.action_overlay_hide = QAction("Esconder legenda", menu)
        self.action_overlay_hide.triggered.connect(self._hide_overlays)
        menu.addAction(self.action_overlay_hide)

        self.action_overlay_reset = QAction("Reposicionar legenda", menu)
        self.action_overlay_reset.triggered.connect(self._reposition_overlays)
        menu.addAction(self.action_overlay_reset)

        self.action_split = QAction("🗂  Bilíngue em duas caixas (2º idioma no topo)", menu)
        self.action_split.setCheckable(True)
        self.action_split.setChecked(bool(self.config.overlay.split_languages))
        self.action_split.triggered.connect(self.toggle_split_languages)
        menu.addAction(self.action_split)

        self.action_presentation = QAction("📺  Modo evento (legenda grande)", menu)
        self.action_presentation.setCheckable(True)
        self.action_presentation.triggered.connect(self.toggle_presentation_mode)
        menu.addAction(self.action_presentation)

        # Which monitor. Rebuilt on open: the projector may have been plugged
        # in after the app started.
        self.screen_menu = QMenu("🖥  Tela da legenda")
        menu.addMenu(self.screen_menu)
        self.screen_menu.aboutToShow.connect(self._rebuild_screen_menu)

        menu.addSeparator()

        self.action_preflight = QAction("🩺  Checagem pré-evento…", menu)
        self.action_preflight.triggered.connect(self.run_preflight)
        menu.addAction(self.action_preflight)

        menu.addSeparator()

        self.action_open_transcripts = QAction("Abrir pasta de transcrições", menu)
        self.action_open_transcripts.triggered.connect(self._open_transcripts_folder)
        menu.addAction(self.action_open_transcripts)

        menu.addSeparator()

        action_settings = QAction("⚙  Configurações…", menu)
        action_settings.triggered.connect(self.open_settings)
        menu.addAction(action_settings)

        action_logs = QAction("Abrir pasta de logs", menu)
        action_logs.triggered.connect(self._open_log_folder)
        menu.addAction(action_logs)

        menu.addSeparator()

        action_quit = QAction("✕  Sair", menu)
        action_quit.triggered.connect(self.quit)
        menu.addAction(action_quit)

        # Breadcrumbs: the process has died three times today with nothing in
        # the log but 'starting'. The last click before a native abort is the
        # only lead there is.
        for action in menu.actions():
            if action.text():
                action.triggered.connect(
                    lambda _c=False, t=action.text(): log.info("ui: menu '%s'", t))
        self.tray.setContextMenu(menu)
        self._refresh_provider_label()

    def _refresh_provider_label(self) -> None:
        label = PROVIDER_LABELS.get(self.config.provider, self.config.provider)
        self.provider_action.setText(f"Provedor: {label}")

    def _rebuild_lang_menu(self) -> None:
        """Populate 'Idioma de origem' — usable in BOTH modes, mid-event.

        Previously this submenu was dead unless Azure streaming was already
        on, which made it useless in exactly the situation it is for: the
        talk has started, auto-detect is struggling with one speaker, and the
        operator wants to pin the language NOW without opening Settings.
        """
        self.lang_menu.clear()

        if self.config.provider != "azure":
            info = QAction("(disponível apenas com o provedor Azure)", self.lang_menu)
            info.setEnabled(False)
            self.lang_menu.addAction(info)
            return

        active = self.controller.source_mode()   # None ⇒ auto-detect

        auto = QAction("{}Auto-detectar (PT / EN / ES)".format(
            "● " if active is None else "   "), self.lang_menu)
        auto.triggered.connect(lambda _checked=False: self.switch_source_language(None))
        self.lang_menu.addAction(auto)

        self.lang_menu.addSeparator()
        fixed_hdr = QAction("Fixar o idioma falado:", self.lang_menu)
        fixed_hdr.setEnabled(False)
        self.lang_menu.addAction(fixed_hdr)

        languages = self.config.azure_quick_languages or list(KNOWN_LANGUAGES.keys())
        for code in languages:
            label = KNOWN_LANGUAGES.get(code, code)
            mark = "● " if code == active else "   "
            action = QAction("{}{}".format(mark, label), self.lang_menu)
            action.triggered.connect(
                lambda _checked=False, c=code: self.switch_source_language(c)
            )
            self.lang_menu.addAction(action)

        self.lang_menu.addSeparator()
        note = QAction("Fixar = legenda palavra a palavra, mais rápida", self.lang_menu)
        note.setEnabled(False)
        self.lang_menu.addAction(note)
        if self.config.azure_switch_hotkey:
            hint = QAction("Atalho global: {} (alterna entre estes)".format(
                self.config.azure_switch_hotkey.upper()), self.lang_menu)
            hint.setEnabled(False)
            self.lang_menu.addAction(hint)

    def _rebuild_screen_menu(self) -> None:
        from PyQt6.QtGui import QGuiApplication
        self.screen_menu.clear()
        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        current = self.config.overlay.screen_name or ""
        for i, s in enumerate(screens):
            geo = s.geometry()
            name = s.name()
            is_current = (name == current) or (not current and s is primary)
            label = "{}{}: {} ({}×{}){}".format(
                "● " if is_current else "   ", i + 1, name,
                geo.width(), geo.height(), "  — principal" if s is primary else "")
            action = QAction(label, self.screen_menu)
            action.triggered.connect(
                lambda _checked=False, n=("" if s is primary else name): self.set_caption_screen(n))
            self.screen_menu.addAction(action)
        if len(screens) < 2:
            hint = QAction("Só um monitor detectado", self.screen_menu)
            hint.setEnabled(False)
            self.screen_menu.addAction(hint)

    # ------------------------------------------------------------ overlays

    def _overlays(self) -> list:
        return [o for o in (self.overlay, self.overlay2) if o is not None]

    def _show_overlays(self) -> None:
        self.overlay.show()
        if self.overlay2 is not None and split_overlay_configs(self.config)[1] is not None:
            self.overlay2.show()

    def _hide_overlays(self) -> None:
        for o in self._overlays():
            o.hide()

    def _reposition_overlays(self) -> None:
        for o in self._overlays():
            o.reposition()

    def _set_presentation(self, on: bool) -> None:
        for o in self._overlays():
            o.set_presentation(on)

    def _apply_overlays(self, cfg: AppConfig) -> None:
        """Give each window the slice of the config it displays."""
        first, second = split_overlay_configs(cfg)
        self.overlay.apply_config(first)
        if second is None:
            if self.overlay2 is not None:
                self.overlay2.hide()
            return
        if self.overlay2 is None:
            self.overlay2 = CaptionOverlay(second)
            self.overlay2.close_requested.connect(self._on_overlay_close_requested)
            self.controller.caption_ready.connect(self.overlay2.push_caption)
            self.overlay2.set_presentation(self._presentation_mode_active)
        else:
            self.overlay2.apply_config(second)
        if self.overlay.isVisible():
            self.overlay2.show()

    def toggle_split_languages(self, checked: bool) -> None:
        from dataclasses import replace
        if checked and len(self.config.target_languages) < 2:
            self.action_split.setChecked(False)
            self.tray.showMessage(
                "Duas caixas", "Precisa de 2 idiomas de saída (Configurações → Idiomas).",
                QSystemTrayIcon.MessageIcon.Information, 3000)
            return
        overlay = replace(self.config.overlay, split_languages=bool(checked))
        if self._saved_overlay_config is not None:
            self._saved_overlay_config = replace(
                self._saved_overlay_config, split_languages=bool(checked))
        self._apply_config(replace(self.config, overlay=overlay))
        try:
            save_config(self.config)
        except Exception:
            log.exception("could not persist split_languages")
        if checked:
            self._show_overlays()
            self.tray.showMessage(
                "Duas caixas",
                "{} no rodapé, {} no topo. Arraste cada caixa para onde quiser.".format(
                    self.config.target_languages[0].upper(),
                    " + ".join(t.upper() for t in self.config.target_languages[1:])),
                QSystemTrayIcon.MessageIcon.Information, 4000)

    def set_caption_screen(self, screen_name: str) -> None:
        """Move the caption to another monitor and remember it."""
        from dataclasses import replace
        overlay = replace(self.config.overlay, screen_name=screen_name)
        # Keep the pre-preset baseline in step, or toggling the event preset
        # off would drag the caption back to the old monitor.
        if self._saved_overlay_config is not None:
            self._saved_overlay_config = replace(
                self._saved_overlay_config, screen_name=screen_name)
        self._apply_config(replace(self.config, overlay=overlay))
        try:
            save_config(self.config)
        except Exception:
            log.exception("could not persist caption screen")
        self._show_overlays()

    def switch_source_language(self, language: Optional[str]) -> None:
        """Tray submenu and global hotkey. `None` means auto-detect."""
        if self.config.provider != "azure":
            return
        if language == self.controller.source_mode():
            return

        ok = self.controller.set_source_mode(language)
        if not ok:
            self.tray.showMessage(
                "Idioma de origem",
                "Aguarde — a troca anterior ainda está em andamento.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )
            return

        # Immediate feedback; the swap itself finishes on a worker thread and
        # _on_source_mode_changed announces the result.
        self._pending_mode_label = (
            "Auto-detectar (PT / EN / ES)" if language is None
            else "Fixado em {}".format(KNOWN_LANGUAGES.get(language, language)))
        if self.controller.is_running():
            self.status_action.setText("Status: trocando idioma…")
        else:
            self._on_source_mode_changed(True, "")

    def _on_source_mode_changed(self, ok: bool, message: str) -> None:
        # The controller owns the authoritative config after a swap.
        self.config = self.controller.config
        self._apply_overlays(self.config)
        self._update_tooltip()
        if not ok:
            self.tray.showMessage(
                "Idioma de origem",
                message or "Não foi possível trocar agora. Veja os logs.",
                QSystemTrayIcon.MessageIcon.Warning, 5000,
            )
            return
        if self.controller.is_running():
            self.status_action.setText("Status: ▶ rodando")
        self.tray.showMessage(
            "Idioma de origem", self._pending_mode_label,
            QSystemTrayIcon.MessageIcon.Information, 1800,
        )

    def _language_cycle(self) -> list:
        """The order the hotkey walks: auto first, then each quick language."""
        return [None] + list(self.config.azure_quick_languages or [])

    def cycle_source_language(self) -> None:
        """Hotkey handler — steps through auto → pt → en → es → auto."""
        cycle = self._language_cycle()
        if len(cycle) < 2:
            return
        current = self.controller.source_mode()
        try:
            idx = cycle.index(current)
        except ValueError:
            idx = -1
        nxt = cycle[(idx + 1) % len(cycle)]
        # Publish the target BEFORE signalling: the GUI thread may run the
        # slot as soon as the signal is emitted, and would otherwise read the
        # previous value.
        self._pending_language = nxt
        # The hotkey fires on the `keyboard` library's own thread. Hop to the
        # GUI thread with a signal: QTimer.singleShot from a non-GUI thread
        # creates its timer in a thread with no event loop and never fires.
        self._language_cycle_requested.emit()

    def _apply_pending_language(self) -> None:
        """GUI-thread half of the hotkey handler."""
        self.switch_source_language(self._pending_language)

    def _register_hotkey(self) -> None:
        """Register the global cycle-language hotkey if configured."""
        self._unregister_hotkey()
        hotkey = (self.config.azure_switch_hotkey or "").strip()
        if not hotkey:
            return
        # Registered for Azure in EITHER mode. Gating it on streaming_mode
        # made the shortcut unavailable while auto-detecting, which is the
        # state the operator is most likely to want out of mid-talk.
        if self.config.provider != "azure":
            return
        try:
            import keyboard  # type: ignore
            self._hotkey_handle = keyboard.add_hotkey(hotkey, self.cycle_source_language)
            log.info("registered global hotkey: %s (cycle source language)", hotkey)
        except Exception:
            log.exception("failed to register global hotkey %r", hotkey)
            self._hotkey_handle = None

    def _unregister_hotkey(self) -> None:
        if self._hotkey_handle is None:
            return
        try:
            import keyboard  # type: ignore
            keyboard.remove_hotkey(self._hotkey_handle)
        except Exception:
            log.exception("failed to remove global hotkey")
        self._hotkey_handle = None

    def _on_tray_activated(self, reason) -> None:
        # Right-click already opens the context menu natively on Windows.
        # We bind:
        #   Trigger (left-click)  → open the context menu programmatically
        #   DoubleClick           → open Settings
        # We never silently hide the overlay on click — it's confusing.
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            menu = self.tray.contextMenu()
            if menu is not None:
                from PyQt6.QtGui import QCursor
                menu.popup(QCursor.pos())
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings()

    # ------------------------------------------------------------------ state

    def _on_provider_changed(self, old: str, new: str) -> None:
        from providers import PROVIDER_LABELS

        old_label = PROVIDER_LABELS.get(old, old)
        new_label = PROVIDER_LABELS.get(new, new)
        self.config = self.controller.config
        self._refresh_provider_label()
        # The F9 hotkey is Azure-only; after a fallback away from Azure it
        # would keep swallowing the key for nothing.
        self._register_hotkey()
        self.tray.showMessage(
            "Provedor trocado",
            f"O provedor {old_label} falhou repetidamente. Trocou para {new_label}.",
            QSystemTrayIcon.MessageIcon.Warning,
            6000,
        )

    def _on_state_changed(self, state: str) -> None:
        if state == "running":
            self.tray.setIcon(self._icons["running"])
            self.status_action.setText("Status: ▶ rodando")
            self.action_start.setEnabled(False)
            self.action_stop.setEnabled(True)
        elif state == "error":
            self.tray.setIcon(self._icons["error"])
            self.status_action.setText("Status: ⚠ erro (ver logs)")
            self.action_start.setEnabled(True)
            self.action_stop.setEnabled(False)
        else:
            self.tray.setIcon(self._icons["stopped"])
            self.status_action.setText("Status: parado")
            self.action_start.setEnabled(True)
            self.action_stop.setEnabled(False)
        self._update_tooltip()

    def _on_health_changed(self, kind: str, code: str, message: str) -> None:
        """Operator-facing health. Never drawn on the overlay.

        The overlay is the audience's projection screen; a status dot blinking
        there during a talk is a distraction for the room and useless for the
        operator, who is watching this tray icon and not the projection.
        """
        self._health = (kind, code, message)
        if kind == STATUS_OK:
            self.status_action.setText("Status: ▶ rodando")
            if self.controller.is_running():
                self.tray.setIcon(self._icons["running"])
        elif kind == STATUS_DEGRADED:
            self.status_action.setText("Status: ▶ rodando (instável)")
            self.tray.setIcon(self._icons["degraded"])
        else:
            self.status_action.setText("Status: ⚠ {}".format(message[:60]))
            self.tray.setIcon(self._icons["error"])

        self._update_tooltip()

        # Only interrupt the operator for things they must act on. A rate
        # limit clears itself; a bad key or a dead audio device does not.
        # A reconnect countdown is a NEW message every few seconds; one
        # balloon per window is plenty — the icon and status line carry the
        # rest. FATAL always shows: it is the one that needs a human.
        if kind in (STATUS_FAILING, STATUS_FATAL) and message:
            import time as _t
            now = _t.monotonic()
            if kind == STATUS_FATAL or now - self._last_balloon_at >= self.BALLOON_MIN_GAP_S:
                self._last_balloon_at = now
                self.tray.showMessage(
                    "Tradução com problema",
                    message,
                    QSystemTrayIcon.MessageIcon.Warning,
                    9000,
                )

    def _update_tooltip(self) -> None:
        provider = PROVIDER_LABELS.get(self.config.provider, self.config.provider)
        state = "rodando" if self.controller.is_running() else "parado"
        kind, _code, message = self._health
        suffix = ""
        if self.controller.is_running() and kind != STATUS_OK and message:
            # Windows cuts tray tooltips at ~128 chars; a truncated sentence
            # beats a truncated word salad.
            suffix = "\n{}".format(message[:90])
        self.tray.setToolTip(
            "CaptionBand — {} ({}){}".format(provider, state, suffix)
        )

    def _show_welcome_notification(self) -> None:
        self.tray.showMessage(
            "CaptionBand",
            "Clique no ícone (canto inferior direito da tela) para abrir o menu. "
            "Duplo-clique abre Configurações.",
            QSystemTrayIcon.MessageIcon.Information,
            7000,
        )

    # ------------------------------------------------------------------ actions

    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.isVisible():
            self.settings_window.raise_()
            self.settings_window.activateWindow()
            return
        self.settings_window = SettingsWindow(self.config)
        self.settings_window.config_saved.connect(self._on_config_saved)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def _on_config_saved(self, cfg: AppConfig) -> None:
        from dataclasses import replace
        was_running = self.controller.is_running()
        # Font, colours, position, display mode: the overlay applies these
        # live. Restarting the pipeline for them cut the captions for a few
        # seconds and split the transcript into two files mid-talk.
        appearance_only = replace(
            cfg, overlay=self.config.overlay, display_mode=self.config.display_mode,
        ) == self.config
        restart = was_running and not appearance_only
        if restart:
            self.stop_translation()
        self._apply_config(cfg)
        # A saved config replaces the preset baseline too, otherwise toggling
        # the event preset off would restore an overlay the operator has
        # since edited away.
        self._presentation_mode_active = False
        self.action_presentation.setChecked(False)
        self._saved_overlay_config = None
        self._saved_display_mode = ""
        self._set_presentation(False)
        if restart:
            self.start_translation()
        self.tray.showMessage(
            "CaptionBand",
            "Configurações salvas.",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )

    def start_translation(self) -> None:
        try:
            self.controller.start()
        except ProviderUnavailable as exc:
            # Missing/broken dependency: the message already tells the
            # operator which package to install. Do not bury it in "failed
            # to start" boilerplate.
            log.error("provider unavailable: %s", exc)
            self._on_state_changed("error")
            QMessageBox.critical(None, "Provedor indisponível", str(exc))
            return
        except Exception as exc:
            log.exception("failed to start translation")
            self._on_state_changed("error")
            QMessageBox.critical(None, "Erro ao iniciar", f"Não foi possível iniciar:\n\n{exc}")
            return
        self._register_hotkey()
        self._show_overlays()
        self.tray.showMessage(
            "Tradução iniciada",
            "Capturando áudio do sistema. Toque um vídeo no Teams para testar.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def stop_translation(self) -> None:
        self._unregister_hotkey()
        self.controller.stop()
        self.tray.showMessage(
            "Tradução parada",
            "Áudio do sistema não está mais sendo capturado.",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def run_preflight(self) -> None:
        """Walk the whole chain and report in plain language.

        Runs on the GUI thread with a busy cursor: it takes a few seconds and
        the operator is doing nothing else while waiting for the verdict.
        Stopping an active session first, because the audio probe needs the
        capture device the session is holding.
        """
        import preflight
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtWidgets import QApplication as _QApp

        was_running = self.controller.is_running()
        if was_running:
            self.controller.stop()

        self.tray.showMessage(
            "Checagem pré-evento",
            "Testando provedor, credenciais e áudio. Toque um som para "
            "validar a captura.",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )
        _QApp.setOverrideCursor(_Qt.CursorShape.WaitCursor)
        try:
            steps = preflight.run(self.config)
            ready, report = preflight.summarize(steps)
        except Exception as exc:
            log.exception("preflight failed")
            ready, report = False, "A checagem falhou: {}".format(exc)
        finally:
            _QApp.restoreOverrideCursor()

        box = QMessageBox()
        box.setWindowTitle("Checagem pré-evento")
        box.setIcon(
            QMessageBox.Icon.Information if ready else QMessageBox.Icon.Warning
        )
        box.setText("Pronto para o evento." if ready
                    else "Ainda NÃO está pronto.")
        box.setDetailedText(report)
        box.setInformativeText(report.rsplit("\n\n", 1)[-1])
        box.exec()

        if was_running:
            self.start_translation()

    def _open_log_folder(self) -> None:
        import os
        import subprocess

        folder = str(log_path().parent)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            subprocess.Popen(["explorer", folder])

    def _open_transcripts_folder(self) -> None:
        import os
        import subprocess
        from config import app_data_dir

        folder = str(app_data_dir() / "transcripts")
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            subprocess.Popen(["explorer", folder])

    def _apply_config(self, cfg: AppConfig) -> None:
        """The ONE path by which a config change reaches every holder.

        Previously some call sites mutated `self.config.overlay` in place
        while others used dataclasses.replace() (which copies). After any
        replace(), the tray and the controller held DIFFERENT AppConfig
        objects, so an in-place overlay edit reached only one of them.
        """
        self.config = cfg
        self.controller.update_config(cfg)
        self._apply_overlays(cfg)
        # Settings and the tray toggle are two views of the same flag.
        self.action_split.setChecked(bool(cfg.overlay.split_languages))
        self._refresh_provider_label()
        self._update_tooltip()

    def toggle_presentation_mode(self) -> None:
        """Toggle the live-event preset: big, stable, bottom-of-screen.

        Tuned for the actual use — a projected caption with ONE target
        language that an audience reads from the back of a room:
        - large type, wide band, high contrast;
        - a fixed-height band so the caption never jumps mid-sentence;
        - the newest line anchored, older text pushing upward, so the eye
          can rest on one spot;
        - bottom placement, which does not collide with slide titles.
        """
        from dataclasses import replace

        if not self._presentation_mode_active:
            self._saved_overlay_config = self.config.overlay
            self._saved_display_mode = self.config.display_mode
            targets = self.config.target_languages
            bilingual = len(targets) >= 2
            preset = replace(
                self.config.overlay,
                primary_font_size=54,
                secondary_font_size=28,
                # Bilingual: only the CURRENT utterance, one line per
                # language — two languages already fill the band, and a
                # projected caption is read once, not scrolled back through.
                max_history=0 if bilingual else 1,
                # Room for each language to wrap onto a second line without
                # resizing the band.
                reserved_lines=(2 * len(targets)) if bilingual else 3,
                stable_height=True,
                anchor_newest=True,
                width_ratio=0.92,
                position="bottom",
                background_opacity=0.92,
                padding=28,
            )
            # Two output languages on a projector ⇒ the bilingual layout,
            # without the spoken language: the room does not need Portuguese
            # captions of Portuguese speech, and the third line made both
            # translations smaller.
            display_mode = ("translations_only_multi" if bilingual
                            else self.config.display_mode)
            self._apply_config(replace(self.config, overlay=preset,
                                       display_mode=display_mode))
            self._set_presentation(True)
            self._presentation_mode_active = True
            self.action_presentation.setChecked(True)
            self.tray.showMessage(
                "Modo evento",
                ("Legenda bilíngue ({}) grande e fixa no rodapé, sem o idioma "
                 "falado. Clique no menu para voltar.".format(
                     " + ".join(t.upper() for t in targets))
                 if bilingual else
                 "Legenda grande e fixa no rodapé. Clique no menu para voltar."),
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
        else:
            if self._saved_overlay_config is not None:
                self._apply_config(replace(
                    self.config, overlay=self._saved_overlay_config,
                    display_mode=self._saved_display_mode or self.config.display_mode,
                ))
            self._set_presentation(False)
            self._presentation_mode_active = False
            self.action_presentation.setChecked(False)
            self.tray.showMessage(
                "Modo evento desligado",
                "Legenda voltou ao layout normal.",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )

    def quit(self) -> None:
        """Tear down in a defined order instead of leaving it to the GC.

        Previously this stopped translation, hid the tray icon and called
        app.quit(); the two overlays and the Settings window were still alive
        and got destroyed by Python's collector in an unspecified order, after
        QApplication. Destroying Qt widgets after the application object is a
        classic source of the native "it just closed" abort at shutdown — and
        1 of the 154 launches in the log ended with a clean "app quit
        normally".
        """
        self.stop_translation()
        win = getattr(self, "settings_window", None)
        if win is not None:
            try:
                win.close()
            except Exception:
                log.exception("error closing the settings window at quit")
        for ov in self._overlays():
            try:
                ov.close()
            except Exception:
                log.exception("error closing an overlay at quit")
        self.tray.hide()
        self.app.quit()

    def _on_overlay_close_requested(self) -> None:
        """× on a caption band: HIDE the caption. Quitting lives in the tray.

        It used to ask "close the whole app?" — a stray click on the
        projected band was one 'Sim' away from killing the session in front
        of the audience. Translation keeps running; the transcript keeps
        recording; 'Mostrar legenda' brings the band back.
        """
        self._hide_overlays()
        self.tray.showMessage(
            "Legenda escondida",
            "A tradução continua rodando. Bandeja → 'Mostrar legenda' para "
            "voltar; para sair do app use Bandeja → 'Sair'.",
            QSystemTrayIcon.MessageIcon.Information, 4000,
        )

    def run(self) -> int:
        return self.app.exec()


# ---------------------------------------------------------------------- entry


# Held for the process lifetime: Windows releases a named mutex when the last
# handle to it closes, so dropping this would defeat the whole guard.
_INSTANCE_MUTEX = None


def _claim_single_instance() -> bool:
    """True if this process is the only one. Windows-only; True elsewhere."""
    global _INSTANCE_MUTEX
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Local\\CaptionBand.SingleInstance")
        if not handle:
            return True                     # cannot tell: do not block startup
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        _INSTANCE_MUTEX = handle
        return True
    except Exception:
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", action="store_true", help="Abrir apenas a janela de configurações")
    args = parser.parse_args()

    _setup_logging()
    log.info("starting CaptionBand")

    # An unhandled Python exception inside a Qt slot makes PyQt6 abort the
    # whole process. In the frozen exe there is no console, so the operator
    # saw "it just closed" and app.log ended at 'starting'. Log it — with the
    # traceback — and keep the event loop alive; a broken menu item is
    # recoverable, a dead app mid-event is not.
    def _log_uncaught(exc_type, exc, tb):
        log.critical("uncaught exception in GUI thread",
                     exc_info=(exc_type, exc, tb))
    sys.excepthook = _log_uncaught

    # sys.excepthook only covers the GUI thread. This app runs five workers
    # (capture, provider-swap, update-checker, the Speech SDK's own threads, and
    # the global-hotkey listener); an exception escaping any of them printed to
    # a stderr that does not exist in the windowed exe and vanished without a
    # trace. That blind spot is why several "it just closed" reports had a log
    # that ended mid-sentence with nothing to go on.
    def _log_uncaught_thread(args):
        log.critical("uncaught exception in thread %s",
                     getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = _log_uncaught_thread

    # Qt's own diagnostics. A qFatal() ends the process with abort() and its
    # message goes to a debugger nobody is attached to; routing every Qt
    # message through logging puts that text in app.log right before the
    # 'Fatal Python error: Aborted' that crash.log records.
    try:
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType

        _qt_levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR, QtMsgType.QtFatalMsg: logging.CRITICAL,
        }

        def _qt_message(mode, context, message):
            logging.getLogger("qt").log(
                _qt_levels.get(mode, logging.WARNING), "%s (%s:%s)",
                message, getattr(context, "file", "") or "", getattr(context, "line", "") or "")
            if mode == QtMsgType.QtFatalMsg:
                for h in logging.getLogger().handlers:
                    try:
                        h.flush()
                    except Exception:
                        pass
        qInstallMessageHandler(_qt_message)
    except Exception:
        log.exception("qt message handler not installed")

    # A NATIVE crash (access violation inside the Speech SDK or Qt) never
    # reaches Python logging: the process is simply gone and app.log ends
    # mid-sentence. faulthandler writes the Python stacks of every thread
    # to crash.log at that moment, so the next 'it just closed' has a trace.
    try:
        import faulthandler
        crash_file = open(log_path().with_name("crash.log"), "a", encoding="utf-8")
        crash_file.write("\n=== process start {} pid={}\n".format(
            __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            __import__("os").getpid()))
        # READ THIS BEFORE CHASING ANYTHING BELOW IT.
        # faulthandler's Windows hook reports FIRST-CHANCE exceptions — ones
        # that something further up then handles. Most entries here are
        # therefore NOT crashes.
        #   0x8001010d (RPC_E_CANTCALLOUT_ININPUTSYNCCALL): COM told a thread
        #     it could not make an outgoing call at that moment. Handled; the
        #     process keeps running. Verified twice on the installed build —
        #     one launch that logged it went on to record "app quit normally",
        #     another drove a full F9 language cycle afterwards and was still
        #     alive at the end. Chasing this as a crash already cost one
        #     debugging session.
        #   "Fatal Python error: Aborted"               -> THIS is a death.
        #   "Windows fatal exception: access violation" -> so is this.
        # The authority on whether a launch died is app.log: a session that
        # ends WITHOUT "app quit normally" is the one worth investigating.
        crash_file.write(
            "    note: 0x8001010d is a handled first-chance COM exception, "
            "NOT a crash.\n"
            "    Real deaths say 'Fatal Python error' or 'access violation'.\n")
        crash_file.flush()
        faulthandler.enable(file=crash_file, all_threads=True)
    except Exception:
        log.exception("faulthandler not enabled")

    if args.settings:
        app = QApplication(sys.argv)
        app.setApplicationName("CaptionBand")
        cfg = load_config()
        win = SettingsWindow(cfg)
        win.show()
        win.raise_()
        win.activateWindow()
        return app.exec()

    # One copy at a time. Two instances both open WASAPI loopback on the same
    # endpoint, both register the F9 hotkey (the second one wins, so the tray
    # the operator is looking at stops responding to it) and both write the
    # same transcript directory. Two tray icons is also just confusing.
    if not _claim_single_instance():
        log.warning("another instance is already running; exiting")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "O CaptionBand já está aberto.\n\n"
                "Procure o ícone na bandeja, ao lado do relógio.",
                "CaptionBand", 0x40)     # MB_ICONINFORMATION
        except Exception:
            pass
        return 0

    tray_app = TrayApp()
    return tray_app.run()


if __name__ == "__main__":
    sys.exit(main())
