# Auditoria das telas de Configurações

Levantado em 2026-09-18 por leitura do código **e** por instrumentação: um
script abre a janela real, percorre a árvore de widgets de cada aba e cruza
com os campos que `_build_config()` de fato escreve. Onde há número aqui, ele
foi medido, não estimado.

Duas correções desta auditoria já estão no código (`f31a081`, `802200b`). O
resto está proposto no fim, para decisão.

---

## 1. O mapa: 7 abas, 37 controles

| Aba | Do que ela decide | Campos |
|---|---|---|
| **Credenciais** | chaves de API | `azure_speech_key`, `google_credentials_json`, `openai_api_key`, `openrouter_api_key` |
| **Provedor** | quem transcreve, com que modelo, e o reserva | `provider`, `fallback_providers`, `chunk_seconds`, `auto_start_translation`, `azure_streaming_*`, `azure_quick_languages`, `azure_switch_hotkey`, `whisper_*`, `google_*`, `openrouter_*` |
| **Idiomas** | de quais idiomas, para quais, e **o que aparece em cada fala** | `source_languages`, `target_languages`, `display_mode` |
| **Áudio** | de onde vem o som | `audio.device_name` |
| **Aparência** | como a banda se parece | `background_opacity`, `primary/secondary_font_size`, 4 cores, `font_family`, `padding`, `width_ratio`, `max_history`, `concat_gap_ms`, `click_through` |
| **Layout / Projeção** | onde a banda fica e quantas são | `position`, `second_position`, `split_languages`, `screen_name`, `stable_height`, `reserved_lines` |
| **Sobre** | versão, custo, atualização | — |

---

## 2. A redundância central: uma decisão, duas abas ✅ corrigido

`position` estava em **Aparência**. `second_position` em **Layout /
Projeção**. São as duas metades da mesma decisão.

**Isso não era só desarrumação — era a causa de um defeito.** A janela
oferecia as nove combinações de (posição da legenda, posição da 2ª caixa) e
**as três da diagonal sobrepunham as bandas em 100%**:

```
        2a=topo    2a=centro  2a=inferior
1a=topo    100%         0%         0%
1a=centro    0%       100%         0%
1a=inferior  0%         0%       100%
```

Duas janelas em geometria idêntica não leem como "duas caixas no mesmo
lugar"; leem como **uma caixa com os idiomas empilhados** — que é exatamente
o que a outra opção do mesmo combo produz de propósito. Foi o defeito
reportado como "configuro as 2 caixas e só aparece 1 e empilhada".

Ninguém viu isso por anos porque **nunca era possível olhar as duas escolhas
na mesma tela**.

Corrigido: as duas posições moram juntas na aba Layout / Projeção; a lista da
2ª caixa não oferece a posição já ocupada pela 1ª; e o guard de verdade está
em `split_overlay_configs()`, o único ponto por onde o preview, o runtime e um
`config.json` editado à mão passam.

O rótulo também mentia: em duas caixas, "Posição da legenda" posiciona a
**1ª caixa**, não "a legenda". Agora ele troca de texto conforme o modo.

---

## 3. A altura da banda é decidida em três abas

Quatro campos determinam a altura da banda, e estão espalhados:

| Campo | Aba | Papel na altura |
|---|---|---|
| `display_mode` | **Idiomas** | quantas linhas cada fala ocupa |
| `max_history` | **Aparência** | quantas falas anteriores aparecem |
| `reserved_lines` | **Layout / Projeção** | piso de altura |
| `stable_height` | **Layout / Projeção** | liga/desliga a altura fixa |

A própria aba Layout já carrega uma frase pedindo desculpa pela divisão: *"O
que aparece em cada frase … fica em Idiomas → Como mostrar"*. Um texto de
ajuda que explica onde está o outro controle é o sintoma, não a solução.

Agravante medido: até `e56e03a`, `max_history` acima de 1 **não tinha efeito
nenhum**, porque a altura vinha só de `reserved_lines`. Dois controles em abas
diferentes disputando o mesmo número, e o de Aparência perdia em silêncio.
Hoje `max_history` manda e `reserved_lines` é piso — **mas a tela não diz
isso.**

---

## 4. Configuração morta e configuração sem controle

Medido campo a campo contra `_build_config()` e contra quem lê no app:

| Campo | UI | Quem lê | Veredito |
|---|---|---|---|
| `overlay.fade_ms` | — | **ninguém** | 🔴 morto: apagar |
| `overlay.streaming_partials` | — | **ninguém** | 🔴 morto: apagar |
| `overlay.max_chars` | — | `overlay_qt.py` | 🟡 vivo, sem controle |

