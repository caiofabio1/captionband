"""Vocabulários prontos, para o operador não começar de uma página branca.

São termos que a pessoa DIZ EM VOZ ALTA e que o reconhecimento erra. A phrase
list da Azure enviesa o reconhecimento, não a saída traduzida.

COMO ESTA LISTA FOI ESCOLHIDA (e o que um painel derrubou)

A primeira versão selecionava termos para "economizar o teto de 2000 frases".
Um painel de três modelos, olhando os termos de verdade, derrubou isso com
aritmética: 222 termos escritos à mão + 224 da taxonomia do EGM = 446, que é
22% do teto. **O teto nunca foi a restrição.**

O custo real é outro, e dois painelistas de famílias diferentes chegaram nele
por caminhos independentes: **viés falso**. O peso da phrase list vale para a
lista inteira (documentado pela Azure), então um termo boostado aparece na
transcrição mesmo quando ninguém o disse. Isso muda o critério:

    NÃO é "o modelo já acerta, então fora".
    É  "a chance de alguém dizer isto compensa o risco de o modelo passar a
        ouvir isto onde não foi dito?"

Consequências, aplicadas abaixo:

1. FORA palavra comum e curta sem par ambíguo — "dor", "ansiedade", "fadiga",
   "diabetes", "colesterol". O modelo já acerta e ela é ímã de falso positivo.
2. DENTRO termo raro, estrangeiro ou romanizado — "baduanjin", "wuqinxi",
   "panchakarma", "rasayana", "sowa rigpa". São os verdadeiros positivos.
3. DENTRO frase de 2 a 4 palavras. Os três painelistas convergiram: é onde a
   phrase list funciona melhor, porque corrige fronteira de palavra. "Ginkgo
   biloba" inteiro entra; o gênero "Ginkgo" sozinho não — ele não colide com
   nada e não ganha nada.
4. FORA sentença longa. A doc da Azure: "a longer phrase list will impact
   quality and latency".

DUAS COISAS QUE FICARAM SEM RESPOSTA, E ESTÃO MARCADAS NO ARQUIVO

a) Sigla dita letra por letra. "DPOC" falado "de-pê-o-cê" é corrigido por uma
   entrada escrita "DPOC"? O painel dividiu. O argumento mais técnico é do
   Gemini: a phrase list ajusta o modelo de LINGUAGEM, não é dicionário de
   pronúncia, então ela só reforça um caminho acústico que já existe. Ninguém
   sabe, e a doc não diz. As siglas ficam, numa seção separada para poderem
   sair de uma vez.

b) Lista em português num evento em inglês. DOIS painelistas apontaram, sem se
   falarem, o mesmo risco não documentado: com identificação automática de
   idioma, uma lista pesada em português pode empurrar áudio em inglês para
   fonemas portugueses — e pior, enviesar a própria escolha de idioma nos
   primeiros segundos. É o maior risco deste arquivo. `medir_vocabulario.py`
   reporta o idioma detectado por fala exatamente para medir isso.

O peso padrão do app é 1.0 (o padrão da Azure), não 2.0. Os dois painelistas
recomendaram ficar em 1.0 ou abaixo até esse experimento rodar.

O QUE NÃO ESTÁ AQUI, DE PROPÓSITO

Nomes de pessoas, sistemas internos e siglas de projeto. Varia por instituição
e este arquivo é público. O operador acrescenta os dele no próprio
vocabulary.txt, que vive em %LOCALAPPDATA% e nunca entra no repositório.

As intervenções vêm da taxonomia dos Mapas de Evidência CABSIN/BIREME,
publicada na BVS MTCI (110 intervenções, 114 desfechos), filtrada pela regra
acima.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Práticas integrativas — os verdadeiros positivos.
# Raras, estrangeiras ou romanizadas. É aqui que o reconhecimento erra.
# --------------------------------------------------------------------------
PRATICAS_INTEGRATIVAS = """
# --- Medicina Tradicional Chinesa ---
Medicina Tradicional Chinesa
auriculoterapia
auriculoterapia com sementes
auriculoterapia a laser
eletroacupuntura
acupuntura de escalpe
acupuntura craniana
acupuntura punho-tornozelo
moxabustão
moxibustão
moxa
moxabustão direta
moxabustão indireta
ventosaterapia
ventosa seca
ventosa úmida
ventosa deslizante
tui na
tuiná
fitoterapia chinesa
baduanjin
wuqinxi
qigong
chi kung
tai chi chuan
lian gong
acupontos
meridianos
zang fu
Huangdi Neijing
auriculotherapy
electroacupuncture
scalp acupuncture
moxibustion
cupping therapy
dry cupping
wet cupping

