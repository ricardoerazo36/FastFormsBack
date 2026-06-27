from typing import List, Optional

from pydantic import BaseModel


class TranscribeSegment(BaseModel):
    """US-12 — Segmento temporal devuelto por el proveedor de STT."""

    start: float
    end: float
    text: str


class TranscribeResponse(BaseModel):
    """US-12 / US-18 / US-19 — Resultado de transcribir un audio (Groq Whisper)."""

    text: str
    language: str
    confidence: Optional[float] = None
    segments: List[TranscribeSegment] = []
    # US-19 — Codigo normalizado (solo cuando se pide `normalize=code`).
    normalized_code: Optional[str] = None
