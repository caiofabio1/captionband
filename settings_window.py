"""Friendly settings GUI for CaptionBand.

Tabs:
- Credenciais: all API keys / credentials in one place (Azure, OpenAI, Google, OpenRouter)
- Provedor: choose which composition to use + composition-specific settings (models, etc.)
- Idiomas: source candidates + target languages, display mode
- Áudio: input device picker (WASAPI loopback list)
- Aparência: position, opacity, fonts, colors with live preview
- Sobre: version + paths

Saves to %LOCALAPPDATA%\\CaptionBand\\config.json on Apply.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import (
    DISPLAY_MODES,
    KNOWN_LANGUAGES,
    KNOWN_TARGETS,
    POSITIONS,
    WHISPER_COMPUTE_TYPES,
    WHISPER_DEVICE_OPTIONS,
    WHISPER_MODEL_OPTIONS,
    AppConfig,
    config_path,
    log_path,
    save_config,
)
from providers import PROVIDER_LABELS

log = logging.getLogger(__name__)


PROVIDER_HINTS = {
    "openai_realtime": (
        "OpenAI gpt-realtime-translate: tradução de fala em streaming, um hop só, "
        "detecta o idioma falado sozinho. ATENÇÃO ao custo: a API abre UMA SESSÃO "
        "POR IDIOMA DE DESTINO, então o preço multiplica — US$0.034/min por sessão "
        "(2 idiomas ≈ US$4.08/h). Preço conferido em 2026-09-15. Usa a chave OpenAI "
        "da aba Credenciais."
    ),
    "openrouter": (
        "OpenRouter roteia pra 200+ modelos com 1 chave só. STT via Whisper + tradução "
        "via LLM (Llama, GPT-OSS, etc). Latência ~25-40ms maior que ir direto. "
        "Custo ~$0.40/h. Uma chave no lugar de varias contas."
    ),
    "azure": (
        "Azure Speech Translation. Auto-detect até 10 idiomas. Latência ~2–3s. "
        "Custo F0 grátis (5h/mês), S0 ~US$2.50/h."
    ),
    "google": (
        "Google Cloud Speech v2 + Translate v3. Latência 1–2s, partials. "
        "Até 4 idiomas-fonte. Custo ~US$1.44/h Speech + ~US$0.10/h Translate."
    ),
    "whisper_local": (
        "Whisper local (faster-whisper) + Argos Translate. 100% offline. "
        "Custo zero. Latência 3–6s no CPU; 1–3s na GPU. Modelo baixa só uma vez."
    ),
}


class ColorButton(QPushButton):
    color_changed = pyqtSignal(str)

    def __init__(self, color_hex: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.color_hex = color_hex
        self._update_swatch()
        self.clicked.connect(self._pick)
        self.setFixedWidth(110)

    def _update_swatch(self) -> None:
        c = QColor(self.color_hex)
        readable = "#000000" if c.lightness() > 128 else "#FFFFFF"
        self.setStyleSheet(
            f"background-color: {self.color_hex}; color: {readable};"
            f" border: 1px solid #555; padding: 6px;"
        )
        self.setText(self.color_hex)

    def _pick(self) -> None:
        color = QColorDialog.getColor(QColor(self.color_hex), self, "Escolher cor")
        if color.isValid():
            self.color_hex = color.name()
            self._update_swatch()
            self.color_changed.emit(self.color_hex)


class LanguageChecklist(QListWidget):
    selection_changed = pyqtSignal()

    def __init__(self, options: dict[str, str], selected: list[str], parent=None):
        super().__init__(parent)
        for code, label in options.items():
            item = QListWidgetItem(f"{label}  ({code})")
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if code in selected else Qt.CheckState.Unchecked)
            self.addItem(item)
        self.itemChanged.connect(lambda _: self.selection_changed.emit())
        self.setMaximumHeight(160)

    def selected_codes(self) -> list[str]:
        out = []
        for i in range(self.count()):
            item = self.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out


class SettingsWindow(QDialog):
    config_saved = pyqtSignal(object)
    # Audio-test meter level, emitted from the capture thread; Qt queues the
    # delivery onto the GUI thread where the QProgressBar lives.
    _capture_level = pyqtSignal(int)

    def __init__(self, config: AppConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("CaptionBand — Configurações")
        # Smaller min height so the dialog fits on laptops/half-screen layouts;
        # tab content scrolls if it overflows (see _wrap_in_scroll).
        self.setMinimumSize(780, 520)
        self.config = config

        self._build_ui()
        self._load_from_config()
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        from PyQt6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        size = self.sizeHint()
        x = geo.x() + (geo.width() - size.width()) // 2
        y = geo.y() + (geo.height() - size.height()) // 2
        self.move(max(0, x), max(0, y))

    def showEvent(self, event):  # type: ignore[override]
        super().showEvent(event)
        # Monitors may have been (un)plugged since the dialog was built —
        # the projector typically arrives AFTER the app is already open.
        if hasattr(self, "screen_combo"):
            self._populate_screens()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.tabs.addTab(self._wrap_in_scroll(self._build_credentials_tab()), "Credenciais")
        self.tabs.addTab(self._wrap_in_scroll(self._build_provider_tab()), "Provedor")
        self.tabs.addTab(self._wrap_in_scroll(self._build_languages_tab()), "Idiomas")
        self.tabs.addTab(self._wrap_in_scroll(self._build_audio_tab()), "Áudio")
        self.tabs.addTab(self._wrap_in_scroll(self._build_appearance_tab()), "Aparência")
        self.tabs.addTab(self._wrap_in_scroll(self._build_layout_tab()), "Legenda")
        self.tabs.addTab(self._wrap_in_scroll(self._build_about_tab()), "Sobre")
        # Breadcrumbs for the crash log: which tab, which button, last.
        log.info("ui: settings opened")
        self.tabs.currentChanged.connect(
            lambda i: log.info("ui: settings tab '%s'", self.tabs.tabText(i)))
        for btn in self.findChildren(QPushButton):
            btn.clicked.connect(
                lambda _c=False, t=btn.text() or btn.objectName(): log.info(
                    "ui: settings button '%s'", t))
        for combo in self.findChildren(QComboBox):
            combo.activated.connect(
                lambda i, c=combo: log.info("ui: settings combo -> '%s'", c.itemText(i)))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Salvar e fechar")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _wrap_in_scroll(content: QWidget) -> QScrollArea:
        """Wrap a tab page in a scroll area so tall content stays inside the
        dialog and the Save/Cancel buttons remain visible at the bottom."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def _build_credentials_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)

        intro = QLabel(
            "Configure aqui as chaves dos serviços que pretende usar. "
            "Depois, na aba <b>Provedor</b>, escolha qual composição (combinação) "
            "de serviços a app deve usar pra transcrever e traduzir."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: rgba(255,255,255,0.7);")
        outer.addWidget(intro)

        # --- OpenRouter (recomendado simples — 1 chave para tudo) ---
        or_box = QGroupBox("OpenRouter (1 chave para STT + tradução — recomendado simples)")
        or_layout = QFormLayout(or_box)

        self.openrouter_api_key_input = QLineEdit()
        self.openrouter_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.openrouter_api_key_input.setText(self.config.openrouter_api_key)
        self.openrouter_api_key_input.setPlaceholderText("sk-or-v1-...")
        or_layout.addRow("API Key:", self.openrouter_api_key_input)

        or_link = QLabel('<a href="https://openrouter.ai/settings/keys">Pegue uma chave em openrouter.ai/settings/keys</a>')
        or_link.setOpenExternalLinks(True)
        or_layout.addRow("", or_link)

        or_note = QLabel("<small><i>Uma chave no lugar de várias contas. Acesso a 200+ modelos. Latência ~25-40ms maior.</i></small>")
        or_note.setWordWrap(True)
        or_layout.addRow("", or_note)

        self.openrouter_test_btn = QPushButton("Testar conexão")
        self.openrouter_test_btn.clicked.connect(self._test_openrouter_credential)
        or_layout.addRow("", self.openrouter_test_btn)
        outer.addWidget(or_box)

        # --- OpenAI ---
        openai_box = self._make_credential_section(
            title="OpenAI (Whisper STT)",
            link_text="Pegue uma chave em platform.openai.com/api-keys",
            link_url="https://platform.openai.com/api-keys",
            key_attr_name="openai_api_key_input",
            placeholder="sk-...",
            current_value=self.config.openai_api_key,
            test_method="_test_openai_credential",
            test_button_attr="openai_test_btn",
        )
        outer.addWidget(openai_box)

        # --- Azure ---
        azure_box = QGroupBox("Azure Speech (STT + Translation combinados)")
        azure_layout = QFormLayout(azure_box)
        self.azure_key_input_credentials = QLineEdit()
        self.azure_key_input_credentials.setEchoMode(QLineEdit.EchoMode.Password)
        self.azure_key_input_credentials.setText(self.config.azure_speech_key)
        self.azure_key_input_credentials.setPlaceholderText("32 caracteres hex")
        azure_layout.addRow("Speech Key:", self.azure_key_input_credentials)

        self.azure_region_input_credentials = QLineEdit(self.config.azure_speech_region)
        self.azure_region_input_credentials.setMaximumWidth(200)
        azure_layout.addRow("Region:", self.azure_region_input_credentials)

        azure_link = QLabel('<a href="https://portal.azure.com">portal.azure.com</a> → Speech resource')
        azure_link.setOpenExternalLinks(True)
        azure_layout.addRow("", azure_link)

        self.azure_test_btn = QPushButton("Testar conexão")
        self.azure_test_btn.clicked.connect(self._test_azure_credential)
        azure_layout.addRow("", self.azure_test_btn)
        outer.addWidget(azure_box)

        # --- Google ---
        google_box = QGroupBox("Google Cloud (Speech v2 + Translate)")
        google_layout = QFormLayout(google_box)

        google_path_row = QHBoxLayout()
        self.google_creds_input_credentials = QLineEdit(self.config.google_credentials_json)
        self.google_creds_input_credentials.setPlaceholderText("Caminho do JSON de service account")
        google_browse = QPushButton("Procurar...")
        google_browse.clicked.connect(self._browse_google_creds_file)
        google_path_row.addWidget(self.google_creds_input_credentials, 1)
        google_path_row.addWidget(google_browse)
        google_creds_w = QWidget()
        google_creds_w.setLayout(google_path_row)
        google_layout.addRow("Credentials JSON:", google_creds_w)

        self.google_project_input_credentials = QLineEdit(self.config.google_project_id)
        google_layout.addRow("Project ID:", self.google_project_input_credentials)

        google_link = QLabel('<a href="https://console.cloud.google.com">console.cloud.google.com</a>')
        google_link.setOpenExternalLinks(True)
        google_layout.addRow("", google_link)

        self.google_test_btn = QPushButton("Testar conexão")
        self.google_test_btn.clicked.connect(self._test_google_credential)
        google_layout.addRow("", self.google_test_btn)
        outer.addWidget(google_box)

        outer.addStretch()
        return page

    def _make_credential_section(
        self,
        title: str,
        link_text: str,
        link_url: str,
        key_attr_name: str,
        placeholder: str,
        current_value: str,
        test_method: str,
        test_button_attr: str,
    ) -> QGroupBox:
        box = QGroupBox(title)
        form = QFormLayout(box)

        key_input = QLineEdit()
        key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_input.setText(current_value)
        key_input.setPlaceholderText(placeholder)
        setattr(self, key_attr_name, key_input)
        form.addRow("API Key:", key_input)

        link = QLabel(f'<a href="{link_url}">{link_text}</a>')
        link.setOpenExternalLinks(True)
        form.addRow("", link)

        test_btn = QPushButton("Testar conexão")
        test_btn.clicked.connect(getattr(self, test_method))
        setattr(self, test_button_attr, test_btn)
        form.addRow("", test_btn)

        return box

    # ------------------------------------------------------------------ credential test slots

    def _test_openrouter_credential(self) -> None:
        import functools

        from connection_test import test_openrouter
        stt = self.openrouter_stt_model_combo.currentData() or ""
        self._do_credential_test(
            button=self.openrouter_test_btn,
            title="OpenRouter",
            api_key=self.openrouter_api_key_input.text().strip(),
            test_func=functools.partial(test_openrouter, stt_model=stt),
        )


    def _test_openai_credential(self) -> None:
        from connection_test import test_openai_whisper
        self._do_credential_test(
            button=self.openai_test_btn,
            title="OpenAI Whisper",
            api_key=self.openai_api_key_input.text().strip(),
            test_func=test_openai_whisper,
        )


    def _test_azure_credential(self) -> None:
        from connection_test import test_azure
        key = self.azure_key_input_credentials.text().strip()
        region = self.azure_region_input_credentials.text().strip() or "brazilsouth"
        self.azure_test_btn.setEnabled(False)
        self.azure_test_btn.setText("Testando...")
        QApplication.processEvents()
        try:
            ok, msg = test_azure(key, region)
        finally:
            self.azure_test_btn.setEnabled(True)
            self.azure_test_btn.setText("Testar conexão")
        if ok:
            QMessageBox.information(self, "Azure", msg)
        else:
            QMessageBox.warning(self, "Azure", msg)

    def _test_google_credential(self) -> None:
        from connection_test import test_google
        creds_path = self.google_creds_input_credentials.text().strip()
        project_id = self.google_project_input_credentials.text().strip()
        self.google_test_btn.setEnabled(False)
        self.google_test_btn.setText("Testando...")
        QApplication.processEvents()
        try:
            ok, msg = test_google(creds_path, project_id)
        finally:
            self.google_test_btn.setEnabled(True)
            self.google_test_btn.setText("Testar conexão")
        if ok:
            QMessageBox.information(self, "Google", msg)
        else:
            QMessageBox.warning(self, "Google", msg)

    def _do_credential_test(self, button: QPushButton, title: str, api_key: str, test_func) -> None:
        button.setEnabled(False)
        button.setText("Testando...")
        QApplication.processEvents()
        try:
            ok, msg = test_func(api_key)
        finally:
            button.setEnabled(True)
            button.setText("Testar conexão")
        if ok:
            QMessageBox.information(self, title, msg)
        else:
            from connection_test import explain_failure
            QMessageBox.warning(self, title, explain_failure(msg))

    def _browse_google_creds_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Selecionar Google service account JSON",
            "",
            "JSON files (*.json)",
        )
        if path:
            self.google_creds_input_credentials.setText(path)

    # ------------------------------------------------------------------ provider tab

    def _build_provider_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)

        chooser_row = QHBoxLayout()
        chooser_row.addWidget(QLabel("Provedor:"))
        self.provider_combo = QComboBox()
        for code, label in PROVIDER_LABELS.items():
            self.provider_combo.addItem(label, code)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        chooser_row.addWidget(self.provider_combo, 1)
        chooser_w = QWidget()
        chooser_w.setLayout(chooser_row)
        outer.addWidget(chooser_w)

        self.provider_hint = QLabel()
        self.provider_hint.setWordWrap(True)
        self.provider_hint.setStyleSheet("color: #888; padding: 4px 0 8px 0;")
        outer.addWidget(self.provider_hint)

        self.provider_stack = QStackedWidget()
        outer.addWidget(self.provider_stack, 1)

        # A pagina de cada provedor e registrada pelo CODIGO, nao por um
        # indice escrito a mao em dois lugares. O mapa duplicado ja existia e
        # e a forma classica de a tela abrir o formulario errado no dia em que
        # um provedor sai da lista.
        self._provider_pages = {
            code: self.provider_stack.addWidget(build())
            for code, build in (
                ("azure", self._build_azure_form),
                ("google", self._build_google_form),
                ("whisper_local", self._build_whisper_form),
                ("openrouter", self._build_openrouter_form),
                ("openai_realtime", self._build_openai_realtime_form),
            )
        }

        chunk_row = QFormLayout()
        self.chunk_seconds_spin = QDoubleSpinBox()
        self.chunk_seconds_spin.setRange(2.0, 15.0)
        self.chunk_seconds_spin.setSingleStep(0.5)
        self.chunk_seconds_spin.setSuffix(" s")
        chunk_row.addRow(
            "Tamanho do chunk (OpenRouter/Whisper local):", self.chunk_seconds_spin
        )
        chunk_w = QWidget()
        chunk_w.setLayout(chunk_row)
        outer.addWidget(chunk_w)

        # Reserve provider — the fallback chain had no UI at all, so the only
        # way to configure it was editing config.json by hand.
        fb_row = QFormLayout()
        self.fallback_combo = QComboBox()
        self.fallback_combo.addItem("(nenhum — se o provedor cair, a legenda para)", "")
        for code, label in PROVIDER_LABELS.items():
            self.fallback_combo.addItem(label, code)
        self.fallback_combo.setToolTip(
            "Usado automaticamente se o provedor principal falhar repetidamente "
            "durante o evento. Precisa da própria credencial na aba Credenciais."
        )
        fb_row.addRow("Provedor de reserva:", self.fallback_combo)
        fb_w = QWidget()
        fb_w.setLayout(fb_row)
        outer.addWidget(fb_w)

        # Comportamento — auto-start when launched
        self.auto_start_check = QCheckBox(
            "Iniciar tradução automaticamente ao abrir o app"
        )
        self.auto_start_check.setToolTip(
            "Útil pra autostart do Windows: o app conecta no provider e começa "
            "a capturar áudio sem você precisar clicar em 'Iniciar tradução'."
        )
        outer.addWidget(self.auto_start_check)

        return w

    def _build_azure_form(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)

        info = QLabel(
            "Esta composição usa:<br>"
            "• <b>Azure Speech key + region</b> — STT e tradução combinados<br><br>"
            "Configure as credenciais na aba <b>Credenciais</b>."
        )
        info.setWordWrap(True)
        outer.addWidget(info)

        # ----- Modo Streaming (single-language) -----
        group = QGroupBox("Modo Streaming (palavra-por-palavra)")
        gl = QFormLayout(group)

        explain = QLabel(
            "Liga partials incrementais (latência ~300-500ms, igual Microsoft "
            "Live Captions) ao custo de fixar UMA língua de origem por vez.<br>"
            "Sem streaming: detecção automática de até 10 línguas (latência ~2-3s)."
        )
        explain.setWordWrap(True)
        explain.setStyleSheet("color: #666; padding: 0 0 6px 0;")
        gl.addRow(explain)

        self.azure_streaming_check = QCheckBox("Ativar streaming single-language")
        gl.addRow(self.azure_streaming_check)

        self.azure_streaming_lang_combo = QComboBox()
        for code, label in KNOWN_LANGUAGES.items():
            self.azure_streaming_lang_combo.addItem(label, code)
        gl.addRow("Língua inicial:", self.azure_streaming_lang_combo)

        # Multi-select list of quick-switch languages.
        from PyQt6.QtWidgets import QListWidget as _QLW
        self.azure_quick_langs_list = _QLW()
        self.azure_quick_langs_list.setSelectionMode(
            _QLW.SelectionMode.MultiSelection
        )
        for code, label in KNOWN_LANGUAGES.items():
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, code)
            self.azure_quick_langs_list.addItem(it)
        self.azure_quick_langs_list.setMaximumHeight(140)
        gl.addRow("Línguas de troca rápida:", self.azure_quick_langs_list)

        self.azure_switch_hotkey_input = QLineEdit()
        self.azure_switch_hotkey_input.setPlaceholderText("ex: f9, ctrl+f9, alt+l (vazio = desativar)")
        self.azure_switch_hotkey_input.setMaximumWidth(220)
        gl.addRow("Atalho global (cíclico):", self.azure_switch_hotkey_input)

        outer.addWidget(group)
        outer.addStretch(1)
        return w


    def _build_google_form(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        info = QLabel(
            "Esta composição usa:<br>"
            "• <b>Google service account JSON + Project ID</b> — Speech v2 + Translate<br><br>"
            "Configure as credenciais na aba <b>Credenciais</b>."
        )
        info.setWordWrap(True)
        layout.addRow(info)

        self.google_location_edit = QLineEdit()
        self.google_location_edit.setPlaceholderText("global, us-central1, europe-west1, southamerica-east1")
        layout.addRow("Location:", self.google_location_edit)

        self.google_recognizer_edit = QLineEdit()
        self.google_recognizer_edit.setPlaceholderText("_  (default)  ou ID customizado")
        layout.addRow("Recognizer ID:", self.google_recognizer_edit)

        return w

    def _build_whisper_form(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)

        self.whisper_model_combo = QComboBox()
        for code, label in WHISPER_MODEL_OPTIONS.items():
            self.whisper_model_combo.addItem(label, code)
        layout.addRow("Modelo Whisper:", self.whisper_model_combo)

        self.whisper_device_combo = QComboBox()
        for code, label in WHISPER_DEVICE_OPTIONS.items():
            self.whisper_device_combo.addItem(label, code)
        layout.addRow("Device:", self.whisper_device_combo)

        self.whisper_compute_combo = QComboBox()
        for code, label in WHISPER_COMPUTE_TYPES.items():
            self.whisper_compute_combo.addItem(label, code)
        layout.addRow("Tipo de cálculo:", self.whisper_compute_combo)

        test_btn = QPushButton("Testar carga do modelo")
        test_btn.clicked.connect(self._on_test_whisper)
        layout.addRow(test_btn)

        layout.addRow(QLabel(
            "<small>100% offline. Modelos baixados em "
            "<code>%LOCALAPPDATA%\\CaptionBand\\models</code> "
            "no primeiro uso. Argos Translate baixa pacotes de idioma sob demanda.</small>"
        ))
        return w

    def _on_test_whisper(self) -> None:
        from connection_test import test_whisper_local
        reply = QMessageBox.question(
            self,
            "Testar modelo Whisper",
            "O teste vai carregar o modelo selecionado. Na primeira execução, "
            "isto baixa de 75 MB a 3 GB de dados (uma vez só, fica em cache).\n\n"
            "Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, msg = test_whisper_local(
                self.whisper_model_combo.currentData() or "small",
                self.whisper_device_combo.currentData() or "cpu",
                self.whisper_compute_combo.currentData() or "int8",
            )
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            QMessageBox.information(self, "Whisper local", msg)
        else:
            QMessageBox.warning(self, "Whisper local — Falha", msg)



    def _build_openai_realtime_form(self) -> QWidget:
        """No credential fields of its own — it uses the OpenAI key from the
        Credenciais tab. What it DOES need is the cost warning in front of the
        operator at the moment they pick it, because this is the only provider
        here whose price scales with the number of target languages."""
        form_widget = QWidget()
        layout = QVBoxLayout()

        info = QLabel(
            "Usa a <b>chave OpenAI</b> da aba <b>Credenciais</b>.<br><br>"
            "O idioma de origem é detectado automaticamente — só os idiomas de "
            "<b>destino</b> (aba Idiomas) importam aqui."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.openai_realtime_cost_label = QLabel()
        self.openai_realtime_cost_label.setWordWrap(True)
        self.openai_realtime_cost_label.setStyleSheet(
            "color: #b06000; border: 1px solid #b06000; border-radius: 6px; "
            "padding: 8px; margin-top: 8px;"
        )
        layout.addWidget(self.openai_realtime_cost_label)
        self._refresh_openai_realtime_cost()

        btn = QPushButton("Testar chave OpenAI")
        btn.clicked.connect(self._test_openai_credential)
        layout.addWidget(btn)

        layout.addStretch(1)
        form_widget.setLayout(layout)
        return form_widget

    def _refresh_openai_realtime_cost(self) -> None:
        """Recompute the projected hourly cost from the CURRENT target count."""
        label = getattr(self, "openai_realtime_cost_label", None)
        if label is None:
            return
        try:
            n = max(1, len(self.targets_list.selected_codes()))
        except Exception:
            n = max(1, len(self.config.target_languages or ["es"]))
        per_min = 0.034                      # US$/min por sessão, 2026-09-15
        total_h = per_min * 60 * n
        label.setText(
            "⚠ Este provedor abre <b>uma sessão por idioma de destino</b>.<br>"
            f"Com <b>{n} idioma(s)</b> selecionado(s): {n} × US$ {per_min:.3f}/min "
            f"≈ <b>US$ {total_h:.2f} por hora</b> de evento.<br>"
            "<span style='color:#666'>Azure faz multi-idioma numa sessão só e "
            "tem 5 h/mês grátis (F0).</span>"
        )

    def _build_openrouter_form(self) -> QWidget:
        form_widget = QWidget()
        layout = QFormLayout(form_widget)

        info = QLabel(
            "<b>1 chave OpenRouter para tudo</b> — STT + tradução.<br><br>"
            "O OpenRouter <b>não serve Whisper</b>. A transcrição vai por modelo "
            "multimodal que aceita áudio (Gemini, Voxtral, gpt-audio), via "
            "chat/completions.<br><br>"
            "Configure na aba <b>Credenciais</b>.<br>"
            "<small>Latência ~25-40ms maior que ir direto. Custo similar ou ~5-10% maior.</small>"
        )
        info.setWordWrap(True)
        layout.addRow(info)

        self.openrouter_stt_model_combo = QComboBox()
        for m in [
            # Verificados contra openrouter.ai/api/v1/models em 2026-09-18:
            # todos existem e aceitam audio de entrada. Os cinco ids anteriores
            # foram retirados do catalogo e faziam a transcricao falhar em
            # silencio; a lista morta vive em config._DEAD_OPENROUTER_STT.
            "google/gemini-3.5-flash-lite",
            "google/gemini-2.5-flash-lite",
            "mistralai/voxtral-small-24b-2507",
            "openai/gpt-audio-mini",
            "google/gemini-2.5-flash",
        ]:
            self.openrouter_stt_model_combo.addItem(m, m)
        idx = self.openrouter_stt_model_combo.findData(self.config.openrouter_stt_model)
        if idx >= 0:
            self.openrouter_stt_model_combo.setCurrentIndex(idx)
        layout.addRow("Modelo STT:", self.openrouter_stt_model_combo)

        self.openrouter_translation_model_combo = QComboBox()
        for m in [
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "meta-llama/llama-3.3-70b-instruct",
            "meta-llama/llama-3.1-8b-instruct",
            "anthropic/claude-haiku-4.5",
            "google/gemini-2.5-flash",
        ]:
            self.openrouter_translation_model_combo.addItem(m, m)
        idx = self.openrouter_translation_model_combo.findData(self.config.openrouter_translation_model)
        if idx >= 0:
            self.openrouter_translation_model_combo.setCurrentIndex(idx)
        layout.addRow("Modelo tradução:", self.openrouter_translation_model_combo)

        return form_widget

    def _build_languages_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)

        sources_group = QGroupBox("Idiomas falados (auto-detect)")
        sg_layout = QVBoxLayout(sources_group)
        sg_layout.addWidget(QLabel("Marque os idiomas que poderão ser falados no webinar."))
        self.sources_list = LanguageChecklist(KNOWN_LANGUAGES, self.config.source_languages)
        sg_layout.addWidget(self.sources_list)
        outer.addWidget(sources_group)

        targets_group = QGroupBox("Idiomas de saída (tradução)")
        tg_layout = QVBoxLayout(targets_group)
        tg_layout.addWidget(QLabel("Marque um ou mais idiomas que aparecerão como legenda."))
        self.targets_list = LanguageChecklist(KNOWN_TARGETS, self.config.target_languages)
        tg_layout.addWidget(self.targets_list)
        # The OpenAI Realtime cost is a function of how many targets are
        # ticked, so it has to follow this list rather than be written once.
        self.targets_list.selection_changed.connect(
            self._refresh_openai_realtime_cost
        )
        self._refresh_openai_realtime_cost()
        outer.addWidget(targets_group)

        vocab_group = QGroupBox("Vocabulário do evento")
        vg = QFormLayout(vocab_group)
        vg.addRow(self._wrap_label(
            "Termos que o reconhecimento costuma errar: nomes próprios, siglas, "
            "termos da área. Um por linha, num arquivo de texto.<br>"
            "<b>Corrige o que é OUVIDO</b>, não como o termo é traduzido — e é a "
            "metade que mais importa, porque palavra mal ouvida gera tradução "
            "ruim de qualquer jeito.<br>"
            "<small>A lista é enviada quando o reconhecimento começa. Edite entre "
            "sessões, não durante uma fala.</small>"))

        self.vocabulary_weight_spin = QDoubleSpinBox()
        self.vocabulary_weight_spin.setRange(0.0, 2.0)
        self.vocabulary_weight_spin.setSingleStep(0.1)
        self.vocabulary_weight_spin.setDecimals(1)
        self.vocabulary_weight_spin.setSpecialValueText("desligado")
        self.vocabulary_weight_spin.setToolTip(
            "Quanto o vocabulário pesa contra o dicionário padrão do serviço.\n"
            "0 desliga, 1.0 é o padrão da Azure, 2.0 é o máximo. Vale para a "
            "lista inteira, não por termo.\n"
            "Peso alto faz o serviço preferir seus termos — inclusive quando a "
            "pessoa não disse nenhum deles.")
        vg.addRow("Peso:", self.vocabulary_weight_spin)

        self.vocabulary_count_label = QLabel()
        vg.addRow("Termos:", self.vocabulary_count_label)

        vocab_btn = QPushButton("Abrir vocabulário para editar")
        vocab_btn.clicked.connect(self._open_vocabulary_file)
        vg.addRow(vocab_btn)
        outer.addWidget(vocab_group)

        outer.addStretch(1)
        return w

    @staticmethod
    def _wrap_label(html: str) -> QLabel:
        label = QLabel(html)
        label.setWordWrap(True)
        label.setStyleSheet("color: #888;")
        return label

    def _refresh_vocabulary_count(self) -> None:
        """How many terms the file actually yields, read from disk.

        Counting here rather than trusting a remembered number: the operator
        edits the file in Notepad behind the app's back, and a stale count is
        the kind of thing that makes someone believe a term was added when the
        line was commented out.
        """
        from config import vocabulary_path
        from vocabulary import MAX_PHRASES, load_terms

        path = vocabulary_path()
        if not path.exists():
            self.vocabulary_count_label.setText(
                "nenhum ainda — o arquivo é criado ao abrir")
            return
        termos = load_terms(path)
        aviso = ""
        if len(termos) > MAX_PHRASES:
            aviso = f"  ⚠ acima do limite de {MAX_PHRASES} da Azure"
        self.vocabulary_count_label.setText(f"{len(termos)}{aviso}")

    def _open_vocabulary_file(self) -> None:
        import os
        import subprocess

        from config import vocabulary_path
        from vocabulary import ensure_file

        path = vocabulary_path()
        ensure_file(path)
        if not path.exists():
            QMessageBox.warning(
                self, "Vocabulário",
                f"Não foi possível criar o arquivo em:\n{path}")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            try:
                subprocess.Popen(["notepad.exe", str(path)])
            except Exception as exc:
                QMessageBox.warning(self, "Vocabulário",
                                    f"Abra à mão:\n{path}\n\n{exc}")
                return
        self._refresh_vocabulary_count()

    def _build_audio_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        self.device_combo = QComboBox()
        self.device_combo.setEditable(False)
        self._populate_devices()
        refresh = QPushButton("Atualizar lista")
        refresh.clicked.connect(self._populate_devices)
        device_row = QHBoxLayout()
        device_row.addWidget(self.device_combo, 1)
        device_row.addWidget(refresh)
        device_w = QWidget()
        device_w.setLayout(device_row)
        layout.addRow("Saída a capturar:", device_w)

        # Audio level meter + test button
        from PyQt6.QtWidgets import QProgressBar

        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setValue(0)
        self.level_bar.setTextVisible(True)
        self.level_bar.setFormat("Nível: %p%")
        self.level_bar.setMinimumWidth(280)
        self._capture_level.connect(self.level_bar.setValue)

        test_btn = QPushButton("Testar captura (3s)")
        self.test_capture_btn = test_btn
        test_btn.clicked.connect(self._on_test_capture)

        meter_row = QHBoxLayout()
        meter_row.addWidget(self.level_bar, 1)
        meter_row.addWidget(test_btn)
        meter_w = QWidget()
        meter_w.setLayout(meter_row)
        layout.addRow("Medidor de áudio:", meter_w)

        layout.addRow(QLabel(
            "<small>Escolha o dispositivo de <b>saída</b> onde o Teams toca o áudio. "
            "O app captura via <b>WASAPI loopback</b> — não precisa de cabo virtual. "
            "Use o botão <b>Testar captura</b> com áudio tocando para confirmar "
            "que o dispositivo certo foi selecionado.</small>"
        ))
        return w

    def _on_test_capture(self) -> None:
        from PyQt6.QtCore import QTimer as _QTimer

        from audio_capture import AudioCapture, find_device

        # The button was never disabled, so a second click replaced
        # self._test_capture while the first stream was still recording. The
        # finish() closure reads self._test_capture at FIRE time, so the first
        # stream was orphaned with nothing left holding it — a live WASAPI
        # capture leaked per extra click, in the dialog whose capture callback
        # already caused one confirmed abort.
        if getattr(self, "_test_capture", None) is not None:
            return

        cfg = self._build_config()
        device = find_device(cfg.audio.device_name)
        if device is None:
            QMessageBox.warning(self, "Teste", "Nenhum dispositivo encontrado.")
            return

        self._test_max_rms = 0.0
        self._test_chunks = 0
        if getattr(self, "test_capture_btn", None) is not None:
            self.test_capture_btn.setEnabled(False)

        import numpy as np

        def on_audio(data: bytes) -> None:
            # Runs on the CAPTURE thread (soundcard/cffi), not the GUI thread.
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if arr.size == 0:
                return
            rms = float(np.sqrt(np.mean(arr * arr)))
            self._test_max_rms = max(self._test_max_rms, rms)
            self._test_chunks += 1
            # NEVER touch a widget from here. This used to call
            # self.level_bar.setValue() directly — ~60 cross-thread widget
            # mutations per test click. Qt does not raise for that; it
            # corrupts internal state and the process abort()s later, in
            # QApplication.exec(), with no Python frame to blame (crash.log:
            # 'Fatal Python error: Aborted', three times in one afternoon).
            # A signal emitted across threads is queued onto the GUI thread.
            self._capture_level.emit(min(100, int(rms * 200 * 100)))

        self._test_capture = AudioCapture(
            on_audio=on_audio,
            device_index=device,
            samplerate=cfg.audio.samplerate,
            channels=cfg.audio.channels,
        )
        self._test_capture.start()

        def finish():
            cap, self._test_capture = getattr(self, "_test_capture", None), None
            if cap is None:
                return                      # already stopped by _stop_test_capture
            cap.stop()
            if getattr(self, "test_capture_btn", None) is not None:
                self.test_capture_btn.setEnabled(True)
            if not self.isVisible():
                # The operator closed the dialog inside the 3 s window; a
                # modal box parented to a hidden dialog is a stuck window.
                self.level_bar.setValue(0)
                return
            level = int(self._test_max_rms * 200 * 100)
            if self._test_max_rms < 0.005:
                msg = (
                    f"Pouco ou nenhum áudio detectado durante 3s "
                    f"(pico={level}%). Confirme que está tocando algo "
                    f"pelo dispositivo selecionado."
                )
                QMessageBox.warning(self, "Teste de captura", msg)
            else:
                QMessageBox.information(
                    self, "Teste de captura",
                    f"Captura OK ✓\n\n"
                    f"Pico: {level}%\nChunks recebidos: {self._test_chunks}\n"
                    f"Dispositivo: {getattr(device, 'name', 'default')}"
                )
            self.level_bar.setValue(0)

        _QTimer.singleShot(3000, finish)

    def _build_appearance_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)

        # ---- Aparência (existing controls) ---------------------------------
        aparencia_group = QGroupBox("Aparência")
        layout = QFormLayout(aparencia_group)


        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(20, 100)
        self.opacity_label = QLabel()
        self.opacity_slider.valueChanged.connect(
            lambda v: self.opacity_label.setText(f"{v}%")
        )
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self.opacity_slider, 1)
        opacity_row.addWidget(self.opacity_label)
        opacity_w = QWidget()
        opacity_w.setLayout(opacity_row)
        layout.addRow("Opacidade do fundo:", opacity_w)

        self.width_slider = QSlider(Qt.Orientation.Horizontal)
        self.width_slider.setRange(40, 100)
        self.width_label = QLabel()
        self.width_slider.valueChanged.connect(
            lambda v: self.width_label.setText(f"{v}% da tela")
        )
        width_row = QHBoxLayout()
        width_row.addWidget(self.width_slider, 1)
        width_row.addWidget(self.width_label)
        width_w = QWidget()
        width_w.setLayout(width_row)
        layout.addRow("Largura:", width_w)

        self.primary_size = QSpinBox()
        self.primary_size.setRange(16, 96)
        layout.addRow("Tamanho fonte principal:", self.primary_size)

        self.secondary_size = QSpinBox()
        self.secondary_size.setRange(12, 64)
        layout.addRow("Tamanho fonte secundária:", self.secondary_size)

        self.primary_color_btn = ColorButton("#FFFFFF")
        layout.addRow("Cor texto principal:", self.primary_color_btn)
        self.secondary_color_btn = ColorButton("#CCCCCC")
        layout.addRow("Cor texto secundário:", self.secondary_color_btn)
        self.outline_color_btn = ColorButton("#000000")
        layout.addRow("Cor do contorno:", self.outline_color_btn)
        self.bg_color_btn = ColorButton("#000000")
        layout.addRow("Cor de fundo:", self.bg_color_btn)

        self.font_family_combo = QComboBox()
        try:
            from PyQt6.QtGui import QFontDatabase
            families = sorted(set(QFontDatabase.families()))
        except Exception:
            families = ["Segoe UI", "Arial", "Calibri", "Verdana", "Tahoma"]
        for fam in families:
            self.font_family_combo.addItem(fam, fam)
        layout.addRow("Família da fonte:", self.font_family_combo)

        self.padding_spin = QSpinBox()
        self.padding_spin.setRange(8, 64)
        self.padding_spin.setSuffix(" px")
        self.padding_spin.setToolTip("Espaço interno entre o texto e a borda da legenda.")
        layout.addRow("Padding interno:", self.padding_spin)

        preview = QPushButton("Visualizar legenda agora")
        preview.clicked.connect(self._on_preview)
        layout.addRow(preview)

        outer.addWidget(aparencia_group)
        outer.addStretch(1)
        return w

    def _build_layout_tab(self) -> QWidget:
        """Own tab, not a group at the bottom of Aparência: that tab already
        scrolls, and an option below the fold is an option nobody finds.

        Everything here used to live only in the tray menu (two boxes,
        screen) or nowhere at all (second box position, fixed band). The
        operator asked where the option was; the answer must be "here".
        """
        w = QWidget()
        outer = QVBoxLayout(w)
        intro = QLabel(
            "Tudo o que decide a legenda: o que aparece em cada fala, onde "
            "aparece, quantas caixas e que altura elas têm.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #888; padding: 4px 0 8px 0;")
        outer.addWidget(intro)

        proj_group = QGroupBox("Legenda")
        pl = QFormLayout(proj_group)

        # "Como mostrar" abre a tela porque e ele que define quantas linhas
        # cada fala ocupa -- todas as decisoes abaixo dependem disso. Estava em
        # Idiomas, e as duas abas se referiam uma a outra por escrito.
        self.mode_combo = QComboBox()
        for code, label in DISPLAY_MODES.items():
            self.mode_combo.addItem(label, code)
        self.mode_combo.setToolTip(
            "O QUE aparece em cada fala: só a tradução, com o idioma falado, "
            "ou bilíngue. Decide quantas linhas cada fala ocupa.")
        pl.addRow("Como mostrar:", self.mode_combo)

        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Empilhado — os idiomas um sobre o outro, numa caixa só", "stacked")
        self.layout_combo.addItem("Duas caixas — 1º idioma nesta caixa, os outros numa 2ª caixa", "split")
        self.layout_combo.setToolTip(
            "Com 2 idiomas de saída. Cada caixa se arrasta com o mouse para onde "
            "quiser (topo/rodapé, lado a lado, outra tela). Atalho: bandeja → "
            "'Bilíngue em duas caixas'.")
        pl.addRow("Com 2 idiomas de saída:", self.layout_combo)

        # As duas posicoes moram juntas de proposito: sao as duas metades da
        # mesma decisao. Enquanto `position` ficava em Aparencia e
        # `second_position` aqui, dava para escolher a MESMA posicao para as
        # duas sem nunca ver as duas escolhas na mesma tela -- e o resultado
        # eram duas bandas sobrepostas lidas como uma caixa empilhada.
        self.position_combo = QComboBox()
        for code, label in POSITIONS.items():
            self.position_combo.addItem(label, code)
        self.position_label = QLabel("Posição da legenda:")
        pl.addRow(self.position_label, self.position_combo)

        self.second_position_combo = QComboBox()
        for code, label in POSITIONS.items():
            self.second_position_combo.addItem(label, code)
        pl.addRow("Posição da 2ª caixa:", self.second_position_combo)
        self.layout_combo.currentIndexChanged.connect(
            lambda _i: self._sync_layout_labels())
        # A posicao da 1a banda sai da lista da 2a: duas bandas no mesmo lugar
        # ficam 100% sobrepostas e lem como UMA caixa com os idiomas
        # empilhados. Oferecer a opcao e depois corrigi-la no backend faria a
        # tela mentir sobre o que vai acontecer; entao ela nao e oferecida.
        self.position_combo.currentIndexChanged.connect(
            lambda _i: self._sync_second_position_choices())
        self._sync_layout_labels()
        self._sync_second_position_choices()

        self.screen_combo = QComboBox()
        self._populate_screens()
        self.screen_combo.setToolTip(
            "Em modo 'estender', o projetor é a 2ª tela. Atalho: bandeja → 'Tela da legenda'.")
        pl.addRow("Tela da legenda:", self.screen_combo)

        self.stable_height_check = QCheckBox(
            "Banda de altura fixa (a legenda não pula quando o texto cresce)")
        pl.addRow(self.stable_height_check)
        self.reserved_lines_spin = QSpinBox()
        self.reserved_lines_spin.setRange(1, 12)
        self.reserved_lines_spin.setSuffix(" linha(s)")
        self.reserved_lines_spin.setToolTip(
            "PISO da altura, nao o valor final. Quem manda e o historico: a "
            "banda cresce sozinha para caber as linhas anteriores pedidas. "
            "Nunca fica menor que uma fala completa no modo escolhido.")
        pl.addRow("Altura mínima da banda:", self.reserved_lines_spin)
        self.stable_height_check.toggled.connect(self.reserved_lines_spin.setEnabled)

        # Historico: mora aqui porque e ele que decide a altura da banda desde
        # e56e03a. Ficava em Aparencia, a duas abas de distancia do piso de
        # altura com que disputava o mesmo numero -- e perdia em silencio.
        self.max_history_spin = QSpinBox()
        self.max_history_spin.setRange(0, 5)
        self.max_history_spin.setSuffix(" fala(s)")
        self.max_history_spin.setToolTip(
            "0 = só a fala atual. 1-5 = mostra também as anteriores, esmaecidas.\n"
            "A banda cresce para caber o que você pedir aqui.")
        pl.addRow("Falas anteriores visíveis:", self.max_history_spin)

        # max_chars NAO quebra linha -- isso quem faz e a largura da banda com
        # o tamanho da fonte. Ele CORTA a fala, mantendo o FINAL e pondo "..."
        # na frente. Medido: com max_chars=60, uma fala de 122 caracteres
        # aparece como "...que e esta parte que voce esta lendo agora no fim da
        # frase." E uma valvula contra fala que nao termina, e o rotulo tem de
        # dizer isso: a primeira versao desta linha dizia "Maximo por linha" e
        # citava as diretrizes da BBC, que sao sobre quebra de linha. Errado.
        self.max_chars_spin = QSpinBox()
        self.max_chars_spin.setRange(0, 600)
        self.max_chars_spin.setSingleStep(20)
        self.max_chars_spin.setSuffix(" caracteres")
        self.max_chars_spin.setSpecialValueText("nunca cortar")
        self.max_chars_spin.setToolTip(
            "Fala mais longa que isto aparece CORTADA NO COMEÇO, mostrando o "
            "final com \"…\" na frente. Não é quebra de linha — quem quebra a "
            "linha é a largura da banda com o tamanho da fonte.\n"
            "É uma válvula contra fala que não termina. 220 = praticamente "
            "nunca corta. 0 = nunca corta.")
        pl.addRow("Cortar fala acima de:", self.max_chars_spin)

        self.concat_gap_spin = QSpinBox()
        self.concat_gap_spin.setRange(0, 5000)
        self.concat_gap_spin.setSingleStep(250)
        self.concat_gap_spin.setSuffix(" ms")
        self.concat_gap_spin.setToolTip(
            "Gap máximo para juntar duas falas curtas numa linha "
            "(OpenRouter/Whisper).\n0 desativa. O Azure Streaming ignora este "
            "valor — sempre cria linha nova."
        )
        pl.addRow("Juntar falas próximas:", self.concat_gap_spin)

        self.click_through_check = QCheckBox(
            "Click-through (a legenda não bloqueia cliques no que está atrás)")
        pl.addRow(self.click_through_check)

        preview = QPushButton("Visualizar legenda agora")
        preview.clicked.connect(self._on_preview)
        pl.addRow(preview)

        outer.addWidget(proj_group)
        outer.addStretch(1)

        return w

    def _populate_screens(self) -> None:
        """Fill the screen combo from the monitors connected RIGHT NOW.

        Used to run once at dialog build: a projector plugged in after the
        dialog was opened never appeared in the list (and one unplugged stayed
        there as a ghost). Called again from showEvent so reopening the dialog
        always shows the current layout. The current selection is preserved —
        including a configured monitor that is momentarily disconnected, which
        is kept as an explicit entry instead of being silently dropped.
        """
        current = self.screen_combo.currentData()
        if current is None:
            current = self.config.overlay.screen_name or ""
        self.screen_combo.blockSignals(True)
        try:
            self.screen_combo.clear()
            self.screen_combo.addItem("Tela principal", "")
            for i, s in enumerate(QGuiApplication.screens()):
                geo = s.geometry()
                self.screen_combo.addItem(
                    f"{i + 1}: {s.name()} ({geo.width()}×{geo.height()})", s.name())
            idx = self.screen_combo.findData(current or "")
            if idx < 0 and current:
                # Configured monitor not plugged in right now: keep the choice.
                self.screen_combo.addItem(
                    f"{current} (não conectada agora)", current)
                idx = self.screen_combo.count() - 1
            self.screen_combo.setCurrentIndex(max(0, idx))
        finally:
            self.screen_combo.blockSignals(False)

    def _build_about_tab(self) -> QWidget:
        from constants import APP_VERSION
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel(f"<h3>CaptionBand v{APP_VERSION}</h3>"))
        layout.addWidget(QLabel(
            "Tradução simultânea de webinars no Teams via Azure / OpenRouter / "
            "Google Speech / Whisper local."
        ))

        # Usage / cost dashboard
        usage_group = QGroupBox("Uso este mês (estimativa local)")
        usage_layout = QVBoxLayout(usage_group)
        try:
            from datetime import datetime

            import usage_tracker
            summary = usage_tracker.summary()
            ym = datetime.now(UTC).strftime("%Y-%m")
            usage_layout.addWidget(QLabel(f"<small>Período: {ym} (UTC)</small>"))
            html_lines = ["<table cellspacing='6' cellpadding='2'>"]
            html_lines.append(
                "<tr><th align='left'>Provedor</th>"
                "<th>Horas</th><th>Free restante</th>"
                "<th>Custo USD</th></tr>"
            )
            for prov, info in summary.items():
                hours = info["hours_month"]
                free = info["free_remaining_hours"]
                cost = info["estimated_cost_usd"]
                free_str = "∞" if free == float("inf") else f"{free:.2f}h"
                html_lines.append(
                    f"<tr><td>{PROVIDER_LABELS.get(prov, prov)}</td>"
                    f"<td align='right'>{hours:.2f}h</td>"
                    f"<td align='right'>{free_str}</td>"
                    f"<td align='right'>${cost:.2f}</td></tr>"
                )
            html_lines.append("</table>")
            usage_layout.addWidget(QLabel("\n".join(html_lines)))
            usage_layout.addWidget(QLabel(
                "<small>Estimativa baseada em tempo de áudio enviado pelo app local. "
                "Custos reais são faturados pelos provedores diretamente.</small>"
            ))
        except Exception:
            usage_layout.addWidget(QLabel("<i>Sem dados de uso ainda.</i>"))
        layout.addWidget(usage_group)

        layout.addWidget(QLabel(f"<b>Config:</b> <code>{config_path()}</code>"))
        layout.addWidget(QLabel(f"<b>Logs:</b> <code>{log_path()}</code>"))
        layout.addWidget(QLabel(
            "<small>GitHub: <a href='https://github.com/caiofabio1/captionband'>"
            "caiofabio1/captionband</a></small>"
        ))
        layout.addStretch(1)
        return w

    # ------------------------------------------------------------------ logic

    def _on_provider_changed(self, idx: int) -> None:
        code = self.provider_combo.itemData(idx)
        self.provider_stack.setCurrentIndex(self._provider_pages.get(code, 0))
        self.provider_hint.setText(PROVIDER_HINTS.get(code, ""))

    def _populate_devices(self) -> None:
        self.device_combo.clear()
        self.device_combo.addItem("(Padrão do sistema)", None)
        try:
            from audio_capture import list_output_devices

            for dev in list_output_devices():
                self.device_combo.addItem(f"{dev['name']}  [{dev['channels']}ch]", dev["name"])
        except Exception:
            log.exception("could not list audio devices")

    def _load_from_config(self) -> None:
        idx = self.provider_combo.findData(self.config.provider)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        else:
            self.provider_combo.setCurrentIndex(0)
        self._on_provider_changed(self.provider_combo.currentIndex())
        fb = (self.config.fallback_providers or [""])[0]
        self.fallback_combo.setCurrentIndex(max(0, self.fallback_combo.findData(fb)))

        # Credenciais tab inputs
        self.openrouter_api_key_input.setText(self.config.openrouter_api_key)
        self.openai_api_key_input.setText(self.config.openai_api_key)
        self.azure_key_input_credentials.setText(self.config.azure_speech_key)
        self.azure_region_input_credentials.setText(self.config.azure_speech_region)
        self.google_creds_input_credentials.setText(self.config.google_credentials_json)
        self.google_project_input_credentials.setText(self.config.google_project_id)

        # Azure streaming controls
        self.azure_streaming_check.setChecked(self.config.azure_streaming_mode)
        idx = self.azure_streaming_lang_combo.findData(self.config.azure_streaming_language)
        if idx >= 0:
            self.azure_streaming_lang_combo.setCurrentIndex(idx)
        quick = set(self.config.azure_quick_languages or [])
        for i in range(self.azure_quick_langs_list.count()):
            it = self.azure_quick_langs_list.item(i)
            it.setSelected(it.data(Qt.ItemDataRole.UserRole) in quick)
        self.azure_switch_hotkey_input.setText(self.config.azure_switch_hotkey or "")
        self.vocabulary_weight_spin.setValue(float(self.config.vocabulary_weight))
        self._refresh_vocabulary_count()

        # Provedor tab model combos
        idx = self.openrouter_stt_model_combo.findData(self.config.openrouter_stt_model)
        if idx >= 0:
            self.openrouter_stt_model_combo.setCurrentIndex(idx)
        idx = self.openrouter_translation_model_combo.findData(self.config.openrouter_translation_model)
        if idx >= 0:
            self.openrouter_translation_model_combo.setCurrentIndex(idx)

        self.google_location_edit.setText(self.config.google_location)
        self.google_recognizer_edit.setText(self.config.google_recognizer_id)

        idx = self.whisper_model_combo.findData(self.config.whisper_model)
        if idx >= 0:
            self.whisper_model_combo.setCurrentIndex(idx)
        idx = self.whisper_device_combo.findData(self.config.whisper_device)
        if idx >= 0:
            self.whisper_device_combo.setCurrentIndex(idx)
        idx = self.whisper_compute_combo.findData(self.config.whisper_compute_type)
        if idx >= 0:
            self.whisper_compute_combo.setCurrentIndex(idx)

        self.chunk_seconds_spin.setValue(self.config.chunk_seconds)

        idx = self.mode_combo.findData(self.config.display_mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)

        idx = self.position_combo.findData(self.config.overlay.position)
        if idx >= 0:
            self.position_combo.setCurrentIndex(idx)

        self.opacity_slider.setValue(int(self.config.overlay.background_opacity * 100))
        self.opacity_label.setText(f"{self.opacity_slider.value()}%")
        self.width_slider.setValue(int(self.config.overlay.width_ratio * 100))
        self.width_label.setText(f"{self.width_slider.value()}% da tela")
        self.primary_size.setValue(self.config.overlay.primary_font_size)
        self.secondary_size.setValue(self.config.overlay.secondary_font_size)
        self.primary_color_btn.color_hex = self.config.overlay.primary_color
        self.primary_color_btn._update_swatch()
        self.secondary_color_btn.color_hex = self.config.overlay.secondary_color
        self.secondary_color_btn._update_swatch()
        self.outline_color_btn.color_hex = self.config.overlay.outline_color
        self.outline_color_btn._update_swatch()
        self.bg_color_btn.color_hex = self.config.overlay.background_color
        self.bg_color_btn._update_swatch()
        self.click_through_check.setChecked(self.config.overlay.click_through)
        self.max_history_spin.setValue(self.config.overlay.max_history)
        ov = self.config.overlay
        self.layout_combo.setCurrentIndex(
            max(0, self.layout_combo.findData("split" if ov.split_languages else "stacked")))
        self.second_position_combo.setCurrentIndex(
            max(0, self.second_position_combo.findData(ov.second_position or "top")))
        self._sync_layout_labels()
        # Um config.json que ja traz as duas caixas na mesma posicao cai aqui:
        # sem este sync a tela abriria mostrando a escolha conflitante.
        self._sync_second_position_choices()
        idx = self.screen_combo.findData(ov.screen_name or "")
        if idx < 0 and ov.screen_name:
            # Configured monitor not plugged in right now: keep the choice.
            self.screen_combo.addItem(f"{ov.screen_name} (não conectada agora)", ov.screen_name)
            idx = self.screen_combo.count() - 1
        self.screen_combo.setCurrentIndex(max(0, idx))
        self.stable_height_check.setChecked(bool(ov.stable_height))
        self.reserved_lines_spin.setValue(int(ov.reserved_lines))
        self.reserved_lines_spin.setEnabled(bool(ov.stable_height))
        self.concat_gap_spin.setValue(self.config.overlay.concat_gap_ms)
        self.max_chars_spin.setValue(int(self.config.overlay.max_chars))
        idx = self.font_family_combo.findData(self.config.overlay.font_family)
        if idx >= 0:
            self.font_family_combo.setCurrentIndex(idx)
        self.padding_spin.setValue(self.config.overlay.padding)
        self.auto_start_check.setChecked(self.config.auto_start_translation)

        if self.config.audio.device_name:
            idx = self.device_combo.findData(self.config.audio.device_name)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)

    def _build_config(self) -> AppConfig:
        current_provider = self.provider_combo.currentData() or "azure"
        # Start from the CURRENT config, not a blank AppConfig: any field this
        # window has no widget for (fallback_providers, stable_height, …) must
        # survive a save. Building from scratch silently reset the fallback
        # chain to [] on every "Salvar" — which is why it was always empty.
        fallback = self.fallback_combo.currentData() or ""
        return replace(
            self.config,
            fallback_providers=[fallback] if fallback else [],
            vocabulary_weight=self.vocabulary_weight_spin.value(),
            provider=current_provider,
            openrouter_api_key=self.openrouter_api_key_input.text().strip(),
            openrouter_stt_model=self.openrouter_stt_model_combo.currentData() or "openai/whisper-1",
            openrouter_translation_model=self.openrouter_translation_model_combo.currentData() or "openai/gpt-oss-120b",
            azure_speech_key=self.azure_key_input_credentials.text().strip(),
            azure_speech_region=self.azure_region_input_credentials.text().strip() or "brazilsouth",
            azure_streaming_mode=self.azure_streaming_check.isChecked(),
            azure_streaming_language=(
                self.azure_streaming_lang_combo.currentData() or "pt-BR"
            ),
            azure_quick_languages=[
                self.azure_quick_langs_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.azure_quick_langs_list.count())
                if self.azure_quick_langs_list.item(i).isSelected()
            ] or ["pt-BR", "en-US", "es-ES"],
            azure_switch_hotkey=self.azure_switch_hotkey_input.text().strip().lower(),
            openai_api_key=self.openai_api_key_input.text().strip(),
            google_credentials_json=self.google_creds_input_credentials.text().strip(),
            google_project_id=self.google_project_input_credentials.text().strip(),
            google_location=self.google_location_edit.text().strip() or "global",
            google_recognizer_id=self.google_recognizer_edit.text().strip() or "_",
            whisper_model=self.whisper_model_combo.currentData() or "small",
            whisper_device=self.whisper_device_combo.currentData() or "cpu",
            whisper_compute_type=self.whisper_compute_combo.currentData() or "int8",
            chunk_seconds=self.chunk_seconds_spin.value(),
            source_languages=self.sources_list.selected_codes(),
            target_languages=self.targets_list.selected_codes(),
            display_mode=self.mode_combo.currentData() or "translations_only",
            audio=replace(
                self.config.audio,
                device_name=self.device_combo.currentData(),
            ),
            overlay=replace(
                self.config.overlay,
                position=self.position_combo.currentData() or "bottom",
                width_ratio=self.width_slider.value() / 100.0,
                background_opacity=self.opacity_slider.value() / 100.0,
                primary_font_size=self.primary_size.value(),
                secondary_font_size=self.secondary_size.value(),
                primary_color=self.primary_color_btn.color_hex,
                secondary_color=self.secondary_color_btn.color_hex,
                outline_color=self.outline_color_btn.color_hex,
                background_color=self.bg_color_btn.color_hex,
                padding=self.padding_spin.value(),
                max_history=self.max_history_spin.value(),
                click_through=self.click_through_check.isChecked(),
                font_family=self.font_family_combo.currentData() or "Segoe UI",
                concat_gap_ms=self.concat_gap_spin.value(),
            max_chars=self.max_chars_spin.value(),
                split_languages=self.layout_combo.currentData() == "split",
                second_position=self.second_position_combo.currentData() or "top",
                screen_name=self.screen_combo.currentData() or "",
                stable_height=self.stable_height_check.isChecked(),
                reserved_lines=self.reserved_lines_spin.value(),
            ),
            auto_start_translation=self.auto_start_check.isChecked(),
        )

    # The preview band closes itself after this long.
    PREVIEW_MS = 8000

    def _sync_layout_labels(self) -> None:
        """Em duas caixas, `position` posiciona a 1a CAIXA, nao "a legenda"."""
        dividido = self.layout_combo.currentData() == "split"
        self.second_position_combo.setEnabled(dividido)
        self.position_label.setText(
            "Posição da 1ª caixa:" if dividido else "Posição da legenda:")

    def _sync_second_position_choices(self) -> None:
        """Grey out, in the second box's list, wherever the first band sits."""
        taken = self.position_combo.currentData()
        model = self.second_position_combo.model()
        for i in range(self.second_position_combo.count()):
            item = model.item(i)
            if item is not None:
                item.setEnabled(self.second_position_combo.itemData(i) != taken)
        if self.second_position_combo.currentData() == taken:
            for i in range(self.second_position_combo.count()):
                if self.second_position_combo.itemData(i) != taken:
                    self.second_position_combo.setCurrentIndex(i)
                    break

    def _on_preview(self) -> None:
        """Show ONE preview band with the current (unsaved) appearance.

        Every click used to create a fresh overlay window and keep it — with
        nothing wired to close it, not even its own × button. After three
        clicks the operator had three black bands that never went away.

        Note: 0x8001010D (RPC_E_CANTCALLOUT_ININPUTSYNCCALL) shows up in
        crash.log on this path. It is a FIRST-CHANCE exception that faulthandler
        reports and COM then handles — the launch that logged it went on to
        record "app quit normally". Do not chase it as a crash.
        """
        try:
            from PyQt6.QtCore import QTimer

            from overlay_qt import CaptionOverlay
            from translator import split_overlay_configs

            cfg = self._build_config()
            # One preview window per box — the two-box layout previews as two.
            cfgs = [c for c in split_overlay_configs(cfg) if c is not None]
            previews = getattr(self, "_previews", None)
            if previews is None:
                previews = []
                self._previews = previews
                self._preview_timer = QTimer(self)
                self._preview_timer.setSingleShot(True)
                self._preview_timer.timeout.connect(self._close_preview)
            while len(previews) < len(cfgs):
                p = CaptionOverlay(cfgs[len(previews)], parent=self)
                p.close_requested.connect(self._close_preview)
                previews.append(p)
            sample = {
                "en": "This is what the caption will look like on screen.",
                "es": "Así se verá el subtítulo en la pantalla.",
                "pt": "Assim ficará a legenda na tela.",
                "fr": "Voici à quoi ressemblera le sous-titre.",
            }
            translations = {t: sample.get(t, sample["en"]) for t in cfg.target_languages} \
                or {"es": sample["es"]}
            for i, p in enumerate(previews):
                if i >= len(cfgs):
                    p.hide()
                    continue
                p.clear()
                p.apply_config(cfgs[i])
                p.show()
                p.push_caption("Assim ficará a legenda na tela.", translations,
                               detected_language="pt-BR", is_final=True)
            self._preview_timer.start(self.PREVIEW_MS)
        except Exception as exc:
            QMessageBox.warning(self, "Preview", f"Erro ao gerar preview: {exc}")

    def _close_preview(self) -> None:
        for preview in getattr(self, "_previews", None) or []:
            preview.hide()
        timer = getattr(self, "_preview_timer", None)
        if timer is not None:
            timer.stop()

    def _stop_test_capture(self) -> None:
        """Kill a running 'Testar captura' stream immediately.

        Closing the dialog inside the 3 s window used to leave the WASAPI
        stream recording until the timer fired, and then pop a message box
        parented to a hidden dialog.
        """
        cap, self._test_capture = getattr(self, "_test_capture", None), None
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass
        btn = getattr(self, "test_capture_btn", None)
        if btn is not None:
            btn.setEnabled(True)

    def hideEvent(self, event):  # type: ignore[override]
        # Covers Salvar (accept), Cancelar and the window's ×: neither the
        # preview nor a test capture outlives the dialog that owns it.
        self._close_preview()
        self._stop_test_capture()
        super().hideEvent(event)

    def _on_save(self) -> None:
        cfg = self._build_config()
        if not cfg.source_languages:
            QMessageBox.warning(self, "Configuração", "Selecione ao menos um idioma falado.")
            return
        if not cfg.target_languages:
            QMessageBox.warning(self, "Configuração", "Selecione ao menos um idioma de saída.")
            return
        if not cfg.is_valid():
            QMessageBox.warning(
                self, "Configuração",
                f"Preencha as credenciais do provedor selecionado ({cfg.provider})."
            )
            return
        if cfg.provider == "google" and len(cfg.source_languages) > 4:
            QMessageBox.warning(
                self, "Configuração",
                "Google Speech v2 suporta no máximo 4 idiomas-fonte. Reduza a seleção."
            )
            return
        if cfg.provider == "azure" and len(cfg.source_languages) > 10:
            QMessageBox.warning(
                self, "Configuração",
                "Azure Continuous LID suporta no máximo 10 idiomas-fonte."
            )
            return
        try:
            save_config(cfg)
        except Exception as exc:
            QMessageBox.critical(self, "Configuração", f"Não foi possível salvar: {exc}")
            return
        self.config = cfg
        self.config_saved.emit(cfg)
        self.accept()


def _demo() -> None:  # pragma: no cover
    import sys

    from config import load_config

    app = QApplication(sys.argv)
    win = SettingsWindow(load_config())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    _demo()
