"""Regressions from the stability/UX review (2026-09-15).

Each test names the defect it pins down; the fix is in the module under test.
"""
from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

import config as config_mod
from config import AppConfig, OverlayConfig


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_app_dir(monkeypatch, tmp_path):
    import transcript as transcript_mod
    monkeypatch.setattr(config_mod, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(transcript_mod, "app_data_dir", lambda: tmp_path)
    return tmp_path


class TestTranscriptTwoTargets:
    def test_each_target_gets_its_own_srt(self, isolated_app_dir):
        from transcript import TranscriptWriter
        w = TranscriptWriter(provider_name="azure", target_languages=["en", "es"])
        w.start()
        w.append("Bom dia", {"en": "Good morning", "es": "Buenos días"}, "pt-BR", True)
        w.stop()

        en = w.srt_path.read_text(encoding="utf-8")
        es = w.srt_path_for("es").read_text(encoding="utf-8")
        assert "Good morning" in en and "Buenos días" not in en
        assert "Buenos días" in es and "Good morning" not in es
        assert w.srt_path_for("en") == w.srt_path

    def test_srt_is_checkpointed_before_stop(self, isolated_app_dir, monkeypatch):
        import transcript as transcript_mod
        from transcript import TranscriptWriter
        monkeypatch.setattr(transcript_mod, "TRANSCRIPT_SRT_CHECKPOINT_EVERY", 2)
        w = TranscriptWriter(provider_name="azure", target_languages=["es"])
        w.start()
        w.append("um", {"es": "uno"}, "pt-BR", True)
        assert not w.srt_path.exists()
        w.append("dois", {"es": "dos"}, "pt-BR", True)
        # A crash right here must still leave a usable subtitle file.
        assert w.srt_path.exists()
        assert "dos" in w.srt_path.read_text(encoding="utf-8")
        w.stop()


class TestOverlayTwoTargets:
    @pytest.fixture
    def overlay(self, qapp):
        from overlay_qt import CaptionOverlay
        cfg = AppConfig(
            provider="azure", azure_speech_key="k",
            target_languages=["en", "es"],
            display_mode="original_plus_translation",
        )
        ov = CaptionOverlay(cfg)
        yield ov
        ov.close()

    def test_original_plus_translation_shows_every_target(self, overlay):
        overlay._on_update("Bom dia", {"en": "Good morning", "es": "Buenos días"},
                           "pt-BR", True, 0.0, "rid-1")
        texts = [t for _p, t, _a, _c in overlay._compose_lines_with_lang()]
        assert "Good morning" in texts
        assert "Buenos días" in texts, "second target was silently dropped"

    def test_bilingual_mode_gives_both_languages_equal_weight(self, overlay):
        from overlay_qt import _color_for_language
        overlay.display_mode = "translations_only_multi"
        overlay._on_update("Bom dia", {"en": "Good morning", "es": "Buenos días"},
                           "pt-BR", True, 0.0, "rid-1")
        lines = overlay._compose_lines_with_lang()
        assert [t for _p, t, _a, _c in lines] == ["Good morning", "Buenos días"]
        # Same size, same brightness; accent bar keyed to the TARGET language.
        assert all(is_primary for is_primary, _t, _a, _c in lines)
        assert len({a for _p, _t, a, _c in lines}) == 1
        assert [c for _p, _t, _a, c in lines] == [
            _color_for_language("en"), _color_for_language("es")]

    def test_presentation_mode_hides_operator_widgets(self, overlay):
        overlay.set_presentation(True)
        assert not overlay._close_btn.isVisibleTo(overlay)
        overlay.set_presentation(False)
        assert overlay._close_btn.isVisibleTo(overlay)

    def test_configured_screen_falls_back_to_primary_when_gone(self, overlay):
        from PyQt6.QtGui import QGuiApplication
        overlay.overlay_config = OverlayConfig(screen_name="\\\\.\\PROJECTOR_THAT_LEFT")
        assert overlay._screen() is QGuiApplication.primaryScreen()


class TestSettingsPreserveUneditedFields:
    def test_save_keeps_fallback_and_overlay_flags(self, qapp):
        from settings_window import SettingsWindow
        cfg = AppConfig(
            provider="azure", azure_speech_key="k", groq_api_key="g",
            fallback_providers=["groq"],
            overlay=OverlayConfig(stable_height=False, reserved_lines=5,
                                  screen_name="DISPLAY2"),
        )
        win = SettingsWindow(cfg)
        try:
            built = win._build_config()
        finally:
            win.close()
        # Before: a fresh AppConfig(...) reset all of these to their defaults.
        assert built.fallback_providers == ["groq"]
        assert built.overlay.stable_height is False
        assert built.overlay.reserved_lines == 5
        assert built.overlay.screen_name == "DISPLAY2"

    def test_layout_and_projection_options_are_in_the_window(self, qapp):
        """The operator asked where the two-box option was: it existed only in
        the tray menu, and the second box's position nowhere at all."""
        from settings_window import SettingsWindow
        cfg = AppConfig(provider="azure", azure_speech_key="k", target_languages=["en", "es"],
                        overlay=OverlayConfig(split_languages=True, second_position="middle",
                                              screen_name="", stable_height=False, reserved_lines=5))
        win = SettingsWindow(cfg)
        try:
            # Loaded from config…
            assert win.layout_combo.currentData() == "split"
            assert win.second_position_combo.currentData() == "middle"
            assert win.stable_height_check.isChecked() is False
            assert win.reserved_lines_spin.value() == 5
            # …and edits round-trip into the built config.
            win.layout_combo.setCurrentIndex(win.layout_combo.findData("stacked"))
            win.stable_height_check.setChecked(True)
            win.reserved_lines_spin.setValue(4)
            built = win._build_config()
            assert built.overlay.split_languages is False
            assert built.overlay.stable_height is True
            assert built.overlay.reserved_lines == 4
            assert built.overlay.second_position == "middle"
        finally:
            win.close()

    def test_two_box_preview_opens_two_bands(self, qapp):
        from settings_window import SettingsWindow
        from overlay_qt import CaptionOverlay
        win = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k",
                                       target_languages=["en", "es"],
                                       overlay=OverlayConfig(split_languages=True)))
        try:
            win.show()
            win._on_preview()
            previews = [p for p in win.findChildren(CaptionOverlay) if p.isVisible()]
            assert len(previews) == 2
            assert {tuple(p.app_config.target_languages) for p in previews} == {("en",), ("es",)}
            win.hide()
            assert not any(p.isVisible() for p in previews)
        finally:
            win.close()

    def test_fallback_combo_round_trips_to_config(self, qapp):
        from settings_window import SettingsWindow
        win = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k"))
        try:
            idx = win.fallback_combo.findData("groq")
            assert idx > 0
            win.fallback_combo.setCurrentIndex(idx)
            assert win._build_config().fallback_providers == ["groq"]
            win.fallback_combo.setCurrentIndex(0)
            assert win._build_config().fallback_providers == []
        finally:
            win.close()


