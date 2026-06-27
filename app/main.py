import sys
import os
from pathlib import Path

# Asegurar que el directorio app/ este en el path de Python
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import (
    surveys,
    questions,
    responses,
    transcribe,
    surveys_generate,
    responses_voice,
)


app = FastAPI(
    title="FastForms API",
    description="Backend para la creacion y distribucion de encuestas.",
    version="0.3.0",
)

# ---------------------------------------------------------------
# CORS — ajusta los origenes cuando tengas el dominio del front
# ---------------------------------------------------------------
frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_url.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------
# Routers
# Importante: los routers con rutas estaticas (`surveys_generate` →
# `/surveys/generate`, `responses_voice` → `/responses/auto-fill`) se
# incluyen ANTES de los routers con path params (`surveys/{survey_id}`)
# para que FastAPI no las confunda con ids dinamicos.
# ---------------------------------------------------------------
app.include_router(surveys_generate.router, prefix="/api/v1")
app.include_router(surveys.router, prefix="/api/v1")
app.include_router(questions.router, prefix="/api/v1")
app.include_router(responses_voice.router, prefix="/api/v1")
app.include_router(responses.router, prefix="/api/v1")
app.include_router(transcribe.router, prefix="/api/v1")


# ---------------------------------------------------------------
# Health check
# ---------------------------------------------------------------
@app.get("/", tags=["Health"])
def root():
    return {"status": "ok", "app": "FastForms API"}
