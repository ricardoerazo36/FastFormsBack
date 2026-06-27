"""US-16 — Schemas del análisis de sentimientos por pregunta abierta.

El endpoint `POST /surveys/{survey_id}/questions/{question_id}/sentiment-analysis`
devuelve un resumen ejecutivo del sentimiento agregado de las respuestas
abiertas de una pregunta. La respuesta está pensada para que el frontend la
muestre en el dashboard de resultados (sin necesidad de persistir nada en BD).
"""

from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class SentimentLabel(str, Enum):
    """Etiquetas permitidas para `overall_sentiment`.

    El backend acepta los valores que Gemini pueda emitir y los normaliza a
    uno de los cuatro canónicos.
    """

    POSITIVE = "positivo"
    NEGATIVE = "negativo"
    NEUTRAL = "neutral"
    MIXED = "mixto"


class SentimentDistribution(BaseModel):
    """Conteo agregado de respuestas por categoría de sentimiento.

    La suma `positive + negative + neutral` debe coincidir con
    `SentimentAnalysisResponse.total_answers`.
    """

    positive: int = Field(ge=0, description="Respuestas con sentimiento positivo.")
    negative: int = Field(ge=0, description="Respuestas con sentimiento negativo.")
    neutral: int = Field(ge=0, description="Respuestas neutras o sin emoción clara.")


class SentimentAnalysisResponse(BaseModel):
    """Salida del endpoint de análisis de sentimientos."""

    survey_id: int = Field(description="ID de la encuesta analizada.")
    question_id: int = Field(description="ID de la pregunta analizada.")
    question_content: str = Field(
        description="Enunciado de la pregunta (incluido para que el front no tenga que cruzar con /results)."
    )
    total_answers: int = Field(
        ge=0, description="Número de respuestas abiertas consideradas en el análisis."
    )
    overall_sentiment: SentimentLabel = Field(
        description="Etiqueta agregada del sentimiento del grupo."
    )
    score: float = Field(
        ge=-1.0,
        le=1.0,
        description="Score en [-1.0, 1.0]. Negativo = más negativo, positivo = más positivo.",
    )
    distribution: SentimentDistribution = Field(
        description="Conteo de respuestas por categoría."
    )
    summary: str = Field(
        min_length=1,
        description="Párrafo breve (2-4 frases) con el resumen ejecutivo en español.",
    )
    key_themes: List[str] = Field(
        default_factory=list,
        description="Hasta 5 temas cortos recurrentes en las respuestas.",
    )
