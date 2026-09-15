# CI não está ativo neste repositório ainda

O arquivo `.github/workflows/ci.yml` existe no repositório de origem mas não
foi enviado no primeiro push: o token do GitHub CLI usado na publicação não
tinha o escopo `workflow`, e o GitHub recusa criar workflows sem ele.

Para ativar (uma vez):

```bash
gh auth refresh -h github.com -s workflow
```

Depois copie o `ci.yml` e faça push normalmente. O workflow roda os 142 testes
em Windows nas versões 3.12, 3.13 e 3.14 do Python, e publica a pasta do
executável como artefato a cada push na main.
