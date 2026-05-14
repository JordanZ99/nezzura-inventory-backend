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
SUPABASE_URL = os.getenv("SUPABASE_URL", os.getenv("NEXT_PUBLIC_SUPABASE_URL", ""))
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""))

_DEV_MODE = not SUPABASE_URL

# URL de las llaves públicas
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"

# Cliente JWKS
jwks_client = PyJWKClient(JWKS_URL) if not _DEV_MODE else None

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
        # Intentamos obtener la llave de firma
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["HS256", "RS256", "ES256"],
            audience="authenticated",
        )
        
        tid = payload.get("sub")
        if not tid:
            raise Exception("El token no contiene el ID de usuario (sub)")
        return tid

    except Exception as e:
        print(f"DEBUG AUTH: Fallo en la validación: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Error de acceso: {str(e)}. Revisa las llaves en Render.",
        )
