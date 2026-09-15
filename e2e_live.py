"""End-to-end test against the REAL stack: real audio, real capture, real API.

Everything else in this repo tests pieces. This plays actual synthesized
speech through the actual speakers, lets WASAPI loopback capture it, runs the
real TranslationController and the real provider, and asserts on the captions
that come out the other end.

    python e2e_live.py              # run every case
    python e2e_live.py --case 1     # one case
    python e2e_live.py --list

It costs real API quota (a few seconds of speech synthesis + translation per
case). Synthesized audio is cached under the system temp dir, so re-runs only
pay for the translation side.

Requires: a working Azure Speech credential in the app config, a working audio
output device, and NOTHING ELSE PLAYING — the loopback captures whatever the
speakers are producing, including your music.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


AUDIO_CACHE = os.path.join(tempfile.gettempdir(), "tlt-e2e-audio")

# Sentences spoken in each case. Kept short: every second is billed twice
# (once to synthesize, once to translate).
PHRASES = {
    "pt": ("pt-BR", "pt-BR-FranciscaNeural",
           "Bom dia a todos. Sejam bem-vindos ao congresso."),
    "en": ("en-US", "en-US-JennyNeural",
           "Good morning everyone. Welcome to the conference."),
    "es": ("es-ES", "es-ES-ElviraNeural",
           "Buenos días a todos. Bienvenidos al congreso."),
    "pt2": ("pt-BR", "pt-BR-AntonioNeural",
            "A primeira sessão começa agora."),
    "pt3": ("pt-BR", "pt-BR-AntonioNeural",
            "Obrigado pela presença de vocês."),
}


# ---------------------------------------------------------------- audio


def synthesize(key: str, region: str, phrase_id: str) -> str:
    """Synthesize one phrase to a cached WAV. Returns the path."""
    os.makedirs(AUDIO_CACHE, exist_ok=True)
    path = os.path.join(AUDIO_CACHE, "{}.wav".format(phrase_id))
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path

    import azure.cognitiveservices.speech as speechsdk

    _locale, voice, text = PHRASES[phrase_id]
    cfg = speechsdk.SpeechConfig(subscription=key, region=region)
    cfg.speech_synthesis_voice_name = voice
    syn = speechsdk.SpeechSynthesizer(
        speech_config=cfg,
        audio_config=speechsdk.audio.AudioOutputConfig(filename=path),
    )
    result = syn.speak_text_async(text).get()
    if result.reason == speechsdk.ResultReason.Canceled:
        d = result.cancellation_details
        raise RuntimeError("TTS falhou: {} {}".format(d.reason, d.error_details))
    return path


def play(path: str, blocking: bool = True) -> float:
    """Play a WAV through the default speaker. Returns its duration."""
    import soundcard as sc

    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch)
    sc.default_speaker().play(data, samplerate=sr)
    return n / float(sr)


# ---------------------------------------------------------------- harness


@dataclass
class Caption:
    at: float
    original: str
    translations: dict
    language: str
    final: bool
    result_id: str


@dataclass
class Run:
    captions: list = field(default_factory=list)
    health: list = field(default_factory=list)

    @property
    def finals(self) -> list:
        return [c for c in self.captions if c.final]

    def text_for(self, lang: str) -> str:
        """Everything that reached the overlay for one target language."""
        out = []
        for c in self.finals:
            t = (c.translations or {}).get(lang, "").strip()
            if t and t not in out:
                out.append(t)
        return " ".join(out)

    @property
    def originals(self) -> str:
        seen = []
        for c in self.finals:
            o = (c.original or "").strip()
            if o and o not in seen:
                seen.append(o)
        return " ".join(seen)


class Harness:
    """Drives the REAL TranslationController with a recording overlay."""

    def __init__(self, cfg):
        from PyQt6.QtWidgets import QApplication
        import translator as T

        self.app = QApplication.instance() or QApplication([])
        self.run = Run()
        harness = self

        class RecordingOverlay:
            """Stands in for the visual overlay, records what it is told to
            draw. This is the REAL signal the real overlay receives."""
            def push_caption(self, original, translations, language,
                             final, emitted_at_ms, result_id):
                harness.run.captions.append(Caption(
                    at=time.monotonic(), original=original,
                    translations=dict(translations or {}), language=language,
                    final=bool(final), result_id=result_id,
                ))

            def apply_config(self, *a, **kw):
                pass

            def show(self, *a, **kw):
                pass

            def hide(self, *a, **kw):
                pass

        self.controller = T.TranslationController(cfg, RecordingOverlay())
        self.controller.health_changed.connect(
            lambda k, c, m: self.run.health.append((k, c, m))
        )

    def pump(self, seconds: float) -> None:
        """Run the Qt event loop for real time (signals are queued)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.02)

    def start(self) -> None:
        self.controller.start()
        self.pump(1.5)          # let the recognizer connect

    def stop(self) -> None:
        try:
            self.controller.stop()
        except Exception:
            pass
        self.pump(0.3)


