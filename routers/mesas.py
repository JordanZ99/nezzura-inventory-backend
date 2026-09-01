# ==============================================================================
# backend/routers/mesas.py
# API de mesas del preset restaurante (Fase 2). El tenant_id SIEMPRE sale del
# JWT; nunca del cliente.
#
# NOTA de rutas: /mesas/reordenar se declara ANTES de /mesas/{mesa_id} para que
# FastAPI no confunda "reordenar" con un id.
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_tenant_id
from schemas.mesas import (
    NuevaMesa, ActualizarMesa, ReordenarMesas, AgregarItemsMesa, ActualizarItemMesa,
)
from database.mesas import (
    listar_mesas, crear_mesa, actualizar_mesa, eliminar_mesa, reordenar_mesas,
    agregar_items_mesa, editar_item_mesa, quitar_item_mesa,
    pedir_cuenta, regresar_a_ocupada, cancelar_orden_mesa,
)

router = APIRouter(prefix="/mesas", tags=["Mesas"])


def _resultado(resultado: dict):
    """Convierte el dict de database/ en respuesta o HTTPException (422/400)."""
    if not resultado.get("ok"):
        raise HTTPException(
            status_code=422 if resultado.get("tipo") == "validacion" else 400,
            detail=resultado.get("mensaje", "Error"),
        )
    return resultado


@router.get("/")
def listar(tenant_id: str = Depends(get_tenant_id)):
    """Parrilla completa: mesas con sus renglones abiertos y agregados."""
    return listar_mesas(tenant_id)


@router.post("/")
def crear(data: NuevaMesa, tenant_id: str = Depends(get_tenant_id)):
    return _resultado(crear_mesa(tenant_id, data.nombre, data.capacidad))


@router.patch("/reordenar")
def reordenar(data: ReordenarMesas, tenant_id: str = Depends(get_tenant_id)):
    return _resultado(reordenar_mesas(tenant_id, [m.dict() for m in data.mesas]))


@router.patch("/{mesa_id}")
def actualizar(mesa_id: str, data: ActualizarMesa, tenant_id: str = Depends(get_tenant_id)):
    return _resultado(actualizar_mesa(mesa_id, tenant_id, nombre=data.nombre, capacidad=data.capacidad))


@router.delete("/{mesa_id}")
def eliminar(mesa_id: str, tenant_id: str = Depends(get_tenant_id)):
    return _resultado(eliminar_mesa(mesa_id, tenant_id))


@router.post("/{mesa_id}/items")
def agregar_items(mesa_id: str, data: AgregarItemsMesa, tenant_id: str = Depends(get_tenant_id)):
    """Agrega renglones a la orden; mesa Libre → Ocupada (orden abierta)."""
    return _resultado(agregar_items_mesa(mesa_id, tenant_id, [it.dict() for it in data.items]))


@router.patch("/{mesa_id}/items/{item_id}")
def editar_item(mesa_id: str, item_id: str, data: ActualizarItemMesa,
                tenant_id: str = Depends(get_tenant_id)):
    return _resultado(editar_item_mesa(
        mesa_id, item_id, tenant_id,
        cantidad=data.cantidad, precio_unitario=data.precio_unitario, notas=data.notas,
    ))


@router.delete("/{mesa_id}/items/{item_id}")
def quitar_item(mesa_id: str, item_id: str, tenant_id: str = Depends(get_tenant_id)):
    return _resultado(quitar_item_mesa(mesa_id, item_id, tenant_id))


@router.post("/{mesa_id}/cuenta")
def cuenta(mesa_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Ocupada → Cuenta (pidieron la bill)."""
    return _resultado(pedir_cuenta(mesa_id, tenant_id))


@router.post("/{mesa_id}/regresar")
def regresar(mesa_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Cuenta → Ocupada (se equivocaron y siguen pidiendo)."""
    return _resultado(regresar_a_ocupada(mesa_id, tenant_id))


@router.post("/{mesa_id}/cancelar")
def cancelar(mesa_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Cancela la orden abierta SIN cobrar: borra renglones y libera la mesa."""
    return _resultado(cancelar_orden_mesa(mesa_id, tenant_id))
