"""O microfone da sala somado ao áudio do sistema.

O que estes testes protegem, na ordem em que o defeito apareceria num evento:

1. a soma em si (estouro de int16 vira estalo, não voz alta);
2. a deriva entre DOIS relógios físicos — medido nesta máquina: loopback a
   16041 Hz contra microfone a 16002 Hz, 0,25%, ~9 s de descompasso por hora
   se a fila não tiver teto;
3. o microfone cair sem levar a legenda com ele;
4. a interface: o controller tem três watchdogs em cima de `AudioCapture`, e
   `MixedCapture` entra no lugar dele. Um método que falte é um watchdog que
   morre de AttributeError na thread de captura, no meio do evento.

Nada aqui abre dispositivo: `MixedCapture.__init__` só constrói, e a soma é
uma função de bytes. Teste que precisa de placa de som não roda em CI nem na
máquina de quem não está com fone ligado.
"""
from __future__ import annotations

import numpy as np
import pytest

from audio_capture import _MIX_MAX_LAG_MS, _MIX_PREFILL_MS, AudioCapture, MixedCapture

SR = 16000
BLOCO = 800                      # 50 ms a 16 kHz
BYTES_POR_MS = SR * 2 / 1000.0


def pcm(valores) -> bytes:
    return np.asarray(valores, dtype=np.int16).tobytes()


def amostras(data: bytes):
    return np.frombuffer(data, dtype=np.int16)


def mix(gain: float = 1.0) -> MixedCapture:
    return MixedCapture(on_audio=lambda b: None, samplerate=SR,
                        channels=1, mic_gain=gain)


def encher(m: MixedCapture, ms: float, valor: int = 0) -> None:
    n = int(ms * SR / 1000)
    m._collect_mic(pcm([valor] * n))


class TestSomaDeVerdade:
    def test_sem_microfone_na_fila_sai_o_loopback_intacto(self):
        """Nunca inventar silêncio.

        A alternativa preguiçosa — somar zeros quando a fila está vazia — é
        indistinguível disto no resultado, mas custa uma cópia e um clip por
        bloco a 20 blocos por segundo durante duas horas. E mais importante:
        devolver o objeto original prova que o caminho sem microfone é o
        MESMO de antes desta funcionalidade existir.
        """
        m = mix()
        entrada = pcm([100, -100, 200])
        assert m._mix(entrada) == entrada

    def test_depois_do_colchao_soma_amostra_por_amostra(self):
        m = mix()
        encher(m, _MIX_PREFILL_MS + 50, valor=10)
        saida = amostras(m._mix(pcm([1, 2, 3, 4])))
        assert list(saida) == [11, 12, 13, 14]

    def test_ganho_multiplica_so_o_microfone(self):
        m = mix(gain=2.0)
        encher(m, _MIX_PREFILL_MS + 50, valor=10)
        saida = amostras(m._mix(pcm([1, 1])))
        assert list(saida) == [21, 21]        # 1 + 10*2

    def test_ganho_zero_devolve_o_sistema_sem_tocar_no_audio(self):
        """0 é "microfone desligado", e tem de ser o caminho exato do desligado."""
        m = mix(gain=0.0)
        encher(m, _MIX_PREFILL_MS + 50, valor=9999)
        entrada = pcm([1, 2])
        assert m._mix(entrada) == entrada

    def test_estouro_satura_em_vez_de_dar_a_volta(self):
        """O teste que existe por causa de um bug concreto de int16.

        30000 + 20000 em int16 não dá 50000: dá -15536. Na projeção isso não
        soa como "alto demais", soa como um estalo, e o reconhecimento devolve
        lixo exatamente quando duas pessoas falam ao mesmo tempo — que é o
        caso para o qual esta funcionalidade existe.
        """
        m = mix()
        encher(m, _MIX_PREFILL_MS + 50, valor=20000)
        saida = amostras(m._mix(pcm([30000, -30000])))
        assert list(saida) == [32767, -10000]
        assert saida.dtype == np.int16

    def test_o_bloco_de_saida_tem_o_tamanho_do_bloco_de_entrada(self):
        """O provedor recebe um stream contínuo; bloco de tamanho errado
        desalinha tudo que vem depois, sem erro nenhum."""
        m = mix()
        encher(m, 500, valor=5)
        for _ in range(4):
            assert len(m._mix(pcm([1] * BLOCO))) == BLOCO * 2


