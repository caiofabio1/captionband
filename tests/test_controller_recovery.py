"""The controller's failure paths must always leave the operator a way back.

Found by review, confirmed by two independent model families and by reading:
after a capture loss (headphones unplugged, projector re-plugged, Windows
switching the default output) or an exhausted fallback chain, the controller
emitted state "error" but left `_running=True`. The tray then showed "Iniciar"
enabled — and clicking it did nothing, because `start()` returns early while
`_running`. The only way out of a two-hour event's most likely failure was
"Sair".

These tests drive the REAL TranslationController with fakes only at the
edges (provider, capture, transcript), the same way rehearsal.py does.
"""
from __future__ import annotations

import time

import pytest
from PyQt6.QtWidgets import QApplication

import translator as T
from config import AppConfig
from providers.base import (
    CODE_AUTH,
    STATUS_FATAL,
    STATUS_OK,
    ProviderCapabilities,
    ProviderStatus,
    TranslationEvent,
    TranslationProvider,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


class FakeProvider(TranslationProvider):
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False, translates=True, streaming=False, label="fake",
    )
    instances: list["FakeProvider"] = []

    def __init__(self, on_event, on_status):
        self.on_event = on_event
        self.on_status = on_status
        self.running = False
        FakeProvider.instances.append(self)

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def push_audio(self, audio_bytes: bytes) -> None:
        pass

    @property
    def is_running(self) -> bool:
        return self.running


