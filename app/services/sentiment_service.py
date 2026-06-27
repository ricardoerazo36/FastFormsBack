"""US-16 — Servicio de análisis de sentimientos por pregunta abierta.

Composición:
  - Carga el prompt maestro desde `prompts/promptSentimentV1.txt`.
  - Inyecta la pregunta y las respuestas (en JSON) en el prompt.
  - Delega en Gemini (`gemini_service._build_client`) con `response_schema`
    estructurado para garantizar JSON válido.

Excepciones tipadas (re-exportadas desde `gemini_service`):
  * ``GeminiConfigError``     → 503 (API key no configurada / SDK no instalada).
  * ``GeminiProviderError``   → 502 (fallo del proveedor Gemini).
  * ``GeminiParseError``      → 502 (la respuesta no parsea o no cumple el schema).

La salida final es un dict compatible con ``SentimentAnalysisResponse``
(sin los metadatos `survey_id`, `question_id`, `question_content`,
`total_answers`, que añade el router a partir del contexto).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from services import gemini_service

logger = logging.getLogger(__name__)


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

_PROMPT_FILE = Path(__file__).resolve().parents[2] / "prompts/promptSentimentV1.txt"

_prompt_cache: Optional[str] = None
_prompt_mtime: Optional[float] = None


def _load_master_prompt() -> str:
    """Lee ``prompts/promptSentimentV1.txt`` con caché por mtime."""
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


def _build_answers_json(answers: list[str]) -> str:
    """Serializa las respuestas como una lista JSON bonita (una por línea)."""
    cleaned = [(a or "").strip() for a in answers if (a or "").strip()]
    return json.dumps(cleaned, ensure_ascii=False, indent=2)


def _render_prompt(
    master: str,
    question_content: str,
    answers_json: str,
    n_answers: int,
) -> str:
    """Sustituye los placeholders del prompt maestro."""
    return (
        master
        .replace("{question_content}", (question_content or "").strip())
        .replace("{answers_json}", answers_json)
        .replace("{n_answers}", str(n_answers))
    )


# ---------------------------------------------------------------------------
# Esquema de respuesta para Gemini (response_schema)
# ---------------------------------------------------------------------------

# Espejo del modelo Pydantic de salida. Garantiza estructura JSON válida
# aunque Gemini intente improvisar campos extra.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall_sentiment": {
            "type": "string",
            "enum": ["positivo", "negativo", "neutral", "mixto"],
        },
        "score": {"type": "number"},
        "distribution": {
            "type": "object",
            "properties": {
                "positive": {"type": "integer"},
                "negative": {"type": "integer"},
                "neutral": {"type": "integer"},
            },
            "required": ["positive", "negative", "neutral"],
        },
        "summary": {"type": "string"},
        "key_themes": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["overall_sentiment", "score", "distribution", "summary", "key_themes"],
}


# ---------------------------------------------------------------------------
# Validación con Pydantic (capa de seguridad adicional al response_schema)
# ---------------------------------------------------------------------------


class _Distribution(BaseModel):
    positive: int = 0
    negative: int = 0
    neutral: int = 0


class _GeminiSentiment(BaseModel):
    # `overall_sentiment` se declara como `Any` para que la normalización
    # downstream (`_normalize_sentiment`) pueda tolerar valores no-string
    # (números, None, etc.) que Gemini a veces emite por error.
    overall_sentiment: Any
    score: Any
    distribution: Any
    summary: Any
    key_themes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Saneamiento: defensa en profundidad sobre la salida de Gemini
# ---------------------------------------------------------------------------

_ALLOWED_SENTIMENTS = {"positivo", "negativo", "neutral", "mixto"}


def _clamp_score(raw: float) -> float:
    """Fuerza el score al rango [-1.0, 1.0] y limita a 2 decimales."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    value = max(-1.0, min(1.0, value))
    return round(value, 2)


def _normalize_sentiment(raw) -> str:
    """Lowercase + strip; default a 'neutral' si no es una de las 4 válidas.

    Tolera valores no-string (números, None, etc.) que Gemini a veces emite
    por error; los castea a string antes de comparar.
    """
    if raw is None or raw == "":
        return "neutral"
    try:
        candidate = str(raw).strip().lower()
    except Exception:
        return "neutral"
    if not candidate:
        return "neutral"
    if candidate in _ALLOWED_SENTIMENTS:
        return candidate
    # Acepta algunas variantes razonables que Gemini podría emitir.
    aliases = {
        "pos": "positivo",
        "positive": "positivo",
        "neg": "negativo",
        "negative": "negativo",
        "neu": "neutral",
        "mixed": "mixto",
    }
    return aliases.get(candidate, "neutral")


