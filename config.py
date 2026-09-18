"""Configuration management for CaptionBand.

Stores user settings in %LOCALAPPDATA%\\CaptionBand\\config.json on Windows.
Provides a dataclass for type-safe access and a loader/saver pair.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import MISSING, asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "CaptionBand"


# Folders this app used before it was renamed. Settings live in a directory
# named after the app, so a rename orphans them: the operator would launch a
# freshly renamed build and find every language, layout and font back at the
# default, with their real config sitting untouched next door.
LEGACY_APP_NAMES = ("TeamsLiveTranslation",)


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    p = Path(base) / APP_NAME
    first_run = not p.exists()
    p.mkdir(parents=True, exist_ok=True)
    if first_run:
        _adopt_previous_settings(Path(base), p)
    return p


def _adopt_previous_settings(base: Path, target: Path) -> None:
    """Copy config.json over from a previous name, once, on first run."""
    import shutil
    for legacy in LEGACY_APP_NAMES:
        old_cfg = base / legacy / "config.json"
        if not old_cfg.is_file():
            continue
        try:
            shutil.copy2(old_cfg, target / "config.json")
            log.info("adopted settings from the previous app folder %s", legacy)
        except OSError:
            log.exception("could not adopt settings from %s", legacy)
        return


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_path() -> Path:
    return app_data_dir() / "app.log"


@dataclass
class AudioConfig:
    device_name: str | None = None
    samplerate: int = 16000
    channels: int = 1


@dataclass
class OverlayConfig:
    position: str = "bottom"
    width_ratio: float = 0.8
    background_opacity: float = 0.85
    primary_font_size: int = 36
    secondary_font_size: int = 22
    primary_color: str = "#FFFFFF"
    secondary_color: str = "#CCCCCC"
    outline_color: str = "#000000"
    background_color: str = "#000000"
    padding: int = 24
    # max_history: how many previous utterances to keep visible above the
    # current one (0 = single line only, 1 = +1 prev, 2 = +2 prev, 3 = +3).
    max_history: int = 2
    click_through: bool = False
    fade_ms: int = 200
    max_chars: int = 220
    font_family: str = "Segoe UI"
    # Auto-concatenate consecutive utterances within this gap if same language.
    # Set to 0 to disable.
    concat_gap_ms: int = 1500
    # If true and provider supports it (Google), enable streaming partials
    # so original text appears as the speaker talks.
    streaming_partials: bool = True
    # Reserve a fixed caption band instead of resizing the window on every
    # event. Resizing per event makes the whole caption jump on screen as
    # text grows and shrinks; on a projector that is what the room notices
    # before they notice the words. The band is sized once from the config.
    stable_height: bool = True
    # How many text lines the reserved band is tall. Only used when
    # stable_height is on.
    reserved_lines: int = 3
    # Anchor the newest line to a FIXED position and let older text push
    # upward, instead of letting every line shift when the block grows.
    # Only meaningful with stable_height.
    anchor_newest: bool = True
    # Bilingual in TWO boxes instead of stacked lines: the first output
    # language in this band, the others in a second band at
    # `second_position`. Each box is dragged independently, so "one on top,
    # one at the bottom" and "side by side" are both just where you drop them.
    split_languages: bool = False
    second_position: str = "top"
    # Which monitor carries the caption (QScreen.name()). Empty = primary.
    # At an event the projector is usually the SECOND screen in "extend"
    # mode, and a caption pinned to the laptop panel is invisible to the room.
    screen_name: str = ""


@dataclass
class AppConfig:
    provider: str = "azure"

    azure_speech_key: str = ""
    azure_speech_region: str = "brazilsouth"
    # When True, AzureProvider switches to single-language streaming mode
    # (incremental "word-by-word with correction" via the `recognizing` event).
    # Trade-off: must know the source language up front (no Continuous LID).
    azure_streaming_mode: bool = False
    # Currently active source language when streaming_mode is on. Tray menu /
    # global hotkey can change this at runtime via TrayApp.switch_source_language().
    azure_streaming_language: str = "pt-BR"
    # Languages exposed in the tray's "Idioma de origem ▸" submenu and cycled
    # through by the global hotkey. Subset of KNOWN_LANGUAGES keys.
    azure_quick_languages: list[str] = field(default_factory=lambda: ["pt-BR", "en-US", "es-ES"])
    # Global hotkey that cycles through azure_quick_languages. Empty = disabled.
    azure_switch_hotkey: str = "f9"



    openai_api_key: str = ""

    openrouter_api_key: str = ""
    openrouter_stt_model: str = "google/gemini-3.5-flash-lite"
    openrouter_translation_model: str = "openai/gpt-oss-120b"

    google_credentials_json: str = ""
    google_project_id: str = ""
    google_location: str = "global"
    google_recognizer_id: str = "_"

    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    chunk_seconds: float = 4.0

    # Ordered list of providers to try if the primary fails repeatedly.
    # Empty = no fallback. The first valid provider in the list is used as fallback.
    fallback_providers: list[str] = field(default_factory=list)

    source_languages: list[str] = field(default_factory=lambda: ["pt-BR", "es-ES", "en-US"])
    target_languages: list[str] = field(default_factory=lambda: ["es"])
    display_mode: str = "original_plus_translation"
    audio: AudioConfig = field(default_factory=AudioConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    auto_start_translation: bool = False

    def is_valid(self) -> bool:
        if not self.source_languages or not self.target_languages:
            return False
        if self.provider == "azure":
            return bool(self.azure_speech_key and self.azure_speech_region)
        if self.provider == "google":
            return bool(self.google_credentials_json and self.google_project_id)
        if self.provider == "whisper_local":
            return bool(self.whisper_model)
        if self.provider == "openrouter":
            return bool(self.openrouter_api_key)
        if self.provider == "openai_realtime":
            return bool(self.openai_api_key)
        return False


DISPLAY_MODES = {
    "translations_only": "Só a tradução (1 idioma de saída)",
    "original_plus_translation": "Idioma falado + tradução(ões)",
    "translations_only_multi": "Bilíngue: só as traduções, sem o idioma falado (ex.: EN + ES)",
}

POSITIONS = {
    "top": "Topo",
    "middle": "Centro",
    "bottom": "Inferior",
}

KNOWN_LANGUAGES = {
    "pt-BR": "Português (Brasil)",
    "pt-PT": "Português (Portugal)",
    "en-US": "Inglês (EUA)",
    "en-GB": "Inglês (Reino Unido)",
    "es-ES": "Espanhol (Espanha)",
    "es-MX": "Espanhol (México)",
    "fr-FR": "Francês",
    "it-IT": "Italiano",
    "de-DE": "Alemão",
    "zh-CN": "Chinês (Mandarim)",
    "ja-JP": "Japonês",
}

KNOWN_TARGETS = {
    "es": "Espanhol",
    "en": "Inglês",
    "pt": "Português",
    "fr": "Francês",
    "it": "Italiano",
    "de": "Alemão",
    "zh-Hans": "Chinês Simplificado",
    "ja": "Japonês",
}

WHISPER_MODEL_OPTIONS = {
    "tiny": "Tiny (~75 MB) - rápido, baixa precisão",
    "base": "Base (~145 MB) - leve",
    "small": "Small (~480 MB) - recomendado CPU",
    "medium": "Medium (~1.5 GB) - bom equilíbrio",
    "large-v3": "Large v3 (~3 GB) - máxima qualidade",
    "large-v3-turbo": "Large v3 Turbo (~1.6 GB) - rápido + alta qualidade (GPU)",
}

WHISPER_DEVICE_OPTIONS = {
    "cpu": "CPU (sempre funciona)",
    "cuda": "CUDA (NVIDIA, mais rápido)",
}

WHISPER_COMPUTE_TYPES = {
    "int8": "int8 (CPU recomendado)",
    "int8_float16": "int8_float16 (GPU)",
    "float16": "float16 (GPU)",
    "float32": "float32 (qualidade máxima, lento)",
}

SECRET_FIELDS = (
    "azure_speech_key", "google_credentials_json",
    "openai_api_key", "openrouter_api_key",
)


class _Invalid:
    """Sentinel for a config value that cannot be coerced to its field type."""


_INVALID = _Invalid()


def _default_of(f) -> object:
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[attr-defined]
        return f.default_factory()  # type: ignore[misc]
    return None


def _coerce_value(value: object, default: object) -> object:
    """Best-effort coercion of one JSON value to the type of its default.

    Returns _INVALID when the value is unusable, so the caller falls back to
    the field's default. The point: a config.json edited by hand (or written
    by an older/newer build) with `"width_ratio": "0.8"` must not abort the
    whole load — and must not reach the overlay as a string either.
    """
    if value is None:
        # None is only meaningful for optional fields (default None).
        return None if default is None else _INVALID
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
            return value.strip().lower() in ("true", "1")
        if isinstance(value, (int, float)):
            return bool(value)
        return _INVALID
    if isinstance(default, float):
        try:
            return float(value)  # accepts "0.8", 1, 0.8
        except (TypeError, ValueError):
            return _INVALID
    if isinstance(default, int):
        if isinstance(value, bool):
            return _INVALID
        try:
            return int(value)
        except (TypeError, ValueError):
            return _INVALID
    if isinstance(default, str):
        return value if isinstance(value, str) else _INVALID
    if isinstance(default, list):
        # A bare string here would explode the providers downstream ("pt-BR"
        # iterated as characters); only real sequences are accepted.
        return list(value) if isinstance(value, (list, tuple)) else _INVALID
    if default is None:
        # Optional field (e.g. device_name: str | None). Accept scalars.
        return value if isinstance(value, (str, int, float, bool)) else _INVALID
    return value


def _coerce_fields(model, raw: dict) -> dict:
    """Filter raw JSON down to the dataclass's fields, coercing per field.

    One rotten field falls back to its default (with a warning) instead of
    aborting the load of every other setting — the previous `Model(**raw)`
    raised TypeError on the first bad value and the app booted on factory
    defaults with nothing recovered.
    """
    out: dict = {}
    for name, f in model.__dataclass_fields__.items():
        if name not in raw:
            continue
        value = _coerce_value(raw[name], _default_of(f))
        if value is _INVALID:
            log.warning("config field %r has an unusable value %r; "
                        "falling back to the default", name, raw[name])
            continue
        out[name] = value
    return out


def load_config() -> AppConfig:
    """Load AppConfig from disk, hydrating secret fields from the OS keyring.

    Migration: if the JSON still contains secrets in plaintext (older versions),
    we move them to keyring and clear them from disk on the next save_config().
    """
    path = config_path()
    if not path.exists():
        return _hydrate_secrets(AppConfig())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # Valid JSON that is not an object ([...], "abc", 42) used to raise
        # AttributeError on raw.pop() OUTSIDE this try and kill the boot.
        if not isinstance(raw, dict):
            raise ValueError(f"config root is {type(raw).__name__}, not an object")
        audio_raw = raw.pop("audio", {}) or {}
        if not isinstance(audio_raw, dict):
            raise ValueError(f'"audio" is {type(audio_raw).__name__}, not an object')
        overlay_raw = raw.pop("overlay", {}) or {}
        if not isinstance(overlay_raw, dict):
            raise ValueError(f'"overlay" is {type(overlay_raw).__name__}, not an object')
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        # Do NOT silently hand back defaults. A truncated config.json reset
        # every setting of the event — languages, layout, screen, fonts — with
        # no message anywhere, and the operator would only find out by looking
        # at the projected band. Keep the broken file so it can be recovered
        # by hand, and say so loudly in the log.
        log.error("config.json is unreadable (%s); falling back to defaults", exc)
        try:
            broken = path.with_name("config.corrupt-{}.json".format(
                datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
            path.replace(broken)
            log.error("the unreadable config was kept at %s", broken)
        except OSError:
            log.exception("could not set the unreadable config aside")
        return _hydrate_secrets(AppConfig())

    cfg = AppConfig(
        **_coerce_fields(AppConfig, raw),
        audio=AudioConfig(**_coerce_fields(AudioConfig, audio_raw)),
        overlay=OverlayConfig(**_coerce_fields(OverlayConfig, overlay_raw)),
    )
    _retire_dead_openrouter_models(cfg)
    return _hydrate_secrets(cfg)


# Ids que o OpenRouter servia e nao serve mais. Medido em 2026-09-18 contra
# openrouter.ai/api/v1/models: nenhum dos cinco aparece no catalogo, e um
# config antigo guardando qualquer um deles faz a transcricao do provedor de
# reserva falhar com "model not found" - justamente quando o Azure ja caiu.
_DEAD_OPENROUTER_STT = {
    "openai/whisper-1",
    "openai/whisper-large-v3-turbo",
    "openai/whisper-large-v3",
    "openai/gpt-4o-mini-transcribe",
    "openai/gpt-4o-transcribe",
}
_DEAD_OPENROUTER_TRANSLATION = {
    "anthropic/claude-3.5-haiku": "anthropic/claude-haiku-4.5",
    "google/gemini-flash-1.5": "google/gemini-2.5-flash",
}


def _retire_dead_openrouter_models(cfg: "AppConfig") -> None:
    if cfg.openrouter_stt_model in _DEAD_OPENROUTER_STT:
        log.warning("openrouter_stt_model %r nao existe mais; usando %r",
                    cfg.openrouter_stt_model, AppConfig.openrouter_stt_model)
        cfg.openrouter_stt_model = AppConfig.openrouter_stt_model
    replacement = _DEAD_OPENROUTER_TRANSLATION.get(cfg.openrouter_translation_model)
    if replacement:
        log.warning("openrouter_translation_model %r nao existe mais; usando %r",
                    cfg.openrouter_translation_model, replacement)
        cfg.openrouter_translation_model = replacement


def _hydrate_secrets(cfg: AppConfig) -> AppConfig:
    """Replace secret fields in cfg with values from the keyring.

    If the dataclass already holds a non-empty value (legacy JSON), prefer
    that value but also write it to keyring so the next save can clear it
    from disk (migration path).
    """
    try:
        from secrets_store import get_secret, is_available, set_secret
    except ImportError:
        return cfg
    if not is_available():
        return cfg

    for name in SECRET_FIELDS:
        current = getattr(cfg, name, "") or ""
        if current:
            # Legacy plain-text value — push to keyring once, keep using it.
            set_secret(name, current)
            continue
        stored = get_secret(name) or ""
        if stored:
            setattr(cfg, name, stored)
    return cfg


def save_config(cfg: AppConfig) -> None:
    """Persist config.json. Secrets are stripped from the JSON and stored in keyring."""
    path = config_path()
    payload = asdict(cfg)

    # Move secret fields into keyring; blank them in the JSON payload
    try:
        from secrets_store import is_available, set_secret
        if is_available():
            for field in SECRET_FIELDS:
                value = payload.get(field) or ""
                if set_secret(field, value):
                    payload[field] = ""
                else:
                    # Blanking the field here used to erase the key from the
                    # JSON even when the keyring write had FAILED — the
                    # credential vanished from BOTH stores. Keep it in the
                    # JSON (the pre-keyring behaviour) and say so loudly.
                    log.critical(
                        "keyring write FAILED for %s; keeping the value in %s "
                        "(PLAIN TEXT) so the key is not lost from both stores",
                        field, path)
        else:
            # No keyring: the blanking loop above never runs, so every API key
            # goes to disk in the clear. That used to happen silently.
            log.warning("keyring unavailable — API keys are being written to "
                        "%s in PLAIN TEXT", path)
    except ImportError:
        pass

    # Atomic write. A plain write_text() interrupted by a crash, a power cut
    # or the app being killed mid-save leaves truncated JSON, and the next
    # launch reads that as "no configuration at all".
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
