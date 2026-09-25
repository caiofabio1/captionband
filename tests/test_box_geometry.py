"""A caixa fica onde o operador a deixou — e ele pode redimensioná-la.

Reportado: "Não consigo mover as caixas de legenda, nem redimensioná-las".
MEDIDO em 25/09/2026 com o mouse real (SendInput, pelo hit-test do Windows):
o arraste sempre moveu a janela. O que não existia era memória — qualquer
Iniciar, troca de idioma, Salvar ou Modo evento chamava _apply_position() e
devolvia a caixa ao preset; ao abrir o app, idem. Redimensionar não existia.
E com a tradução parada a caixa nem aparece para ser arrastada.
"""
from __future__ import annotations

import types
from dataclasses import replace

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from config import AppConfig, OverlayConfig, _coerce_fields
from overlay_qt import CaptionOverlay
from translator import keep_tray_owned, split_overlay_configs

L = Qt.MouseButton.LeftButton
NONE = Qt.MouseButton.NoButton
NOMOD = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _cfg(**ov) -> AppConfig:
    base = dict(split_languages=True, position="bottom", second_position="top",
                width_ratio=0.77, primary_font_size=25, secondary_font_size=25)
    base.update(ov)
    return AppConfig(provider="azure", azure_speech_key="k", target_languages=["es", "en"],
                     display_mode="translations_only_multi", overlay=OverlayConfig(**base))


def _shown(qapp, **ov) -> CaptionOverlay:
    ov_ = CaptionOverlay(_cfg(**ov))
    ov_.show()
    qapp.processEvents()
    return ov_


def _gesture(ov: CaptionOverlay, local: QPoint, dx: int, dy: int) -> None:
    """Press at `local` (widget coords), move by (dx, dy), release."""
    g0 = ov.mapToGlobal(local)
    ov.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(local),
                                   QPointF(g0), L, L, NOMOD))
    for k in (1, 2):
        g = QPoint(g0.x() + dx * k // 2, g0.y() + dy * k // 2)
        ov.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(ov.mapFromGlobal(g)),
                                      QPointF(g), L, L, NOMOD))
    ov.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(ov.mapFromGlobal(g)),
                                     QPointF(g), L, NONE, NOMOD))


