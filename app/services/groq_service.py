"""US-12 / US-15 — Servicio de transcripción de audio con Groq.

Groq ofrece los modelos Whisper (`whisper-large-v3`, `whisper-large-v3-turbo`)
servidos sobre su infraestructura, con latencia muy baja y un endpoint
OpenAI-compatible. A diferencia del paquete `openai-whisper`, Groq:

- No requiere descargar/cargar modelos localmente.
- No necesita `ffmpeg` (la decodificación del audio se hace server-side).
- Necesita únicamente una API key (`GROQ_API_KEY`) y elegir el modelo.

Endpoint utilizado:
    POST https://api.groq.com/openai/v1/audio/transcriptions
    POST https://api.groq.com/openai/v1/audio/translations  (task='translate')

Parámetros relevantes del API:
    file, model, language (ISO-639-1, opcional), response_format
    ('json' | 'verbose_json' | 'text'), temperature, prompt,
    timestamp_granularities=['word'] (requiere 'verbose_json').

Con `response_format='verbose_json'` y `timestamp_granularities=['word']`
Groq devuelve, además del texto, el idioma detectado, la duración, los
segmentos con marcas de tiempo y, opcionalmente, palabras individuales
con su nivel de confianza. La confianza global que se expone a la API
se calcula como el promedio de la confianza por palabra.
"""

from __future__ import annotations

import io
from typing import Optional

from core.config import settings

ALLOWED_CONTENT_TYPES = {
    "audio/webm",
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/m4a",
    "audio/ogg",
    "audio/flac",
}

ALLOWED_EXTENSIONS = {"webm", "wav", "mp3", "mpeg", "mpga", "m4a", "ogg", "mp4", "flac"}

MAX_BYTES = 10 * 1024 * 1024  # 10 MB

DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_LANGUAGE = "es"

# Cliente cacheado a nivel de módulo. Groq es thread-safe, así que un único
# cliente es suficiente para todo el proceso.
_client = None


# ---------------------------------------------------------------------------
# Excepciones tipadas — mantienen el mismo shape que teniamos con Whisper
# para que el router las siga mapeando a los mismos codigos HTTP.
# ---------------------------------------------------------------------------


class TranscriptionFormatError(ValueError):
    """Formato de audio no permitido (400)."""


class TranscriptionSizeError(ValueError):
    """Audio mayor al limite permitido (413)."""


class TranscriptionProviderError(RuntimeError):
    """Fallo del proveedor de transcripcion (502)."""


class GroqConfigError(RuntimeError):
    """`GROQ_API_KEY` no configurada o SDK no disponible (503)."""


# ---------------------------------------------------------------------------
# Validacion del audio recibido
# ---------------------------------------------------------------------------


def _extension(filename: str) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def validate_audio(filename: str, content_type: str, data: bytes) -> None:
    """Valida formato (MIME/extension) y tamano del audio."""
    if len(data) == 0:
        raise TranscriptionFormatError("El archivo de audio esta vacio.")

    if len(data) > MAX_BYTES:
        raise TranscriptionSizeError(
            f"El audio excede el limite de {MAX_BYTES // (1024 * 1024)} MB."
        )

    ext = _extension(filename)
    mime = (content_type or "").lower()

    if ext not in ALLOWED_EXTENSIONS and mime not in ALLOWED_CONTENT_TYPES:
        raise TranscriptionFormatError(
            "Formato de audio no soportado. Use webm, mp3 o wav."
        )


# ---------------------------------------------------------------------------
# Resolucion de parametros
# ---------------------------------------------------------------------------


def _resolve_language(language: Optional[str]) -> Optional[str]:
    """Traduce el parametro de idioma del cliente al que espera Groq.

    - ``None`` / ``""``  → idioma por defecto (``es``).
    - ``"auto"``         → ``None`` (Groq detecta el idioma solo, US-18).
    - codigo especifico  → se respeta tal cual.
    """
    if language is None or language == "":
        return DEFAULT_LANGUAGE
    if language.strip().lower() == "auto":
        return None
    return language


