"""As duas caixas nunca podem cair no mesmo lugar.

Reportado pelo operador: "Quando configuro as 2 caixas so aparece 1 e
empilhada". Medido em 2026-09-18: a janela de Configuracoes oferecia as NOVE
combinacoes de (Posicao da legenda, Posicao inicial da 2a caixa) e as tres da
diagonal punham as duas janelas em geometria identica -- sobreposicao de 100%.
Na tela isso nao le como "duas caixas no mesmo lugar", le como UMA caixa com
os idiomas empilhados, que e exatamente o modo que a outra opcao do mesmo
combo produz de proposito. Nada no app avisava.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtWidgets import QApplication

from config import AppConfig, OverlayConfig
from overlay_qt import CaptionOverlay
from translator import split_overlay_configs

POSICOES = ("top", "middle", "bottom")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _cfg(position: str, second_position: str) -> AppConfig:
    return AppConfig(
        provider="azure", azure_speech_key="k", target_languages=["es", "en"],
        display_mode="translations_only_multi",
        overlay=OverlayConfig(split_languages=True, position=position,
                              second_position=second_position,
                              primary_font_size=25, secondary_font_size=25))


class TestAsDuasBandasNuncaSeSobrepoem:
    @pytest.mark.parametrize("p1", POSICOES)
    @pytest.mark.parametrize("p2", POSICOES)
    def test_geometrias_disjuntas_nas_nove_combinacoes(self, qapp, p1, p2):
        first, second = split_overlay_configs(_cfg(p1, p2))
        assert second is not None
        b1, b2 = CaptionOverlay(first), CaptionOverlay(second)
        try:
            g1, g2 = b1.geometry(), b2.geometry()
            assert not g1.intersects(g2), (
                f"1a={p1} 2a={p2}: bandas sobrepostas "
                f"(y={g1.y()} e y={g2.y()}) -- na tela parece uma caixa so")
        finally:
            b1.close()
            b2.close()

    @pytest.mark.parametrize("pos", POSICOES)
    def test_a_2a_caixa_sai_do_lugar_da_1a_nao_o_contrario(self, qapp, pos):
        """Quem manda na posicao e a banda principal; a 2a e que cede."""
        first, second = split_overlay_configs(_cfg(pos, pos))
        assert first.overlay.position == pos
        assert second.overlay.position != pos

    def test_escolha_valida_do_operador_e_respeitada(self, qapp):
        first, second = split_overlay_configs(_cfg("bottom", "top"))
        assert (first.overlay.position, second.overlay.position) == ("bottom", "top")
        first, second = split_overlay_configs(_cfg("bottom", "middle"))
        assert (first.overlay.position, second.overlay.position) == ("bottom", "middle")


class TestATelaNaoOfereceOConflito:
    def test_a_posicao_da_1a_banda_fica_desabilitada_na_lista_da_2a(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(_cfg("bottom", "top"))
        try:
            w.position_combo.setCurrentIndex(w.position_combo.findData("top"))
            model = w.second_position_combo.model()
            estado = {w.second_position_combo.itemData(i): model.item(i).isEnabled()
                      for i in range(w.second_position_combo.count())}
            assert estado["top"] is False, f"topo continua oferecido: {estado}"
            assert estado["bottom"] is True
            assert w.second_position_combo.currentData() != "top", (
                "a 2a caixa ficou selecionada na posicao da 1a")
        finally:
            w.close()

    def test_config_que_ja_vem_conflitante_e_corrigida_ao_abrir(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(_cfg("bottom", "bottom"))
        try:
            assert w.second_position_combo.currentData() != "bottom"
        finally:
            w.close()


class TestUmaDecisaoUmaTela:
    """A posicao da legenda nao mora em duas abas.

    Apontado pelo operador: "Aparencia e posicao, ambas manipulando a posicao
    da legenda. Para a legenda dividida a configuracao legenda no topo nem faz
    sentido." Enquanto `position` ficava em Aparencia e `second_position` em
    Layout/Projecao, dava para escolher a mesma posicao para as duas caixas sem
    nunca ver as duas escolhas na mesma tela.
    """

    def _aba_de(self, win, widget):
        from PyQt6.QtWidgets import QWidget
        for i in range(win.tabs.count()):
            pilha = [win.tabs.widget(i)]
            while pilha:
                cur = pilha.pop()
                if cur is widget:
                    return win.tabs.tabText(i)
                pilha.extend(c for c in cur.children() if isinstance(c, QWidget))
        return None

    def test_as_duas_posicoes_vivem_na_mesma_aba(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(_cfg("bottom", "top"))
        try:
            a = self._aba_de(w, w.position_combo)
            b = self._aba_de(w, w.second_position_combo)
            assert a is not None and a == b, (
                f"posicao da 1a esta em {a!r} e da 2a em {b!r}")
            assert a == "Legenda"
        finally:
            w.close()

    def test_o_rotulo_diz_1a_caixa_quando_ha_duas(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(_cfg("bottom", "top"))
        try:
            w.layout_combo.setCurrentIndex(w.layout_combo.findData("split"))
            assert "1ª caixa" in w.position_label.text(), w.position_label.text()
            assert w.second_position_combo.isEnabled()

            w.layout_combo.setCurrentIndex(w.layout_combo.findData("stacked"))
            assert w.position_label.text() == "Posição da legenda:"
            assert not w.second_position_combo.isEnabled()
        finally:
            w.close()