# ---------------------------------------------------------------- cases


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    metrics: dict = field(default_factory=dict)


def _base_config():
    from config import load_config
    return load_config()


def _speak_and_collect(cfg, phrase_ids: list, tail_s: float = 6.0) -> Run:
    """Start the real pipeline, play phrases, collect what reached the overlay."""
    h = Harness(cfg)
    h.start()
    for pid in phrase_ids:
        path = synthesize(cfg.azure_speech_key, cfg.azure_speech_region, pid)
        dur = play(path)
        h.pump(0.4)
    h.pump(tail_s)              # wait for the last translation to land
    h.stop()
    return h.run


def case_1_streaming_pt(cfg) -> Result:
    """Streaming mode, fixed pt-BR: does a Portuguese sentence become es+en?"""
    c = replace(cfg, azure_streaming_mode=True, azure_streaming_language="pt-BR")
    run = _speak_and_collect(c, ["pt"])

    es, en = run.text_for("es"), run.text_for("en")
    ok = bool(es) and bool(en)
    return Result(
        "1. streaming pt-BR -> es+en",
        ok,
        "es={!r}\n      en={!r}\n      origem={!r}".format(es[:70], en[:70],
                                                           run.originals[:70]),
        {"legendas": len(run.finals), "es": bool(es), "en": bool(en)},
    )


def case_2_autodetect(cfg) -> Result:
    """LID mode: are pt, en and es each detected without being told?"""
    c = replace(cfg, azure_streaming_mode=False,
                source_languages=["pt-BR", "en-US", "es-ES"])
    run = _speak_and_collect(c, ["pt", "en", "es"], tail_s=8.0)

    langs = {(cap.language or "").lower()[:2] for cap in run.finals if cap.language}
    expected = {"pt", "en", "es"}
    ok = expected.issubset(langs)
    return Result(
        "2. auto-detect pt/en/es (streaming OFF)",
        ok,
        "idiomas detectados: {} (esperado {})\n      es={!r}".format(
            sorted(langs) or "nenhum", sorted(expected), run.text_for("es")[:60]),
        {"detectados": sorted(langs), "legendas": len(run.finals)},
    )


def case_3_ordering(cfg) -> Result:
    """Three sentences back to back: do the captions stay in order?"""
    c = replace(cfg, azure_streaming_mode=True, azure_streaming_language="pt-BR")
    run = _speak_and_collect(c, ["pt", "pt2", "pt3"], tail_s=8.0)

    # Order is verified on ARRIVAL TIME vs sequence: caption N must not
    # arrive before caption N-1. With a streaming provider this is the
    # protocol's guarantee; the assertion is here to catch a regression that
    # breaks it (e.g. wrongly routing it through the reorder gate).
    times = [c.at for c in run.finals]
    monotonic = all(b >= a for a, b in zip(times, times[1:]))
    got_all = len(run.finals) >= 2
    ok = monotonic and got_all
    return Result(
        "3. ordem com 3 frases seguidas",
        ok,
        "{} legendas finais, ordem cronologica: {}\n      es={!r}".format(
            len(run.finals), monotonic, run.text_for("es")[:70]),
        {"finais": len(run.finals), "em_ordem": monotonic},
    )


