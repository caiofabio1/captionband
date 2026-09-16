"""Regressions for the 2026-09-16 code-review findings (phase 2) — provider
robustness.

Covered here, one test class per finding:

* Google assigned no sequence to its finals even though the translation
  fan-out emits in COMPLETION order, not utterance order — a short utterance
  B submitted after A reached the overlay first and the band read backwards;
* Google overwrote the process-wide GOOGLE_APPLICATION_CREDENTIALS and left
  it pointing at a temp file it deleted on stop, breaking every other Google
  client in the process;
* OpenRouter STT failures returned None with only a log line — a dead key
  presented as a "quiet room" behind a green tray icon;
* Groq/Whisper-local had no backpressure cap: when a chunk took longer than
  the speech that produced it, the executor's unbounded queue became a
  latency accumulator (captions arriving tens of seconds late, hundreds of
  MB of PCM pinned);
* the multi-target translation fan-out built a FRESH ThreadPoolExecutor per
  utterance — thread-spawn cost on every sentence of the event;
* OpenAI Realtime resampled every 50 ms block in isolation, redesigning the
  FIR per block and putting an edge transient (click) on every boundary;
* OpenAI Realtime stamped audio_emitted_at_ms at RESULT time, so the
  overlay's end-to-end latency read ~0 for this provider;
* usage_tracker had no rate for openai_realtime — the dashboard showed $0
  for the most expensive provider in the app;
* Azure push_audio read _push_stream unsynchronized while stop()/reconnect
  swap and close it — a write into a closed stream mid-race.

Style follows test_phase1_fixes.py: real classes, fakes only at the edges,
no real SDK calls.
"""
from __future__ import annotations

import os
import re
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

import usage_tracker
from ordering import ReorderGate
from providers.base import (
    CODE_AUTH,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_FATAL,
)
from providers.google import GoogleProvider
from providers.groq import GroqProvider
from providers.openai_realtime import (
    OpenAIRealtimeProvider,
    _resample_to_24k,
    _StreamResampler,
)
from providers.openrouter import OpenRouterProvider
from providers.whisper_local import WhisperLocalProvider

from .conftest import requires_azure

REPO = Path(__file__).resolve().parent.parent


def _strip_comments(src: str) -> str:
    """Static guards must match CODE, not the comment explaining the code."""
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


# ---------------------------------------------------------------------- fakes


class _FakeGoogleTranslateClient:
    """translate_text that finishes utterance "B" before utterance "A"."""

    def translate_text(self, parent, contents, target_language_code,
                       source_language_code, mime_type):
        if contents[0] == "A":
            time.sleep(0.25)        # the first utterance is the slow one
        return types.SimpleNamespace(translations=[
            types.SimpleNamespace(translated_text=f"tx:{contents[0]}")
        ])


class _FakeChatCompletions:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc

    def create(self, model, messages, temperature, max_tokens):
        if self.exc is not None:
            raise self.exc
        text = messages[-1]["content"]
        return types.SimpleNamespace(choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=f"tx:{text}"))
        ])


class _FakeChatClient:
    def __init__(self, exc: Exception | None = None):
        self.chat = types.SimpleNamespace(completions=_FakeChatCompletions(exc))


def _google(on_event, targets=("en",)) -> GoogleProvider:
    return GoogleProvider(
        credentials_json='{"type": "service_account"}',
        project_id="p",
        location="global",
        recognizer_id="_",
        source_languages=["pt-BR"],
        target_languages=list(targets),
        on_event=on_event,
    )


def _groq(on_event, targets=("en",)) -> GroqProvider:
    return GroqProvider(
        api_key="k",
        transcription_model="m",
        translation_model="m",
        source_languages=["pt"],
        target_languages=list(targets),
        on_event=on_event,
    )


# ---------------------------------------------------- google: seq + ordering


class TestGoogleSequencing:
    def test_finals_declare_the_gate(self):
        """The translation fan-out emits in completion order, so the class
        MUST ask the controller for the reorder gate. Declaring
        ordered_by_protocol=True here is how the band reads backwards."""
        assert GoogleProvider.CAPABILITIES.ordered_by_protocol is False

    def test_out_of_order_translations_come_out_in_seq_order(self):
        """Utterance A is slow, B overtakes it in the pool; the gate must
        still publish A before B."""
        released: list = []
        gate = ReorderGate(released.append)
        arrival_order: list[int] = []

        def on_event(ev):
            arrival_order.append(ev.seq)
            gate.submit(ev.seq, ev)

        p = _google(on_event)
        p._translation_pool = ThreadPoolExecutor(max_workers=2)
        p._translate_client = _FakeGoogleTranslateClient()
        try:
            slow = p._translation_pool.submit(p._translate_and_emit, 0, "A", "pt")
            fast = p._translation_pool.submit(p._translate_and_emit, 1, "B", "pt")
            slow.result(timeout=5)
            fast.result(timeout=5)
        finally:
            p._translation_pool.shutdown(wait=True)
        gate.flush()

        assert arrival_order == [1, 0], (
            "test setup broken: B was supposed to finish before A")
        assert [ev.seq for ev in released] == [0, 1]
        assert released[0].translations.get("en") == "tx:A"
        assert all(ev.is_final and ev.seq is not None for ev in released)

    def test_a_dead_translation_still_releases_its_slot(self):
        """If the fan-out raises, the gate must not hold every later caption
        hostage to a seq that will never arrive."""
        events: list = []
        p = _google(events.append)
        p._translate_client = None     # _translate_all returns {}; force a crash:
        p._translation_pool = None

        def boom(text, lang):
            raise RuntimeError("translate worker exploded")

        p._translate_all = boom
        p._translate_and_emit(7, "hello", "pt")

        assert len(events) == 1
        assert events[0].seq == 7
        assert events[0].is_final
        assert events[0].original_text == ""


# ------------------------------------------- google: env var save / restore


class TestGoogleCredentialsEnv:
    def test_stop_restores_a_preexisting_value(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", r"C:\orig.json")
        p = _google(lambda ev: None)
        p._setup_credentials()
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] != r"C:\orig.json"

        p._running = True              # start() minus the SDK clients
        p.stop()

        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == r"C:\orig.json", (
            "the process-wide credentials of the OPERATOR were left overwritten")

    def test_stop_unsets_when_there_was_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        p = _google(lambda ev: None)
        p._setup_credentials()
        assert "GOOGLE_APPLICATION_CREDENTIALS" in os.environ

        p._running = True
        p.stop()

        assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ, (
            "stop() leaked a variable pointing at a temp file it deleted")
        assert p._creds_temp_path is None


# ------------------------------------------------------- openrouter: status


class TestOpenRouterSttStatus:
    def _provider(self, statuses: list) -> OpenRouterProvider:
        p = OpenRouterProvider(
            openrouter_api_key="k",
            openrouter_stt_model="openai/whisper-1",
            openrouter_translation_model="m",
            source_languages=["pt"],
            target_languages=["en"],
            on_event=lambda ev: None,
        )
        p.on_status = statuses.append
        return p

    def test_401_on_the_audio_endpoint_is_fatal_auth(self, monkeypatch):
        statuses: list = []
        p = self._provider(statuses)
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **k: types.SimpleNamespace(status_code=401, text="bad key"),
        )
        assert p._stt_via_audio_endpoint(b"wav") is None
        assert any(s.kind == STATUS_FATAL and s.code == CODE_AUTH
                   for s in statuses), (
            "a dead key must reach the operator, not just the log")

    def test_500_on_the_audio_endpoint_is_reported_not_silent(self, monkeypatch):
        statuses: list = []
        p = self._provider(statuses)
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **k: types.SimpleNamespace(status_code=500, text="boom"),
        )
        assert p._stt_via_audio_endpoint(b"wav") is None
        assert any(s.kind == STATUS_FAILING for s in statuses)

    def test_chat_audio_failure_is_reported_not_silent(self):
        statuses: list = []
        p = self._provider(statuses)
        p._stt_client = _FakeChatClient(exc=RuntimeError("500 server error"))
        assert p._stt_via_chat_audio(b"wav") is None
        assert statuses, "the STT failure never reached on_status"
        assert all(s.kind in (STATUS_FAILING, STATUS_FATAL) for s in statuses)


# ------------------------------------------------------------- backpressure


