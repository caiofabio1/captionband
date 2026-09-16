"""Tests for the OpenAI Realtime Translate provider.

Covers the two parts that can be verified without a live API key and that
fail SILENTLY if wrong:

- resampling 16 kHz → 24 kHz. Sending 16 kHz samples to an endpoint that
  expects 24 kHz raises no error; it just makes the speaker sound fast and
  quietly wrecks recognition.
- assembling utterances from `.delta` events. The API documents no
  end-of-turn event, so the boundary logic lives in this codebase and nothing
  upstream will catch a regression in it.
"""
from __future__ import annotations

import numpy as np
import pytest

from providers.base import ProviderCapabilities
from providers.openai_realtime import (
    TARGET_SAMPLERATE,
    OpenAIRealtimeProvider,
    _resample_to_24k,
    _TranslationSession,
)


class TestResampling:
    def test_16k_to_24k_changes_length_by_3_over_2(self):
        # 1 second of 16 kHz mono silence.
        pcm = np.zeros(16_000, dtype=np.int16).tobytes()
        out = _resample_to_24k(pcm, 16_000)
        n_out = len(out) // 2
        # 3:2 ratio; allow a couple of samples of filter edge effect.
        assert abs(n_out - 24_000) <= 4, n_out

    def test_same_rate_is_passthrough(self):
        pcm = np.arange(100, dtype=np.int16).tobytes()
        assert _resample_to_24k(pcm, TARGET_SAMPLERATE) is pcm

    def test_empty_input_does_not_crash(self):
        assert _resample_to_24k(b"", 16_000) == b""

    def test_preserves_tone_frequency(self):
        """A 1 kHz tone must still be 1 kHz after resampling.

        Length alone would pass even if the samples were garbage; this is the
        assertion that actually catches a broken resampler.
        """
        sr = 16_000
        t = np.arange(sr, dtype=np.float64) / sr
        tone = (np.sin(2 * np.pi * 1000 * t) * 16000).astype(np.int16)

        out = np.frombuffer(_resample_to_24k(tone.tobytes(), sr), dtype=np.int16)
        spectrum = np.abs(np.fft.rfft(out.astype(np.float64)))
        peak_hz = np.fft.rfftfreq(out.size, 1.0 / TARGET_SAMPLERATE)[
            int(np.argmax(spectrum))
        ]
        assert abs(peak_hz - 1000) < 15, peak_hz

    def test_output_is_int16_range(self):
        loud = (np.ones(16_000, dtype=np.int16) * 32767)
        out = np.frombuffer(_resample_to_24k(loud.tobytes(), 16_000), dtype=np.int16)
        assert out.max() <= 32767 and out.min() >= -32768

    def test_numpy_fallback_path_also_preserves_the_tone(self, monkeypatch):
        """scipy is NOT in requirements.txt, so a clean install runs the
        fallback branch. Testing only the scipy path would validate a code
        path most users never execute."""
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("scipy"):
                raise ImportError("scipy unavailable (simulated clean install)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)

        sr = 16_000
        t = np.arange(sr, dtype=np.float64) / sr
        tone = (np.sin(2 * np.pi * 1000 * t) * 16000).astype(np.int16)
        out = np.frombuffer(_resample_to_24k(tone.tobytes(), sr), dtype=np.int16)

        assert abs(out.size - 24_000) <= 4, out.size
        spectrum = np.abs(np.fft.rfft(out.astype(np.float64)))
        peak_hz = np.fft.rfftfreq(out.size, 1.0 / TARGET_SAMPLERATE)[
            int(np.argmax(spectrum))
        ]
        assert abs(peak_hz - 1000) < 15, peak_hz


class TestUtteranceAssembly:
    def _session(self):
        got = []
        errs = []
        s = _TranslationSession(
            api_key="k",
            target_language="es",
            on_text=lambda *a: got.append(a),
            on_error=errs.append,
        )
        return s, got, errs

    def test_deltas_accumulate_into_one_utterance(self):
        s, got, _ = self._session()
        s._handle_event({"type": "session.input_transcript.delta", "delta": "Bom "})
        s._handle_event({"type": "session.input_transcript.delta", "delta": "dia"})
        s._handle_event({"type": "session.output_transcript.delta", "delta": "Buenos "})
        s._handle_event({"type": "session.output_transcript.delta", "delta": "días"})

        target, src, out, final, rid = got[-1]
        assert target == "es"
        assert src == "Bom dia"
        assert out == "Buenos días"
        assert final is False        # still streaming
        # Every delta of one utterance shares a result_id so the overlay
        # REPLACES the line instead of stacking one line per delta.
        assert len({g[4] for g in got}) == 1

    def test_flush_marks_final_and_starts_a_new_utterance(self):
        s, got, _ = self._session()
        s._handle_event({"type": "session.output_transcript.delta", "delta": "uno"})
        first_rid = got[-1][4]
        s._flush_utterance()
        assert got[-1][3] is True, "flush deve emitir final"

        s._handle_event({"type": "session.output_transcript.delta", "delta": "dos"})
        assert got[-1][4] != first_rid, "nova frase precisa de result_id novo"
        assert got[-1][2] == "dos", "buffer da frase anterior vazou"

    def test_flush_with_nothing_buffered_emits_nothing(self):
        s, got, _ = self._session()
        s._flush_utterance()
        assert got == []

    def test_translated_audio_is_ignored(self):
        # We render captions, not sound; audio deltas must not become text.
        s, got, _ = self._session()
        s._handle_event({"type": "session.output_audio.delta", "delta": "AAAA"})
        assert got == []

    def test_error_event_is_reported_not_swallowed(self):
        s, _, errs = self._session()
        s._handle_event({"type": "error", "error": {"message": "invalid_api_key"}})
        assert errs, "evento de erro da API precisa chegar ao provider"

    def test_unknown_event_does_not_crash(self):
        s, got, errs = self._session()
        s._handle_event({"type": "some.future.event", "foo": 1})
        assert got == [] and errs == []


class TestProvider:
    def test_declares_ordered_by_protocol(self):
        caps = OpenAIRealtimeProvider.capabilities()
        assert isinstance(caps, ProviderCapabilities)
        # One socket per target, each an ordered stream: the controller must
        # NOT add its reorder gate (that would be pure added latency).
        assert caps.ordered_by_protocol is True
        assert caps.streaming is True
        assert caps.translates is True

    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="api_key"):
            OpenAIRealtimeProvider(
                api_key="", target_languages=["es"], on_event=lambda e: None
            )

    def test_requires_a_target_language(self):
        with pytest.raises(ValueError, match="target language"):
            OpenAIRealtimeProvider(
                api_key="k", target_languages=[], on_event=lambda e: None
            )

    def test_one_session_per_target_and_locale_is_stripped(self):
        p = OpenAIRealtimeProvider(
            api_key="k",
            target_languages=["en", "es-ES"],
            on_event=lambda e: None,
        )
        # The API takes a bare language code for the output language.
        assert p.target_languages == ["en", "es"]

    def test_event_carries_no_seq(self):
        """seq must stay None or the controller would gate an ordered stream."""
        seen = []
        p = OpenAIRealtimeProvider(
            api_key="k", target_languages=["es"], on_event=seen.append
        )
        p._on_session_text("es", "bom dia", "buenos días", True, "es:0")
        assert seen[0].seq is None
        assert seen[0].translations == {"es": "buenos días"}
        assert seen[0].result_id == "es:0"
