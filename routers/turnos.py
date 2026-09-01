# ==============================================================================
# backend/routers/turnos.py
# Turnos de caja con arqueo (Fase C): abrir (con fondo), listar con agregados
# y cerrar (contando el cajón contra el efectivo esperado).
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_tenant_id
from schemas.turnos import AbrirTurno, CerrarTurno
from database.turnos import abrir_turno, cerrar_turno, listar_turnos

router = APIRouter(prefix="/turnos", tags=["Turnos"])


@router.get("/")
def listar(limit: int = 50, tenant_id: str = Depends(get_tenant_id)):
    """Historial de turnos (abiertos con esperado en vivo, cerrados con arqueo)."""
    return listar_turnos(tenant_id, limit)


@router.post("/")
def abrir(data: AbrirTurno, tenant_id: str = Depends(get_tenant_id)):
    resultado = abrir_turno(tenant_id, data.monto_apertura)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado


@router.post("/{turno_id}/cerrar")
def cerrar(turno_id: str, data: CerrarTurno, tenant_id: str = Depends(get_tenant_id)):
    resultado = cerrar_turno(turno_id, tenant_id, data.efectivo_contado, data.notas)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado
