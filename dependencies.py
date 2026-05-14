# ==============================================================================
# backend/dependencies.py
# FastAPI Dependency: extrae y valida el tenant_id del JWT de Supabase usando JWKS.
# ==============================================================================

import os
import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from database.conexion import DEFAULT_TENANT_ID

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
# Priorizamos SUPABASE_URL, fallback a NEXT_PUBLIC_SUPABASE_URL
SUPABASE_URL = os.getenv("SUPABASE_URL", os.getenv("NEXT_PUBLIC_SUPABASE_URL", ""))
_DEV_MODE = not SUPABASE_URL

# URL de las llaves públicas de Supabase
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"

# Cliente para manejar las llaves dinámicamente
jwks_client = PyJWKClient(JWKS_URL) if not _DEV_MODE else None
_bearer_scheme = HTTPBearer(auto_error=False)


def get_tenant_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """
    Extrae y verifica el token usando las llaves públicas de Supabase (JWKS).
    Esto soporta automáticamente tanto HS256 como ES256.
    """
    if _DEV_MODE:
        return DEFAULT_TENANT_ID

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticación requerido.",
        )

    token = credentials.credentials
    try:
        # Obtenemos la llave de firma directamente del JWT
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["HS256", "RS256", "ES256"],
            audience="authenticated",
        )
        
        tenant_id = payload.get("sub")
        if not tenant_id:
            raise HTTPException(status_code=401, detail="Token sin 'sub'")
            
        return tenant_id

    except Exception as e:
        print(f"DEBUG AUTH: Fallo en verificación JWKS: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Error de autenticación: {str(e)}. Verifica que SUPABASE_URL esté en Render.",
        )
