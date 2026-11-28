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

if not SUPABASE_URL:
    raise RuntimeError("ERROR CRÍTICO: SUPABASE_URL no está configurada en las variables de entorno.")

# URL de las llaves públicas
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"

# Cliente JWKS
jwks_client = PyJWKClient(JWKS_URL)

_bearer_scheme = HTTPBearer(auto_error=False)


def validar_sesion(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> bool:


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

    except Exception:
        # El detalle de PyJWT (algoritmo, kid, expiración) no viaja al cliente:
        # sirve para afinar un token. El 401 genérico es suficiente.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión inválida o expirada.",
        )

def get_tenant_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """
    Igual que validar_sesion, pero además de verificar el token,
    extrae y devuelve el UUID del usuario (el tenant_id).
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión requerida.",
        )

    token = credentials.credentials
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["HS256", "RS256", "ES256"],
            audience="authenticated",
        )
        
        # ¡Aquí está la magia! Extraemos el 'sub' (ID único de Supabase)
        tenant_id = payload.get("sub")
        if not tenant_id:
            raise HTTPException(status_code=401, detail="El token no contiene un ID de usuario.")
            
        return tenant_id

    except HTTPException:
        # Conserva el 401 específico de "el token no contiene un ID".
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado.",
        )
