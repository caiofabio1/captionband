# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] - 2026-09-22

> Nota honesta sobre este arquivo: as versões 0.7.0 e 0.8.0 foram construídas
> localmente e **nunca anotadas aqui nem publicadas** (a última tag é v0.6.1).
> Esta seção cobre o que mudou desde a v0.6.1 e está no instalador; não é uma
> reconstrução retroativa daquelas duas.

### Added — vocabulário do evento (phrase list da Azure)

- Lista de termos que o reconhecimento costuma errar — nomes próprios, siglas,
  termos da área — enviada à Azure como *phrase list*. **Corrige o que é
  OUVIDO**, não como o termo é traduzido, que é a metade que mais importa:
  palavra mal ouvida gera tradução ruim de qualquer jeito.
- Arquivo de texto em `%LOCALAPPDATA%`, um termo por linha, editável no
  Bloco de Notas entre sessões. Botão em Configurações → Idiomas.
- **Pacote de saúde** com 258 termos, montado a partir da taxonomia real dos
  Mapas de Evidência CABSIN/BIREME. Acrescenta só o que falta: clicar duas
  vezes não duplica, e os termos do operador nunca são sobrescritos.
- `medir_vocabulario.py`: roda o mesmo WAV com e sem o vocabulário pelo
  provedor REAL e conta acertos por termo. Existe porque a documentação da
  Azure **não diz** se a phrase list funciona junto com identificação
  automática de idioma — nenhuma das duas páginas menciona a outra. O script
  também reporta o idioma escolhido por fala, que é o risco não documentado.

### Added — microfone da sala somado ao áudio do sistema

Numa conferência o loopback traz quem fala do OUTRO lado; nenhuma ferramenta
devolve a sua própria voz para os seus alto-falantes. Quem falava NA SALA
simplesmente não era legendado.

- **Configurações → Áudio → "Capturar também o microfone"**, com escolha de
  dispositivo e ganho (microfone de sala chega mais baixo que o áudio da
  chamada, que já vem normalizado). Desligado por padrão.
- **Um reconhecedor, não dois:** as duas fontes são somadas antes de ir ao
  provedor. Um segundo reconhecedor dobraria o custo por minuto, dobraria a
  identificação de idioma e exigiria costurar duas legendas numa banda só.
- **Deriva de relógio tratada, porque foi medida:** nesta máquina o loopback
  entrega 16041 Hz e o microfone 16002 Hz — relógios físicos diferentes,
  0,25%, ~9 s de descompasso por hora. O loopback dá o ritmo (ele entrega
  blocos mesmo em silêncio), a fila do microfone tem teto de 300 ms e um
  colchão de 100 ms absorve o engasgo do agendador.
- **Microfone que cai não derruba a legenda:** vai para a saúde (bandeja
  âmbar) em vez do caminho de reabertura de captura. Perder a voz da sala é
  ruim; perder a legenda inteira por causa dela é pior.
- **O que já existia continua sendo checado:** "Testar captura" e o
  `preflight` abrem a captura SOMADA quando a opção está ligada e reprovam se
  o microfone não subir — um teste que abrisse só o loopback aprovaria o áudio
  do evento sem nunca tocar no dispositivo de onde vem metade da fala.
- **Sem cancelamento de eco, e dito em voz alta:** com o som em alto-falante o
  microfone devolve o que o loopback já capturou e a legenda duplica. A tela e
  o README pedem fone de ouvido.

## [0.6.1] - 2026-09-16

### Fixed — revisão de código em 3 frentes (2026-09-16)

Revisão independente do pipeline, da UI/persistência e da esteira de release.
189 testes (143 anteriores + 46 novos), ruff limpo.

- **Race na troca de provider podia deixar sessão órfã cobrando e reverter a
  config.** O sinal `_swap_result` não carregava geração: swap de idioma em
  voo + Parar + Iniciar instalava o provider velho por cima do novo. Agora
  todo swap carrega um token de geração, incrementado também no `stop()`, e
  resultados defasados são descartados (com o provider tardio parado).