def _safe_int(value) -> int:
    """Convierte a entero >= 0; tolera None, strings numéricos, floats."""
    try:
        if value is None:
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _sanitize_distribution(
    raw: dict, total_answers: int
) -> dict[str, int]:
    """Sanea los conteos: enteros >= 0, suma == total_answers.

    Si Gemini devuelve conteos incoherentes (negativos, suma distinta al
    total, valores no enteros), los corregimos con la mejor aproximación
    posible sin inventar polaridad.
    """
    if not isinstance(raw, dict):
        raw = {}
    pos = max(0, _safe_int(raw.get("positive")))
    neg = max(0, _safe_int(raw.get("negative")))
    neu = max(0, _safe_int(raw.get("neutral")))

    current_sum = pos + neg + neu
    if current_sum == total_answers:
        return {"positive": pos, "negative": neg, "neutral": neu}

    # Si la suma difiere del total, ajustamos la categoría mayor hacia arriba
    # o hacia abajo (y como último recurso, neutral) para cuadrar los números
    # sin alterar la polaridad agregada.
    diff = total_answers - current_sum
    if diff > 0:
        # Falta repartir `diff` votos: al mayor, luego al segundo mayor, etc.
        order = sorted(
            [("positive", pos), ("negative", neg), ("neutral", neu)],
            key=lambda kv: kv[1],
            reverse=True,
        )
        i = 0
        while diff > 0 and order:
            key, value = order[i % len(order)]
            order[i % len(order)] = (key, value + 1)
            diff -= 1
            i += 1
        pos, neg, neu = (
            dict(order)["positive"],
            dict(order)["negative"],
            dict(order)["neutral"],
        )
    else:
        # Sobran votos: recortamos desde el mayor hacia abajo.
        order = sorted(
            [("positive", pos), ("negative", neg), ("neutral", neu)],
            key=lambda kv: kv[1],
            reverse=True,
        )
        i = 0
        while diff < 0 and order:
            key, value = order[i % len(order)]
            if value > 0:
                order[i % len(order)] = (key, value - 1)
                diff += 1
            i += 1
            if i > 10_000:  # safety net
                break
        pos, neg, neu = (
            dict(order)["positive"],
            dict(order)["negative"],
            dict(order)["neutral"],
        )
    return {"positive": pos, "negative": neg, "neutral": neu}


def _sanitize_themes(raw: list[str], limit: int = 5) -> list[str]:
    """Limpia, deduplica (case-insensitive) y trunca los temas."""
    seen: set[str] = set()
    out: list[str] = []
    for item in raw or []:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _sanitize_summary(raw: str) -> str:
    """Garantiza un resumen no vacío (default si Gemini lo entrega vacío)."""
    cleaned = (raw or "").strip()
    if cleaned:
        return cleaned
    return "No fue posible generar un resumen para estas respuestas."


# ---------------------------------------------------------------------------
# Llamada a Gemini (re-usa el cliente de gemini_service)
# ---------------------------------------------------------------------------


def _call_gemini_for_sentiment(prompt: str) -> str:
    """Invoca a Gemini con ``response_schema`` y devuelve el texto crudo."""
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


def _parse_and_validate(raw_text: str) -> dict:
    """Parsea y valida la respuesta cruda de Gemini.

    Devuelve un dict con claves normalizadas:
        {
            "overall_sentiment": <cualquier tipo, lo sanea _normalize_sentiment>,
            "score": <cualquier tipo, lo sanea _clamp_score>,
            "distribution": <dict saneable por _sanitize_distribution>,
            "summary": <cualquier tipo, lo sanea _sanitize_summary>,
            "key_themes": <list, lo sanea _sanitize_themes>,
        }
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

    missing = [k for k in ("overall_sentiment", "score", "distribution", "summary", "key_themes") if k not in data]
    if missing:
        raise GeminiParseError(
            f"La respuesta de Gemini no contiene los campos requeridos: {missing}."
        )

    distribution = data.get("distribution")
    if not isinstance(distribution, dict):
        # Gemini a veces omite o cambia el tipo; lo aceptamos como {} y los
        # saneadores los reemplazarán por conteos coherentes.
        distribution = {}

    key_themes = data.get("key_themes") or []
    if not isinstance(key_themes, list):
        key_themes = []

    return {
        "overall_sentiment": data.get("overall_sentiment"),
        "score": data.get("score"),
        "distribution": distribution,
        "summary": data.get("summary"),
        "key_themes": key_themes,
    }


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def analyze_sentiment(question: dict, answers: list[str]) -> dict:
    """Genera el análisis de sentimientos de las respuestas abiertas de una pregunta.

    Parámetros:
      - ``question``: dict de la pregunta con al menos ``content``.
      - ``answers``: lista de textos de respuesta (ya saneados, no vacíos).

    Devuelve un dict compatible con ``SentimentAnalysisResponse`` (sin los
    metadatos que añade el router: ``survey_id``, ``question_id``,
    ``question_content``, ``total_answers``).

    Lanza las mismas excepciones que ``gemini_service``.
    """
    cleaned_answers = [(a or "").strip() for a in (answers or []) if (a or "").strip()]
    n_answers = len(cleaned_answers)

    master = _load_master_prompt()
    answers_json = _build_answers_json(cleaned_answers)
    final_prompt = _render_prompt(
        master=master,
        question_content=question.get("content") or "",
        answers_json=answers_json,
        n_answers=n_answers,
    )

    logger.info(
        "Sentiment: question_id=%s n_answers=%d prompt_len=%d",
        question.get("id"),
        n_answers,
        len(final_prompt),
    )

    raw = _call_gemini_for_sentiment(final_prompt)
    parsed = _parse_and_validate(raw)

    distribution = _sanitize_distribution(parsed["distribution"], n_answers)

    return {
        "overall_sentiment": _normalize_sentiment(parsed["overall_sentiment"]),
        "score": _clamp_score(parsed["score"]),
        "distribution": distribution,
        "summary": _sanitize_summary(parsed["summary"]),
        "key_themes": _sanitize_themes(parsed["key_themes"]),
    }
