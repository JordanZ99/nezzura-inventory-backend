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
    fecha: str,
    cantidad: int,
    precio_real: float,
    total_venta: float,
    ganancia_bruta: float
) -> dict:
    """Corrige fecha, cantidad y precio de una venta existente ajustando el stock."""
    # 1. Leer los datos de la venta actual
    venta_rows = query("SELECT * FROM ventas WHERE id=%s", (venta_id,))
    if not venta_rows:
        return {"ok": False, "mensaje": "Venta no encontrada"}
        
    v = venta_rows[0]
    vieja_cantidad = int(v["cantidad"])
    dif = cantidad - vieja_cantidad
    
    # 2. Ajustar inventario si la cantidad cambió
    if dif > 0:
        # Aumentó la cantidad vendida, descontar de inventario
        lotes = query("""
            SELECT * FROM lotes
            WHERE Producto=%s AND Stock_Lote > 0 AND Estado='Activo'
            ORDER BY Fecha_Entrada ASC
        """, (v["producto"],))
        
        stock_disponible = sum(int(l["stock_lote"]) for l in lotes)
        if stock_disponible < dif:
            return {"ok": False, "mensaje": f"Stock insuficiente para editar la venta. Faltan {dif - stock_disponible} unidades de '{v['producto']}'."}
            
        restante = dif
        for lote in lotes:
            if restante <= 0: break
            consumir = min(restante, int(lote["stock_lote"]))
            nuevo_stock = int(lote["stock_lote"]) - consumir
            execute("UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s", (nuevo_stock, lote["id_lote"]))
            restante -= consumir
            
    elif dif < 0:
        # Disminuyó la cantidad vendida, restaurar al inventario
        from database.lotes import agregar_lote
        agregar_lote(
            producto=v["producto"],
            descripcion="", 
            costo=v["costo_unitario"],
            precio_venta=v["precio_lista"],
            stock=abs(dif)
        )

    # 3. Guardar los cambios
    execute("""
        UPDATE ventas
        SET Fecha=%s, Cantidad=%s, Precio_Real=%s, Total_Venta=%s, Ganancia_Bruta=%s
        WHERE id=%s
    """, (fecha, cantidad, precio_real, total_venta, ganancia_bruta, venta_id))
    
    return {"ok": True, "id": venta_id}


def eliminar_venta(venta_id: int) -> dict:
    """Elimina una venta por id y restaura el stock al inventario."""
    # 1. Leer los datos de la venta
    venta_rows = query("SELECT * FROM ventas WHERE id=%s", (venta_id,))
    if not venta_rows:
        return {"ok": False, "mensaje": "Venta no encontrada"}
    
    v = venta_rows[0]

    # 2. Restaurar el stock
    # Importamos aquí para evitar referencias circulares
    from database.lotes import agregar_lote
    
    agregar_lote(
        producto=v["producto"],
        descripcion="", 
        costo=v["costo_unitario"],
        precio_venta=v["precio_lista"],
        stock=v["cantidad"]
    )

    # 3. Eliminar el registro
    execute("DELETE FROM ventas WHERE id=%s", (venta_id,))
    return {"ok": True, "id": venta_id, "stock_restaurado": v["cantidad"]}