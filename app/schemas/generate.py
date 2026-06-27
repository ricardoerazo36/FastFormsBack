"""US-13 — Schemas del generador de encuestas con IA."""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Optional

from schemas.question import QuestionType


class GenerateSurveyRequest(BaseModel):
    """Entrada del endpoint POST /api/v1/surveys/generate."""

    prompt: str = Field(
        min_length=5,
        max_length=2000,
        description="Idea o contexto libre que el usuario da para inspirar la encuesta.",
    )
    num_questions: int = Field(
        ge=1,
        le=12,
        description="Cantidad de preguntas a generar (1-12, alineado con el límite de SurveyCreate).",
    )
    language: str = Field(
        default="es",
        min_length=2,
        max_length=8,
        description='Código ISO del idioma de salida (ej. "es", "en", "pt-BR").',
    )

    @field_validator("prompt")
    @classmethod
    def prompt_no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El prompt no puede estar vacío.")
        return v.strip()

    @field_validator("language")
    @classmethod
    def language_normalizado(cls, v: str) -> str:
        return v.strip().lower()


class GeneratedQuestion(BaseModel):
    """Pregunta generada por la IA. Mismas reglas de validación que QuestionCreate."""

    content: str
    question_type: QuestionType
    options: Optional[List[str]] = None
    position: int

    @field_validator("content")
    @classmethod
    def content_no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El enunciado de la pregunta no puede estar vacío.")
        return v.strip()

    @field_validator("options")
    @classmethod
    def opciones_no_vacias(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is not None:
            if any(not opt or not opt.strip() for opt in v):
                raise ValueError("Ninguna opción puede estar vacía.")
        return v

    @model_validator(mode="after")
    def validar_opciones_segun_tipo(self) -> "GeneratedQuestion":
        if self.question_type == QuestionType.MULTIPLE_CHOICE:
            if not self.options or len(self.options) < 2:
                raise ValueError(
                    "Las preguntas de opción múltiple deben tener al menos 2 opciones."
                )
        elif self.question_type in (QuestionType.OPEN, QuestionType.YES_NO):
            if self.options:
                raise ValueError(
                    f"Las preguntas de tipo '{self.question_type}' no deben incluir opciones."
                )
        return self


class GenerateSurveyResponse(BaseModel):
    """Salida que el frontend inyecta en el formulario de creación."""

    title: str
    questions: List[GeneratedQuestion]

    @field_validator("title")
    @classmethod
    def title_no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El título de la encuesta no puede estar vacío.")
        return v.strip()
