# ==============================================================================
# backend/routers/licencias.py
# Licencias de dispositivo para el Modo Local (plan "local").
#
# 3 tokens distintos en esta API a propósito:
#   - JWT de Supabase (Authorization Bearer): DESEMPEÑA el login. Solo en
#     /activar y /dispositivos.
#   - JWT de LICENCIA (Authorization Bearer): hablando del dispositivo ya
#     activado, sin sesión Supabase. Solo /revalidar.
#
# El tenant_id SIEMPRE sale del token correspondiente; nunca del body.
# ==============================================================================

from fastapi import Depends, HTTPException, APIRouter
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from dependencies import get_tenant_id
from database.licencias import (
    activar_dispositivo, revalidar_licencia, listar_dispositivos, revocar_dispositivo,
)

router = APIRouter(prefix="/licencias", tags=["Licencias"])

_bearer = HTTPBearer(auto_error=False)


class SolicitudActivacion(BaseModel):
    device_id: str          # UUID v4 generado por el cliente (persistente en el aparato)
    nombre: str = "Dispositivo"


@router.post("/activar")
def activar(
    data: SolicitudActivacion,
    tenant_id: str = Depends(get_tenant_id),
):
    """Login (Supabase) + plan 'local' → licencia firmada para este device_id."""
    resultado = activar_dispositivo(tenant_id, data.device_id, data.nombre or "Dispositivo")
    if not resultado.get("ok"):
        codigo = resultado.get("codigo")
        status = 403 if codigo in ("PLAN_SIN_LOCAL", "LIMITE_DISPOSITIVOS") else 400
        raise HTTPException(status_code=status, detail=resultado.get("mensaje"), headers={"x-codigo": codigo or ""})
    return resultado


@router.post("/revalidar")
def revalidar(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """Sin sesión Supabase: el aparato presenta su licencia y estrena token.

    Aquí vive el enforcement server-side: dispositivo revocado o plan
    sin Modo Local → 401/403 y el dispositivo se bloquea al caducar su
    token actual.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Licencia requerida.")
    resultado = revalidar_licencia(credentials.credentials)
    if not resultado.get("ok"):
        codigo = resultado.get("codigo", "")
        status = 403 if codigo == "PLAN_SIN_LOCAL" else 401
        raise HTTPException(status_code=status, detail=resultado.get("mensaje"), headers={"x-codigo": codigo})
    return resultado


@router.get("/dispositivos")
def listar(tenant_id: str = Depends(get_tenant_id)):
    """Lista de aparatos con licencia (para el panel de configuración)."""
    return listar_dispositivos(tenant_id)


@router.delete("/dispositivos/{device_id}")
def revocar(device_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Revoca un dispositivo: su licencia muere al próximo revalidar."""
    return revocar_dispositivo(tenant_id, device_id)