class TestDerivaDeRelogio:
    def test_fila_do_microfone_nao_cresce_sem_limite(self):
        """0,25% de deriva medida = 2,5 ms por segundo. Em duas horas são 18
        segundos de atraso acumulado: a legenda sairia com a voz da sala
        quase vinte segundos atrasada, e nada no app acusaria."""
        m = mix()
        encher(m, 5000, valor=1)                 # 5 s de microfone adiantado
        teto = int(_MIX_MAX_LAG_MS * BYTES_POR_MS)
        assert len(m._mic_buf) == teto
        assert m._trimmed_bytes == int(5000 * BYTES_POR_MS) - teto

    def test_o_que_sobrevive_ao_corte_e_o_audio_MAIS_NOVO(self):
        """Cortar a ponta errada dá o mesmo tamanho de fila e o defeito
        oposto: a fala mais velha seria emitida e a recente jogada fora, ou
        seja, atraso permanente em vez de corte pontual."""
        m = mix()
        encher(m, _MIX_MAX_LAG_MS, valor=111)    # velho, deve sair
        encher(m, _MIX_MAX_LAG_MS, valor=222)    # novo, deve ficar
        m._primed = True
        assert list(amostras(m._mix(pcm([0, 0])))) == [222, 222]

    def test_fila_que_seca_volta_a_encher_o_colchao(self):
        """Sem isto, um engasgo do agendador viraria um buraco de silêncio
        por bloco pelo resto do evento, em vez de um só."""
        m = mix()
        # Exatamente o colchão, nada mais: 100 ms = dois blocos de 50 ms.
        encher(m, _MIX_PREFILL_MS, valor=7)
        assert amostras(m._mix(pcm([0] * BLOCO)))[0] == 7     # 1o bloco: soma
        assert amostras(m._mix(pcm([0] * BLOCO)))[0] == 7     # 2o: esvazia
        # 3o: fila seca. Sai o loopback puro e o colchão volta a encher.
        assert m._mix(pcm([1] * BLOCO)) == pcm([1] * BLOCO)
        assert m._primed is False

    def test_bloco_maior_que_a_fila_nao_mistura_meio_bloco(self):
        m = mix()
        encher(m, 10, valor=3)                   # bem menos que o colchão
        entrada = pcm([1] * BLOCO)
        assert m._mix(entrada) == entrada


class TestMicrofoneCaindoNaoDerrubaOEvento:
    def test_morte_do_microfone_nao_chama_o_on_died_do_controller(self):
        """`on_died` é o gatilho de reabertura/queda da captura no controller.

        Encaminhar a morte do microfone para lá trocaria uma perda parcial
        (a voz da sala) por uma total (a legenda). O microfone é a metade
        extra; o loopback é o evento.
        """
        mortes: list[str] = []
        avisos: list[str] = []
        m = MixedCapture(on_audio=lambda b: None, samplerate=SR,
                         on_died=mortes.append, on_mic_lost=avisos.append)
        # Pela LIGAÇÃO, não chamando _mic_died na mão: o que a thread do
        # microfone invoca é `self.on_died` da captura dele. Uma primeira
        # versão deste teste chamava `m._mic_died(...)` direto e por isso
        # passava mesmo com a ligação trocada para o on_died do controller —
        # mutação sobreviveu, teste media o corpo do método e não o fio.
        m._mic.on_died("dispositivo removido")
        assert mortes == []
        assert avisos == ["dispositivo removido"]
        # E a morte do LOOPBACK continua chegando ao controller: é ela que
        # aciona a reabertura de captura.
        m._primary.on_died("saída removida")
        assert mortes == ["saída removida"]

    def test_morte_do_microfone_esvazia_a_fila(self):
        """Áudio parado numa fila é áudio que seria emitido fora de hora se o
        microfone voltasse."""
        m = mix()
        encher(m, 200, valor=5)
        m._mic_died("qualquer coisa")
        assert len(m._mic_buf) == 0 and m._primed is False

    def test_callback_de_aviso_que_explode_nao_mata_a_thread_do_microfone(self):
        def explode(_):
            raise RuntimeError("boom")
        m = MixedCapture(on_audio=lambda b: None, on_mic_lost=explode)
        m._mic_died("x")            # não deve propagar

    def test_falha_na_mixagem_emite_o_audio_do_sistema(self):
        """Roda na thread de captura: exceção aqui mataria a captura inteira.

        Legenda só com o áudio da chamada é degradação; sem legenda é queda.
        """
        recebidos: list[bytes] = []
        m = MixedCapture(on_audio=recebidos.append)
        m._mix = lambda data: (_ for _ in ()).throw(ValueError("qualquer"))
        entrada = pcm([1, 2, 3])
        m._mix_and_emit(entrada)
        assert recebidos == [entrada]


