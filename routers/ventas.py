# Reemplaza todo backend/routers/ventas.py por esto:

from database.ventas import (
    get_ventas,
    actualizar_venta,
    eliminar_venta,
    cobrar_carrito as cobrar_carrito_atomico,
)
from database.conexion import query          # Necesario para leer la venta actual al procesar un PATCH parcial
from dependencies import get_tenant_id  # <--- Importación segura
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/ventas", tags=["Ventas"])

class ItemCarrito(BaseModel):
    producto   : str
    cantidad   : int
    precio_real: float
    id_lote    : Optional[str] = None

class Carrito(BaseModel):
    items: list[ItemCarrito]

class ActualizarVenta(BaseModel):
    # Todos los campos son opcionales para soportar PATCH parcial.
    # El router se encarga de rellenar los campos ausentes con los valores
    # actuales de la venta antes de delegar a la lógica de negocio, evitando
    # el error 422 que ocurría cuando el frontend enviaba solo los campos modificados.
    fecha         : Optional[str]   = None
    cantidad      : Optional[int]   = None
    precio_real   : Optional[float] = None
    costo_unitario: Optional[float] = None
    total_venta   : Optional[float] = None
    ganancia_bruta: Optional[float] = None

@router.get("/")
def listar_ventas(limit: int = 500, tenant_id: str = Depends(get_tenant_id)):
    resultado = get_ventas(tenant_id, limit)
    return resultado

@router.post("/cobrar")
def cobrar_carrito(carrito: Carrito, tenant_id: str = Depends(get_tenant_id)):
    """
    Cobra los items del carrito en UNA SOLA transacción atómica:
    el stock se descuenta (PEPS) y las ventas se registran juntas.
    Si algo falla a mitad del proceso, todo se revierte (rollback) y no queda
    ni stock descontado sin venta, ni venta sin stock descontado.

    NOTA: Ya NO se valida stock insuficiente. Si no hay suficiente inventario,
    el stock se manejará en negativo para permitir la venta.
    La advertencia al usuario se maneja desde el frontend.
    """
    items = [
        {
            "producto": item.producto,
            "cantidad": item.cantidad,
            "precio_real": item.precio_real,
            "id_lote": item.id_lote,
        }
        for item in carrito.items
    ]

    resultado = cobrar_carrito_atomico(items, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=resultado.get("mensaje", "Error al cobrar el carrito"),
        )
    return resultado

@router.patch("/{venta_id}")
def corregir_venta(venta_id: int, data: ActualizarVenta, tenant_id: str = Depends(get_tenant_id)):
    """
    Modifica una venta.
    Acepta PATCH parcial: si un campo no viene en el body, se conserva el valor
    actual de la venta. Esto resuelve el desfase con el frontend, que enviaba
    solo los campos modificados y provocaba un 422 Unprocessable Entity porque
    el modelo Pydantic exigía todos los campos como obligatorios.
    """
    # 1. Obtener la venta actual para rellenar los campos omitidos en el PATCH.
    #    Filtramos por tenant_id para garantizar que el usuario solo pueda
    #    modificar ventas de su propio tenant (aislamiento multitenant).
    fila = query(
        "SELECT fecha, cantidad, precio_real, costo_unitario, total_venta, ganancia_bruta "
        "FROM ventas WHERE id = %s AND tenant_id = %s",
        (venta_id, tenant_id)
    )
    if not fila:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    v = fila[0]

    # 2. Construir el payload completo mezclando valores existentes + nuevos.
    #    Si el frontend envió un campo, se usa ese; si no, se conserva el actual.
    fecha          = data.fecha          if data.fecha          is not None else v["fecha"]
    cantidad       = data.cantidad       if data.cantidad       is not None else int(v["cantidad"])
    precio_real    = data.precio_real    if data.precio_real    is not None else float(v["precio_real"])
    costo_unitario = data.costo_unitario if data.costo_unitario is not None else float(v["costo_unitario"])
    total_venta    = data.total_venta    if data.total_venta    is not None else float(v["total_venta"])
    ganancia_bruta = data.ganancia_bruta if data.ganancia_bruta is not None else float(v["ganancia_bruta"])

    # 3. Delegar a la lógica de negocio con valores completos.
    #    actualizar_venta recalcula el stock según la diferencia de cantidad,
    #    por lo que siempre necesita los valores finales, no parciales.
    resultado = actualizar_venta(venta_id, fecha, cantidad, precio_real, costo_unitario, total_venta, ganancia_bruta, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado

@router.delete("/{venta_id}")
def borrar_venta(venta_id: int, tenant_id: str = Depends(get_tenant_id)):
    return eliminar_venta(venta_id, tenant_id)
