"""O provedor de reserva precisa transcrever, nao so autenticar.

Medido em 2026-09-18 contra openrouter.ai/api/v1/models (445 modelos): os
CINCO ids de STT que o app oferecia haviam sido retirados do catalogo --
whisper-1, whisper-large-v3, whisper-large-v3-turbo, gpt-4o-transcribe e
gpt-4o-mini-transcribe. O teste de conexao passava assim mesmo, porque so
conferia a autenticacao. Ou seja: o unico provedor de reserva alcancavel do
Brasil reportava OK e nao transcrevia -- defeito que so apareceria depois de
o Azure cair, no meio do evento.

Estes testes sao offline de proposito: o catalogo vivo muda, e um teste que
depende de rede nao pode rodar no CI. O que eles travam e a coerencia interna
-- nenhum id aposentado pode voltar a ser oferecido, e o teste de conexao tem
de reprovar um modelo ausente em vez de dizer OK.
"""
from __future__ import annotations

import config
import connection_test


class TestIdsAposentadosNaoVoltam:
    def test_nenhum_id_morto_e_oferecido_na_ui(self):
        import settings_window
        src = __import__("inspect").getsource(
            settings_window.SettingsWindow._build_openrouter_form)
        for dead in config._DEAD_OPENROUTER_STT:
            assert f'"{dead}"' not in src, f"{dead} voltou para o combo de STT"
        for dead in config._DEAD_OPENROUTER_TRANSLATION:
            assert f'"{dead}"' not in src, f"{dead} voltou para o combo de traducao"

    def test_o_default_nao_e_um_id_morto(self):
        assert config.AppConfig.openrouter_stt_model not in config._DEAD_OPENROUTER_STT
        assert (config.AppConfig.openrouter_translation_model
                not in config._DEAD_OPENROUTER_TRANSLATION)


class TestConfigAntigaEMigrada:
    def test_stt_morto_vira_o_default(self):
        cfg = config.AppConfig(openrouter_stt_model="openai/whisper-1")
        config._retire_dead_openrouter_models(cfg)
        assert cfg.openrouter_stt_model == config.AppConfig.openrouter_stt_model

    def test_traducao_morta_vira_a_substituta(self):
        cfg = config.AppConfig(openrouter_translation_model="anthropic/claude-3.5-haiku")
        config._retire_dead_openrouter_models(cfg)
        assert cfg.openrouter_translation_model == "anthropic/claude-haiku-4.5"

    def test_escolha_valida_do_operador_nao_e_mexida(self):
        cfg = config.AppConfig(openrouter_stt_model="mistralai/voxtral-small-24b-2507")
        config._retire_dead_openrouter_models(cfg)
        assert cfg.openrouter_stt_model == "mistralai/voxtral-small-24b-2507"


class TestOTesteDeConexaoProvaOQueImporta:
    def _fake_client(self, monkeypatch, ids):
        class _M:
            def __init__(self, i): self.id = i

        class _Models:
            def list(self_inner):
                return type("R", (), {"data": [_M(i) for i in ids]})()

        class _Client:
            def __init__(self, **kw): self.models = _Models()

        import openai
        monkeypatch.setattr(openai, "OpenAI", _Client)

    def test_reprova_quando_o_modelo_escolhido_sumiu(self, monkeypatch):
        self._fake_client(monkeypatch, ["google/gemini-2.5-flash", "openai/gpt-oss-120b"])
        ok, msg = connection_test.test_openrouter("k", stt_model="openai/whisper-1")
        assert ok is False, "auth valida com STT inexistente nao pode passar"
        assert "whisper-1" in msg

    def test_aprova_quando_o_modelo_escolhido_existe(self, monkeypatch):
        self._fake_client(monkeypatch, ["google/gemini-3.5-flash-lite"])
        ok, msg = connection_test.test_openrouter("k", stt_model="google/gemini-3.5-flash-lite")
        assert ok is True
        assert "disponível" in msg


class TestBloqueioGeograficoNaoParecerChaveErrada:
    def test_1009_vira_explicacao_de_pais_bloqueado(self):
        bruto = ("Falhou: Error code: 403 - {'error_code': 1009, "
                 "'error_name': 'country_banned', 'detail': 'The site owner has "
                 "blocked the country or region associated with your IP address.'}")
        out = connection_test.explain_failure(bruto)
        assert "bloqueia conexões do país" in out
        assert "trocar de chave não resolve" in out

    def test_falha_comum_passa_intacta(self):
        bruto = "Falhou: Error code: 401 - Invalid API Key"
        assert connection_test.explain_failure(bruto) == bruto
