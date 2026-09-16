"""Provider connection tests.

Each function does the cheapest call the provider supports to validate
credentials are correct without consuming real translation quota.
Returns (ok, message) — ok True on success, message a short user-facing
string in PT-BR.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)


def test_azure(speech_key: str, region: str) -> tuple[bool, str]:
    if not speech_key:
        return False, "Speech Key vazia."
    if not region:
        return False, "Região vazia."
    try:
        import urllib.error
        import urllib.request

        # Hit the issueToken endpoint — fastest auth check Azure exposes
        url = f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "Ocp-Apim-Subscription-Key": speech_key,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            token = resp.read().decode("utf-8", errors="ignore")
            if token and len(token) > 50:
                return True, f"OK — token recebido ({len(token)} chars). Região '{region}' válida."
            return False, "Resposta inesperada do servidor Azure."
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "Speech Key inválida (HTTP 401)."
        if exc.code == 403:
            return False, "Acesso negado (HTTP 403). Verifique pricing tier e região."
        return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, f"Erro: {exc}"


def test_groq(api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, "API Key vazia."
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
            timeout=8.0,
            max_retries=0,
        )
        models = list(client.models.list().data)
        if not models:
            return False, "Conexão OK mas nenhum modelo retornado."
        return True, f"OK — {len(models)} modelos disponíveis (ex.: {models[0].id})."
    except Exception as exc:
        msg = str(exc).lower()
        if "401" in msg or "unauthor" in msg:
            return False, "API Key inválida (401)."
        if "403" in msg or "forbidden" in msg:
            return False, "Acesso negado (403)."
        if "rate" in msg or "429" in msg:
            return False, "Rate limit atingido (429). Aguarde 1 minuto."
        return False, f"Erro: {exc}"


def test_google(credentials_json: str, project_id: str, location: str = "global") -> tuple[bool, str]:
    if not credentials_json:
        return False, "Service Account JSON vazio."
    if not project_id:
        return False, "Project ID vazio."

    # Resolve credentials: file path OR inline JSON
    creds_path: str | None = None
    temp_path: str | None = None
    try:
        if os.path.isfile(credentials_json):
            creds_path = credentials_json
        else:
            try:
                json.loads(credentials_json)
            except Exception:
                return False, "Credentials não é nem caminho válido nem JSON válido."
            fd, temp_path = tempfile.mkstemp(suffix=".json", prefix="tlt-test-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(credentials_json)
            creds_path = temp_path

        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path

        from google.cloud import translate_v3 as translate

        client = translate.TranslationServiceClient()
        parent = f"projects/{project_id}/locations/{location or 'global'}"
        # translate_text with 1 char is the cheapest call
        resp = client.translate_text(
            parent=parent,
            contents=["a"],
            target_language_code="es",
            source_language_code="en",
            mime_type="text/plain",
        )
        if resp.translations:
            return True, f"OK — Translation API responde (project '{project_id}')."
        return False, "Conexão OK mas resposta vazia."
    except Exception as exc:
        msg = str(exc)
        if "PERMISSION_DENIED" in msg or "403" in msg:
            return False, "Acesso negado. Habilite as APIs Speech-to-Text + Cloud Translation."
        if "NOT_FOUND" in msg or "404" in msg:
            return False, f"Project ID '{project_id}' não encontrado."
        if "INVALID_ARGUMENT" in msg:
            return False, "Argumento inválido. Verifique location."
        return False, f"Erro: {exc[:200]}"
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def test_cerebras(api_key: str) -> tuple[bool, str]:
    """Smoke-test Cerebras API key by listing available models.

    Doesn't depend on any specific model — robust against deprecations.
    """
    if not api_key:
        return False, "API key vazia. Pegue grátis em inference.cerebras.ai"
    try:
        from openai import OpenAI
    except ImportError:
        return False, "Pacote 'openai' não instalado."
    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.cerebras.ai/v1",
            timeout=10.0,
        )
        models = client.models.list()
        names = [m.id for m in models.data] if hasattr(models, 'data') else []
        if not names:
            return True, "OK · auth válida (lista de modelos vazia)"
        preview = ", ".join(names[:3])
        suffix = f" (+{len(names) - 3} outros)" if len(names) > 3 else ""
        return True, f"OK · {len(names)} modelos: {preview}{suffix}"
    except Exception as exc:
        return False, f"Falhou: {exc!s}"


def test_openai_whisper(api_key: str) -> tuple[bool, str]:
    """Smoke test OpenAI Whisper API by listing models."""
    if not api_key:
        return False, "API key vazia. Pegue uma em platform.openai.com/api-keys"
    try:
        from openai import OpenAI
    except ImportError:
        return False, "Pacote 'openai' não instalado."
    try:
        client = OpenAI(api_key=api_key, timeout=10.0)
        models = client.models.list()
        names = [m.id for m in models.data] if hasattr(models, "data") else []
        if "whisper-1" in names:
            return True, "OK · whisper-1 disponível"
        return True, f"OK · auth válida ({len(names)} modelos)"
    except Exception as exc:
        return False, f"Falhou: {exc!s}"


def test_openrouter(api_key: str) -> tuple[bool, str]:
    """Smoke test OpenRouter API key by listing available models.

    Doesn't depend on any specific model — robust against catalog changes.
    """
    if not api_key:
        return False, "API key vazia. Pegue grátis em openrouter.ai (free tier disponível)"
    try:
        from openai import OpenAI
    except ImportError:
        return False, "Pacote 'openai' não instalado."
    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=10.0,
        )
        models = client.models.list()
        names = [m.id for m in models.data] if hasattr(models, 'data') else []
        if not names:
            return True, "OK · auth válida (lista vazia inesperada)"
        # Quick sanity check: verify whisper + gpt models present
        has_whisper = any("whisper" in n.lower() for n in names)
        has_chat = any("gpt" in n.lower() or "llama" in n.lower() for n in names)
        suffix = ""
        if has_whisper and has_chat:
            suffix = " (STT + chat OK)"
        return True, f"OK · {len(names)} modelos disponíveis{suffix}"
    except Exception as exc:
        return False, f"Falhou: {exc!s}"


def test_whisper_local(model: str, device: str = "cpu", compute_type: str = "int8") -> tuple[bool, str]:
    """Validate that faster-whisper can load the requested model.
    First call downloads ~150MB-3GB depending on model size; subsequent are cached."""
    try:
        from faster_whisper import WhisperModel

        from config import app_data_dir
        from providers.whisper_local import WHISPER_MODELS

        repo = WHISPER_MODELS.get(model, model)
        cache_dir = str(app_data_dir() / "models")
        # Lazy load — this validates the device/compute combination too
        m = WhisperModel(repo, device=device, compute_type=compute_type, download_root=cache_dir)
        del m
        return True, f"OK — modelo '{model}' carrega no dispositivo '{device}' ({compute_type})."
    except ImportError:
        return False, "faster-whisper não instalado."
    except Exception as exc:
        msg = str(exc).lower()
        if "cuda" in msg:
            return False, f"CUDA não disponível. Use device='cpu'. ({exc})"
        if "compute_type" in msg:
            return False, f"compute_type '{compute_type}' incompatível com device '{device}'."
        return False, f"Erro: {exc}"
