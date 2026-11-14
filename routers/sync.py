# ==============================================================================
# backend/routers/sync.py
# Sincronización del Modo Offline (híbrido) para planes básico/plus.
#
# Autenticación: sesión Supabase (Depends(get_tenant_id)) — el modo híbrido
# opera con sesión; NO usa licencias de dispositivo (eso era el plan local).
#
#   POST /sync/subir  → delta del dispositivo (append-only + replay PEPS)
#   GET  /sync/bajar  → filas de la nube con updated_at > cursor
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import get_tenant_id
from database.sync import subir_delta, bajar_delta, query_log, _ErrorSync

router = APIRouter(prefix="/sync", tags=["Sincronización"])


class SolicitudSubida(BaseModel):
    """Delta del dispositivo. Las secciones son opcionales; el extractor del
    cliente solo manda lo que cambió (hash contra su espejo local)."""
    desde: str | None = None          # cursor de bajada del dispositivo (para divergencias)
    productos: list[dict] = []
    variaciones: list[dict] = []
    lotes: list[dict] = []
    ediciones_productos: list[dict] = []
    ediciones_lotes: list[dict] = []
    ordenes: list[dict] = []          # [{orden: {...}, ventas: [...]}]
    gastos: list[dict] = []


@router.post("/subir")
def subir(data: SolicitudSubida, tenant_id: str = Depends(get_tenant_id)):
    try:
        return subir_delta(tenant_id, data.model_dump())
    except _ErrorSync as e:
        raise HTTPException(status_code=e.status, detail=e.mensaje)


@router.get("/bajar")
def bajar(desde: str | None = None, tenant_id: str = Depends(get_tenant_id)):
    try:
        return bajar_delta(tenant_id, desde)
    except _ErrorSync as e:
        raise HTTPException(status_code=e.status, detail=e.mensaje)


@router.get("/log")
def log(limite: int = 30, tenant_id: str = Depends(get_tenant_id)):
    """Auditoría de la sync: decisiones LWW y valores descartados."""
    return {"ok": True, "log": query_log(tenant_id, limite)}
