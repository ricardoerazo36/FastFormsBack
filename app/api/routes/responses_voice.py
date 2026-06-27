"""US-15 t2 — `POST /api/v1/responses/auto-fill`.

Recibe un audio + el `unique_code` de una encuesta y devuelve un payload con
la misma forma que `ResponseCreate` (sin persistir), para que el frontend
autorrellene el formulario. El usuario revisa, corrige si quiere, y solo
entonces hace `POST /api/v1/responses/` para persistir.

Anonimato: igual que `/transcribe` y `/responses/`, el endpoint es accesible
sin JWT (`get_optional_user_id`).

Orquestación:
  audio -> groq_service.transcribe_audio
        -> supabase_service.get_survey_by_code
        -> autofill_service.generate_auto_fill
        -> AutoFillResponse
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from api.deps import get_optional_user_id
from schemas.response import AutoFillResponse
from services import autofill_service, groq_service, supabase_service

router = APIRouter(prefix="/responses", tags=["Responses · Voice"])


@router.post(
    "/auto-fill",
    response_model=AutoFillResponse,
    status_code=status.HTTP_200_OK,
    summary="Autorrellenar una encuesta a partir de un audio + código",
)
async def auto_fill_response(
    audio: UploadFile = File(..., description="Audio webm/mp3/wav, ≤ 10MB"),
    code: str = Form(..., description="Código único de la encuesta (case-insensitive)."),
    language: str | None = Form(
        default=None,
        description='Código ISO, "auto" para detectar (US-18). Default: "es".',
    ),
    _user_id: str | None = Depends(get_optional_user_id),
):
    """Transcribe el audio, busca la encuesta por `code` y devuelve las
    respuestas sugeridas por Gemini (con `answer_text=null` en lo que no se
    haya podido mapear). No persiste nada.

    Errores:
      - 400 formato de audio no soportado
      - 404 `code` no existe
      - 409 encuesta no está en `active`
      - 413 audio > 10 MB
      - 422 falta `code` o `audio`
      - 502 fallo de Groq o Gemini
      - 503 falta `GROQ_API_KEY` o `GEMINI_API_KEY`
    """
    raw_code = (code or "").strip()
    if not raw_code:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El código de la encuesta es obligatorio.",
        )

    audio_bytes = await audio.read()

    # 1) Transcripción (Groq). Ya valida formato y tamaño.
    try:
        transcript = groq_service.transcribe_audio(
            filename=audio.filename or "audio.webm",
            content_type=audio.content_type or "",
            data=audio_bytes,
            language=language,
            task="transcribe",
        )
    except groq_service.TranscriptionFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )
    except groq_service.TranscriptionSizeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)
        )
    except groq_service.GroqConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    except groq_service.TranscriptionProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )

    # 2) Búsqueda de la encuesta por código.
    survey = supabase_service.get_survey_by_code(raw_code)
    if survey is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No existe una encuesta con ese código.",
        )

    # 3) Estado: solo `active` puede recibir respuestas (alineado con POST /responses/).
    if survey.get("status") != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "La encuesta no está activa para recibir respuestas "
                f"(estado: '{survey.get('status')}')."
            ),
        )

    # 4) Gemini: mapea la transcripción a las preguntas de la encuesta.
    try:
        result = autofill_service.generate_auto_fill(
            survey=survey,
            transcript=transcript.get("text") or "",
            detected_language=transcript.get("language"),
        )
    except autofill_service.GeminiConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    except autofill_service.GeminiParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )
    except autofill_service.GeminiProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        )

    return AutoFillResponse(**result)
