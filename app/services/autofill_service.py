"""US-15 t2 — Servicio de auto-rellenado de encuestas por voz.

Composición:
  1) Groq transcribe el audio (consume `app.services.groq_service`).
  2) Gemini mapea la transcripción a las preguntas de la encuesta con un
     prompt maestro + `response_schema` JSON estructurado (consume
     `app.services.gemini_service` para reusar cliente y excepciones).

Excepciones tipadas (re-exportadas desde `gemini_service`):
  * `GeminiConfigError`     → 503 (API key no configurada / SDK no instalada).
  * `GeminiProviderError`   → 502 (fallo del proveedor Gemini).
  * `GeminiParseError`      → 502 (la respuesta no parsea o no cumple el schema).

La salida final es un dict compatible con `AutoFillResponse`:
  {"survey_id": int, "answers": [{"question_id": int, "answer_text": str|None}, ...]}

Reglas de saneamiento aplicadas a la respuesta de Gemini (defensa en
profundidad, además de las restricciones del prompt maestro):
  - Se descartan `question_id` que no existen en la encuesta.
  - Las preguntas que Gemini omitió se inyectan con `answer_text=None`.
  - Para `multiple_choice`: la respuesta debe matchear EXACTAMENTE (case-
    insensitive, ignorando espacios) una de las opciones declaradas. Si no,
    se reemplaza por `None`.
  - Para `yes_no`: la respuesta se normaliza a `"Sí"` o `"No"`. Si no
    matchea, se reemplaza por `None`.
  - Para `open`: si Gemini devuelve una cadena vacía o solo espacios, se
    reemplaza por `None`.
  - El orden de `answers` sigue el `position` ascendente de las preguntas
    de la encuesta (no el orden devuelto por Gemini).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from services import gemini_service

logger = logging.getLogger(__name__)

# Opciones implícitas para yes_no (alineado con supabase_service.YES_NO_OPTIONS).
YES_NO_OPTIONS = ["Sí", "No"]


# ---------------------------------------------------------------------------
# Re-exports — el router mapea estas excepciones a HTTP sin importar
# `gemini_service` directamente.
# ---------------------------------------------------------------------------

GeminiConfigError = gemini_service.GeminiConfigError
GeminiProviderError = gemini_service.GeminiProviderError
GeminiParseError = gemini_service.GeminiParseError


# ---------------------------------------------------------------------------
# Prompt maestro
# ---------------------------------------------------------------------------

_PROMPT_FILE = Path(__file__).resolve().parents[2] / "prompts/promptAutoFillV1.txt"

_prompt_cache: Optional[str] = None
_prompt_mtime: Optional[float] = None


def _load_master_prompt() -> str:
    """Lee `prompts/promptAutoFillV1.txt` con caché por mtime."""
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


def _normalize_question(q: dict) -> dict:
    """Filtra las claves que Gemini verá: id, content, type, options, position."""
    return {
        "id": q.get("id"),
        "content": (q.get("content") or "").strip(),
        "type": q.get("question_type"),
        "options": q.get("options"),
        "position": q.get("position"),
    }


def _build_questions_json(questions: list[dict]) -> str:
    """Serializa la lista de preguntas (en orden de la encuesta) como JSON bonito."""
    return json.dumps(
        [_normalize_question(q) for q in questions],
        ensure_ascii=False,
        indent=2,
    )


def _render_prompt(
    master: str,
    survey_id: int,
    title: str,
    questions: list[dict],
    transcript: str,
    language: str,
) -> str:
    """Sustituye los placeholders del prompt maestro."""
    return (
        master
        .replace("{survey_id}", str(survey_id))
        .replace("{title}", (title or "").strip())
        .replace("{questions_json}", _build_questions_json(questions))
        .replace("{transcript}", transcript.strip())
        .replace("{language}", language or "es")
    )


# ---------------------------------------------------------------------------
# Esquema de respuesta para Gemini (response_schema)
# ---------------------------------------------------------------------------

# Espejo del modelo Pydantic de salida. Garantiza estructura JSON válida.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "integer"},
                    "answer_text": {
                        "anyOf": [{"type": "string"}, {"type": "null"}]
                    },
                },
                "required": ["question_id", "answer_text"],
            },
        }
    },
    "required": ["answers"],
}


# ---------------------------------------------------------------------------
# Validación con Pydantic (capa de seguridad adicional al response_schema)
# ---------------------------------------------------------------------------


class _GeminiAnswer(BaseModel):
    question_id: int
    answer_text: Optional[str] = None


class _GeminiAutoFill(BaseModel):
    answers: list[_GeminiAnswer] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Llamada a Gemini (re-usa el cliente de gemini_service)
# ---------------------------------------------------------------------------


def _call_gemini_for_autofill(prompt: str) -> str:
    """Invoca a Gemini con `response_schema` y devuelve el texto crudo."""
    client = gemini_service._build_client()

    try:
        from google.genai import types
    except ImportError as exc:
        raise GeminiConfigError(
            "La librería 'google-genai' no está instalada en el servidor."
        ) from exc

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
        temperature=0.2,
    )
    try:
        response = client.models.generate_content(
            model=gemini_service.settings.GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
    except Exception as exc:
        text = str(exc)
        if "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
            raise GeminiProviderError(
                "La cuenta de Gemini no tiene cuota disponible (429)."
            ) from exc
        raise GeminiProviderError(f"Fallo del proveedor Gemini: {exc}") from exc

    raw = (getattr(response, "text", None) or "").strip()
    if not raw:
        raise GeminiProviderError("Gemini devolvió una respuesta vacía.")
    return raw


def _parse_and_validate(raw_text: str) -> _GeminiAutoFill:
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
        return _GeminiAutoFill.model_validate(data)
    except ValidationError as exc:
        raise GeminiParseError(
            f"La respuesta de Gemini no cumple el esquema esperado: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Saneamiento: aplica las reglas de la encuesta sobre la salida de Gemini
# ---------------------------------------------------------------------------


def _norm_text(value: Optional[str]) -> str:
    """Lowercase + colapsa espacios + trim. Usado para matching tolerante."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def _sanitize_yes_no(raw: Optional[str]) -> Optional[str]:
    """Normaliza respuestas yes_no a 'Sí' / 'No'. None si no matchea.

    La transcripción puede contener muletillas antes/después ("sí porfa",
    "no, gracias"). Para ser tolerantes, decidimos mirando la PRIMERA palabra
    normalizada. Si no matchea ninguno de los dos lados, devolvemos None.
    """
    n = _norm_text(raw)
    if not n:
        return None
    first = n.split(" ", 1)[0]
    positive = {"sí", "si", "s", "yes", "y", "true", "1", "claro", "obvio", "afirmativo", "correcto"}
    negative = {"no", "n", "false", "0"}
    if first in positive:
        return "Sí"
    if first in negative:
        return "No"
    return None