class TestBackpressure:
    def _check_drop_oldest(self, p, monkeypatch):
        events: list = []
        statuses: list = []
        p.on_event = events.append
        p.on_status = statuses.append
        p._executor = ThreadPoolExecutor(max_workers=1)
        p._sequence = 0

        def slow_process(seq, pcm, emitted_at_ms):
            time.sleep(0.3)

        monkeypatch.setattr(p, "_process_chunk", slow_process)
        try:
            for _ in range(p.MAX_PENDING_CHUNKS + 3):
                p._on_chunk(b"\x00\x00" * 50)

            with p._pending_lock:
                pending = len(p._pending)
            assert pending <= p.MAX_PENDING_CHUNKS, (
                f"backlog grew to {pending} — the cap did not hold")
            assert any(ev.is_final and not ev.original_text for ev in events), (
                "a dropped chunk did not release its reorder-gate slot — "
                "every later caption would stall on it")
            degraded = [s for s in statuses if s.kind == STATUS_DEGRADED]
            assert degraded, "the operator was not told chunks are being dropped"

            # The warning is throttled: an immediate second report is a no-op.
            p._report_backpressure()
            assert len([s for s in statuses if s.kind == STATUS_DEGRADED]) == len(degraded)
        finally:
            p._executor.shutdown(wait=False, cancel_futures=True)

    def test_groq_drops_oldest_and_releases_the_slot(self, monkeypatch):
        self._check_drop_oldest(_groq(lambda ev: None), monkeypatch)

    def test_whisper_local_drops_oldest_and_releases_the_slot(self, monkeypatch):
        p = WhisperLocalProvider(
            whisper_model="small",
            whisper_device="cpu",
            whisper_compute_type="int8",
            source_languages=["pt"],
            target_languages=["en"],
            on_event=lambda ev: None,
        )
        self._check_drop_oldest(p, monkeypatch)


# ------------------------------------------- persistent translation pools


class TestPersistentTranslationPool:
    def test_groq_fanout_reuses_one_pool_across_utterances(self):
        p = _groq(lambda ev: None, targets=("en", "es"))
        pool = ThreadPoolExecutor(max_workers=2)
        p._translation_pool = pool
        p._translation_client = _FakeChatClient()
        try:
            r1 = p._translate_all("ola", "pt")
            r2 = p._translate_all("mundo", "pt")
            assert r1 == {"en": "tx:ola", "es": "tx:ola"}
            assert r2 == {"en": "tx:mundo", "es": "tx:mundo"}
            assert p._translation_pool is pool, (
                "the pool was rebuilt per utterance — thread-spawn cost on "
                "every sentence of the event")
        finally:
            pool.shutdown(wait=True)

    def test_no_per_utterance_executor_in_the_fanout(self):
        """Static guard against reintroducing `with ThreadPoolExecutor(...)`
        inside a per-utterance path."""
        for name in ("groq.py", "google.py"):
            src = _strip_comments(
                (REPO / "providers" / name).read_text(encoding="utf-8"))
            assert "with ThreadPoolExecutor" not in src, (
                f"{name} builds a throwaway executor per call again")


# ------------------------------------------------- openai realtime resampler


class TestStreamResampler:
    def test_block_stream_matches_one_shot(self):
        """The whole point of the carry buffer: streaming 50 ms blocks through
        the stateful resampler must produce (nearly) the same signal as a
        one-shot resample of the whole second — no per-block FIR redesign."""
        rate = 16_000
        t = np.arange(rate) / rate
        sig = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)

        rs = _StreamResampler(rate)
        block = 800                                  # 50 ms at 16 kHz
        streamed = np.frombuffer(b"".join(
            rs.process(sig[i:i + block].tobytes())
            for i in range(0, sig.size, block)
        ), dtype=np.int16)
        ref = np.frombuffer(_resample_to_24k(sig.tobytes(), rate), dtype=np.int16)

        assert abs(streamed.size - ref.size) <= 4, (
            f"streamed {streamed.size} vs one-shot {ref.size} samples")
        n = min(streamed.size, ref.size)
        diff = np.abs(streamed[:n].astype(np.int32) - ref[:n].astype(np.int32))
        assert diff.max() <= 600, (
            f"max divergence {diff.max()} — the block boundaries are audible")

    def test_no_click_at_block_boundaries(self):
        """A per-block resampler put an edge transient on every 50 ms seam.
        The seam must be no rougher than the reference signal is there."""
        rate = 16_000
        t = np.arange(rate) / rate
        sig = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)

        rs = _StreamResampler(rate)
        block = 800
        streamed = np.frombuffer(b"".join(
            rs.process(sig[i:i + block].tobytes())
            for i in range(0, sig.size, block)
        ), dtype=np.int16).astype(np.int32)
        ref = np.frombuffer(_resample_to_24k(sig.tobytes(), rate),
                            dtype=np.int16).astype(np.int32)

        out_per_block = 1200                         # 800 in at 3:2
        for seam in range(out_per_block, streamed.size - 4, out_per_block):
            local = np.abs(np.diff(streamed[seam - 4:seam + 4])).max()
            ref_local = np.abs(np.diff(ref[seam - 4:seam + 4])).max()
            assert local <= ref_local + 400, (
                f"discontinuity at output sample {seam}: {local} vs {ref_local}")

    def test_passthrough_at_target_rate(self):
        pcm = b"\x01\x02" * 100
        assert _StreamResampler(24_000).process(pcm) is pcm