# --- Sistemas médicos ---
homeopatia unicista
homeopatia complexista
medicina antroposófica
euritmia
euritmia curativa
eurythmy
arte-terapia antroposófica
ayurveda
panchakarma
rasayana
dosha
vata
pitta
kapha
naturopatia
unani
siddha
kampo
sowa rigpa

# --- Mente-corpo ---
mindfulness
atenção plena
MBSR
MBCT
meditação transcendental
vipassana
meditação da compaixão
hatha yoga
iyengar yoga
ashtanga yoga
vinyasa yoga
kundalini yoga
yoga nidra
relaxamento muscular progressivo
imaginação guiada
hipnoterapia
biofeedback

# --- Terapias manuais ---
reflexologia podal
reflexologia palmar
shantala
liberação miofascial
myofascial release
quiropraxia
osteopatia
terapia craniossacral
craniosacral therapy
shiatsu
do-in

# --- Outras práticas ---
apiterapia
apitoxina
própolis
geleia real
ozonioterapia
musicoterapia
arte-terapia
dança-terapia
biodança
dança circular
termalismo
crenoterapia
balneoterapia
florais de Bach
terapia floral
reiki
cromoterapia
geoterapia
imposição de mãos
terapia comunitária integrativa
constelação familiar
laserterapia
aromaterapia
óleo essencial de lavanda
óleo essencial de alecrim
"""

# --------------------------------------------------------------------------
# Plantas e canabinoides — binômios INTEIROS.
# O gênero sozinho sai: não colide com nada e não ganha nada. O binômio de
# duas palavras é justamente o n-grama que o modelo genérico estropia.
# --------------------------------------------------------------------------
FITOTERAPIA = """
# --- Binômios ---
Ginkgo biloba
Valeriana officinalis
Hypericum perforatum
Curcuma longa
Panax ginseng
Echinacea purpurea
Passiflora incarnata
Mentha piperita
Matricaria recutita
Cynara scolymus

# --- Nomes populares que o modelo troca ---
erva de São João
espinheira-santa
guaco
unha-de-gato
garra-do-diabo
maracujá medicinal

# --- Cannabis ---
cannabis medicinal
canabidiol
canabinoides
canabinoides sintéticos
tetrahidrocanabinol
full spectrum
"""

# --------------------------------------------------------------------------
# Campo, políticas e organismos. Termos de 2 a 4 palavras entram inteiros —
# é o formato em que a phrase list mais ajuda.
# --------------------------------------------------------------------------
CAMPO_E_POLITICAS = """
# --- Campo ---
práticas integrativas
práticas integrativas e complementares
medicina integrativa
saúde integrativa
medicina tradicional
medicina complementar
integrative medicine
traditional medicine
complementary medicine
saberes tradicionais
medicina indígena
parteira tradicional
raizeiro
benzedeira
pajé
interculturalidade

# --- Políticas e programas (Brasil) ---
política nacional de PICS
atenção primária à saúde
estratégia saúde da família
farmácia viva
horta medicinal
Relação Nacional de Medicamentos
residência multiprofissional

# --- Organismos internacionais ---
Organização Mundial da Saúde
Organização Pan-Americana da Saúde
Assembleia Mundial da Saúde
World Health Assembly
cobertura universal de saúde
universal health coverage
Regulamento Sanitário Internacional
Codex Alimentarius
Banco Mundial
Fiocruz
Nações Unidas

