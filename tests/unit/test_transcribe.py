"""US-12 / US-15 — Tests del endpoint POST /api/v1/transcribe (Groq)."""

import io
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_mock_supabase = MagicMock()
sys.modules.setdefault(
    "supabase", MagicMock(create_client=lambda url, key: _mock_supabase)
)

from app.main import app
from app.api.routes import transcribe as transcribe_route

groq_service = transcribe_route.groq_service

client = TestClient(app)


def _auth_headers():
    return {"Authorization": "Bearer test-token"}


@pytest.fixture
def _override_auth():
    from app.api import deps

    app.dependency_overrides[deps.get_current_user_id] = lambda: "user-123"
    yield
    app.dependency_overrides.pop(deps.get_current_user_id, None)


def _fake_groq_response(
    text: str = "Hola mundo",
    language: str = "es",
    confidence_words: list[float] | None = None,
    segments: list | None = None,
    **extra,
) -> MagicMock:
    """Construye un mock del objeto `Transcription` que devuelve Groq."""
    if confidence_words is None:
        confidence_words = [0.9, 0.95]
    # Generamos tokens ficticios alineados con la cantidad de confidences
    # para que la cantidad de `words` no dependa del `text` que llega.
    tokens = [f"w{i}" for i in range(len(confidence_words))]
    words = [
        MagicMock(word=t, start=0.0, end=0.5, confidence=c)
        for t, c in zip(tokens, confidence_words)
    ]
    obj = MagicMock()
    obj.text = text
    obj.language = language
    obj.duration = 1.2
    obj.segments = segments or [
        {"start": 0.0, "end": 1.2, "text": text}
    ]
    obj.words = words
    for k, v in extra.items():
        setattr(obj, k, v)
    return obj


