"""Tests for the chunked audio buffer.

Validates the live-captioning best-practices logic without requiring a real
audio device or LLM provider.
"""
from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from providers._audio_buffer import ChunkedAudioBuffer, pcm16_to_wav_bytes


def gen_pcm(seconds: float, samplerate: int = 16000, amplitude: float = 0.0) -> bytes:
    """Generate `seconds` of mono int16 PCM at given amplitude (0.0 = silence)."""
    n = int(seconds * samplerate)
    if amplitude == 0.0:
        return (np.zeros(n, dtype=np.int16)).tobytes()
    # Sine wave to keep RMS approximately equal to amplitude
    t = np.arange(n) / samplerate
    samples = (np.sin(2 * np.pi * 440 * t) * amplitude * 32767).astype(np.int16)
    return samples.tobytes()


class TestChunkedAudioBuffer:
    def test_silence_does_not_emit(self):
        chunks = []
        # Slightly higher threshold to ensure pure-zero PCM stays below it
        buf = ChunkedAudioBuffer(
            on_chunk=chunks.append,
            samplerate=16000,
            speech_rms_threshold=0.005,
        )
        buf.push(gen_pcm(2.0, amplitude=0.0))
        assert chunks == []

    def test_speech_then_silence_emits_once(self):
        chunks = []
        buf = ChunkedAudioBuffer(on_chunk=chunks.append, samplerate=16000)
        buf.push(gen_pcm(1.5, amplitude=0.3))   # speech
        buf.push(gen_pcm(0.8, amplitude=0.0))   # silence > hangover
        assert len(chunks) == 1
        # Each chunk includes pre-roll (200ms) + speech (1.5s) + part of silence
        # Should be at least 1.5s of audio
        chunk_seconds = len(chunks[0]) / 2 / 16000
        assert chunk_seconds >= 1.4

    def test_long_speech_force_flushes_at_max_chunk_s(self):
        chunks = []
        buf = ChunkedAudioBuffer(on_chunk=chunks.append, samplerate=16000, max_chunk_s=3.0)
        # Continuous 8s speech with no silence
        buf.push(gen_pcm(8.0, amplitude=0.3))
        # We should have at least 2 chunks (since 8s > 3s and we cap)
        assert len(chunks) >= 2
        # No chunk should be much larger than max_chunk_s + preroll
        for chunk in chunks:
            chunk_seconds = len(chunk) / 2 / 16000
            assert chunk_seconds <= 3.5  # 3s cap + small overshoot for frame alignment

    def test_short_speech_below_min_is_dropped(self):
        chunks = []
        # smoothing_window=1 disables RMS averaging so we test the raw threshold
        buf = ChunkedAudioBuffer(
            on_chunk=chunks.append,
            samplerate=16000,
            min_speech_ms=400,
            smoothing_window=1,
        )
        # 200ms of speech then silence — below min_speech (400ms)
        buf.push(gen_pcm(0.2, amplitude=0.3))
        buf.push(gen_pcm(0.8, amplitude=0.0))
        assert chunks == []

    def test_flush_emits_buffered_speech(self):
        chunks = []
        buf = ChunkedAudioBuffer(on_chunk=chunks.append, samplerate=16000)
        buf.push(gen_pcm(1.0, amplitude=0.3))
        buf.flush()
        assert len(chunks) == 1


class TestWavWrapping:
    def test_pcm16_to_wav_round_trip(self):
        pcm = gen_pcm(0.5, amplitude=0.5)
        wav = pcm16_to_wav_bytes(pcm, samplerate=16000)
        # WAV header is 44 bytes; total = 44 + len(pcm)
        assert wav.startswith(b"RIFF")
        assert b"WAVE" in wav
        assert b"fmt " in wav
        assert b"data" in wav
        # Sample rate field at offset 24-27 (little endian)
        sr = struct.unpack_from("<I", wav, 24)[0]
        assert sr == 16000
