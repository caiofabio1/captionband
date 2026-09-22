"""Event vocabulary: terms the recogniser should be biased towards.

Fixes the RECOGNITION half of the terminology problem — "CABSIN" coming out as
"cab sim", "PNPIC" as "pé en pi i ce". It does NOT control how a term is
rendered in the target language; Azure's phrase list biases recognition only.
Getting the source word right is still the higher-leverage half, because a
term the model mis-heard produces a garbage translation whatever else is done
downstream.

The file is plain text on purpose: one term per line, `#` starts a comment.
The operator edits it in Notepad between sessions, not during a talk.

Limits and the allowed character set come from Microsoft's own page, quoted
where they matter (checked 2026-09-21):
https://learn.microsoft.com/azure/ai-services/speech-service/improve-accuracy-phrase-list

  - "a phrase list shouldn't have more than 2,000 phrases. Note that a longer
    phrase list will impact quality and latency."
  - "Allowed characters include locale-specific letters and digits, white
    space characters, and special characters such as + - $ : ( ) { } _ . ? @
    \\ ' & # % ^ * ` < > ; / . The system removes other special characters
    from the phrase."
  - Weight runs 0.0 to 2.0: "0.0: Disables the phrase list / 1.0: Default
    weight / 2.0: Maximum weight". It "applies to the complete list", not per
    phrase.

Whether a phrase list actually helps while automatic language identification
is active is NOT documented either way — neither the phrase-list page nor the
language-identification page mentions the other. `medir_vocabulario.py` exists
to answer that empirically before anyone trusts this.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Microsoft's ceiling, verbatim from the page above. Going over it is not an
# error at the API level — the service just gets worse and slower, which on a
# projected caption is indistinguishable from the app being broken.
MAX_PHRASES = 2000

# The punctuation the service keeps. Everything else it silently strips, so we
# strip it here too: a term that reaches the service already clean is a term
# the operator can compare against the log without wondering what happened.
ALLOWED_PUNCTUATION = set("+-$:(){}_.?@\\'’&#%^*`<>;/\"")

WEIGHT_MIN = 0.0
WEIGHT_MAX = 2.0
WEIGHT_DEFAULT = 1.0

HEADER = """\
# Vocabulário do evento — CaptionBand
#
# Um termo por linha. Linhas começando com # são ignoradas.
# Serve para o reconhecimento ACERTAR a palavra falada: nomes próprios,
# siglas, termos da área. Não muda como o termo é traduzido.
#
# Depois de editar, feche e abra a legenda de novo (ou pare e inicie a
# tradução) — a lista é enviada ao serviço quando o reconhecimento começa.
#
# Máximo de 2000 termos. Lista longa piora a qualidade e a latência, então
# prefira 100-300 termos que de fato aparecem nas falas.

CABSIN
PICS
PNPIC
"""


def sanitize(term: str) -> str:
    """Trim the term and drop characters the service would strip anyway.

    Allow-list, not deny-list, and that is the whole design: a term pasted
    from a PDF or a browser carries zero-width joiners and bidi marks that
    look like nothing on screen and make two visually identical terms compare
    unequal. That class of invisible character already cost this project a day
    on Google Scholar titles. An allow-list drops all of them without anyone
    having to enumerate them — `isalnum()` is False for U+200B and U+202A, and
    they are not punctuation the service keeps.

    A first version also filtered Unicode category C explicitly. A mutation
    test showed that line was dead: every codepoint it removed was already
    excluded by the allow-list below, so the test that claimed to cover it was
    passing through a different branch.
    """
    cleaned = [ch for ch in term.strip()
               if ch.isalnum() or ch.isspace() or ch in ALLOWED_PUNCTUATION]
    return " ".join("".join(cleaned).split())


def parse(text: str) -> list[str]:
    """Terms from the file's text, in order, without duplicates.

    Case is preserved (the operator writes CABSIN, not cabsin), but the
    duplicate check is case-insensitive: the same term twice in different
    casing is one phrase to the service and two lines of noise in the file.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        term = sanitize(line)
        if not term:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def load_terms(path) -> list[str]:
    """Read the vocabulary file. A missing or unreadable file means no bias.

    Never raises. A vocabulary that cannot be read must cost the operator
    nothing worse than unimproved captions — the file is edited by hand, in a
    text editor, minutes before a talk.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        log.exception("vocabulário ilegível em %s; seguindo sem ele", path)
        return []
    terms = parse(text)
    if len(terms) > MAX_PHRASES:
        log.warning(
            "vocabulário tem %d termos; a Azure recomenda no máximo %d "
            "(lista longa piora qualidade e latência). Usando os primeiros %d.",
            len(terms), MAX_PHRASES, MAX_PHRASES)
        terms = terms[:MAX_PHRASES]
    return terms


def clamp_weight(value: float) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return WEIGHT_DEFAULT
    return max(WEIGHT_MIN, min(WEIGHT_MAX, weight))


def append_terms(path, texto: str) -> int:
    """Acrescenta ao arquivo os termos de `texto` que ainda não estão lá.

    Devolve quantos entraram. Idempotente: clicar no botão duas vezes não
    duplica nada, porque a comparação é a mesma de `parse()` — sanitizada e
    sem diferenciar caixa. Sem isso, o segundo clique encheria o arquivo de
    repetição que o operador teria de limpar à mão.

    Acrescenta, nunca reescreve: o arquivo pode já ter os termos do evento
    dele, e um pacote pronto não tem o direito de sobrepor isso.
    """
    ensure_file(path)
    try:
        atual = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log.exception("vocabulário ilegível em %s; nada acrescentado", path)
        return 0

    ja_tem = {t.casefold() for t in parse(atual)}
    novas: list[str] = []
    for raw in texto.splitlines():
        linha = raw.strip()
        if not linha or linha.startswith("#"):
            novas.append(raw.rstrip())   # cabeçalhos de seção entram junto
            continue
        termo = sanitize(linha)
        if not termo or termo.casefold() in ja_tem:
            continue
        ja_tem.add(termo.casefold())
        novas.append(termo)

    # Só termo, sem termo novo nenhum: não sujar o arquivo com cabeçalhos
    # órfãos de um pacote que já estava inteiro lá.
    if not any(x and not x.startswith("#") for x in novas):
        return 0
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(novas) + "\n")
    except OSError:
        log.exception("não foi possível escrever em %s", path)
        return 0
    return sum(1 for x in novas if x and not x.startswith("#"))


def ensure_file(path) -> bool:
    """Create the file with its instructions if it is not there yet.

    Returns True when a file was created. Anything that goes wrong is logged
    and swallowed: this runs from a button in Settings and must not be able to
    take the window down with it.
    """
    try:
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")
        return True
    except OSError:
        log.exception("não foi possível criar o vocabulário em %s", path)
        return False