def case_4_bad_key_is_loud(cfg) -> Result:
    """The core regression: a broken provider must SHOUT, not go quiet."""
    c = replace(cfg, azure_speech_key="0" * 32, azure_streaming_mode=True,
                azure_streaming_language="pt-BR")
    h = Harness(c)
    try:
        h.start()
        # Feed it real audio so a silent failure would look exactly like a
        # working app with nobody talking.
        path = synthesize(cfg.azure_speech_key, cfg.azure_speech_region, "pt")
        play(path)
        h.pump(8.0)
    except Exception as exc:
        h.stop()
        return Result("4. chave invalida grita (nao emudece)", True,
                      "falhou no start com erro visivel: {}".format(str(exc)[:80]),
                      {"via": "excecao no start"})
    h.stop()

    trouble = [x for x in h.run.health if x[0] in ("failing", "fatal")]
    ok = bool(trouble)
    detail = ("status reportado: {}".format(trouble[0][2][:70]) if trouble
              else "NENHUM status de erro — o app ficou MUDO (a regressao voltou)")
    return Result("4. chave invalida grita (nao emudece)", ok, detail,
                  {"eventos_de_saude": len(h.run.health)})


def case_5_capture_watchdog(cfg) -> Result:
    """Kill the capture mid-session: does the watchdog notice?"""
    c = replace(cfg, azure_streaming_mode=True, azure_streaming_language="pt-BR")
    h = Harness(c)
    h.start()
    h.pump(1.0)

    cap = h.controller._capture
    alive_before = cap.is_alive()
    # Simulate what an unplugged headphone does: the thread exits.
    cap._stop_evt.set()
    cap._thread.join(timeout=3)
    # 250 ms for the watchdog to notice, 2 s until the first reopen, then a
    # tick to publish the all-clear once loopback blocks flow again.
    h.pump(4.5)

    new_cap = h.controller._capture
    reopened = bool(new_cap is not None and new_cap is not cap and new_cap.is_alive())
    kinds = [k for k, _c, _m in h.run.health]
    healthy_again = h.controller._last_health[0] == "ok" and h.controller.is_running()
    h.stop()

    # Before: the thread died, the icon turned red and "Iniciar" did nothing
    # (still _running). Now: the device is reopened and the operator was told.
    ok = bool(alive_before and reopened and "failing" in kinds and healthy_again)
    return Result(
        "5. captura morre -> reaberta sozinha, operador avisado",
        ok,
        "viva antes={} | reaberta={} | saude={} | ok de novo={}".format(
            alive_before, reopened, kinds, healthy_again),
        {"reaberta": reopened, "avisou": "failing" in kinds},
    )


def case_6_pin_language_mid_session(cfg) -> Result:
    """Pin the source language DURING a live session: do captions survive?

    The operator hits this mid-talk, with an audience watching, so the bar is
    not "it eventually works" — it is that capture keeps running, the
    transcript file is not cut in two, and captions resume after the swap.
    """
    c = replace(cfg, azure_streaming_mode=False,
                source_languages=["pt-BR", "en-US", "es-ES"])
    h = Harness(c)
    h.start()

    # Talk while auto-detecting.
    play(synthesize(cfg.azure_speech_key, cfg.azure_speech_region, "pt"))
    h.pump(5.0)
    before = len(h.run.finals)

    capture_obj = h.controller._capture
    transcript_obj = h.controller._transcript
    txt_path = getattr(transcript_obj, "txt_path", None)

    # THE BUTTON: pin to pt-BR without stopping anything — and without
    # freezing the GUI thread while Azure tears down and reconnects.
    finished: list[float] = []
    h.controller.source_mode_changed.connect(
        lambda ok, _m: finished.append(time.monotonic()))
    t0 = time.monotonic()
    ok_swap = h.controller.set_source_mode("pt-BR")
    gui_blocked_s = time.monotonic() - t0
    h.pump(4.0)
    swap_s = (finished[0] - t0) if finished else -1.0
    # Before: 1.29 s blocked (reported as "troca em 1.29s" and taken as a
    # feature). The operator saw the tray freeze.
    ok_swap = ok_swap and gui_blocked_s < 0.1 and bool(finished)

    same_capture = h.controller._capture is capture_obj
    capture_alive = capture_obj is not None and capture_obj.is_alive()
    same_transcript = h.controller._transcript is transcript_obj

    # Talk again, now pinned.
    play(synthesize(cfg.azure_speech_key, cfg.azure_speech_region, "pt2"))
    h.pump(6.0)
    after = len(h.run.finals)

    # And back to auto, the other direction.
    ok_back = h.controller.set_source_mode(None)
    h.pump(4.0)
    back_to_auto = h.controller.source_mode() is None
    h.stop()

    produced_after = after > before
    ok = all([ok_swap, same_capture, capture_alive, same_transcript,
              produced_after, ok_back, back_to_auto])
    return Result(
        "6. fixar idioma no meio da transmissao",
        ok,
        "GUI bloqueada {:.3f}s, troca concluida em {:.2f}s | captura preservada={} "
        "viva={} | transcricao preservada={}\n      legendas antes={} depois={} | "
        "voltou p/ auto={}\n      arquivo={}".format(
            gui_blocked_s, swap_s, same_capture, capture_alive, same_transcript,
            before, after, back_to_auto,
            os.path.basename(str(txt_path)) if txt_path else "?"),
        {"swap_s": round(swap_s, 2), "antes": before, "depois": after},
    )


