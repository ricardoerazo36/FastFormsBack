# FastForms Backend — Agent Guide

## Stack

Python 3.11+ · FastAPI + Uvicorn · Supabase (supabase-py) · Pydantic v2 · pytest + httpx

## Commands

```bash
pip install -r requirements.txt          # setup
uvicorn app.main:app --reload            # dev server at :8000
pytest tests/unit -v                     # unit tests (no creds needed)
pytest tests/integration -v              # integration (needs .env)
pytest tests/unit/test_foo.py -v -k test_bar  # single test
```

No linter/formatter is configured.

## Setup

- `.env` at repo root with `SUPABASE_URL` + `SUPABASE_KEY` (required at import time).
- Voice transcription (Groq): `GROQ_API_KEY` (required for `/transcribe`, otherwise the endpoint returns 503). Optional: `GROQ_TRANSCRIBE_MODEL` (default `whisper-large-v3-turbo`).
- `FRONTEND_URL` controls CORS origins (default `http://localhost:5173`).

## Critical architecture

- **`app/main.py`** inserts `app/` dir into `sys.path` (line 7) — important for import resolution in tests.
- **`app/core/config.py`** runs at import time: creates `Settings` and global `supabase` client. Tests must mock `supabase` module **before** importing `app.main`.
- All routes under `/api/v1/`. Four routers: `surveys`, `questions`, `responses`, `transcribe`.
- Route ordering matters: `/surveys/draft` must be declared before `/surveys/{survey_id}`.
- Auth: JWT Bearer via `get_current_user_id` (required). Exception: `/transcribe` uses `get_optional_user_id` (anonymous allowed).
- Pydantic v2: use `model_dump()` not `.dict()`.

## Testing quirks

- **Unit tests mock Supabase at the module level BEFORE importing the app.** Pattern in every test file:
  ```python
  import sys
  from unittest.mock import MagicMock
  _mock = MagicMock()
  sys.modules.setdefault("supabase", MagicMock(create_client=lambda url, key: _mock))
  # then: from app.main import app
  ```
- The shared `tests/unit/conftest.py` sets `SUPABASE_URL`/`SUPABASE_KEY` env vars and mocks `supabase` — but individual test files also do it defensively.
- `pytest.ini` sets `pythonpath = .`; no `PYTHONPATH` export needed.
- Integration tests (`tests/integration/`) require a real Supabase instance with valid `.env`.
- `app.dependency_overrides` is used to bypass JWT auth in endpoint tests (see `test_transcribe.py:_override_auth`).
- Tests for `groq_service` mock `app.services.groq_service._get_client` to avoid real HTTP calls; the cached client (`_client`) is reset between tests when needed.

## Key domain rules

- **Immutability (US-04)**: surveys in `draft` are editable. Once `active` or `closed`, questions/routes reject modifications with 403. Guard in `app/api/deps.py:assert_survey_in_status`.
- **States**: `draft` → `active` (publish) → `closed` (irreversible).

## AI generation (US-13)

- Endpoint: `POST /api/v1/surveys/generate` (JWT requerido). Recibe
  `{prompt, num_questions (1-12), language}` y devuelve `{title, questions}`
  listo para hidratar el formulario de creación. No persiste en Supabase.
- Servicio: `app/services/gemini_service.py`. Excepciones tipadas
  (`GeminiConfigError` → 503, `GeminiProviderError`/`GeminiParseError` → 502).
- SDK: `google-genai` (`pip install google-genai`). `response_mime_type`
  y `response_schema` garantizan JSON estructurado.
- Prompt maestro: `promptbeta.txt` en la raíz del repo. Cargado con caché
  en memoria (se re-lee si cambia el mtime). Iterar el prompt no requiere
  cambios de código.
- Variables: `GEMINI_API_KEY` (requerida), `GEMINI_MODEL` (default
  `gemini-2.0-flash`).
- Orden de routers: `surveys_generate` se incluye ANTES de `surveys` en
  `app/main.py` para que `/surveys/generate` no sea capturado por
  `/{survey_id}`.

## Voice (Groq)

- Servicio: `app/services/groq_service.py` (SDK oficial `groq`).
- Modelos: `whisper-large-v3-turbo` (default) o `whisper-large-v3`, seleccionables vía `GROQ_TRANSCRIBE_MODEL`.
- Audio limits: ≤ 10 MB, formats: webm/mp3/wav/ogg/m4a/mp4/flac.
- Error codes: 400 (format), 413 (size), 502 (provider), 503 (sin `GROQ_API_KEY`).
- `language=auto` deja que Groq detecte el idioma; default es `es` cuando se omite.
- `confidence` se calcula como promedio de la confianza por palabra devuelta en `verbose_json`.
- `normalize=code` param runs `voice_code.normalize_spoken_code()` (US-19).

## Voice-driven auto-fill (US-15 t2)

- Endpoint: `POST /api/v1/responses/auto-fill` (anónimo, igual que `/transcribe` y `/responses/`).
- Inputs (`multipart/form-data`): `audio` (UploadFile, ≤ 10MB), `code` (Form, `unique_code` de la encuesta), `language` (opcional).
- Orquestación: `groq_service.transcribe_audio` → `supabase_service.get_survey_by_code` → `autofill_service.generate_auto_fill` (Gemini con `response_schema` + prompt maestro).
- Output: `AutoFillResponse` (`schemas/response.py`) — **misma forma que `ResponseCreate`** (`{survey_id, answers: [{question_id, answer_text}]}`) pero `answer_text` puede ser `null` y `answers` puede ser `[]`.
- El endpoint **no persiste nada**: el frontend autorrellena el formulario, el usuario revisa/corrige y solo entonces hace `POST /api/v1/responses/`.
- Estado de la encuesta: solo `active` (409 si está en `draft` o `closed`).
- Servicio: `app/services/autofill_service.py`. Re-exporta las excepciones de `gemini_service` (`GeminiConfigError` → 503, `GeminiProviderError`/`GeminiParseError` → 502).
- Prompt maestro: `prompts/promptAutoFillV1.txt` con caché por mtime. Forza a Gemini a devolver exactamente las opciones declaradas en `multiple_choice` / `yes_no`, o `null` si no respondió / fue ambiguo.
- Router: `app/api/routes/responses_voice.py`, registrado **antes** de `responses.py` en `main.py` (path estático para no chocar con futuros `/{response_id}`).
- Tests: `tests/unit/test_autofill.py` (31 tests, todos en verde).
