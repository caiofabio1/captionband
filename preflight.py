"""Pre-event check — everything that must be true before the room fills up.

The settings window already tests individual API credentials. That is not the
same question. The operator's question 10 minutes before an event is "will
captions appear when someone speaks?", and that fails for reasons a
credential test never touches: the wrong output device selected, the audio
device delivering nothing, a target language equal to the source, or a
provider package that will not import on this machine.

So this walks the whole chain in the order it breaks, and stops at the first
failure with a sentence that says what to do.

Used by the tray menu ("Checagem pré-evento") and runnable on its own:

    python preflight.py
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional


log = logging.getLogger(__name__)

# How long to listen for real audio before concluding nothing is coming.
# Long enough for the operator to start a video, short enough to not feel
# broken.
AUDIO_PROBE_S = 4.0

# Below this RMS we treat the signal as digital silence rather than sound.
# Matches the VAD threshold used by the chunk buffer.
SILENCE_RMS = 0.004


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    fatal: bool = False   # True ⇒ captions definitely will not appear


def _fmt(step: Step) -> str:
    mark = "OK  " if step.ok else ("FALHA" if step.fatal else "AVISO")
    return "[{}] {}: {}".format(mark, step.name, step.detail)


# ---------------------------------------------------------------- steps


def check_provider_importable(cfg) -> Step:
    from providers import provider_capabilities, PROVIDER_LABELS

    name = cfg.provider
    label = PROVIDER_LABELS.get(name, name)
    caps = provider_capabilities(name)
    if caps is None:
        return Step(
            "Provedor", False,
            "'{}' não carrega neste Python (pacote ausente ou incompatível). "
            "Abra Configurações e escolha outro, ou reinstale o pacote.".format(label),
            fatal=True,
        )
    mode = "streaming (ordem garantida pelo protocolo)" if caps.ordered_by_protocol \
        else "em blocos (legenda reordenada pelo app)"
    return Step("Provedor", True, "{} — {}".format(label, mode))


def check_credentials(cfg) -> Step:
    """Cheapest call each provider supports, reusing connection_test."""
    import connection_test as ct

    name = cfg.provider
    try:
        if name == "azure":
            ok, msg = ct.test_azure(cfg.azure_speech_key, cfg.azure_speech_region)
        elif name == "groq":
            ok, msg = ct.test_groq(cfg.groq_api_key)
        elif name == "google":
            ok, msg = ct.test_google(
                cfg.google_credentials_json, cfg.google_project_id, cfg.google_location
            )
        elif name == "cerebras":
            ok, msg = ct.test_cerebras(cfg.cerebras_api_key)
        elif name in ("openai_cerebras", "openai_realtime"):
            # Same OpenAI credential. The realtime translate endpoint has no
            # cheap probe of its own, so we validate the key against the
            # models endpoint — a valid key there is a necessary condition,
            # not a sufficient one (the account still needs realtime access).
            ok, msg = ct.test_openai_whisper(cfg.openai_api_key)
        elif name == "openrouter":
            ok, msg = ct.test_openrouter(cfg.openrouter_api_key)
        elif name == "whisper_local":
            ok, msg = ct.test_whisper_local(
                cfg.whisper_model, cfg.whisper_device, cfg.whisper_compute_type
            )
        else:
            return Step("Credenciais", False,
                        "Provedor desconhecido: {}".format(name), fatal=True)
    except Exception as exc:
        return Step("Credenciais", False,
                    "Teste falhou: {}".format(exc), fatal=True)
    return Step("Credenciais", ok, msg, fatal=not ok)


def check_languages(cfg) -> Step:
    targets = list(cfg.target_languages or [])
    if not targets:
        return Step("Idiomas", False,
                    "Nenhum idioma-alvo configurado — não há o que traduzir.",
                    fatal=True)
    sources = list(cfg.source_languages or [])
    # Same-language pairs are not an error (the provider passes text through),
    # but they are almost always a misconfiguration before an event.
    overlap = [
        t for t in targets
        for s in sources
        if s.split("-")[0].lower() == t.split("-")[0].lower()
    ]
    if overlap and len(targets) == 1:
        return Step("Idiomas", False,
                    "O idioma-alvo ({}) é o mesmo da fala. A legenda vai sair "
                    "no idioma original.".format(targets[0]))
    return Step("Idiomas", True,
                "{} → {}".format(", ".join(sources) or "auto", ", ".join(targets)))


def check_audio(cfg, on_progress: Optional[Callable[[str], None]] = None) -> Step:
    """Open the real capture path and listen for actual sound.

    This is the check the credential tests cannot replace: the most common
    live failure is the app capturing the wrong output device, which looks
    exactly like nobody speaking.
    """
    import numpy as np
    from audio_capture import AudioCapture, find_device

    device = find_device(cfg.audio.device_name)
    peak = {"rms": 0.0, "blocks": 0}

    def on_audio(pcm: bytes) -> None:
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if arr.size:
            peak["rms"] = max(peak["rms"], float(np.sqrt(np.mean(arr * arr))))
            peak["blocks"] += 1

    died: list[str] = []
    cap = AudioCapture(
        on_audio=on_audio,
        device_index=device,
        samplerate=cfg.audio.samplerate,
        channels=cfg.audio.channels,
        on_died=died.append,
    )
    try:
        cap.start()
    except Exception as exc:
        return Step("Áudio", False, "Não foi possível abrir a captura: {}".format(exc),
                    fatal=True)

    if on_progress:
        on_progress("ouvindo por {:.0f}s — toque um vídeo ou fale no Teams"
                    .format(AUDIO_PROBE_S))
    deadline = time.monotonic() + AUDIO_PROBE_S
    while time.monotonic() < deadline:
        if died or peak["rms"] > SILENCE_RMS:
            break
        time.sleep(0.25)
    cap.stop()

    if died:
        return Step("Áudio", False, died[0], fatal=True)
    name = getattr(device, "name", None) or "dispositivo padrão"
    if peak["blocks"] == 0:
        return Step("Áudio", False,
                    "Nenhum bloco de áudio chegou de '{}'. Confira o "
                    "dispositivo de saída em Configurações.".format(name),
                    fatal=True)
    if peak["rms"] <= SILENCE_RMS:
        # Not fatal: capture works, the machine was simply silent. But the
        # operator must be told, because this is indistinguishable from the
        # failure case until someone speaks.
        return Step("Áudio", False,
                    "Captura funcionando em '{}', mas só silêncio digital. "
                    "Toque um som e repita a checagem.".format(name))
    return Step("Áudio", True,
                "Som detectado em '{}' (nível {:.3f}).".format(name, peak["rms"]))


# ---------------------------------------------------------------- runner


def check_fallback(cfg) -> Step:
    """Is there somewhere to go if the primary provider dies mid-event?

    Never fatal — captions will appear — but the whole fallback chain was
    rebuilt this month and it is worth nothing if this list is empty, which
    it silently was: saving Settings used to reset it.
    """
    from dataclasses import replace
    listed = [p for p in (cfg.fallback_providers or []) if p != cfg.provider]
    usable = [p for p in listed if replace(cfg, provider=p).is_valid()]
    if usable:
        return Step("Reserva", True, "Provedor de reserva: {}.".format(", ".join(usable)))
    if listed:
        return Step(
            "Reserva", False,
            "Provedor de reserva ({}) sem credencial válida — não vai "
            "funcionar na hora em que for preciso.".format(", ".join(listed)),
        )
    return Step(
        "Reserva", False,
        "Nenhum provedor de reserva: se o principal cair e não reconectar, a "
        "legenda para até alguém agir. Configurações → Provedor → reserva.",
    )


def run(cfg, on_progress: Optional[Callable[[str], None]] = None) -> list[Step]:
    """Run every check in break-order. Never raises."""
    steps: list[Step] = []

    def note(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    note("Verificando o provedor…")
    steps.append(check_provider_importable(cfg))
    if steps[-1].fatal:
        return steps

    note("Verificando idiomas…")
    steps.append(check_languages(cfg))

    note("Testando credenciais…")
    steps.append(check_credentials(cfg))
    steps.append(check_fallback(cfg))

    note("Testando a captura de áudio…")
    try:
        steps.append(check_audio(cfg, on_progress=note))
    except Exception as exc:
        log.exception("audio preflight crashed")
        steps.append(Step("Áudio", False, "Checagem falhou: {}".format(exc), fatal=True))

    return steps


def summarize(steps: list[Step]) -> tuple[bool, str]:
    """(ready, operator-facing report)."""
    lines = [_fmt(s) for s in steps]
    blocking = [s for s in steps if s.fatal]
    warnings = [s for s in steps if not s.ok and not s.fatal]

    if blocking:
        verdict = "NÃO ESTÁ PRONTO — resolva: {}".format(
            "; ".join(s.name for s in blocking))
    elif warnings:
        verdict = "Pronto com ressalva: {}".format(
            "; ".join(s.name for s in warnings))
    else:
        verdict = "PRONTO para o evento."
    return (not blocking), "\n".join(lines) + "\n\n" + verdict


def main() -> int:
    # The Windows console defaults to cp1252, which cannot encode the
    # characters this report uses (arrows, accented names of devices). Without
    # this the tool crashes on its own output — and it did, but only once the
    # configuration became VALID, because the failure branch happened to be
    # pure ASCII. A diagnostic tool that dies when things are working is worse
    # than useless.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(level=logging.WARNING)
    from config import load_config

    print("Checagem pré-evento — CaptionBand\n")
    steps = run(load_config(), on_progress=lambda m: print("  ...", m))
    ready, report = summarize(steps)
    print("\n" + report)
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
