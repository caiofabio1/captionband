"""Transparent always-on-top caption overlay using PyQt6.

Designed for legibility on top of any video content with multi-line history
following live-captioning best practices:

- **Rolling history**: shows N previous utterances (config: max_history) above
  the current one, dimmed and slightly smaller. Newest at bottom.
- **Auto-concatenate**: consecutive utterances within `concat_gap_ms` and same
  detected language are merged into a single line (mimics natural reading of
  continuous speech).
- **Two-stage rendering**: when an interim/preview event arrives without a
  translation (Google two-stage emit, or Whisper local still running), shows
  original transcript dim with "…" placeholder. When translation arrives, the
  same caption is updated in-place — no flashing.
- **Time-windowed dedup**: identical originals within 4s are dropped.
- **Min display time**: each new caption stays at least MIN_DISPLAY_MS
  (800 ms) before being replaced (queues if events come faster).
- **Idle clear**: the band hides IDLE_CLEAR_MS (20 s) after the last event.

Display modes:
  * "translations_only" — translation only (with original dim above while pending)
  * "original_plus_translation" — original (dim) + translation (bright)
  * "translations_only_multi" — multiple target languages stacked
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field

from PyQt6.QtCore import QPoint, QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPixmap,
)
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from config import AppConfig, OverlayConfig

log = logging.getLogger(__name__)


# Per-language accent colors (vertical bar on left of caption)
# Using accessible palette (passes WCAG AA on dark background)
LANGUAGE_COLORS = {
    "pt": "#4A9EFF",      # blue
    "en": "#2EA043",      # green
    "es": "#FF8C42",      # orange
    "fr": "#B581E5",      # purple
    "it": "#F2C94C",      # yellow
    "de": "#FFC107",      # amber
    "zh": "#E36209",      # deep orange (Chinese)
}


def _color_for_language(lang: str | None) -> str | None:
    if not lang:
        return None
    code = lang.lower().split("-")[0].split("_")[0]
    # Also accept full names returned by Whisper/Groq ("Portuguese", "English")
    name_map = {
        "portuguese": "pt", "english": "en", "spanish": "es",
        "french": "fr", "italian": "it", "german": "de",
        "chinese": "zh", "mandarin": "zh",
    }
    if code in name_map:
        code = name_map[code]
    return LANGUAGE_COLORS.get(code)


@dataclass
class Utterance:
    """One logical caption line in the rolling history."""
    original: str
    translations: dict[str, str] = field(default_factory=dict)
    detected_language: str | None = None
    created_at_ms: float = 0.0
    updated_at_ms: float = 0.0
    is_final: bool = False
    audio_emitted_at_ms: float | None = None
    # Stable utterance ID from streaming providers (Azure recognizing →
    # recognized share the same result_id). Used to replace partials in-place
    # rather than appending a new line. Empty for chunk-final providers.
    result_id: str = ""

    def primary_translation(self, primary_lang: str) -> str:
        return self.translations.get(primary_lang, "") if primary_lang else ""

    def language_color(self) -> str | None:
        return _color_for_language(self.detected_language)


class CaptionOverlay(QWidget):
    """Always-on-top transparent overlay window with multi-line history.

    Use push_caption(original, translations) to push new content (thread-safe).
    """

    update_signal = pyqtSignal(str, dict, str, bool, float, str)
    # extended signal: original, translations, detected_language, is_final,
    #                  audio_emitted_at_ms, result_id

    close_requested = pyqtSignal()
    # Emitted when the user clicks the in-overlay × (the tray hides the band)
    geometry_edited = pyqtSignal(object)
    # Emitted on mouse release after a drag or a resize: (x, y, w, h) as
    # fractions of the screen's available area — what the tray remembers.

    RESIZE_GRIP_PX = 16           # right / bottom edge band that resizes
    MIN_W = 240

    # Best-practice timings
    MIN_DISPLAY_MS = 800           # min time current caption stays before scroll
    DEDUP_WINDOW_MS = 4000         # time-window to ignore duplicate originals
    IDLE_CLEAR_MS = 20000          # clear all when no events for this long
    ANIM_DURATION_MS = 280         # fade + slide duration when caption changes

    def __init__(self, app_config: AppConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self.app_config = app_config
        self.overlay_config: OverlayConfig = app_config.overlay
        self.display_mode = app_config.display_mode

        # Rolling history of utterances (newest at end)
        max_total = max(1, self.overlay_config.max_history + 1)
        self._history: list[Utterance] = []
        self._max_total = max_total

        # Recent (text, ts) tuples for dedup
        self._recent_seen: list[tuple[str, float]] = []

        # Animation state
        self._anim_started_ms: float = 0.0
        self._last_latency_ms: float | None = None
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(33)  # ~30fps
        self._anim_timer.timeout.connect(self._tick_animation)

        self._dragging = False
        self._drag_offset = QPoint()
        self._resizing = (False, False)          # (right edge, bottom edge)
        self._resize_anchor = (QPoint(), self.size())
        self._press_geometry = self.geometry()
        self._presentation = False
        # O operador mandou esconder (bandeja, atalho ou o × da faixa). Fala
        # nova NÃO reabre a faixa enquanto isto estiver ligado. Sem este
        # estado, cada legenda chamava show() e "Esconder legenda" durava até
        # a próxima frase — justamente quando alguém pede para tirar a
        # legenda da tela durante um vídeo. Não confundir com a faixa sumir
        # por ociosidade (`_clear_all`), que é da própria faixa e volta sozinha.
        self._hidden_by_operator = False
        # How many composed lines belong to the NEWEST utterance. The fixed
        # band may drop older lines when it overflows, never these.
        self._current_block_lines = 0
        # Rendered lines, keyed by text + type + colours (see _line_pixmap).
        self._line_cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        # (created_at_ms of the newest utterance, point size it was fitted
        # at): the size a sentence shrank to is kept while THAT sentence keeps
        # changing (partials, then the final). See _paint.
        self._fit_lock: tuple[float | None, int] | None = None
        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.setInterval(self.REPAINT_MIN_MS)
        self._repaint_timer.timeout.connect(super().update)

        self._setup_window()
        self._build_close_button()
        self._apply_position()
        self._set_click_through(self.overlay_config.click_through)

        self.update_signal.connect(self._on_update)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._clear_all)

        self._watch_screens()

    def _watch_screens(self) -> None:
        """Reposition the band when the monitor layout changes.

        _screen() already falls back to the primary monitor when the
        configured one is gone, but nothing ever re-ran _apply_position():
        a projector unplugged mid-event left the window at coordinates that
        belonged to a screen that no longer exists — off every real screen.
        """
        app = QGuiApplication.instance()
        if app is None:
            return
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)
        for s in QGuiApplication.screens():
            s.virtualGeometryChanged.connect(
                self._on_virtual_geometry_changed,
                Qt.ConnectionType.UniqueConnection)

    def _on_screen_added(self, screen) -> None:
        log.info("screen connected: %s — repositioning the caption", screen.name())
        try:
            screen.virtualGeometryChanged.connect(
                self._on_virtual_geometry_changed,
                Qt.ConnectionType.UniqueConnection)
        except Exception:
            log.exception("could not watch the new screen's geometry")
        self._apply_position()

    def _on_screen_removed(self, screen) -> None:
        configured = getattr(self.overlay_config, "screen_name", "") or ""
        if configured and screen.name() == configured:
            # The projector left in the middle of the event. The band moves
            # to the primary screen — a caption on the wrong screen beats no
            # caption — but the operator needs to know WHY it moved.
            log.warning(
                "the screen configured for the caption (%s) was disconnected; "
                "falling back to the primary screen", configured)
        else:
            log.info("screen disconnected: %s — repositioning the caption",
                     screen.name())
        self._apply_position()

    def _on_virtual_geometry_changed(self, _rect) -> None:
        # Monitors rearranged in "extend" mode: the same screen now lives at
        # different coordinates, so the saved geometry is stale.
        self._apply_position()

    # ------------------------------------------------------------------ window setup

    def _setup_window(self) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)              # resize cursor on the edges

    def _screen(self):
        """The monitor the caption belongs on: the configured one, else primary.

        Falls back silently when the configured screen is gone (projector
        unplugged): a caption on the wrong screen beats no caption.
        """
        name = getattr(self.overlay_config, "screen_name", "") or ""
        if name:
            for s in QGuiApplication.screens():
                if s.name() == name:
                    return s
        return QGuiApplication.primaryScreen()

    def set_presentation(self, on: bool) -> None:
        """Projection mode: nothing on the band but the words.

        The × button and the latency badge are operator tools; on the
        room's screen they are clutter — and the × is one stray click away
        from quitting the app in front of the audience.
        """
        self._presentation = bool(on)
        if hasattr(self, "_close_btn"):
            self._close_btn.setVisible(not self._presentation)
        self._request_repaint()

    def _min_height(self) -> int:
        return self.overlay_config.padding * 2 + 20

    def _ratios(self) -> tuple[float, float, float, float]:
        """This box's geometry as fractions of its screen's available area."""
        geo = self._screen().availableGeometry()
        g = self.geometry()
        return (round((g.x() - geo.x()) / geo.width(), 4),
                round((g.y() - geo.y()) / geo.height(), 4),
                round(g.width() / geo.width(), 4),
                round(g.height() / geo.height(), 4))

    def _apply_position(self) -> None:
        screen = self._screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        custom = getattr(self.overlay_config, "custom_rect", None)
        if custom:
            # Where the operator left the box. Clamped to the screen: a rect
            # remembered on the projector must still be reachable on the
            # laptop panel after the projector is unplugged.
            rx, ry, rw, rh = (float(v) for v in custom)
            width = max(self.MIN_W, int(rw * geo.width()))
            height = max(self._min_height(), int(rh * geo.height()))
            x = geo.x() + int(rx * geo.width())
            y = geo.y() + int(ry * geo.height())
            x = min(max(x, geo.x()), geo.x() + geo.width() - width)
            y = min(max(y, geo.y()), geo.y() + geo.height() - height)
            self.setGeometry(x, y, width, height)
            return
        width = int(geo.width() * self.overlay_config.width_ratio)
        height = self._estimate_height()
        x = geo.x() + (geo.width() - width) // 2

        position = self.overlay_config.position
        if position == "top":
            y = geo.y() + 40
        elif position == "middle":
            y = geo.y() + (geo.height() - height) // 2
            if self.overlay_config.split_languages:
                # Two boxes: the other one sits in the top or the bottom
                # slot. Keep clear of a band of this height in either, when
                # the screen has room for three; otherwise stay centred and
                # let the operator see the overlap in the preview.
                lo = geo.y() + 40 + height + 8
                hi = geo.y() + geo.height() - 80 - height - 8 - height
                if lo <= hi:
                    y = min(max(y, lo), hi)
        else:
            y = geo.y() + geo.height() - height - 80

        self.setGeometry(x, y, width, height)

    def _estimate_height(self) -> int:
        cfg = self.overlay_config
        if getattr(cfg, "stable_height", True):
            return self._reserved_height()
        # rough estimate: 1 primary line + (max_history) secondary lines
        rough = (
            cfg.primary_font_size * 2
            + cfg.secondary_font_size * 2 * cfg.max_history
            + cfg.padding * 2
            + 40
        )
        return rough

    @staticmethod
    def _bar_x(cfg, bar_w: int) -> int:
        """Left edge of the language bar, kept inside the band."""
        return max(2, cfg.padding - 6 - bar_w)

    def _reserved_height(self) -> int:
        """Height of the fixed caption band, derived from config alone.

        Deliberately independent of the current text: the whole point is that
        it does NOT change while captions come and go. It is recomputed only
        when the operator changes the configuration.
        """
        cfg = self.overlay_config
        primary = QFontMetrics(
            QFont(cfg.font_family, cfg.primary_font_size, QFont.Weight.Bold)
        ).lineSpacing()
        secondary = QFontMetrics(
            QFont(cfg.font_family, cfg.secondary_font_size, QFont.Weight.Normal)
        ).lineSpacing()

        # Never smaller than what ONE utterance needs in the current display
        # mode, with room for each of its lines to wrap once. A 3-line band
        # under "spoken + EN + ES" (3 lines per sentence, before wrapping)
        # is what forced the font to 26 pt and the languages to alternate.
        targets = list(getattr(self.app_config, "target_languages", None) or [])
        per_utt = {
            "translations_only": 1,
            "original_plus_translation": 1 + max(1, len(targets)),
            "translations_only_multi": max(1, len(targets)),
        }.get(self.display_mode, 1)
        # A banda tambem precisa caber o historico que o operador pediu.
        # MEDIDO antes desta linha existir: com max_history em 2, 3 ou 5 o
        # overlay guardava 3, 4 e 6 falas e desenhava sempre 4 linhas — o
        # laco de ajuste descartava toda linha de historico que nao coubesse
        # na banda fixa. "Linhas anteriores visiveis" era um no-op acima de 1.
        # reserved_lines continua sendo PISO, entao aumentar a banda a mao
        # ainda funciona; ela so nao pode mais anular o max_history em silencio.
        history = max(0, int(getattr(cfg, "max_history", 0)))
        wanted = (history + 1) * per_utt
        lines = max(1, int(getattr(cfg, "reserved_lines", 3)), per_utt * 2, wanted)
        # Same arithmetic as _heights() in _paint: every line costs its
        # spacing + 6, and utterances are UTTERANCE_GAP_PX apart. The band
        # used to be sized without those, 6 px per line and 14 per gap
        # short — and the fit loop quietly shrank the type on every paint
        # to make up the difference.
        # The newest utterance is primary; whatever else fits is secondary.
        height = cfg.padding * 2 + min(lines, per_utt * 2) * (primary + 6)
        height += max(0, lines - per_utt * 2) * (secondary + 6)
        utterances = min(history + 1, max(1, -(-lines // per_utt)))
        height += (utterances - 1) * self.UTTERANCE_GAP_PX

        screen = self._screen()
        if screen is not None:
            height = min(height, int(screen.availableGeometry().height() * 0.5))
        return max(height, cfg.padding * 2 + 20)

    def _set_click_through(self, enabled: bool) -> None:
        # Whole overlay window is click-through, but the close button stays
        # interactive — its widget keeps WA_TransparentForMouseEvents=False
        # because Qt evaluates this attribute per-widget, not inherited.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, enabled)

    def _build_close_button(self) -> None:
        """Small × button in the top-right corner that asks the app to quit."""
        self._close_btn = QPushButton("×", self)
        self._close_btn.setToolTip("Esconder a legenda (o app continua na bandeja)")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._close_btn.setFixedSize(26, 26)
        self._close_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(207, 34, 46, 200);"
            "  color: white;"
            "  border: none;"
            "  border-radius: 13px;"
            "  font-weight: bold;"
            "  font-size: 16px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 60, 70, 240);"
            "}"
        )
        self._close_btn.clicked.connect(self.close_requested.emit)
        self._close_btn.raise_()

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        if hasattr(self, "_close_btn"):
            self._close_btn.move(self.width() - 32, 6)
            self._close_btn.raise_()

    # ------------------------------------------------------------------ public API

    def push_caption(
        self,
        original: str,
        translations: dict[str, str],
        detected_language: str | None = None,
        is_final: bool = True,
        audio_emitted_at_ms: float | None = None,
        result_id: str = "",
    ) -> None:
        """Thread-safe entry point used by background threads (provider callbacks)."""
        log.debug(
            "push_caption: original=%r translations=%s lang=%s final=%s rid=%s",
            (original or "")[:60],
            list((translations or {}).keys()),
            detected_language,
            is_final,
            (result_id or "")[:8],
        )
        self.update_signal.emit(
            original or "",
            dict(translations or {}),
            detected_language or "",
            bool(is_final),
            float(audio_emitted_at_ms) if audio_emitted_at_ms is not None else 0.0,
            result_id or "",
        )

    def apply_config(self, app_config: AppConfig) -> None:
        self.app_config = app_config
        self.overlay_config = app_config.overlay
        self.display_mode = app_config.display_mode
        self._max_total = max(1, self.overlay_config.max_history + 1)
        # trim history to new size
        if len(self._history) > self._max_total:
            self._history = self._history[-self._max_total:]
        self._line_cache.clear()
        self._fit_lock = None
        self._set_click_through(self.overlay_config.click_through)
        self._apply_position()
        self._request_repaint()

    def reposition(self) -> None:
        self._apply_position()

    def clear(self) -> None:
        self._history.clear()
        # A cleared band has nothing to be a duplicate of. Keeping the dedup
        # memory made the Settings preview drop its own sample caption on
        # the second click (same text within DEDUP_WINDOW_MS) — blank band.
        self._recent_seen.clear()
        self._fit_lock = None
        self._request_repaint()

    # ------------------------------------------------------------------ dedup

    def _now_ms(self) -> float:
        import time
        return time.monotonic() * 1000

    # Barra colorida que identifica o idioma de cada linha. MEDIDO: com 4px,
    # espanhol e ingles saiam na MESMA cor de texto, mesmo tamanho e mesmo
    # peso — a barrinha era a unica distincao, invisivel do fundo de um
    # auditorio. No modo dividido cada caixa tem um idioma so e o problema
    # nao existe; ele aparece justamente ao unificar as caixas.
    LANG_BAR_PX = 14
    LANG_BAR_PX_PRESENTATION = 22
    # Folga vertical entre FALAS (nao entre os idiomas de uma mesma fala).
    UTTERANCE_GAP_PX = 14

    # Floor for shrinking the type, reached only when the current utterance
    # ALONE does not fit the band. Below this it is unreadable from the back
    # of a room.
    MIN_FIT_PT_HARD = 16
    # Point size step while shrinking. 4 overshoots on small fonts.
    FIT_STEP_PT = 2

    # Hard ceiling on the dedup window, independent of time. DEDUP_WINDOW_MS
    # already bounds it in principle, but only for entries that pass through
    # the pruning path — see _record_seen.
    MAX_RECENT_SEEN = 200

    def _prune_recent_seen(self) -> None:
        cutoff = self._now_ms() - self.DEDUP_WINDOW_MS
        self._recent_seen = [(t, ts) for t, ts in self._recent_seen if ts >= cutoff]
        if len(self._recent_seen) > self.MAX_RECENT_SEEN:
            self._recent_seen = self._recent_seen[-self.MAX_RECENT_SEEN:]

    def _is_recent_duplicate(self, original: str) -> bool:
        if not original:
            return False
        self._prune_recent_seen()
        return any(t == original for t, _ in self._recent_seen)

    def _record_seen(self, original: str) -> None:
        """Remember an utterance for the dedup window.

        Pruning happens HERE too, not only in _is_recent_duplicate. The
        streaming path (an event carrying a result_id) records and returns
        without ever consulting the dedup check, so with pruning only on the
        read side this list grew for the whole event: measured at 3594
        entries over a simulated two-hour talk, still climbing linearly. The
        bytes were trivial; the problem was that _is_recent_duplicate scans
        the list, so the per-caption cost grew the longer the event ran.
        """
        if not original:
            return
        self._recent_seen.append((original, self._now_ms()))
        if len(self._recent_seen) > self.MAX_RECENT_SEEN:
            self._prune_recent_seen()

    # ------------------------------------------------------------------ update logic

    def _on_update(
        self,
        original: str,
        translations: dict,
        detected_language: str,
        is_final: bool,
        audio_emitted_at_ms: float,
        result_id: str = "",
    ) -> None:
        now = self._now_ms()
        detected = detected_language or None
        last = self._history[-1] if self._history else None
        # Compute latency if audio_emitted_at provided
        if is_final and audio_emitted_at_ms > 0:
            self._last_latency_ms = now - audio_emitted_at_ms

        # --- Case A0: the last utterance is still OPEN (a partial) ---
        # Any event — the next partial or the final — CONTINUES it, replacing
        # the text in place. This used to key on result_id, on the belief
        # that Azure's `recognizing` events share the final's id. Measured
        # against the real service: every event carries its own id (6 ids
        # for the 6 events of one sentence, in both LID and pinned mode), so
        # the match never fired and each partial became a new caption line —
        # the operator saw the same sentence stacked three times in three
        # versions.
        if last is not None and not last.is_final:
            changed = False
            if original and original != last.original:
                last.original = original
                changed = True
            for k, v in translations.items():
                if not v:
                    continue
                cur = last.translations.get(k, "")
                # Partial translations re-word themselves every half second
                # ("That customer service" → "This customer service"). Only
                # let a partial REPLACE when it is at least as long — the
                # translation grows instead of flickering; the final wins
                # unconditionally.
                if is_final or len(v) >= len(cur):
                    if v != cur:
                        last.translations[k] = v
                        changed = True
            if detected and detected != last.detected_language:
                last.detected_language = detected
                changed = True
            if is_final:
                last.is_final = True
                changed = True
                self._record_seen(last.original)
            last.updated_at_ms = now
            if changed:
                self._adjust_height_for_text()
                self._request_repaint()
                self._show_for_caption()
            self._idle_timer.start(self.IDLE_CLEAR_MS)
            return

        # --- Case A: incoming has same `original` as the last utterance ---
        # This is the Google two-stage emit case (interim → final-with-translation,
        # or original-empty-translation → original-with-translation).
        if last is not None and original == last.original and original:
            merged = dict(last.translations)
            merged.update({k: v for k, v in translations.items() if v})
            changed = (merged != last.translations) or (is_final and not last.is_final)
            last.translations = merged
            last.updated_at_ms = now
            if is_final:
                last.is_final = True
            if changed:
                self._adjust_height_for_text()
                self._request_repaint()
                self._show_for_caption()
                self._idle_timer.start(self.IDLE_CLEAR_MS)
            return

        # --- Case B: incoming is a recent duplicate of any utterance ---
        if self._is_recent_duplicate(original):
            log.debug("dedup: dropping recent duplicate %r", original[:60])
            return

        # --- Case C: auto-concat to last utterance ---
        # If the previous utterance was final, has same detected language,
        # and was produced recently (within concat_gap_ms), append to it
        # instead of creating a new line.
        #
        # SKIP when the incoming event carries a result_id (streaming provider
        # like Azure recognizing/recognized): those events already have crisp
        # utterance boundaries from the speech engine itself — concatenating
        # them produces ever-growing legend text. Chunk-final providers (Groq,
        # Whisper local) keep this behavior because their chunk boundaries are
        # arbitrary buffer-driven, not speech-driven.
        concat_gap = self.overlay_config.concat_gap_ms
        if (
            concat_gap > 0
            and not result_id
            and last is not None
            and last.is_final
            and last.detected_language
            and detected
            and last.detected_language == detected
            and (now - last.updated_at_ms) <= concat_gap
            and len(last.original) + len(original) < self.overlay_config.max_chars * 2
        ):
            last.original = (last.original + " " + original).strip()
            # also concatenate each translation
            for lang in set(last.translations) | set(translations):
                a = last.translations.get(lang, "")
                b = translations.get(lang, "")
                if a and b:
                    last.translations[lang] = (a + " " + b).strip()
                elif b:
                    last.translations[lang] = b
            last.detected_language = detected
            last.updated_at_ms = now
            last.is_final = is_final
            self._record_seen(original)
            self._adjust_height_for_text()
            self._request_repaint()
            self._show_for_caption()
            self._idle_timer.start(self.IDLE_CLEAR_MS)
            return

        # --- Case D: fresh utterance — push to history ---
        utt = Utterance(
            original=original,
            translations=dict(translations),
            detected_language=detected,
            created_at_ms=now,
            updated_at_ms=now,
            is_final=is_final,
            audio_emitted_at_ms=audio_emitted_at_ms if audio_emitted_at_ms > 0 else None,
            result_id=result_id or "",
        )
        self._history.append(utt)
        if len(self._history) > self._max_total:
            self._history.pop(0)
        self._record_seen(original)
        self._start_animation()
        self._adjust_height_for_text()
        self._request_repaint()
        self._show_for_caption()
        self._idle_timer.start(self.IDLE_CLEAR_MS)

    # Repaints are coalesced to this rate. Partials arrive 2–4×/s and the
    # fade animation asked for 30 fps; with a multi-line bilingual block each
    # frame is tens of ms, and unthrottled that was the whole GUI thread.
    REPAINT_MIN_MS = 66

    def _request_repaint(self) -> None:
        if not self._repaint_timer.isActive():
            self._repaint_timer.start()

    def _start_animation(self) -> None:
        """Kick off a fade+slide animation for the current state change."""
        self._anim_started_ms = self._now_ms()
        if not self._anim_timer.isActive():
            self._anim_timer.start()

    def _tick_animation(self) -> None:
        if self._now_ms() - self._anim_started_ms >= self.ANIM_DURATION_MS:
            self._anim_timer.stop()
        self._request_repaint()

    def _anim_progress(self) -> float:
        """0.0 at start of animation → 1.0 at end. Eased (cubic out)."""
        elapsed = self._now_ms() - self._anim_started_ms
        if elapsed >= self.ANIM_DURATION_MS:
            return 1.0
        t = elapsed / self.ANIM_DURATION_MS
        # cubic-out easing: 1 - (1-t)^3
        return 1.0 - (1.0 - t) ** 3

    def set_hidden_by_operator(self, hidden: bool) -> None:
        """Esconder/mostrar por decisão do operador — dura até ele desfazer."""
        self._hidden_by_operator = bool(hidden)
        if self._hidden_by_operator:
            self.hide()
        else:
            self.show()

    def is_hidden_by_operator(self) -> bool:
        return self._hidden_by_operator

    def _show_for_caption(self) -> None:
        """Legenda nova chegou: aparece, a menos que o operador tenha escondido."""
        if not self._hidden_by_operator:
            self.show()

    def _clear_all(self) -> None:
        """Idle: nobody has spoken for IDLE_CLEAR_MS. Take the band off the
        screen too — an empty black bar across the slides between talks is
        clutter, and it read to the operator as a window that 'never
        closes'. The next caption calls show() again."""
        self._history.clear()
        self._recent_seen.clear()
        self.hide()
        self._request_repaint()

    # ------------------------------------------------------------------ rendering

    def _compose_lines_with_lang(self) -> list[tuple[bool, str, float, str | None, int]]:
        """Like _compose_lines but each tuple carries colour and utterance id."""
        return list(self._compose_lines_internal())

    def _compose_lines_internal(self) -> list[tuple[bool, str, float, str | None]]:
        """Returns list of (is_primary, text, alpha, lang_color) tuples in render order
        (top to bottom). Alpha 0..1 controls dimming for older lines.

        - The newest utterance (last) is rendered as primary (full alpha, big font).
        - Previous utterances are rendered as secondary (smaller, dimmer).
        - For each utterance, we apply display_mode logic to pick original vs
          translation vs both.
        - lang_color: hex string for the left-side accent bar, or None.
        """
        if not self._history:
            return []

        targets = self.app_config.target_languages
        primary_lang = targets[0] if targets else ""

        lines: list[tuple[bool, str, float, str | None, int]] = []
        n = len(self._history)
        for i, utt in enumerate(self._history):
            is_current = i == n - 1
            if is_current:
                before_current = len(lines)
            alpha = 1.0 if is_current else (0.45 + 0.4 * (i / max(1, n - 1)))
            lang_color = utt.language_color()

            translation = utt.primary_translation(primary_lang)

            if self.display_mode == "translations_only":
                # Strict: only translation rows. If translation hasn't arrived
                # yet, show NOTHING for this utterance (cleaner UX than briefly
                # flashing the source language).
                if translation:
                    lines.append((is_current, translation, alpha, lang_color, i))
            elif self.display_mode == "original_plus_translation":
                if utt.original:
                    lines.append((False, self._truncate(utt.original), alpha * 0.7, lang_color, i))
                # EVERY target, not only the first: with EN+ES configured the
                # Spanish line simply never appeared in this mode.
                shown = False
                for j, lang in enumerate(targets):
                    txt = utt.translations.get(lang, "")
                    if txt:
                        lines.append((is_current and j == 0, txt,
                                      alpha if j == 0 else alpha * 0.85, lang_color, i))
                        shown = True
                if not shown and utt.original:
                    lines.append((is_current, "…", alpha * 0.6, lang_color, i))
            elif self.display_mode == "translations_only_multi":
                # Bilingual projection: two audiences, two languages, EQUAL
                # weight — same size, same brightness, each in its fixed slot
                # (config order), and the accent bar coloured by the TARGET
                # language so a reader finds "their" line at a glance. Making
                # the second language smaller and dimmer told the Spanish
                # speakers they were the afterthought.
                for lang in targets:
                    txt = utt.translations.get(lang, "")
                    if txt:
                        lines.append((is_current, txt, alpha,
                                      _color_for_language(lang) or lang_color, i))
            else:
                if translation:
                    lines.append((is_current, translation, alpha, lang_color, i))

        self._current_block_lines = len(lines) - before_current if n else 0
        return lines

    def _truncate(self, text: str) -> str:
        max_chars = self.overlay_config.max_chars
        if max_chars and len(text) > max_chars:
            return "…" + text[-max_chars:]
        return text

    def _adjust_height_for_text(self) -> None:
        cfg = self.overlay_config
        if getattr(cfg, "stable_height", True):
            # Fixed band: the window geometry must NOT follow the text.
            # Resizing here on every event is what made the caption jump
            # around the projection screen as lines grew and shrank.
            return
        primary_font = QFont(cfg.font_family, cfg.primary_font_size, QFont.Weight.Bold)
        secondary_font = QFont(cfg.font_family, cfg.secondary_font_size, QFont.Weight.Normal)
        primary_metrics = QFontMetrics(primary_font)
        secondary_metrics = QFontMetrics(secondary_font)

        inner_width = self.width() - cfg.padding * 2
        if inner_width <= 0:
            return
        height = cfg.padding * 2

        for is_primary, text, _alpha, _lang, _utt in self._compose_lines_with_lang():
            if not text:
                continue
            metrics = primary_metrics if is_primary else secondary_metrics
            line_height = metrics.lineSpacing()
            wrapped = self._wrap_lines(text, metrics, inner_width)
            height += line_height * max(1, len(wrapped)) + 6

        screen = self._screen()
        if screen is not None:
            geo = screen.availableGeometry()
            height = min(height, int(geo.height() * 0.5))

        if height < cfg.padding * 2 + 20:
            height = cfg.padding * 2 + 20  # avoid degenerate window
        self.setFixedHeight(height)
        self._apply_position()

    # Words that do not end a line (short articles, prepositions and
    # conjunctions of PT/EN/ES). "…começa com o | estudo" reads worse than
    # "…começa com | o estudo": the eye expects the noun right after them.
    # Subtitle style guides call this breaking at a linguistic boundary.
    NO_DANGLE = frozenset(
        "a o e à ao as os um uma de do da dos das em no na nos nas por com que se "
        "the an of to in on at by for from with and or "
        "el la los las un una del al en y con para".split())

    @classmethod
    def _wrap_lines(cls, text: str, metrics: QFontMetrics, max_width: int) -> list[str]:
        if not text:
            return [""]
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip() if current else word
            if metrics.horizontalAdvance(candidate) <= max_width:
                current = candidate
            else:
                if current:
                    kept = current.split(" ")
                    # A dangling "o"/"de"/"the" moves down with the word that
                    # did not fit — the whole run of them ("con los"), or the
                    # last one just gets exposed. Never empties the line.
                    moved: list[str] = []
                    while (len(kept) > 1 and kept[-1].isalpha()
                           and kept[-1].lower() in cls.NO_DANGLE):
                        moved.insert(0, kept.pop())
                    if moved:
                        word = " ".join(moved + [word])
                        current = " ".join(kept)
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    # ------------------------------------------------------------------ paint

    def paintEvent(self, event) -> None:
        """Guarded: a bug in the paint path must not kill the session.

        PyQt turns an unhandled exception in a slot into qFatal() -> abort(),
        and paintEvent runs on every frame. DEMONSTRATED while writing the
        change above: one wrong tuple unpack in here killed the whole process
        with no traceback anywhere — the same silent death the operator kept
        reporting. A caption that fails to draw should cost one blank frame
        and a log line, never the event in front of an audience.
        """
        try:
            self._paint(event)
        except Exception:
            # Once per widget: a broken paint repeats ~15x a second and would
            # fill the log faster than the talk lasts.
            if not getattr(self, "_paint_failed", False):
                self._paint_failed = True
                log.exception("caption paint failed; band stays blank")

    def _paint(self, event) -> None:
        cfg = self.overlay_config
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        bg = QColor(cfg.background_color)
        bg.setAlphaF(cfg.background_opacity)
        painter.setBrush(bg)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 16, 16)

        lines = self._compose_lines_with_lang()
        if not any(text for _, text, _, _, _ in lines):
            hint = self._empty_hint()
            if hint:
                painter.setFont(QFont(cfg.font_family, max(9, cfg.secondary_font_size // 2)))
                dim = QColor(cfg.secondary_color)
                dim.setAlphaF(0.55)
                painter.setPen(dim)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, hint)
            return

        primary_font = QFont(cfg.font_family, cfg.primary_font_size, QFont.Weight.Bold)
        secondary_font = QFont(cfg.font_family, cfg.secondary_font_size, QFont.Weight.Normal)

        primary_color = QColor(cfg.primary_color)
        secondary_color = QColor(cfg.secondary_color)
        outline_color = QColor(cfg.outline_color)

        anim_progress = self._anim_progress()  # 0..1
        # newest line is the last in our list — animate it fading in
        last_idx = len(lines) - 1

        x = cfg.padding
        y = cfg.padding
        inner_width = self.width() - cfg.padding * 2 - 14  # leave space for color bar

        if getattr(cfg, "stable_height", True):
            # The band does not grow with the text, so the text must yield:
            # drop the OLDEST lines until the block fits. Otherwise a second
            # target language or a long wrap pushes the NEWEST line — the
            # one the room is reading — off the bottom edge.
            def _heights() -> list[int]:
                out = []
                prev_utt = None
                for p, t, _a, _c, u in lines:
                    if not t:
                        out.append(0)
                        continue
                    m = QFontMetrics(primary_font if p else secondary_font)
                    h = m.lineSpacing() * len(self._wrap_lines(t, m, inner_width)) + 6
                    # Folga ao TROCAR de fala. As linhas de uma mesma fala (um
                    # idioma cada) ficam juntas e as falas ficam separadas; sem
                    # isso, com dois idiomas e historico ligado a banda vira
                    # ES/EN/ES/EN sem agrupamento nenhum, que e o que o
                    # operador descreveu como "as letras misturam".
                    if prev_utt is not None and u != prev_utt:
                        h += self.UTTERANCE_GAP_PX
                    prev_utt = u
                    out.append(h)
                return out

            available = self.height() - cfg.padding * 2
            ratio = cfg.secondary_font_size / max(1, cfg.primary_font_size)
            step = self.FIT_STEP_PT

            def _fonts(pt: int) -> None:
                nonlocal primary_font, secondary_font
                primary_font = QFont(cfg.font_family, pt, QFont.Weight.Bold)
                secondary_font = QFont(
                    cfg.font_family, max(self.MIN_FIT_PT_HARD, int(pt * ratio)),
                    QFont.Weight.Normal)

            # The size a sentence already shrank to stays while THAT sentence
            # keeps changing (partials, then the final). Refitting from the
            # configured size on every event made the type pulse
            # mid-sentence, which on a 4 m screen reads as flicker.
            cur_key = self._history[-1].created_at_ms if self._history else None
            size = cfg.primary_font_size
            if self._fit_lock is not None and self._fit_lock[0] == cur_key:
                size = min(size, self._fit_lock[1])
                _fonts(size)
            heights = _heights()
            # Order, for a room reading from the back: drop OLDER utterances
            # first, and shrink the type only when the newest utterance alone
            # still does not fit. Shrinking first kept more history on screen
            # at a size nobody past the third row could read. The newest
            # utterance is never cut: with "spoken + EN + ES" an older rule
            # dropped the Portuguese and the English of the CURRENT sentence
            # and left only Spanish — the languages alternated from one
            # sentence to the next.
            keep = max(1, self._current_block_lines)
            while len(lines) > keep and sum(heights) > available:
                lines.pop(0)
                heights.pop(0)
            while sum(heights) > available and size - step >= self.MIN_FIT_PT_HARD:
                size -= step
                _fonts(size)
                heights = _heights()
            self._fit_lock = (cur_key, size)
            last_idx = len(lines) - 1
            if getattr(cfg, "anchor_newest", True):
                # Bottom-anchor the block inside the fixed band so the NEWEST
                # line always renders at the same spot and older lines push
                # upward. Top-anchoring inside a fixed band moves every line
                # down as the block grows — the same jitter, just relocated.
                y = max(cfg.padding, self.height() - cfg.padding - sum(heights))

        prev_utt = None
        for idx, (is_primary, text, alpha, lang_color, utt) in enumerate(lines):
            if not text:
                continue
            if prev_utt is not None and utt != prev_utt:
                y += self.UTTERANCE_GAP_PX
            prev_utt = utt
            font = primary_font if is_primary else secondary_font
            base = primary_color if is_primary else secondary_color

            # Apply animation: newest line fades from 0→alpha, others from prev_alpha→alpha
            if idx == last_idx and anim_progress < 1.0:
                line_alpha = alpha * anim_progress
                # slide-up offset: starts +18px, ends 0
                slide_offset = int((1.0 - anim_progress) * 14)
            else:
                line_alpha = alpha
                slide_offset = 0

            metrics = QFontMetrics(font)
            line_height = metrics.lineSpacing()
            wrapped = self._wrap_lines(text, metrics, inner_width)

            block_height = line_height * len(wrapped) + 4

            # Draw left-side color bar for this utterance (language indicator)
            if lang_color:
                bar_color = QColor(lang_color)
                bar_color.setAlphaF(line_alpha * 0.85)
                painter.setBrush(bar_color)
                painter.setPen(Qt.PenStyle.NoPen)
                # Wider on the projection: the colour is how a reader across
                # the room finds their language's line.
                bar_w = (self.LANG_BAR_PX_PRESENTATION if self._presentation
                         else self.LANG_BAR_PX)
                painter.drawRoundedRect(
                    self._bar_x(cfg, bar_w),
                    y + slide_offset,
                    bar_w,
                    block_height,
                    3, 3,
                )

            # A barra ficou grossa: o texto precisa comecar depois dela.
            _bw = ((self.LANG_BAR_PX_PRESENTATION if self._presentation
                    else self.LANG_BAR_PX) if lang_color else 0)
            text_x = max(x + 4, self._bar_x(cfg, _bw) + _bw + 10) if _bw else x + 4
            for line in wrapped:
                self._draw_line(painter, line, text_x, y + slide_offset,
                                font, base, outline_color, line_alpha)
                y += line_height
            y += 6

        # Latency badge (top-right corner) — small, dim. Operator telemetry,
        # so never on the projected band.
        if self._last_latency_ms is not None and not self._presentation:
            latency_text = f"{self._last_latency_ms / 1000:.1f}s"
            badge_font = QFont(cfg.font_family, max(8, cfg.secondary_font_size - 8))
            painter.setFont(badge_font)
            badge_color = QColor("#888888")
            badge_color.setAlphaF(0.7)
            painter.setPen(badge_color)
            metrics = QFontMetrics(badge_font)
            tw = metrics.horizontalAdvance(latency_text)
            painter.drawText(
                self.width() - cfg.padding - tw,
                cfg.padding // 2 + metrics.ascent(),
                latency_text,
            )

    # Every drawn line is kept as an image. Past ~64 device pixels of glyph
    # height Qt leaves its glyph cache and traces each letter as a curve on
    # every frame: MEASURED on the real band, screen at 125 %, 80 ms per
    # paint at 54 pt (Modo evento) against 4 ms at 25 pt, for a 33 ms
    # budget. Copying the finished image costs ~1 ms, and only a line whose
    # text changed is rendered again.
    LINE_CACHE_MAX = 32
    LINE_PAD = 4                  # room for the 2 px outline offsets
    # Over a solid box the outline adds nothing a reader can see, and it is
    # 9 drawText calls per line instead of 1 — nearly the whole cost of
    # rendering a line at large sizes. Kept for translucent backgrounds,
    # where it is what keeps white text readable over a white slide.
    OUTLINE_BELOW_BG_OPACITY = 0.6

    def _line_pixmap(self, text: str, font: QFont, fill: QColor, outline: QColor) -> QPixmap:
        dpr = self.devicePixelRatioF()
        with_outline = self.overlay_config.background_opacity < self.OUTLINE_BELOW_BG_OPACITY
        key = (text, font.family(), font.pointSize(), font.weight(),
               fill.rgba(), outline.rgba(), with_outline, round(dpr, 3))
        pm = self._line_cache.get(key)
        if pm is not None:
            self._line_cache.move_to_end(key)
            return pm
        metrics = QFontMetrics(font)
        pad = self.LINE_PAD
        w = max(metrics.horizontalAdvance(text), metrics.boundingRect(text).width()) + pad * 2
        h = metrics.lineSpacing() + pad * 2
        pm = QPixmap(max(1, int(w * dpr) + 1), max(1, int(h * dpr) + 1))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if with_outline:
            self._draw_outlined_text(p, text, pad, pad + metrics.ascent(), fill, outline, font)
        else:
            p.setFont(font)
            p.setPen(fill)
            p.drawText(pad, pad + metrics.ascent(), text)
        p.end()
        self._line_cache[key] = pm
        while len(self._line_cache) > self.LINE_CACHE_MAX:
            self._line_cache.popitem(last=False)
        return pm

    def _draw_line(self, painter: QPainter, text: str, x: int, top: int,
                   font: QFont, fill: QColor, outline: QColor, alpha: float) -> None:
        """One wrapped line with its top-left at (x, top).

        Dimming of older lines and the fade-in of the newest one are painter
        opacity over the cached image, so one image serves every alpha.
        """
        pm = self._line_pixmap(text, font, fill, outline)
        dpr = self.devicePixelRatioF()
        # Snap to the device pixel grid: at 125 % an integer logical x lands
        # on a quarter pixel and the copy would be resampled — soft text.
        pos = QPointF(round((x - self.LINE_PAD) * dpr) / dpr,
                      round((top - self.LINE_PAD) * dpr) / dpr)
        painter.setOpacity(min(1.0, max(0.0, alpha)))
        painter.drawPixmap(pos, pm)
        painter.setOpacity(1.0)

    @staticmethod
    def _draw_outlined_text(
        painter: QPainter,
        text: str,
        x: int,
        y: int,
        fill: QColor,
        outline: QColor,
        font: QFont,
    ) -> None:
        # Glyph-run text with an 8-direction offset "outline". Runs only
        # while a cached line is built (see _line_pixmap), and only over a
        # translucent background.
        painter.setFont(font)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(outline)
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            painter.drawText(x + dx, y + dy, text)
        painter.setPen(fill)
        painter.drawText(x, y, text)

    # ------------------------------------------------------------------ drag / resize

    def _empty_hint(self) -> str:
        """Text for a box the operator made visible with nothing in it — the
        moment they are arranging the boxes before the event. Never on the
        projection (presentation mode): the room does not need to know."""
        if self._presentation:
            return ""
        if self.overlay_config.locked:
            return "Legenda travada — destrave no menu da bandeja para mover"
        return "Arraste para mover · borda direita e inferior redimensionam · fica guardado"

    def _mouse_free(self) -> bool:
        # `locked`: a faixa posicionada para o evento não sai do lugar com um
        # esbarrão no mouse da máquina que projeta. O × tem o próprio botão e
        # continua respondendo.
        return not self.overlay_config.click_through and not self.overlay_config.locked

    def _edges_at(self, pos) -> tuple[bool, bool]:
        return (pos.x() >= self.width() - self.RESIZE_GRIP_PX,
                pos.y() >= self.height() - self.RESIZE_GRIP_PX)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._mouse_free():
            gp = event.globalPosition().toPoint()
            right, bottom = self._edges_at(event.position())
            if right or bottom:
                self._resizing = (right, bottom)
                self._resize_anchor = (gp, self.size())
            else:
                self._dragging = True
                self._drag_offset = gp - self.frameGeometry().topLeft()
            self._press_geometry = self.geometry()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        gp = event.globalPosition().toPoint()
        if self._dragging:
            self.move(gp - self._drag_offset)
            event.accept()
        elif any(self._resizing):
            start, size = self._resize_anchor
            w = size.width() + (gp.x() - start.x()) if self._resizing[0] else size.width()
            h = size.height() + (gp.y() - start.y()) if self._resizing[1] else size.height()
            self.resize(max(self.MIN_W, w), max(self._min_height(), h))
            event.accept()
        elif self._mouse_free():
            right, bottom = self._edges_at(event.position())
            self.setCursor({(True, True): Qt.CursorShape.SizeFDiagCursor,
                            (True, False): Qt.CursorShape.SizeHorCursor,
                            (False, True): Qt.CursorShape.SizeVerCursor}
                           .get((right, bottom), Qt.CursorShape.OpenHandCursor))
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_editing = self._dragging or any(self._resizing)
        self._dragging = False
        self._resizing = (False, False)
        # A click that did not move anything is not an edit to remember.
        if was_editing and self.geometry() != self._press_geometry:
            self.geometry_edited.emit(self._ratios())


def _demo() -> None:  # pragma: no cover
    import sys

    app = QApplication(sys.argv)
    cfg = AppConfig()
    overlay = CaptionOverlay(cfg)
    overlay.show()

    # simulate a sequence of utterances arriving
    seq = [
        ("Welcome everyone to today's webinar.", {"es": "Bienvenidos todos al seminario de hoy."}, "en", True),
        ("We will discuss research methods.", {"es": "Discutiremos métodos de investigación."}, "en", True),
        ("Vamos começar com a introdução.", {"es": "Vamos a empezar con la introducción."}, "pt", True),
        ("Esse tema é muito importante.", {"es": "Este tema es muy importante."}, "pt", True),
    ]
    timer = QTimer()
    idx = [0]
    def tick():
        if idx[0] < len(seq):
            orig, trans, lang, fin = seq[idx[0]]
            overlay.push_caption(orig, trans, lang, fin)
            idx[0] += 1
    timer.timeout.connect(tick)
    timer.start(2500)

    sys.exit(app.exec())


if __name__ == "__main__":
    _demo()
