from pydantic import BaseModel, field_validator
from typing import List, Optional
from datetime import datetime


class AnswerCreate(BaseModel):
    question_id: int
    answer_text: str

    @field_validator("answer_text")
    @classmethod
    def answer_no_vacia(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("La respuesta no puede estar vacía.")
        return v.strip()


class ResponseCreate(BaseModel):
    survey_id: int
    answers: List[AnswerCreate]

    @field_validator("answers")
    @classmethod
    def al_menos_una_respuesta(cls, v: List[AnswerCreate]) -> List[AnswerCreate]:
        if not v:
            raise ValueError("Debes responder al menos una pregunta.")
        return v


# ---------------------------------------------------------------------------
# US-15 t2 — Auto-fill por voz.
# Misma forma que ResponseCreate, pero `answer_text` puede ser `null` (la IA
# no encontró respuesta en la transcripción) y `answers` puede ser `[]` (la
# transcripción no contenía respuestas válidas). Se usa como DTO de salida de
# `POST /api/v1/responses/auto-fill` para que el frontend autorrellene el
# formulario y luego el usuario haga el `POST /responses/` definitivo.
# ---------------------------------------------------------------------------


class AnswerDraft(BaseModel):
    """Respuesta sugerida por la IA. `answer_text` puede ser `None`."""

    question_id: int
    answer_text: Optional[str] = None


class AutoFillResponse(BaseModel):
    survey_id: int
    answers: List[AnswerDraft] = []


class AnswerResult(BaseModel):
    id: int
    response_id: int
    question_id: int
    answer_text: str


class ResponseResult(BaseModel):
    id: int
    survey_id: int
    submitted_at: datetime
    answers: List[AnswerResult]
