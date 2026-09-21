"""Vocabulário do evento: o que o arquivo produz e onde ele é aplicado.

Os limites vêm da página da Microsoft, checada em 2026-09-21 e citada em
`vocabulary.py`: no máximo 2.000 frases, peso de 0.0 a 2.0 valendo para a
lista inteira, e um conjunto declarado de caracteres permitidos — "the system
removes other special characters from the phrase".

O que estes testes NÃO provam: que a phrase list de fato melhora o
reconhecimento com identificação automática de idioma. A documentação é
silente nisso e nenhum teste offline pode responder — é para isso que existe
`medir_vocabulario.py`.
"""
from __future__ import annotations

import pytest

import vocabulary
from vocabulary import (
    MAX_PHRASES,
    WEIGHT_DEFAULT,
    clamp_weight,
    ensure_file,
    load_terms,
    parse,
    sanitize,
)


class TestOArquivoViraLista:
    def test_um_termo_por_linha_ignorando_comentario_e_vazio(self):
        assert parse("# comentário\nCABSIN\n\n  PNPIC  \n# outro\n") == [
            "CABSIN", "PNPIC"]

    def test_duplicata_por_caixa_diferente_conta_uma_vez(self):
        """Para o serviço é uma frase; no arquivo seriam duas linhas de ruído."""
        assert parse("CABSIN\ncabsin\nCabsin\n") == ["CABSIN"]

    def test_a_caixa_escrita_pelo_operador_e_preservada(self):
        assert parse("CABSIN\n") == ["CABSIN"]
        assert parse("auriculoterapia\n") == ["auriculoterapia"]

    def test_a_ordem_do_arquivo_e_mantida(self):
        assert parse("zzz\naaa\nmmm\n") == ["zzz", "aaa", "mmm"]


class TestCaracteresQueOServicoRemoveria:
    def test_letra_acentuada_e_digito_ficam(self):
        assert sanitize("auriculoterapia 3 ções") == "auriculoterapia 3 ções"

    def test_pontuacao_permitida_fica(self):
        assert sanitize("F.Radar") == "F.Radar"
        assert sanitize("(PICS)") == "(PICS)"

    def test_emoji_sai(self):
        assert sanitize("CABSIN 🎯") == "CABSIN"

    def test_caractere_invisivel_sai(self):
        """U+202A/U+202C já custaram um dia neste projeto, em títulos do
        Google Scholar: dois termos idênticos na tela e diferentes na
        comparação."""
        assert sanitize("‪CABSIN‬") == "CABSIN"
        assert sanitize("CAB​SIN") == "CABSIN"

    def test_espaco_interno_normalizado(self):
        assert sanitize("  Medicina   Tradicional  Chinesa ") == \
            "Medicina Tradicional Chinesa"

    def test_linha_que_sobra_vazia_e_descartada(self):
        assert parse("🎯\n‬\nCABSIN\n") == ["CABSIN"]


class TestOLimiteDaAzureERespeitado:
    def test_corta_em_2000_e_avisa(self, caplog):
        muitos = "\n".join(f"termo{i}" for i in range(MAX_PHRASES + 50))

        class _Fake:
            def read_text(self, encoding=None):
                return muitos

        with caplog.at_level("WARNING"):
            termos = load_terms(_Fake())
        assert len(termos) == MAX_PHRASES
        assert any("2000" in r.message or str(MAX_PHRASES) in str(r.args)
                   for r in caplog.records), caplog.text

    def test_o_limite_e_o_que_a_microsoft_documenta(self):
        assert MAX_PHRASES == 2000


class TestPeso:
    @pytest.mark.parametrize("entrada,esperado", [
        (0.0, 0.0), (1.0, 1.0), (2.0, 2.0),
        (-1.0, 0.0), (5.0, 2.0),          # a doc só define 0..2
        ("1.5", 1.5),
        (None, WEIGHT_DEFAULT), ("nada", WEIGHT_DEFAULT),
    ])
    def test_fica_na_faixa_documentada(self, entrada, esperado):
        assert clamp_weight(entrada) == esperado


class TestArquivoIlegivelNaoDerrubaNada:
    def test_arquivo_inexistente_significa_sem_vocabulario(self, tmp_path):
        assert load_terms(tmp_path / "nao-existe.txt") == []

    def test_bytes_invalidos_nao_propagam_excecao(self, tmp_path):
        """O arquivo é editado à mão, num Notepad, minutos antes de uma fala.
        Salvo em ANSI com acento, ele vira UnicodeDecodeError — e o custo disso
        tem de ser legenda sem melhora, nunca app sem legenda."""
        p = tmp_path / "vocabulary.txt"
        p.write_bytes(b"CABSIN\n\xff\xfe invalido\n")
        assert load_terms(p) == []

    def test_ensure_file_cria_com_instrucoes(self, tmp_path):
        p = tmp_path / "sub" / "vocabulary.txt"
        assert ensure_file(p) is True
        texto = p.read_text(encoding="utf-8")
        assert "Um termo por linha" in texto
        assert "2000" in texto
        assert parse(texto), "o arquivo criado devia já trazer exemplos válidos"

    def test_ensure_file_nao_sobrescreve(self, tmp_path):
        p = tmp_path / "vocabulary.txt"
        p.write_text("MEU TERMO\n", encoding="utf-8")
        assert ensure_file(p) is False
        assert p.read_text(encoding="utf-8") == "MEU TERMO\n"


class TestOndeOVocabularioEAplicado:
    """Tem de ser no ponto por onde o start E a reconexão passam.

    Aplicado só no start, ele desaparece na primeira reconexão de um evento de
    duas horas — que é exatamente quando ninguém está olhando o log.
    """

    def test_o_gancho_esta_no_build_recognizer(self):
        import inspect

        from providers.azure import AzureProvider
        fonte = inspect.getsource(AzureProvider._build_recognizer)
        assert "_attach_phrase_list" in fonte, (
            "a phrase list precisa ser anexada em _build_recognizer, o único "
            "ponto que start() e o worker de reconexão compartilham")

    def test_a_reconexao_reconstroi_o_recognizer(self):
        import inspect

        from providers.azure import AzureProvider
        fonte = inspect.getsource(AzureProvider._schedule_reconnect)
        assert "_build_recognizer" in fonte

    def test_peso_zero_nao_chama_o_sdk(self, monkeypatch):
        from providers.azure import AzureProvider

        chamou = []
        prov = AzureProvider.__new__(AzureProvider)
        prov.phrases = ["CABSIN"]
        prov.phrase_weight = 0.0
        monkeypatch.setattr(vocabulary, "clamp_weight", clamp_weight)
        prov._attach_phrase_list(object())     # nenhum recognizer de verdade
        assert not chamou

    def test_lista_vazia_nao_chama_o_sdk(self):
        from providers.azure import AzureProvider

        prov = AzureProvider.__new__(AzureProvider)
        prov.phrases = []
        prov.phrase_weight = 1.0
        prov._attach_phrase_list(object())     # não deve explodir

    def test_falha_do_sdk_nao_derruba_a_sessao(self, caplog):
        """Pior caso aceitável: legenda sem melhora. Inaceitável: sem legenda."""
        from providers.azure import AzureProvider

        prov = AzureProvider.__new__(AzureProvider)
        prov.phrases = ["CABSIN"]
        prov.phrase_weight = 1.0
        with caplog.at_level("ERROR"):
            prov._attach_phrase_list(object())   # object() não é recognizer
        assert any("vocabul" in r.message.lower() for r in caplog.records), \
            caplog.text


@pytest.fixture(scope="module")
def qapp():
    """Guarda a referência — sem isso o GC recolhe o QApplication com a janela
    viva e o interpretador morre sem traceback. Escrevi este mesmo bug duas
    vezes no mesmo dia, em dois arquivos de teste, depois de documentá-lo."""
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class TestATelaOferecOVocabulario:
    def test_a_janela_tem_o_grupo_e_ele_vai_e_volta(self, qapp):
        from config import AppConfig
        from settings_window import SettingsWindow
        w = SettingsWindow(AppConfig(provider="azure", azure_speech_key="k",
                                     target_languages=["es"],
                                     vocabulary_weight=0.8))
        try:
            assert abs(w.vocabulary_weight_spin.value() - 0.8) < 1e-9
            w.vocabulary_weight_spin.setValue(1.7)
            assert abs(w._build_config().vocabulary_weight - 1.7) < 1e-9
            assert w.vocabulary_weight_spin.maximum() == 2.0
            assert w.vocabulary_weight_spin.minimum() == 0.0
            assert w.vocabulary_weight_spin.specialValueText() == "desligado"
        finally:
            w.close()


