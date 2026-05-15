# ==============================================================================
# backend/routers/ventas.py
# Endpoints de ventas — registro, edición y carrito PEPS.
# ==============================================================================

from database.lotes  import descontar_stock_peps
from database.ventas import get_ventas, insertar_venta, actualizar_venta, eliminar_venta
from dependencies import validar_sesion
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/ventas", tags=["Ventas"])


# --- Modelos ---

class ItemCarrito(BaseModel):
    producto   : str
    cantidad   : int
    precio_real: float

class Carrito(BaseModel):
    items: list[ItemCarrito]

class ActualizarVenta(BaseModel):
    fecha         : str
    cantidad      : int
    precio_real   : float
    total_venta   : float
    ganancia_bruta: float


# --- Endpoints ---

@router.get("/")
def listar_ventas(limit: int = 500, _: bool = Depends(validar_sesion)):
    """Historial de ventas ordenado por fecha descendente."""
    resultado = get_ventas(limit)
    return resultado


@router.post("/cobrar")
def cobrar_carrito(carrito: Carrito, _: bool = Depends(validar_sesion)):
    """
    Procesa el carrito completo de una sola vez.
    Aplica PEPS a cada item, valida stock antes de guardar cualquier venta.
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
def corregir_venta(venta_id: int, data: ActualizarVenta, _: bool = Depends(validar_sesion)):
    """Corrige la fecha, cantidad o precio de una venta registrada por error."""
    resultado = actualizar_venta(
        venta_id, data.fecha, data.cantidad,
        data.precio_real, data.total_venta, data.ganancia_bruta
    )
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje", "Error al actualizar venta"))
    return resultado


@router.delete("/{venta_id}")
def borrar_venta(venta_id: int, _: bool = Depends(validar_sesion)):
    """Elimina una venta por id."""
    return eliminar_venta(venta_id)