class TestAInterfaceQueOsWatchdogsUsam:
    def test_mixedcapture_responde_a_tudo_que_audiocapture_responde(self):
        """O controller não sabe qual das duas está rodando, e não deve.

        Comparar as superfícies públicas é o único teste que falha quando
        alguém acrescenta um método a `AudioCapture` e o watchdog passa a
        depender dele — o AttributeError real aconteceria na thread de
        captura, no meio do evento, sem traceback no log.
        """
        publicos = {n for n in dir(AudioCapture)
                    if not n.startswith("_") and callable(getattr(AudioCapture, n))}
        faltando = publicos - set(dir(MixedCapture))
        assert not faltando, f"MixedCapture não implementa {faltando}"

    def test_stop_limpa_o_estado(self):
        m = mix()
        encher(m, 200, valor=1)
        m._primed = True
        m.stop()
        assert len(m._mic_buf) == 0 and m._primed is False

    def test_o_microfone_e_aberto_como_microfone_e_nao_como_loopback(self):
        """Um `loopback=True` esquecido aqui abriria a SAÍDA duas vezes: a
        mesma fala somada a si mesma, que passa em todo teste de nível e
        destrói o reconhecimento."""
        m = mix()
        assert m._mic.loopback is False
        assert m._primary.loopback is True


class TestOControllerEscolheACaptura:
    def _controller(self, monkeypatch, **audio):
        import translator as T
        from config import AppConfig, AudioConfig
        monkeypatch.setattr(T, "find_device", lambda n: None)
        cfg = AppConfig(azure_speech_key="k", azure_speech_region="r",
                        audio=AudioConfig(**audio))
        c = T.TranslationController.__new__(T.TranslationController)
        c.config = cfg
        c._push_audio = lambda data: None
        c._on_capture_died = lambda reason: None
        c._on_microphone_lost = lambda reason: None
        return c

    def test_desligado_e_exatamente_a_captura_de_antes(self, monkeypatch):
        c = self._controller(monkeypatch)
        cap = c._build_capture(None)
        assert type(cap) is AudioCapture

    def test_ligado_constroi_a_captura_somada_com_o_que_foi_configurado(self, monkeypatch):
        c = self._controller(monkeypatch, capture_microphone=True,
                             microphone_name="Realtek", microphone_gain=1.7)
        cap = c._build_capture(None)
        assert type(cap) is MixedCapture
        assert cap.mic_gain == pytest.approx(1.7)
        assert cap._mic.device_arg == "Realtek"

    def test_ganho_negativo_no_json_nao_inverte_a_fase_do_microfone(self, monkeypatch):
        """Config é arquivo de texto que o operador pode editar à mão. Ganho
        negativo somaria a onda invertida e CANCELARIA parte do áudio."""
        c = self._controller(monkeypatch, capture_microphone=True,
                             microphone_gain=-2.0)
        assert c._build_capture(None).mic_gain == 0.0


class TestEnumeracaoNaoOferecOProprioLoopback:
    def test_list_input_devices_pede_microfone_real(self, monkeypatch):
        """Se os pseudo-microfones de loopback aparecessem nesta lista, o
        operador escolheria a própria saída como "microfone" e capturaria o
        mesmo áudio duas vezes — sem nenhum sintoma até a legenda duplicar.
        """
        import audio_capture

        pedidos = {}

        class FakeMic:
            id, name, channels = "id-1", "Microfone Real", 2

        def fake_all(include_loopback=True):
            pedidos["include_loopback"] = include_loopback
            return [FakeMic()]

        monkeypatch.setattr(audio_capture.sc, "all_microphones", fake_all)
        devs = audio_capture.list_input_devices()
        assert pedidos["include_loopback"] is False
        assert devs == [{"id": "id-1", "name": "Microfone Real", "channels": 2}]

    def test_placa_sem_microfone_devolve_lista_vazia_sem_explodir(self, monkeypatch):
        import audio_capture

        def boom(**kw):
            raise RuntimeError("sem dispositivo")

        monkeypatch.setattr(audio_capture.sc, "all_microphones", boom)
        assert audio_capture.list_input_devices() == []


