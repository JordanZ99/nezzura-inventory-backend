# Reemplaza todo backend/routers/ventas.py por esto:

from database.ventas import (
    get_ventas,
    get_ordenes,
    get_ordenes_paginadas,
    actualizar_venta,
    eliminar_venta,
    anular_orden,
    actualizar_orden,
    actualizar_pago_orden,
    cobrar_carrito as cobrar_carrito_atomico,
)
from database.conexion import query          # Necesario para leer la venta actual al procesar un PATCH parcial
from dependencies import get_tenant_id
from database.helpers import resolver_rango_q, ventana_ts_de_rango
from fastapi import APIRouter, HTTPException, Depends
from datetime import date
from typing import Optional
from schemas.ventas import (
    ItemCarrito, PagoItem, PagoCarrito, Carrito, ActualizarVenta, ActualizarOrden
)

router = APIRouter(prefix="/ventas", tags=["Ventas"])

@router.get("/")
def listar_ventas(
    limit: int = 500,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Renglones de venta en la ventana contable pedida (?desde/?hasta, 'YYYY-MM-DD').
    Sin ?desde ni ?hasta devuelve SOLO el mes contable actual (zona horaria del
    negocio) para no transportar todo el historial en cada carga.
    """
    try:
        d_desde, d_hasta = resolver_rango_q(tenant_id, desde, hasta)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d_desde, d_hasta)
    resultado = get_ventas(tenant_id, limit, desde_ts, hasta_ts)
    return resultado

@router.get("/ordenes")
def listar_ordenes(
    limit: int = 500,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Tickets (órdenes) con sus renglones anidados, más recientes primero.
    Cada orden: id, n_ticket (folio), fecha (ISO con zona), total, ganancia,
    cantidad_items (unidades), estado y ventas[] (renglones de la venta).
    Sin ?desde ni ?hasta devuelve SOLO el mes contable actual.
    """
    try:
        d_desde, d_hasta = resolver_rango_q(tenant_id, desde, hasta)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d_desde, d_hasta)
    return get_ordenes(tenant_id, limit, desde_ts, hasta_ts)


@router.get("/ordenes/paginadas")
def listar_ordenes_paginadas(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    todo: bool = False,
    pagina: int = 1,
    por_pagina: int = 10,
    busqueda: Optional[str] = None,
    orden: str = "fecha-desc",
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Historial de tickets paginado en servidor (una página por request).
    ?busqueda busca por folio o producto; ?orden: fecha-desc|fecha-asc|
    monto-desc|monto-asc|ganancia-desc. Sin ?desde/?hasta usa el mes contable;
    con ?todo=true abre el histórico completo.
    """
    if todo:
        d_desde, d_hasta = date(1970, 1, 1), None
    else:
        try:
            d_desde, d_hasta = resolver_rango_q(tenant_id, desde, hasta)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d_desde, d_hasta)
    return get_ordenes_paginadas(
        tenant_id, desde_ts, hasta_ts,
        pagina=pagina, por_pagina=por_pagina,
        busqueda=busqueda, orden=orden,
    )

@router.patch("/ordenes/{orden_id}")
def editar_orden(orden_id: str, data: ActualizarOrden, tenant_id: str = Depends(get_tenant_id)):
    """
    Edita la FECHA y/o el COBRO de un ticket.
    - fecha: cambia el día contable del ticket y de todos sus renglones.
    - metodo_pago / pagos / propina: edita el pago; el total no se toca
      (la suma de pagos se valida contra total + propina).
    """
    respuesta: dict = {"ok": True}
    if data.fecha:
        respuesta = actualizar_orden(orden_id, data.fecha, tenant_id)
        if not respuesta.get("ok"):
            raise HTTPException(
                status_code=422 if respuesta.get("tipo") == "validacion" else 400,
                detail=respuesta.get("mensaje"),
            )
    if data.metodo_pago or data.pagos or data.propina is not None:
        respuesta = actualizar_pago_orden(
            orden_id,
            data.metodo_pago or "",
            [p.model_dump() for p in data.pagos] if data.pagos else None,
            data.propina,
            tenant_id,
        )
        if not respuesta.get("ok"):
            raise HTTPException(
                status_code=422 if respuesta.get("tipo") == "validacion" else 400,
                detail=respuesta.get("mensaje"),
            )
    if respuesta == {"ok": True}:
        raise HTTPException(status_code=422, detail="Nada que actualizar: envía fecha, metodo_pago, pagos o propina")
    return respuesta

@router.delete("/ordenes/{orden_id}")
def anular_ticket(orden_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Anula un ticket completo: restaura el stock de todos sus renglones."""
    resultado = anular_orden(orden_id, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
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
            "variacion": item.variacion,
            "descripcion": item.descripcion,
            "costo": item.costo,
        }
        for item in carrito.items
    ]

    pago = None
    if carrito.pago is not None:
        pago = {
            "metodo": carrito.pago.metodo,
            "propina": carrito.pago.propina,
            "pagos": (
                [{"metodo": p.metodo, "monto": p.monto, "referencia": p.referencia} for p in carrito.pago.pagos]
                if carrito.pago.pagos is not None else None
            ),
            "monto_recibido": carrito.pago.monto_recibido,
        }

    resultado = cobrar_carrito_atomico(
        items, tenant_id, pago=pago, mesa_id=carrito.mesa_id,
        cliente_id=carrito.cliente_id,
        puntos_usados=carrito.puntos_usados,
        ajuste_puntos=carrito.ajuste_puntos,
        ajuste_concepto=carrito.ajuste_concepto,
    )
    if not resultado.get("ok"):
        # Errores de regla de negocio (ej. compuesto sin receta) → 422;
        # errores internos/db → 500.
        raise HTTPException(
            status_code=422 if resultado.get("tipo") == "validacion" else 500,
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
    cantidad       = data.cantidad       if data.cantidad       is not None else float(v["cantidad"])
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
    """Anula una venta y restaura el stock (transacción única)."""
    resultado = eliminar_venta(venta_id, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("mensaje"))
    return resultado
