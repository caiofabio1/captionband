"""Two defects the operator reported after a real event, both reproduced.

1. "Recursos de mostrar legendas passadas não funciona."
   MEASURED: with max_history at 2, 3 and 5 the overlay STORED 3, 4 and 6
   utterances and DREW exactly 4 lines every time. The fixed band was sized
   from `reserved_lines` alone, and the fit loop dropped every history line
   that did not fit — so the "Linhas anteriores visíveis" spin was a no-op
   above 1 and nothing the operator changed had any effect.

2. "Quando mudamos de dividida em 2 caixas para unificada, as letras misturam."
   MEASURED: in the unified band Spanish and English were drawn in the SAME
   colour (#ffffff), the SAME size and the same weight. The only thing telling
   them apart was a 4 px bar at the left edge, invisible from the back of a
   room. In split mode each box holds one language, so the problem appears
   exactly when the boxes are merged.

Plus the hardening that this session demonstrated the need for: a single bad
tuple unpack inside paintEvent killed the whole process with no traceback,
which is the same silent death reported from the event floor.
"""
from __future__ import annotations

import time

import pytest
from PyQt6.QtGui import QFontMetrics, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from config import AppConfig, OverlayConfig
from overlay_qt import CaptionOverlay


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _cfg(**overlay_kw) -> AppConfig:
    base = dict(split_languages=False, stable_height=True, reserved_lines=3,
                primary_font_size=25, secondary_font_size=25, padding=20,
                concat_gap_ms=1000, max_history=1)
    base.update(overlay_kw)
    return AppConfig(provider="azure", azure_speech_key="k",
                     target_languages=["es", "en"],
                     display_mode="translations_only_multi",
                     overlay=OverlayConfig(**base))


def _drawn(monkeypatch, overlay, qapp) -> list[tuple[int, str, int]]:
    """Every line the REAL paint path puts on screen: (top_y, text, x)."""
    rows: list[tuple[int, str, int]] = []
    original = CaptionOverlay._draw_outlined_text

    def spy(painter, text, x, y, fill, outline, font=None, *a, **k):
        metrics = QFontMetrics(font if font is not None else painter.font())
        rows.append((y - metrics.ascent(), text, x))
        return original(painter, text, x, y, fill, outline, font, *a, **k)

    monkeypatch.setattr(CaptionOverlay, "_draw_outlined_text", staticmethod(spy))
    pixmap = QPixmap(overlay.size())
    painter = QPainter(pixmap)
    overlay.render(painter)
    painter.end()
    return sorted(rows)


def _speak(overlay, qapp, sentences) -> None:
    """Push captions far enough apart that concat_gap does not merge them."""
    for i, text in enumerate(sentences):
        overlay.push_caption(
            text, {"es": f"ES {text}", "en": f"EN {text}"},
            detected_language="pt-BR", is_final=True)
        qapp.processEvents()
        if i < len(sentences) - 1:
            time.sleep(1.2)


class TestPastCaptionsActuallyAppear:
    @pytest.mark.parametrize("max_history", [0, 1, 2, 3])
    def test_the_band_grows_to_fit_the_history_it_promises(
            self, qapp, monkeypatch, max_history):
        """Storing the history is not the same as showing it.

        Before the fix this drew 4 lines for every value above 1, because the
        band height came from reserved_lines alone and the fit loop discarded
        whatever did not fit.
        """
        overlay = CaptionOverlay(_cfg(max_history=max_history))
        overlay.resize(1470, overlay.height())
        overlay.show()
        qapp.processEvents()
        try:
            _speak(overlay, qapp, [f"f{i}" for i in range(max_history + 1)])
            rows = _drawn(monkeypatch, overlay, qapp)
            expected = (max_history + 1) * 2      # utterances x languages
            assert len(rows) == expected, (
                f"max_history={max_history} devia desenhar {expected} linhas, "
                f"desenhou {len(rows)}: {[t for _y, t, _x in rows]}")
        finally:
            overlay.close()

    def test_reserved_lines_can_still_make_the_band_taller(self, qapp):
        """The height spin stays useful: it is a floor, not a cap."""
        short = CaptionOverlay(_cfg(max_history=1, reserved_lines=3))
        tall = CaptionOverlay(_cfg(max_history=1, reserved_lines=10))
        try:
            assert tall.height() > short.height()
        finally:
            short.close()
            tall.close()


