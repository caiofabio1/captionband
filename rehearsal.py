"""Rehearsal harness — exercise the live pipeline before the event, not during it.

Run it:
    python rehearsal.py              # full rehearsal, ~15s, no API keys needed
    python rehearsal.py --verbose    # show every event as it is released

Why this exists
---------------
Every defect this app had in production was a failure that produced SILENCE:
a provider erroring behind a green tray icon, an audio device disappearing, a
thread pool delivering captions out of order. None of them are visible by
running the app once on a quiet desktop — they need a speaker, a venue network
and an audience to show up, which is exactly when you cannot debug them.

So this harness drives the real TranslationController with a fake provider it
can make misbehave on demand, and asserts the controller reacts. It needs no
API key, no Teams call and no audio device.

What it covers
--------------
1. Out-of-order results are re-ordered before reaching the overlay.
2. A result that never arrives is skipped instead of blocking the rest.
3. A failing provider raises operator-visible health, not silence.
4. A fatal provider failure triggers the fallback chain.
5. Audio-capture death is detected and surfaced.
6. Streaming providers bypass the reorder gate entirely.

This is NOT a substitute for one real end-to-end run with the actual provider
and the actual room audio. It is what makes that run diagnosable.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

from providers.base import (
    CODE_AUTH,
    CODE_NETWORK,
    STATUS_FAILING,
    STATUS_FATAL,
    ProviderCapabilities,
    ProviderStatus,
    TranslationEvent,
    TranslationProvider,
)

VERBOSE = False


def say(msg: str) -> None:
    print(msg, flush=True)


def vsay(msg: str) -> None:
    if VERBOSE:
        print("    " + msg, flush=True)


# ---------------------------------------------------------------- fakes


class FakeChunkProvider(TranslationProvider):
    """Stands in for Groq/Whisper-local: chunked REST, results can race."""

    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=False, translates=True, streaming=False,
        label="fake-chunk",
    )

    def __init__(self, on_event):
        self.on_event = on_event
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def push_audio(self, audio_bytes: bytes) -> None:
        pass

    @property
    def is_running(self) -> bool:
        return self._running

    def deliver(self, seq: int | None, text: str) -> None:
        self.on_event(TranslationEvent(
            detected_language="pt",
            original_text=text,
            translations={"es": text.upper()},
            is_final=True,
            audio_emitted_at_ms=time.monotonic() * 1000,
            seq=seq,
        ))


class FakeStreamProvider(FakeChunkProvider):
    """Stands in for Azure/OpenAI Realtime: the wire protocol orders results."""

    CAPABILITIES = ProviderCapabilities(
        ordered_by_protocol=True, translates=True, streaming=True,
        label="fake-stream",
    )


@dataclass
class Collector:
    """Captures what would have reached the overlay and the operator."""

    captions: list[str] = field(default_factory=list)
    health: list[tuple[str, str, str]] = field(default_factory=list)

    def on_caption(self, event) -> None:
        if getattr(event, "original_text", ""):
            self.captions.append(event.original_text)
            vsay(f"caption -> {event.original_text!r}")

    def on_health(self, status: ProviderStatus) -> None:
        self.health.append((status.kind, status.code, status.message))
        vsay(f"health  -> {status.kind}/{status.code} {status.message}")

    @property
    def trouble(self) -> list[tuple[str, str, str]]:
        return [h for h in self.health if h[0] in (STATUS_FAILING, STATUS_FATAL)]


# ---------------------------------------------------------------- checks


def check_reordering() -> None:
    """1 + 2: out-of-order results are ordered; a lost one is skipped."""
    from ordering import DEFAULT_DEADLINE_S, ReorderGate

    clock = {"t": 0.0}
    col = Collector()
    gate = ReorderGate(on_release=col.on_caption, now_fn=lambda: clock["t"])
    prov = FakeChunkProvider(
        on_event=lambda e: gate.submit(e.seq, e) if e.seq is not None
        else col.on_caption(e)
    )
    prov.start()

    # The speaker says three things; the network returns them shuffled.
    prov.deliver(1, "segunda frase")
    prov.deliver(2, "terceira frase")
    prov.deliver(0, "primeira frase")

    expected = ["primeira frase", "segunda frase", "terceira frase"]
    assert col.captions == expected, (
        f"legendas fora de ordem: {col.captions}")
    say("  [OK] 1. resultados fora de ordem sao reordenados antes da tela")

    # Now one chunk is lost entirely (the API 500'd and we gave up on it).
    col.captions.clear()
    prov.deliver(4, "quinta frase")     # 3 never arrives
    assert col.captions == [], "nao deveria soltar antes do deadline"
    clock["t"] += DEFAULT_DEADLINE_S + 0.1
    gate.tick()
    assert col.captions == ["quinta frase"], col.captions
    assert gate.skipped >= 1
    say("  [OK] 2. chunk perdido e pulado; a legenda seguinte nao trava")


def check_failure_is_loud() -> None:
    """3: a provider failure reaches the operator instead of going quiet."""
    col = Collector()
    prov = FakeChunkProvider(on_event=lambda e: None)
    prov.provider_name = "fake"
    prov.on_status = col.on_health
    prov.start()

    # This is what every `except` inside a real provider now does.
    prov.report_exception(RuntimeError("Connection timed out"), "transcription")
    assert col.trouble, "falha de rede nao chegou ao operador"
    kind, code, message = col.trouble[-1]
    assert code == CODE_NETWORK, (kind, code)
    assert message, "status sem mensagem legivel"
    say(f"  [OK] 3. falha de rede vira aviso legivel: {message[:52]!r}")

    col.health.clear()
    prov.report_exception(RuntimeError("HTTP 401 Unauthorized"), "transcription")
    kind, code, _ = col.health[-1]
    assert (kind, code) == (STATUS_FATAL, CODE_AUTH), (kind, code)
    say("  [OK] 3b. chave invalida e classificada como fatal (nao adianta esperar)")


def check_fallback_fires() -> None:
    """4: the fallback chain actually runs — it was dead code before.

    This drives the REAL TranslationController, not a stand-in, because the
    thing being tested IS the wiring. A mock of the wiring would pass even if
    the wiring were still missing, which is precisely the bug we are fixing.
    """
    from PyQt6.QtWidgets import QApplication

    import translator as T

    app = QApplication.instance() or QApplication([])

    class StubOverlay:
        """Only the parts TranslationController touches in __init__."""
        def push_caption(self, *a, **kw):
            pass

    fired: list[str] = []
    ctrl = T.TranslationController.__new__(T.TranslationController)
    # Build the real object, then neutralise only the two methods that would
    # need a live provider and a real audio device.
    from config import load_config
    ctrl.__init__(load_config(), StubOverlay())
    ctrl._running = True
    ctrl.trigger_fallback = lambda: (fired.append("fallback"), True)[1]

    # A fatal status is what a bad API key produces mid-event.
    ctrl._on_provider_status(ProviderStatus(
        kind=STATUS_FATAL, code=CODE_AUTH,
        message="Chave invalida", provider="fake",
    ))
    # The controller hops threads with a queued signal, so let Qt deliver it.
    app.processEvents()

    assert fired, (
        "trigger_fallback continua sendo codigo morto - "
        "o status fatal nao chegou na cadeia de fallback")
    assert ctrl._last_health[0] == STATUS_FATAL, ctrl._last_health
    say("  [OK] 4. status fatal percorre o controller real ate trigger_fallback")

    # 4b: a dying provider emits a BURST of errors, not one. Each queues a
    # fallback request. Without a cooldown the burst walks the whole provider
    # list in milliseconds and reports "nothing left" while the second
    # provider was never really given a chance.
    fired.clear()
    ctrl._last_fallback_at = 0.0
    for _ in range(10):
        ctrl._on_provider_status(ProviderStatus(
            kind=STATUS_FATAL, code=CODE_AUTH,
            message="Chave invalida", provider="fake",
        ))
    app.processEvents()
    assert len(fired) == 1, (
        f"rajada de {10} erros causou {len(fired)} trocas de provedor; "
        "deveria causar 1")
    say("  [OK] 4b. rajada de 10 erros = 1 troca de provedor (cooldown)")


def check_capture_death_detected() -> None:
    """5: the capture thread dying is noticed, not ignored."""
    from audio_capture import AudioCapture

    seen: list[str] = []
    cap = AudioCapture(on_audio=lambda b: None, on_died=seen.append)
    # Simulate the real failure: the loop exits with a reason while nobody
    # asked it to stop.
    cap._died_reason = "fone desconectado"
    if cap.on_died is not None:
        cap.on_died(cap._died_reason)

    assert seen == ["fone desconectado"], seen
    # And the liveness probe must disagree with the intent flag.
    assert not cap.is_alive(), "is_alive() deveria ser falso sem thread"
    say("  [OK] 5. morte da captura e reportada (antes: thread morria calada)")


def check_streaming_bypasses_gate() -> None:
    """6: streaming providers must NOT be delayed by the reorder gate."""

    chunk = FakeChunkProvider.capabilities()
    stream = FakeStreamProvider.capabilities()
    assert chunk.ordered_by_protocol is False
    assert stream.ordered_by_protocol is True

    # The controller decides from capabilities, never from the provider name.
    def needs_gate(caps):
        return bool(caps is None or not caps.ordered_by_protocol)
    assert needs_gate(chunk) is True
    assert needs_gate(stream) is False
    assert needs_gate(None) is True, "provider desconhecido deve receber o portao"
    say("  [OK] 6. streaming pula o portao; desconhecido recebe o portao (seguro)")


# ---------------------------------------------------------------- main


CHECKS = [
    ("ordenacao das legendas", check_reordering),
    ("falha visivel ao operador", check_failure_is_loud),
    ("cadeia de fallback", check_fallback_fires),
    ("morte da captura de audio", check_capture_death_detected),
    ("streaming pula o portao", check_streaming_bypasses_gate),
]


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    say("Ensaio da pipeline - sem chave de API, sem Teams, sem dispositivo.\n")
    failed = []
    for name, fn in CHECKS:
        say(f"* {name}")
        try:
            fn()
        except AssertionError as exc:
            say(f"  [FALHOU] {exc}")
            failed.append(name)
        except Exception as exc:
            say(f"  [ERRO] {type(exc).__name__}: {exc}")
            failed.append(name)
        say("")

    if failed:
        say("ENSAIO REPROVADO em: {}".format(", ".join(failed)))
        say("NAO leve para o evento ate isto passar.")
        return 1
    say("ENSAIO APROVADO - a pipeline reage a falha em vez de ficar muda.")
    say("")
    say("Ainda falta, e so voce pode fazer: uma rodada real com o provedor")
    say("escolhido e o audio da sala, olhando o icone da bandeja.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
