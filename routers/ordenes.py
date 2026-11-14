# ==============================================================================
# backend/routers/ordenes.py
# Órdenes de servicio (giro 'servicios', migración 077): endpoints del registro
# del TRABAJO. El tenant_id SIEMPRE sale del JWT; nunca del request. Patrón de
# routers/cobros.py (404 'no_encontrado', 422 validación).
# ==============================================================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import get_tenant_id
from schemas.ordenes import OrdenNueva, OrdenActualizar
from database import ordenes as db_ordenes

router = APIRouter(prefix="/ordenes", tags=["Órdenes de servicio"])


def _revisar(resultado: dict, mensaje: str) -> dict:
    """404 si el tipo es 'no_encontrado', 422 en validación — patrón cobros.py."""
    if not resultado.get("ok"):
        status = 404 if resultado.get("tipo") == "no_encontrado" else 422
        raise HTTPException(status_code=status, detail=resultado.get("mensaje", mensaje))
    return resultado


@router.get("/")
def listar(
    estado: Optional[str] = Query(None, pattern="^(recibido|en_proceso|esperando|listo|entregado|cancelado)$"),
    cliente_id: Optional[str] = None,
    solo_abiertas: bool = False,
    tenant_id: str = Depends(get_tenant_id),
):
    """Órdenes del negocio (con nombre del cliente y saldo del cobro enlazado).
    `solo_abiertas` = cola de trabajo vivo (todo menos entregado/cancelado)."""
    return db_ordenes.listar_ordenes(
        tenant_id, estado=estado, cliente_id=cliente_id, solo_abiertas=solo_abiertas,
    )


@router.post("/")
def crear(data: OrdenNueva, tenant_id: str = Depends(get_tenant_id)):
    """Abre una orden de servicio (intake mínimo: título)."""
    resultado = db_ordenes.crear_orden(
        tenant_id, data.titulo, tipo=data.tipo, cliente_id=data.cliente_id,
        equipo=data.equipo, detalle_reportado=data.detalle_reportado,
        solucion=data.solucion, estado=data.estado,
        fecha_recibido=data.fecha_recibido, precio=data.precio, notas=data.notas,
    )
    _revisar(resultado, "Error al crear la orden")
    return resultado


@router.patch("/{orden_id}")
def actualizar(orden_id: str, data: OrdenActualizar, tenant_id: str = Depends(get_tenant_id)):
    """Edita la orden. Pasar a 'entregado' sella fecha_entregado=HOY."""
    resultado = db_ordenes.actualizar_orden(
        tenant_id, orden_id,
        cliente_id=data.cliente_id, tipo=data.tipo, titulo=data.titulo,
        equipo=data.equipo, detalle_reportado=data.detalle_reportado,
        solucion=data.solucion, estado=data.estado,
        fecha_recibido=data.fecha_recibido, fecha_entregado=data.fecha_entregado,
        precio=data.precio, cobro_id=data.cobro_id, cuota_id=data.cuota_id,
        notas=data.notas,
    )
    _revisar(resultado, "Error al actualizar la orden")
    return resultado


@router.delete("/{orden_id}")
def eliminar(orden_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Borra la orden (el cobro enlazado no se toca)."""
    resultado = db_ordenes.eliminar_orden(tenant_id, orden_id)
    _revisar(resultado, "Orden no encontrada")
    return resultado
