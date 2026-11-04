"""Configure the local LLM endpoint (Ollama / OpenAI-compatible server)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..llm import DEFAULT_URLS, PROVIDERS, LlmClient, LlmConfig, LlmError
from .geometry import fit_dialog
from .workers import run_async


class LlmSettingsDialog(QDialog):
    def __init__(self, config: LlmConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Local LLM")
        fit_dialog(self, 560, 320)

        self.provider = QComboBox()
        for p in PROVIDERS:
            self.provider.addItem({"ollama": "Ollama", "openai": "OpenAI-compatible server"}[p], p)
        self.provider.setCurrentIndex(max(self.provider.findData(config.provider), 0))
        self.provider.currentIndexChanged.connect(self._provider_changed)

        self.base_url = QLineEdit(config.base_url)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(config.model)
        self.model.setCurrentText(config.model)
        fetch = QPushButton("List models")
        fetch.clicked.connect(self._fetch_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        model_row.addWidget(fetch)

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(config.temperature)
        self.temperature.setToolTip("0 gives the most reproducible rewrites")
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 1800)
        self.timeout.setValue(int(config.timeout))
        self.timeout.setSuffix(" s")
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(128, 32768)
        self.max_tokens.setValue(config.max_tokens)
        self.api_key = QLineEdit(config.api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("only needed by some servers; never stored on disk")

        test = QPushButton("Test connection")
        test.clicked.connect(self._test)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Provider", self.provider)
        form.addRow("Server URL", self.base_url)
        form.addRow("Model", model_row)
        form.addRow("Temperature", self.temperature)
        form.addRow("Timeout", self.timeout)
        form.addRow("Max tokens", self.max_tokens)
        form.addRow("API key", self.api_key)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        intro = QLabel("The query, the declared schema and the static findings are sent to this server. "
                       "Point it at a model running on your machine so nothing leaves it.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(test)
        row.addWidget(self.status, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------ helpers
    def config(self) -> LlmConfig:
        return LlmConfig(provider=self.provider.currentData(), base_url=self.base_url.text().strip(),
                         model=self.model.currentText().strip(), temperature=self.temperature.value(),
                         timeout=float(self.timeout.value()), max_tokens=self.max_tokens.value(),
                         api_key=self.api_key.text())

    def _provider_changed(self) -> None:
        current = self.base_url.text().strip()
        if current in DEFAULT_URLS.values() or not current:
            self.base_url.setText(DEFAULT_URLS[self.provider.currentData()])

    def _test(self) -> None:
        self.status.setText("Contacting the server…")
        client = LlmClient(self.config())
        run_async(self, client.ping, lambda msg: self.status.setText(msg),
                  lambda msg: self.status.setText(f"<span style='color:#d73027'>{msg}</span>"))

    def _fetch_models(self) -> None:
        self.status.setText("Loading the model list…")
        client = LlmClient(self.config())

        def done(models: list[str]) -> None:
            current = self.model.currentText()
            self.model.clear()
            self.model.addItems(models or [current])
            self.model.setCurrentText(current if current in models else (models[0] if models else current))
            self.status.setText(f"{len(models)} model(s) available." if models else "The server lists no models.")

        def failed(msg: str) -> None:
            self.status.setText(f"<span style='color:#d73027'>{msg}</span>")

        run_async(self, client.models, done, failed)


def describe_error(exc: Exception) -> str:  # pragma: no cover - trivial
    return str(exc) if isinstance(exc, LlmError) else f"{exc.__class__.__name__}: {exc}"
