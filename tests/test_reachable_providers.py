"""Só ficam provedores que este país alcança.

Medido em 2026-09-17/18, desta máquina, sem chave nenhuma:

    api.cerebras.ai   403 + "error code: 1009"   (Cloudflare, country_banned)
    api.groq.com      403 "Forbidden"
    openrouter.ai     200
    ...cognitive...   200   (Azure brazilsouth)

Que os dois 403 são geográficos e não de autenticação foi provado repetindo
pelo proxy, que sai por outro país: a Cerebras passa a responder
{"detail":"Not authenticated"} e a Groq passa a responder 401 Invalid API Key.
Com IP do Brasil nenhuma das duas chega a olhar a chave.

Então `groq`, `cerebras` e `openai_cerebras` (que dependia da metade Cerebras)
saíram. O que os substitui é `openrouter`. A checagem é OFFLINE de propósito:
depender da rede aqui tornaria o teste inútil justamente no CI.

`providers/groq.py` virou `providers/chunked_rest.py` porque o OpenRouter
herda aquele pipeline — o módulo é a base de chunked REST, não o fornecedor.
"""
from __future__ import annotations

import pathlib

import pytest

import config
from providers import PROVIDER_LABELS, PROVIDER_REQUIREMENTS

REMOVIDOS = ("groq", "cerebras", "openai_cerebras")
RAIZ = pathlib.Path(__file__).resolve().parent.parent


class TestOsBloqueadosNaoVoltam:
    @pytest.mark.parametrize("nome", REMOVIDOS)
    def test_nao_aparecem_na_lista_de_provedores(self, nome):
        assert nome not in PROVIDER_LABELS
        assert nome not in PROVIDER_REQUIREMENTS

    @pytest.mark.parametrize("nome", REMOVIDOS)
    def test_a_fabrica_recusa(self, nome):
        from providers import ProviderUnavailable, build_provider
        cfg = config.AppConfig(provider=nome, target_languages=["en"])
        with pytest.raises(ProviderUnavailable):
            build_provider(cfg, lambda ev: None)

    @pytest.mark.parametrize("campo", (
            "groq_api_key", "groq_transcription_model", "groq_translation_model",
            "cerebras_api_key", "cerebras_translation_model"))
    def test_os_campos_de_config_sumiram(self, campo):
        assert not hasattr(config.AppConfig, campo)
        assert campo not in config.SECRET_FIELDS

    def test_config_antiga_com_esses_campos_ainda_carrega(self, tmp_path, monkeypatch):
        """Ninguém perde a configuração por causa desta remoção."""
        import json
        # O conftest ja aponta config_path() para um arquivo temporario, com
        # OUTRO nome. Escrever "config.json" a mao aqui faria o teste ler os
        # defaults e passar sem provar nada -- foi o que aconteceu na primeira
        # versao, onde "brazilsouth" so passou por ser o valor padrao.
        config.config_path().write_text(json.dumps({
            "provider": "azure", "azure_speech_region": "brazilsouth",
            "groq_api_key": "gsk_velha", "cerebras_api_key": "csk_velha",
            "target_languages": ["es", "en"],
        }), encoding="utf-8")
        cfg = config.load_config()
        assert cfg.provider == "azure"
        assert cfg.azure_speech_region == "brazilsouth"
        assert cfg.target_languages == ["es", "en"]


class TestOsQueFicamContinuamInteiros:
    @pytest.mark.parametrize("nome", ("azure", "google", "whisper_local",
                                      "openrouter", "openai_realtime"))
    def test_seguem_na_lista_e_tem_requisito_declarado(self, nome):
        assert nome in PROVIDER_LABELS
        assert nome in PROVIDER_REQUIREMENTS

    def test_o_openrouter_herda_a_base_de_chunked_rest(self):
        from providers.chunked_rest import ChunkedRestProvider
        from providers.openrouter import OpenRouterProvider
        assert issubclass(OpenRouterProvider, ChunkedRestProvider)

    def test_a_base_nao_tem_endpoint_padrao(self):
        """Sem default, um subclasse esquecido falha alto em vez de mandar
        o áudio do evento para o host que estava no hardcode."""
        from providers.chunked_rest import ChunkedRestProvider
        assert not hasattr(ChunkedRestProvider, "GROQ_BASE_URL")
        fonte = (RAIZ / "providers" / "chunked_rest.py").read_text(encoding="utf-8")
        assert "api.groq.com" not in fonte


@pytest.fixture(scope="module")
def qapp():
    """Guarda a referencia do QApplication.

    Sem guardar, `QApplication.instance() or QApplication([])` dentro do teste
    descarta o objeto, o GC o recolhe com a janela ainda viva e o interpretador
    morre sem traceback nenhum -- a mesma assinatura silenciosa dos crashes que
    este projeto persegue. Aconteceu aqui, escrevendo estes testes.
    """
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class TestATelaNaoOfereceOQueNaoExiste:
    def test_nenhum_provedor_removido_na_janela_de_configuracoes(self, qapp):
        from settings_window import SettingsWindow
        w = SettingsWindow(config.AppConfig(provider="azure", azure_speech_key="k",
                                            target_languages=["es", "en"]))
        try:
            oferecidos = {w.provider_combo.itemData(i)
                          for i in range(w.provider_combo.count())}
            assert oferecidos.isdisjoint(REMOVIDOS), f"ainda oferecidos: {oferecidos}"
            reservas = {w.fallback_combo.itemData(i)
                        for i in range(w.fallback_combo.count())}
            assert reservas.isdisjoint(REMOVIDOS), f"reserva ainda oferece: {reservas}"
        finally:
            w.close()

    def test_cada_provedor_oferecido_abre_a_propria_pagina(self, qapp):
        """O mapa de índices escrito à mão sumiu; este teste é o que garante
        que a página mostrada é a do provedor escolhido."""
        from settings_window import SettingsWindow
        w = SettingsWindow(config.AppConfig(provider="azure", azure_speech_key="k",
                                            target_languages=["es", "en"]))
        try:
            for i in range(w.provider_combo.count()):
                code = w.provider_combo.itemData(i)
                w.provider_combo.setCurrentIndex(i)
                assert w.provider_stack.currentIndex() == w._provider_pages[code], (
                    f"{code} abriu a pagina errada")
        finally:
            w.close()