class TestSettingsPreviewIsOneWindowThatCloses:
    def test_preview_reuses_one_overlay_and_dies_with_the_dialog(self, qapp):
        from settings_window import SettingsWindow
        from overlay_qt import CaptionOverlay
        win = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k",
                                       target_languages=["en", "es"]))
        try:
            win.show()
            win._on_preview()
            win._on_preview()
            win._on_preview()
            previews = win.findChildren(CaptionOverlay)
            assert len(previews) == 1, "each click used to open another band"
            assert previews[0].isVisible()
            # The band shows every configured target, not a hard-coded es.
            qapp.processEvents()
            assert set(previews[0]._history[-1].translations) == {"en", "es"}
            win.hide()
            assert not previews[0].isVisible(), "preview outlived the dialog"
        finally:
            win.close()

    def test_overlay_hides_itself_when_idle_clears(self, qapp):
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(AppConfig(provider="azure", azure_speech_key="k"))
        try:
            ov.show()
            ov._on_update("Olá", {"es": "Hola"}, "pt-BR", True, 0.0, "")
            assert ov.isVisible()
            ov._clear_all()
            assert not ov.isVisible(), "an empty band stayed on the projection"
        finally:
            ov.close()


class TestTwoBoxBilingual:
    """One output language per window: EN in the bottom band, ES in a second
    band at the top (or wherever it is dragged). The controller still gets
    the full config — the provider keeps translating to both."""

    def test_split_narrows_each_window_but_not_the_controller(self):
        from translator import split_overlay_configs
        cfg = AppConfig(provider="azure", azure_speech_key="k",
                        target_languages=["en", "es"], display_mode="translations_only_multi",
                        overlay=OverlayConfig(split_languages=True, second_position="top",
                                              position="bottom"))
        first, second = split_overlay_configs(cfg)
        assert first.target_languages == ["en"] and first.overlay.position == "bottom"
        assert second is not None
        assert second.target_languages == ["es"] and second.overlay.position == "top"
        assert cfg.target_languages == ["en", "es"], "the controller's config must not be narrowed"

    def test_split_keeps_the_spoken_line_only_in_the_first_box(self):
        from translator import split_overlay_configs
        cfg = AppConfig(provider="azure", azure_speech_key="k",
                        target_languages=["en", "es"], display_mode="original_plus_translation",
                        overlay=OverlayConfig(split_languages=True))
        first, second = split_overlay_configs(cfg)
        assert first.display_mode == "original_plus_translation"
        assert second.display_mode == "translations_only"

    def test_split_is_a_no_op_with_one_target_or_when_off(self):
        from translator import split_overlay_configs
        one = AppConfig(provider="azure", azure_speech_key="k", target_languages=["es"],
                        overlay=OverlayConfig(split_languages=True))
        assert split_overlay_configs(one) == (one, None)
        off = AppConfig(provider="azure", azure_speech_key="k", target_languages=["en", "es"])
        assert split_overlay_configs(off) == (off, None)


