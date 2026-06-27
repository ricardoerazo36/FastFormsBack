"""US-13 — Servicio de generación de encuestas con Gemini.

Carga el prompt maestro desde `promptbeta.txt` (en la raíz del repo), lo
combina con la idea del usuario y delega en la SDK oficial `google-genai`.

Diseño:
- Excepciones tipadas que el router mapea a HTTP:
  * `GeminiConfigError`     → 503 (API key no configurada / SDK no instalada).
  * `GeminiProviderError`   → 502 (fallo del proveedor: red, 429, 5xx, etc.).
  * `GeminiParseError`      → 502 (la respuesta no es JSON válido o no
                              cumple el schema esperado).
- Caché en memoria del prompt maestro (se relee si cambia el archivo).
- Validación final con Pydantic (`GenerateSurveyResponse`).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from core.config import settings
from schemas.generate import GenerateSurveyResponse

logger = logging.getLogger(__name__)

# Ruta al prompt maestro. Se resuelve relativo a este archivo para que
# funcione tanto en `uvicorn app.main:app` como desde los tests.
_PROMPT_FILE = Path(__file__).resolve().parents[2] / "prompts/promptGeneracionV1.txt"

# Esquema JSON (OpenAPI 3.0 subset) que Gemini usa para garantizar
# `response_schema`. Es un espejo del modelo Pydantic de salida.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Título corto de la encuesta."},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "question_type": {
                        "type": "string",
                        "enum": ["open", "multiple_choice", "yes_no"],
                    },
                    "options": {
                        "anyOf": [
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "null"},
                        ]
                    },
                    "position": {"type": "integer"},
                },
                "required": ["content", "question_type", "options", "position"],
            },
        },
    },
    "required": ["title", "questions"],
}


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class GeminiConfigError(RuntimeError):
    """La API key no está configurada o la SDK no está instalada (503)."""


class GeminiProviderError(RuntimeError):
    """Fallo del proveedor Gemini (red, 429 quota, 5xx, etc.) → 502."""


class GeminiParseError(ValueError):
    """La respuesta de Gemini no es JSON válido o no cumple el schema (502)."""


# ---------------------------------------------------------------------------
# Carga del prompt maestro
# ---------------------------------------------------------------------------

_prompt_cache: Optional[str] = None
_prompt_mtime: Optional[float] = None


def _load_master_prompt() -> str:
    """Lee `promptbeta.txt`. Re-lee si el archivo cambió en disco."""
    global _prompt_cache, _prompt_mtime

    if not _PROMPT_FILE.exists():
        raise GeminiConfigError(
            f"No se encontró el archivo de prompt maestro en '{_PROMPT_FILE}'."
        )

    mtime = _PROMPT_FILE.stat().st_mtime
    if _prompt_cache is None or mtime != _prompt_mtime:
        _prompt_cache = _PROMPT_FILE.read_text(encoding="utf-8")
        _prompt_mtime = mtime
    return _prompt_cache


def _render_prompt(master: str, num_questions: int, language: str, user_prompt: str) -> str:
    """Sustituye los placeholders del prompt maestro."""
    return (
        master
        .replace("{N}", str(num_questions))
        .replace("{language}", language)
        .replace("{user_prompt}", user_prompt.strip())
    )


# ---------------------------------------------------------------------------
# Cliente de Gemini
# ---------------------------------------------------------------------------


def _build_client():
    """Crea (o cachea) el cliente de Gemini. Lanza GeminiConfigError si falta la key."""
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise GeminiConfigError(
            "GEMINI_API_KEY no está configurada en el servidor."
        )
    try:
        from google import genai
    except ImportError as exc:
        raise GeminiConfigError(
            "La librería 'google-genai' no está instalada en el servidor. "
            "Instálala con 'pip install google-genai'."
        ) from exc
    return genai.Client(api_key=api_key)


def _call_gemini(client, final_prompt: str) -> str:
    """Invoca a Gemini y devuelve el texto de la respuesta."""
    try:
        from google.genai import types
    except ImportError as exc:
        raise GeminiConfigError(
            "La librería 'google-genai' no está instalada en el servidor."
        ) from exc

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
        temperature=0.7,
    )
    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=final_prompt,
            config=config,
        )
    except Exception as exc:
        text = str(exc)
        if "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
            raise GeminiProviderError(
                "La cuenta de Gemini no tiene cuota disponible (429)."
            ) from exc
        raise GeminiProviderError(f"Fallo del proveedor Gemini: {exc}") from exc

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise GeminiProviderError("Gemini devolvió una respuesta vacía.")
    return text


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------


def _parse_and_validate(raw_text: str) -> dict:
    """Parsea la respuesta cruda y la valida con Pydantic.

    Devuelve un dict listo para serializar (compatible con el response_model).
    """
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise GeminiParseError(
            f"La respuesta de Gemini no es JSON válido: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise GeminiParseError(
            "La respuesta de Gemini no es un objeto JSON de primer nivel."
        )

    try:
        validated = GenerateSurveyResponse.model_validate(data)
    except ValidationError as exc:
        raise GeminiParseError(
            f"La respuesta de Gemini no cumple el esquema esperado: {exc}"
        ) from exc

    return validated.model_dump()


# ---------------------------------------------------------------------------
# Punto de entrada público
# ---------------------------------------------------------------------------


def generate_survey_draft(
    user_prompt: str,
    num_questions: int,
    language: str = "es",
) -> dict:
    """Genera un borrador de encuesta a partir de una idea del usuario.

    Devuelve un dict ``{title, questions: [{content, question_type, options, position}, ...]}``
    que el frontend puede inyectar directamente en el formulario de creación.

    Lanza:
      - ``GeminiConfigError``     si falta la API key o la SDK.
      - ``GeminiProviderError``   si Gemini falla (red, 429, 5xx).
      - ``GeminiParseError``      si la respuesta no parsea o no valida.
    """
    master = _load_master_prompt()
    final_prompt = _render_prompt(master, num_questions, language, user_prompt)

    client = _build_client()

    logger.info(
        "Generando encuesta con Gemini: model=%s n=%d lang=%s prompt_len=%d",
        settings.GEMINI_MODEL,
        num_questions,
        language,
        len(user_prompt),
    )

    raw = _call_gemini(client, final_prompt)
    return _parse_and_validate(raw)
