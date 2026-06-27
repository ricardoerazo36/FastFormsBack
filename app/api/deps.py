"""
Dependencias y guards reutilizables de la capa de API.

`assert_survey_in_status` centraliza la regla de inmutabilidad (US-04): una
encuesta solo puede modificarse mientras esté en alguno de los estados
permitidos. Por defecto el único estado editable es ``draft``, pero la función
recibe ``allowed_statuses`` para poder reutilizarse con otros estados futuros.
"""

from typing import Iterable, Optional

from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from core.config import supabase

# Estados en los que una encuesta (y sus preguntas) puede editarse.
EDITABLE_STATUSES = ("draft",)

security = HTTPBearer()


def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    Extrae y valida el token JWT (Bearer) del header Authorization.
    Utiliza el cliente de Supabase para obtener el usuario autenticado
    y devuelve su ID real (UUID).
    """
    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if not user_response or not user_response.user:
            raise ValueError("Usuario no encontrado")
        return user_response.user.id
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )


_optional_security = HTTPBearer(auto_error=False)


def get_optional_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_security),
) -> Optional[str]:
    """
    Variante opcional del guard: si el header `Authorization` no llega, devuelve
    `None` en vez de 401. Se usa en endpoints accesibles por encuestados
    anónimos (p. ej. `/transcribe` para US-14).
    """
    if credentials is None:
        return None
    try:
        user_response = supabase.auth.get_user(credentials.credentials)
        if not user_response or not user_response.user:
            return None
        return user_response.user.id
    except Exception:
        return None


def assert_survey_in_status(
    survey: Optional[dict],
    allowed_statuses: Iterable[str] = EDITABLE_STATUSES,
) -> dict:
    """
    Valida que la encuesta exista y esté en uno de los estados permitidos.

    - 404 si la encuesta no existe.
    - 403 si la encuesta está en un estado distinto a los permitidos
      (p. ej. ya fue publicada).
    """
    allowed = tuple(allowed_statuses)

    if survey is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Encuesta no encontrada.",
        )

    if survey.get("status") not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Operación no permitida: la encuesta está en estado "
                f"'{survey.get('status')}'. Solo se permite cuando está en: "
                f"{', '.join(allowed)}."
            ),
        )

    return survey