class TestLanguagesAreTellableApart:
    def test_utterances_are_separated_more_than_their_own_languages(
            self, qapp, monkeypatch):
        """ES/EN of ONE sentence belong together; sentences do not.

        Without this the band was ES/EN/ES/EN evenly spaced, and the reader
        could not tell where one sentence ended — reported as "as letras
        misturam" right after merging the two boxes into one.
        """
        overlay = CaptionOverlay(_cfg(max_history=2))
        overlay.resize(1470, overlay.height())
        overlay.show()
        qapp.processEvents()
        try:
            _speak(overlay, qapp, ["um", "dois", "tres"])
            rows = _drawn(monkeypatch, overlay, qapp)
            assert len(rows) == 6
            gaps = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
            within = [gaps[0], gaps[2], gaps[4]]     # ES -> EN, same sentence
            between = [gaps[1], gaps[3]]             # EN -> ES, next sentence
            assert min(between) > max(within), (
                f"falas nao estao agrupadas: dentro={within} entre={between}")
        finally:
            overlay.close()

    def test_the_language_bar_is_visible_from_a_distance(self):
        """4 px on a projected band is not a language indicator."""
        assert CaptionOverlay.LANG_BAR_PX >= 10
        assert CaptionOverlay.LANG_BAR_PX_PRESENTATION > CaptionOverlay.LANG_BAR_PX

    def test_each_target_language_gets_its_own_bar_colour(self, qapp):
        overlay = CaptionOverlay(_cfg())
        try:
            overlay.push_caption("ola", {"es": "hola", "en": "hello"},
                                 detected_language="pt-BR", is_final=True)
            colours = [c for _p, _t, _a, c, _u in overlay._compose_lines_with_lang()]
            assert len(set(colours)) == 2, (
                f"espanhol e ingles com a mesma cor de barra: {colours}")
        finally:
            overlay.close()

    def test_text_starts_clear_of_the_bar(self, qapp, monkeypatch):
        overlay = CaptionOverlay(_cfg())
        overlay.resize(1470, overlay.height())
        overlay.show()
        qapp.processEvents()
        try:
            _speak(overlay, qapp, ["ola"])
            rows = _drawn(monkeypatch, overlay, qapp)
            assert rows, "nada foi desenhado"
            bar_right = CaptionOverlay._bar_x(overlay.overlay_config,
                                              CaptionOverlay.LANG_BAR_PX) \
                + CaptionOverlay.LANG_BAR_PX
            assert min(x for _y, _t, x in rows) >= bar_right, (
                "o texto encosta na barra de idioma")
        finally:
            overlay.close()


class TestPaintFailureIsNotFatal:
    def test_a_broken_paint_costs_a_frame_not_the_session(self, qapp, monkeypatch):
        """PyQt turns an exception in a slot into abort().

        This is not hypothetical: writing the changes above, one wrong tuple
        unpack inside paintEvent killed the interpreter with no traceback
        anywhere — indistinguishable from the crashes reported on site. The
        band may go blank; the event may not end.
        """
        overlay = CaptionOverlay(_cfg())
        overlay.resize(900, overlay.height())
        overlay.show()
        qapp.processEvents()
        try:
            overlay.push_caption("ola", {"es": "hola", "en": "hello"},
                                 detected_language="pt-BR", is_final=True)
            qapp.processEvents()

            def boom(self, event):
                raise ValueError("defeito injetado no caminho de pintura")

            monkeypatch.setattr(CaptionOverlay, "_paint", boom)
            pixmap = QPixmap(overlay.size())
            painter = QPainter(pixmap)
            overlay.render(painter)          # would abort() without the guard
            painter.end()
            assert overlay._paint_failed is True
        finally:
            overlay.close()

    def test_the_failure_is_logged_once_not_every_frame(self, qapp, monkeypatch, caplog):
        overlay = CaptionOverlay(_cfg())
        overlay.resize(900, overlay.height())
        overlay.show()
        qapp.processEvents()
        try:
            def boom(self, event):
                raise ValueError("defeito")

            monkeypatch.setattr(CaptionOverlay, "_paint", boom)
            with caplog.at_level("ERROR"):
                for _ in range(5):
                    pixmap = QPixmap(overlay.size())
                    painter = QPainter(pixmap)
                    overlay.render(painter)
                    painter.end()
            failures = [r for r in caplog.records if "paint failed" in r.message]
            assert len(failures) == 1, (
                f"uma pintura quebrada repete 15x por segundo; "
                f"logou {len(failures)} vezes")
        finally:
            overlay.close()
