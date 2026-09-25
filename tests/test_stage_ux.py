"""Três ajustes de palco, tirados do que Sokuji e TransKit fazem bem.

1. Erro que diz o que fazer, em português, com rota para a aba que corrige.
2. Atalhos globais para mostrar/esconder a legenda e iniciar/parar.
3. Trava da posição da faixa.

E o defeito que apareceu no caminho: a faixa chamava show() a cada fala
nova, então "Esconder legenda" durava só até a próxima frase. O atalho de
esconder seria inútil exatamente no momento para o qual ele existe.

A TrayApp cria o próprio QApplication e não é construível em teste; os
métodos dela são exercidos num objeto feito com __new__ e dublês mínimos, e
a regra de decisão mora em funções puras (`fix_tab_for`, `stop_confirmed`,
`keep_tray_owned`) que se testam sozinhas.
"""
from __future__ import annotations

import time
import types

import pytest

from config import AppConfig, OverlayConfig
from providers.base import (
    CODE_AUTH,
    CODE_DEVICE,
    CODE_NETWORK,
    CODE_QUOTA,
    CODE_UNKNOWN,
    MESSAGES,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_FATAL,
    STATUS_OK,
)

RAW_401 = ("WebSocket upgrade failed: Authentication error (401). Please check "
           "subscription information and region name. SessionId: 3f2a9c")


@pytest.fixture(scope="module")
def qapp():
    """Guarda a referência: sem isso o GC recolhe o QApplication com a janela
    viva e o interpretador morre sem traceback."""
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ======================================================================
# 1. Erro acionável
# ======================================================================

class TestMensagemQuandoAAzureEncerra:
    def test_chave_recusada_vira_instrucao_em_portugues(self):
        from providers.azure import canceled_message
        msg = canceled_message(CODE_AUTH, RAW_401, "Error")
        assert "Credenciais" in msg
        assert "região" in msg, "a 401 da Azure é chave OU região errada"
        for cru in ("WebSocket", "Authentication", "SessionId"):
            assert cru not in msg

    def test_codigo_conhecido_usa_a_mensagem_do_catalogo(self):
        from providers.azure import canceled_message
        assert canceled_message(CODE_QUOTA, "quota exceeded", "Error") == MESSAGES[CODE_QUOTA]

    def test_desconhecido_mantem_um_pedaco_do_detalhe_para_o_suporte(self):
        from providers.azure import canceled_message
        detalhe = "x" * 300
        msg = canceled_message(CODE_UNKNOWN, detalhe, "Error")
        assert "não identificado" in msg
        assert "x" * 80 in msg and "x" * 81 not in msg

    def test_catalogo_diz_onde_corrigir(self):
        """"Confira em Configurações" mandava o operador vasculhar sete abas."""
        assert "Credenciais" in MESSAGES[CODE_AUTH]
        assert "Provedor" in MESSAGES[CODE_QUOTA]
        assert "Áudio" in MESSAGES[CODE_DEVICE]

    def test_o_caminho_real_do_cancelamento_emite_a_frase_nova(self):
        pytest.importorskip("azure.cognitiveservices.speech")
        from providers.azure import AzureProvider
        prov = AzureProvider.__new__(AzureProvider)
        emitidos = []
        prov.on_status = emitidos.append
        prov.provider_name = "azure"
        prov._on_canceled(types.SimpleNamespace(reason="Error", error_details=RAW_401))
        assert len(emitidos) == 1
        st = emitidos[0]
        assert (st.kind, st.code) == (STATUS_FATAL, CODE_AUTH)
        assert "Credenciais" in st.message and "WebSocket" not in st.message


class TestRotaParaAAbaQueCorrige:
    @pytest.mark.parametrize("kind,code,aba", [
        (STATUS_FATAL, CODE_AUTH, "Credenciais"),
        (STATUS_FATAL, CODE_QUOTA, "Provedor"),
        (STATUS_FAILING, CODE_DEVICE, "Áudio"),
        (STATUS_FAILING, CODE_NETWORK, None),      # a correção é a internet do local
        (STATUS_DEGRADED, CODE_AUTH, None),        # degradado passa sozinho
        (STATUS_OK, CODE_AUTH, None),
    ])
    def test_fix_tab_for(self, kind, code, aba):
        from translator import fix_tab_for
        assert fix_tab_for(kind, code) == aba

    def test_toda_aba_do_mapa_existe_de_verdade(self, qapp):
        """Uma aba já foi renomeada nesta base. Mapa velho abriria a janela na
        aba errada, sem erro nenhum."""
        from settings_window import SettingsWindow
        from translator import FIX_TAB, HOTKEYS_TAB
        w = SettingsWindow(AppConfig())
        try:
            rotulos = {w.tabs.tabText(i) for i in range(w.tabs.count())}
            faltam = (set(FIX_TAB.values()) | {HOTKEYS_TAB}) - rotulos
            assert not faltam, f"abas inexistentes no mapa: {faltam}"
        finally:
            w.close()

    def test_show_tab_abre_a_aba_pedida_e_recusa_a_inexistente(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(AppConfig())
        try:
            assert w.show_tab("Áudio") is True
            assert w.tabs.tabText(w.tabs.currentIndex()) == "Áudio"
            assert w.show_tab("Não existe") is False
            assert w.tabs.tabText(w.tabs.currentIndex()) == "Áudio"
        finally:
            w.close()


class _Gravador:
    """Dublê que aceita qualquer chamada e guarda as que interessam."""

    def __init__(self):
        self.chamadas = []
        self.visivel = None
        self.texto = ""

    def setVisible(self, v):
        self.visivel = v

    def setText(self, t):
        self.texto = t

    def showMessage(self, *a):
        self.chamadas.append(a)

    def __getattr__(self, nome):          # setIcon, setToolTip, ...
        return lambda *a, **k: None


def _tray(**extra):
    import translator as T
    t = T.TrayApp.__new__(T.TrayApp)
    t.tray = _Gravador()
    t.status_action = _Gravador()
    t.action_fix = _Gravador()
    t._icons = {k: None for k in ("running", "degraded", "error", "stopped")}
    t.controller = types.SimpleNamespace(is_running=lambda: True)
    t.config = AppConfig()
    t._health = (STATUS_OK, "", "")
    t._last_balloon_at = 0.0
    t._balloon_fix_tab = None
    t._fix_tab = None
    abertas = []
    t.open_settings = lambda tab=None: abertas.append(tab)
    t._abertas = abertas
    for k, v in extra.items():
        setattr(t, k, v)
    return t


class TestBandejaLevaAteOConserto:
    def test_erro_de_chave_mostra_o_item_corrigir_e_o_balao_leva_la(self):
        t = _tray()
        t._on_health_changed(STATUS_FATAL, CODE_AUTH, MESSAGES[CODE_AUTH])
        assert t.action_fix.visivel is True
        assert "Credenciais" in t.action_fix.texto
        assert t.tray.chamadas, "FATAL sempre mostra balão"
        t._on_balloon_clicked()
        assert t._abertas == ["Credenciais"]

    def test_voltar_ao_normal_esconde_o_corrigir(self):
        t = _tray()
        t._on_health_changed(STATUS_FATAL, CODE_AUTH, "x")
        t._on_health_changed(STATUS_OK, CODE_UNKNOWN, "")
        assert t.action_fix.visivel is False

    def test_clique_num_balao_posterior_nao_herda_a_rota_do_erro(self):
        """O Qt só diz "clicaram num balão", não qual. Sem zerar a rota a
        cada balão, clicar em "Legenda escondida" abriria Credenciais."""
        t = _tray()
        t._on_health_changed(STATUS_FATAL, CODE_AUTH, "x")
        t._notify("Legenda escondida", "qualquer coisa")
        t._on_balloon_clicked()
        assert t._abertas == []

    def test_problema_de_rede_nao_oferece_aba_nenhuma(self):
        t = _tray()
        t._on_health_changed(STATUS_FATAL, CODE_NETWORK, MESSAGES[CODE_NETWORK])
        assert t.action_fix.visivel is False
        t._on_balloon_clicked()
        assert t._abertas == []


# ======================================================================
# 2. Atalhos de palco
# ======================================================================

class TestAtalhos:
    def test_padroes_sao_teclas_de_funcao_e_nao_ctrl_alt(self):
        """No ABNT2, AltGr chega como Ctrl+Alt: um atalho em Ctrl+Alt+Q
        dispararia toda vez que o operador digitasse "/" no chat."""
        cfg = AppConfig()
        assert cfg.hotkey_toggle_caption == "f8"
        assert cfg.hotkey_start_stop == "ctrl+f8"
        # Limpar não é Esc: Esc sai do modo apresentação do PowerPoint.
        assert cfg.hotkey_clear_caption == "f7"
        for tecla in (cfg.hotkey_toggle_caption, cfg.hotkey_start_stop,
                      cfg.hotkey_clear_caption):
            assert not ("ctrl" in tecla and "alt" in tecla)
            assert tecla != "esc"

    def test_a_biblioteca_real_aceita_os_padroes(self):
        keyboard = pytest.importorskip("keyboard")
        for tecla in (AppConfig().hotkey_toggle_caption, AppConfig().hotkey_start_stop,
                      AppConfig().hotkey_clear_caption):
            keyboard.parse_hotkey(tecla)          # levanta se inválido

    @pytest.mark.parametrize("armado,agora,esperado", [
        (0.0, 100.0, False),        # nunca armou
        (100.0, 101.5, True),       # segundo toque dentro da janela
        (100.0, 103.0, True),       # no limite
        (100.0, 103.1, False),      # tarde demais: rearma
        (100.0, 99.0, False),       # relógio andou para trás
    ])
    def test_stop_confirmed(self, armado, agora, esperado):
        from translator import stop_confirmed
        assert stop_confirmed(armado, agora) is esperado

    def _tray_rodando(self, monkeypatch, rodando):
        chamadas = []
        t = _tray(
            controller=types.SimpleNamespace(is_running=lambda: rodando[0]),
            start_translation=lambda: chamadas.append("start"),
            stop_translation=lambda: chamadas.append("stop"),
            _stop_armed_at=0.0,
        )
        return t, chamadas

    def test_parado_um_toque_inicia(self, monkeypatch):
        t, chamadas = self._tray_rodando(monkeypatch, [False])
        t._on_start_stop_hotkey()
        assert chamadas == ["start"]

    def test_rodando_um_toque_so_arma_e_avisa(self, monkeypatch):
        t, chamadas = self._tray_rodando(monkeypatch, [True])
        monkeypatch.setattr(time, "monotonic", lambda: 50.0)
        t._on_start_stop_hotkey()
        assert chamadas == []
        assert t.tray.chamadas and "de novo" in t.tray.chamadas[-1][1]

    def test_rodando_dois_toques_em_3s_param(self, monkeypatch):
        t, chamadas = self._tray_rodando(monkeypatch, [True])
        relogio = iter([50.0, 51.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(relogio))
        t._on_start_stop_hotkey()
        t._on_start_stop_hotkey()
        assert chamadas == ["stop"]

    def test_segundo_toque_tarde_demais_so_rearma(self, monkeypatch):
        t, chamadas = self._tray_rodando(monkeypatch, [True])
        relogio = iter([50.0, 60.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(relogio))
        t._on_start_stop_hotkey()
        t._on_start_stop_hotkey()
        assert chamadas == []

    def _fake_keyboard(self, monkeypatch, invalidos=()):
        import sys
        registrados = []

        def add_hotkey(tecla, acao):
            if tecla in invalidos:
                raise ValueError(f"tecla desconhecida: {tecla}")
            registrados.append(tecla)
            return tecla

        fake = types.SimpleNamespace(add_hotkey=add_hotkey,
                                     remove_hotkey=lambda h: registrados.remove(h))
        monkeypatch.setitem(sys.modules, "keyboard", fake)
        return registrados

    def _tray_atalhos(self, **cfg):
        from dataclasses import replace
        t = _tray(_stage_hotkey_handles=[])
        t.config = replace(AppConfig(), **cfg)
        t._caption_toggle_requested = types.SimpleNamespace(emit=lambda: None)
        t._start_stop_requested = types.SimpleNamespace(emit=lambda: None)
        t._clear_caption_requested = types.SimpleNamespace(emit=lambda: None)
        return t

    def test_registra_os_tres_atalhos_padrao(self, monkeypatch):
        reg = self._fake_keyboard(monkeypatch)
        t = self._tray_atalhos()
        t._register_stage_hotkeys()
        assert reg == ["f8", "ctrl+f8", "f7"]

    def test_registrar_de_novo_nao_duplica(self, monkeypatch):
        """Salvar Configurações re-registra; sem soltar antes, cada Salvar
        somaria um disparo a mais por toque."""
        reg = self._fake_keyboard(monkeypatch)
        t = self._tray_atalhos()
        t._register_stage_hotkeys()
        t._register_stage_hotkeys()
        assert reg == ["f8", "ctrl+f8", "f7"]

    def test_atalho_igual_ao_do_idioma_fica_desligado_e_avisa(self, monkeypatch):
        reg = self._fake_keyboard(monkeypatch)
        t = self._tray_atalhos(hotkey_toggle_caption="f9", azure_switch_hotkey="f9")
        t._register_stage_hotkeys()
        assert reg == ["ctrl+f8", "f7"]
        assert t._balloon_fix_tab == "Legenda"
        assert "repetido" in t.tray.chamadas[-1][0].lower()

    def test_atalho_invalido_avisa_em_vez_de_morrer_calado(self, monkeypatch):
        reg = self._fake_keyboard(monkeypatch, invalidos=("xyz",))
        t = self._tray_atalhos(hotkey_start_stop="xyz")
        t._register_stage_hotkeys()
        assert reg == ["f8", "f7"]
        assert "inválido" in t.tray.chamadas[-1][0].lower()
        assert t._balloon_fix_tab == "Legenda"

    def test_vazio_desliga(self, monkeypatch):
        reg = self._fake_keyboard(monkeypatch)
        t = self._tray_atalhos(hotkey_toggle_caption="", hotkey_start_stop="",
                               hotkey_clear_caption="")
        t._register_stage_hotkeys()
        assert reg == []

    def test_a_tela_salva_e_recarrega_os_atalhos(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(AppConfig(hotkey_toggle_caption="f7"))
        try:
            assert w.hotkey_toggle_caption_input.text() == "f7"
            w.hotkey_start_stop_input.setText("  Ctrl+F6 ")
            salvo = w._build_config()
            assert salvo.hotkey_toggle_caption == "f7"
            assert salvo.hotkey_start_stop == "ctrl+f6"
        finally:
            w.close()


# ======================================================================
# 3. Trava + esconder que dura
# ======================================================================

def _cfg(**overlay_kw) -> AppConfig:
    base = dict(split_languages=False, stable_height=True, reserved_lines=3,
                primary_font_size=25, secondary_font_size=25, padding=20,
                concat_gap_ms=1000, max_history=1)
    base.update(overlay_kw)
    return AppConfig(provider="azure", azure_speech_key="k",
                     target_languages=["es", "en"],
                     display_mode="translations_only_multi",
                     overlay=OverlayConfig(**base))


def _press(ov):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(10, 10), QPointF(10, 10),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    ov.mousePressEvent(ev)


class TestTravaDaFaixa:
    def test_destravada_arrasta(self, qapp):
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(_cfg(locked=False))
        try:
            _press(ov)
            assert ov._dragging is True
        finally:
            ov.close()

    def test_travada_nao_arrasta(self, qapp):
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(_cfg(locked=True))
        try:
            _press(ov)
            assert ov._dragging is False
        finally:
            ov.close()

    def test_salvar_configuracoes_nao_desfaz_a_trava_da_bandeja(self):
        from dataclasses import replace

        from translator import keep_tray_owned
        atual = replace(AppConfig(), overlay=replace(AppConfig().overlay, locked=True))
        # A janela foi aberta antes de travar: guarda locked=False, e muda outra coisa.
        salvo = replace(AppConfig(), overlay=replace(AppConfig().overlay,
                                                     locked=False, width_ratio=0.6))
        final = keep_tray_owned(salvo, atual)
        assert final.overlay.locked is True
        assert final.overlay.width_ratio == 0.6

    def test_travar_no_modo_evento_sobrevive_ao_desligar_o_modo(self, monkeypatch):
        """O Modo evento guarda a config de antes dele e a restaura ao sair.
        Sem acompanhar a trava nessa cópia, sair do Modo evento destravaria."""
        from dataclasses import replace

        import translator as T
        monkeypatch.setattr(T, "save_config", lambda cfg: None)
        t = _tray()
        t._saved_overlay_config = replace(AppConfig().overlay, locked=False)
        t._apply_config = lambda cfg: setattr(t, "config", cfg)
        t.toggle_lock(True)
        assert t.config.overlay.locked is True
        assert t._saved_overlay_config.locked is True


class TestEsconderDuraAteOOperadorDesfazer:
    def _falar(self, ov, qapp, texto):
        ov.push_caption(texto, {"es": f"ES {texto}", "en": f"EN {texto}"},
                        detected_language="pt-BR", is_final=True)
        qapp.processEvents()

    def test_limpar_tira_a_frase_da_tela_e_a_proxima_fala_volta(self, qapp):
        """F7: uma tradução errada sai da tela agora. Não é "esconder pelo
        operador" — a fala seguinte reabre a faixa sozinha."""
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(_cfg())
        try:
            self._falar(ov, qapp, "frase constrangedora")
            t = _tray(overlay=ov, overlay2=None, config=_cfg())
            t._clear_caption()
            assert ov._history == [] and ov.isVisible() is False
            assert ov.is_hidden_by_operator() is False
            self._falar(ov, qapp, "frase constrangedora")   # repetida: sem dedup
            assert ov.isVisible() is True
            assert [u.original for u in ov._history] == ["frase constrangedora"]
        finally:
            ov.close()

    def test_fala_nova_nao_reabre_a_faixa_escondida(self, qapp):
        """O defeito que existia: cada legenda chamava show()."""
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(_cfg())
        try:
            ov.set_hidden_by_operator(True)
            self._falar(ov, qapp, "vídeo começou")
            assert ov.isVisible() is False
            ov.set_hidden_by_operator(False)
            assert ov.isVisible() is True
        finally:
            ov.close()

    def test_sumir_por_ociosidade_nao_e_esconder(self, qapp):
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(_cfg())
        try:
            self._falar(ov, qapp, "primeira")
            ov._clear_all()                     # IDLE_CLEAR_MS sem fala
            assert ov.isVisible() is False
            assert ov.is_hidden_by_operator() is False
            self._falar(ov, qapp, "segunda")
            assert ov.isVisible() is True
        finally:
            ov.close()

    def test_atalho_decide_pelo_operador_e_nao_pela_visibilidade(self, qapp):
        """Numa pausa a faixa já está invisível por ociosidade. Ler
        isVisible() faria o atalho MOSTRAR quando o operador quer esconder."""
        from overlay_qt import CaptionOverlay
        ov = CaptionOverlay(_cfg())
        try:
            self._falar(ov, qapp, "fala")
            ov._clear_all()
            t = _tray(overlay=ov, overlay2=None, config=_cfg())
            t._toggle_caption_visibility()
            assert ov.is_hidden_by_operator() is True
            self._falar(ov, qapp, "fala durante o vídeo")
            assert ov.isVisible() is False
            t._toggle_caption_visibility()
            assert ov.is_hidden_by_operator() is False and ov.isVisible() is True
        finally:
            ov.close()


class TestSalvarConfiguracoes:
    """A ligação, não só a função: `_on_config_saved` usa o que foi testado acima."""

    def _tray_salvar(self, rodando: bool):
        eventos = []
        t = _tray(
            controller=types.SimpleNamespace(is_running=lambda: rodando),
            stop_translation=lambda: eventos.append("stop"),
            start_translation=lambda: eventos.append("start"),
            _register_stage_hotkeys=lambda: eventos.append("atalhos"),
            _register_hotkey=lambda: eventos.append("f9"),
            _set_presentation=lambda on: None,
            action_presentation=_Gravador(),
            _presentation_mode_active=False,
            _saved_overlay_config=None,
            _saved_display_mode="",
        )
        t._apply_config = lambda cfg: setattr(t, "config", cfg)
        return t, eventos

    def test_salvar_com_a_janela_velha_nao_destrava(self):
        from dataclasses import replace
        t, _ = self._tray_salvar(rodando=False)
        t.config = replace(t.config, overlay=replace(t.config.overlay, locked=True))
        janela_velha = replace(t.config, overlay=replace(t.config.overlay, locked=False))
        t._on_config_saved(janela_velha)
        assert t.config.overlay.locked is True

    @pytest.mark.parametrize("campo", ["hotkey_toggle_caption", "hotkey_clear_caption"])
    def test_trocar_so_o_atalho_rodando_nao_reinicia_a_legenda(self, campo):
        from dataclasses import replace
        t, eventos = self._tray_salvar(rodando=True)
        t._on_config_saved(replace(t.config, **{campo: "f11"}))
        assert "stop" not in eventos and "start" not in eventos
        assert "atalhos" in eventos and "f9" in eventos

    def test_mudar_idioma_ainda_reinicia(self):
        """A exceção dos atalhos não pode engolir o que É do pipeline."""
        from dataclasses import replace
        t, eventos = self._tray_salvar(rodando=True)
        t._on_config_saved(replace(t.config, target_languages=["fr"]))
        assert eventos[:1] == ["stop"] and "start" in eventos
