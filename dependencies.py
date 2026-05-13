# ==============================================================================
# backend/dependencies.py
# FastAPI Dependency: extrae y valida el tenant_id del JWT de Supabase.
# ==============================================================================

import os
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from database.conexion import DEFAULT_TENANT_ID

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")
# En modo desarrollo (sin secret configurado) devuelve el tenant por defecto
_DEV_MODE = not SUPABASE_JWT_SECRET

_bearer_scheme = HTTPBearer(auto_error=False)


def get_tenant_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """
    Extrae el tenant_id (= sub del JWT) del token Bearer enviado por el frontend.

    Modo desarrollo (SUPABASE_JWT_SECRET vacío):
        Devuelve DEFAULT_TENANT_ID para que la app siga funcionando sin login.

    Modo producción:
        Verifica la firma del token con el JWT secret de Supabase y extrae
        el claim 'sub' (= auth.uid()), que es el tenant_id del usuario.
    """
    if _DEV_MODE:
        # Sin secret configurado → modo desarrollo, usa tenant por defecto
        return DEFAULT_TENANT_ID

    # Modo producción: el token es obligatorio
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticación requerido.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expirado. Inicia sesión nuevamente.",
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido: {str(e)}",
        )

    # El 'sub' de Supabase es el auth.uid(), que coincide con el tenant_id
    tenant_id = payload.get("sub")
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token sin identidad de usuario.",
        )

    return tenant_id
