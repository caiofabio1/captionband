"""Live transcript writer.

Saves every final caption to two files in real-time:
- session_<timestamp>_<provider>.txt — readable plain text
- session_<timestamp>_<provider>.srt — SubRip subtitle file (for YouTube/VLC)

Files live at %LOCALAPPDATA%\\CaptionBand\\transcripts\\.

The .txt is written as captions arrive (line-buffered, append).
The .srt accumulates all entries in memory and is finalized on stop()
because SRT requires sequential index numbering and end timestamps.

Retention: on each new session start, old transcripts are pruned. Keeps at
most TRANSCRIPT_KEEP_SESSIONS sessions (txt+srt counted as one) and deletes
anything older than TRANSCRIPT_MAX_AGE_DAYS regardless of count.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TextIO

from config import app_data_dir
from constants import (
    TRANSCRIPT_KEEP_SESSIONS,
    TRANSCRIPT_MAX_AGE_DAYS,
    TRANSCRIPT_SRT_CHECKPOINT_EVERY,
)


log = logging.getLogger(__name__)


def _apply_retention(base_dir: Path) -> None:
    """Delete old transcript files based on count and age limits."""
    if not base_dir.exists():
        return
    files = list(base_dir.glob("session_*.txt")) + list(base_dir.glob("session_*.srt"))
    if not files:
        return
    cutoff = time.time() - (TRANSCRIPT_MAX_AGE_DAYS * 86400)

    # Group txt+srt as one session by base stem (filename without extension)
    sessions: dict[str, list[Path]] = {}
    for p in files:
        sessions.setdefault(p.stem, []).append(p)

    # Order sessions newest-first by max mtime
    ordered = sorted(
        sessions.items(),
        key=lambda kv: max(p.stat().st_mtime for p in kv[1]),
        reverse=True,
    )

    deleted = 0
    for i, (_stem, paths) in enumerate(ordered):
        too_old = all(p.stat().st_mtime < cutoff for p in paths)
        too_many = i >= TRANSCRIPT_KEEP_SESSIONS
        if too_old or too_many:
            for p in paths:
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass

    if deleted:
        log.info("transcript retention pruned %s files in %s", deleted, base_dir)


@dataclass
class TranscriptEntry:
    start_offset_s: float
    end_offset_s: float
    detected_language: Optional[str]
    original_text: str
    translations: dict[str, str]


class TranscriptWriter:
    def __init__(self, provider_name: str, target_languages: list[str]):
        self.provider_name = provider_name
        self.target_languages = target_languages
        self.start_time: Optional[float] = None
        self._lock = threading.Lock()
        self._txt_file: Optional[TextIO] = None
        self._txt_path: Optional[Path] = None
        self._srt_path: Optional[Path] = None
        self._entries: list[TranscriptEntry] = []
        self._last_end_offset: float = 0.0

    @property
    def txt_path(self) -> Optional[Path]:
        return self._txt_path

    @property
    def srt_path(self) -> Optional[Path]:
        return self._srt_path

    def start(self) -> None:
        with self._lock:
            if self._txt_file is not None:
                return
            self.start_time = time.monotonic()
            self._entries.clear()
            self._last_end_offset = 0.0

            base_dir = app_data_dir() / "transcripts"
            base_dir.mkdir(parents=True, exist_ok=True)

            # Apply retention policy before writing the new session
            try:
                _apply_retention(base_dir)
            except Exception:
                log.exception("transcript retention failed (non-fatal)")

            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            base_name = f"session_{stamp}_{self.provider_name}"
            self._txt_path = base_dir / f"{base_name}.txt"
            self._srt_path = base_dir / f"{base_name}.srt"

            self._txt_file = self._txt_path.open("w", encoding="utf-8", buffering=1)
            self._txt_file.write(
                f"# CaptionBand — Transcrição\n"
                f"# Início: {datetime.now().isoformat(timespec='seconds')}\n"
                f"# Provedor: {self.provider_name}\n"
                f"# Targets: {', '.join(self.target_languages)}\n\n"
            )
            self._txt_file.flush()
            log.info("transcript started: %s", self._txt_path)

    def append(
        self,
        original_text: str,
        translations: dict[str, str],
        detected_language: Optional[str],
        is_final: bool,
    ) -> None:
        # Only persist final translations (the events with the actual translation text)
        if not is_final or not original_text:
            return

        with self._lock:
            if self._txt_file is None or self.start_time is None:
                return
            now_offset = time.monotonic() - self.start_time
            entry = TranscriptEntry(
                start_offset_s=self._last_end_offset,
                end_offset_s=now_offset,
                detected_language=detected_language,
                original_text=original_text,
                translations=dict(translations),
            )
            self._entries.append(entry)
            self._last_end_offset = now_offset

            # Append to txt — readable format
            ts = datetime.now().strftime("%H:%M:%S")
            lang_tag = f"[{detected_language}]" if detected_language else ""
            try:
                self._txt_file.write(f"\n{ts} {lang_tag} {original_text}\n")
                for lang, txt in translations.items():
                    self._txt_file.write(f"        → [{lang}] {txt}\n")
                self._txt_file.flush()
            except Exception:
                log.exception("failed to write transcript line")

            if len(self._entries) % TRANSCRIPT_SRT_CHECKPOINT_EVERY == 0:
                self._write_all_srt()

    def _write_all_srt(self) -> None:
        """One .srt per target language. Caller holds the lock.

        The first target keeps the plain `.srt` name; the others get
        `.<lang>.srt`, so a PT talk translated to EN and ES yields two files
        ready for YouTube instead of an SRT that silently carries only one.
        """
        if self._srt_path is None or not self._entries:
            return
        targets = self.target_languages or [None]
        for i, lang in enumerate(targets):
            path = self._srt_path if i == 0 else self.srt_path_for(lang)
            try:
                self._write_srt(path, self._entries, lang)
            except Exception:
                log.exception("failed to write SRT %s", path)

    def srt_path_for(self, lang: str) -> Optional[Path]:
        if self._srt_path is None:
            return None
        if self.target_languages and lang == self.target_languages[0]:
            return self._srt_path
        return self._srt_path.with_name(
            "{}.{}.srt".format(self._srt_path.stem, lang))

    def stop(self) -> None:
        with self._lock:
            if self._txt_file is None:
                return
            try:
                self._txt_file.write(
                    f"\n# Fim: {datetime.now().isoformat(timespec='seconds')}\n"
                )
                self._txt_file.close()
            except Exception:
                pass
            self._txt_file = None

            self._write_all_srt()
            if self._entries:
                log.info("transcript saved: %s + %s", self._txt_path, self._srt_path)

    def _write_srt(self, path: Path, entries: list[TranscriptEntry],
                   target: Optional[str]) -> None:
        """Write SubRip format with `target`'s translation as the subtitle
        line (falling back to the original when that translation is missing)."""
        primary_target = target

        def fmt_time(s: float) -> str:
            td = timedelta(seconds=s)
            total_ms = int(td.total_seconds() * 1000)
            hours, rem = divmod(total_ms, 3600 * 1000)
            mins, rem = divmod(rem, 60 * 1000)
            secs, ms = divmod(rem, 1000)
            return f"{hours:02d}:{mins:02d}:{secs:02d},{ms:03d}"

        with path.open("w", encoding="utf-8") as fh:
            for i, e in enumerate(entries, 1):
                # Each subtitle is shown for at most 5s (live captioning best
                # practice). If the next entry is sooner, we end at its start.
                start = e.start_offset_s
                end_cap = start + 5.0
                end = min(e.end_offset_s, end_cap)
                if i < len(entries):
                    next_start = entries[i].start_offset_s
                    end = min(end, next_start)
                if end <= start:
                    end = start + 0.5

                if primary_target and e.translations.get(primary_target):
                    text = e.translations[primary_target]
                else:
                    text = e.original_text

                fh.write(f"{i}\n")
                fh.write(f"{fmt_time(start)} --> {fmt_time(end)}\n")
                fh.write(f"{text}\n\n")

    def is_active(self) -> bool:
        return self._txt_file is not None
