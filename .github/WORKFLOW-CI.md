# CI ainda não está ativo neste repositório

O arquivo `.github/workflows/ci.yml` está pronto (o conteúdo está na revisão
`chore/review-improvements`), mas ainda não pôde ser enviado: nem o token do
Git Credential Manager nem o OAuth do plugin Kimi têm o escopo `workflow`, e
o GitHub **recusa criar workflows sem ele** — o push até aceita, mas o arquivo
simplesmente não aparece no repositório (comportamento confirmado ao tentar).

Para ativar (uma vez, por você — precisa de interação de OAuth que o agente
não consegue fazer):

```bash
gh auth refresh -h github.com -s workflow
```

Depois:

```bash
git checkout chore/review-improvements
git push origin chore/review-improvements   # o ci.yml entra no mesmo commit/PR
```

O workflow roda ruff + pytest em Windows nas versões 3.12, 3.13 e 3.14 do
Python, e publica a pasta do executável como artefato a cada push na main.