def _sanitize_multiple_choice(raw: Optional[str], options: Optional[list]) -> Optional[str]:
    """Devuelve la opción declarada que matchea (case-insensitive), o None."""
    if not raw or not options:
        return None
    n = _norm_text(raw)
    if not n:
        return None
    # Primero, match exacto case-insensitive.
    for opt in options:
        if _norm_text(opt) == n:
            return opt
    # Después, match "contiene" (la transcripción pudo haber añadido muletillas).
    for opt in options:
        if n in _norm_text(opt) or _norm_text(opt) in n:
            return opt
    return None


def _sanitize_open(raw: Optional[str]) -> Optional[str]:
    """Para preguntas abiertas: strip; None si queda vacío."""
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _build_final_answers(
    survey: dict,
    gemini_answers: list[_GeminiAnswer],
) -> list[dict]:
    """Genera la lista final en el orden de la encuesta.

    - Garantiza una entrada por cada pregunta de la encuesta.
    - Aplica matching estricto para `multiple_choice` / `yes_no`.
    - Filtra `question_id` que no existen en la encuesta.
    """
    questions = list(survey.get("questions") or [])
    questions.sort(key=lambda q: (q.get("position") or 0))

    gemini_by_id: dict[int, Optional[str]] = {
        a.question_id: a.answer_text for a in gemini_answers
    }
    valid_ids = {q.get("id") for q in questions}

    out: list[dict] = []
    for q in questions:
        qid = q.get("id")
        qtype = q.get("question_type")
        raw_text = gemini_by_id.get(qid) if qid in valid_ids else None

        if qtype == "multiple_choice":
            text = _sanitize_multiple_choice(raw_text, q.get("options"))
        elif qtype == "yes_no":
            text = _sanitize_yes_no(raw_text)
        else:  # open u otros
            text = _sanitize_open(raw_text)

        out.append(
            {
                "question_id": qid,
                "answer_text": text,
            }
        )
    return out


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def generate_auto_fill(
    survey: dict,
    transcript: str,
    detected_language: Optional[str] = None,
) -> dict:
    """Genera el auto-rellenado de una encuesta a partir de una transcripción.

    Parámetros:
      - `survey`: dict de Supabase con `{id, title, questions: [...]}`.
        `questions` debe contener `id`, `content`, `question_type`, `options`,
        `position`.
      - `transcript`: texto transcrito por Groq.
      - `detected_language`: ISO 639-1 detectado por Groq. Se usa SOLO para
        inyectar el idioma en el prompt maestro; no se persiste ni se devuelve
        en la respuesta (alineado con el contrato de la tabla `answers`).

    Devuelve un dict compatible con `AutoFillResponse`:
      `{"survey_id": int, "answers": [{"question_id", "answer_text"}, ...]}`

    Lanza las mismas excepciones que `gemini_service`.
    """
    survey_id = survey.get("id")
    title = survey.get("title") or ""
    questions = list(survey.get("questions") or [])

    master = _load_master_prompt()
    final_prompt = _render_prompt(
        master=master,
        survey_id=survey_id,
        title=title,
        questions=questions,
        transcript=transcript or "",
        language=detected_language or "es",
    )

    logger.info(
        "Auto-fill: survey_id=%s n_questions=%d transcript_len=%d lang=%s",
        survey_id,
        len(questions),
        len(transcript or ""),
        detected_language,
    )

    raw = _call_gemini_for_autofill(final_prompt)
    parsed = _parse_and_validate(raw)

    answers = _build_final_answers(survey, parsed.answers)

    return {"survey_id": survey_id, "answers": answers}
