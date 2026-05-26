# Reemplaza todo backend/routers/ventas.py por esto:

from database.lotes  import descontar_stock_peps
from database.ventas import get_ventas, insertar_venta, actualizar_venta, eliminar_venta
from dependencies import get_tenant_id  # <--- Importación segura
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/ventas", tags=["Ventas"])

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

@router.get("/")
def listar_ventas(limit: int = 500, tenant_id: str = Depends(get_tenant_id)):
    resultado = get_ventas(tenant_id, limit)
    return resultado

@router.post("/cobrar")
def cobrar_carrito(carrito: Carrito, tenant_id: str = Depends(get_tenant_id)):
    """
    Cobra los items del carrito.
    
    NOTA: Ya NO se valida stock insuficiente. Si no hay suficiente inventario,
    el stock se manejará en negativo para permitir la venta.
    La advertencia al usuario se maneja desde el frontend.
    """
    ventas_a_guardar = []

    for item in carrito.items:
        # descontar_stock_peps ahora siempre devuelve una lista (nunca None)
        # porque permite stock negativo
        resultado = descontar_stock_peps(item.producto, item.cantidad, item.precio_real, tenant_id)
        ventas_a_guardar.extend(resultado)

    for venta in ventas_a_guardar:
        insertar_venta(venta, tenant_id)

    total = sum(v["total_venta"] for v in ventas_a_guardar)
    return {"ok": True, "ventas": len(ventas_a_guardar), "total_cobrado": total}

@router.patch("/{venta_id}")
def corregir_venta(venta_id: int, data: ActualizarVenta, tenant_id: str = Depends(get_tenant_id)):
    resultado = actualizar_venta(venta_id, data.fecha, data.cantidad, data.precio_real, data.total_venta, data.ganancia_bruta, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado

@router.delete("/{venta_id}")
def borrar_venta(venta_id: int, tenant_id: str = Depends(get_tenant_id)):
    return eliminar_venta(venta_id, tenant_id)
