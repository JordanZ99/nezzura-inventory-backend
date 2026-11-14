# ==============================================================================
# backend/routers/planes_clientes.py
# "Mis cuentas" (giro 'servicios', migración 075): endpoints del registro manual
# de planes otorgados a clientes. El tenant_id SIEMPRE sale del JWT; nunca del
# request. Patrón de routers/cobros.py (404 'no_encontrado', 422 validación).
# ==============================================================================

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import get_tenant_id
from schemas.planes_clientes import PlanNuevo, PlanActualizar
from database import planes_clientes as db_planes

router = APIRouter(prefix="/planes", tags=["Mis cuentas"])


def _revisar(resultado: dict, mensaje: str) -> dict:
    """404 si el tipo es 'no_encontrado', 422 en validación — patrón cobros.py."""
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", mensaje))
    return resultado


@router.get("/")
def listar(
    estado: Optional[str] = Query(None, pattern="^(cobrando|gratis|pausado)$"),
    tenant_id: str = Depends(get_tenant_id),
):
    """Cuaderno completo de planes otorgados (con nombre del cliente).
    Con `estado` filtra una sola vista (cobrando/gratis/pausado)."""
    return db_planes.listar_planes(tenant_id, estado=estado)


@router.post("/")
def crear(data: PlanNuevo, tenant_id: str = Depends(get_tenant_id)):
    """Anota que este cliente tiene el plan indicado (registro informativo:
    si va a cobrarse, la cuota se crea aparte en la pestaña Cuotas)."""
    resultado = db_planes.crear_plan(
        tenant_id, data.cliente_id, data.plan,
        estado_cobro=data.estado_cobro, monto_acordado=data.monto_acordado,
        otorgado=data.otorgado, notas=data.notas,
    )
    _revisar(resultado, "Error al crear la anotación")
    return resultado


@router.patch("/{plan_id}")
def actualizar(plan_id: str, data: PlanActualizar, tenant_id: str = Depends(get_tenant_id)):
    """Edita cliente/plan/estado/monto/fecha/notas (monto 0 = quitarlo)."""
    resultado = db_planes.actualizar_plan(
        tenant_id, plan_id,
        cliente_id=data.cliente_id, plan=data.plan, estado_cobro=data.estado_cobro,
        monto_acordado=data.monto_acordado, otorgado=data.otorgado, notas=data.notas,
    )
    _revisar(resultado, "Error al actualizar la anotación")
    return resultado


@router.delete("/{plan_id}")
def eliminar(plan_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Borra la anotación del cuaderno (no toca cuotas ni cobros)."""
    resultado = db_planes.eliminar_plan(tenant_id, plan_id)
    _revisar(resultado, "Anotación no encontrada")
    return resultado
