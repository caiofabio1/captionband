r"""O instalador tem de remover a instalação do nome antigo.

Aconteceu com o operador em 2026-09-18. O app foi renomeado de
TeamsLiveTranslation para CaptionBand mantendo o `AppId` (de propósito: é ele
que dá continuidade à entrada de desinstalação), mas `DefaultDirName` e
`DefaultGroupName` mudaram. O Inno chaveia a entrada de desinstalação pelo
AppId, então a instalação nova sobrescreveu a entrada da antiga apontando para
a pasta nova — e a antiga ficou órfã:

  - 159 MB em %LOCALAPPDATA%\Programs\TeamsLiveTranslation
  - um unins000.exe que não aparece mais em "Aplicativos instalados"
  - um atalho de menu Iniciar TAMBÉM chamado "CaptionBand", no grupo
    "Teams Live Translation"

Ele abriu o atalho errado e usou por um tempo uma versão de dois dias antes,
digitando credenciais nela — sem nada na tela que distinguisse as duas. Este
teste falha se a limpeza sair do installer.iss.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ISS = (pathlib.Path(__file__).resolve().parent.parent / "installer.iss").read_text(
    encoding="utf-8")


def _secao(nome: str) -> str:
    m = re.search(rf"^\[{nome}\](.*?)(?=^\[|\Z)", ISS, re.MULTILINE | re.DOTALL)
    assert m, f"secao [{nome}] nao existe no installer.iss"
    return m.group(1)


class TestOInstaladorLimpaONomeAntigo:
    @pytest.mark.parametrize("alvo", (
        r"{autopf}\TeamsLiveTranslation",
        r"{userprograms}\Teams Live Translation",
    ))
    def test_a_pasta_e_o_grupo_antigos_saem(self, alvo):
        assert alvo in _secao("InstallDelete"), (
            f"{alvo} nao e removido: uma instalacao orfa fica no disco e o "
            f"atalho dela abre uma versao velha do app")

    def test_a_remocao_e_recursiva(self):
        for linha in _secao("InstallDelete").splitlines():
            if linha.lstrip().startswith(";"):
                continue                      # comentario, nao diretiva
            if "TeamsLiveTranslation" in linha or "Teams Live Translation" in linha:
                assert "filesandordirs" in linha, (
                    f"Type errado, a pasta nao seria apagada inteira: {linha.strip()}")


class TestOQueNaoPodeMudar:
    def test_o_appid_continua_o_mesmo(self):
        """Mudar o AppId cria uma SEGUNDA entrada de desinstalacao e a versao
        instalada deixa de ser substituida -- o oposto do que se quer."""
        assert "CABSINTLT0001" in ISS

    def test_o_desinstalador_antigo_nao_e_chamado(self):
        """Mesmo AppId: rodar o unins000.exe antigo apagaria a entrada de
        desinstalacao da versao ATUAL. A remocao tem de ser direta."""
        diretivas = _secao("Run") + ISS[ISS.find("[UninstallRun]"):]             if "[UninstallRun]" in ISS else _secao("Run")
        assert "unins000" not in diretivas

    def test_o_nome_e_o_grupo_atuais_estao_declarados(self):
        assert 'DefaultGroupName=CaptionBand' in ISS
        assert "DefaultDirName={autopf}" + chr(92) + "CaptionBand" in ISS
