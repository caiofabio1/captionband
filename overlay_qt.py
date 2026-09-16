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
- **Min display time**: each new caption stays at least 1.5s before being
  replaced (queues if events come faster).
- **Idle clear**: caption fades out 15s after the last event.

Display modes:
  * "translations_only" — translation only (with original dim above while pending)
  * "original_plus_translation" — original (dim) + translation (bright)
  * "translations_only_multi" — multiple target languages stacked
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QMouseEvent,
    QPainter,
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
        self._presentation = False
        # How many composed lines belong to the NEWEST utterance. The fixed
        # band may drop older lines when it overflows, never these.
        self._current_block_lines = 0
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

    def _apply_position(self) -> None:
        screen = self._screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        width = int(geo.width() * self.overlay_config.width_ratio)
        height = self._estimate_height()
        x = geo.x() + (geo.width() - width) // 2

        position = self.overlay_config.position
        if position == "top":
            y = geo.y() + 40
        elif position == "middle":
            y = geo.y() + (geo.height() - height) // 2
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
        lines = max(1, int(getattr(cfg, "reserved_lines", 3)), per_utt * 2)
        # The newest utterance is primary; whatever else fits is secondary.
        height = cfg.padding * 2 + primary * min(lines, per_utt * 2) + 6
        height += max(0, lines - per_utt * 2) * (secondary + 6)

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
        self._request_repaint()

    # ------------------------------------------------------------------ dedup

    def _now_ms(self) -> float:
        import time
        return time.monotonic() * 1000

    # Smallest point size the fixed band may shrink the caption to before it
    # starts dropping history lines instead. RELATIVE to the size the operator
    # chose, not an absolute point value: as a hard 26 it silently disabled
    # itself for every font below 30 pt, because the loop condition is
    # `size - step >= floor`. The operator's live config is 25 pt, so the
    # comfortable-shrink pass never ran at all and the band jumped straight to
    # discarding history — the opposite of what this was written to do.
    MIN_FIT_RATIO = 0.72
    # Absolute floor, used only when even the current utterance alone does not
    # fit the band. Below this it is unreadable from the back of a room.
    MIN_FIT_PT_HARD = 16
    # Point size step while shrinking. 4 overshoots on small fonts.
    FIT_STEP_PT = 2

    def _min_fit_pt(self, configured: int) -> int:
        """Comfortable floor for this operator's font size."""
        return max(self.MIN_FIT_PT_HARD, int(configured * self.MIN_FIT_RATIO))

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
                self.show()
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
                self.show()
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
            self.show()
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
        self.show()
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

    def _compose_lines_with_lang(self) -> list[tuple[bool, str, float, str | None]]:
        """Like _compose_lines but each tuple has language color at the end."""
        out: list[tuple[bool, str, float, str | None]] = []
        for is_primary, text, alpha, lang_color in self._compose_lines_internal():
            out.append((is_primary, text, alpha, lang_color))
        return out

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

        lines: list[tuple[bool, str, float, str | None]] = []
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
                    lines.append((is_current, translation, alpha, lang_color))
            elif self.display_mode == "original_plus_translation":
                if utt.original:
                    lines.append((False, self._truncate(utt.original), alpha * 0.7, lang_color))
                # EVERY target, not only the first: with EN+ES configured the
                # Spanish line simply never appeared in this mode.
                shown = False
                for j, lang in enumerate(targets):
                    txt = utt.translations.get(lang, "")
                    if txt:
                        lines.append((is_current and j == 0, txt,
                                      alpha if j == 0 else alpha * 0.85, lang_color))
                        shown = True
                if not shown and utt.original:
                    lines.append((is_current, "…", alpha * 0.6, lang_color))
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
                                      _color_for_language(lang) or lang_color))
            else:
                if translation:
                    lines.append((is_current, translation, alpha, lang_color))

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

        for is_primary, text, _alpha, _lang in self._compose_lines_with_lang():
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

    @staticmethod
    def _wrap_lines(text: str, metrics: QFontMetrics, max_width: int) -> list[str]:
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
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    # ------------------------------------------------------------------ paint

    def paintEvent(self, event) -> None:
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
        if not any(text for _, text, _, _ in lines):
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
                for p, t, _a, _c in lines:
                    if not t:
                        out.append(0)
                        continue
                    m = QFontMetrics(primary_font if p else secondary_font)
                    out.append(m.lineSpacing() * len(self._wrap_lines(t, m, inner_width)) + 6)
                return out

            available = self.height() - cfg.padding * 2
            heights = _heights()
            # First make the TYPE yield, down to a floor: in the bilingual
            # layout the block is one utterance in two languages, and
            # dropping "the oldest line" there drops a whole language — the
            # Spanish speakers read on while the English speakers get
            # nothing (seen in an offscreen render: only 'La primera sesión…'
            # survived). ponytail: recomputed per paint; a per-session
            # hysteresis would stop the size hopping between utterances.
            size = cfg.primary_font_size
            ratio = cfg.secondary_font_size / max(1, cfg.primary_font_size)
            soft_floor = self._min_fit_pt(cfg.primary_font_size)
            step = self.FIT_STEP_PT
            while sum(heights) > available and size - step >= soft_floor:
                size -= step
                primary_font = QFont(cfg.font_family, size, QFont.Weight.Bold)
                secondary_font = QFont(cfg.font_family,
                                       max(self.MIN_FIT_PT_HARD, int(size * ratio)),
                                       QFont.Weight.Normal)
                heights = _heights()
            # Only then drop HISTORY lines (older utterances). The newest
            # utterance is never cut: with "spoken + EN + ES" the old rule
            # dropped the Portuguese and the English of the CURRENT sentence
            # and left only Spanish — the operator saw the languages
            # alternate from one sentence to the next.
            keep = max(1, self._current_block_lines)
            while len(lines) > keep and sum(heights) > available:
                lines.pop(0)
                heights.pop(0)
            # Still too tall with only the current utterance left: go smaller
            # than the comfortable floor rather than clip a language.
            while sum(heights) > available and size - step >= self.MIN_FIT_PT_HARD:
                size -= step
                primary_font = QFont(cfg.font_family, size, QFont.Weight.Bold)
                secondary_font = QFont(cfg.font_family, max(self.MIN_FIT_PT_HARD, int(size * ratio)),
                                       QFont.Weight.Normal)
                heights = _heights()
            last_idx = len(lines) - 1
            if getattr(cfg, "anchor_newest", True):
                # Bottom-anchor the block inside the fixed band so the NEWEST
                # line always renders at the same spot and older lines push
                # upward. Top-anchoring inside a fixed band moves every line
                # down as the block grows — the same jitter, just relocated.
                y = max(cfg.padding, self.height() - cfg.padding - sum(heights))

        for idx, (is_primary, text, alpha, lang_color) in enumerate(lines):
            if not text:
                continue
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

            color = QColor(base)
            color.setAlphaF(min(1.0, max(0.0, line_alpha)))
            outline = QColor(outline_color)
            outline.setAlphaF(min(1.0, max(0.0, line_alpha)))

            metrics = QFontMetrics(font)
            painter.setFont(font)
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
                bar_w = 8 if self._presentation else 4
                painter.drawRoundedRect(
                    cfg.padding - 4 - bar_w,
                    y + slide_offset,
                    bar_w,
                    block_height,
                    2, 2,
                )

            for line in wrapped:
                self._draw_outlined_text(
                    painter, line,
                    x + 4,
                    y + slide_offset + metrics.ascent(),
                    color, outline, font,
                )
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
        # Glyph-run text with an 8-direction offset "outline". Stroking a
        # QPainterPath of the text cost 82 ms per frame with nine lines
        # (measured with the operator's config), and the overlay repaints on
        # every partial — the tray menu stopped responding.
        painter.setFont(font)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(outline)
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            painter.drawText(x + dx, y + dy, text)
        painter.setPen(fill)
        painter.drawText(x, y, text)

    # ------------------------------------------------------------------ drag

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.overlay_config.click_through:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._dragging = False


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
