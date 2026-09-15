"""Regressions for the crash/stall family found in the 2026-09-15 review.

Every test here corresponds to something MEASURED on the operator's machine
(app.log of 8994 lines, crash.log of 12 launches, plus control experiments),
not to a code smell:

* the Qt GUI thread was running in the COM MTA apartment on every single
  launch, because `soundcard` claims MTA at import time;
* an Azure recognizer kept delivering results after `stop()`, and its
  reconnect worker could start a recognizer that nothing would ever stop;
* a provider swap that finished after the operator pressed "Parar" installed a
  live provider on a stopped controller;
* one lost swap worker disabled language switching and the stall watchdog for
  the rest of the session;
* health never returned to OK, so the tray stayed amber after recovery.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

import translator as T
from config import AppConfig
from providers.base import (
    STATUS_FAILING,
    STATUS_OK,
    ProviderCapabilities,
    TranslationProvider,
)

REPO = Path(__file__).resolve().parent.parent

# conftest.py has an autouse fixture that replaces config.app_data_dir with a
# temp-dir stub, so that no test can write to the operator's real config. The
# migration tests below need the GENUINE function; grab it at import time,
# which runs before any fixture.
import config as _config_at_import
_REAL_APP_DATA_DIR = _config_at_import.app_data_dir


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def _strip_comments(src: str) -> str:
    """Static guards must match CODE, not the comment explaining the code."""
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


# ------------------------------------------------------------------ COM apartment


class TestComApartment:
    def test_entry_point_claims_sta_before_importing_audio_capture(self):
        """A Qt GUI thread on Windows must be STA.

        `audio_capture` imports `soundcard`, which runs
        CoInitializeEx(MULTITHREADED) at import time on the importing thread.
        If that happens before we claim STA, Qt's OleInitialize() fails with
        RPC_E_CHANGED_MODE — which it did on all 154 launches in the log.
        """
        src = (REPO / "translator.py").read_text(encoding="utf-8")
        sta = src.index("COINIT_APARTMENTTHREADED")
        imp = src.index("from audio_capture import")
        assert sta < imp, (
            "translator.py must claim the STA apartment BEFORE anything pulls "
            "soundcard in, or the GUI thread ends up in MTA")

    def test_capture_thread_claims_mta(self):
        """STA on the main thread is only half the fix.

        soundcard relies on the process being MTA so that any thread can use
        COM without initialising it. Once the main thread is STA its
        com_loaded goes False, and the capture thread is left with no COM at
        all: measured failure was 0x800401F0 CO_E_NOTINITIALIZED. This is
        exactly why the previous attempt at the apartment fix was reverted.
        """
        src = _strip_comments((REPO / "audio_capture.py").read_text(encoding="utf-8"))
        assert "_com_initialize_mta()" in src
        assert "_com_uninitialize()" in src
        run = src[src.index("def _run(self)"):src.index("def start(self)")]
        assert "_com_initialize_mta()" in run, "capture thread never claims COM"
        assert "_com_uninitialize()" in run, "capture thread never releases COM"

    def test_mta_initializer_does_not_unbalance_a_foreign_apartment(self):
        """RPC_E_CHANGED_MODE means somebody else owns this thread's apartment.

        Returning True there would make the caller CoUninitialize() a COM
        instance it never initialised.
        """
        from audio_capture import _com_initialize_mta, _com_uninitialize

        import ctypes
        # Claim STA on a throwaway thread, then ask for MTA on the same thread.
        result: dict[str, bool] = {}

        def worker():
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # STA first
            result["claimed"] = _com_initialize_mta()
            ctypes.windll.ole32.CoUninitialize()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert result.get("claimed") is False, (
            "a thread already in another apartment must not be un-initialised "
            "by us")

    def test_capture_really_delivers_audio_under_the_new_apartment(self, qapp):
        """The end-to-end check the reverted fix would have failed."""
        from audio_capture import AudioCapture

        got: list[int] = []
        cap = AudioCapture(on_audio=lambda b: got.append(len(b)), blocksize_ms=50)
        cap.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(got) < 3:
            time.sleep(0.05)
        reason = cap.died_reason()
        cap.stop()
        assert not reason, f"capture died: {reason}"
        assert len(got) >= 3, "no audio delivered from the capture thread"


# ------------------------------------------------------------------ Azure lifecycle


class _Sig:
    def __init__(self):
        self.handlers: list = []
        self.disconnected = False

    def connect(self, fn):
        self.handlers.append(fn)

    def disconnect_all(self):
        self.disconnected = True
        self.handlers.clear()


class _FakeRecognizer:
    def __init__(self):
        for name in ("recognizing", "recognized", "canceled",
                     "session_started", "session_stopped"):
            setattr(self, name, _Sig())
        self.stopped = False

    def start_continuous_recognition(self):
        pass

    def stop_continuous_recognition(self):
        self.stopped = True


def _azure(monkeypatch, **kw):
    from providers.azure import AzureProvider

    on_status = kw.pop("on_status", lambda s: None)
    p = AzureProvider(
        speech_key="k", region="r",
        source_languages=kw.pop("source_languages", ["pt-BR", "en-US", "es-ES"]),
        target_languages=["en", "es"],
        on_event=kw.pop("on_event", lambda e: None),
        **kw,
    )
    # The status channel is wired by the base class after construction.
    p.on_status = on_status
    return p


class TestAzureStopIsFinal:
    def test_events_after_stop_never_reach_the_controller(self, monkeypatch):
        """The SDK dispatches on its own thread; results outlive stop().

        Measured tail of a real session: 207 `canceled` callbacks inside ONE
        second at shutdown. Any of those reaching a controller that believes
        it is stopped is a use-after-free waiting to happen at quit, when the
        overlay is already gone.
        """
        seen: list = []
        p = _azure(monkeypatch, on_event=seen.append)
        fake = _FakeRecognizer()
        monkeypatch.setattr(p, "_build_recognizer", lambda: fake)
        p.start()
        assert p.is_running

        p.stop()
        # A result the SDK had already dispatched now lands.
        evt = type("E", (), {"result": type("R", (), {
            "text": "tarde demais", "translations": {}, "result_id": "x",
            "properties": {}, "reason": None})()})()
        p._on_recognizing(evt)
        p._on_recognized(evt)
        assert seen == [], "a stopped provider still pushed captions"


    def test_first_results_of_a_session_are_not_dropped(self, monkeypatch):
        """The stop-guard must not swallow the START of a session.

        _is_defunct() drops anything arriving while _running is False, so if
        _running were set only AFTER start_continuous_recognition() returned,
        a result dispatched during the handshake would be lost — the guard
        would have created the very symptom it was added to fix.
        """
        seen: list = []
        p = _azure(monkeypatch, on_event=seen.append)
        fake = _FakeRecognizer()

        def _start():
            # The SDK dispatches while we are still inside start().
            evt = type("E", (), {"result": type("R", (), {
                "text": "primeira frase", "translations": {"en": "first"},
                "result_id": "1", "properties": {}, "reason": None})()})()
            p._on_recognizing(evt)

        fake.start_continuous_recognition = _start
        monkeypatch.setattr(p, "_build_recognizer", lambda: fake)
        p.start()
        assert [e.original_text for e in seen] == ["primeira frase"]

    def test_failed_start_does_not_leave_the_provider_looking_alive(self, monkeypatch):
        p = _azure(monkeypatch)
        fake = _FakeRecognizer()

        def _boom():
            raise RuntimeError("sem rede")

        fake.start_continuous_recognition = _boom
        monkeypatch.setattr(p, "_build_recognizer", lambda: fake)
        with pytest.raises(RuntimeError):
            p.start()
        assert not p.is_running

    def test_stop_disconnects_every_signal(self, monkeypatch):
        p = _azure(monkeypatch)
        fake = _FakeRecognizer()
        monkeypatch.setattr(p, "_build_recognizer", lambda: fake)
        p.start()
        p.stop()
        for name in ("recognizing", "recognized", "canceled",
                     "session_started", "session_stopped"):
            assert getattr(fake, name).disconnected, f"{name} left connected"

    def test_stopping_flag_is_set_before_the_blocking_stop(self):
        """INVARIANT, load-bearing and easy to break by tidying.

        stop_continuous_recognition() is called while holding self._lock, and
        the SDK fires session_stopped on its callback thread DURING that call.
        _on_session_stopped only avoids deadlocking because it checks
        `not self._stopping` and returns before touching the lock. Set the
        flag after the blocking call and the app hangs on every stop.
        """
        src = _strip_comments((REPO / "providers" / "azure.py").read_text(encoding="utf-8"))
        body = src[src.index("def stop(self)"):src.index("def set_source_language")]
        assert body.index("self._stopping = True") < body.index("stop_continuous_recognition()"), (
            "_stopping must be set BEFORE stop_continuous_recognition()")

    def test_reconnect_rechecks_stop_inside_the_lock(self):
        """Checking _running outside the lock races with stop().

        Lose that race and the worker builds and starts a recognizer that
        nothing will ever stop, against a provider whose is_running() is False.
        """
        src = _strip_comments((REPO / "providers" / "azure.py").read_text(encoding="utf-8"))
        worker = src[src.index("def worker()"):src.index("threading.Thread(target=worker")]
        lock_at = worker.index("with self._lock:")
        build_at = worker.index("self._build_recognizer()")
        guarded = worker[lock_at:build_at]
        assert "self._stopping or not self._running" in guarded, (
            "the reconnect worker must re-check the stop flags INSIDE the lock")

    def test_reconnect_disconnects_the_replaced_recognizers_results(self):
        """Leaving recognizing/recognized connected let two sessions write the
        same caption band at once."""
        src = _strip_comments((REPO / "providers" / "azure.py").read_text(encoding="utf-8"))
        worker = src[src.index("def worker()"):src.index("threading.Thread(target=worker")]
        assert "old.recognizing.disconnect_all()" in worker
        assert "old.recognized.disconnect_all()" in worker


class TestAzureLanguageIdentification:
    """Azure returns one of the candidate languages EVEN IF none was spoken.

    Measured consequence with pt/en/es candidates on Portuguese speech: the
    service emitted 'bom dia a todos' (pt-BR) and 'bongiatos' (en-US) for the
    same audio, milliseconds apart. At-start LID decides once and holds.
    """

    def _lid_mode(self, monkeypatch, langs):
        import providers.azure as az

        captured: dict[str, str] = {}

        class _Cfg:
            def add_target_language(self, t): pass

            def set_property(self, *a, **kw):
                pid = kw.get("property_id", a[0] if a else None)
                val = kw.get("value", a[1] if len(a) > 1 else None)
                captured[str(pid)] = str(val)

            speech_recognition_language = ""

        monkeypatch.setattr(az.speechsdk.translation, "SpeechTranslationConfig",
                            lambda **kw: _Cfg())
        monkeypatch.setattr(az.speechsdk.audio, "AudioStreamFormat", lambda **kw: object())
        monkeypatch.setattr(az.speechsdk.audio, "PushAudioInputStream", lambda **kw: object())
        monkeypatch.setattr(az.speechsdk.audio, "AudioConfig", lambda **kw: object())
        monkeypatch.setattr(az.speechsdk.languageconfig,
                            "AutoDetectSourceLanguageConfig", lambda **kw: object())
        monkeypatch.setattr(az.speechsdk.translation, "TranslationRecognizer",
                            lambda **kw: _FakeRecognizer())
        p = _azure(monkeypatch, source_languages=langs)
        p._build_recognizer()
        return captured

    def test_three_languages_use_at_start_lid(self, monkeypatch):
        captured = self._lid_mode(monkeypatch, ["pt-BR", "en-US", "es-ES"])
        assert not any("Continuous" == v for v in captured.values()), (
            "3 candidates must use at-start LID, which is the service default "
            "and is obtained by NOT setting the mode property")

    def test_more_than_four_languages_fall_back_to_continuous(self, monkeypatch):
        captured = self._lid_mode(
            monkeypatch, ["pt-BR", "en-US", "es-ES", "fr-FR", "de-DE"])
        assert any("Continuous" == v for v in captured.values()), (
            "at-start LID caps at 4 candidates; above that we must ask for "
            "continuous or the service rejects the config")

    def test_stable_partial_threshold_is_requested(self, monkeypatch):
        captured = self._lid_mode(monkeypatch, ["pt-BR"])
        from constants import AZURE_STABLE_PARTIAL_THRESHOLD
        assert str(AZURE_STABLE_PARTIAL_THRESHOLD) in captured.values(), (
            "the service-side flicker control must be requested")

    def test_segmentation_timeout_stays_under_the_hallucination_threshold(self):
        """Microsoft known issue 3002: > 1000 ms generates random words."""
        from constants import AZURE_SEGMENTATION_SILENCE_MS
        assert 100 <= AZURE_SEGMENTATION_SILENCE_MS <= 1000


# ------------------------------------------------------------------ controller


class _SwapProvider(TranslationProvider):
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=True, translates=True, streaming=True, label="swap")
    built: list["_SwapProvider"] = []

    def __init__(self, on_event, on_status):
        self.on_event, self.on_status = on_event, on_status
        self.running = False
        self.stop_calls = 0
        _SwapProvider.built.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False
        self.stop_calls += 1

    def push_audio(self, b):
        pass

    @property
    def is_running(self):
        return self.running


class _Capture:
    def __init__(self, on_audio, device_index=None, samplerate=16000,
                 channels=1, on_died=None):
        self.alive = False

    def start(self):
        self.alive = True

    def stop(self):
        self.alive = False

    def is_alive(self):
        return self.alive

    def seconds_since_audio(self):
        return 0.0

    def died_reason(self):
        return ""


class _Transcript:
    def __init__(self, provider_name, target_languages):
        self.txt_path = self.srt_path = None

    def start(self):
        pass

    def stop(self):
        pass

    def append(self, **kw):
        pass


class _Overlay:
    def push_caption(self, *a, **kw):
        pass

    def apply_config(self, cfg):
        pass


@pytest.fixture
def ctrl(qapp, monkeypatch):
    _SwapProvider.built.clear()
    monkeypatch.setattr(T, "build_provider",
                        lambda cfg, on_event, on_status: _SwapProvider(on_event, on_status))
    monkeypatch.setattr(T, "provider_capabilities", lambda n: _SwapProvider.CAPABILITIES)
    monkeypatch.setattr(T, "find_device", lambda n: None)
    monkeypatch.setattr(T, "AudioCapture", _Capture)
    monkeypatch.setattr(T, "TranscriptWriter", _Transcript)
    import usage_tracker
    monkeypatch.setattr(usage_tracker, "add_seconds", lambda *a, **k: None)
    cfg = AppConfig(provider="azure", azure_speech_key="k", azure_speech_region="r",
                    fallback_providers=[])
    c = T.TranslationController(cfg, _Overlay())
    yield c
    if c.is_running():
        c.stop()


class TestSwapVersusStop:
    def test_swap_finishing_after_stop_does_not_install_a_live_provider(self, ctrl, qapp):
        """The operator pressing Parar during an F9 language change.

        The swap runs on a worker precisely so the GUI stays responsive, which
        is exactly what makes this reachable. Installing here left a started
        provider attached to a stopped controller: stop() never saw it again,
        it kept billing, and the tray announced "rodando" on a dead app.
        """
        ctrl.start()
        assert ctrl.is_running()
        late = _SwapProvider(lambda e: None, lambda s: None)
        late.start()
        ctrl.stop()

        ctrl._on_swap_result(True, ctrl.config, late, "")

        assert not late.running, "the orphaned provider was left running"
        assert late.stop_calls == 1
        assert ctrl._translator is not late, "installed a provider on a stopped controller"

    def test_stop_clears_the_swap_latch(self, ctrl):
        """A swap worker that never reports back used to disable language
        switching AND the stall watchdog for the rest of the session."""
        ctrl.start()
        ctrl._swap_in_progress = True
        ctrl.stop()
        assert ctrl._swap_in_progress is False

    def test_stop_clears_the_fallback_cooldown(self, ctrl):
        """Stop+start inside the cooldown silently swallowed the new session's
        first fallback — right when the operator had restarted to recover."""
        ctrl.start()
        ctrl._last_fallback_at = time.monotonic()
        ctrl.stop()
        assert ctrl._last_fallback_at == 0.0


class TestStaleCaptureReopen:
    def test_reopen_from_a_previous_session_is_ignored(self, ctrl, qapp):
        """A pending 2/5/10 s reopen timer outlived stop() and tore down the
        capture of the session the operator had just restarted."""
        ctrl.start()
        stale_session = ctrl._capture_session
        ctrl.stop()
        ctrl.start()
        fresh = ctrl._capture

        ctrl._reopen_capture(stale_session)

        assert ctrl._capture is fresh, "a dead session's timer rebuilt the live capture"


class TestHealthReturnsToOk:
    def test_successful_swap_clears_the_warning(self, ctrl, qapp):
        """Health only ever went back to OK from start() and capture recovery,
        so one stall left the tray amber for the rest of the event."""
        ctrl.start()
        seen: list[str] = []
        ctrl.health_changed.connect(lambda kind, code, msg: seen.append(kind))
        ctrl._set_health(STATUS_FAILING, "", "algo deu errado")
        assert seen[-1] == STATUS_FAILING

        ctrl._on_swap_result(True, ctrl.config,
                             _SwapProvider(lambda e: None, lambda s: None), "")
        assert seen[-1] == STATUS_OK, "recovery never announced itself"


class TestThreadingExcepthook:
    def test_worker_thread_exceptions_are_logged(self):
        """Five worker threads, and an exception escaping any of them printed
        to a stderr that does not exist in the windowed exe."""
        src = _strip_comments((REPO / "translator.py").read_text(encoding="utf-8"))
        assert "threading.excepthook" in src, (
            "sys.excepthook only covers the GUI thread")


# ------------------------------------------------------------------ config durability


class TestConfigSurvivesAnInterruptedWrite:
    @pytest.fixture
    def isolated(self, monkeypatch, tmp_path):
        import config as config_mod
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(config_mod, "app_data_dir", lambda: tmp_path)
        return tmp_path

    def test_save_is_atomic(self, isolated, monkeypatch):
        """A half-written config.json is read as 'no config at all', which
        silently resets every setting of the event."""
        import config as config_mod
        from config import save_config

        seen: dict[str, object] = {}
        real_replace = config_mod.os.replace

        def spy(src, dst):
            # At this instant the destination must still hold the OLD bytes:
            # that is what "atomic" buys us.
            seen["dst_before"] = Path(dst).read_text(encoding="utf-8") \
                if Path(dst).exists() else None
            return real_replace(src, dst)

        save_config(AppConfig(provider="azure", azure_speech_key="k"))
        first = (isolated / "config.json").read_text(encoding="utf-8")
        monkeypatch.setattr(config_mod.os, "replace", spy)
        save_config(AppConfig(provider="groq", groq_api_key="k"))
        assert seen["dst_before"] == first, (
            "the real file was being mutated in place instead of swapped")

    def test_corrupt_config_is_kept_not_silently_discarded(self, isolated, caplog):
        from config import load_config

        (isolated / "config.json").write_text('{"provider": "azu', encoding="utf-8")
        with caplog.at_level("ERROR"):
            cfg = load_config()
        assert cfg.provider == "azure"          # the dataclass default
        kept = list(isolated.glob("config.corrupt-*.json"))
        assert kept, "the unreadable config was thrown away without a copy"
        assert any("unreadable" in r.message for r in caplog.records), (
            "resetting every setting must not be silent")


# ------------------------------------------------------------------ overlay fit


class TestShrinkBeforeDroppingLines:
    def test_soft_floor_follows_the_operators_font_size(self):
        """As an absolute 26 pt, the comfortable-shrink pass silently disabled
        itself for every font below 30 pt — the loop condition is
        `size - step >= floor`. The operator's live config is 25 pt, so the
        band skipped shrinking entirely and went straight to discarding
        history, which is the opposite of the design."""
        from overlay_qt import CaptionOverlay as C

        # 25 pt: a floor of 26 would make the first shrink unreachable.
        floor_25 = C._min_fit_pt(C, 25)
        assert floor_25 < 25 - C.FIT_STEP_PT, (
            "at 25 pt the shrink pass can still never run")
        # Never below readability from the back of a room.
        assert C._min_fit_pt(C, 18) >= C.MIN_FIT_PT_HARD
        # Large fonts still get a proportional, not absolute, allowance.
        assert C._min_fit_pt(C, 60) > floor_25


# ------------------------------------------------------------------ settings dialog


class TestTestCaptureDoesNotLeakAStream:
    def test_second_click_is_refused_while_one_is_running(self, qapp, monkeypatch):
        """Each extra click used to orphan a live WASAPI stream: finish() reads
        self._test_capture at FIRE time, so only the last one was ever
        stopped."""
        import settings_window as sw
        from settings_window import SettingsWindow

        started: list[object] = []

        class _Cap:
            def __init__(self, **kw):
                self.stopped = False
                started.append(self)

            def start(self):
                pass

            def stop(self):
                self.stopped = True

        monkeypatch.setattr(sw, "AudioCapture", _Cap, raising=False)
        win = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k"))
        try:
            monkeypatch.setattr("audio_capture.AudioCapture", _Cap)
            monkeypatch.setattr("audio_capture.find_device", lambda n: object())
            win._on_test_capture()
            win._on_test_capture()
            win._on_test_capture()
            assert len(started) == 1, "extra clicks opened extra capture streams"
            assert win.test_capture_btn.isEnabled() is False
        finally:
            win.close()

    def test_closing_the_dialog_stops_the_capture(self, qapp, monkeypatch):
        import settings_window as sw
        from settings_window import SettingsWindow

        class _Cap:
            def __init__(self, **kw):
                self.stopped = False

            def start(self):
                pass

            def stop(self):
                self.stopped = True

        win = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k"))
        try:
            monkeypatch.setattr("audio_capture.AudioCapture", _Cap)
            monkeypatch.setattr("audio_capture.find_device", lambda n: object())
            win.show()
            win._on_test_capture()
            cap = win._test_capture
            win.hide()
            assert cap.stopped, "the stream kept recording after the dialog closed"
            assert win.test_capture_btn.isEnabled() is True
        finally:
            win.close()


# ------------------------------------------------------------------ rename migration


class TestRenameDoesNotOrphanTheOperator:
    """Settings live in a folder named after the app and secrets are keyed on
    that name in Credential Manager. A rename without migration means the
    operator opens a freshly renamed build on the morning of an event and finds
    default languages, default layout, and a prompt for the API key they
    already entered weeks ago."""

    def test_settings_are_adopted_from_the_previous_app_folder(self, tmp_path, monkeypatch):
        import config as config_mod

        legacy = tmp_path / "TeamsLiveTranslation"
        legacy.mkdir()
        (legacy / "config.json").write_text(
            '{"provider": "azure", "target_languages": ["en", "es"]}', encoding="utf-8")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(config_mod, "APP_NAME", "CaptionBand")
        monkeypatch.setattr(config_mod, "app_data_dir", _REAL_APP_DATA_DIR)

        new_dir = config_mod.app_data_dir()
        assert (new_dir / "config.json").is_file(), "settings were left behind"
        loaded = (new_dir / "config.json").read_text(encoding="utf-8")
        assert '"en", "es"' in loaded

    def test_adoption_never_overwrites_an_existing_config(self, tmp_path, monkeypatch):
        import config as config_mod

        legacy = tmp_path / "TeamsLiveTranslation"
        legacy.mkdir()
        (legacy / "config.json").write_text('{"provider": "groq"}', encoding="utf-8")
        current = tmp_path / "CaptionBand"
        current.mkdir()
        (current / "config.json").write_text('{"provider": "azure"}', encoding="utf-8")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(config_mod, "APP_NAME", "CaptionBand")
        monkeypatch.setattr(config_mod, "app_data_dir", _REAL_APP_DATA_DIR)

        config_mod.app_data_dir()
        assert '"azure"' in (current / "config.json").read_text(encoding="utf-8"), (
            "an existing config was clobbered by the migration")

    def test_secrets_are_read_from_the_previous_service_name(self, monkeypatch):
        import secrets_store

        store = {("TeamsLiveTranslation", "azure_speech_key"): "a-chave-do-operador"}

        class _KR:
            def get_password(self, service, key):
                return store.get((service, key))

            def set_password(self, service, key, value):
                store[(service, key)] = value

        monkeypatch.setattr(secrets_store, "_backend", lambda: _KR())
        got = secrets_store.get_secret("azure_speech_key")
        assert got == "a-chave-do-operador", "the rename hid the stored API key"
        # ...and it is copied forward so this happens only once.
        assert store.get((secrets_store.SERVICE, "azure_speech_key")) == "a-chave-do-operador"
