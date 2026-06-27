from pydantic import BaseModel
from typing import List, Optional

from schemas.question import QuestionType
from schemas.survey import SurveyStatus


class OptionResult(BaseModel):
    option: str
    count: int
    percentage: float


class QuestionResult(BaseModel):
    question_id: int
    content: str
    question_type: QuestionType
    total_answers: int
    # Para preguntas de opción (multiple_choice / yes_no)
    options: Optional[List[OptionResult]] = None
    # Para preguntas abiertas (open) — lista plana de textos.
    texts: Optional[List[str]] = None


class SurveyResults(BaseModel):
    survey_id: int
    title: str
    status: SurveyStatus
    total_responses: int
    questions: List[QuestionResult]
