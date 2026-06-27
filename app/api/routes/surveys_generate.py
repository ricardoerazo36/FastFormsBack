"""US-13 — Endpoint POST /api/v1/surveys/generate.

Recibe un prompt libre del usuario y un número de preguntas, y devuelve un
borrador de encuesta generado por Gemini. **No persiste nada en Supabase**:
el frontend inyecta el JSON en el formulario de creación y el usuario decide
si crear la encuesta (`POST /surveys`) o guardarla como borrador
(`POST /surveys/draft`).
"""

from fastapi import APIRouter, Depends, HTTPException, status

from api.deps import get_current_user_id
from schemas.generate import GenerateSurveyRequest, GenerateSurveyResponse
from services import gemini_service

router = APIRouter(prefix="/surveys", tags=["AI Generation"])


@router.post(
    "/generate",
    response_model=GenerateSurveyResponse,
    status_code=status.HTTP_200_OK,
    summary="Generar un borrador de encuesta con IA (Gemini)",
)
def generate_survey(
    body: GenerateSurveyRequest,
    _user_id: str = Depends(get_current_user_id),
):
    """Genera un borrador de encuesta a partir de la idea del usuario.

    El response tiene la misma forma que un `SurveyCreate` (sin `creator_id`),
    de modo que el frontend puede hidratar el formulario de creación con un
    único `setState(...)`.
    """
    try:
        return gemini_service.generate_survey_draft(
            user_prompt=body.prompt,
            num_questions=body.num_questions,
            language=body.language,
        )
    except gemini_service.GeminiConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except gemini_service.GeminiParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )
    except gemini_service.GeminiProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )
