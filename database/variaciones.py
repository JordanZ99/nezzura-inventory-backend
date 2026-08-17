# ==============================================================================
# backend/database/variaciones.py
# Variaciones de producto (Fase 2)
# Una variación es una presentación con su PROPIO precio para un mismo
# producto (ej. Sencilla/Doble, S/M/L, Caballero/Dama). Aplica a cualquier
# tipo de producto (stock, servicio o compuesto). En esta fase no consume
# nada extra: solo cambia el precio.
# Extraído de lotes.py — Fase 1/2 del refactor por dominio.
# ==============================================================================

import uuid
import datetime
from psycopg2.errors import UniqueViolation
from database.conexion import query
from database.helpers import _TZ, _resolver_producto_id


def _adjuntar_variaciones(filas: list[dict], tenant_id: str) -> None:
    """
    Adjunta las variaciones de cada producto a una lista de filas que ya
    contienen la llave `producto` (nombre). Muta las filas in-place añadiendo
    el campo `variaciones` = [{id, nombre, precio}, ...].

    Los productos sin variaciones quedan con lista vacía (el frontend lo trata
    como "sin selector").
    """
    if not filas:
        return
    variaciones = query("""
        SELECT p.Producto AS producto, v.id, v.nombre, v.precio, v.foto
        FROM producto_variaciones v
        JOIN productos p ON p.id = v.producto_id
        WHERE p.tenant_id = %s
        ORDER BY p.Producto ASC, v.nombre ASC
    """, (tenant_id,))
    # Suma el stock de los lotes ligados a cada variación (cada variación
    # lleva su propio inventario).
    stocks = query("""
        SELECT l.Producto AS producto, l.variacion_id AS variacion_id,
               SUM(l.Stock_Lote) AS stock
        FROM lotes l
        WHERE l.tenant_id = %s AND l.Estado = 'Activo' AND l.variacion_id IS NOT NULL
        GROUP BY l.Producto, l.variacion_id
    """, (tenant_id,))
    stock_por_var: dict = {}
    for s in stocks:
        stock_por_var[(s["producto"], s["variacion_id"])] = float(s["stock"] or 0)
    por_producto: dict = {}
    for v in variaciones:
        por_producto.setdefault(v["producto"], []).append({
            "id": v["id"],
            "nombre": v["nombre"],
            "precio": float(v["precio"] or 0),
            "foto": v.get("foto") or "",
            "stock": stock_por_var.get((v["producto"], v["id"]), 0),
        })
    for f in filas:
        f["variaciones"] = por_producto.get(f["producto"], [])


def listar_variaciones_producto(producto: str, tenant_id: str) -> list[dict]:
    """Variaciones de un producto concreto (nombre + precio propio + foto)."""
    pid = _resolver_producto_id(producto, tenant_id)
    if not pid:
        return []
    return query("""
        SELECT v.id, v.nombre, v.precio, v.foto,
               COALESCE((SELECT SUM(l.Stock_Lote) FROM lotes l
                         WHERE l.variacion_id = v.id AND l.Estado='Activo'
                           AND l.tenant_id = v.tenant_id), 0) AS stock
        FROM producto_variaciones v
        WHERE v.producto_id = %s AND v.tenant_id = %s
        ORDER BY v.nombre ASC
    """, (pid, tenant_id))


def crear_variacion(producto: str, nombre: str, precio: float, tenant_id: str, foto: str = "", stock_inicial: float | None = None, costo: float | None = None) -> dict:
    """
    Crea una variación nueva para un producto.
    Si ya existe una variación con el mismo nombre, devuelve error (UNIQUE).

    Si stock_inicial > 0 y el producto es de tipo 'stock', crea el LOTE de esa
    variación (costo propio opcional; si se omite usa 0) — misma semántica que
    el ALTA de producto, para que una variación agregada en edición pueda nacer
    con stock en lugar de aparecer "Agotado" sin forma de darle inventario.
    """
    producto = (producto or "").strip()
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "mensaje": "El nombre de la variación es obligatorio"}
    pid = _resolver_producto_id(producto, tenant_id)
    if not pid:
        return {"ok": False, "mensaje": "Producto no encontrado"}
    try:
        result = query("""
            INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id, foto)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, nombre, precio, foto
        """, (pid, nombre, float(precio or 0), tenant_id, foto or ""))
    except UniqueViolation:
        return {"ok": False, "mensaje": "Ya existe una variación con ese nombre"}
    if not result:
        return {"ok": False, "mensaje": "Ya existe una variación con ese nombre"}
    v = result[0]

    # Lote inicial de la variación (espejo del ALTA): solo si trae stock y el
    # producto es de tipo stock.
    stock_final = 0
    if float(stock_inicial or 0) > 0:
        tipo_row = query(
            "SELECT tipo_producto FROM productos WHERE id = %s AND tenant_id = %s",
            (pid, tenant_id))
        es_stock = (tipo_row[0].get("tipo_producto") or "stock") == "stock" if tipo_row else True
        if es_stock:
            stock_final = float(stock_inicial)
            costo_lote = float(costo or 0) or 0
            query("""
                INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta,
                                   Stock_Lote, Fecha_Entrada, Estado, tenant_id, variacion_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s, %s)
            """, (str(uuid.uuid4())[:12], producto, costo_lote, float(precio or 0),
                  stock_final, str(datetime.datetime.now(_TZ)), tenant_id, v["id"]))
    return {"ok": True, "variacion": {"id": v["id"], "nombre": v["nombre"], "precio": float(v["precio"] or 0), "foto": v.get("foto") or "", "stock": stock_final}}


def actualizar_variacion(variacion_id: int, nombre: str, precio: float, tenant_id: str, foto: str | None = None) -> dict:
    """Actualiza nombre y/o precio de una variación (validando pertenencia del tenant).
    foto: None = conservar la actual; '' = quitar la foto."""
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "mensaje": "El nombre de la variación es obligatorio"}
    try:
        result = query("""
            UPDATE producto_variaciones
            SET nombre = %s, precio = %s, foto = COALESCE(%s, foto)
            WHERE id = %s AND tenant_id = %s
            RETURNING id, nombre, precio, foto
        """, (nombre, float(precio or 0), foto, variacion_id, tenant_id))
    except UniqueViolation:
        return {"ok": False, "mensaje": "Ya existe una variación con ese nombre"}
    if not result:
        return {"ok": False, "mensaje": "Variación no encontrada"}
    v = result[0]
    return {"ok": True, "variacion": {"id": v["id"], "nombre": v["nombre"], "precio": float(v["precio"] or 0), "foto": v.get("foto") or ""}}


def eliminar_variacion(variacion_id: int, tenant_id: str) -> dict:
    """Elimina una variación (validando pertenencia del tenant)."""
    result = query("""
        DELETE FROM producto_variaciones
        WHERE id = %s AND tenant_id = %s
        RETURNING id
    """, (variacion_id, tenant_id))
    if not result:
        return {"ok": False, "mensaje": "Variación no encontrada"}
    return {"ok": True, "id": variacion_id}
