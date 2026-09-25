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


def _drawn_pt(monkeypatch, overlay, qapp) -> list[tuple[int, str, int, int]]:
    """Every line the REAL paint path puts on screen: (top_y, text, x, pt)."""
    rows: list[tuple[int, str, int, int]] = []
    original = CaptionOverlay._draw_line

    def spy(self, painter, text, x, top, font, fill, outline, alpha):
        rows.append((top, text, x, font.pointSize()))
        return original(self, painter, text, x, top, font, fill, outline, alpha)

    monkeypatch.setattr(CaptionOverlay, "_draw_line", spy)
    pixmap = QPixmap(overlay.size())
    painter = QPainter(pixmap)
    overlay.render(painter)
    painter.end()
    return sorted(rows)


def _drawn(monkeypatch, overlay, qapp) -> list[tuple[int, str, int]]:
    return [(top, text, x) for top, text, x, _pt in _drawn_pt(monkeypatch, overlay, qapp)]


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
    # 3 falas anteriores a 25 pt passam da metade da tela num notebook de
    # 864 px: a banda para no teto e mostra o que cabe, em tamanho cheio
    # (TestHistoryYieldsBeforeTheType). Até 2 tem de caber inteiro.
    @pytest.mark.parametrize("max_history", [0, 1, 2])
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


LONGA = "A primeira sessão começa agora com os resultados do estudo"   # ~2 lines at 600 px


def _push(overlay, qapp, text, rid, final=True):
    overlay.push_caption(text, {"es": f"ES {text}", "en": f"EN {text}"},
                         detected_language="pt-BR", is_final=final, result_id=rid)
    qapp.processEvents()


class TestHistoryYieldsBeforeTheType:
    """Painel de 3 modelos, 3/3: para quem lê do fundo da sala, uma legenda
    legível com menos histórico vale mais que uma completa e pequena. A
    ordem anterior encolhia a fonte até 72 % antes de descartar falas."""

    def test_older_utterances_are_dropped_and_the_size_is_kept(self, qapp, monkeypatch):
        overlay = CaptionOverlay(_cfg(max_history=2))
        overlay.resize(600, overlay.height())      # narrow: every sentence wraps
        overlay.show()
        qapp.processEvents()
        try:
            for i in range(3):
                _push(overlay, qapp, f"{LONGA} {i}", rid=f"r{i}")
            rows = _drawn_pt(monkeypatch, overlay, qapp)
            texts = {t for _y, t, _x, _pt in rows}
            assert any(t.startswith("ES ") for t in texts), "nada desenhado"
            assert len(rows) > 2, "a frase devia quebrar em várias linhas neste teste"
            assert {pt for _y, _t, _x, pt in rows} == {25}, (
                "encolheu a fonte em vez de soltar o histórico")
            shown = {t.split()[-1] for t in texts}      # the "0"/"1"/"2" suffix
            assert "2" in shown and "0" not in shown, shown
        finally:
            overlay.close()

    def test_a_shrunk_sentence_keeps_its_size_until_the_next_one(self, qapp, monkeypatch):
        """Refitting from the configured size on every partial made the type
        pulse mid-sentence. The size is held per utterance."""
        overlay = CaptionOverlay(_cfg(max_history=0, reserved_lines=1))
        overlay.resize(600, overlay.height())
        overlay.show()
        qapp.processEvents()
        try:
            _push(overlay, qapp, " ".join([LONGA] * 3), rid="r1", final=False)
            partial = {pt for *_r, pt in _drawn_pt(monkeypatch, overlay, qapp)}
            assert partial and max(partial) < 25, "a frase longa devia forçar encolher"
            _push(overlay, qapp, "curta", rid="r1", final=True)   # same utterance, rewritten
            final = {pt for *_r, pt in _drawn_pt(monkeypatch, overlay, qapp)}
            assert final == partial, f"pulsou dentro da fala: {partial} -> {final}"
            _push(overlay, qapp, "outra curta", rid="r2")
            assert {pt for *_r, pt in _drawn_pt(monkeypatch, overlay, qapp)} == {25}
        finally:
            overlay.close()


