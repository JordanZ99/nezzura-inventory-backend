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
import requests
from jwt import PyJWKClient

SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL", "")
# En modo desarrollo (sin URL configurada) devuelve el tenant por defecto
_DEV_MODE = not SUPABASE_URL
JWKS_URL = f"{SUPABASE_URL}/auth/v1/jwks"

# Cliente para manejar las llaves dinámicamente
jwks_client = PyJWKClient(JWKS_URL)
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
        # Obtenemos la llave correcta automáticamente desde Supabase
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["HS256", "ES256"],
            audience="authenticated",
        )
    except Exception as e:
        print(f"DEBUG AUTH: Error validando token: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Error de autenticación: {str(e)}",
        )

    tenant_id = payload.get("sub")
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token sin identidad de usuario.",
        )

    return tenant_id
