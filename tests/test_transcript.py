"""Tests for the transcript writer (.txt + .srt) and retention policy."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import config as config_mod
from transcript import TranscriptWriter


@pytest.fixture
def isolated_app_dir(monkeypatch, tmp_path):
    # Monkeypatch BOTH the original binding and the one already imported into
    # transcript module (since transcript.py does `from config import app_data_dir`)
    monkeypatch.setattr(config_mod, "app_data_dir", lambda: tmp_path)
    import transcript as transcript_mod
    monkeypatch.setattr(transcript_mod, "app_data_dir", lambda: tmp_path)
    return tmp_path


class TestTranscriptWriter:
    def test_creates_files_on_start(self, isolated_app_dir):
        w = TranscriptWriter(provider_name="azure", target_languages=["es"])
        w.start()
        assert w.txt_path is not None
        assert w.txt_path.exists()
        assert w.srt_path is not None
        # SRT is only written on stop AND only if there are entries —
        # add one so we can verify it materializes.
        w.append("hello", {"es": "hola"}, "en-US", is_final=True)
        w.stop()
        assert w.srt_path.exists()

    def test_appends_only_final_events_with_translations(self, isolated_app_dir):
        w = TranscriptWriter(provider_name="azure", target_languages=["es"])
        w.start()
        # Interim event — should NOT be persisted
        w.append("Hello", {}, "en-US", is_final=False)
        # Final without translation — should NOT be persisted (only final WITH translation)
        w.append("Hello", {}, "en-US", is_final=True)
        # Final with translation — should be persisted
        w.append("Hello", {"es": "Hola"}, "en-US", is_final=True)
        w.stop()

        txt = w.txt_path.read_text(encoding="utf-8")
        assert "Hola" in txt
        # Make sure the interim/no-translation versions did not get written multiple times
        assert txt.count("Hola") == 1

    def test_srt_format_is_valid(self, isolated_app_dir):
        w = TranscriptWriter(provider_name="azure", target_languages=["es"])
        w.start()
        w.append("Hello world", {"es": "Hola mundo"}, "en-US", is_final=True)
        time.sleep(0.05)  # ensure non-zero offset between entries
        w.append("Goodbye", {"es": "Adiós"}, "en-US", is_final=True)
        w.stop()

        srt = w.srt_path.read_text(encoding="utf-8")
        # Should have 2 entries
        assert "1\n" in srt
        assert "2\n" in srt
        assert "Hola mundo" in srt
        assert "Adiós" in srt
        # Timestamp format HH:MM:SS,mmm
        import re
        timestamps = re.findall(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", srt)
        assert len(timestamps) == 2

    def test_no_writes_after_stop(self, isolated_app_dir):
        w = TranscriptWriter(provider_name="azure", target_languages=["es"])
        w.start()
        w.append("first", {"es": "primero"}, "en-US", is_final=True)
        w.stop()
        # Append after stop should be a no-op (not raise)
        w.append("second", {"es": "segundo"}, "en-US", is_final=True)
        txt = w.txt_path.read_text(encoding="utf-8")
        assert "primero" in txt
        assert "segundo" not in txt

    def test_idempotent_stop(self, isolated_app_dir):
        w = TranscriptWriter(provider_name="azure", target_languages=["es"])
        w.start()
        w.stop()
        # Second stop should not raise
        w.stop()