def _get_client():
    """Devuelve un cliente Groq cacheado, validando la configuracion."""
    global _client
    if _client is not None:
        return _client

    api_key = settings.GROQ_API_KEY
    if not api_key:
        raise GroqConfigError(
            "GROQ_API_KEY no esta configurada en el servidor. "
            "Definia la variable de entorno GROQ_API_KEY."
        )

    try:
        from groq import Groq
    except ImportError as exc:
        raise GroqConfigError(
            "La libreria 'groq' no esta instalada en el servidor. "
            "Instala las dependencias con 'pip install -r requirements.txt'."
        ) from exc

    _client = Groq(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Normalizacion de la respuesta
# ---------------------------------------------------------------------------


def _extract_segments(segments) -> list[dict]:
    """Normaliza los segmentos de Groq a `[{start, end, text}]`."""
    if not segments:
        return []
    out: list[dict] = []
    for segment in segments:
        if isinstance(segment, dict):
            start = segment.get("start")
            end = segment.get("end")
            text = segment.get("text")
        else:
            start = getattr(segment, "start", None)
            end = getattr(segment, "end", None)
            text = getattr(segment, "text", None)
        if text is None:
            continue
        out.append(
            {
                "start": round(float(start or 0.0), 3),
                "end": round(float(end or 0.0), 3),
                "text": str(text).strip(),
            }
        )
    return out


def _score_from_words(words) -> Optional[float]:
    """Promedia la confianza por palabra (US-15) en un score [0, 1]."""
    if not words:
        return None

    confidences: list[float] = []
    for word in words:
        if isinstance(word, dict):
            value = word.get("confidence")
        else:
            value = getattr(word, "confidence", None)
        if value is None:
            continue
        try:
            confidences.append(float(value))
        except (TypeError, ValueError):
            continue

    if not confidences:
        return None
    return round(sum(confidences) / len(confidences), 4)


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------


def transcribe_audio(
    filename: str,
    content_type: str,
    data: bytes,
    language: Optional[str] = None,
    task: str = "transcribe",
) -> dict:
    """Transcribe el audio y devuelve ``{text, language, confidence, segments}``.

    - ``language``: codigo ISO (``"es"``), ``"auto"`` para deteccion
      automatica (US-18) o ``None`` para usar el idioma por defecto.
    - ``task``: ``"transcribe"`` (mismo idioma) o ``"translate"`` (traduce
      a ingles usando el endpoint de traducciones de Groq, US-18).
    - ``confidence`` proviene del promedio de la confianza por palabra
      devuelta por Groq en ``verbose_json`` (US-15).
    """
    validate_audio(filename, content_type, data)

    lang = _resolve_language(language)
    task = task if task in ("transcribe", "translate") else "transcribe"
    model = settings.GROQ_TRANSCRIBE_MODEL or DEFAULT_MODEL

    client = _get_client()

    # Groq espera un objeto "file-like" con un nombre (atributo `.name`) para
    # inferir el formato. Usamos BytesIO y le seteamos un nombre con la
    # extension del archivo original.
    buffer = io.BytesIO(data)
    buffer.name = filename or f"audio.{(_extension(filename) or 'webm')}"

    endpoint = (
        client.audio.translations if task == "translate"
        else client.audio.transcriptions
    )

    # `verbose_json` permite obtener `language`, `segments` y
    # `timestamp_granularities` para conseguir `words[].confidence`.
    kwargs: dict = {
        "model": model,
        "file": buffer,
        "response_format": "verbose_json",
        "timestamp_granularities": ["word"],
    }
    if task != "translate" and lang:
        # `language` solo aplica a transcripciones; en `translate` el idioma
        # destino es siempre `en` y el parametro no se envia.
        kwargs["language"] = lang

    try:
        result = endpoint.create(**kwargs)
    except Exception as exc:
        # 401/403: key invalida o sin permisos.
        if "401" in str(exc) or "invalid_api_key" in str(exc).lower():
            raise GroqConfigError(
                "GROQ_API_KEY invalida o sin permisos para el modelo "
                f"'{model}'. Verifica la variable de entorno."
            ) from exc
        raise TranscriptionProviderError(
            f"Fallo del proveedor de transcripcion (Groq): {exc}"
        ) from exc

    # La respuesta puede llegar como objeto pydantic (con `extra='allow'`
    # poblado por verbose_json) o como dict, segun la version del SDK.
    def _get(name, default=None):
        if isinstance(result, dict):
            return result.get(name, default)
        return getattr(result, name, default)

    text = (_get("text", "") or "").strip()
    detected_language = _get("language") or lang or DEFAULT_LANGUAGE
    raw_segments = _get("segments")
    raw_words = _get("words")

    confidence = _score_from_words(raw_words)
    if confidence is None:
        # Fallback: si no hay palabras (p. ej. `timestamp_granularities` no
        # se respeto), intentamos derivar la confianza del campo `words`
        # aunque venga vacio y dejamos `None` para no inventar el numero.
        confidence = None

    return {
        "text": text,
        "language": detected_language,
        "confidence": confidence,
        "segments": _extract_segments(raw_segments),
    }