- **Fallback e stall-recovery não congelam mais a GUI.** Os dois caminhos de
  recuperação automática faziam swap síncrono na thread da GUI (1,3–1,6 s de
  I/O bloqueante, medido); agora usam o caminho assíncrono, e falha de swap
  tem retry com backoff 2/5/10 s em vez de depender do watchdog de 45 s.
- **`openai_realtime` não podia ser salvo pela UI.** `is_valid()` não tinha
  branch para o provider e o Save rejeitava com mensagem enganosa. Há teste
  que itera `PROVIDER_LABELS` exigindo comportamento definido por provider.
- **Falha no Credential Manager não apaga mais a chave.** O campo só sai do
  `config.json` se a escrita no keyring confirmar; em falha, `log.critical`
  e o valor é preservado. `load_config` valida formato (JSON não-objeto,
  sub-dicts inválidos) com quarentena, e faz coerção por campo com fallback
  individual — um campo podre não derruba os demais nem o boot.
- **Overlay reage a hot-plug de monitor.** `screenAdded`/`screenRemoved`/
  `virtualGeometryChanged` disparam reposicionamento com `log.warning`
  quando a tela da legenda some; o combo de telas das Configurações é
  re-populado a cada abertura da janela (projetor plugado com o dialog
  aberto agora aparece).
- **Google não declara mais `ordered_by_protocol=True`.** O STT é ordenado,
  mas o final traduzido saía de um pool com ≥2 workers e podia completar
  fora de ordem com o gate do app desligado. Agora o seq é atribuído no
  STT-final e o ReorderGate do app ordena — mesmo contrato dos demais
  providers em bloco.
- **OpenRouter não falha mais em silêncio.** Erros de STT (HTTP ≠ 200,
  exceções) chamam `report_exception`; 401/403 viram FATAL/auth no tray em
  vez de "sala silenciosa" com tray verde.
- **Backpressure nos providers em bloco (Groq, Whisper local, Cerebras,
  OpenRouter).** Fila limitada com drop-oldest (liberando o slot do reorder
  gate) e aviso DEGRADED throttled — sem mais latência crescente nem
  centenas de MB de PCM acumulado em eventos longos. Pool de tradução
  multi-alvo passa a ser persistente (um executor por utterance antes).
- **Resample 16→24 kHz com estado (OpenAI Realtime).** O filtro era
  redesenhado e aplicado bloco a bloco, gerando cliques de borda; agora um
  resampler com carry vê sinal contínuo. `audio_emitted_at_ms` é carimbado
  na chegada do áudio (a latência exibida não lê mais ~0).
- **Azure `push_audio` lê a push stream sob lock dedicado** (ordem de locks
  documentada; a thread de captura nunca espera o lock do reconnect).
- **`GOOGLE_APPLICATION_CREDENTIALS` restaurada no `stop()`** — a mutação
  process-wide não vaza mais para fora do provider.
- **`usage_tracker` cobre `openai_realtime`** (US$ 2,04/h por idioma-alvo) —
  o provider mais caro do catálogo aparecia como custo zero.
- **Esteira de release:** CI roda `rehearsal.py`, matrix alinhada a
  `requires-python` (3.11–3.13), timeouts nos jobs, ruff pinado igual ao
  pre-commit (`requirements-dev.txt`), build com lock
  (`requirements-build.lock`), CI compila o instalador Inno e smoke-testa o
  exe com o novo flag `--version`; `translator.spec` usa
  `collect_submodules('providers')` e `SPECPATH`; teste novo falha se
  `APP_VERSION`, `installer.iss` e `pyproject.toml` divergirem;
  `requirements.txt` inclui `keyboard` e `requests` e dropa `python-dotenv`
  (era dependência morta, com `.env.example` enganoso — removido).

### Fixed — revisão adversarial completa (2026-09-15, tarde)