class TestLinesAreCachedImages:
    """MEDIDO na faixa real, tela a 125 %: 80 ms por pintura a 54 pt contra
    4 ms a 25 pt (orçamento de 33 ms), porque acima de ~64 px o Qt traça
    cada letra como curva a cada quadro. Uma linha é desenhada UMA vez."""

    def _count_renders(self, monkeypatch):
        calls = []
        original = CaptionOverlay._line_pixmap

        def spy(self, text, font, fill, outline):
            before = len(self._line_cache)
            pm = original(self, text, font, fill, outline)
            if len(self._line_cache) != before:
                calls.append(text)
            return pm

        monkeypatch.setattr(CaptionOverlay, "_line_pixmap", spy)
        return calls

    def test_second_paint_renders_nothing_again(self, qapp, monkeypatch):
        overlay = CaptionOverlay(_cfg(max_history=1))
        overlay.show()
        qapp.processEvents()
        try:
            renders = self._count_renders(monkeypatch)
            _push(overlay, qapp, "um", rid="r1")
            _push(overlay, qapp, "dois", rid="r2")
            _drawn(monkeypatch, overlay, qapp)
            first = list(renders)
            assert sorted(first) == ["EN dois", "EN um", "ES dois", "ES um"]
            _drawn(monkeypatch, overlay, qapp)
            assert renders == first, "repintar sem mudança renderizou de novo"
            # The new sentence, plus "dois" once more: demoted to history it
            # is a different image (normal weight, dimmer colour).
            _push(overlay, qapp, "três", rid="r3")
            _drawn(monkeypatch, overlay, qapp)
            assert sorted(renders[len(first):]) == ["EN dois", "EN três", "ES dois", "ES três"]
            n = len(renders)
            _drawn(monkeypatch, overlay, qapp)
            assert len(renders) == n
        finally:
            overlay.close()

    def test_outline_only_over_a_translucent_background(self, qapp, monkeypatch):
        """Sobre fundo sólido o contorno é invisível e custa 9 drawText por
        linha em vez de 1 — quase todo o custo de renderizar a 54 pt."""
        outlined = []
        monkeypatch.setattr(
            CaptionOverlay, "_draw_outlined_text",
            staticmethod(lambda p, text, *a: outlined.append(text)))
        for opacity, expect in ((0.85, 0), (0.3, 2)):
            outlined.clear()
            overlay = CaptionOverlay(_cfg(max_history=0, background_opacity=opacity))
            overlay.show()
            qapp.processEvents()
            try:
                _push(overlay, qapp, "um", rid="r1")
                _drawn(monkeypatch, overlay, qapp)
                assert len(outlined) == expect, (opacity, outlined)
            finally:
                overlay.close()

    def test_the_cache_is_bounded(self, qapp, monkeypatch):
        overlay = CaptionOverlay(_cfg(max_history=0))
        overlay.show()
        qapp.processEvents()
        try:
            for i in range(CaptionOverlay.LINE_CACHE_MAX):
                _push(overlay, qapp, f"frase {i}", rid=f"r{i}")
                _drawn(monkeypatch, overlay, qapp)
            assert len(overlay._line_cache) == CaptionOverlay.LINE_CACHE_MAX
        finally:
            overlay.close()


class _CharMetrics:
    def horizontalAdvance(self, s):
        return len(s)


class TestNoDanglingLittleWord:
    def _wrap(self, text, width):
        return CaptionOverlay._wrap_lines(text, _CharMetrics(), width)

    def test_article_moves_down_with_its_noun(self):
        assert self._wrap("começa agora com o estudo", 18) == ["começa agora", "com o estudo"]
        assert self._wrap("starts now with the study", 19) == ["starts now", "with the study"]

    def test_a_run_of_little_words_moves_together(self):
        """Seen on the real band: moving only "los" left "con" dangling."""
        assert self._wrap("empieza ahora con los resultados", 22) == [
            "empieza ahora", "con los resultados"]

    def test_other_words_break_greedily_as_before(self):
        assert self._wrap("começa agora sim estudo hoje", 18) == ["começa agora sim", "estudo hoje"]

    def test_a_line_is_never_emptied_and_punctuation_is_a_pause(self):
        assert self._wrap("o estudooooooooooo", 5) == ["o", "estudooooooooooo"]
        assert self._wrap("vem com, estudo", 8) == ["vem com,", "estudo"]
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


class TestHotkeyCycleAdvancesOnePerPress:
    """F9 is a STEP, not a destination.

    MEASURED on the installed exe: four presses produced only two swaps
    (auto -> pt -> en) and the cycle never came back to auto-detect, because
    every press computed "the next language" from the LIVE config — which has
    not moved while a swap is still in flight. So presses 2, 3 and 4 all
    resolved to the same target and collapsed into one move.
    """

    class _Ctrl:
        def __init__(self):
            self.pending = False
            self.live = None
            self.asked: list = []

        def source_mode(self):
            return self.live

        def source_switch_pending(self):
            return self.pending

    def _tray(self, quick):
        import translator as T

        tray = T.TrayApp.__new__(T.TrayApp)          # sem Qt: só a lógica
        tray.config = AppConfig(provider="azure", azure_speech_key="k",
                                azure_quick_languages=quick)
        tray.controller = self._Ctrl()
        tray._pending_language = None
        tray._language_cycle_requested = type(
            "S", (), {"emit": lambda self: None})()
        return tray

    def test_four_presses_walk_the_whole_cycle_even_while_busy(self):
        tray = self._tray(["pt-BR", "en-US", "es-ES"])
        tray.controller.pending = True                # troca sempre em curso
        targets = []
        for _ in range(4):
            tray.cycle_source_language()
            targets.append(tray._pending_language)
        assert targets == ["pt-BR", "en-US", "es-ES", None], (
            f"o ciclo colapsou: {targets}")

    def test_when_idle_the_step_comes_from_the_live_state(self):
        tray = self._tray(["pt-BR", "en-US"])
        tray.controller.pending = False
        tray.controller.live = "pt-BR"
        tray.cycle_source_language()
        assert tray._pending_language == "en-US"
