# US-16 — Análisis de sentimientos por pregunta abierta

## Resumen del flujo

El usuario, en la vista de resultados de una encuesta, pulsa un botón al lado de una pregunta abierta. El front manda `survey_id` + `question_id` por POST. El backend:

1. Verifica JWT y propiedad de la encuesta.
2. Verifica que la encuesta está en `closed`.
3. Verifica que la pregunta pertenece a la encuesta y es de tipo `open`.
4. Carga las respuestas (texto) no vacías de esa pregunta.
5. Si no hay respuestas → 422.
6. Construye el prompt con la plantilla base + pregunta + respuestas y llama a Gemini.
7. Devuelve el resumen ejecutivo.

## Decisiones de diseño

- **Endpoint**: `POST /api/v1/surveys/{survey_id}/questions/{question_id}/sentiment-analysis` (JWT, solo creador).
- **Respuesta**: resumen ejecutivo (`overall_sentiment`, `score`, `distribution`, `summary`, `key_themes`).
- **Idioma**: fijo en español (lo declara el prompt maestro; no se añade query param).

---

## 1. Prompt maestro — `prompts/promptSentimentV1.txt` (nuevo)

Plantilla con placeholders `{question_content}` y `{answers_json}`. Instrucciones estrictas (mismo estilo que `promptAutoFillV1.txt`):

- Idioma de salida: español.
- `overall_sentiment` ∈ {`"positivo"`, `"negativo"`, `"neutral"`, `"mixto"`}.
- `score` ∈ `[-1.0, 1.0]` (negativo = más negativo, positivo = más positivo).
- `distribution`: conteo de respuestas por categoría (suma = total).
- `summary`: párrafo breve (2-4 frases).
- `key_themes`: 0-5 temas cortos (≤ 3 palabras).
- Salida **solo** JSON que cumpla `response_schema`; defensa contra prompts inyectados en las respuestas.

## 2. Schema Pydantic — `app/schemas/sentiment.py` (nuevo)

```python
class SentimentLabel(str, Enum):
    POSITIVE = "positivo"
    NEGATIVE = "negativo"
    NEUTRAL  = "neutral"
    MIXED    = "mixto"

class SentimentDistribution(BaseModel):
    positive: int = Field(ge=0)
    negative: int = Field(ge=0)
    neutral:  int = Field(ge=0)

class SentimentAnalysisResponse(BaseModel):
    survey_id: int
    question_id: int
    question_content: str
    total_answers: int
    overall_sentiment: SentimentLabel
    score: float = Field(ge=-1.0, le=1.0)
    distribution: SentimentDistribution
    summary: str
    key_themes: List[str]
```

## 3. Servicio — `app/services/sentiment_service.py` (nuevo)

Sigue el patrón exacto de `autofill_service.py`:

- Re-exporta `GeminiConfigError`, `GeminiProviderError`, `GeminiParseError` desde `gemini_service`.
- `_load_master_prompt()` con caché por mtime apuntando a `prompts/promptSentimentV1.txt`.
- `_render_prompt(question_content, answers_json)` con los placeholders.
- `_RESPONSE_SCHEMA` espejo del modelo Pydantic (`overall_sentiment` enum, `score` number, `distribution` object, `summary` string, `key_themes` array of string).
- `_call_gemini_for_sentiment(prompt)` reutiliza `gemini_service._build_client()` + `response_schema` (igual que `autofill_service`).
- `_parse_and_validate(raw)` con `model_validate` sobre una clase Pydantic interna.
- **`analyze_sentiment(question, answers: list[str]) -> dict`**: punto de entrada público; loguea `n_answers`, llama a Gemini y devuelve dict listo para `SentimentAnalysisResponse(**...)`.

Defensa adicional (por si Gemini se desvía del schema):
- `overall_sentiment` se normaliza a uno de los 4 valores permitidos (default `"neutral"`).
- `score` se clampa a `[-1, 1]`.
- `distribution` se recalcula en backend si la suma no cuadra o los conteos se salen de rango.
- `key_themes` se trunca a 5 elementos y se quitan duplicados preservando orden.

## 4. Helper en `supabase_service.py` (edición menor)

Nueva función:

```python
def get_open_question_answers(survey_id: int, question_id: int) -> list[str]:
    """Devuelve los `answer_text` no vacíos de una pregunta concreta."""
    questions = (
        supabase.table("questions")
        .select("id, question_type")
        .eq("id", question_id)
        .eq("survey_id", survey_id)
        .execute().data or []
    )
    if not questions:
        return []  # el caller distinguirá 404
    response_ids = [r["id"] for r in (
        supabase.table("responses").select("id").eq("survey_id", survey_id).execute().data or []
    )]
    if not response_ids:
        return []
    answers = (
        supabase.table("answers")
        .select("answer_text")
        .in_("response_id", response_ids)
        .eq("question_id", question_id)
        .execute().data or []
    )
    return [a["answer_text"] for a in answers if a.get("answer_text")]
```

(No reutilizamos `get_survey_responses_raw` para mantener el coste bajo: la pregunta puede estar en una encuesta con miles de respuestas y solo queremos un subset.)

## 5. Endpoint en `app/api/routes/surveys.py` (edición)