> 🔴 **Correção a esta auditoria.** A primeira versão dizia que `max_chars`
> "limita caracteres por linha" e recomendava aproximá-lo dos 37 da BBC. Está
> errado, e a proposta E saiu daí. `_wrap_lines()` não recebe `max_chars`
> nenhum — a quebra de linha vem da largura da banda com o tamanho da fonte.
> O que `max_chars` faz, medido: **corta o começo da fala** e mostra o final
> com `…` na frente. Com `max_chars=60`, uma fala de 122 caracteres aparece
> como `…que e esta parte que voce esta lendo agora no fim da frase.` É uma
> válvula contra fala que não termina. O controle foi exposto com esse nome
> ("Cortar fala acima de"), não com o nome errado.
| `overlay.anchor_newest` | — | `overlay_qt.py`, `translator.py` | 🟡 vivo, sem controle |
| `audio.samplerate` | — | captura e todos os provedores | ⚪ interno, correto ficar fora |
| `audio.channels` | — | captura, Azure | ⚪ interno, correto ficar fora |

`fade_ms` e `streaming_partials` só existem em `config.py`, com valor padrão e
zero leitores. Estão no `config.json` do operador sugerindo que fazem algo.

---

## 5. Frontend × backend: onde não batem

**Índice de formulário escrito à mão em dois lugares** ✅ corrigido. A aba
Provedor montava as páginas numa ordem e um segundo dicionário mapeava
`provider → índice`. Dois lugares para a mesma verdade: no dia em que um
provedor sai da lista, a tela abre o formulário errado. Agora a página é
registrada pelo código do provedor, e um teste percorre todos conferindo.

**Cinco modelos de STT que não existiam** ✅ corrigido (`3f78b51`). O combo do
OpenRouter oferecia `whisper-1`, `whisper-large-v3`, `whisper-large-v3-turbo`,
`gpt-4o-transcribe` e `gpt-4o-mini-transcribe`. Nenhum dos cinco está no
catálogo do OpenRouter, e o "Testar conexão" passava, porque só conferia
autenticação.

**`openai_stt_model` ficou órfão** ✅ corrigido. O combo que o alimentava
vivia dentro do formulário do provedor `openai_cerebras`; removido o provedor,
`_build_config()` seguia lendo o widget inexistente e a janela quebrava ao
salvar.

**Bloqueio geográfico lido como chave errada** ✅ corrigido. O diálogo
despejava o JSON cru do Cloudflare.

---

## 6. Bandeja × Configurações: cinco controles em dois lugares

Duas caixas, tela da legenda, modo evento, idioma de origem e mostrar/ocultar
existem na bandeja **e** nas Configurações. Isso é **deliberado e correto** —
durante um evento a bandeja é o caminho rápido, e o código já trata os dois
como vistas de um mesmo estado (`_apply_config` é o único caminho por onde uma
mudança chega a todos, e `action_split.setChecked` sincroniza o menu).

Não mexer.

---

## 7. Decisões tomadas em 2026-09-18 — todas implementadas

**A + B ✅ "Layout / Projeção" virou "Legenda".** Recebeu `display_mode`
(de Idiomas), `max_history`, `concat_gap_ms` e `click_through` (de Aparência).
"Aparência" ficou só com cor, fonte, opacidade, largura e margem. As duas
frases de ajuda que apontavam de uma aba para a outra saíram — não havia mais
para onde apontar. Resultado medido: nenhum controle em duas abas, e os quatro
campos que decidem a altura na mesma tela.

`display_mode` abre a aba, porque é ele que define quantas linhas cada fala
ocupa: tudo abaixo depende disso.

**C ✅ "Altura da banda fixa" virou "Altura mínima da banda"**, com a dica
explicando que quem manda é o histórico.

**D ✅ `fade_ms` e `streaming_partials` apagados.**

**E ✅ `max_chars` exposto como "Cortar fala acima de"** — com o nome certo,
depois da correção acima. Aceita 0 = "nunca cortar". `anchor_newest` ficou
interno.

Travado por `tests/test_settings_organization.py` (16 testes, verificados por
mutação nos quatro caminhos: histórico de volta em Aparência, os dois rótulos
voltando a mentir, e `fade_ms` ressuscitando).

### Aba "Legenda", na ordem em que aparece

| Controle | Campo |
|---|---|
| Como mostrar | `display_mode` |
| Com 2 idiomas de saída | `split_languages` |
| Posição da legenda / da 1ª caixa | `position` |
| Posição da 2ª caixa | `second_position` |
| Tela da legenda | `screen_name` |
| Banda de altura fixa | `stable_height` |
| Altura mínima da banda | `reserved_lines` |
| Falas anteriores visíveis | `max_history` |
| Cortar fala acima de | `max_chars` |
| Juntar falas próximas | `concat_gap_ms` |
| Click-through | `click_through` |
