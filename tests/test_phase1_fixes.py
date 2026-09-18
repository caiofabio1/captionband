"""Regressions for the 2026-09-16 code-review findings (phase 1).

Covered here, one test class per finding:

* the provider-swap signal carried no generation token, so a worker that
  finished after stop()+start() installed the OLD provider over the new one
  (live, billing, and with self.config silently reverted);
* fallback and stall-recovery swapped the provider SYNCHRONOUSLY on the GUI
  thread — 1.3–1.6 s frozen per failure, measured in a real session's log;
* a failed async swap left the controller with NO provider until the 45 s
  stall watchdog noticed;
* is_valid() had no branch for "openai_realtime": the provider showed in the
  UI but Save always rejected it;
* save_config() blanked a secret in the JSON even when the keyring write
  FAILED — the key vanished from both stores;
* load_config() died on valid JSON that was not an object, and on sub-dicts
  or field values of the wrong type;
* nothing reacted to monitor hot-plug: an unplugged projector left the band
  off every real screen;
* _failure_timestamps was cleared without the lock that record_failure()
  takes;
* start() kept a reference to a provider whose start() had raised;
* no --version flag for the CI smoke test of the frozen exe.

Style follows test_stability_fixes.py / test_controller_recovery.py: the REAL
controller and config loader, fakes only at the edges.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

import config as config_mod
import translator as T
from config import AppConfig, OverlayConfig, load_config, save_config
from constants import APP_VERSION
from providers import PROVIDER_LABELS
from providers.base import (
    CODE_AUTH,
    STATUS_FATAL,
    STATUS_OK,
    ProviderCapabilities,
    ProviderStatus,
    TranslationProvider,
)

REPO = Path(__file__).resolve().parent.parent


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


# ---------------------------------------------------------------------- fakes


class FakeProvider(TranslationProvider):
    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False, translates=True, streaming=False, label="fake",
    )
    instances: list[FakeProvider] = []
    # How many of the NEXT start() calls raise (broken network, bad key).
    fail_next_starts = 0

    def __init__(self, on_event, on_status):
        self.on_event = on_event
        self.on_status = on_status
        self.running = False
        self.stop_calls = 0
        FakeProvider.instances.append(self)

    def start(self) -> None:
        if FakeProvider.fail_next_starts > 0:
            FakeProvider.fail_next_starts -= 1
            raise RuntimeError("start boom")
        self.running = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    def push_audio(self, audio_bytes: bytes) -> None:
        pass

    @property
    def is_running(self) -> bool:
        return self.running


class FakeCapture:
    # How many of the NEXT constructions raise on start().
    fail_next = 0

    def __init__(self, on_audio, device_index=None, samplerate=16000,
                 channels=1, on_died=None):
        self.alive = False

    def start(self) -> None:
        if FakeCapture.fail_next > 0:
            FakeCapture.fail_next -= 1
            raise RuntimeError("no device")
        self.alive = True

    def stop(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    def seconds_since_audio(self) -> float:
        return 0.0

    def died_reason(self) -> str:
        return ""


class FakeTranscript:
    def __init__(self, provider_name, target_languages):
        self.txt_path = self.srt_path = None

    def start(self) -> None:
        pass

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
    FakeProvider.fail_next_starts = 0
    FakeCapture.fail_next = 0

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
    # The backoff SHAPE is what matters; the test cannot wait 2/5/10 s.
    monkeypatch.setattr(T.TranslationController, "SWAP_RETRY_DELAYS_S",
                        (0.02, 0.02, 0.02), raising=False)

    cfg = AppConfig(provider="azure", azure_speech_key="k", azure_speech_region="r",
                    openrouter_api_key="k",
                    fallback_providers=[])
    c = T.TranslationController(cfg, StubOverlay())
    c.states: list[str] = []
    c.state_changed.connect(c.states.append)
    yield c
    if c.is_running():
        c.stop()


def _fatal(provider) -> None:
    provider.on_status(ProviderStatus(
        kind=STATUS_FATAL, code=CODE_AUTH, message="401", provider="fake"))


# ------------------------------------------------------- swap generation token


class TestSwapGeneration:
    def test_stale_swap_result_is_discarded_and_stopped(self, ctrl):
        """A worker reporting for a superseded swap must not be installed.

        Before the token, this installed the OLD provider on top of the new
        one: the new session kept billing with nothing able to stop it, and
        self.config was reverted underneath it.
        """
        ctrl.start()
        ctrl._swap_in_progress = True      # a NEWER swap is supposedly running
        stale = FakeProvider(lambda e: None, lambda s: None)
        stale.running = True

        ctrl._on_swap_result(True, ctrl.config, stale, "", ctrl._swap_gen - 1)

        assert stale.stop_calls == 1, "the orphaned provider kept billing"
        assert ctrl._translator is not stale
        # ...and the latch of the swap that is ACTUALLY running survives.
        assert ctrl._swap_in_progress is True

    def test_current_generation_installs(self, ctrl):
        ctrl.start()
        new = FakeProvider(lambda e: None, lambda s: None)
        new.running = True
        ctrl._on_swap_result(True, ctrl.config, new, "", ctrl._swap_gen)
        assert ctrl._translator is new
        assert new.stop_calls == 0

    def test_stop_bumps_the_generation(self, ctrl):
        ctrl.start()
        gen = ctrl._swap_gen
        ctrl.stop()
        assert ctrl._swap_gen > gen

    def test_swap_worker_finishing_after_stop_is_rejected(self, ctrl, qapp):
        """The full async path: swap starts, Parar lands, worker reports."""
        ctrl.start()
        ctrl._swap_provider_async(ctrl.config)
        ctrl.stop()
        pump(qapp, 0.4)

        built = [p for p in FakeProvider.instances if p is not None]
        # Whatever the worker managed to build was stopped, never installed.
        assert ctrl._translator is None
        for p in built[1:]:
            assert p.running is False
        assert ctrl.states[-1] == "stopped"


# ------------------------------------------------- fallback/stall off the GUI


class TestFallbackIsAsync:
    def test_fallback_teardown_runs_off_the_gui_thread(self, ctrl, qapp, monkeypatch):
        """Stopping/starting a recognizer is 1.3–1.6 s of blocking I/O; on the
        GUI thread the operator read it as "the app hung"."""
        from dataclasses import replace as _replace
        ctrl.config = _replace(ctrl.config, fallback_providers=["whisper_local"])
        switched: list[tuple[str, str]] = []
        ctrl.provider_changed.connect(lambda a, b: switched.append((a, b)))
        ctrl.start()
        old = FakeProvider.instances[-1]

        gui_thread = threading.get_ident()
        stopped_on: list[int] = []

        def slow_stop():
            stopped_on.append(threading.get_ident())
            time.sleep(0.25)
            old.running = False

        monkeypatch.setattr(old, "stop", slow_stop)
        _fatal(old)
        pump(qapp, 0.8)

        assert stopped_on and stopped_on[0] != gui_thread, (
            "the provider teardown blocked the GUI thread")
        assert ctrl.is_running()
        assert ctrl.config.provider == "whisper_local"
        assert switched == [("azure", "whisper_local")]

    def test_failed_fallback_candidate_walks_to_the_next(self, ctrl, qapp):
        """A candidate that cannot start must not end the chain: it is marked
        tried and the NEXT candidate gets its turn."""
        ctrl.config = replace(ctrl.config,
                              fallback_providers=["whisper_local", "openrouter"])
        ctrl.start()
        FakeProvider.fail_next_starts = 1     # the first swap's start() raises

        _fatal(FakeProvider.instances[-1])
        pump(qapp, 0.8)

        assert ctrl.is_running()
        assert ctrl.config.provider == "openrouter"
        assert "whisper_local" in ctrl._tried_fallbacks
        assert ctrl._last_health[0] == STATUS_OK

    def test_failed_swap_retries_on_a_short_backoff(self, ctrl, qapp):
        """A failed swap leaves _translator None; waiting for the 45 s stall
        watchdog meant nearly a minute of mute captions for a transient
        network error."""
        ctrl.start()
        FakeProvider.fail_next_starts = 2     # swap + first retry fail

        assert ctrl.set_source_mode("en-US") is True
        pump(qapp, 1.0)

        new = FakeProvider.instances[-1]
        assert ctrl._translator is new
        assert new.running is True
        # initial provider + failed swap + failed retry + successful retry
        assert len(FakeProvider.instances) == 4
        assert ctrl._swap_retry == 0
        assert ctrl._last_health[0] == STATUS_OK

    def test_exhausted_retry_budget_escalates_to_the_fallback_chain(self, ctrl, qapp):
        """With no fallback configured, an unrecoverable provider must leave a
        CONSISTENT state — stopped — not a running controller with no provider."""
        ctrl.start()
        FakeProvider.fail_next_starts = 99

        assert ctrl.set_source_mode("en-US") is True
        pump(qapp, 1.5)

        assert not ctrl.is_running()
        assert ctrl.states[-1] == "error"
        assert ctrl._last_health[0] == STATUS_FATAL


class TestStallRecoveryIsAsync:
    def test_stall_recovery_swaps_off_the_gui_thread(self, ctrl, qapp, monkeypatch):
        monkeypatch.setattr(T.TranslationController, "RESULT_STALL_S", 0.01,
                            raising=False)
        ctrl.start()
        old = FakeProvider.instances[-1]

        gui_thread = threading.get_ident()
        stopped_on: list[int] = []

        def slow_stop():
            stopped_on.append(threading.get_ident())
            time.sleep(0.25)
            old.running = False

        monkeypatch.setattr(old, "stop", slow_stop)

        now = time.monotonic()
        ctrl._last_speech_at = now
        ctrl._last_result_at = now - 100.0
        ctrl._check_result_stall()

        assert ctrl._swap_in_progress, "stall recovery did not start a swap"
        pump(qapp, 0.8)

        assert stopped_on and stopped_on[0] != gui_thread
        assert ctrl._translator is FakeProvider.instances[-1] is not old
        assert ctrl._last_health[0] == STATUS_OK, (
            "recovery never announced itself — the tray stayed amber")


# -------------------------------------------------------------- lock hygiene


class TestFailureListLocking:
    def test_every_clear_of_failure_timestamps_holds_the_state_lock(self):
        """record_failure() mutates the list under _state_lock from provider
        worker threads; the two clears (stop, _attempt_fallback) ran without
        it, and a failure could be lost exactly when fallback was deciding."""
        src = _strip_comments((REPO / "translator.py").read_text(encoding="utf-8"))
        total = src.count("self._failure_timestamps.clear()")
        locked = len(re.findall(
            r"with self\._state_lock:\s*\n\s*self\._failure_timestamps\.clear\(\)",
            src))
        assert total == 2, f"expected 2 clear sites, found {total}"
        assert locked == total, "a clear of _failure_timestamps runs unlocked"


# ------------------------------------------------------------------ start()


class TestStartFailure:
    def test_provider_start_exception_leaves_no_reference(self, ctrl):
        """start() used to keep _translator pointing at a provider that never
        started — stop() would later call provider.stop() on it as if live."""
        FakeProvider.fail_next_starts = 1
        with pytest.raises(RuntimeError, match="start boom"):
            ctrl.start()
        assert ctrl._translator is None
        assert not ctrl.is_running()

    def test_capture_failure_stops_the_provider_and_surfaces_the_real_error(self, ctrl):
        FakeCapture.fail_next = 1
        with pytest.raises(RuntimeError, match="no device"):
            ctrl.start()
        assert ctrl._translator is None
        provider = FakeProvider.instances[-1]
        assert provider.stop_calls == 1, "the provider session kept billing"
        assert not ctrl.is_running()


# ------------------------------------------------------------------ --version


class TestVersionFlag:
    def test_version_prints_and_exits_without_a_qapplication(self, qapp, monkeypatch, capsys):
        """CI smoke test for the frozen exe: no display, no QApplication."""

        class _NoQApp:
            def __init__(self, *a, **kw):
                raise AssertionError("--version must not create a QApplication")

        monkeypatch.setattr(T, "QApplication", _NoQApp)
        monkeypatch.setattr(sys, "argv", ["translator.py", "--version"])
        rc = T.main()
        assert rc == 0
        assert capsys.readouterr().out.strip() == APP_VERSION


# ------------------------------------------------------------------ is_valid


class TestIsValidCoversEveryAdvertisedProvider:
    def test_every_provider_in_the_ui_has_a_validation_rule(self):
        """The bug: PROVIDER_LABELS and is_valid() are maintained by hand, and
        openai_realtime was added to one but not the other — the provider
        showed in the UI and Save ALWAYS rejected it."""
        full = AppConfig(
            provider="azure",
            azure_speech_key="k", azure_speech_region="r",
            google_credentials_json="{}", google_project_id="p",
            whisper_model="small",
            openai_api_key="k",
            openrouter_api_key="k",
        )
        for name in PROVIDER_LABELS:
            cfg = replace(full, provider=name)
            assert cfg.is_valid(), (
                f"is_valid() has no branch for provider {name!r} — "
                "it appears in the UI but can never be saved")

    def test_unknown_provider_is_still_invalid(self):
        assert not AppConfig(provider="does-not-exist").is_valid()

    def test_openai_realtime_requires_the_openai_key(self):
        assert not AppConfig(provider="openai_realtime").is_valid()
        assert AppConfig(provider="openai_realtime", openai_api_key="sk-x").is_valid()


# ------------------------------------------------------------- save_config


class TestSaveConfigKeyringFailure:
    def test_failed_keyring_write_keeps_the_key_in_the_json(self, monkeypatch, caplog):
        """Blanking the field after a FAILED set_secret erased the key from
        the JSON too — it vanished from both stores."""
        import secrets_store
        monkeypatch.setattr(secrets_store, "is_available", lambda: True)
        monkeypatch.setattr(secrets_store, "set_secret", lambda k, v: False)

        save_config(AppConfig(provider="azure", azure_speech_key="real-key"))
        payload = json.loads(config_mod.config_path().read_text(encoding="utf-8"))

        assert payload["azure_speech_key"] == "real-key", (
            "the key was erased from the JSON after the keyring write failed")
        assert any(r.levelno >= logging.CRITICAL for r in caplog.records), (
            "losing the only copy of a credential must not be quiet")

    def test_successful_keyring_write_blanks_the_field(self, monkeypatch):
        import secrets_store
        monkeypatch.setattr(secrets_store, "is_available", lambda: True)
        monkeypatch.setattr(secrets_store, "set_secret", lambda k, v: True)

        save_config(AppConfig(provider="azure", azure_speech_key="real-key"))
        payload = json.loads(config_mod.config_path().read_text(encoding="utf-8"))

        assert payload["azure_speech_key"] == ""


# ------------------------------------------------------------- load_config


class TestLoadConfigRobustness:
    def test_top_level_list_is_quarantined_not_fatal(self):
        """Valid JSON, wrong shape: '[...]' raised AttributeError on raw.pop()
        OUTSIDE the quarantine try and killed the boot."""
        config_mod.config_path().write_text('["provider", "azure"]', encoding="utf-8")
        cfg = load_config()
        assert cfg.provider == "azure"          # the dataclass default
        assert list(config_mod.config_path().parent.glob("config.corrupt-*.json")), (
            "the unreadable config was thrown away without a copy")

    def test_audio_sub_dict_with_wrong_type_is_quarantined(self):
        config_mod.config_path().write_text(
            '{"provider": "openrouter", "audio": "not-an-object"}', encoding="utf-8")
        cfg = load_config()
        assert cfg.provider == "azure"
        assert list(config_mod.config_path().parent.glob("config.corrupt-*.json"))

    def test_numeric_strings_are_coerced_per_field(self):
        """width_ratio: "0.8" (string) used to reach the overlay and explode
        the geometry math."""
        config_mod.config_path().write_text(json.dumps({
            "provider": "openrouter", "openrouter_api_key": "k",
            "overlay": {"width_ratio": "0.8", "position": "top"},
        }), encoding="utf-8")
        cfg = load_config()
        assert cfg.overlay.width_ratio == 0.8
        assert isinstance(cfg.overlay.width_ratio, float)
        assert cfg.overlay.position == "top"

    def test_one_rotten_field_does_not_take_the_others_down(self):
        config_mod.config_path().write_text(json.dumps({
            "provider": "openrouter", "openrouter_api_key": "k",
            "chunk_seconds": "not-a-number",
            "target_languages": ["en"],
        }), encoding="utf-8")
        cfg = load_config()
        assert cfg.chunk_seconds == 4.0          # field default
        assert cfg.provider == "openrouter"
        assert cfg.openrouter_api_key == "k"
        assert cfg.target_languages == ["en"]


# ------------------------------------------------------------ monitor hot-plug


class TestMonitorHotPlug:
    def test_overlay_repositions_and_warns_when_the_configured_screen_leaves(
            self, qapp, monkeypatch, caplog):
        """The projector unplugged mid-event used to leave the band at
        coordinates of a screen that no longer existed."""
        from overlay_qt import CaptionOverlay
        cfg = AppConfig(provider="azure", azure_speech_key="k",
                        overlay=OverlayConfig(screen_name="PROJETOR-1"))
        ov = CaptionOverlay(cfg)
        try:
            calls: list[int] = []
            monkeypatch.setattr(ov, "_apply_position", lambda: calls.append(1))
            fake_screen = types.SimpleNamespace(name=lambda: "PROJETOR-1")
            with caplog.at_level(logging.WARNING):
                ov._on_screen_removed(fake_screen)
            assert calls, "the band was not repositioned"
            assert any("PROJETOR-1" in r.message for r in caplog.records), (
                "the operator was not told why the caption moved")
        finally:
            ov.close()

    def test_unrelated_screen_removal_repositions_without_warning(
            self, qapp, monkeypatch, caplog):
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(AppConfig(provider="azure", azure_speech_key="k"))
        try:
            calls: list[int] = []
            monkeypatch.setattr(ov, "_apply_position", lambda: calls.append(1))
            fake_screen = types.SimpleNamespace(name=lambda: "OUTRA-TELA")
            with caplog.at_level(logging.WARNING):
                ov._on_screen_removed(fake_screen)
            assert calls
            assert not any(r.levelno >= logging.WARNING for r in caplog.records)
        finally:
            ov.close()

    def test_the_overlay_subscribes_to_screen_changes(self):
        src = _strip_comments((REPO / "overlay_qt.py").read_text(encoding="utf-8"))
        assert "screenAdded.connect" in src
        assert "screenRemoved.connect" in src
        assert "virtualGeometryChanged.connect" in src

    def test_settings_repopulates_the_screen_combo_on_show(self, qapp, monkeypatch):
        """The combo was filled once at build time: a projector plugged in
        after the dialog was opened never appeared in it."""
        from settings_window import SettingsWindow
        win = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k"))
        try:
            calls: list[int] = []
            monkeypatch.setattr(win, "_populate_screens", lambda: calls.append(1))
            win.show()
            qapp.processEvents()
            assert calls, "the screen list was not refreshed on show"
        finally:
            win.close()

    def test_a_disconnected_configured_screen_is_kept_in_the_combo(self, qapp):
        from settings_window import SettingsWindow
        win = SettingsWindow(AppConfig(
            provider="azure", azure_speech_key="k",
            overlay=OverlayConfig(screen_name="TELA-FANTASMA")))
        try:
            win._populate_screens()
            assert win.screen_combo.findData("TELA-FANTASMA") >= 0, (
                "re-populating dropped the configured monitor instead of "
                "keeping it as 'não conectada agora'")
        finally:
            win.close()
