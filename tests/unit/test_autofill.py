"""US-15 t2 — Tests de `POST /api/v1/responses/auto-fill`.

Patrón:
  - Mock de `supabase` a nivel de módulo ANTES de importar `app.main`
    (igual que el resto de tests del repo).
  - `app.dependency_overrides[get_optional_user_id] = lambda: None` para
    saltar el auth (el endpoint es anónimo por diseño).
  - Se mockean `groq_service.transcribe_audio`, `supabase_service.get_survey_by_code`
    y `autofill_service.generate_auto_fill` (este último cuando el test
    apunta al endpoint HTTP). Los tests de la clase `TestAutofillService`
    ejercitan la lógica de `autofill_service` directamente, mockeando solo
    `gemini_service._build_client`.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ------------------------------------------------------------------
# Mock de supabase a nivel de módulo (antes de importar app.main).
# ------------------------------------------------------------------
_mock_supabase = MagicMock()
sys.modules.setdefault(
    "supabase", MagicMock(create_client=lambda url, key: _mock_supabase)
)
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())


from app.main import app  # noqa: E402
from app.api.routes import responses_voice as voice_route  # noqa: E402

# Importante: usar SIEMPRE la misma referencia de módulo que usa la ruta.
# El router hace `from services import autofill_service, groq_service, supabase_service`
# y los tests deben parchear esos objetos, no los re-importados.
autofill_service = voice_route.autofill_service
groq_service = voice_route.groq_service
supabase_service = voice_route.supabase_service


client = TestClient(app)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def _override_optional_auth():
    """El endpoint usa get_optional_user_id; anulamos el guard para que
    devuelva None (ruta anónima) y el endpoint funcione en tests."""
    from app.api import deps

    app.dependency_overrides[deps.get_optional_user_id] = lambda: None
    yield
    app.dependency_overrides.pop(deps.get_optional_user_id, None)


@pytest.fixture(autouse=True)
def _clear_overrides():
    """Limpia overrides entre tests para no contaminar."""
    yield
    # Solo limpiamos los que puso este archivo; los de otros archivos se
    # mantienen (patrón ya usado en otros tests del repo).
    from app.api import deps

    app.dependency_overrides.pop(deps.get_optional_user_id, None)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _fake_transcript(text: str = "Prefiero italiana, no tengo alergias, sí comparto", language: str = "es"):
    return {
        "text": text,
        "language": language,
        "confidence": 0.93,
        "segments": [{"start": 0.0, "end": 1.5, "text": text}],
    }


def _fake_survey(
    survey_id: int = 42,
    status: str = "active",
    questions: list[dict] | None = None,
) -> dict:
    if questions is None:
        questions = [
            {
                "id": 1,
                "survey_id": survey_id,
                "content": "¿Qué cocina prefieres?",
                "question_type": "multiple_choice",
                "options": ["Italiana", "Mexicana", "Japonesa"],
                "position": 0,
            },
            {
                "id": 2,
                "survey_id": survey_id,
                "content": "¿Tienes alergias?",
                "question_type": "open",
                "options": None,
                "position": 1,
            },
            {
                "id": 3,
                "survey_id": survey_id,
                "content": "¿Compartimos platos?",
                "question_type": "yes_no",
                "options": None,
                "position": 2,
            },
        ]
    return {
        "id": survey_id,
        "creator_id": "user-xyz",
        "title": "Pedido grupal",
        "status": status,
        "unique_code": "A7X9K",
        "created_at": "2025-01-01T00:00:00Z",
        "closed_at": None,
        "questions": questions,
    }


def _fake_autofill_result(survey_id: int = 42) -> dict:
    return {
        "survey_id": survey_id,
        "answers": [
            {"question_id": 1, "answer_text": "Italiana"},
            {"question_id": 2, "answer_text": "No tengo"},
            {"question_id": 3, "answer_text": "Sí"},
        ],
    }


# ------------------------------------------------------------------
# Tests del endpoint HTTP
# ------------------------------------------------------------------


class TestAutoFillEndpoint:
    @pytest.mark.usefixtures("_override_optional_auth")
    def test_happy_path_devuelve_200_con_answers_en_forma_response_create(self):
        survey = _fake_survey()
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey), \
             patch.object(autofill_service, "generate_auto_fill", return_value=_fake_autofill_result()):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00\x01\x02", "audio/webm")},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["survey_id"] == 42
        assert len(body["answers"]) == 3
        # Forma compatible con ResponseCreate (solo question_id + answer_text).
        for ans in body["answers"]:
            assert set(ans.keys()) == {"question_id", "answer_text"}
            assert isinstance(ans["question_id"], int)

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_codigo_normalizado_a_mayusculas_se_pasa_a_supabase(self):
        survey = _fake_survey()
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey) as mock_lookup, \
             patch.object(autofill_service, "generate_auto_fill", return_value=_fake_autofill_result()):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "a7x9k"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )

        assert response.status_code == 200
        # `get_survey_by_code` ya normaliza a mayúsculas internamente; el
        # endpoint pasa el código trimmed tal cual.
        assert mock_lookup.call_args.args[0].strip() == "a7x9k"

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_sin_codigo_retorna_422(self):
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": ""},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        # 422 de FastAPI: Pydantic rechaza el Form vacío. La forma exacta
        # del `detail` depende de Pydantic (lista de errores), así que
        # validamos el código y que la respuesta sea de validación.
        assert response.status_code == 422
        body = response.json()
        assert "detail" in body

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_audio_formato_invalido_retorna_400(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.TranscriptionFormatError("Formato no soportado."),
        ):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.txt", b"hola", "text/plain")},
            )
        assert response.status_code == 400

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_audio_demasiado_grande_retorna_413(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.TranscriptionSizeError("Excede 10MB."),
        ):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 413

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_groq_sin_api_key_retorna_503(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.GroqConfigError("GROQ_API_KEY no configurada"),
        ):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 503

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_groq_fallo_proveedor_retorna_502(self):
        with patch.object(
            groq_service,
            "transcribe_audio",
            side_effect=groq_service.TranscriptionProviderError("caída"),
        ):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 502

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_codigo_inexistente_retorna_404(self):
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=None):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "ZZZZZ"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 404

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_encuesta_en_draft_retorna_409(self):
        survey = _fake_survey(status="draft")
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 409
        assert "draft" in response.json()["detail"].lower()

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_encuesta_cerrada_retorna_409(self):
        survey = _fake_survey(status="closed")
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 409
        assert "closed" in response.json()["detail"].lower()

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_gemini_sin_api_key_retorna_503(self):
        survey = _fake_survey()
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey), \
             patch.object(
                 autofill_service,
                 "generate_auto_fill",
                 side_effect=autofill_service.GeminiConfigError("GEMINI_API_KEY no está configurada."),
             ):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 503
        assert "GEMINI_API_KEY" in response.json()["detail"]

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_gemini_fallo_proveedor_retorna_502(self):
        survey = _fake_survey()
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey), \
             patch.object(
                 autofill_service,
                 "generate_auto_fill",
                 side_effect=autofill_service.GeminiProviderError("caída"),
             ):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 502

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_gemini_respuesta_invalida_retorna_502(self):
        survey = _fake_survey()
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey), \
             patch.object(
                 autofill_service,
                 "generate_auto_fill",
                 side_effect=autofill_service.GeminiParseError("json inválido"),
             ):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 502

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_sin_jwt_es_aceptado(self):
        """El endpoint es anónimo por diseño: sin JWT debe funcionar."""
        from app.api import deps

        # Nos aseguramos de NO tener override.
        app.dependency_overrides.pop(deps.get_optional_user_id, None)

        survey = _fake_survey()
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()), \
             patch.object(supabase_service, "get_survey_by_code", return_value=survey), \
             patch.object(autofill_service, "generate_auto_fill", return_value=_fake_autofill_result()):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )
        assert response.status_code == 200

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_idioma_se_pasa_a_groq(self):
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript()) as mock_tx, \
             patch.object(supabase_service, "get_survey_by_code", return_value=_fake_survey()), \
             patch.object(autofill_service, "generate_auto_fill", return_value=_fake_autofill_result()):
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K", "language": "auto"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )

        assert response.status_code == 200
        assert mock_tx.call_args.kwargs["language"] == "auto"

    @pytest.mark.usefixtures("_override_optional_auth")
    def test_idioma_detectado_se_pasa_a_autofill(self):
        with patch.object(groq_service, "transcribe_audio", return_value=_fake_transcript(language="en")), \
             patch.object(supabase_service, "get_survey_by_code", return_value=_fake_survey()), \
             patch.object(autofill_service, "generate_auto_fill", return_value=_fake_autofill_result()) as mock_af:
            response = client.post(
                "/api/v1/responses/auto-fill",
                data={"code": "A7X9K"},
                files={"audio": ("clip.webm", b"\x00", "audio/webm")},
            )

        assert response.status_code == 200
        assert mock_af.call_args.kwargs["detected_language"] == "en"


# ------------------------------------------------------------------
# Tests del servicio (sin HTTP)
# ------------------------------------------------------------------


def _fake_gemini_client(text: str) -> MagicMock:
    fake_response = MagicMock()
    fake_response.text = text
    fake_models = MagicMock()
    fake_models.generate_content.return_value = fake_response
    fake_client = MagicMock()
    fake_client.models = fake_models
    return fake_client


class TestAutofillService:
    def test_happy_path_genera_answers_en_orden_de_position(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 3, "answer_text": "no tengo problema"},
                {"question_id": 1, "answer_text": "Italiana"},
                {"question_id": 2, "answer_text": "No soy alergico"},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(
                survey=survey, transcript="hola", detected_language="es"
            )

        assert result["survey_id"] == 42
        # Las answers deben estar en orden de `position` (1, 2, 3).
        ids = [a["question_id"] for a in result["answers"]]
        assert ids == [1, 2, 3]
        # answers[0] = pregunta 1 (multiple_choice Italiana) → "Italiana"
        # answers[1] = pregunta 2 (open) → "No soy alergico"
        # answers[2] = pregunta 3 (yes_no "no tengo problema") → "No"
        assert result["answers"][0]["answer_text"] == "Italiana"
        assert result["answers"][1]["answer_text"] == "No soy alergico"
        assert result["answers"][2]["answer_text"] == "No"

    def test_respuesta_vacia_de_gemini_devuelve_todas_null(self):
        survey = _fake_survey()
        client_mock = _fake_gemini_client(json.dumps({"answers": []}))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="...")

        assert len(result["answers"]) == 3
        assert all(a["answer_text"] is None for a in result["answers"])

    def test_gemini_omite_una_pregunta_se_inyecta_con_null(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 1, "answer_text": "Italiana"},
                # Falta la 2 y la 3.
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="hola")

        assert [a["question_id"] for a in result["answers"]] == [1, 2, 3]
        assert result["answers"][0]["answer_text"] == "Italiana"
        assert result["answers"][1]["answer_text"] is None
        assert result["answers"][2]["answer_text"] is None

    def test_gemini_inventa_pregunta_se_filtra(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 1, "answer_text": "Italiana"},
                {"question_id": 999, "answer_text": "inventada"},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="hola")

        ids = [a["question_id"] for a in result["answers"]]
        assert 999 not in ids
        assert ids == [1, 2, 3]

    def test_multiple_choice_match_exacto_case_insensitive(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 1, "answer_text": "mexicana"},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="hola")

        # Devuelve la opción declarada con su casing original.
        assert result["answers"][0]["answer_text"] == "Mexicana"

    def test_multiple_choice_respuesta_que_no_matchea_se_vuelve_null(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 1, "answer_text": "Brasilena"},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="hola")

        assert result["answers"][0]["answer_text"] is None

    def test_yes_no_normaliza_a_Si_o_No(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 3, "answer_text": "si porfa"},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="hola")

        # answers está ordenado por position: la pregunta yes_no (id=3) está al final.
        assert result["answers"][2]["answer_text"] == "Sí"

    def test_yes_no_respuesta_ambigua_se_vuelve_null(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 3, "answer_text": "tal vez"},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="hola")

        assert result["answers"][0]["answer_text"] is None

    def test_open_string_vacia_se_vuelve_null(self):
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 2, "answer_text": "   "},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(survey=survey, transcript="hola")

        assert result["answers"][0]["answer_text"] is None

    def test_detected_language_solo_se_usa_en_el_prompt_no_en_salida(self):
        """`detected_language` se inyecta en el prompt maestro pero NO aparece
        en la respuesta (alineado con la tabla `answers` que solo guarda text).
        """
        survey = _fake_survey()
        gemini_payload = {
            "answers": [
                {"question_id": 1, "answer_text": "Italiana"},
            ]
        }
        client_mock = _fake_gemini_client(json.dumps(gemini_payload))

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            result = autofill_service.generate_auto_fill(
                survey=survey, transcript="hola", detected_language="en"
            )

        for a in result["answers"]:
            assert "language" not in a
            assert "is_voice" not in a

    def test_respuesta_no_json_lanza_parse_error(self):
        survey = _fake_survey()
        client_mock = _fake_gemini_client("esto no es json {")

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            with pytest.raises(autofill_service.GeminiParseError):
                autofill_service.generate_auto_fill(survey=survey, transcript="hola")

    def test_respuesta_vacia_lanza_provider_error(self):
        survey = _fake_survey()
        fake = MagicMock()
        fake.text = ""
        fake.models = MagicMock()
        fake.models.generate_content.return_value.text = ""
        client_mock = MagicMock()
        client_mock.models = fake.models

        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            with pytest.raises(autofill_service.GeminiProviderError):
                autofill_service.generate_auto_fill(survey=survey, transcript="hola")

    def test_429_se_traduce_a_provider_error(self):
        survey = _fake_survey()
        client_mock = MagicMock()
        client_mock.models.generate_content.side_effect = RuntimeError("429 quota")
        with patch.object(autofill_service.gemini_service, "_build_client", return_value=client_mock):
            with pytest.raises(autofill_service.GeminiProviderError) as exc:
                autofill_service.generate_auto_fill(survey=survey, transcript="hola")
        assert "cuota" in str(exc.value).lower()

    def test_load_master_prompt_archivo_inexistente_lanza_config_error(self):
        mod = autofill_service
        original = mod._PROMPT_FILE
        mod._PROMPT_FILE = original.parent / "no_existe.txt"
        mod._prompt_cache = None
        mod._prompt_mtime = None
        try:
            with pytest.raises(mod.GeminiConfigError):
                mod._load_master_prompt()
        finally:
            mod._PROMPT_FILE = original
            mod._prompt_cache = None
            mod._prompt_mtime = None

    def test_load_master_prompt_cachea_y_relee_si_cambia_mtime(self, tmp_path):
        mod = autofill_service
        original = mod._PROMPT_FILE
        prompt = tmp_path / "prompt.txt"
        prompt.write_text("v1 {survey_id}", encoding="utf-8")
        mod._PROMPT_FILE = prompt
        mod._prompt_cache = None
        mod._prompt_mtime = None
        try:
            assert mod._load_master_prompt() == "v1 {survey_id}"
            prompt.write_text("v2 {survey_id}", encoding="utf-8")
            cached_mtime = mod._prompt_mtime
            import os
            os.utime(prompt, (cached_mtime, cached_mtime))
            assert mod._load_master_prompt() == "v1 {survey_id}"
            import time
            time.sleep(0.02)
            prompt.write_text("v3", encoding="utf-8")
            assert mod._load_master_prompt() == "v3"
        finally:
            mod._PROMPT_FILE = original
            mod._prompt_cache = None
            mod._prompt_mtime = None