Diagnóstico a partir do `app.log` do operador (8.994 linhas, 154 lançamentos),
do `crash.log` (12 lançamentos) e de experimentos de controle. Painel de 3
famílias de modelo (Gemini 3.1 Pro, Kimi K3-256k, GPT-OSS-120B) levantou
hipóteses; **cada uma foi adjudicada contra fonte primária ou medição**, e em
três pontos a medição contrariou o painel (ver "Não fizemos" abaixo).

- **A thread da GUI rodava no apartamento COM errado (MTA) em TODO lançamento.**
  `audio_capture` importa `soundcard`, que chama `CoInitializeEx(MULTITHREADED)`
  no import, na thread que importa — antes de existir `QApplication`. Resultado
  medido nos 154 lançamentos: `QWindowsContext: OleInitialize() failed: COM
  error 0x80010106`. Uma thread GUI do Qt no Windows deve ser STA.
  A tentativa anterior (revertida) falhou por resolver só metade. Medido agora:

  | | ordem | apartamento da GUI | captura loopback |
  |---|---|---|---|
  | A | atual | MTA | ok |
  | B | só STA no main | MAIN-STA | **morre, `0x800401F0 CO_E_NOTINITIALIZED`** |
  | C | STA no main + MTA na thread de captura | MAIN-STA | **ok, 1600 frames** |

  `soundcard` conta com o processo inteiro em MTA para que qualquer thread use
  COM sem inicializar; com o main em STA o `com_loaded` dele vira `False` e a
  thread de captura fica sem COM nenhum. `TLT_COM_APARTMENT=mta` restaura o
  comportamento antigo para comparar em campo.

- **Provider Azure continuava entregando resultado depois do `stop()`.** Os
  handlers não checavam flag de parada e o `stop()` nunca desconectava os
  sinais. Sintoma no log: 207 callbacks `canceled` em **um** segundo no
  encerramento. Agora há guard, `disconnect_all()` nos cinco sinais antes da
  parada bloqueante, e o worker de reconexão re-checa as flags **dentro** do
  lock — checar fora deixava ele iniciar um recognizer que ninguém mais
  pararia. A reconexão também desconecta `recognizing`/`recognized` do
  recognizer substituído, que continuavam ligados e faziam duas sessões
  escreverem na mesma banda.

- **Idiomas misturados na tela: at-start LID no lugar do contínuo.** A Azure
  documenta que o serviço *"returns one of the candidate languages provided
  even if those languages weren't in the audio"*. Medido, mesma fala em
  português, hipóteses concorrentes com milissegundos de diferença:
  `'bom dia a todos'` (pt-BR) intercalado com `'bongiatos'` (en-US). At-start
  LID decide uma vez e segura pela sessão. Acima de 4 candidatos volta ao
  contínuo, que é o limite documentado do at-start.

- **Legenda que se reescrevia sozinha:** passamos a pedir
  `SpeechServiceResponse_StablePartialResultThreshold`, que é a resposta
  documentada da Microsoft para *"the 'flickering' or changing text"*. A
  tradução projetada estava sendo reescrita inteira a cada parcial.

- **Swap de provider x `stop()`:** um swap que terminava depois do "Parar"
  instalava um provider **vivo** num controller parado — que nunca mais era
  parado, seguia faturando, e fazia a bandeja anunciar "rodando" num app
  parado. `stop()` também passou a limpar `_swap_in_progress` (um worker
  perdido desabilitava troca de idioma e o watchdog pelo resto da sessão) e
  `_last_fallback_at`.

- **Saúde volta a OK** depois de swap/recuperação bem-sucedida. Antes só
  `start()` e a recuperação de captura restauravam, então uma única falha
  deixava a bandeja âmbar pelo resto do evento.

- **Timer de reabertura de captura carrega o número da sessão**, então um
  timer de uma sessão morta não derruba mais a captura recém-criada.

- **Configuração deixou de se perder em silêncio:** `save_config` escreve em
  `.tmp` + `os.replace`; um `config.json` ilegível é preservado como
  `config.corrupt-<data>.json` e registrado, em vez de virar default sem
  avisar; sem keyring, o app avisa que as chaves vão para o disco em texto
  claro.