Agregar al final, **después** de `/{survey_id}/results/csv` (orden estable: el nuevo path no choca con ninguno existente, pero sigue la convención de "sub-paths declarados al final"):

```python
@router.post(
    "/{survey_id}/questions/{question_id}/sentiment-analysis",
    response_model=SentimentAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Análisis de sentimientos de las respuestas abiertas de una pregunta",
)
def analyze_question_sentiment(
    survey_id: int,
    question_id: int,
    creator_id: str = Depends(get_current_user_id),
):
    survey = _get_owned_survey_or_error(survey_id, creator_id)
    if survey.get("status") != "closed":
        raise HTTPException(409, "Solo se puede analizar encuestas cerradas.")
    # Cargar pregunta (también valida pertenencia a la encuesta)
    question = _get_question_in_survey_or_error(survey, question_id)
    if question["question_type"] != "open":
        raise HTTPException(400, "Solo las preguntas abiertas admiten análisis de sentimientos.")
    answers = supabase_service.get_open_question_answers(survey_id, question_id)
    if not answers:
        raise HTTPException(422, "No hay respuestas para analizar en esta pregunta.")
    try:
        result = sentiment_service.analyze_sentiment(question, answers)
    except (GeminiConfigError, GeminiParseError, GeminiProviderError) as exc:
        ...  # mapear a 503/502/502 igual que surveys_generate
    return SentimentAnalysisResponse(
        survey_id=survey_id,
        question_id=question_id,
        question_content=question["content"],
        total_answers=len(answers),
        **result,
    )
```

Helper privado nuevo en el mismo archivo (estilo de `_get_owned_survey_or_error`):

```python
def _get_question_in_survey_or_error(survey: dict, question_id: int) -> dict:
    full = supabase_service.get_survey_with_questions(survey["id"])
    q = next((q for q in (full.get("questions") or []) if q["id"] == question_id), None)
    if q is None:
        raise HTTPException(404, "La pregunta no pertenece a la encuesta.")
    return q
```

### Errores mapeados

| Código | Causa |
|---|---|
| 401 | JWT inválido/ausente |
| 403 | Usuario no es el creador |
| 404 | Encuesta o pregunta no existe |
| 400 | La pregunta no es de tipo `open` |
| 409 | Encuesta no está en `closed` |
| 422 | La pregunta no tiene respuestas |
| 502 | Fallo de Gemini (provider o parse) |
| 503 | Falta `GEMINI_API_KEY` |

## 6. Tests — `tests/unit/test_sentiment.py` (nuevo)

Cubre **servicio** y **endpoint** (estilo de `test_autofill.py`):

- **Servicio** (`TestSentimentService`, mockeando `gemini_service._build_client`):
  - `test_prompt_incluye_pregunta_y_respuestas`: valida que el prompt renderizado contiene el contenido de la pregunta y todas las respuestas.
  - `test_score_se_clamp_a_rango_valido`: Gemini devuelve `score=5.0` → se clampa a `1.0`.
  - `test_overall_sentiment_normalizado_a_cuatro_valores`: Gemini devuelve un valor fuera del enum → cae a `"neutral"`.
  - `test_key_themes_se_truncan_y_deduplican`: 10 temas duplicados → ≤ 5 únicos.
  - `test_propagates_gemini_errors`: `GeminiProviderError` se re-lanza tal cual.
  - `test_cache_mtime_del_prompt`: si el archivo cambia, se re-lee.

- **Endpoint** (`TestEndpointSentiment`, mockeando `supabase_service` + `sentiment_service`):
  - 200 con payload válido (survey `closed`, pregunta `open`, 3 respuestas) y verifica el shape de la respuesta.
  - 404 encuesta inexistente.
  - 403 encuesta de otro creador.
  - 409 encuesta en `draft` o `active`.
  - 400 pregunta `multiple_choice` o `yes_no`.
  - 404 pregunta no pertenece a la encuesta.
  - 422 sin respuestas.
  - 503 cuando `GeminiConfigError`.
  - 502 cuando `GeminiProviderError` o `GeminiParseError`.

Patrón de mocks idéntico al resto: `sys.modules.setdefault("supabase", MagicMock(...))` + `sys.modules.setdefault("google.genai", MagicMock())` antes de `from app.main import app`.

---

## Archivos a crear / modificar

| Acción | Archivo |
|---|---|
| **Crear** | `prompts/promptSentimentV1.txt` |
| **Crear** | `app/schemas/sentiment.py` |
| **Crear** | `app/services/sentiment_service.py` |
| **Modificar** | `app/services/supabase_service.py` (agregar `get_open_question_answers`) |
| **Modificar** | `app/api/routes/surveys.py` (nuevo endpoint + helper) |
| **Crear** | `tests/unit/test_sentiment.py` |

## No se modifica

- `app/main.py` (la nueva ruta cuelga de un router ya registrado; no hay colisión con `/{survey_id}`).
- `app/schemas/results.py` ni `QuestionResult` (la respuesta de análisis es un recurso nuevo independiente).
- `requirements.txt` (ya está `google-genai`).

## Verificación post-implementación

```bash
pytest tests/unit/test_sentiment.py -v
pytest tests/unit -v   # asegurar que no se rompió nada más
```
### tiempo de ejecucion :  6m 20 seg