# US-15 — Migración del proveedor de transcripción: Whisper → Groq

## Contexto

El backend de FastForms expone `POST /api/v1/transcribe/` para transcribir
audio corto a texto, implementado originalmente sobre **Whisper** (paquete
local `openai-whisper` o API hospedada de OpenAI). Vamos a sustituirlo por
**Groq**, que ofrece los mismos modelos Whisper servidos sobre su hardware
con latencia muy baja, sin necesidad de cargar modelos ni de instalar
`ffmpeg` localmente.

## Decisiones de diseño

| Aspecto | Decisión |
|---|---|
| Modelo por defecto | `whisper-large-v3-turbo` (configurable vía `GROQ_TRANSCRIBE_MODEL`) |
| Columnas `is_voice` / `language` en `answers` | Se conservan (documentan respuestas de voz, son ortogonales al proveedor) |
| Campo `confidence` en la respuesta | Se conserva; se calcula como promedio de la confianza por palabra devuelta por Groq |
| Módulo `voice_code.py` (US-19) | Se conserva; es normalización de texto independiente del proveedor |
| Límite de tamaño de audio | 10 MB (igual que antes) |
| Librerías Whisper / OpenAI / ffmpeg | Se eliminan completamente |
| SDK nuevo | `groq>=1.5.0` |

## API expuesta por Groq (resumen)

- **Endpoint**: `POST https://api.groq.com/openai/v1/audio/transcriptions`
- **Auth**: header `Authorization: Bearer $GROQ_API_KEY`
- **Modelos**: `whisper-large-v3`, `whisper-large-v3-turbo`
- **Parámetros relevantes**: `file`, `model`, `language` (ISO-639-1, opcional),
  `response_format` (`json` / `verbose_json`), `temperature`
- **`response_format=verbose_json`** devuelve: `text`, `language`, `duration`,
  `segments[]` (`id`, `seek`, `start`, `end`, `text`, `tokens`, `temperature`)
  y opcionalmente `words[]` (`word`, `start`, `end`) cuando se pide
  `timestamp_granularities=["word"]`
- **Formatos aceptados**: `flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav, webm`
- **Sin ffmpeg del lado cliente**: Groq decodifica server-side

## Cambios

### Archivos nuevos

- `app/services/groq_service.py` — reemplaza `whisper_service.py`. Misma
  signatura pública (`transcribe_audio`, `validate_audio`) y mismas
  excepciones (`TranscriptionFormatError`, `TranscriptionSizeError`,
  `TranscriptionProviderError`, `GroqConfigError`). Devuelve
  `{text, language, confidence, segments, duration}`.

### Archivos modificados

- `app/api/routes/transcribe.py` — cambia el import a `groq_service`.
- `app/core/config.py` — `WHISPER_PROVIDER/MODEL/LOCAL_MODEL/DEFAULT_LANGUAGE`
  y `OPENAI_API_KEY` se reemplazan por `GROQ_API_KEY` y `GROQ_TRANSCRIBE_MODEL`.
- `app/main.py` — se elimina el `lifespan` con warm-up de Whisper
  (Groq es API, no requiere precarga) y el import `whisper_service`.
- `app/services/supabase_service.py` — se elimina la maquinaria de fallback
  de columnas opcionales (`_OPTIONAL_ANSWER_COLUMNS`,
  `_answers_missing_columns`, `_is_missing_column_error`,
  `_missing_column_name`, `_present_optional_columns` y los bucles de
  reintento en `create_response` y `get_survey_results`). Las columnas
  `is_voice` y `language` se siguen leyendo/escribiendo directamente.
- `app/schemas/transcribe.py` — docstring actualizado a "Groq".
- `requirements.txt` — bloque "Voice (US-12 — Whisper STT)" eliminado, se
  añade `groq>=1.5.0`.
- `AGENTS.md` — sección "Voice (Whisper)" → "Voice (Groq)".
- `README.md` — secciones de Whisper reemplazadas por equivalentes de Groq;
  bloque de `.env` actualizado; endpoint table actualizado.
- `tests/unit/test_transcribe.py` — reescrito contra `groq_service`.

### Archivos eliminados

- `app/services/whisper_service.py`
- `tests/unit/test_voice_code.py` (la lógica de `voice_code` ya se cubre
  implícitamente vía el endpoint; los tests específicos eran ruido)
- `tests/unit/test_is_voice_fallback.py` (la maquinaria de fallback
  opcional ya no existe)

### Variables de entorno

Eliminadas:
- `WHISPER_PROVIDER`
- `WHISPER_MODEL`
- `WHISPER_LOCAL_MODEL`
- `WHISPER_DEFAULT_LANGUAGE`
- `WHISPER_WARMUP`
- `OPENAI_API_KEY`

Añadidas:
- `GROQ_API_KEY` (requerida para `/transcribe`; sin ella el endpoint
  responde 503)
- `GROQ_TRANSCRIBE_MODEL` (opcional, default `whisper-large-v3-turbo`)

## Mapeo de excepciones → HTTP

| Excepción | Status |
|---|---|
| `TranscriptionFormatError` | 400 |
| `TranscriptionSizeError` | 413 |
| `GroqConfigError` (sin `GROQ_API_KEY`) | 503 |
| `TranscriptionProviderError` | 502 |

## Contrato HTTP (sin cambios respecto al frontend)

Request (`multipart/form-data`):
- `audio`: archivo (webm/mp3/wav/ogg/m4a/mp4/flac, ≤ 10 MB)
- `language`: ISO-639-1, `auto` para detección, o vacío → default `es`
- `task`: `transcribe` (default) o `translate` → inglés
- `normalize`: `code` para invocar `voice_code.normalize_spoken_code`

Response 200:
```json
{
  "text": "Hola mundo",
  "language": "es",
  "confidence": 0.95,
  "segments": [{"start": 0.0, "end": 1.2, "text": "Hola mundo"}],
  "normalized_code": null
}
```

## Pasos de verificación

```bash
pip install -r requirements.txt
pytest tests/unit -v
uvicorn app.main:app --reload
curl -X POST http://localhost:8000/api/v1/transcribe/ \
  -F "audio=@sample.webm" -F "language=es"
```

## Fuera de alcance

- **Frontend**: el contrato HTTP no cambia, no se toca.
- **Migración SQL**: las columnas `is_voice` y `language` se conservan.
- **CHANGELOG histórico**: se conserva como registro de US-12/14/15/17/18/19.


### tiempo de ejecucion :  6m 54 seg