class TestArrastarERedimensionar:
    def test_arrastar_move_e_avisa_em_fracoes_da_tela(self, qapp):
        ov = _shown(qapp)
        try:
            avisos = []
            ov.geometry_edited.connect(avisos.append)
            x0, y0 = ov.x(), ov.y()
            _gesture(ov, QPoint(120, 30), 60, -40)
            assert (ov.x(), ov.y()) == (x0 + 60, y0 - 40)
            assert len(avisos) == 1
            geo = ov._screen().availableGeometry()
            rx, ry, rw, rh = avisos[0]
            assert abs(rx * geo.width() + geo.x() - ov.x()) < 2
            assert abs(rw * geo.width() - ov.width()) < 2
            assert 0 < rh < 1
        finally:
            ov.close()

    def test_borda_direita_e_inferior_redimensionam(self, qapp):
        ov = _shown(qapp)
        try:
            avisos = []
            ov.geometry_edited.connect(avisos.append)
            w0, h0, x0 = ov.width(), ov.height(), ov.x()
            _gesture(ov, QPoint(w0 - 4, h0 // 2), 80, 30)      # right edge: width only
            assert (ov.width(), ov.height(), ov.x()) == (w0 + 80, h0, x0)
            _gesture(ov, QPoint(w0 // 2, h0 - 4), 30, 50)      # bottom edge: height only
            assert (ov.width(), ov.height()) == (w0 + 80, h0 + 50)
            _gesture(ov, QPoint(ov.width() - 4, ov.height() - 4), -20, -20)   # corner
            assert (ov.width(), ov.height()) == (w0 + 60, h0 + 30)
            assert len(avisos) == 3
        finally:
            ov.close()

    def test_nao_encolhe_abaixo_do_minimo(self, qapp):
        ov = _shown(qapp)
        try:
            _gesture(ov, QPoint(ov.width() - 4, ov.height() - 4), -5000, -5000)
            assert (ov.width(), ov.height()) == (CaptionOverlay.MIN_W, ov._min_height())
        finally:
            ov.close()

    def test_travada_nao_move_nem_redimensiona(self, qapp):
        ov = _shown(qapp, locked=True)
        try:
            avisos = []
            ov.geometry_edited.connect(avisos.append)
            antes = ov.geometry()
            _gesture(ov, QPoint(120, 30), 60, -40)
            _gesture(ov, QPoint(ov.width() - 4, ov.height() - 4), 80, 80)
            assert ov.geometry() == antes and avisos == []
        finally:
            ov.close()

    def test_um_clique_sem_mover_nao_avisa(self, qapp):
        ov = _shown(qapp)
        try:
            avisos = []
            ov.geometry_edited.connect(avisos.append)
            _gesture(ov, QPoint(120, 30), 0, 0)
            assert avisos == []
        finally:
            ov.close()


class TestAPosicaoGuardadaManda:
    def test_apply_position_honra_o_retangulo_guardado(self, qapp):
        ov = CaptionOverlay(_cfg(custom_rect=(0.1, 0.2, 0.5, 0.25)))
        try:
            geo = ov._screen().availableGeometry()
            g = ov.geometry()
            assert abs(g.x() - (geo.x() + 0.1 * geo.width())) < 2
            assert abs(g.y() - (geo.y() + 0.2 * geo.height())) < 2
            assert abs(g.width() - 0.5 * geo.width()) < 2
            assert abs(g.height() - 0.25 * geo.height()) < 2
            # Mudar o preset com o retângulo guardado NÃO move: o guardado manda.
            ov.apply_config(_cfg(position="top", custom_rect=(0.1, 0.2, 0.5, 0.25)))
            assert ov.geometry() == g
            ov.apply_config(_cfg(position="top"))
            assert ov.y() == geo.y() + 40, "sem retângulo guardado volta ao preset"
        finally:
            ov.close()

    def test_retangulo_fora_da_tela_e_puxado_para_dentro(self, qapp):
        """Guardado no projetor, aberto no notebook: tem de dar para alcançar."""
        ov = CaptionOverlay(_cfg(custom_rect=(0.9, 0.95, 0.5, 0.25)))
        try:
            geo = ov._screen().availableGeometry()
            g = ov.geometry()
            assert g.right() <= geo.right() and g.bottom() <= geo.bottom()
        finally:
            ov.close()

    def test_a_segunda_caixa_tem_o_proprio_retangulo(self):
        cfg = _cfg(custom_rect=(0.1, 0.1, 0.5, 0.2), second_custom_rect=(0.4, 0.6, 0.5, 0.2))
        first, second = split_overlay_configs(cfg)
        assert first.overlay.custom_rect == (0.1, 0.1, 0.5, 0.2)
        assert second.overlay.custom_rect == (0.4, 0.6, 0.5, 0.2)

    def test_salvar_configuracoes_nao_esquece_onde_a_caixa_ficou(self):
        """A janela de Configurações guarda a config de quando foi ABERTA."""
        atual = _cfg(custom_rect=(0.1, 0.1, 0.5, 0.2), second_custom_rect=(0.4, 0.6, 0.5, 0.2))
        janela_velha = _cfg()
        salvo = keep_tray_owned(janela_velha, atual)
        assert salvo.overlay.custom_rect == atual.overlay.custom_rect
        assert salvo.overlay.second_custom_rect == atual.overlay.second_custom_rect
        # Escolher OUTRA posição predefinida é como se volta ao preset — só
        # da caixa cuja posição mudou.
        salvo = keep_tray_owned(_cfg(position="middle"), atual)
        assert salvo.overlay.custom_rect is None
        assert salvo.overlay.second_custom_rect == atual.overlay.second_custom_rect

    def test_config_json_devolve_tupla_ou_nada(self):
        got = _coerce_fields(OverlayConfig, {"custom_rect": [0.1, 0.2, 0.3, 0.4],
                                             "second_custom_rect": None})
        assert got["custom_rect"] == (0.1, 0.2, 0.3, 0.4)
        assert got.get("second_custom_rect") is None


class TestBandejaGuardaPorCaixa:
    def _tray(self, monkeypatch, cfg):
        import translator as T
        t = T.TrayApp.__new__(T.TrayApp)
        t.config = cfg
        t._saved_overlay_config = replace(cfg.overlay, primary_font_size=54)
        t._apply_config = lambda c: setattr(t, "config", c)
        gravados = []
        monkeypatch.setattr(T, "save_config", gravados.append)
        return t, gravados

    def test_cada_caixa_no_seu_campo_e_salva(self, monkeypatch):
        t, gravados = self._tray(monkeypatch, _cfg())
        t._on_box_edited((0.1, 0.2, 0.3, 0.4), second=True)
        assert t.config.overlay.second_custom_rect == (0.1, 0.2, 0.3, 0.4)
        assert t.config.overlay.custom_rect is None
        t._on_box_edited([0.5, 0.6, 0.3, 0.1], second=False)
        assert t.config.overlay.custom_rect == (0.5, 0.6, 0.3, 0.1)
        assert len(gravados) == 2
        # O Modo evento guarda a config de antes; sair dele não pode devolver
        # a caixa ao lugar antigo.
        assert t._saved_overlay_config.custom_rect == (0.5, 0.6, 0.3, 0.1)
        assert t._saved_overlay_config.primary_font_size == 54

    def test_voltar_ao_padrao_esquece_as_duas(self, monkeypatch):
        t, gravados = self._tray(monkeypatch, _cfg(custom_rect=(0.1, 0.1, 0.5, 0.2),
                                                   second_custom_rect=(0.4, 0.6, 0.5, 0.2)))
        t._reposition_overlays()
        assert (t.config.overlay.custom_rect, t.config.overlay.second_custom_rect) == (None, None)
        assert t._saved_overlay_config.custom_rect is None
        assert gravados


class TestCaixaVaziaExplica:
    def test_dica_so_fora_da_projecao(self, qapp):
        ov = CaptionOverlay(_cfg())
        try:
            assert "rraste" in ov._empty_hint()
            ov.set_presentation(True)
            assert ov._empty_hint() == ""
            ov.set_presentation(False)
            ov.apply_config(_cfg(locked=True))
            assert "travada" in ov._empty_hint().lower()
        finally:
            ov.close()