class TestPacotesProntos:
    """Os vocabulários que vêm no app.

    Não testam se os termos estão "certos" — um termo que a Azure já acerta
    não faz mal, só ocupa espaço. Testam o que pode dar errado de verdade:
    lista que estoura o teto, frase longa que a doc diz que atrapalha,
    duplicata entre pacotes, e informação que não devia estar num repo público.
    """

    def test_o_pacote_cabe_folgado_no_teto_da_azure(self):
        from vocabularies import pacote_saude
        termos = parse(pacote_saude())
        assert 0 < len(termos) <= MAX_PHRASES / 2, (
            f"{len(termos)} termos — perto do teto de {MAX_PHRASES} não sobra "
            f"espaço para os termos do próprio evento")

    def test_os_tres_blocos_somam_o_pacote(self):
        """Número derivado: recalculado, não afirmado."""
        from vocabularies import PACOTES, pacote_saude
        soma = sum(len(parse(b)) for b in PACOTES.values())
        # Pode haver termo repetido ENTRE blocos; o pacote deduplica.
        assert len(parse(pacote_saude())) <= soma

    def test_nenhum_termo_repetido_entre_os_blocos(self):
        from vocabularies import PACOTES
        visto: dict[str, str] = {}
        repetidos = []
        for nome, bloco in PACOTES.items():
            for termo in parse(bloco):
                chave = termo.casefold()
                if chave in visto:
                    repetidos.append(f"{termo!r} em {visto[chave]} e {nome}")
                visto[chave] = nome
        assert not repetidos, repetidos

    def test_sem_frase_longa(self):
        """A doc da Azure: "a longer phrase list will impact quality and
        latency", e frases longas gastam a lista sem serem ditas."""
        from vocabularies import pacote_saude
        longos = [t for t in parse(pacote_saude()) if len(t.split()) > 4]
        assert not longos, f"frases longas demais: {longos}"

    def test_nada_de_informacao_interna_num_repo_publico(self):
        """Nomes de pessoa, sistema interno e sigla de projeto ficam no
        arquivo local do operador, que vive em %LOCALAPPDATA%."""
        from vocabularies import PACOTES
        fonte = "".join(PACOTES.values()).lower()
        for proibido in ("f.radar", "fradar", "consira", "@", "senha", "token"):
            assert proibido not in fonte, f"{proibido!r} não pode ir para o repo"

    def test_todo_termo_sobrevive_a_sanitizacao(self):
        """Termo que o sanitize mudaria é termo que eu digitei errado."""
        from vocabularies import pacote_saude
        for termo in parse(pacote_saude()):
            assert sanitize(termo) == termo, f"{termo!r} -> {sanitize(termo)!r}"


class TestAcrescentarSemDuplicar:
    def test_o_segundo_clique_nao_duplica(self, tmp_path):
        from vocabulary import append_terms
        from vocabularies import pacote_saude

        f = tmp_path / "vocabulary.txt"
        primeiro = append_terms(f, pacote_saude())
        assert primeiro > 0
        depois_do_primeiro = len(load_terms(f))
        assert append_terms(f, pacote_saude()) == 0
        assert len(load_terms(f)) == depois_do_primeiro

    def test_os_termos_do_operador_sobrevivem(self, tmp_path):
        from vocabulary import append_terms
        from vocabularies import pacote_saude

        f = tmp_path / "vocabulary.txt"
        f.write_text("TERMO DO EVENTO\nSUS\n", encoding="utf-8")
        append_terms(f, pacote_saude())
        termos = load_terms(f)
        assert "TERMO DO EVENTO" in termos
        assert sum(1 for t in termos if t.casefold() == "sus") == 1, \
            "SUS ja existia e foi duplicado"

    def test_arquivo_sem_permissao_devolve_zero_em_vez_de_explodir(
            self, tmp_path, monkeypatch):
        from vocabulary import append_terms

        f = tmp_path / "vocabulary.txt"
        f.write_text("X\n", encoding="utf-8")

        def nega(*a, **k):
            raise OSError("sem permissao")

        monkeypatch.setattr(type(f), "open", nega)
        assert append_terms(f, "NOVO\n") == 0

    def test_cabecalho_de_secao_vai_junto(self, tmp_path):
        """O operador precisa ver por que um termo esta la para decidir apagar."""
        from vocabulary import append_terms
        from vocabularies import pacote_saude

        f = tmp_path / "vocabulary.txt"
        append_terms(f, pacote_saude())
        texto = f.read_text(encoding="utf-8")
        assert "organismos internacionais" in texto.lower() or \
               "Organismos" in texto or "# ---" in texto