# --- Evidência e método ---
revisão sistemática
revisão de escopo
scoping review
mapa de evidências
evidence gap map
lacuna de evidência
ensaio clínico randomizado
ensaio clínico pragmático
duplo-cego
cego simples
alocação aleatória
metanálise
metanálise em rede
razão de chances
risco relativo
intervalo de confiança
desfecho primário
desfecho secundário
desfecho relatado pelo paciente
viés de publicação
certeza da evidência
custo-efetividade
eventos adversos graves
"""

# --------------------------------------------------------------------------
# Siglas e nomes próprios curtos.
#
# SEÇÃO SOB SUSPEITA, e separada para poder sair de uma vez.
#
# Dois motivos, os dois levantados pelo painel e NENHUM verificado:
#
# 1. Uma entrada escrita "DPOC" pode não corrigir nada quando a pessoa fala
#    "de-pê-o-cê", porque a phrase list ajusta o modelo de LINGUAGEM e não é
#    dicionário de pronúncia.
# 2. Sigla de três letras ("SUS", "OMS", "CNS") pode virar ímã de falso
#    positivo, porque soa como transição acústica comum.
#
# Ficam porque são os termos MAIS DITOS num evento de saúde brasileiro, e
# tirar o mais importante com base em hipótese não medida é pior que medir.
# Se a qualidade cair com o vocabulário ligado, apague este bloco primeiro.
# --------------------------------------------------------------------------
SIGLAS = """
# --- Sistema de saúde (Brasil) ---
SUS
ANVISA
CONITEC
ABRASCO
CONASS
CONASEMS
CONEP
DATASUS
RENAME
PNPIC
PICS
MTCI
MTYCI
APS
ESF
UBS
NASF

# --- Financiamento e pós-graduação ---
CNPq
CAPES
FAPESP
FINEP
PPSUS

# --- Organismos ---
OMS
OPAS
PAHO
UNICEF
UNESCO
UNAIDS
OCDE
Mercosul
CPLP

# --- Instrumentos e bases ---
CID-11
ICD-11
GRADE
AMSTAR
PROSPERO
PRISMA
CONSORT
STROBE
ICTRP
ReBEC
Cochrane
LILACS
SciELO
BIREME
DeCS
MeSH
PubMed
ORCID

# --- Desfechos que são sigla ou termo raro ---
HbA1c
hemoglobina glicada
proteína C reativa
DPOC
TEPT
burnout
SII
DII
SF-36
WHOQOL
dismenorreia
cervicalgia
lombalgia
cefaleia
enxaqueca
fibromialgia
osteoartrite
artrite reumatoide
esclerose múltipla
fadiga oncológica
neuropatia induzida por quimioterapia
"""

#: Blocos na ordem em que entram no arquivo do operador.
PACOTES = {
    "práticas integrativas": PRATICAS_INTEGRATIVAS,
    "fitoterapia": FITOTERAPIA,
    "campo e políticas": CAMPO_E_POLITICAS,
    "siglas (seção sob suspeita)": SIGLAS,
}

#: Termos deliberadamente EXCLUÍDOS, com o motivo. Não é documentação solta:
#: `tests/test_vocabulary.py` verifica que nenhum deles voltou para os blocos.
EXCLUIDOS_DE_PROPOSITO = {
    # Palavra comum que o modelo já acerta e que, boostada, passa a aparecer
    # onde ninguém a disse.
    "dor", "ansiedade", "depressão", "estresse", "sono", "insônia",
    "memória", "atenção", "humor", "diabetes", "obesidade", "asma",
    "hipertensão", "psoríase", "eczema", "colesterol", "triglicerídeos",
    "glicemia", "cortisol", "náusea", "vômito", "fadiga", "hospitalização",
    "placebo", "escopo", "saúde", "hospital", "paciente",
    # Gênero sem espécie: não colide com nada, não ganha nada.
    "ginkgo", "valeriana", "hypericum", "curcuma", "echinacea", "passiflora",
}


def pacote_saude() -> str:
    """Os blocos, na ordem, com os cabeçalhos de seção preservados.

    Os comentários vão junto de propósito: o operador precisa ver por que um
    termo está lá para decidir apagar, e `parse()` ignora linha com `#`.
    """
    return "\n".join(PACOTES.values())
