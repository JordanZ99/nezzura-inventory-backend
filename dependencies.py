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
_DEV_MODE = not SUPABASE_JWT_SECRET

_bearer_scheme = HTTPBearer(auto_error=False)


def get_tenant_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    if _DEV_MODE:
        return DEFAULT_TENANT_ID

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticación requerido.",
        )

    token = credentials.credentials
    try:
        # Volvemos al método manual pero con soporte de algoritmos ampliado
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256", "ES256"],
            audience="authenticated",
        )
    except Exception as e:
        print(f"DEBUG AUTH: Error con la Secret de Render: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Error de firma: {str(e)}. Revisa la JWT Secret en Render.",
        )

    tenant_id = payload.get("sub")
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token sin identidad de usuario.",
        )

    return tenant_id
