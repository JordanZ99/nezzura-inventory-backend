# ==============================================================================
# backend/routers/actividad.py
# Historial completo (migración 062): feed paginado del registro de actividad
# que NO es stock: ediciones de producto (foto, precio, nombre...), CRM
# (clientes, etiquetas, interacciones, puntos) y cobros del giro 'servicios'.
# El detalle de STOCK vive en el ledger 040 (routers/inventario.py).
# ==============================================================================

from fastapi import APIRouter, Query, Depends
from typing import Optional

from dependencies import get_tenant_id
from database.actividad import get_actividad

router = APIRouter(prefix="/actividad", tags=["Actividad"])


@router.get("")
def listar_actividad(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    dominio: Optional[str] = Query(
        None, pattern="^(producto|cliente|cobro)$",
        description="Filtra por sección de la app"),
    texto: Optional[str] = Query(None, description="Búsqueda en el resumen"),
    desde: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    hasta: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    tenant_id: str = Depends(get_tenant_id),
):
    """Feed paginado del historial. Fechas de inicio/fin en la zona horaria
    del negocio (días). Las fechas de inicio/fin son inclusivas."""
    return get_actividad(
        tenant_id, limit=limit, offset=offset, dominio=dominio,
        texto=texto, desde=desde, hasta=hasta,
    )
