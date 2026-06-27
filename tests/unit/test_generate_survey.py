"""US-13 — Tests del endpoint POST /api/v1/surveys/generate."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ------------------------------------------------------------------
# Mocks de importacion: se ejecutan ANTES de importar `app.main`.
# Mismo patron que el resto de tests del repo.
# ------------------------------------------------------------------
_mock_supabase = MagicMock()
sys.modules.setdefault(
    "supabase", MagicMock(create_client=lambda url, key: _mock_supabase)
)

# Evita que el modulo del SDK se cargue de verdad durante el import.
# Los tests parchearan `_build_client` o `_call_gemini` segun convenga.
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.genai", MagicMock())


from app.main import app
from app.api.routes import surveys_generate as generate_route

# Importante: usar SIEMPRE la misma referencia de módulo que usa la ruta
# (`surveys_generate.gemini_service`), porque la ruta hace
# `from services import gemini_service` y si en el test importamos via
# `app.services.gemini_service` Python crea DOS module objects distintos y
# las clases de excepción no matchean en el `except` de la ruta.
gemini_service_mod = generate_route.gemini_service

client = TestClient(app)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _auth_headers():
    return {"Authorization": "Bearer test-token"}


def _fake_response_json(title: str, questions: list[dict]) -> str:
    import json

    return json.dumps({"title": title, "questions": questions})


def _patch_user():
    return patch(
        "app.api.deps.get_current_user_id",
        return_value="user-123",
    )


@pytest.fixture
def _override_auth():
    from app.api import deps

    app.dependency_overrides[deps.get_current_user_id] = lambda: "user-123"
    yield
    app.dependency_overrides.pop(deps.get_current_user_id, None)


def _fake_client(text: str) -> MagicMock:
    """Crea un mock de `genai.Client` cuyo `models.generate_content().text` devuelve `text`."""
    fake_response = MagicMock()
    fake_response.text = text
    fake_models = MagicMock()
    fake_models.generate_content.return_value = fake_response
    fake_client_obj = MagicMock()
    fake_client_obj.models = fake_models
    return fake_client_obj


# ------------------------------------------------------------------
# Tests del endpoint
# ------------------------------------------------------------------


@pytest.mark.usefixtures("_override_auth")
class TestGenerateSurveyEndpoint:
    def test_happy_path_devuelve_200_con_titulo_y_preguntas(self):
        payload = {
            "title": "Pedido grupal de comida",
            "questions": [
                {
                    "content": "¿Qué cocina prefieres?",
                    "question_type": "multiple_choice",
                    "options": ["Italiana", "Mexicana"],
                    "position": 0,
                },
                {
                    "content": "¿Alergias?",
                    "question_type": "open",
                    "options": None,
                    "position": 1,
                },
                {
                    "content": "¿Compartimos?",
                    "question_type": "yes_no",
                    "options": None,
                    "position": 2,
                },
            ],
        }
        fake_client = _fake_client(_fake_response_json(payload["title"], payload["questions"]))

        with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
            response = client.post(
                "/api/v1/surveys/generate",
                json={
                    "prompt": "ordenar comida grupal",
                    "num_questions": 3,
                    "language": "es",
                },
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Pedido grupal de comida"
        assert len(body["questions"]) == 3
        assert body["questions"][0]["question_type"] == "multiple_choice"
        assert body["questions"][1]["question_type"] == "open"
        assert body["questions"][2]["question_type"] == "yes_no"
        # Validamos que se llamo a Gemini con el modelo configurado.
        call_kwargs = fake_client.models.generate_content.call_args.kwargs
        assert call_kwargs["model"] == gemini_service_mod.settings.GEMINI_MODEL

    def test_prompt_vacio_retorna_400(self):
        response = client.post(
            "/api/v1/surveys/generate",
            json={"prompt": "", "num_questions": 3},
            headers=_auth_headers(),
        )
        assert response.status_code == 422  # Pydantic

    def test_prompt_solo_espacios_retorna_400(self):
        response = client.post(
            "/api/v1/surveys/generate",
            json={"prompt": "   ", "num_questions": 3},
            headers=_auth_headers(),
        )
        assert response.status_code == 422

    def test_num_questions_cero_retorna_400(self):
        response = client.post(
            "/api/v1/surveys/generate",
            json={"prompt": "ordenar comida", "num_questions": 0},
            headers=_auth_headers(),
        )
        assert response.status_code == 422

    def test_num_questions_mayor_a_12_retorna_400(self):
        response = client.post(
            "/api/v1/surveys/generate",
            json={"prompt": "ordenar comida", "num_questions": 13},
            headers=_auth_headers(),
        )
        assert response.status_code == 422

    def test_sin_api_key_retorna_503(self):
        with patch.object(
            gemini_service_mod,
            "_build_client",
            side_effect=gemini_service_mod.GeminiConfigError("GEMINI_API_KEY no esta configurada."),
        ):
            response = client.post(
                "/api/v1/surveys/generate",
                json={"prompt": "ordenar comida", "num_questions": 3},
                headers=_auth_headers(),
            )
        assert response.status_code == 503
        assert "GEMINI_API_KEY" in response.json()["detail"]

    def test_fallo_del_proveedor_retorna_502(self):
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = RuntimeError("red caida")
        with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
            response = client.post(
                "/api/v1/surveys/generate",
                json={"prompt": "ordenar comida", "num_questions": 3},
                headers=_auth_headers(),
            )
        assert response.status_code == 502

    def test_quota_exhausted_retorna_502_con_mensaje_claro(self):
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED quota")
        with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
            response = client.post(
                "/api/v1/surveys/generate",
                json={"prompt": "ordenar comida", "num_questions": 3},
                headers=_auth_headers(),
            )
        assert response.status_code == 502
        assert "cuota" in response.json()["detail"].lower() or "429" in response.json()["detail"]

    def test_respuesta_no_json_retorna_502(self):
        fake_client = _fake_client("esto no es json {")
        with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
            response = client.post(
                "/api/v1/surveys/generate",
                json={"prompt": "ordenar comida", "num_questions": 3},
                headers=_auth_headers(),
            )
        assert response.status_code == 502

    def test_respuesta_json_invalida_retorna_502(self):
        # multiple_choice sin options -> falla el validador de Pydantic.
        bad = {
            "title": "X",
            "questions": [
                {
                    "content": "pregunta invalida",
                    "question_type": "multiple_choice",
                    "options": None,
                    "position": 0,
                }
            ],
        }
        fake_client = _fake_client(_fake_response_json(bad["title"], bad["questions"]))
        with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
            response = client.post(
                "/api/v1/surveys/generate",
                json={"prompt": "ordenar comida", "num_questions": 1},
                headers=_auth_headers(),
            )
        assert response.status_code == 502

    def test_respuesta_vacia_retorna_502(self):
        fake_client = _fake_client("   ")
        with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
            response = client.post(
                "/api/v1/surveys/generate",
                json={"prompt": "ordenar comida", "num_questions": 3},
                headers=_auth_headers(),
            )
        assert response.status_code == 502


class TestAuth:
    def test_sin_jwt_retorna_401(self):
        # Para que FastAPI rechace con 401, NO debe haber override. Pero el
        # repo tiene una cadena de dependencias (test_csv_export setea
        # `app.dependency_overrides` a nivel de módulo), asi que guardamos
        # el estado y lo restauramos al final para no romper tests
        # posteriores que dependan de ese override.
        saved = dict(app.dependency_overrides)
        app.dependency_overrides.clear()
        try:
            response = client.post(
                "/api/v1/surveys/generate",
                json={"prompt": "ordenar comida", "num_questions": 3},
            )
            assert response.status_code == 401
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved)

    def test_con_jwt_pasa_la_autenticacion(self):
        saved = dict(app.dependency_overrides)
        from app.api import deps
        app.dependency_overrides[deps.get_current_user_id] = lambda: "user-123"
        try:
            fake_client = _fake_client(
                _fake_response_json(
                    "T",
                    [
                        {"content": "q1", "question_type": "yes_no", "options": None, "position": 0}
                    ],
                )
            )
            with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
                response = client.post(
                    "/api/v1/surveys/generate",
                    json={"prompt": "ordenar comida", "num_questions": 1},
                    headers=_auth_headers(),
                )
            assert response.status_code == 200
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(saved)


# ------------------------------------------------------------------
# Tests del servicio (sin HTTP)
# ------------------------------------------------------------------


class TestGeminiService:
    def test_render_prompt_sustituye_placeholders(self):
        master = "lang={language} N={N} user={user_prompt}"
        out = gemini_service_mod._render_prompt(master, 5, "es", "  hola  ")
        assert out == "lang=es N=5 user=hola"

    def test_parse_and_validate_ok(self):
        data = {
            "title": "T",
            "questions": [
                {"content": "p1", "question_type": "yes_no", "options": None, "position": 0}
            ],
        }
        result = gemini_service_mod._parse_and_validate(_fake_response_json("T", data["questions"]))
        assert result["title"] == "T"
        assert result["questions"][0]["question_type"] == "yes_no"

    def test_parse_and_validate_json_invalido_lanza_parse_error(self):
        with pytest.raises(gemini_service_mod.GeminiParseError):
            gemini_service_mod._parse_and_validate("no es json")

    def test_parse_and_validate_schema_invalido_lanza_parse_error(self):
        bad = _fake_response_json("T", [{"content": "", "question_type": "yes_no", "options": None, "position": 0}])
        with pytest.raises(gemini_service_mod.GeminiParseError):
            gemini_service_mod._parse_and_validate(bad)

    def test_parse_and_validate_tipo_desconocido_lanza_parse_error(self):
        bad = _fake_response_json(
            "T",
            [{"content": "p", "question_type": "rare", "options": None, "position": 0}],
        )
        with pytest.raises(gemini_service_mod.GeminiParseError):
            gemini_service_mod._parse_and_validate(bad)

    def test_parse_and_validate_respuesta_no_objeto_lanza_parse_error(self):
        with pytest.raises(gemini_service_mod.GeminiParseError):
            gemini_service_mod._parse_and_validate("[]")

    def test_build_client_sin_api_key_lanza_config_error(self):
        with patch.object(gemini_service_mod.settings, "GEMINI_API_KEY", ""):
            with pytest.raises(gemini_service_mod.GeminiConfigError):
                gemini_service_mod._build_client()

    def test_load_master_prompt_cachea_y_relee_si_cambia_mtime(self, tmp_path):
        # Forzamos un prompt maestro temporal y reseteamos el cache.
        mod = gemini_service_mod

        original_file = mod._PROMPT_FILE
        prompt = tmp_path / "prompt.txt"
        prompt.write_text("v1 {language} {N} {user_prompt}", encoding="utf-8")
        mod._PROMPT_FILE = prompt
        mod._prompt_cache = None
        mod._prompt_mtime = None

        try:
            assert mod._load_master_prompt() == "v1 {language} {N} {user_prompt}"
            # Llamada repetida: debe usar cache y NO re-leer.
            prompt.write_text("v2 {language} {N} {user_prompt}", encoding="utf-8")
            # Truco: dejamos el mtime igual para forzar uso de cache.
            cached_mtime = mod._prompt_mtime
            import os
            os.utime(prompt, (cached_mtime, cached_mtime))
            assert mod._load_master_prompt() == "v1 {language} {N} {user_prompt}"

            # Si cambia el mtime, re-lee.
            import time
            time.sleep(0.02)
            prompt.write_text("v3", encoding="utf-8")
            assert mod._load_master_prompt() == "v3"
        finally:
            mod._PROMPT_FILE = original_file
            mod._prompt_cache = None
            mod._prompt_mtime = None

    def test_load_master_prompt_archivo_inexistente_lanza_config_error(self):
        mod = gemini_service_mod

        original_file = mod._PROMPT_FILE
        mod._PROMPT_FILE = original_file.parent / "no_existe_prompt.txt"
        mod._prompt_cache = None
        mod._prompt_mtime = None
        try:
            with pytest.raises(gemini_service_mod.GeminiConfigError):
                mod._load_master_prompt()
        finally:
            mod._PROMPT_FILE = original_file
            mod._prompt_cache = None
            mod._prompt_mtime = None

    def test_call_gemini_respuesta_vacia_lanza_provider_error(self):
        fake_client = MagicMock()
        fake_response = MagicMock()
        fake_response.text = ""
        fake_client.models.generate_content.return_value = fake_response
        with pytest.raises(gemini_service_mod.GeminiProviderError):
            gemini_service_mod._call_gemini(fake_client, "cualquier prompt")

    def test_call_gemini_429_se_traduce_a_provider_error(self):
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = RuntimeError("HTTP 429 quota exhausted")
        with pytest.raises(gemini_service_mod.GeminiProviderError) as exc:
            gemini_service_mod._call_gemini(fake_client, "cualquier prompt")
        assert "cuota" in str(exc.value).lower()

    def test_call_gemini_error_generico_se_traduce_a_provider_error(self):
        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = RuntimeError("network unreachable")
        with pytest.raises(gemini_service_mod.GeminiProviderError) as exc:
            gemini_service_mod._call_gemini(fake_client, "cualquier prompt")
        assert "network unreachable" in str(exc.value)

    def test_generate_survey_draft_flujo_completo(self):
        data = {
            "title": "T",
            "questions": [
                {"content": "p1", "question_type": "yes_no", "options": None, "position": 0}
            ],
        }
        fake_client = _fake_client(_fake_response_json("T", data["questions"]))
        with patch.object(gemini_service_mod, "_build_client", return_value=fake_client):
            result = gemini_service_mod.generate_survey_draft("hola", 1, "es")
        assert result["title"] == "T"
        assert result["questions"][0]["position"] == 0
