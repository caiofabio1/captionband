"""Quatro ideias tiradas do Sokuji (AGPL-3.0), reimplementadas — não copiadas.

1. Frase que não cabe nem no tamanho mínimo: fica o FIM, o começo sai pelo
   topo esmaecido. Antes saía o fim — justamente o que está sendo falado.
2. Estratégia de fim de frase da Azure (pausa / pontuação / teto de tempo).
3. A faixa reafirma o "sempre no topo" a cada segundo (PowerPoint em
   apresentação se põe por cima).
4. Fala nova chega com um fundo tingido que some em ~2 s.
"""
from __future__ import annotations

import sys
import time

import pytest
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

from config import AppConfig, OverlayConfig, load_config, save_config
from overlay_qt import CaptionOverlay


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _cfg(**ov) -> AppConfig:
    base = dict(split_languages=False, stable_height=True, reserved_lines=1,
                primary_font_size=25, secondary_font_size=25, padding=20, max_history=0)
    base.update(ov)
    return AppConfig(provider="azure", azure_speech_key="k", target_languages=["es"],
                     display_mode="translations_only", overlay=OverlayConfig(**base))


def _drawn(monkeypatch, overlay) -> list[tuple[int, str]]:
    rows = []
    original = CaptionOverlay._draw_line

    def spy(self, painter, text, x, top, font, fill, outline, alpha):
        rows.append((top, text))
        return original(self, painter, text, x, top, font, fill, outline, alpha)

    monkeypatch.setattr(CaptionOverlay, "_draw_line", spy)
    pm = QPixmap(overlay.size())
    p = QPainter(pm)
    overlay.render(p)
    p.end()
    return sorted(rows)


WORDS = " ".join(f"palavra{i}" for i in range(120))


