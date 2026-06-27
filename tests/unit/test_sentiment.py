"""US-16 — Tests de `POST /api/v1/surveys/{id}/questions/{qid}/sentiment-analysis`.

Patrón (idéntico al resto de tests del repo):
  - Mock de `supabase` a nivel de módulo ANTES de importar `app.main`.
  - `app.dependency_overrides[_original_get_user_id] = lambda: _OWNER` usando
    la referencia que el router importa (no `app.api.deps` — son módulos
    distintos en `sys.modules` por el `sys.path` que mete `app/main.py`).
  - Para tests del servicio: mockeamos `gemini_service._build_client` y
    conservamos la referencia al fake client para inspeccionar `call_args`
    DENTRO del `with`.
  - Para tests del endpoint: mockeamos `supabase_service.get_survey`,
    `supabase_service.get_survey_with_questions`,
    `supabase_service.get_open_question_answers` y
    `sentiment_service.analyze_sentiment`.
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
from app.api.routes import surveys as surveys_route  # noqa: E402
from app.services import sentiment_service  # noqa: E402

# Importante: usar SIEMPRE la misma referencia de módulo que usa la ruta.
# El router hace `from services import supabase_service, sentiment_service`
# y los tests deben parchear esos objetos, no los re-importados.
supabase_service = surveys_route.supabase_service
sentiment_service_route = surveys_route.sentiment_service


# Override de autenticación usando la función que realmente usa el router
# (ver `tests/unit/test_csv_export.py:25`).
_OWNER = "832071cb-5f6a-4d2d-8c0c-901cd13e78ad"
_original_get_user_id = surveys_route.get_current_user_id
app.dependency_overrides[_original_get_user_id] = lambda: _OWNER

client = TestClient(app)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _fake_question(
    question_id: int = 1,
    survey_id: int = 10,
    qtype: str = "open",
    content: str = "¿Qué te pareció el evento?",
) -> dict:
    return {
        "id": question_id,
        "survey_id": survey_id,
        "content": content,
        "question_type": qtype,
        "options": None,
        "position": 1,
    }


def _fake_owned_survey(
    survey_id: int = 10,
    status: str = "closed",
    creator: str = _OWNER,
) -> dict:
    return {
        "id": survey_id,
        "creator_id": creator,
        "title": "Encuesta demo",
        "status": status,
        "unique_code": "ABCDE",
    }


def _fake_gemini_response() -> dict:
    return {
        "overall_sentiment": "positivo",
        "score": 0.75,
        "distribution": {"positive": 3, "negative": 0, "neutral": 0},
        "summary": "Las respuestas son mayoritariamente positivas.",
        "key_themes": ["buena organización", "buen ambiente"],
    }


class _FakeGenaiResponse:
    def __init__(self, text: str):
        self.text = text


def _patch_gemini_response(payload):
    """Parchea `gemini_service._build_client` y devuelve (patcher, fake_client).

    El test puede inspeccionar `fake_client.models.generate_content.call_args`
    DENTRO del `with`.
    """
    if isinstance(payload, dict):
        text = json.dumps(payload)
    else:
        text = payload

    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _FakeGenaiResponse(text)

    patcher = patch.object(
        sentiment_service.gemini_service,
        "_build_client",
        return_value=fake_client,
    )
    return patcher, fake_client


# ==================================================================
# Tests del SERVICIO
# ==================================================================


class TestSentimentService:

    def test_prompt_incluye_pregunta_y_respuestas(self):
        question = _fake_question(
            content="¿Qué mejorarías del servicio?",
        )
        answers = [
            "La atención fue excelente",
            "Tardaron mucho en atenderme",
            "Sin comentarios",
        ]

        patcher, fake_client = _patch_gemini_response(_fake_gemini_response())
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)

        # Verificamos el prompt enviado a Gemini.
        call = fake_client.models.generate_content.call_args
        prompt = call.kwargs.get("contents") or call.args[0]
        assert "¿Qué mejorarías del servicio?" in prompt
        for ans in answers:
            assert ans in prompt
        assert "3" in prompt  # n_answers

        # Y que el shape de salida es el esperado.
        assert result["overall_sentiment"] == "positivo"
        assert result["score"] == 0.75
        assert result["distribution"] == {"positive": 3, "negative": 0, "neutral": 0}
        assert "positivas" in result["summary"]
        assert "buena organización" in result["key_themes"]

    def test_score_se_clamp_a_rango_valido(self):
        question = _fake_question()
        answers = ["ok", "ok"]

        bad = _fake_gemini_response()
        bad["score"] = 5.0
        patcher, _ = _patch_gemini_response(bad)
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)
        assert result["score"] == 1.0

        bad["score"] = -99.0
        patcher, _ = _patch_gemini_response(bad)
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)
        assert result["score"] == -1.0

    def test_overall_sentiment_normalizado_a_cuatro_valores(self):
        question = _fake_question()
        answers = ["x", "y"]

        for weird in ["POSITIVO!!!", "algo raro", "", 123]:
            payload = _fake_gemini_response()
            payload["overall_sentiment"] = weird
            patcher, _ = _patch_gemini_response(payload)
            with patcher:
                result = sentiment_service.analyze_sentiment(question, answers)
            assert result["overall_sentiment"] in {
                "positivo",
                "negativo",
                "neutral",
                "mixto",
            }
        # Caso "positive" en inglés → alias a "positivo".
        payload = _fake_gemini_response()
        payload["overall_sentiment"] = "positive"
        patcher, _ = _patch_gemini_response(payload)
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)
        assert result["overall_sentiment"] == "positivo"

    def test_key_themes_se_truncan_y_deduplican(self):
        question = _fake_question()
        answers = ["a", "b"]

        payload = _fake_gemini_response()
        payload["key_themes"] = [
            "Atención",
            "atencion",  # duplicado case-insensitive
            "  precio  ",
            "precio",  # duplicado
            "instalaciones",
            "limpieza",
            "horarios",
            "comida",
            "musica",
            "decoración",  # queda fuera del top 5
        ]
        patcher, _ = _patch_gemini_response(payload)
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)
        assert len(result["key_themes"]) <= 5
        # No hay duplicados case-insensitive
        lowered = [t.lower() for t in result["key_themes"]]
        assert len(lowered) == len(set(lowered))
        # El primero se preserva (con su capitalización original).
        assert "Atención" in result["key_themes"]

    def test_distribution_se_cuadra_con_total_answers(self):
        question = _fake_question()
        answers = ["a", "b", "c", "d", "e"]  # 5

        # Gemini miente y dice 3 pos / 1 neg / 0 neu.
        payload = _fake_gemini_response()
        payload["distribution"] = {"positive": 3, "negative": 1, "neutral": 0}
        patcher, _ = _patch_gemini_response(payload)
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)
        dist = result["distribution"]
        assert (
            dist["positive"] + dist["negative"] + dist["neutral"]
        ) == 5

    def test_distribution_negativos_se_clampan_a_cero(self):
        question = _fake_question()
        answers = ["a", "b"]

        payload = _fake_gemini_response()
        payload["distribution"] = {"positive": -1, "negative": -1, "neutral": 4}
        patcher, _ = _patch_gemini_response(payload)
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)
        dist = result["distribution"]
        assert dist["positive"] == 0
        assert dist["negative"] == 0
        assert dist["neutral"] == 2

    def test_resumen_vacio_se_reemplaza(self):
        question = _fake_question()
        answers = ["a", "b"]

        payload = _fake_gemini_response()
        payload["summary"] = "   "
        patcher, _ = _patch_gemini_response(payload)
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)
        assert result["summary"] != ""
        assert "resumen" in result["summary"].lower()

    def test_propagates_gemini_provider_error(self):
        from services.gemini_service import GeminiProviderError

        question = _fake_question()
        answers = ["a"]

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = GeminiProviderError(
            "boom"
        )
        with patch.object(
            sentiment_service.gemini_service,
            "_build_client",
            return_value=fake_client,
        ):
            with pytest.raises(GeminiProviderError):
                sentiment_service.analyze_sentiment(question, answers)

    def test_propagates_gemini_parse_error(self):
        from services.gemini_service import GeminiParseError

        question = _fake_question()
        answers = ["a"]

        patcher, _ = _patch_gemini_response("not-a-json {")
        with patcher:
            with pytest.raises(GeminiParseError):
                sentiment_service.analyze_sentiment(question, answers)

    def test_propagates_gemini_config_error(self):
        from services.gemini_service import GeminiConfigError

        question = _fake_question()
        answers = ["a"]

        with patch.object(
            sentiment_service.gemini_service,
            "_build_client",
            side_effect=GeminiConfigError("sin key"),
        ):
            with pytest.raises(GeminiConfigError):
                sentiment_service.analyze_sentiment(question, answers)

    def test_answers_vacios_o_none_se_filtran(self):
        question = _fake_question()
        # Aunque el router garantiza n>=1, el servicio debe ser defensivo.
        answers = ["", None, "  ", "vale"]

        patcher, fake_client = _patch_gemini_response(_fake_gemini_response())
        with patcher:
            result = sentiment_service.analyze_sentiment(question, answers)

        # El n_answers del prompt debe ser 1 (solo "vale").
        call = fake_client.models.generate_content.call_args
        prompt = call.kwargs.get("contents") or call.args[0]
        # Buscamos la línea exacta "{n_answers}" en la sección de respuestas
        # (un solo dígito "1" podría aparecer en otras partes; validamos que
        # "1" está al menos una vez y que "vale" también).
        assert "1" in prompt
        assert "vale" in prompt
        # Y la suma de distribution debe ser 1.
        assert sum(result["distribution"].values()) == 1

    def test_sin_answers_devuelve_distribution_cero(self):
        # El router filtra este caso antes de llamar al servicio, pero el
        # servicio debe seguir siendo usable si alguien lo invoca directo
        # sin respuestas: devolvemos distribution 0/0/0.
        question = _fake_question()
        patcher, _ = _patch_gemini_response(_fake_gemini_response())
        with patcher:
            result = sentiment_service.analyze_sentiment(question, [])
        assert result["distribution"] == {"positive": 0, "negative": 0, "neutral": 0}


# ==================================================================
# Tests del ENDPOINT
# ==================================================================


class TestEndpointSentiment:

    def _patch_service_ok(self):
        return patch.object(
            sentiment_service_route,
            "analyze_sentiment",
            return_value={
                "overall_sentiment": "positivo",
                "score": 0.6,
                "distribution": {"positive": 3, "negative": 0, "neutral": 0},
                "summary": "Buen rollo general.",
                "key_themes": ["amabilidad"],
            },
        )

    def test_200_con_payload_valido(self):
        survey = _fake_owned_survey(status="closed")
        question = _fake_question(qtype="open")

        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ), patch.object(
            surveys_route.supabase_service,
            "get_open_question_answers",
            return_value=["a", "b", "c"],
        ), self._patch_service_ok():
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["survey_id"] == 10
        assert data["question_id"] == 1
        assert data["question_content"] == question["content"]
        assert data["total_answers"] == 3
        assert data["overall_sentiment"] == "positivo"
        assert data["score"] == 0.6
        assert data["distribution"]["positive"] == 3
        assert data["key_themes"] == ["amabilidad"]

    def test_404_encuesta_inexistente(self):
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=None
        ):
            response = client.post(
                "/api/v1/surveys/999/questions/1/sentiment-analysis"
            )
        assert response.status_code == 404

    def test_403_encuesta_de_otro_creador(self):
        survey = _fake_owned_survey(creator="otro-usuario")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 403

    def test_409_encuesta_en_draft(self):
        survey = _fake_owned_survey(status="draft")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 409
        assert "cerradas" in response.json()["detail"].lower()

    def test_409_encuesta_en_active(self):
        survey = _fake_owned_survey(status="active")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 409

    def test_400_pregunta_no_es_open(self):
        survey = _fake_owned_survey(status="closed")
        question = _fake_question(qtype="multiple_choice")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 400
        assert "abiertas" in response.json()["detail"].lower()

    def test_400_pregunta_yes_no_tambien_rechazada(self):
        survey = _fake_owned_survey(status="closed")
        question = _fake_question(qtype="yes_no")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 400

    def test_404_pregunta_no_pertenece_a_encuesta(self):
        survey = _fake_owned_survey(status="closed")
        other_question = _fake_question(question_id=2, qtype="open")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [other_question]},
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 404

    def test_422_sin_respuestas(self):
        survey = _fake_owned_survey(status="closed")
        question = _fake_question(qtype="open")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ), patch.object(
            surveys_route.supabase_service,
            "get_open_question_answers",
            return_value=[],
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 422
        assert "respuestas" in response.json()["detail"].lower()

    def test_503_gemini_config_error(self):
        from services.gemini_service import GeminiConfigError

        survey = _fake_owned_survey(status="closed")
        question = _fake_question(qtype="open")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ), patch.object(
            surveys_route.supabase_service,
            "get_open_question_answers",
            return_value=["a", "b"],
        ), patch.object(
            sentiment_service_route,
            "analyze_sentiment",
            side_effect=GeminiConfigError("sin GEMINI_API_KEY"),
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 503
        assert "GEMINI" in response.json()["detail"]

    def test_502_gemini_provider_error(self):
        from services.gemini_service import GeminiProviderError

        survey = _fake_owned_survey(status="closed")
        question = _fake_question(qtype="open")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ), patch.object(
            surveys_route.supabase_service,
            "get_open_question_answers",
            return_value=["a", "b"],
        ), patch.object(
            sentiment_service_route,
            "analyze_sentiment",
            side_effect=GeminiProviderError("429"),
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 502

    def test_502_gemini_parse_error(self):
        from services.gemini_service import GeminiParseError

        survey = _fake_owned_survey(status="closed")
        question = _fake_question(qtype="open")
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ), patch.object(
            surveys_route.supabase_service,
            "get_open_question_answers",
            return_value=["a", "b"],
        ), patch.object(
            sentiment_service_route,
            "analyze_sentiment",
            side_effect=GeminiParseError("json inválido"),
        ):
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 502

    def test_pasa_question_content_al_servicio(self):
        """Asegura que el `content` de la pregunta llega al servicio tal cual."""
        survey = _fake_owned_survey(status="closed")
        question = _fake_question(
            qtype="open", content="¿Qué te pareció el evento?"
        )
        with patch.object(
            surveys_route.supabase_service, "get_survey", return_value=survey
        ), patch.object(
            surveys_route.supabase_service,
            "get_survey_with_questions",
            return_value={**survey, "questions": [question]},
        ), patch.object(
            surveys_route.supabase_service,
            "get_open_question_answers",
            return_value=["a"],
        ), patch.object(
            sentiment_service_route, "analyze_sentiment"
        ) as mock_service:
            mock_service.return_value = {
                "overall_sentiment": "neutral",
                "score": 0.0,
                "distribution": {"positive": 0, "negative": 0, "neutral": 1},
                "summary": "x",
                "key_themes": [],
            }
            response = client.post(
                "/api/v1/surveys/10/questions/1/sentiment-analysis"
            )
        assert response.status_code == 200
        # El primer argumento posicional es la pregunta.
        sent_question = mock_service.call_args.args[0]
        assert sent_question["content"] == "¿Qué te pareció el evento?"
        sent_answers = mock_service.call_args.args[1]
        assert sent_answers == ["a"]
