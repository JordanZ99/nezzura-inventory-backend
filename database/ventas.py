# ==============================================================================
# backend/database/ventas.py
# CRUD sobre la tabla ventas.
# ==============================================================================

from database.conexion import query, execute


def get_ventas(limit: int = 500) -> list[dict]:
    """Lee ventas ordenadas por fecha descendente."""
    return query(
        "SELECT * FROM ventas ORDER BY Fecha DESC LIMIT %s",
        (limit,)
    )


def insertar_venta(venta: dict) -> None:
    """Inserta una fila en la tabla ventas."""
    execute("""
        INSERT INTO ventas
            (Fecha, Producto, Cantidad, Precio_Lista,
             Precio_Real, Costo_Unitario, Total_Venta, Ganancia_Bruta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        venta["fecha"],
        venta["producto"],
        venta["cantidad"],
        venta["precio_lista"],
        venta["precio_real"],
        venta["costo_unitario"],
        venta["total_venta"],
        venta["ganancia_bruta"],
    ))


def actualizar_venta(
    venta_id: int,
    cantidad: int,
    precio_real: float,
    total_venta: float,
    ganancia_bruta: float
) -> dict:
    """Corrige cantidad y precio de una venta existente."""
    execute("""
        UPDATE ventas
        SET Cantidad=%s, Precio_Real=%s, Total_Venta=%s, Ganancia_Bruta=%s
        WHERE id=%s
    """, (cantidad, precio_real, total_venta, ganancia_bruta, venta_id))
    return {"ok": True, "id": venta_id}


def eliminar_venta(venta_id: int) -> dict:
    """Elimina una venta por id."""
    execute("DELETE FROM ventas WHERE id=%s", (venta_id,))
    return {"ok": True, "id": venta_id}