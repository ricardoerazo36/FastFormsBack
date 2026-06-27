import random
import string
from datetime import datetime, timezone
from typing import Optional

from core.config import supabase
from schemas.survey import SurveyCreate

# Opciones implicitas para las preguntas Si/No (no se guardan en la BD).
YES_NO_OPTIONS = ["Sí", "No"]


def _generate_unique_code(length: int = 5) -> str:
    """Genera un codigo alfanumerico en mayusculas (ej: A7X9K)."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


def create_survey(payload: SurveyCreate, creator_id: str) -> dict:
    """
    Inserta la encuesta y sus preguntas en Supabase dentro de una operacion
    atomica manual (insert survey -> insert questions).

    Retorna el registro completo de la encuesta con sus preguntas.
    Lanza una excepcion si alguna operacion falla.
    """

    # 1. Generar codigo unico (reintenta si ya existe)
    unique_code = _get_available_code()

    # 2. Insertar la encuesta
    survey_data = {
        "creator_id": creator_id,
        "title": payload.title,
        "status": "draft",
        "unique_code": unique_code,
    }

    survey_result = supabase.table("surveys").insert(survey_data).execute()

    if not survey_result.data:
        raise RuntimeError("Error al guardar la encuesta en la base de datos.")

    survey = survey_result.data[0]
    survey_id = survey["id"]

    # 3. Insertar las preguntas vinculadas a la encuesta
    questions_data = [
        {
            "survey_id": survey_id,
            "content": q.content,
            "question_type": q.question_type.value,
            "options": q.options,
            "position": q.position,
        }
        for q in payload.questions
    ]

    questions_result = supabase.table("questions").insert(questions_data).execute()

    if not questions_result.data:
        # Rollback manual: eliminar la encuesta recien creada
        supabase.table("surveys").delete().eq("id", survey_id).execute()
        raise RuntimeError("Error al guardar las preguntas. La encuesta fue revertida.")

    survey["questions"] = questions_result.data
    return survey


def _get_available_code(max_attempts: int = 10) -> str:
    """Genera un codigo que no exista todavia en la tabla surveys."""
    for _ in range(max_attempts):
        code = _generate_unique_code()
        existing = (
            supabase.table("surveys").select("id").eq("unique_code", code).execute()
        )
        if not existing.data:
            return code
    raise RuntimeError(
        "No se pudo generar un codigo unico despues de varios intentos."
    )


def get_survey_by_code(code: str) -> Optional[dict]:
    """Retorna la encuesta (con preguntas) a partir del codigo unico."""
    result = (
        supabase.table("surveys")
        .select("*, questions(*)")
        .eq("unique_code", code.upper())
        .execute()
    )
    return result.data[0] if result.data else None


def get_survey(survey_id: int) -> Optional[dict]:
    """Retorna la encuesta (sin preguntas) a partir de su id, o None si no existe."""
    result = supabase.table("surveys").select("*").eq("id", survey_id).execute()
    return result.data[0] if result.data else None


def get_survey_with_questions(survey_id: int) -> Optional[dict]:
    """Retorna la encuesta (incluyendo sus preguntas) por id.

    Lo usamos para reabrir un borrador en el editor del frontend.
    """
    result = (
        supabase.table("surveys")
        .select("*, questions(*)")
        .eq("id", survey_id)
        .execute()
    )
    return result.data[0] if result.data else None


def list_surveys_by_creator(creator_id: str) -> list:
    """US-02 — Retorna todas las encuestas creadas por el usuario indicado."""
    result = (
        supabase.table("surveys")
        .select("*")
        .eq("creator_id", creator_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


def set_survey_status(survey_id: int, new_status: str) -> dict:
    """Cambia el estado de una encuesta y retorna el registro actualizado."""
    result = (
        supabase.table("surveys")
        .update({"status": new_status})
        .eq("id", survey_id)
        .execute()
    )
    if not result.data:
        raise RuntimeError("No se pudo actualizar el estado de la encuesta.")
    return result.data[0]


def close_survey(survey_id: int) -> dict:
    """
    US-09 — Cierra una encuesta (accion irreversible): estado `closed` y
    `closed_at` con la fecha actual. Retorna el registro actualizado.
    """
    result = (
        supabase.table("surveys")
        .update(
            {"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()}
        )
        .eq("id", survey_id)
        .execute()
    )
    if not result.data:
        raise RuntimeError("No se pudo cerrar la encuesta.")
    return result.data[0]


# ---------------------------------------------------------------------------
# Borradores: guardado y edicion con contenido parcial.
# ---------------------------------------------------------------------------

def _replace_questions(survey_id: int, questions) -> list:
    """Elimina y reinserta todas las preguntas asociadas a un borrador.

    Snapshot atomico de lo que el usuario ve al momento de guardar; evita
    arrastrar preguntas obsoletas eliminadas en el editor.
    """
    supabase.table("questions").delete().eq("survey_id", survey_id).execute()
    if not questions:
        return []
    questions_data = [
        {
            "survey_id": survey_id,
            "content": (q.content or "").strip(),
            "question_type": q.question_type.value,
            "options": q.options,
            "position": q.position,
        }
        for q in questions
    ]
    result = supabase.table("questions").insert(questions_data).execute()
    return result.data or []


def create_draft(payload, creator_id: str) -> dict:
    """Crea un borrador (status='draft') con sus preguntas, contenido parcial permitido."""
    unique_code = _get_available_code()
    survey_data = {
        "creator_id": creator_id,
        "title": payload.title or "(sin titulo)",
        "status": "draft",
        "unique_code": unique_code,
    }
    survey_result = supabase.table("surveys").insert(survey_data).execute()
    if not survey_result.data:
        raise RuntimeError("Error al guardar el borrador en la base de datos.")
    survey = survey_result.data[0]
    survey["questions"] = _replace_questions(survey["id"], payload.questions)
    return survey


def update_draft(survey_id: int, payload) -> dict:
    """Actualiza un borrador existente y sustituye sus preguntas.

    La validacion de propiedad y de estado (status == 'draft') se hace en la
    capa de API antes de invocar esta funcion.
    """
    update_data = {"title": payload.title or "(sin titulo)"}
    updated = (
        supabase.table("surveys")
        .update(update_data)
        .eq("id", survey_id)
        .execute()
    )
    if not updated.data:
        raise RuntimeError("No se pudo actualizar el borrador.")
    survey = updated.data[0]
    survey["questions"] = _replace_questions(survey_id, payload.questions)
    return survey


def create_response(payload) -> dict:
    """
    US-08 — Persiste una respuesta (tabla responses) y sus answers asociadas.

    Operacion atomica manual: insert response -> insert answers (con rollback).
    Lanza:
      - LookupError si la encuesta no existe.
      - ValueError si la encuesta no esta activa.
      - RuntimeError ante un fallo de la base de datos.
    """
    survey = get_survey(payload.survey_id)
    if survey is None:
        raise LookupError("La encuesta indicada no existe.")
    if survey.get("status") != "active":
        raise ValueError("La encuesta no esta activa para recibir respuestas.")

    response_result = (
        supabase.table("responses").insert({"survey_id": payload.survey_id}).execute()
    )
    if not response_result.data:
        raise RuntimeError("Error al guardar la respuesta en la base de datos.")

    response = response_result.data[0]
    response_id = response["id"]

    def _build_answers() -> list:
        return [
            {
                "response_id": response_id,
                "question_id": a.question_id,
                "answer_text": a.answer_text,
            }
            for a in payload.answers
        ]

    answers_result = (
        supabase.table("answers").insert(_build_answers()).execute()
    )
    if not answers_result.data:
        # Rollback manual
        supabase.table("responses").delete().eq("id", response_id).execute()
        raise RuntimeError(
            "Error al guardar las respuestas. La operacion fue revertida."
        )

    response["answers"] = answers_result.data
    return response


def get_open_question_answers(survey_id: int, question_id: int) -> list:
    """
    US-16 — Devuelve los ``answer_text`` no vacíos de una pregunta concreta.

    A diferencia de ``get_survey_responses_raw``, esta función no carga el resto
    de preguntas/respuestas de la encuesta: hace un filtro directo por
    ``question_id`` para que el coste sea predecible cuando la encuesta tiene
    miles de respuestas.

    Devuelve siempre una lista (posiblemente vacía). La validación de que la
    pregunta pertenece a la encuesta se hace en la capa de API.
    """
    questions = (
        supabase.table("questions")
        .select("id")
        .eq("id", question_id)
        .eq("survey_id", survey_id)
        .execute()
    )
    if not (questions.data or []):
        return []

    response_ids = [
        row["id"]
        for row in (
            supabase.table("responses")
            .select("id")
            .eq("survey_id", survey_id)
            .execute()
            .data
            or []
        )
    ]
    if not response_ids:
        return []

    answers = (
        supabase.table("answers")
        .select("answer_text")
        .in_("response_id", response_ids)
        .eq("question_id", question_id)
        .execute()
        .data
        or []
    )
    return [a["answer_text"] for a in answers if a.get("answer_text")]


def get_survey_responses_raw(survey_id: int) -> list:
    """
    Retorna todas las respuestas de una encuesta en formato fila para
    exportación. Cada elemento del resultado es un dict con:
      response_id, submitted_at, question_position, question_content,
      question_type, answer_text.
    Ordenado por response_id y luego por position de la pregunta.
    """
    questions_result = (
        supabase.table("questions")
        .select("id, content, question_type, position")
        .eq("survey_id", survey_id)
        .order("position")
        .execute()
    )
    questions = {q["id"]: q for q in (questions_result.data or [])}

    responses_result = (
        supabase.table("responses")
        .select("id, submitted_at")
        .eq("survey_id", survey_id)
        .order("id")
        .execute()
    )
    responses_by_id = {r["id"]: r for r in (responses_result.data or [])}
    response_ids = list(responses_by_id.keys())

    rows = []
    if response_ids:
        answers_result = (
            supabase.table("answers")
            .select("response_id, question_id, answer_text")
            .in_("response_id", response_ids)
            .order("response_id")
            .execute()
        )
        for answer in (answers_result.data or []):
            response = responses_by_id.get(answer["response_id"])
            question = questions.get(answer["question_id"])
            if response and question:
                rows.append({
                    "response_id": answer["response_id"],
                    "submitted_at": response["submitted_at"],
                    "question_position": question["position"],
                    "question_content": question["content"],
                    "question_type": question["question_type"],
                    "answer_text": answer["answer_text"],
                })

    rows.sort(key=lambda r: (r["response_id"], r["question_position"]))
    return rows


def _aggregate_choice(answer_texts: list, declared_options: list) -> tuple:
    """
    Agrega respuestas de preguntas de opcion (multiple_choice / yes_no).

    Devuelve `(options, total)` donde `options` es una lista de dicts
    `{option, count, percentage}`. Incluye primero las opciones declaradas
    (aunque tengan 0 respuestas) y luego cualquier respuesta que no coincida
    con las opciones declaradas.
    """
    total = len(answer_texts)
    counts: dict = {}
    for text in answer_texts:
        counts[text] = counts.get(text, 0) + 1

    def _pct(count: int) -> float:
        return round(count / total * 100, 2) if total else 0.0

    options = []
    seen = set()
    for option in declared_options or []:
        count = counts.get(option, 0)
        options.append({"option": option, "count": count, "percentage": _pct(count)})
        seen.add(option)

    for text, count in counts.items():
        if text not in seen:
            options.append({"option": text, "count": count, "percentage": _pct(count)})

    return options, total


def get_survey_results(survey: dict) -> dict:
    """
    US-10 — Agrega las respuestas de una encuesta.

    - Preguntas `multiple_choice` / `yes_no`: porcentajes por opcion.
    - Preguntas `open`: lista de textos.
    Recibe el registro de la encuesta ya cargado (la verificacion de
    propiedad se hace en la capa de API).
    """
    survey_id = survey["id"]

    questions_result = (
        supabase.table("questions")
        .select("*")
        .eq("survey_id", survey_id)
        .order("position")
        .execute()
    )
    questions = questions_result.data or []

    responses_result = (
        supabase.table("responses").select("id").eq("survey_id", survey_id).execute()
    )
    response_ids = [row["id"] for row in (responses_result.data or [])]

    answers_by_question: dict = {}
    if response_ids:
        answers_result = (
            supabase.table("answers")
            .select("question_id, answer_text")
            .in_("response_id", response_ids)
            .execute()
        )

        for answer in (answers_result.data or []):
            answers_by_question.setdefault(answer["question_id"], []).append(
                answer["answer_text"]
            )

    question_results = []
    for question in questions:
        question_type = question["question_type"]
        answer_texts = answers_by_question.get(question["id"], [])
        entry = {
            "question_id": question["id"],
            "content": question["content"],
            "question_type": question_type,
            "total_answers": len(answer_texts),
        }
        if question_type == "open":
            entry["texts"] = answer_texts
        elif question_type == "yes_no":
            options, _ = _aggregate_choice(answer_texts, YES_NO_OPTIONS)
            entry["options"] = options
        elif question_type == "multiple_choice":
            options, _ = _aggregate_choice(answer_texts, question.get("options") or [])
            entry["options"] = options
        else:
            entry["texts"] = answer_texts
        question_results.append(entry)

    return {
        "survey_id": survey_id,
        "title": survey["title"],
        "status": survey["status"],
        "total_responses": len(response_ids),
        "questions": question_results,
    }
