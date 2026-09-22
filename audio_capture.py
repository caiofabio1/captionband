"""WASAPI loopback audio capture for Windows.

Captures system audio (whatever is playing through the speakers/Teams) without
needing a virtual cable, via the `soundcard` library which has native WASAPI
loopback support.

Optionally sums in the local MICROPHONE, so that in a conference the person
speaking in the room is captioned too — see `MixedCapture` at the bottom of
this file for why that is a sum and not a second recogniser.

Enumeration goes through `soundcard` as well (`sc.all_speakers()` /
`sc.all_microphones()`); an earlier version of this note credited
`sounddevice`, which the module no longer imports.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import numpy as np
import soundcard as sc

log = logging.getLogger(__name__)

# COM apartment helpers. Every thread that touches `soundcard` needs COM
# initialised on it, because the process is no longer blanket-MTA: the entry
# point claims STA for the GUI thread so Qt works properly. See the comment at
# the top of translator.py for the measurement behind this.
_COINIT_MULTITHREADED = 0x0
# RPC_E_CHANGED_MODE: this thread already has a different apartment. Harmless —
# it means somebody initialised COM here first, so we must NOT uninitialise it.
_RPC_E_CHANGED_MODE = 0x80010106


def _com_initialize_mta() -> bool:
    """Put the CALLING thread in the MTA apartment. True if we must balance it."""
    try:
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED) & 0xFFFFFFFF
    except Exception:
        return False
    if hr == _RPC_E_CHANGED_MODE:
        log.debug("com: thread already in another apartment; not claiming it")
        return False
    return True


def _com_uninitialize() -> None:
    try:
        import ctypes
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


def list_output_devices() -> list[dict]:
    """List output devices visible to soundcard (these support loopback)."""
    out: list[dict] = []
    try:
        speakers = sc.all_speakers()
    except Exception:
        log.exception("could not list speakers")
        return out
    for spk in speakers:
        out.append({
            "id": spk.id,
            "name": spk.name,
            "channels": spk.channels,
            "default_samplerate": 48000,
        })
    return out


def find_device(name_substring: str | None):
    """Resolve a speaker by partial name match. Returns a soundcard speaker or None for default."""
    if not name_substring:
        try:
            return sc.default_speaker()
        except Exception:
            log.exception("could not get default speaker")
            return None
    try:
        speakers = sc.all_speakers()
    except Exception:
        log.exception("could not enumerate speakers")
        return None
    needle = name_substring.lower()
    for spk in speakers:
        if needle in spk.name.lower():
            return spk
    return None


def list_input_devices() -> list[dict]:
    """Microfones REAIS, sem os pseudo-microfones de loopback.

    `include_loopback=False` é o que separa os dois: com ele ligado, cada
    saída aparece aqui como se fosse um microfone, e o operador escolheria
    "capturar o microfone" apontando para a própria saída — capturando o
    mesmo áudio duas vezes sem nenhum aviso.
    """
    out: list[dict] = []
    try:
        mics = sc.all_microphones(include_loopback=False)
    except Exception:
        log.exception("could not list microphones")
        return out
    for mic in mics:
        out.append({"id": mic.id, "name": mic.name, "channels": mic.channels})
    return out


def find_input_device(name_substring: str | None):
    """Resolve a real microphone by partial name. None = the Windows default."""
    try:
        if not name_substring:
            return sc.default_microphone()
        needle = name_substring.lower()
        for mic in sc.all_microphones(include_loopback=False):
            if needle in mic.name.lower():
                return mic
        log.warning("microfone %r não encontrado; usando o padrão", name_substring)
        return sc.default_microphone()
    except Exception:
        log.exception("could not resolve microphone")
        return None


class AudioCapture:
    """Captures system audio via WASAPI loopback and forwards PCM 16-bit bytes to a callback.

    Uses soundcard.Microphone(include_loopback=True) on the chosen output device.
    """

    def __init__(
        self,
        on_audio: Callable[[bytes], None],
        device_index=None,
        samplerate: int = 16000,
        channels: int = 1,
        blocksize_ms: int = 50,
        on_died: Callable[[str], None] | None = None,
        loopback: bool = True,
    ):
        # device_index can be: None (default), a soundcard speaker, or a name substring
        self.on_audio = on_audio
        self.device_arg = device_index
        # False = capture a REAL microphone instead of what the speakers play.
        # Same recorder API either way; only what gets opened changes.
        self.loopback = loopback
        self.samplerate = samplerate
        self.channels = max(1, channels)
        self.blocksize = max(1, int(samplerate * blocksize_ms / 1000))
        # Called (from the capture thread) when the loop exits for any reason
        # other than a requested stop. Without it, unplugging the headphones
        # mid-event kills this thread while the tray icon stays green and the
        # overlay stays blank — indistinguishable from nobody speaking.
        self.on_died = on_died

        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        # Monotonic timestamp of the last audio block actually delivered.
        # The controller's watchdog reads this: a live thread that has stopped
        # producing audio is just as fatal as a dead one, and only this
        # distinguishes "silence in the room" from "capture is wedged".
        self._last_audio_at: float = 0.0
        self._died_reason: str = ""

    def _resolve_speaker(self):
        if self.device_arg is None or self.device_arg == "":
            return sc.default_speaker()
        if hasattr(self.device_arg, "id") and hasattr(self.device_arg, "name"):
            return self.device_arg
        if isinstance(self.device_arg, str):
            return find_device(self.device_arg)
        return sc.default_speaker()

    def _resolve_microphone(self):
        if hasattr(self.device_arg, "id") and hasattr(self.device_arg, "name"):
            return self.device_arg
        if isinstance(self.device_arg, str) and self.device_arg:
            return find_input_device(self.device_arg)
        return find_input_device(None)

    def _run(self) -> None:
        """Capture loop.

        Every exit path other than a requested stop reports a reason through
        `on_died`. Returning quietly here is what let a disconnected audio
        device silently end the captions for the rest of an event.
        """
        reason = ""
        # Claim MTA for THIS thread. The entry point puts the GUI thread in STA
        # so Qt's OleInitialize() succeeds, which means soundcard's module-level
        # CoInitializeEx(MTA) got RPC_E_CHANGED_MODE and set com_loaded=False.
        # Without this call the capture thread has no COM at all and
        # sc.default_speaker() dies with 0x800401F0 CO_E_NOTINITIALIZED — the
        # exact failure that made the previous attempt at this fix get reverted.
        com_ready = _com_initialize_mta()
        try:
            if self.loopback:
                speaker = self._resolve_speaker()
                if speaker is None:
                    reason = "Nenhum dispositivo de saída de áudio encontrado."
                    log.error("audio capture: no output device resolved")
                    return
                log.info("audio capture using speaker: %s", speaker.name)
                source = sc.get_microphone(speaker.name, include_loopback=True)
            else:
                source = self._resolve_microphone()
                if source is None:
                    reason = "Nenhum microfone encontrado."
                    log.error("audio capture: no input device resolved")
                    return
                log.info("audio capture using microphone: %s", source.name)

            with source.recorder(
                samplerate=self.samplerate,
                channels=self.channels,
                blocksize=self.blocksize,
            ) as recorder:
                self._last_audio_at = time.monotonic()
                while not self._stop_evt.is_set():
                    try:
                        data = recorder.record(numframes=self.blocksize)
                    except Exception as exc:
                        # The common live-event trigger: headphones unplugged,
                        # Bluetooth dropped, or Windows switched the default
                        # output device out from under us.
                        reason = f"A captura de áudio parou: {exc}"
                        log.exception("recorder.record failed")
                        break
                    # Mark liveness BEFORE inspecting the payload. This
                    # timestamp answers "is the recorder still responding?",
                    # not "was there sound?" — a driver that returns empty
                    # buffers during a pause is alive, and skipping the
                    # update there would let the watchdog kill a healthy
                    # session the moment the speaker stopped to breathe.
                    self._last_audio_at = time.monotonic()
                    if data is None or data.size == 0:
                        continue
                    if data.ndim > 1 and data.shape[1] > 1 and self.channels == 1:
                        data = data.mean(axis=1, keepdims=True)
                    pcm16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
                    try:
                        self.on_audio(pcm16.tobytes())
                    except Exception:
                        log.exception("on_audio callback failed")
        except Exception as exc:
            reason = f"A captura de áudio caiu: {exc}"
            log.exception("audio capture thread crashed")
        finally:
            if com_ready:
                _com_uninitialize()
            self._running = False
            if reason and not self._stop_evt.is_set():
                self._died_reason = reason
                if self.on_died is not None:
                    try:
                        self.on_died(reason)
                    except Exception:
                        log.exception("on_died callback raised")

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stop_evt.clear()
            # Start the liveness clock NOW, not when the recorder opens. A
            # thread that hangs opening the WASAPI endpoint never sets it in
            # _run, and seconds_since_audio() would report 0.0 forever —
            # invisible to the watchdog.
            self._last_audio_at = time.monotonic()
            self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
            self._thread.start()
            self._running = True
            log.info("audio capture started: sr=%s ch=%s block=%s", self.samplerate, self.channels, self.blocksize)

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._stop_evt.set()
            if self._thread is not None:
                self._thread.join(timeout=2)
                self._thread = None
            self._running = False
            log.info("audio capture stopped")

    def is_running(self) -> bool:
        return self._running

    def is_alive(self) -> bool:
        """True only if the capture THREAD is actually still running.

        `is_running()` reports intent (start() was called and stop() was not);
        this reports reality. They diverge exactly when something went wrong,
        which is the case the watchdog exists for.
        """
        t = self._thread
        return bool(t is not None and t.is_alive())

    def seconds_since_audio(self) -> float:
        """How long since the last delivered audio block.

        A thread can be alive and still deliver nothing (a wedged WASAPI
        endpoint does exactly this). Loopback capture yields blocks even in a
        silent room — digital silence is still samples — so a long gap here
        means the capture is broken, not that the room went quiet.
        """
        if self._last_audio_at <= 0:
            return 0.0
        return time.monotonic() - self._last_audio_at

    def died_reason(self) -> str:
        return self._died_reason


# --------------------------------------------------------------------------
# Loopback + microfone, somados num único stream.
# --------------------------------------------------------------------------
#
# POR QUE SOMAR, E NÃO ABRIR DOIS RECONHECEDORES
#
# Numa conferência, o loopback traz quem fala do outro lado (o Teams não
# devolve a sua própria voz para os seus alto-falantes) e o microfone traz
# quem fala NA SALA. São metades diferentes da mesma conversa, não duas
# conversas. Um segundo reconhecedor dobraria o custo por minuto na Azure,
# dobraria a identificação de idioma e ainda exigiria costurar duas legendas
# numa banda só. Somar entrega a conversa inteira num reconhecedor.
#
# O QUE FOI MEDIDO NESTA MÁQUINA (2026-09-22), porque tudo abaixo depende
# disso:
#
#   - os dois abrem ao mesmo tempo: WASAPI em modo compartilhado deixa gravar
#     o loopback e o microfone em paralelo, sem exclusividade;
#   - em regime, loopback entregou 16041 Hz e microfone 16002 Hz contra os
#     16000 pedidos. São relógios FÍSICOS diferentes: 0,25% de deriva
#     relativa, ~2,5 ms por segundo, ~9 s por hora se ninguém corrigir;
#   - abrir o loopback levou 396 ms e o microfone 753 ms, então os primeiros
#     ~350 ms de sala não entram. Ninguém começa a falar no primeiro
#     terço de segundo depois de clicar em iniciar.
#
# Daí as três decisões:
#
# 1. O LOOPBACK DÁ O RELÓGIO. Ele entrega blocos mesmo numa sala em silêncio
#    (silêncio digital também é amostra), então não há laço de `sleep` nem
#    thread extra: a cada bloco de loopback, pega-se o mesmo tanto de
#    microfone e soma. O `seconds_since_audio()` do watchdog continua
#    medindo exatamente o que media antes.
# 2. FILA COM TETO. Se o microfone estiver adiantado, a fila dele cresce para
#    sempre e a voz da sala vai atrasando em relação à legenda. O teto corta
#    o excesso mais antigo — perde-se alguns ms, não a sincronia.
# 3. COLCHÃO ANTES DE COMEÇAR. Bloco de áudio não chega com régua; sem um
#    colchão, cada engasgo do agendador viraria um buraco de silêncio no meio
#    da fala. Espera-se o colchão encher antes de somar, e enquanto isso vai
#    só o loopback — nunca silêncio inventado.
#
# O QUE ISTO NÃO FAZ: cancelamento de eco. Se a máquina toca o áudio da
# conferência em ALTO-FALANTE, o microfone captura de volta quem falou do
# outro lado, e a mesma fala chega ao reconhecedor duas vezes, desencontrada
# por alguns ms. O resultado é legenda duplicada e picada. O cancelamento de
# eco do Teams age no que ELE envia, não no endpoint que gravamos aqui.
# Fone de ouvido resolve; e a tela de Configurações avisa isso.

_MIX_PREFILL_MS = 100
_MIX_MAX_LAG_MS = 300


class MixedCapture:
    """Loopback (relógio) + microfone (somado), na interface de AudioCapture.

    Apresenta de propósito os mesmos métodos que `AudioCapture`: o controller
    tem watchdog de thread viva, watchdog de áudio parado e reabertura de
    dispositivo em cima dessa interface, e nada disso precisa saber que agora
    há duas fontes.
    """

    def __init__(
        self,
        on_audio: Callable[[bytes], None],
        device_index=None,
        mic_device=None,
        samplerate: int = 16000,
        channels: int = 1,
        blocksize_ms: int = 50,
        mic_gain: float = 1.0,
        on_died: Callable[[str], None] | None = None,
        on_mic_lost: Callable[[str], None] | None = None,
    ):
        self.on_audio = on_audio
        self.mic_gain = max(0.0, float(mic_gain))
        self.on_mic_lost = on_mic_lost
        bytes_por_ms = samplerate * 2 / 1000.0
        self._prefill = int(_MIX_PREFILL_MS * bytes_por_ms)
        self._max_lag = int(_MIX_MAX_LAG_MS * bytes_por_ms)
        self._mic_buf = bytearray()
        self._mic_lock = threading.Lock()
        self._primed = False
        self._trimmed_bytes = 0

        self._primary = AudioCapture(
            on_audio=self._mix_and_emit,
            device_index=device_index,
            samplerate=samplerate,
            channels=channels,
            blocksize_ms=blocksize_ms,
            on_died=on_died,
        )
        self._mic = AudioCapture(
            on_audio=self._collect_mic,
            device_index=mic_device,
            samplerate=samplerate,
            channels=channels,
            blocksize_ms=blocksize_ms,
            on_died=self._mic_died,
            loopback=False,
        )

    # -- microfone ---------------------------------------------------------
    def _collect_mic(self, data: bytes) -> None:
        """Roda na thread do microfone. Só enfileira; a soma é do loopback."""
        with self._mic_lock:
            self._mic_buf.extend(data)
            excesso = len(self._mic_buf) - self._max_lag
            if excesso > 0:
                del self._mic_buf[:excesso]
                self._trimmed_bytes += excesso

    def _mic_died(self, reason: str) -> None:
        """O microfone caiu. O evento CONTINUA.

        O microfone é a metade extra; o loopback é o evento. Encaminhar isto
        para o `on_died` do controller acionaria a reabertura de captura e,
        se insistisse, derrubaria a legenda inteira por causa de um
        dispositivo auxiliar — trocando uma perda parcial por uma total.
        """
        log.error("microfone parou (a captura do sistema segue): %s", reason)
        with self._mic_lock:
            self._mic_buf.clear()
            self._primed = False
        if self.on_mic_lost is not None:
            try:
                self.on_mic_lost(reason)
            except Exception:
                log.exception("on_mic_lost callback raised")

    # -- soma --------------------------------------------------------------
    def _mix_and_emit(self, data: bytes) -> None:
        """Roda na thread do loopback, uma vez por bloco dele."""
        try:
            saida = self._mix(data)
        except Exception:
            log.exception("mixagem falhou; seguindo so com o audio do sistema")
            saida = data
        self.on_audio(saida)

    def _mix(self, data: bytes) -> bytes:
        precisa = len(data)
        with self._mic_lock:
            if not self._primed:
                if len(self._mic_buf) < self._prefill:
                    return data          # ainda enchendo o colchão
                self._primed = True
            if len(self._mic_buf) < precisa:
                # Fila secou: o colchão foi consumido por um engasgo. Volta a
                # enchê-lo em vez de despejar silêncio bloco após bloco.
                self._primed = False
                return data
            trecho = bytes(self._mic_buf[:precisa])
            del self._mic_buf[:precisa]

        if self.mic_gain <= 0.0:
            return data
        sistema = np.frombuffer(data, dtype=np.int16).astype(np.int32)
        microfone = np.frombuffer(trecho, dtype=np.int16).astype(np.int32)
        if microfone.size != sistema.size:
            return data
        if self.mic_gain != 1.0:
            microfone = (microfone * self.mic_gain).astype(np.int32)
        # int32 antes do clip: somar em int16 da a volta no estouro, e uma
        # sala alta viraria estalo em vez de voz alta.
        return np.clip(sistema + microfone, -32768, 32767).astype(np.int16).tobytes()

    # -- interface de AudioCapture ----------------------------------------
    def start(self) -> None:
        self._primary.start()
        self._mic.start()        # falhar aqui nao impede o evento

    def stop(self) -> None:
        self._mic.stop()
        self._primary.stop()
        with self._mic_lock:
            self._mic_buf.clear()
            self._primed = False
        if self._trimmed_bytes:
            log.info("mix: %d ms de microfone descartados por deriva de relogio",
                     int(self._trimmed_bytes / 32))

    def is_running(self) -> bool:
        return self._primary.is_running()

    def is_alive(self) -> bool:
        return self._primary.is_alive()

    def seconds_since_audio(self) -> float:
        return self._primary.seconds_since_audio()

    def died_reason(self) -> str:
        return self._primary.died_reason()

    def mic_is_alive(self) -> bool:
        return self._mic.is_alive()