@pytest.fixture(scope="module")
def qapp():
    """Guarda a referência: sem isso o GC recolhe o QApplication com a janela
    viva e o interpretador morre sem traceback."""
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class TestATelaSalvaERecarrega:
    def test_o_que_o_operador_marca_volta_na_config(self, qapp):
        from config import AppConfig, AudioConfig
        from settings_window import SettingsWindow

        w = SettingsWindow(AppConfig(audio=AudioConfig(
            capture_microphone=True, microphone_name=None, microphone_gain=1.4)))
        try:
            assert w.mic_check.isChecked() is True
            assert w.mic_gain_spin.value() == pytest.approx(1.4)
            # Desmarcar tem de chegar ao config, não só ao widget.
            w.mic_check.setChecked(False)
            assert w._build_config().audio.capture_microphone is False
            w.mic_check.setChecked(True)
            w.mic_gain_spin.setValue(0.8)
            salvo = w._build_config().audio
            assert salvo.capture_microphone is True
            assert salvo.microphone_gain == pytest.approx(0.8)
        finally:
            w.close()

    def test_desmarcado_desabilita_o_que_nao_se_aplica(self, qapp):
        from config import AppConfig, AudioConfig
        from settings_window import SettingsWindow

        w = SettingsWindow(AppConfig(audio=AudioConfig(capture_microphone=False)))
        try:
            assert w.mic_gain_spin.isEnabled() is False
            w.mic_check.setChecked(True)
            assert w.mic_gain_spin.isEnabled() is True
        finally:
            w.close()


class TestAChecagemDeAntesDoEvento:
    """`preflight.check_audio` é o gate que o operador roda antes de subir ao
    palco. Aprovar o áudio sem nunca abrir o microfone é pior que não checar:
    dá confiança onde não há.
    """

    def _cfg(self, **audio):
        from config import AppConfig, AudioConfig
        return AppConfig(azure_speech_key="k", azure_speech_region="r",
                         audio=AudioConfig(**audio))

    def _fake(self, monkeypatch, mic_viva: bool):
        import audio_capture
        construidos = []

        class FakeMix:
            def __init__(self, on_audio, **kw):
                self.on_audio = on_audio
                self.kw = kw
                construidos.append(self)

            def start(self):
                self.on_audio(pcm([12000] * 800))     # som de verdade

            def stop(self):
                pass

            def mic_is_alive(self):
                return mic_viva

        class FakeSolo:
            """Sem `mic_is_alive`, igual ao AudioCapture de verdade.

            Herdar de FakeMix seria mais curto e tornaria o teste cego: com a
            checagem construindo a captura errada, o falso ainda responderia
            "microfone vivo" e a suíte aprovaria. Medido — a mutação
            sobreviveu até esta classe ficar separada.
            """

            def __init__(self, on_audio, **kw):
                self.on_audio = on_audio
                self.kw = kw
                construidos.append(self)

            def start(self):
                self.on_audio(pcm([12000] * 800))

            def stop(self):
                pass

        monkeypatch.setattr(audio_capture, "MixedCapture", FakeMix)
        monkeypatch.setattr(audio_capture, "AudioCapture", FakeSolo)
        monkeypatch.setattr(audio_capture, "find_device", lambda n: None)
        return construidos, FakeMix, FakeSolo

    def test_microfone_que_nao_abre_reprova_a_checagem(self, monkeypatch):
        import preflight
        self._fake(monkeypatch, mic_viva=False)
        passo = preflight.check_audio(self._cfg(capture_microphone=True))
        assert passo.ok is False
        assert "MICROFONE" in passo.detail
        assert "sala" in passo.detail          # diz o que se perde, não só o quê

    def test_com_tudo_de_pe_a_checagem_diz_que_o_microfone_entrou(self, monkeypatch):
        import preflight
        construidos, FakeMix, _ = self._fake(monkeypatch, mic_viva=True)
        passo = preflight.check_audio(self._cfg(capture_microphone=True))
        assert passo.ok is True
        assert "Microfone somado" in passo.detail
        assert [type(c) for c in construidos] == [FakeMix]

    def test_sem_a_opcao_a_checagem_nao_abre_microfone_nenhum(self, monkeypatch):
        """Quem não pediu o microfone não paga por ele — nem um dispositivo
        aberto, nem uma linha a mais na mensagem."""
        import preflight
        construidos, FakeMix, FakeSolo = self._fake(monkeypatch, mic_viva=True)
        passo = preflight.check_audio(self._cfg())
        assert passo.ok is True
        assert "Microfone" not in passo.detail
        assert [type(c) for c in construidos] == [FakeSolo]