- **Uma instância só** (mutex nomeado): duas cópias abriam o mesmo loopback,
  as duas registravam o F9 e as duas escreviam na mesma pasta de transcrições.

- **Encerramento ordenado:** `quit()` fecha Configurações e as bandas antes de
  `app.quit()`, em vez de deixar o coletor do Python destruir widgets Qt
  depois do `QApplication`.

- **"Testar captura"** (o mesmo código que causou o abort confirmado) não
  desabilitava o botão: cada clique extra abria outro stream WASAPI e orfanava
  o anterior. Fechar o diálogo dentro dos 3 s deixava o stream gravando.

- **Encolhimento da legenda estava desligado na prática.** `MIN_FIT_PT` era 26
  absoluto e a condição do laço é `size - passo >= piso`; com a fonte 25 do
  operador a passada de encolhimento confortável **nunca** rodava e a banda ia
  direto descartar histórico — o oposto do desenho. O piso agora é relativo
  (0,72 do tamanho escolhido) e o passo é 2 pt.

- `threading.excepthook` instalado: as cinco threads worker perdiam exceção em
  silêncio, porque no exe sem console o `stderr` não existe.

### Medições que dispensaram trabalho planejado

- **Pintura não é o gargalo.** `paintEvent` cronometrado com a config real do
  operador (fonte 25, duas caixas, EN+ES): **mediana 3,32 ms**, p90 4,07 ms,
  máx 4,40 ms — contra 33 ms de orçamento a 30 fps. O cache de quebra de linha
  e o render em `QPixmap` que estavam planejados **não foram feitos**.
- **`0x8001010D` no `crash.log` não é morte.** É exceção de primeira chance que
  o `faulthandler` reporta e o COM trata: o lançamento que a registrou seguiu
  até `app quit normally`. Não perseguir como crash.
- **O `QMenu` da bandeja sem parent é seguro**: `setContextMenu()` mantém a
  referência (testado com `del` + `gc.collect()`).

### Não fizemos, de propósito

- **Não trocamos `soundcard`.** O painel recomendou `sounddevice`/PortAudio em
  2 de 3 famílias; verificado no artefato que é de fato empacotado
  (`.venv-build`: `sounddevice 0.5.6`, PortAudio 19.7.0): `WasapiSettings`
  **não tem** o argumento `loopback`. Sem loopback não há captura.
  `PyAudioWPatch` tem wheel cp314 e fica como plano B.
