# US-13 — Generador de encuestas con Gemini

## Resumen

Nuevo endpoint `POST /api/v1/surveys/generate` que recibe `{prompt, num_questions}` y devuelve un JSON `{title, questions: [...]}` listo para inyectar en el formulario de creación existente del frontend. **No persiste nada en Supabase**: el usuario revisa/edita y luego decide si crear (`POST /surveys`) o guardar como borrador (`POST /surveys/draft`).

## Decisiones de diseño

- **Persistencia**: solo devuelve JSON, no guarda. El front inyecta en el formulario y el usuario decide.
- **Distribución de tipos**: el modelo decide libremente (mezcla los 3 tipos cuando tenga sentido).
- **Auth**: requiere JWT (`get_current_user_id`).
- **API key**: se renombra `GENAI_API_KEY` → `GEMINI_API_KEY` y se usa la SDK oficial `google-genai` (`>=1.0`).
- **Prompt maestro**: archivo versionado `promptbeta.txt` en la raíz, cargado en cada request (con caché en memoria).

## Archivos a crear

| Archivo | Propósito |
|---|---|
| `app/services/gemini_service.py` | Cliente de Gemini, carga el prompt maestro, parsea y valida la respuesta contra el schema |
| `app/schemas/generate.py` | Pydantic `GenerateSurveyRequest` y `GenerateSurveyResponse` (reutiliza `QuestionType`) |
| `app/api/routes/surveys_generate.py` | Router con `POST /api/v1/surveys/generate` |
| `promptbeta.txt` | Prompt maestro versionado (cargado en cada request) |
| `tests/unit/test_generate_survey.py` | Tests unitarios con el SDK mockeado |
| `docs/promptus13.md` | Este documento |

## Archivos a modificar

| Archivo | Cambio |
|---|---|
| `app/core/config.py` | Añadir `GEMINI_API_KEY` y `GEMINI_MODEL` (default `gemini-2.0-flash`) a `Settings` |
| `app/main.py` | `app.include_router(surveys_generate.router, prefix="/api/v1")` **antes** de surveys |
| `requirements.txt` | Añadir `google-genai>=1.0.0` |
| `.env` | Renombrar `GENAI_API_KEY` → `GEMINI_API_KEY` (mantener el valor actual) |
| `CHANGELOG.md` | Entrada nueva |
| `AGENTS.md` | Sección "AI generation" |

## Contrato de la API

### Request

`POST /api/v1/surveys/generate`

```json
{
  "prompt": "vamos a ordenar comida grupal y necesito preferencias y alergias",
  "num_questions": 6,
  "language": "es"
}
```

### Response 200

```json
{
  "title": "Pedido grupal de comida",
  "questions": [
    { "content": "¿Qué tipo de cocina prefieres?", "question_type": "multiple_choice",
      "options": ["Italiana", "Mexicana", "Japonesa", "Otra"], "position": 0 },
    { "content": "¿Tienes alguna alergia alimentaria?", "question_type": "open",
      "options": null, "position": 1 },
    { "content": "¿Te animas a compartir platos?", "question_type": "yes_no",
      "options": null, "position": 2 }
  ]
}
```

### Errores

| Código | Causa |
|---|---|
| 400 | `prompt` vacío / `num_questions` fuera de [1, 12] (Pydantic) |
| 401 | JWT ausente o inválido |
| 502 | Gemini caído, 429 quota, JSON no parseable |
| 503 | `GEMINI_API_KEY` no configurada |

## Detalles de implementación

### 1. `promptbeta.txt` — prompt maestro

Estructura: instrucciones estrictas + few-shot example. Marca el JSON como único output permitido y blinda contra prompt injection (le dice al modelo que ignore instrucciones del usuario que pidan cambiar el formato).

### 2. `app/services/gemini_service.py`

Patrón inspirado en `groq_service.py` (servicio de transcripción):

- Excepciones tipadas: `GeminiConfigError` (503), `GeminiProviderError` (502), `GeminiParseError` (502).
- `generate_survey_draft(prompt, num_questions, language)`:
  1. Carga prompt maestro (con caché).
  2. Construye prompt final = master + `N=num_questions, language, user_prompt`.
  3. Llama a `client.models.generate_content` con `response_mime_type="application/json"` y `response_schema` (OpenAPI 3.0 subset) que valida `{title, questions: [...]}`.
  4. `json.loads(response.text)` → valida con Pydantic.
  5. Si falla validación, 1 reintento con instrucción más estricta.
  6. Devuelve dict compatible con `GenerateSurveyResponse`.

### 3. `app/schemas/generate.py`

```python
class GenerateSurveyRequest(BaseModel):
    prompt: str = Field(min_length=5, max_length=2000)
    num_questions: int = Field(ge=1, le=12)
    language: str = Field(default="es", min_length=2, max_length=8)

class GeneratedQuestion(BaseModel):
    content: str
    question_type: QuestionType
    options: Optional[List[str]] = None
    position: int
    # validadores coherentes con QuestionCreate

class GenerateSurveyResponse(BaseModel):
    title: str
    questions: List[GeneratedQuestion]
```

### 4. `app/api/routes/surveys_generate.py`

- Router con `prefix="/surveys"`, `tags=["AI Generation"]`.
- `POST /generate` con `Depends(get_current_user_id)`.
- Mapea cada excepción del servicio al código HTTP correspondiente.

**Orden de registro en `main.py`**: el router nuevo debe incluirse **antes** que el de `surveys` para que la ruta estática `/surveys/generate` no sea capturada por `/{survey_id}`.

### 5. Tests

Patrón idéntico a `test_transcribe.py` (endpoint transcribe):

- Mockear `google_genai` (o `google.genai`) en `sys.modules` antes de importar `app.main`.
- Mockear `_build_client` en el servicio para devolver un `MagicMock` cuyo `models.generate_content(...).text` devuelve JSON controlado.
- Casos: happy path, validaciones 400, 503 sin API key, 502 parse error, 401 sin JWT, bypass con `dependency_overrides`.

## Orden de ejecución

1. `requirements.txt` → añadir `google-genai>=1.0.0`
2. `promptbeta.txt` en la raíz
3. `.env` → renombrar `GENAI_API_KEY` → `GEMINI_API_KEY`
4. `app/core/config.py` → settings
5. `app/schemas/generate.py`
6. `app/services/gemini_service.py`
7. `app/api/routes/surveys_generate.py`
8. `app/main.py` → `include_router` antes de surveys
9. `CHANGELOG.md`, `AGENTS.md`
10. `pip install -r requirements.txt`
11. `tests/unit/test_generate_survey.py`
12. `pytest tests/unit -v`

## Riesgos

- **API key sospechosa**: el prefijo `AQ.` se parece a tokens de ADC de Google Cloud. Si la key no funciona con `genai.Client(api_key=...)`, se reevaluará.
- **JSON Schema en Gemini**: `response_schema` exige OpenAPI 3.0 subset. Fallback a `response_mime_type="application/json"` + parseo + validación Pydantic si rechaza el schema.
- **Cuota**: manejo explícito de 429 → 502 con mensaje claro.
- **Prompt injection**: el `prompt` del usuario se concatena al prompt maestro; se le indica al modelo que ignore instrucciones que pidan saltarse el formato.
- **Costo/latencia**: cada request a Gemini cuesta tokens y tarda 1-5s. Logear `num_questions` y longitud del prompt (sin contenido) para observabilidad futura.


### Tiempo de ejecucion : 7min 53 seg