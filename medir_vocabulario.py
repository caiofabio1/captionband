r"""Mede se o vocabulário do evento melhora o reconhecimento — ou não.

POR QUE ESTE SCRIPT EXISTE
--------------------------
A documentação da Azure NÃO diz se a phrase list funciona junto com a
identificação automática de idioma. A página da phrase list não menciona LID;
a página de LID não menciona phrase list. Verificado em 2026-09-21. Um painel
de três modelos apontou essa combinação como o elo mais fraco do plano, e
nenhum deles sabia a resposta.

E o app roda exatamente essa combinação: at-start LID sobre pt-BR/en-US/es-ES,
com uma lista única de termos misturando os três idiomas. Se a lista for
ignorada nesse modo, colocar 300 termos nela não muda nada e a sensação de
melhora vem de outro lugar.

Então: mesmo áudio, duas condições, contagem por termo. Nada de impressão.

COMO RODAR
----------
    python medir_vocabulario.py audio.wav --termos CABSIN PNPIC auriculoterapia
    python medir_vocabulario.py audio.wav                # usa o vocabulary.txt
    python medir_vocabulario.py audio.wav --repeticoes 3 # o serviço não é determinístico

O áudio precisa ser WAV PCM 16 bits mono. Para extrair de uma gravação:
    ffmpeg -i gravacao.mp4 -ac 1 -ar 16000 -sample_fmt s16 audio.wav

O QUE ELE NÃO FAZ
-----------------
Não julga a TRADUÇÃO. Mede se o termo aparece certo no texto ORIGINAL
reconhecido, que é a metade que a phrase list pode afetar. Tradução é outro
problema, com outra solução.

Consome cota da sua conta Azure: cada repetição manda o áudio inteiro duas
vezes.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time
import unicodedata
import wave
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("medir")

CHUNK_SECONDS = 0.2


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento, sem pontuação — para comparar termo com fala.

    O reconhecimento escreve "Cabsin", "CABSIN," e "cabsin" para a mesma
    palavra, e "auriculoterapia" com e sem acento dependendo do idioma que o
    LID escolheu. Comparar a forma crua transformaria variação de grafia em
    erro de reconhecimento, e o número mediria a minha comparação em vez do
    serviço.
    """
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w\s]", " ", sem_acento.lower())


def conta_ocorrencias(texto_norm: str, termo: str) -> int:
    """Quantas vezes o termo aparece, respeitando fronteira de palavra.

    Fronteira importa: sem ela, "SUS" casa dentro de "sustentável" e o número
    fica alto sem que o serviço tenha acertado nada.
    """
    alvo = normalizar(termo).strip()
    if not alvo:
        return 0
    return len(re.findall(rf"(?<!\w){re.escape(alvo)}(?!\w)", texto_norm))


