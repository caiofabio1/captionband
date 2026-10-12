# Ideias do Sokuji para a caixa de legenda (2026-10-12)

Aprovado pelo Caio no chat ("Manda ver") a partir da análise do Sokuji
(kizuna-ai-lab/sokuji, AGPL-3.0). Fonte lida: código 0.43.4 no GitHub; o pacote
instalado (0.41.1) foi extraído com o REA, mas a análise estática completa
estourou o tempo (582 arquivos minificados). Nada foi copiado — o CaptionBand é
MIT; só as ideias foram reimplementadas.

## O que entra (0.13.0)

1. **Frase que não cabe nem a 16 pt guarda o fim.** O bloco é ancorado na borda
   de baixo e o começo sai pelo topo, esmaecido ao longo de uma linha. Antes
   saía o fim — as palavras sendo ditas. Referência no Sokuji:
   `src/components/Subtitle/SubtitleStream.scss` (modo compacto).
2. **Fim de frase da Azure configurável** (Configurações → Azure → "Fim de
   frase"): por pausa (padrão, nada muda), por pontuação
   (`Speech_SegmentationStrategy=Semantic`, documentado pela Microsoft contra o
   "muro de texto") ou por pausa com teto de 20 s (`Time` +
   `Speech_SegmentationMaximumTimeMs`). Pendência: a doc não confirma suporte
   do modo semântico na tradução de fala — testar com palestrante real.
3. **Sempre no topo reafirmado a cada 1 s** com `SetWindowPos(HWND_TOPMOST)`
   sem ativar nem mover (PowerPoint em apresentação se põe por cima). Para
   quando a faixa está escondida. Referência: `electron/subtitle-window.js`.
4. **Destaque da fala nova:** fundo tingido que some em 2 s, uma vez por fala;
   tinta clara em fundo escuro e vice-versa. `overlay.highlight_new_text`.

## O que ficou para depois

- Controles sobre a faixa que somem em 1,5 s sem mouse (fora do Modo evento).
- Compartilhar a legenda na rede local por QR code (página no celular).

## Verificação

- `tests/test_sokuji_ideas.py` (15 testes); 10 mutações, 9 derrubadas pelo
  teste correspondente e 1 (aplicar as propriedades no recognizer) sem teste
  de unidade — exige o SDK conectado; vale o teste manual.
- Foto da faixa real transbordando: fim visível, topo esmaecido.
