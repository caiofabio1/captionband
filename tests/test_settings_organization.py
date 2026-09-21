"""Cada decisão numa tela só.

Da auditoria de 2026-09-18 (`docs/AUDITORIA-CONFIGURACOES.md`) e das quatro
decisões que o operador tomou em cima dela.

O que estava errado não era só desarrumação. Quatro campos decidiam a ALTURA
da banda e estavam em três abas: `display_mode` em Idiomas, `max_history` em
Aparência, `reserved_lines` e `stable_height` em Layout. Duas dessas abas
disputavam o mesmo número — e até `e56e03a` a de Aparência perdia em silêncio,
com `max_history` acima de 1 não tendo efeito nenhum. A aba Layout carregava
uma frase pedindo desculpa pela divisão, e a de Idiomas outra apontando de
volta; texto de ajuda que explica onde está o outro controle é o sintoma.

Estes testes travam a organização, não a aparência: eles falham se um controle
voltar para a aba errada ou aparecer em duas.
"""
from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

import config
from config import AppConfig, OverlayConfig

# Aparência = como a banda se PARECE. Legenda = o que ela mostra, onde e
# quanto. A fronteira é essa frase; se um controle novo não couber nela,
# provavelmente é porque a fronteira precisa mudar de propósito, e então este
# teste muda junto — deliberadamente, não por acidente.
APARENCIA = {
    "opacity_slider", "width_slider", "primary_size", "secondary_size",
    "primary_color_btn", "secondary_color_btn", "outline_color_btn",
    "bg_color_btn", "font_family_combo", "padding_spin",
}
LEGENDA = {
    "mode_combo", "layout_combo", "position_combo", "second_position_combo",
    "screen_combo", "stable_height_check", "reserved_lines_spin",
    "max_history_spin", "max_chars_spin", "concat_gap_spin",
    "click_through_check",
}
IDIOMAS = {"sources_list", "targets_list"}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp):
    from settings_window import SettingsWindow
    w = SettingsWindow(AppConfig(
        provider="azure", azure_speech_key="k", target_languages=["es", "en"],
        display_mode="translations_only_multi",
        overlay=OverlayConfig(split_languages=True, position="bottom",
                              second_position="top")))
    yield w
    w.close()


def _controles_por_aba(w) -> dict[str, set[str]]:
    nomes = {id(o): n for n, o in vars(w).items() if isinstance(o, QWidget)}
    fora = {}
    for i in range(w.tabs.count()):
        aba = w.tabs.tabText(i)
        pilha = [w.tabs.widget(i)]
        while pilha:
            cur = pilha.pop()
            n = nomes.get(id(cur))
            if n:
                fora.setdefault(aba, set()).add(n)
            pilha.extend(c for c in cur.children() if isinstance(c, QWidget))
    return fora


class TestCadaControleNumaAbaSo:
    def test_nenhum_controle_aparece_em_duas_abas(self, win):
        abas = _controles_por_aba(win)
        onde: dict[str, list[str]] = {}
        for aba, ws in abas.items():
            for x in ws:
                onde.setdefault(x, []).append(aba)
        dobrados = {k: v for k, v in onde.items() if len(v) > 1}
        assert not dobrados, f"controle em mais de uma aba: {dobrados}"

    def test_aparencia_so_tem_aparencia(self, win):
        ws = _controles_por_aba(win).get("Aparência", set())
        intrusos = ws & LEGENDA
        assert not intrusos, (
            f"decisão de legenda na aba Aparência: {sorted(intrusos)}")
        assert APARENCIA <= ws, f"faltou em Aparência: {sorted(APARENCIA - ws)}"

    def test_a_aba_legenda_tem_tudo_que_decide_a_legenda(self, win):
        ws = _controles_por_aba(win).get("Legenda", set())
        assert LEGENDA <= ws, f"faltou na aba Legenda: {sorted(LEGENDA - ws)}"

    def test_idiomas_so_escolhe_idiomas(self, win):
        ws = _controles_por_aba(win).get("Idiomas", set())
        assert IDIOMAS <= ws
        assert "mode_combo" not in ws, (
            "'Como mostrar' decide quantas linhas cada fala ocupa; ele pertence "
            "à aba Legenda, junto da altura que ele determina")

    def test_a_altura_da_banda_e_decidida_numa_aba_so(self, win):
        """Os quatro campos que definem a altura, no mesmo lugar."""
        abas = _controles_por_aba(win)
        quem = {c: aba for aba, ws in abas.items() for c in ws
                if c in ("mode_combo", "max_history_spin",
                         "reserved_lines_spin", "stable_height_check")}
        assert len(set(quem.values())) == 1, f"altura decidida em varias abas: {quem}"