def ler_wav(caminho: Path) -> tuple[bytes, int]:
    with wave.open(str(caminho), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise SystemExit(f"{caminho.name}: precisa ser PCM 16 bits "
                             f"(tem {wf.getsampwidth() * 8} bits)")
        if wf.getnchannels() != 1:
            raise SystemExit(f"{caminho.name}: precisa ser mono "
                             f"(tem {wf.getnchannels()} canais)")
        return wf.readframes(wf.getnframes()), wf.getframerate()


def uma_passada(cfg, audio: bytes, samplerate: int, termos: list[str],
                peso: float) -> tuple[str, float, Counter[str]]:
    """Roda o áudio pelo provedor REAL: (texto, segundos, idiomas detectados).

    Usa o AzureProvider do app, não uma chamada solta ao SDK: a pergunta é se
    o vocabulário funciona NO CAMINHO QUE VAI AO PALCO, com at-start LID, os
    mesmos parâmetros de segmentação e a mesma forma de anexar a phrase list.
    Uma medição sobre um recognizer montado à mão mediria outro programa.
    """
    from providers.azure import AzureProvider

    partes: list[str] = []
    idiomas: Counter[str] = Counter()
    pronto = threading.Event()

    def on_event(ev) -> None:
        if getattr(ev, "is_final", False) and getattr(ev, "original", ""):
            partes.append(ev.original)
            # O idioma que o LID escolheu, por fala. É a metade que faltava:
            # dois painelistas de famílias diferentes apontaram, sem se
            # falarem, que uma lista pesada em português pode enviesar a
            # ESCOLHA DE IDIOMA num evento em inglês — e isso não está
            # documentado em lado nenhum. Contar acerto por termo sem olhar o
            # idioma mediria metade do risco.
            idiomas[getattr(ev, "detected_language", "") or "?"] += 1

    prov = AzureProvider(
        speech_key=cfg.azure_speech_key,
        region=cfg.azure_speech_region,
        source_languages=cfg.source_languages,
        target_languages=cfg.target_languages,
        on_event=on_event,
        samplerate=samplerate,
        streaming_mode=cfg.azure_streaming_mode,
        streaming_language=cfg.azure_streaming_language,
        phrases=termos if peso > 0 else [],
        phrase_weight=peso,
    )
    inicio = time.monotonic()
    prov.start()
    try:
        # Empurrado em tempo real, não de uma vez: o serviço segmenta por
        # silêncio, e jogar 20 minutos de PCM num burst produz uma
        # segmentação que nenhum evento real teria.
        passo = int(samplerate * 2 * CHUNK_SECONDS)
        for i in range(0, len(audio), passo):
            prov.push_audio(audio[i:i + passo])
            time.sleep(CHUNK_SECONDS)
        pronto.wait(timeout=8.0)   # rabo de reconhecimento depois do áudio
    finally:
        prov.stop()
    return " ".join(partes), time.monotonic() - inicio, idiomas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mede o efeito do vocabulário no reconhecimento.")
    ap.add_argument("audio", type=Path, help="WAV PCM 16 bits mono")
    ap.add_argument("--termos", nargs="*", default=None,
                    help="termos a medir (padrão: o vocabulary.txt do app)")
    ap.add_argument("--peso", type=float, default=None,
                    help="peso da phrase list (padrão: o da config)")
    ap.add_argument("--repeticoes", type=int, default=1,
                    help="o serviço não é determinístico; 3 dá uma ideia melhor")
    ap.add_argument("--salvar", type=Path, default=None,
                    help="grava as transcrições cruas para inspeção")
    args = ap.parse_args(argv)

    if not args.audio.exists():
        print(f"não achei o áudio: {args.audio}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import load_config, vocabulary_path
    from vocabulary import MAX_PHRASES, clamp_weight, load_terms

    cfg = load_config()
    if not cfg.azure_speech_key:
        print("sem chave da Azure — abra Configurações → Credenciais primeiro",
              file=sys.stderr)
        return 2

    termos = args.termos if args.termos else load_terms(vocabulary_path())
    if not termos:
        print(f"nenhum termo para medir. Preencha {vocabulary_path()} "
              f"ou passe --termos CABSIN PNPIC ...", file=sys.stderr)
        return 2
    if len(termos) > MAX_PHRASES:
        print(f"aviso: {len(termos)} termos, acima do limite de {MAX_PHRASES} "
              f"da Azure; só os primeiros vão ser enviados")

    peso = clamp_weight(args.peso if args.peso is not None
                        else cfg.vocabulary_weight)
    if peso <= 0:
        print("o peso está em 0 (vocabulário desligado). Use --peso 1.0 para "
              "medir contra ele ligado.", file=sys.stderr)
        return 2

    audio, samplerate = ler_wav(args.audio)
    duracao = len(audio) / (samplerate * 2)
    print(f"áudio    : {args.audio.name}  {duracao / 60:.1f} min  {samplerate} Hz")
    print(f"termos   : {len(termos)}")
    print(f"peso     : {peso}")
    print(f"idiomas  : {cfg.source_languages} (at-start LID)")
    print(f"repetições: {args.repeticoes}  →  "
          f"{args.repeticoes * 2 * duracao / 60:.0f} min de áudio enviados\n")

    sem: Counter[str] = Counter()
    com: Counter[str] = Counter()
    lid_sem: Counter[str] = Counter()
    lid_com: Counter[str] = Counter()
    textos: list[tuple[str, str]] = []
    for r in range(args.repeticoes):
        for rotulo, p, acumulador, lid in (
                ("SEM vocabulário", 0.0, sem, lid_sem),
                ("COM vocabulário", peso, com, lid_com)):
            print(f"  [{r + 1}/{args.repeticoes}] {rotulo}...", flush=True)
            texto, gasto, detectados = uma_passada(
                cfg, audio, samplerate, termos, p)
            textos.append((f"{rotulo} #{r + 1}", texto))
            norm = normalizar(texto)
            for termo in termos:
                acumulador[termo] += conta_ocorrencias(norm, termo)
            lid.update(detectados)
            print(f"        {len(texto)} chars em {gasto:.0f}s  "
                  f"idiomas: {dict(detectados)}")

    print("\n" + "=" * 62)
    print(f"{'termo':<28}{'sem':>7}{'com':>7}{'':>4}")
    print("=" * 62)
    melhorou = piorou = igual = 0
    for termo in termos:
        a, b = sem[termo], com[termo]
        if b > a:
            marca, melhorou = "melhorou", melhorou + 1
        elif b < a:
            marca, piorou = "PIOROU", piorou + 1
        else:
            marca, igual = "=", igual + 1
        print(f"{termo[:28]:<28}{a:>7}{b:>7}   {marca}")
    print("=" * 62)
    print(f"melhoraram {melhorou} · pioraram {piorou} · iguais {igual}")
    print(f"total de acertos: sem={sum(sem.values())}  com={sum(com.values())}")

    print("\nIDIOMA ESCOLHIDO PELO LID (o risco que ninguém documentou)")
    print(f"  sem vocabulário: {dict(lid_sem) or 'nenhuma fala final'}")
    print(f"  com vocabulário: {dict(lid_com) or 'nenhuma fala final'}")
    mudou = sorted(k for k in set(lid_sem) | set(lid_com)
                   if lid_sem.get(k, 0) != lid_com.get(k, 0))
    if mudou:
        print(f"  ATENCAO: a distribuicao MUDOU em {mudou}")
        print("    Se este áudio é de um idioma só e o vocabulário fez o LID")
        print("    escolher outro, o vocabulário está enviesando a DETECÇÃO.")
        print("    O remédio é baixar o peso ou separar a lista por idioma —")
        print("    não acrescentar mais termos.")
    else:
        print("  não mudou — o vocabulário não mexeu na detecção de idioma")

    if melhorou == 0 and sum(com.values()) == sum(sem.values()):
        print("\nVEREDITO: o vocabulário NÃO mudou nada neste áudio.")
        print("  Duas leituras possíveis, e elas pedem ações opostas:")
        print("  1. a phrase list está sendo ignorada com LID ativo — teste de")
        print("     novo com um único idioma fixo (azure_streaming_mode) antes")
        print("     de concluir;")
        print("  2. os termos já eram reconhecidos certo, e não havia o que")
        print("     melhorar — confira as transcrições com --salvar.")
    elif piorou > melhorou:
        print("\nVEREDITO: piorou. Peso alto faz o serviço preferir seus termos")
        print("  mesmo quando ninguém os disse. Tente --peso 1.0 ou menos.")
    else:
        print(f"\nVEREDITO: o vocabulário ajudou em {melhorou} termo(s).")

    if args.salvar:
        args.salvar.write_text(
            "\n\n".join(f"### {r}\n{t}" for r, t in textos), encoding="utf-8")
        print(f"\ntranscrições cruas em {args.salvar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