class FakeCapture:
    instances: list["FakeCapture"] = []
    # How many of the NEXT constructions die on start() (device not found).
    fail_next = 0

    def __init__(self, on_audio, device_index=None, samplerate=16000,
                 channels=1, on_died=None):
        self.on_audio = on_audio
        self.on_died = on_died
        self.alive = False
        self.died = ""
        FakeCapture.instances.append(self)

    def start(self) -> None:
        if FakeCapture.fail_next > 0:
            FakeCapture.fail_next -= 1
            self.died = "Nenhum dispositivo de saída de áudio encontrado."
            if self.on_died:
                self.on_died(self.died)
            return
        self.alive = True

    def stop(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    def seconds_since_audio(self) -> float:
        return 0.0

    def died_reason(self) -> str:
        return self.died


class FakeTranscript:
    started = 0

    def __init__(self, provider_name, target_languages):
        self.txt_path = self.srt_path = None

    def start(self) -> None:
        FakeTranscript.started += 1

    def stop(self) -> None:
        pass

    def append(self, **kw) -> None:
        pass


class StubOverlay:
    def __init__(self):
        self.captions: list[str] = []

    def push_caption(self, original, *a, **kw):
        self.captions.append(original)

    def apply_config(self, cfg):
        pass


@pytest.fixture
def ctrl(qapp, monkeypatch):
    FakeProvider.instances.clear()
    FakeCapture.instances.clear()
    FakeCapture.fail_next = 0
    FakeTranscript.started = 0

    monkeypatch.setattr(
        T, "build_provider",
        lambda cfg, on_event, on_status: FakeProvider(on_event, on_status))
    monkeypatch.setattr(T, "provider_capabilities",
                        lambda name: FakeProvider.CAPABILITIES)
    monkeypatch.setattr(T, "find_device", lambda name: None)
    monkeypatch.setattr(T, "AudioCapture", FakeCapture)
    monkeypatch.setattr(T, "TranscriptWriter", FakeTranscript)
    import usage_tracker
    monkeypatch.setattr(usage_tracker, "add_seconds", lambda *a, **k: None)
    # Fast retries: the backoff schedule is the same shape, just in ms.
    monkeypatch.setattr(T.TranslationController, "CAPTURE_RETRY_DELAYS_S",
                        (0.01, 0.01, 0.01), raising=False)

    cfg = AppConfig(provider="groq", groq_api_key="k",
                    azure_speech_key="k", azure_speech_region="r",
                    fallback_providers=[])
    overlay = StubOverlay()
    c = T.TranslationController(cfg, overlay)
    c.overlay_stub = overlay
    c.states: list[str] = []
    c.state_changed.connect(c.states.append)
    yield c
    if c.is_running():
        c.stop()


def _kill_capture(c: T.TranslationController) -> None:
    cap = FakeCapture.instances[-1]
    cap.alive = False
    cap.died = "A captura de áudio parou: device gone"
    cap.on_died(cap.died)


class TestCaptureLoss:
    def test_reacquires_device_and_keeps_running(self, ctrl, qapp):
        ctrl.start()
        assert len(FakeCapture.instances) == 1
        _kill_capture(ctrl)
        pump(qapp, 0.2)

        assert ctrl.is_running()
        assert len(FakeCapture.instances) == 2, "capture was never re-created"
        assert FakeCapture.instances[-1].is_alive()
        # Audio flowing again is what clears the warning — not the mere
        # attempt to reopen the device.
        ctrl._push_audio(b"\x10\x10" * 800)
        # The all-clear is published by the watchdog tick (TICK_MS = 250).
        pump(qapp, ctrl.TICK_MS / 1000 * 2)
        assert ctrl._last_health[0] == STATUS_OK
        assert "error" not in ctrl.states

    def test_exhausted_retries_leave_a_restartable_state(self, ctrl, qapp):
        ctrl.start()
        FakeCapture.fail_next = 99
        _kill_capture(ctrl)
        pump(qapp, 0.3)

        # The whole point: NOT "error while still running".
        assert not ctrl.is_running()
        assert ctrl.states[-1] == "error"
        assert ctrl._last_health[0] == STATUS_FATAL

        # ...and "Iniciar" must actually start again once the device is back.
        FakeCapture.fail_next = 0
        ctrl.start()
        assert ctrl.is_running()
        assert FakeCapture.instances[-1].is_alive()


class TestFallbackChain:
    def test_no_fallback_left_stops_the_pipeline(self, ctrl, qapp):
        ctrl.start()
        FakeProvider.instances[-1].on_status(ProviderStatus(
            kind=STATUS_FATAL, code=CODE_AUTH, message="401", provider="fake"))
        pump(qapp, 0.1)

        assert not ctrl.is_running()
        assert ctrl.states[-1] == "error"
        # Operator can retry after fixing the key.
        ctrl.start()
        assert ctrl.is_running()

    def test_fallback_keeps_capture_and_transcript(self, ctrl, qapp):
        from dataclasses import replace
        ctrl.config = replace(ctrl.config, fallback_providers=["azure"])
        switched: list[tuple[str, str]] = []
        ctrl.provider_changed.connect(lambda a, b: switched.append((a, b)))

        ctrl.start()
        FakeProvider.instances[-1].on_status(ProviderStatus(
            kind=STATUS_FATAL, code=CODE_AUTH, message="401", provider="fake"))
        pump(qapp, 0.1)

        assert ctrl.is_running()
        assert ctrl.config.provider == "azure"
        assert switched == [("groq", "azure")]
        # The session was NOT torn down: one capture, one transcript file.
        assert len(FakeCapture.instances) == 1
        assert FakeTranscript.started == 1
        assert "stopped" not in ctrl.states

    def test_manual_stop_resets_the_chain(self, ctrl, qapp):
        from dataclasses import replace
        ctrl.config = replace(ctrl.config, fallback_providers=["azure"])
        ctrl.start()
        FakeProvider.instances[-1].on_status(ProviderStatus(
            kind=STATUS_FATAL, code=CODE_AUTH, message="401", provider="fake"))
        pump(qapp, 0.1)
        assert ctrl.config.provider == "azure"

        ctrl.stop()
        assert ctrl._tried_fallbacks == set()


class TestSourceModeIsNonBlocking:
    """Pinning a language used to stop/start the recognizer ON the GUI thread:
    1.3–1.6 s frozen per switch, measured in a real session's log."""

    def _azure(self, ctrl):
        from dataclasses import replace
        ctrl.config = replace(ctrl.config, provider="azure",
                              azure_streaming_mode=False)

    def test_returns_immediately_and_finishes_on_a_worker(self, ctrl, qapp, monkeypatch):
        import threading, time as _t
        self._azure(ctrl)
        ctrl.start()
        old = FakeProvider.instances[-1]
        done: list[tuple[bool, str]] = []
        ctrl.source_mode_changed.connect(lambda ok, m: done.append((ok, m)))

        # Make the provider's stop() slow, like a real network teardown.
        gui_thread = threading.get_ident()
        stopped_on: list[int] = []
        def slow_stop():
            stopped_on.append(threading.get_ident())
            _t.sleep(0.3)
            old.running = False
        monkeypatch.setattr(old, "stop", slow_stop)

        t0 = _t.monotonic()
        assert ctrl.set_source_mode("en-US") is True
        assert _t.monotonic() - t0 < 0.1, "set_source_mode blocked the GUI thread"
        assert ctrl._swap_in_progress
        # A request made DURING the swap is queued, not dropped. It used to be
        # refused with only a log line, and a real F9 press vanished: measured
        # on the installed exe, "source mode change ignored: a swap is already
        # running" swallowed one of four presses, so the cycle never came back
        # round to auto-detect. An operator whose captions are in the wrong
        # language jabs the key; every press has to count.
        assert ctrl.set_source_mode("es-ES") is True, "second request must be queued"
        assert ctrl._pending_source_mode is not None

        pump(qapp, 1.2)
        assert not ctrl._swap_in_progress
        assert stopped_on and stopped_on[0] != gui_thread
        # The LAST request wins — four quick presses land on the fourth
        # language, they do not walk through every one of them.
        assert ctrl.source_mode() == "es-ES"
        assert ctrl._pending_source_mode is None
        assert done == [(True, ""), (True, "")]
        assert ctrl._translator is FakeProvider.instances[-1] is not old

    def test_pinned_wrong_language_is_explained(self, ctrl, qapp, monkeypatch):
        import time as _t
        from dataclasses import replace
        self._azure(ctrl)
        ctrl.config = replace(ctrl.config, azure_streaming_mode=True,
                              azure_streaming_language="en-US")
        monkeypatch.setattr(T.TranslationController, "PIN_HINT_S", 0.05)
        ctrl.start()
        health: list[tuple[str, str, str]] = []
        ctrl.health_changed.connect(lambda k, c, m: health.append((k, c, m)))

        # Speech is arriving (loud block) but the pinned recognizer says nothing.
        ctrl._last_result_at = _t.monotonic() - 1.0
        ctrl._push_audio(b"\x10\x10" * 800)
        pump(qapp, ctrl.TICK_MS / 1000 * 2)

        assert any(k == "degraded" and "Inglês" in m and "Auto-detectar" in m
                   for k, _c, m in health), health


class TestStaleProviderAfterSwap:
    def test_old_provider_events_and_status_are_ignored(self, ctrl, qapp):
        ctrl.start()
        old = FakeProvider.instances[-1]
        assert ctrl._swap_provider(ctrl.config)
        new = FakeProvider.instances[-1]
        assert new is not old

        # A straggler result from the provider we just replaced.
        old.on_event(TranslationEvent(
            detected_language="pt", original_text="atrasado",
            translations={"es": "x"}, is_final=True, seq=57))
        # A late FATAL from its dying worker must not trigger a fallback.
        old.on_status(ProviderStatus(
            kind=STATUS_FATAL, code=CODE_AUTH, message="late", provider="fake"))
        pump(qapp, 0.1)

        assert ctrl.overlay_stub.captions == []
        assert ctrl.is_running()
        assert ctrl._translator is new
        assert ctrl._last_health[0] == STATUS_OK