- **Não tiramos `stop_continuous_recognition()` de dentro do lock.** Os três
  painelistas apontaram essa como a mudança com maior chance de introduzir
  deadlock novo, e nenhum mock reproduz o timing. A invariante ("`_stopping`
  antes da chamada bloqueante") ficou documentada e travada por teste.

### Added
- `tests/test_stability_fixes.py` (24 testes), com verificação por mutação nos
  três principais. Suite total: 137.

### Fixed (revisão de estabilidade/UX, 2026-09-15)
- Perda de captura de áudio (fone desplugado, projetor replugado, Windows trocou a saída padrão) agora **reabre o dispositivo** com backoff 2/5/10 s antes de desistir. Antes: ícone vermelho com o pipeline ainda "rodando", e "Iniciar" era um no-op — a única saída era fechar o app.
- Fallback esgotado agora **para o pipeline de fato**, deixando "Iniciar" funcional.
- Fallback de provedor troca **só o provedor**; captura e arquivo de transcrição sobrevivem, e a bandeja não pisca parado→rodando no meio da falha.
- Eventos e status de um provedor **substituído** (fallback, idioma fixado, watchdog) são ignorados — um FATAL atrasado do provedor antigo disparava fallback espúrio, e seqs velhas envenenavam o portão de ordem novo.
- "Salvar" nas Configurações **zerava `fallback_providers`** e outros campos sem widget; agora parte do config atual.
- `original + tradução` mostrava apenas o **primeiro** idioma-alvo — com EN+ES o espanhol nunca aparecia.
- Watchdog de captura cego para captura que **nunca** abre o dispositivo (`seconds_since_audio()` devolvia 0,0 para sempre).
- Parar manualmente zera a cadeia de fallback ("queimada" ficava assim o dia todo).
- Reconexão Azure desconecta os handlers do reconhecedor antigo antes de derrubá-lo (evita reconexão em cascata) e fecha o stream antigo.

### Added
- **Tela da legenda** (bandeja): escolher em qual monitor a legenda aparece — o projetor em modo "estender" é o caso do evento. Persistido.
- **Provedor de reserva** na aba Provedor das Configurações; a checagem pré-evento avisa quando não há nenhum.
- Um `.srt` **por idioma-alvo** (`sessao.srt` + `sessao.es.srt`), gravados a cada 20 legendas — uma queda de energia no minuto 110 não perde mais o arquivo de legenda.
- Modo evento esconde o botão × e o badge de latência da banda projetada; banda reserva uma linha extra por idioma-alvo; texto mais antigo cede quando não cabe (a linha nova nunca sai da banda).
- Mudanças só de aparência (fonte, cor, posição, modo) aplicam **sem reiniciar** o pipeline.
- Balões de "failing" limitados a um por 30 s (a contagem regressiva de reconexão gerava um por segundo); FATAL sempre aparece.
- Falha ao abrir a transcrição vira status "instável" na bandeja em vez de só log.
- Testes: `tests/test_controller_recovery.py` (6) e `tests/test_review_ux.py` (10); caso E2E 5 passou a exigir a reabertura da captura.

### Fixed (após teste do exe instalado, 2026-09-15)
- **Modo auto-detectar agora mostra parciais palavra a palavra.** O código dizia que o LID contínuo "só emite finais" e não conectava `recognizing`; medido contra a API real: 8 parciais com tradução. Era a causa das "legendas extremamente lentas" no modo padrão.
- **Trocar idioma não bloqueia mais a interface**: a parada/reabertura do reconhecedor Azure (1,3–1,6 s de rede, medido no log) saiu da thread da GUI para um worker. E2E caso 6: GUI bloqueada 0,001 s (antes 1,29 s). Status mostra "trocando idioma…" imediatamente.
- **Idioma fixado errado é explicado**: com áudio entrando e nada reconhecido por 12 s no modo fixado, a bandeja avisa "o idioma falado é outro? Volte para Auto-detectar (F9)" — antes ficava mudo e parecia que a troca falhou.
- `Speech_SegmentationSilenceTimeoutMs=400` (orientação da Microsoft para palestrantes rápidos: 300) — frases fecham antes.
- Linhas duplicadas no `app.log` (handler no logger e na raiz).
- `e2e_installed.py`: instala o Setup, abre o exe instalado, toca fala real, aperta F9 real e lê o log. A rodada anterior só verificou que o exe abria.

### Fixed — o app "fechava sozinho" (2026-09-15)
- **Causa encontrada (`crash.log`: `Fatal Python error: Aborted`, thread principal em `exec()`, nenhum frame Python):** o medidor de "Testar captura" (aba Áudio) atualizava a `QProgressBar` **direto da thread de captura** (~60 mutações por clique). O Qt não levanta exceção para isso: corrompe estado interno e aborta depois, em qualquer lugar — inclusive no meio da tradução. Agora o nível atravessa por sinal (fila da GUI). Painel de 3 modelos apontou; confirmado na linha 989; **reproduzido a partir do fonte**: `QProgressBar.setValue()` de uma thread secundária por 20 s → `Windows fatal exception: access violation` (numa rodada engolida por handler nativo, na outra o processo morreu — o mesmo comportamento intermitente visto em campo). Travado por teste comportamental + guarda estática.
- Mensagens do Qt (inclusive `qFatal`) passam pelo `app.log`; cliques de menu, abas, botões e combos deixam rastro ("ui: …") para a próxima investigação.

### Changed — × da legenda esconde, não fecha o app (2026-09-15)
- O × na banda passava a perguntar "fechar o app?"; um clique errado na projeção estava a um "Sim" de derrubar a sessão. Agora esconde a legenda (tradução e transcrição continuam) e avisa na bandeja; sair é só Bandeja → Sair.
- `crash.log` (faulthandler) na pasta do app para falhas nativas; teste do instalado não mata mais por nome de imagem (levou a instância do operador junto uma vez).

### Changed — Configurações passa a ser a superfície completa (2026-09-15)
- Aba própria **"Layout / Projeção"**: empilhado vs duas caixas, posição inicial da 2ª caixa, tela da legenda, banda fixa e sua altura. Antes: duas caixas e tela só na bandeja; posição da 2ª caixa e banda fixa em lugar nenhum. A bandeja continua como atalho e os dois ficam sincronizados.
- "Como mostrar" ganhou explicação: o QUE aparece por frase fica em Idiomas; ONDE cada idioma aparece fica em Aparência.
- "Visualizar legenda" mostra **as duas caixas** quando o layout é de duas caixas.

### Added — legenda bilíngue em duas caixas (pedido ao vivo, 2026-09-15)
- Bandeja → **🗂 Bilíngue em duas caixas**: o 1º idioma de saída fica na banda de sempre (rodapé) e o 2º numa segunda caixa no topo; cada uma se arrasta para onde quiser — topo/rodapé, lado a lado, ou em telas diferentes com "Reposicionar". Persistido; o provedor continua traduzindo para os dois. Modo evento, mostrar/esconder/reposicionar e tela valem para as duas.

### Fixed — "Visualizar legenda" abria janelas que nunca fechavam (2026-09-15)
- Cada clique criava uma banda nova e nada as fechava (nem o × delas). Agora é **uma** banda, reaproveitada, que se fecha sozinha em 8 s, ao fechar Configurações e ao Salvar; mostra os idiomas de saída configurados em vez de um exemplo fixo em espanhol.
- A legenda ao vivo **esconde a banda** quando fica vazia (20 s sem fala) em vez de deixar uma tarja preta sobre os slides; a próxima fala a traz de volta.
- `clear()` também zera a memória de duplicatas (o preview descartava a própria amostra no 2º clique).

### Fixed (screenshot ao vivo: a mesma frase empilhada em 3 versões, 2026-09-15)
- **A substituição "no lugar" das parciais nunca funcionou com Azure real.** Ela dependia de `result_id` igual entre parcial e final; medido: cada evento tem id próprio (6 ids em 6 eventos de uma frase, nos dois modos). Cada parcial virava uma linha nova. Regra nova, sem id: enquanto a última frase está aberta (parcial), qualquer evento a continua; só depois do final nasce outra linha.
- Tradução parcial só é substituída por uma **igual ou mais longa** (o final sempre vence) — acaba o "That/This customer service" piscando a cada meio segundo.
- Repintura só quando algo mudou (o Azure repete a mesma parcial 10× enquanto o palestrante não pausa).

### Fixed (relato ao vivo: "menu não abre, não consigo editar nada", 2026-09-15)
- **Menu da bandeja esvaziado pelo garbage collector.** `QAction` criada sem pai Qt e guardada só numa variável local é destruída e sai do menu (medido: `QMenu().addAction(QAction("x")); gc.collect()` → 0 ações). No exe congelado isso apagou "Configurações", "Sair" e os dois submenus dinâmicos. Todas as ações agora têm pai; teste varre o fonte e falha se alguma nascer órfã.
- **GUI saturada pela pintura**: contorno de texto por `QPainterPath` custava 82,5 ms por quadro com 9 linhas (medido com a config do operador), repintado a cada parcial e a 30 fps na animação. Agora contorno por `drawText` deslocado (16,9 ms) e repintura coalescida a 15 fps.
- **Idiomas alternando ("hora espanhol, hora inglês")**: com "falado + EN + ES" a regra de estouro descartava linhas da **frase atual**. Agora só descarta histórico; a frase atual nunca é cortada (encolhe até 16 pt se preciso). A banda fixa passa a ser dimensionada pelo modo de exibição (mínimo: uma frase completa com quebra), não pelas 3 linhas fixas.

### Legenda bilíngue para projeção (2026-09-15)
- **Modo evento com 2 idiomas de saída** passa sozinho para o layout bilíngue: só as traduções (sem o idioma falado), só a frase atual, uma faixa por idioma na ordem configurada.
- No modo bilíngue os dois idiomas têm **o mesmo tamanho e brilho**; a barra de cor passa a indicar o idioma-**alvo** (EN verde, ES laranja) e fica mais larga na projeção. Antes o segundo idioma saía menor e apagado.
- Banda fixa: quando o texto não cabe, **a fonte encolhe** (até 26 pt) antes de descartar linhas — descartar removia um idioma inteiro da frase atual (visto em render offscreen: só o espanhol sobreviveu).
- Nomes dos modos de exibição em linguagem clara ("Bilíngue: só as traduções, sem o idioma falado (ex.: EN + ES)").

### Empacotamento (0.6.0)
- Build **one-dir** (antes one-file: centenas de MB extraídos para `%TEMP%` a cada abertura) a partir de um venv dedicado com `requirements-build.txt`. Construir do Python global fazia o PyInstaller varrer a máquina inteira (sympy, stanza apareceram no log).
- Instalador Inno Setup por usuário (sem admin), 46 MB; app instalado 155 MB. Whisper local/Argos e Google **não** vão no instalador — ficam como extras via `pip`; o app avisa "pacote não instalado" se selecionados.
- `build.bat` faz tudo: venv → PyInstaller → ISCC → `Output\TeamsLiveTranslationSetup.exe`.

### Added (earlier)
- LICENSE file (MIT).
- `CHANGELOG.md`.
- `constants.py` extracting magic numbers (display time, idle timeout, animation duration, dedup window, audio buffer parameters).
- `pyproject.toml` with ruff lint configuration.
- Architecture diagram (Mermaid) in README.
- Test suite under `tests/` covering audio buffer, provider factory, overlay dedup, transcript writer, config round-trip.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`).
- Secret management via `keyring` library — API keys are now stored in Windows Credential Manager instead of plain JSON.
- Provider fallback chain — when the primary provider fails repeatedly, the controller automatically retries with the next configured provider and notifies the user via system tray.
- "Testar conexão" button per provider in Settings — validates credentials before starting a webinar instead of failing at runtime.
- Transcript retention policy — old session files are pruned automatically (default: keep last 50 sessions, delete files older than 30 days).
- Update checker — startup checks GitHub releases and notifies if a newer version is available.
- Cost/quota dashboard in About tab — shows minutes used per provider for the current month plus rate-limit headroom.

### Changed
- Inno Setup architecture identifier from deprecated `x64` to `x64compatible`.
- Standardized internal docstrings to English (UI strings remain Portuguese).

## [0.1.0] - 2026-04-30

### Added
- Initial release.
- Four translation providers: Azure Speech Translation (Continuous LID), Google Speech v2 + Translate, Groq Whisper turbo + Llama, Whisper local (faster-whisper + Argos).
- WASAPI loopback audio capture via `soundcard` (no virtual audio cable required).
- Transparent always-on-top caption overlay with 3-line rolling history, auto-concat, fade+slide animations, language color coding, latency badge.
- Presentation Mode toggle (1-click big-font captions at top of screen).
- System tray with status indicator (gray / green / red).
- Settings GUI with 5 tabs (Provider / Languages / Audio / Appearance / About).
- Audio level meter + 3-second test capture button in Settings → Audio.
- Auto-saved transcripts (`.txt` + `.srt`) per session.
- PyInstaller single-file executable + Inno Setup installer with PT-BR/EN UI.
