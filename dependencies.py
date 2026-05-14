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

# Intentaremos la ruta estándar de Supabase
JWKS_URL = f"{SUPABASE_URL}/auth/v1/jwks"

print(f"DEBUG AUTH: Configurando JWKS en {JWKS_URL}")

# Cliente para manejar las llaves dinámicamente
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
        # Obtenemos la llave de firma
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["HS256", "RS256", "ES256"],
            audience="authenticated",
        )
        
        tid = payload.get("sub")
        if not tid:
            raise Exception("Token no contiene el campo 'sub'")
        return tid

    except Exception as e:
        print(f"DEBUG AUTH: Fallo crítico: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Error de validación (JWKS): {str(e)}. Verifica la URL de Supabase.",
        )
