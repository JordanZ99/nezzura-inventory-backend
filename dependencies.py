# ==============================================================================
# backend/dependencies.py
# FastAPI Dependency: valida la sesión del usuario mediante el JWT de Supabase.
# ==============================================================================

import os
import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

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


def validar_sesion(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> bool:
    """Valida que el usuario tenga una sesión activa en Supabase."""
    if _DEV_MODE:
        return True

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión requerida. Inicie sesión nuevamente.",
        )

    token = credentials.credentials
    try:
        # Obtenemos la llave de firma dinámicamente
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        
        # Validamos el token
        jwt.decode(
            token,
            signing_key.key,
            algorithms=["HS256", "RS256", "ES256"],
            audience="authenticated",
        )
        return True

    except Exception as e:
        print(f"DEBUG AUTH: Fallo en la validación de sesión: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Sesión inválida o expirada: {str(e)}",
        )
