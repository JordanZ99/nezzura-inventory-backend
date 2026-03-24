# ==============================================================================
# backend/routers/ventas.py
# Endpoints de ventas — registro, edición y carrito PEPS.
# ==============================================================================

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database.lotes  import descontar_stock_peps
from database.ventas import get_ventas, insertar_venta, actualizar_venta, eliminar_venta

router = APIRouter(prefix="/ventas", tags=["Ventas"])


# --- Modelos ---

class ItemCarrito(BaseModel):
    producto   : str
    cantidad   : int
    precio_real: float

class Carrito(BaseModel):
    items: list[ItemCarrito]

class ActualizarVenta(BaseModel):
    cantidad      : int
    precio_real   : float
    total_venta   : float
    ganancia_bruta: float


# --- Endpoints ---

@router.get("/")
def listar_ventas(limit: int = 500):
    """Historial de ventas ordenado por fecha descendente."""
    return get_ventas(limit)


@router.post("/cobrar")
def cobrar_carrito(carrito: Carrito):
    """
    Procesa el carrito completo de una sola vez.
    Aplica PEPS a cada item, valida stock antes de guardar cualquier venta.
    Si un item falla, no se guarda nada.
    """
    ventas_a_guardar = []
    errores          = []

    # Paso 1 — validar todo antes de guardar nada
    for item in carrito.items:
        resultado = descontar_stock_peps(item.producto, item.cantidad, item.precio_real)
        if resultado is None:
            errores.append(f"Stock insuficiente para {item.producto}")
        else:
            ventas_a_guardar.extend(resultado)

    if errores:
        raise HTTPException(status_code=400, detail=errores)

    # Paso 2 — guardar todas las ventas
    for venta in ventas_a_guardar:
        insertar_venta(venta)

    total = sum(v["total_venta"] for v in ventas_a_guardar)
    return {
        "ok"           : True,
        "ventas"       : len(ventas_a_guardar),
        "total_cobrado": total
    }


@router.patch("/{venta_id}")
def corregir_venta(venta_id: int, data: ActualizarVenta):
    """Corrige cantidad o precio de una venta registrada por error."""
    return actualizar_venta(
        venta_id, data.cantidad,
        data.precio_real, data.total_venta, data.ganancia_bruta
    )


@router.delete("/{venta_id}")
def borrar_venta(venta_id: int):
    """Elimina una venta por id."""
    return eliminar_venta(venta_id)