class TestOsRotulosNaoMentem:
    def test_a_altura_se_chama_minima_porque_e_piso(self, win):
        from PyQt6.QtWidgets import QLabel
        rotulos = [x.text() for x in win.findChildren(QLabel)]
        assert any("Altura mínima" in r for r in rotulos), (
            "o rotulo ainda promete ser a altura final")
        assert not any("Altura da banda fixa:" == r for r in rotulos)

    def test_nenhuma_aba_manda_o_operador_procurar_na_outra(self, win):
        """Ajuda que diz onde está o outro controle é sintoma de divisão."""
        from PyQt6.QtWidgets import QLabel
        for aba in ("Legenda", "Idiomas"):
            i = next(j for j in range(win.tabs.count())
                     if win.tabs.tabText(j) == aba)
            textos = " ".join(x.text() for x in win.tabs.widget(i).findChildren(QLabel))
            assert "fica em Idiomas" not in textos
            assert "fica em Aparência" not in textos


class TestCortarFalaLonga:
    """`max_chars` corta a fala; nao quebra linha.

    A primeira versao deste bloco (e da auditoria) dizia que ele era o limite
    de caracteres POR LINHA e citava as diretrizes da BBC. Errado: `_wrap_lines`
    nao recebe `max_chars` nenhum -- a quebra sai da largura da banda com o
    tamanho da fonte. O que `max_chars` faz, medido, e cortar o COMECO da fala
    e mostrar o final com "..." na frente.
    """

    def test_o_valor_vai_e_volta(self, win):
        win.max_chars_spin.setValue(90)
        assert win._build_config().overlay.max_chars == 90

    def test_zero_significa_nunca_cortar(self, win):
        assert win.max_chars_spin.minimum() == 0
        assert win.max_chars_spin.specialValueText() == "nunca cortar"

    def test_o_rotulo_nao_promete_quebra_de_linha(self, win):
        from PyQt6.QtWidgets import QLabel
        rotulos = [x.text() for x in win.findChildren(QLabel)]
        assert any("Cortar fala acima de" in r for r in rotulos)
        # A asserção original proibia a frase "por linha" em QUALQUER rótulo
        # da janela, e quebrou no dia em que o vocabulário do evento ganhou um
        # rótulo que a usa corretamente ("Um por linha, num arquivo"). O que
        # não pode voltar é o rótulo errado deste campo, não a frase.
        assert not any("Máximo por linha" in r for r in rotulos), (
            "o rótulo voltou a prometer quebra de linha")
        assert not any("por linha" in r for r in rotulos
                       if "Cortar" in r or "caracter" in r.lower())

    def test_o_overlay_realmente_corta_e_mantem_o_final(self, qapp):
        """Nao basta gravar o numero: o texto exibido tem de mudar."""
        from overlay_qt import CaptionOverlay
        frase = ("Comeca aqui a fala e ela segue por bastante tempo ate o "
                 "final, que e esta parte que voce esta lendo agora.")

        def exibido(max_chars):
            o = CaptionOverlay(AppConfig(
                provider="azure", azure_speech_key="k", target_languages=["es"],
                overlay=OverlayConfig(max_chars=max_chars)))
            try:
                return o._truncate(frase)
            finally:
                o.close()

        assert exibido(0) == frase, "0 devia deixar a fala inteira"
        assert exibido(600) == frase, "acima do tamanho da fala, nao corta"
        curto = exibido(60)
        assert len(curto) < len(frase), "max_chars=60 nao cortou nada"
        assert curto.endswith(frase[-20:]), "cortou o FINAL em vez do comeco"
        assert curto.startswith("…")


class TestConfigMortaSumiu:
    @pytest.mark.parametrize("campo", ("fade_ms", "streaming_partials"))
    def test_o_campo_nao_existe_mais(self, campo):
        assert not hasattr(OverlayConfig, campo)

    @pytest.mark.parametrize("campo", ("fade_ms", "streaming_partials"))
    def test_ninguem_no_app_o_le(self, campo):
        import pathlib
        raiz = pathlib.Path(__file__).resolve().parent.parent
        achados = []
        for f in list(raiz.glob("*.py")) + list((raiz / "providers").glob("*.py")):
            if campo in f.read_text(encoding="utf-8"):
                achados.append(f.name)
        assert not achados, f"{campo} ainda aparece em {achados}"

    def test_config_antiga_que_os_traga_ainda_carrega(self, tmp_path):
        import json
        config.config_path().write_text(json.dumps({
            "provider": "azure",
            "overlay": {"fade_ms": 200, "streaming_partials": True,
                        "max_chars": 180, "position": "top"},
        }), encoding="utf-8")
        cfg = config.load_config()
        assert cfg.overlay.max_chars == 180
        assert cfg.overlay.position == "top"
