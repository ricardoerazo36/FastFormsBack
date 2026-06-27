import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

class Settings:
    PROJECT_NAME: str = "Fast Forms API"

    def __init__(self):
        self.URL: str = os.getenv("SUPABASE_URL", "")
        self.KEY: str = os.getenv("SUPABASE_KEY", "")
        # US-12 / US-15 — Proveedor de transcripcion: Groq.
        # Necesita GROQ_API_KEY; sin ella, /transcribe devuelve 503.
        # Modelo Whisper servido por Groq (`whisper-large-v3` o
        # `whisper-large-v3-turbo` por defecto).
        self.GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
        self.GROQ_TRANSCRIBE_MODEL: str = os.getenv(
            "GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo"
        )
        # US-13 — Generador de encuestas con Gemini. Si la key no está
        # configurada, el endpoint /surveys/generate devolverá 503; el resto
        # de la API sigue funcionando.
        self.GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
        self.GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

        if not self.URL or not self.KEY:
            raise RuntimeError(
                "❌ ERROR: No se encontraron SUPABASE_URL o SUPABASE_KEY en el .env. "
                "Revisa el archivo .env o las variables de entorno."
            )

        if not self.URL.startswith("https://"):
            raise RuntimeError(
                "❌ ERROR: SUPABASE_URL debe comenzar con 'https://'. "
                f"Valor actual: '{self.URL}'"
            )

settings = Settings()

# Inicialización global del cliente
supabase: Client = create_client(settings.URL, settings.KEY)