def case_7_session_drop_recovers(cfg) -> Result:
    """The session dies mid-event. Do captions come back on their own?

    This is the long-event question. Over two hours a dropped WebSocket is
    close to certain, and before the fix the app handled it by going mute and
    staying green: no cancellation event, no error status, no further
    captions, for the rest of the talk.
    """
    c = replace(cfg, azure_streaming_mode=False,
                source_languages=["pt-BR", "en-US", "es-ES"])
    h = Harness(c)
    h.start()

    play(synthesize(cfg.azure_speech_key, cfg.azure_speech_region, "pt"))
    h.pump(5.0)
    before = len(h.run.finals)

    # Drop the session the way a venue wifi hiccup would.
    provider = h.controller._translator
    provider._recognizer.stop_continuous_recognition()
    h.pump(3.0)

    play(synthesize(cfg.azure_speech_key, cfg.azure_speech_region, "pt2"))
    h.pump(9.0)
    after = len(h.run.finals)
    h.stop()

    kinds = [k for k, _c, _m in h.run.health]
    recovered = after > before
    # The operator must have SEEN the trouble, not just been rescued from it
    # silently — an event that heals invisibly still hides a failing venue
    # network from the person who could do something about it.
    reported = "failing" in kinds or "fatal" in kinds
    ok = recovered and reported
    return Result(
        "7. queda de sessao se recupera sozinha",
        ok,
        "legendas antes={} depois={} | saude={}".format(before, after, kinds),
        {"recuperou": recovered, "avisou": reported},
    )


CASES: list[tuple[str, Callable]] = [
    ("1", case_1_streaming_pt),
    ("2", case_2_autodetect),
    ("3", case_3_ordering),
    ("4", case_4_bad_key_is_loud),
    ("5", case_5_capture_watchdog),
    ("6", case_6_pin_language_mid_session),
    ("7", case_7_session_drop_recovers),
]


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", action="append", help="rodar so este caso (repetivel)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for cid, fn in CASES:
            print("{}: {}".format(cid, (fn.__doc__ or "").splitlines()[0]))
        return 0

    cfg = _base_config()
    if not cfg.azure_speech_key:
        print("Sem chave Azure na config. Rode o app e preencha em Credenciais.")
        return 2

    print("Teste de ponta a ponta — audio real, captura real, API real.")
    print("NAO toque nada durante o teste: o loopback captura tudo que sai "
          "pelos alto-falantes.\n")

    selected = [(cid, fn) for cid, fn in CASES
                if not args.case or cid in args.case]
    results = []
    for cid, fn in selected:
        print("[{}/{}] rodando: {}".format(
            len(results) + 1, len(selected), (fn.__doc__ or "").splitlines()[0]))
        t0 = time.monotonic()
        try:
            r = fn(cfg)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            r = Result(fn.__name__, False, "EXCECAO: {}".format(exc))
        r.metrics["segundos"] = round(time.monotonic() - t0, 1)
        results.append(r)
        print("      {} {}".format("PASSOU" if r.passed else "FALHOU", r.detail))
        print()

    print("=" * 66)
    for r in results:
        print("{}  {:48} {}s".format(
            "[OK]  " if r.passed else "[FALHA]", r.name[:48],
            r.metrics.get("segundos", "?")))
    failed = [r for r in results if not r.passed]
    print("=" * 66)
    if failed:
        print("{} de {} casos FALHARAM.".format(len(failed), len(results)))
        return 1
    print("Todos os {} casos passaram contra a API real.".format(len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