class TestAudioMeterNeverTouchesWidgetsOffThread:
    """crash.log, 2026-09-15, three times: 'Fatal Python error: Aborted' with
    the main thread idle in QApplication.exec(). The audio-test meter updated
    a QProgressBar straight from the capture thread."""

    def test_capture_callback_updates_meter_via_queued_signal(self, qapp, monkeypatch):
        import threading, time
        import settings_window as S
        from settings_window import SettingsWindow

        captured: dict = {}

        class FakeCapture:
            def __init__(self, on_audio, **kw):
                captured["on_audio"] = on_audio
            def start(self): pass
            def stop(self): pass

        import audio_capture
        monkeypatch.setattr(audio_capture, "AudioCapture", FakeCapture)
        monkeypatch.setattr(audio_capture, "find_device", lambda name: object())
        monkeypatch.setattr(S.QMessageBox, "warning", lambda *a, **k: None)
        monkeypatch.setattr(S.QMessageBox, "information", lambda *a, **k: None)

        win = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k"))
        try:
            win._on_test_capture()
            on_audio = captured["on_audio"]
            gui_thread = threading.get_ident()
            loud = (b"\x00\x40" * 800)          # ~0.5 amplitude → meter near 100
            # Deliver from a worker thread, like soundcard does.
            t = threading.Thread(target=lambda: on_audio(loud))
            t.start(); t.join()
            # The widget must NOT have been touched yet: the update is queued…
            assert win.level_bar.value() == 0
            end = time.monotonic() + 0.5
            while time.monotonic() < end and win.level_bar.value() == 0:
                qapp.processEvents(); time.sleep(0.005)
            # …and lands on the GUI thread once the event loop runs.
            assert win.level_bar.value() > 50
            assert threading.get_ident() == gui_thread
        finally:
            win.close()

    def test_no_widget_access_inside_capture_callback(self):
        """Static guard: the nested on_audio() may not reference self.<widget>."""
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "settings_window.py").read_text(encoding="utf-8")
        m = re.search(r"def on_audio\(data: bytes\) -> None:\n(.*?)\n\n", src, flags=re.S)
        assert m, "on_audio not found"
        body = "\n".join(l for l in m.group(1).splitlines() if not l.strip().startswith("#"))
        assert "level_bar" not in body and ".setValue(" not in body, body


class TestTrayMenuActionsHaveParents:
    """A QAction created without a Qt parent and held only by a local Python
    name is garbage-collected — and silently removed from the menu. Measured:
    QMenu().addAction(QAction("x")); gc.collect() → 0 actions. In the frozen
    exe this emptied "Configurações", "Sair" and both dynamic submenus: the
    operator could not change anything mid-event."""

    def test_every_tray_qaction_names_a_parent(self):
        import re
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "translator.py"
        text = src.read_text(encoding="utf-8")
        calls = re.findall(r"QAction\((?:[^()]|\([^()]*\))*\)", text, flags=re.S)
        assert calls, "no QAction found — did the tray move?"
        orphans = [c for c in calls if not re.search(r",\s*(menu|self\.lang_menu|self\.screen_menu)\)$", c)
                   and c != "QAction(menu)"]
        assert not orphans, orphans


class TestPreflightFallbackStep:
    def test_warns_when_no_reserve_is_configured(self):
        import preflight
        step = preflight.check_fallback(AppConfig(provider="azure", azure_speech_key="k"))
        assert not step.ok and not step.fatal
        assert "reserva" in step.detail.lower()

    def test_ok_when_reserve_has_credentials(self):
        import preflight
        step = preflight.check_fallback(AppConfig(
            provider="azure", azure_speech_key="k",
            groq_api_key="g", fallback_providers=["groq"]))
        assert step.ok

    def test_warns_when_reserve_has_no_credentials(self):
        import preflight
        step = preflight.check_fallback(AppConfig(
            provider="azure", azure_speech_key="k", fallback_providers=["groq"]))
        assert not step.ok and "credencial" in step.detail
