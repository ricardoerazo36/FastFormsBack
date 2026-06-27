# FastForms — Backend

API REST de **FastForms**, una plataforma de encuestas rápidas. Construida con
**FastAPI** y **Supabase** (Postgres + Auth).

## Stack

- Python 3.11+
- FastAPI + Uvicorn
- Supabase (cliente `supabase-py`)
- Pydantic v2 para validación
- Pytest + httpx para tests

## Estructura

```
app/
  main.py              # Punto de entrada, CORS y registro de routers
  core/config.py       # Configuración y cliente global de Supabase
  api/
    deps.py            # Guards reutilizables (p. ej. inmutabilidad de encuestas)
    routes/
      surveys.py       # Crear, listar y publicar encuestas
      questions.py      # Agregar / editar preguntas (bloqueado si está publicada)
      responses.py      # Registrar respuestas de los encuestados
  schemas/             # Modelos Pydantic (survey, question, response)
  services/
    supabase_service.py # Acceso a datos sobre Supabase
tests/
  unit/                # Tests con Supabase simulado
  integration/         # Tests contra una instancia real de Supabase
```

## Configuración

Crea un archivo `.env` en la raíz del repo:

```
SUPABASE_URL=https://<tu-proyecto>.supabase.co
SUPABASE_KEY=<tu-anon-o-service-key>

# US-12 / US-15 — Servicio de transcripción (Groq Whisper)
# Sin GROQ_API_KEY el endpoint /transcribe devuelve 503.
GROQ_API_KEY=<tu-api-key-de-groq>
# Modelo opcional (default: whisper-large-v3-turbo)
# GROQ_TRANSCRIBE_MODEL=whisper-large-v3
```

## Ejecutar en local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

La API queda en `http://localhost:8000` y la documentación interactiva en
`http://localhost:8000/docs`.

## Tests

```bash
# Solo unitarios (no requieren credenciales)
pytest tests/unit -v

# Integración (requiere .env con credenciales válidas de Supabase)
pytest tests/integration -v
```

## Endpoints principales

| Método | Ruta | Descripción |
| --- | --- | --- |
| `POST` | `/api/v1/surveys/` | Crea una encuesta con sus preguntas (estado `draft`). |
| `GET` | `/api/v1/surveys/` | Lista las encuestas del usuario autenticado y su estado. |
| `PATCH` | `/api/v1/surveys/{id}/publish` | Publica la encuesta (`draft` → `active`). |
| `POST` | `/api/v1/surveys/{id}/questions/` | Agrega una pregunta (403 si la encuesta está publicada). |
| `PUT` / `PATCH` | `/api/v1/surveys/{id}/questions/{qid}` | Edita una pregunta (403 si la encuesta está publicada). |
| `POST` | `/api/v1/responses/` | Registra las respuestas de un encuestado. |
| `POST` | `/api/v1/transcribe/` | Transcribe un audio corto con Groq (Whisper hospedado, US-12). |

> Mientras Auth (US-01) no esté integrado, el `creator_id` se toma del header
> `x-creator-id`.

## Voz con Groq (US-12 a US-17, US-18, US-19)

El endpoint de voz (`POST /api/v1/transcribe/`) está implementado sobre
**Groq**, que ofrece los modelos Whisper (`whisper-large-v3`,
`whisper-large-v3-turbo`) servidos en su infraestructura con latencia muy
baja. El servicio vive en `app/services/groq_service.py` y usa el SDK
oficial `groq` (única dependencia nueva).

### Requisitos

1. `pip install -r requirements.txt` instala el SDK `groq` (>= 1.5.0).
2. Configurar `GROQ_API_KEY` en el `.env`. Sin ella, el endpoint devuelve
   `503 Service Unavailable` (mismo comportamiento que antes cuando el
   modelo local no estaba disponible).
3. Tabla `answers` con las columnas `is_voice` (US-17) y `language` (US-18).
   Ver `base.sql` y las migraciones idempotentes que incluye. Las columnas
   se siguen leyendo/escribiendo directamente desde el servicio.

### Cómo consumir el endpoint

`POST /api/v1/transcribe/` — `multipart/form-data` con:

| Campo | Tipo | Descripción |
| --- | --- | --- |
| `audio` | archivo | webm / mp3 / wav / ogg / m4a / mp4 / flac, ≤ 10 MB |
| `language` | string (opcional) | ISO 639-1 (`es`), `auto` para detectar (US-18), o vacío → default `es` |
| `task` | string (opcional) | `transcribe` (def.) o `translate` → inglés (US-18) |
| `normalize` | string (opcional) | `code` para normalizar a código de encuesta (US-19) |

Respuesta (`200 OK`):

```json
{
  "text": "Hola mundo",
  "language": "es",
  "confidence": 0.95,
  "segments": [{ "start": 0.0, "end": 1.2, "text": "Hola mundo" }],
  "normalized_code": null
}
```

Errores: `400` (formato), `413` (excede 10 MB), `502` (fallo del proveedor)
y `503` (sin `GROQ_API_KEY` o SDK no instalado).

Ejemplos con `curl`:

```bash
# Transcripción simple (es)
curl -X POST http://localhost:8000/api/v1/transcribe/ \
  -F "audio=@clip.webm" -F "language=es"

# Detección automática de idioma (US-18)
curl -X POST http://localhost:8000/api/v1/transcribe/ \
  -F "audio=@clip.webm" -F "language=auto"

# Código de encuesta por voz (US-19)
curl -X POST http://localhost:8000/api/v1/transcribe/ \
  -F "audio=@clip.webm" -F "normalize=code"
```

### Multilingüe (US-18)

`language=auto` deja que Groq detecte el idioma del audio sin
configuración previa; el idioma detectado vuelve en `language` y se persiste
en `answers.language` para etiquetarlo en el dashboard. `task=translate` usa
el endpoint de traducciones de Groq para devolver el texto en inglés (útil
para unificar el análisis de respuestas multilingües).

### Código por voz (US-19)

Con `normalize=code`, el backend pasa la transcripción por
`services/voice_code.normalize_spoken_code`, que convierte números y letras
dictados a una cadena `[A-Z0-9]` ("a siete equis nueve ka" → `A7X9K`,
"A-7-X-9-K" → `A7X9K`). El frontend muestra el código interpretado para
confirmar antes de validar.

### Modelo y umbral de confianza

Groq no expone una probabilidad global por respuesta, pero sí la confianza
por palabra (`words[].confidence`) cuando se pide `verbose_json` con
`timestamp_granularities=["word"]`. El servicio promedia esos valores y
devuelve un score en `[0, 1]`. El frontend lo usa como umbral para la
selección por voz (US-15) antes de marcar una opción automáticamente.

El modelo por defecto es `whisper-large-v3-turbo` (más rápido). Si
necesitás más precisión podés fijar `GROQ_TRANSCRIBE_MODEL=whisper-large-v3`.