class TestFraseLongaGuardaOFim:
    def test_o_fim_fica_visivel_e_o_comeco_sai_por_cima(self, qapp, monkeypatch):
        ov = CaptionOverlay(_cfg())
        ov.resize(500, ov.height())
        ov.show()
        qapp.processEvents()
        try:
            ov.push_caption(WORDS, {"es": WORDS}, detected_language="pt-BR",
                            is_final=False, result_id="r1")
            qapp.processEvents()
            rows = _drawn(monkeypatch, ov)
            visible = [(y, t) for y, t in rows if 0 <= y < ov.height()]
            assert rows[0][0] < 0, "nada transbordou: o teste não exercita o caso"
            assert any("palavra119" in t for _y, t in visible), (
                "a última palavra dita ficou fora da caixa")
            assert not any("palavra0 " in t + " " for _y, t in visible)
        finally:
            ov.close()

    def test_frase_que_cabe_continua_ancorada_como_antes(self, qapp, monkeypatch):
        ov = CaptionOverlay(_cfg())
        ov.show()
        qapp.processEvents()
        try:
            ov.push_caption("curta", {"es": "corta"}, detected_language="pt-BR",
                            is_final=True, result_id="r1")
            qapp.processEvents()
            rows = _drawn(monkeypatch, ov)
            assert rows and all(y >= ov.overlay_config.padding for y, _t in rows)
        finally:
            ov.close()

    def test_o_topo_e_esmaecido_quando_transborda(self, qapp):
        """A primeira linha visível não pode aparecer cortada ao meio, nítida."""
        ov = CaptionOverlay(_cfg(background_opacity=0.0, highlight_new_text=False))
        ov.resize(500, ov.height())
        ov.show()
        qapp.processEvents()
        try:
            ov.push_caption(WORDS, {"es": WORDS}, detected_language="pt-BR",
                            is_final=True, result_id="r1")
            qapp.processEvents()
            time.sleep(0.35)
            img = ov.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
            sx = img.width() / ov.width()
            sy = img.height() / ov.height()

            def ink(y0, y1):
                return max(img.pixelColor(x, y).alpha()
                           for y in range(int(y0 * sy), int(y1 * sy))
                           for x in range(0, int((ov.width() - 44) * sx), 2))  # sem o ×
            top = ov.overlay_config.padding // 2
            # Upper half of the fade band: glyphs there must be at most ~half
            # opaque. Without the fade they are solid (255) somewhere.
            assert ink(top + 1, top + 12) < 200
            assert ink(ov.height() // 2 - 6, ov.height() // 2 + 6) > 200
        finally:
            ov.close()


class TestFimDeFraseDaAzure:
    def test_padrao_nao_muda_nada(self):
        from providers.azure import segmentation_properties
        assert segmentation_properties("default") == []

    def test_pontuacao_e_teto(self):
        import azure.cognitiveservices.speech as speechsdk
        from constants import AZURE_SEGMENTATION_MAX_TIME_MS
        from providers.azure import segmentation_properties
        pid = speechsdk.PropertyId
        # Medido contra a Azure: com "Semantic" o reconhecedor de TRADUÇÃO não
        # devolve nada e a sessão cai com erro 1007 — a tradução não iniciava.
        assert segmentation_properties("semantic") == []
        props = dict(segmentation_properties("time"))
        assert props[pid.Speech_SegmentationStrategy] == "Time"
        # Faixa documentada pela Microsoft: 20000-70000 ms.
        assert 20000 <= int(props[pid.Speech_SegmentationMaximumTimeMs]) <= 70000
        assert AZURE_SEGMENTATION_MAX_TIME_MS == int(props[pid.Speech_SegmentationMaximumTimeMs])

    @pytest.mark.parametrize("salvo", ["xpto", "semantic"])
    def test_valor_desconhecido_ou_semantic_vira_padrao(self, salvo):
        """Quem salvou "Por pontuação" na 0.13.0 não pode continuar sem tradução."""
        from providers.azure import AzureProvider
        p = AzureProvider(speech_key="k", region="brazilsouth", source_languages=["pt-BR"],
                          target_languages=["es"], on_event=lambda *a, **k: None,
                          segmentation=salvo)
        assert p.segmentation == "default"

    def test_o_provider_recebe_a_escolha_da_config(self, monkeypatch):
        import providers
        got = {}

        class _Fake:
            def __init__(self, **kw):
                got.update(kw)
        monkeypatch.setattr("providers.azure.AzureProvider", _Fake)
        providers.build_provider(AppConfig(provider="azure", azure_speech_key="k",
                                            azure_segmentation="semantic"), lambda *a: None)
        assert got["segmentation"] == "semantic"

    def test_a_tela_salva_e_recarrega(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(AppConfig(azure_segmentation="time"))
        try:
            assert w.azure_segmentation_combo.currentData() == "time"
            assert w.azure_segmentation_combo.findData("semantic") == -1, "opção quebrada voltou à tela"
            w.azure_segmentation_combo.setCurrentIndex(w.azure_segmentation_combo.findData("default"))
            assert w._build_config().azure_segmentation == "default"
        finally:
            w.close()


class TestSempreNoTopo:
    def test_reafirma_a_cada_segundo_enquanto_visivel(self, qapp):
        ov = CaptionOverlay(_cfg())
        try:
            assert not ov._topmost_timer.isActive()
            ov.show()
            qapp.processEvents()
            assert ov._topmost_timer.isActive()
            assert ov._topmost_timer.interval() == CaptionOverlay.TOPMOST_EVERY_MS == 1000
            ov.hide()
            qapp.processEvents()
            assert not ov._topmost_timer.isActive(), "escondida não pode ficar brigando por topo"
        finally:
            ov.close()

    @pytest.mark.skipif(sys.platform != "win32", reason="SetWindowPos é do Windows")
    def test_usa_setwindowpos_sem_ativar_nem_mover(self, qapp, monkeypatch):
        import ctypes
        chamadas = []

        class _U32:
            def SetWindowPos(self, hwnd, after, x, y, cx, cy, flags):
                chamadas.append((after.value, flags))
                return 1
        monkeypatch.setattr(ctypes.windll, "user32", _U32(), raising=False)
        ov = CaptionOverlay(_cfg())
        try:
            ov.show()
            qapp.processEvents()
            ov._keep_on_top()
            assert chamadas
            after, flags = chamadas[-1]
            assert after == ctypes.c_void_p(-1).value   # HWND_TOPMOST
            assert flags & 0x10 and flags & 0x1 and flags & 0x2   # NOACTIVATE, NOSIZE, NOMOVE
        finally:
            ov.close()

    def test_falha_no_windows_nao_derruba_a_legenda(self, qapp, monkeypatch):
        import ctypes
        if sys.platform != "win32":
            pytest.skip("Windows")

        class _Quebrado:
            def SetWindowPos(self, *a):
                raise OSError("boom")
        monkeypatch.setattr(ctypes.windll, "user32", _Quebrado(), raising=False)
        ov = CaptionOverlay(_cfg())
        try:
            ov.show()
            qapp.processEvents()
            ov._keep_on_top()
            assert not ov._topmost_timer.isActive()
        finally:
            ov.close()


class TestDestaqueDaFalaNova:
    def test_comeca_forte_e_some(self, qapp, monkeypatch):
        ov = CaptionOverlay(_cfg())
        try:
            agora = [1000.0]
            monkeypatch.setattr(ov, "_now_ms", lambda: agora[0])
            ov.push_caption("nova", {"es": "nueva"}, detected_language="pt-BR",
                            is_final=False, result_id="r1")
            qapp.processEvents()
            assert ov._highlight_strength() == pytest.approx(1.0)
            agora[0] += CaptionOverlay.HIGHLIGHT_MS / 2
            assert ov._highlight_strength() == pytest.approx(0.5)
            agora[0] += CaptionOverlay.HIGHLIGHT_MS
            assert ov._highlight_strength() == 0.0
        finally:
            ov.close()

    def test_parcial_e_final_da_mesma_fala_nao_reacendem(self, qapp, monkeypatch):
        ov = CaptionOverlay(_cfg())
        try:
            agora = [1000.0]
            monkeypatch.setattr(ov, "_now_ms", lambda: agora[0])
            ov.push_caption("nova", {"es": "nueva"}, detected_language="pt-BR",
                            is_final=False, result_id="r1")
            qapp.processEvents()
            agora[0] += CaptionOverlay.HIGHLIGHT_MS + 1
            ov.push_caption("nova fala", {"es": "nueva frase"}, detected_language="pt-BR",
                            is_final=True, result_id="r2")
            qapp.processEvents()
            assert ov._highlight_strength() == 0.0
        finally:
            ov.close()

    def test_desligavel_e_tinta_segue_o_fundo(self, qapp):
        ov = CaptionOverlay(_cfg(highlight_new_text=False))
        try:
            ov.push_caption("nova", {"es": "nueva"}, detected_language="pt-BR",
                            is_final=True, result_id="r1")
            qapp.processEvents()
            assert ov._highlight_strength() == 0.0
            assert ov._highlight_tint().lightnessF() > 0.5          # fundo preto -> tinta clara
            ov.apply_config(_cfg(background_color="#FFFFFF"))
            assert ov._highlight_tint().lightnessF() < 0.5
        finally:
            ov.close()


def test_config_guarda_as_opcoes_novas(tmp_path, monkeypatch):
    import config as config_mod
    monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.json")
    cfg = AppConfig(azure_segmentation="semantic",
                    overlay=OverlayConfig(highlight_new_text=False))
    save_config(cfg)
    back = load_config()
    assert back.azure_segmentation == "semantic"
    assert back.overlay.highlight_new_text is False