@pytest.mark.usefixtures("_override_auth")
class TestTranscribeEndpoint:
    def test_happy_path_devuelve_200(self):
        result = _fake_groq_response()
        with patch.object(
            groq_service,
            "transcribe_audio",
            return_value={
                "text": "Hola mundo",
                "language": "es",
                "confidence": 0.925,
                "segments": [{"start": 0.0, "end": 1.2, "text": "Hola mundo"}],
            },
        ):
            response = client.post(
                "/api/v1/transcribe/",
                files={"audio": ("clip.webm", b"\x00\x01\x02", "audio/webm")},
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["text"] == "Hola mundo"
        assert body["language"] == "es"
        assert body["confidence"] == 0.925
        assert body["segments"][0]["text"] == "Hola mundo"

    def test_formato_invalido_retorna_400(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.TranscriptionFormatError(
                "Formato de audio no soportado."
            ),
        ):
            response = client.post(
                "/api/v1/transcribe/",
                files={"audio": ("clip.txt", b"texto", "text/plain")},
                headers=_auth_headers(),
            )
        assert response.status_code == 400

    def test_audio_muy_grande_retorna_413(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.TranscriptionSizeError(
                "El audio excede el limite."
            ),
        ):
            response = client.post(
                "/api/v1/transcribe/",
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
                headers=_auth_headers(),
            )
        assert response.status_code == 413

    def test_fallo_del_proveedor_retorna_502(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.TranscriptionProviderError("proveedor caido"),
        ):
            response = client.post(
                "/api/v1/transcribe/",
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
                headers=_auth_headers(),
            )
        assert response.status_code == 502

    def test_groq_no_configurado_retorna_503(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.GroqConfigError("GROQ_API_KEY no configurada"),
        ):
            response = client.post(
                "/api/v1/transcribe/",
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
                headers=_auth_headers(),
            )
        assert response.status_code == 503

    def test_normalize_code_devuelve_codigo(self):
        """US-19 — con normalize=code el endpoint devuelve normalized_code."""
        with patch.object(
            groq_service,
            "transcribe_audio",
            return_value={
                "text": "a siete equis nueve ka",
                "language": "es",
                "confidence": 0.8,
                "segments": [],
            },
        ):
            response = client.post(
                "/api/v1/transcribe/",
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
                data={"normalize": "code"},
                headers=_auth_headers(),
            )
        assert response.status_code == 200
        assert response.json()["normalized_code"] == "A7X9K"


class TestSinAutenticacion:
    """US-14 — Los encuestados anonimos tambien pueden transcribir audio."""

    def test_sin_jwt_es_aceptado(self):
        app.dependency_overrides.clear()
        with patch.object(
            groq_service,
            "transcribe_audio",
            return_value={"text": "hola", "language": "es", "confidence": 0.9, "segments": []},
        ):
            response = client.post(
                "/api/v1/transcribe/",
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 200
        assert response.json()["text"] == "hola"


class TestValidacionAudio:
    """Tests directos sobre `validate_audio`."""

    def test_audio_vacio_lanza_format_error(self):
        with pytest.raises(groq_service.TranscriptionFormatError):
            groq_service.validate_audio("clip.webm", "audio/webm", b"")

    def test_audio_grande_lanza_size_error(self):
        big = b"\x00" * (groq_service.MAX_BYTES + 1)
        with pytest.raises(groq_service.TranscriptionSizeError):
            groq_service.validate_audio("clip.webm", "audio/webm", big)

    def test_extension_no_soportada_lanza_format_error(self):
        with pytest.raises(groq_service.TranscriptionFormatError):
            groq_service.validate_audio("clip.txt", "text/plain", b"\x00\x01")

    def test_formato_valido_no_lanza(self):
        groq_service.validate_audio("clip.webm", "audio/webm", b"\x00\x01")
        groq_service.validate_audio("clip.mp3", "audio/mpeg", b"\x00\x01")
        groq_service.validate_audio("clip.wav", "audio/wav", b"\x00\x01")


class TestProveedorGroq:
    """`transcribe_audio` despacha al SDK de Groq con los parametros correctos."""

    def test_language_none_o_vacio_aplica_default_es(self):
        with patch.object(groq_service, "_get_client") as mock_get:
            fake_create = MagicMock(return_value=_fake_groq_response())
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = fake_create
            mock_get.return_value = mock_client

            groq_service.transcribe_audio(
                "clip.webm", "audio/webm", b"\x00\x01", language=None
            )

        kwargs = fake_create.call_args.kwargs
        assert kwargs["language"] == "es"

    def test_language_auto_se_traduce_a_no_enviar_idioma(self):
        """US-18 — `auto` deja que Groq detecte el idioma (no enviamos `language`)."""
        with patch.object(groq_service, "_get_client") as mock_get:
            fake_create = MagicMock(return_value=_fake_groq_response())
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = fake_create
            mock_get.return_value = mock_client

            groq_service.transcribe_audio(
                "clip.webm", "audio/webm", b"\x00\x01", language="auto"
            )

        kwargs = fake_create.call_args.kwargs
        assert "language" not in kwargs

    def test_translate_usa_el_endpoint_de_traducciones(self):
        with patch.object(groq_service, "_get_client") as mock_get:
            fake_create = MagicMock(return_value=_fake_groq_response())
            mock_transcriptions = MagicMock()
            mock_translations = MagicMock(create=fake_create)
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = MagicMock()
            mock_client.audio.translations.create = fake_create
            mock_get.return_value = mock_client

            groq_service.transcribe_audio(
                "clip.webm", "audio/webm", b"\x00\x01",
                language="es", task="translate",
            )

        # En modo `translate` NO se envia `language` (siempre traduce a `en`).
        kwargs = fake_create.call_args.kwargs
        assert "language" not in kwargs

    def test_modelo_default_es_whisper_large_v3_turbo(self):
        with patch.object(
            groq_service.settings, "GROQ_TRANSCRIBE_MODEL", ""
        ), patch.object(groq_service, "_get_client") as mock_get:
            fake_create = MagicMock(return_value=_fake_groq_response())
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = fake_create
            mock_get.return_value = mock_client

            groq_service.transcribe_audio("clip.webm", "audio/webm", b"\x00\x01")

        kwargs = fake_create.call_args.kwargs
        assert kwargs["model"] == "whisper-large-v3-turbo"

    def test_sin_groq_api_key_lanza_groq_config_error(self):
        # Reiniciamos el cliente cacheado para forzar la relectura de settings.
        groq_service._client = None
        with patch.object(
            groq_service.settings, "GROQ_API_KEY", ""
        ), pytest.raises(groq_service.GroqConfigError) as exc_info:
            groq_service._get_client()
        assert "GROQ_API_KEY" in str(exc_info.value)
        groq_service._client = None

    def test_fallo_401_de_groq_se_traduce_a_groq_config_error(self):
        groq_service._client = None
        with patch.object(
            groq_service.settings, "GROQ_API_KEY", "fake"
        ), patch("groq.Groq") as mock_groq_cls:
            mock_groq_cls.return_value.audio.transcriptions.create.side_effect = (
                Exception("401 unauthorized: invalid_api_key")
            )
            with pytest.raises(groq_service.GroqConfigError):
                groq_service.transcribe_audio(
                    "clip.webm", "audio/webm", b"\x00\x01"
                )
        groq_service._client = None

    def test_confidence_es_promedio_de_palabras(self):
        """US-15 — la `confidence` global es el promedio de la confianza por palabra."""
        result = _fake_groq_response(confidence_words=[0.5, 0.7, 0.9])
        with patch.object(groq_service, "_get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = MagicMock(return_value=result)
            mock_get.return_value = mock_client

            output = groq_service.transcribe_audio(
                "clip.webm", "audio/webm", b"\x00\x01"
            )

        assert output["confidence"] == round((0.5 + 0.7 + 0.9) / 3, 4)  # = 0.7
        assert output["text"] == "Hola mundo"

    def test_segmentos_se_normalizan_a_start_end_text(self):
        seg_obj = MagicMock()
        seg_obj.start = 0.0
        seg_obj.end = 1.5
        seg_obj.text = "  primer segmento  "
        result = MagicMock()
        result.text = "primer segmento"
        result.language = "es"
        result.duration = 1.5
        result.segments = [seg_obj]
        result.words = [MagicMock(word="primer", start=0.0, end=0.5, confidence=0.9)]

        with patch.object(groq_service, "_get_client") as mock_get:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = MagicMock(return_value=result)
            mock_get.return_value = mock_client

            output = groq_service.transcribe_audio(
                "clip.webm", "audio/webm", b"\x00\x01"
            )

        assert output["segments"] == [
            {"start": 0.0, "end": 1.5, "text": "primer segmento"}
        ]