# --------------------------------------------- openai realtime: timestamps


class TestRealtimeAudioTimestamp:
    def test_event_carries_the_audio_arrival_stamp_not_result_time(self):
        events: list = []
        p = OpenAIRealtimeProvider(
            api_key="k", target_languages=["es"], on_event=events.append)
        p._running = True
        p._sessions = []
        p._resampler = _StreamResampler(16_000)

        before = time.monotonic() * 1000
        p.push_audio(b"\x00\x00" * 320)
        after = time.monotonic() * 1000
        assert before <= p._last_audio_at_ms <= after, (
            "push_audio did not stamp the audio arrival time")

        p._last_audio_at_ms = 12345.0                # audio arrived 'long ago'
        p._on_session_text("es", "Hola", "Hello", True, "es:0")
        assert events[-1].audio_emitted_at_ms == 12345.0, (
            "the latency metric was stamped at result time — it read ~0 for "
            "a result that took seconds")


# ------------------------------------------------------------- usage tracker


class TestUsageTrackerRates:
    def test_openai_realtime_has_a_rate_and_no_free_tier(self, monkeypatch, tmp_path):
        monkeypatch.setattr(usage_tracker, "app_data_dir", lambda: tmp_path)
        usage_tracker.add_seconds("openai_realtime", 3600)
        s = usage_tracker.summary()
        assert "openai_realtime" in s, (
            "the most expensive provider in the app showed $0 on the dashboard")
        entry = s["openai_realtime"]
        assert entry["hours_month"] == pytest.approx(1.0, abs=0.01)
        assert entry["free_remaining_hours"] == 0.0
        assert entry["estimated_cost_usd"] == pytest.approx(2.04, abs=0.01)


# ---------------------------------------------------------------- azure lock


class TestAzurePushStreamLock:
    def test_push_audio_reads_the_stream_under_a_lock(self):
        """Static guard: the unsynchronized read could write into a stream
        stop()/reconnect had just closed."""
        src = _strip_comments(
            (REPO / "providers" / "azure.py").read_text(encoding="utf-8"))
        assert "self._stream_lock" in src
        m = re.search(
            r"def push_audio\(self.*?with self\._stream_lock:",
            src, re.S)
        assert m, "push_audio reads _push_stream without synchronization"

    @requires_azure
    def test_push_audio_delivers_and_survives_a_swap(self):
        from providers.azure import AzureProvider

        class _FakeStream:
            def __init__(self):
                self.writes: list[bytes] = []

            def write(self, b: bytes) -> None:
                self.writes.append(b)

            def close(self) -> None:
                pass

        p = AzureProvider(
            speech_key="k", region="r",
            source_languages=["pt-BR"], target_languages=["en"],
            on_event=lambda ev: None,
        )
        fake = _FakeStream()
        with p._stream_lock:
            p._push_stream = fake
        p.push_audio(b"\x00\x01")
        assert fake.writes == [b"\x00\x01"]

        # stop()/reconnect swap the reference out from under the capture thread.
        with p._stream_lock:
            p._push_stream = None
        p.push_audio(b"\x00\x01")                    # must not raise
        assert fake.writes == [b"\x00\x01"]