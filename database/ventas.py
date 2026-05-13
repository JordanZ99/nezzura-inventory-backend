import psycopg2.extras
from database.conexion import query, execute, DEFAULT_TENANT_ID


from database.conexion import query, execute, DEFAULT_TENANT_ID


def get_ventas(limit: int = 500, tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """Lee ventas ordenadas por fecha descendente y filtradas por tenant."""
    return query(
        "SELECT id, fecha, producto, cantidad, precio_lista, precio_real, costo_unitario, total_venta, ganancia_bruta, estado FROM ventas WHERE tenant_id = %s ORDER BY Fecha DESC LIMIT %s",
        (tenant_id, limit)
    )


def insertar_venta(venta: dict, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    """Inserta una fila en la tabla ventas vinculada a un tenant."""
    p_name = str(venta.get("producto", "")).strip()
    execute("""
        INSERT INTO ventas
            (Fecha, Producto, Cantidad, Precio_Lista,
             Precio_Real, Costo_Unitario, Total_Venta, Ganancia_Bruta, Estado, ID_Lote, tenant_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s)
    """, (
        venta["fecha"],
        p_name,
        venta["cantidad"],
        venta["precio_lista"],
        venta["precio_real"],
        venta["costo_unitario"],
        venta["total_venta"],
        venta["ganancia_bruta"],
        venta.get("id_lote"),
        tenant_id
    ))


def actualizar_venta(
    venta_id: int,
    fecha: str,
    cantidad: int,
    precio_real: float,
    total_venta: float,
    ganancia_bruta: float,
    tenant_id: str = DEFAULT_TENANT_ID
) -> dict:
    """Corrige fecha, cantidad y precio de una venta existente ajustando el stock."""
    from database.conexion import get_conn, release_conn
    from database.lotes import agregar_lote

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Bloquear la fila filtrando por tenant_id
            cur.execute("SELECT * FROM ventas WHERE id=%s AND tenant_id=%s FOR UPDATE", (venta_id, tenant_id))
            v = cur.fetchone()
            
            if not v or v.get("estado") == "Inactivo":
                return {"ok": False, "mensaje": "Venta no encontrada o anulada"}
                
            vieja_cantidad = int(v["cantidad"])
            dif = cantidad - vieja_cantidad
            
            # 2. Ajustar inventario si la cantidad cambió
            if dif > 0:
                # Aumentó la cantidad vendida, descontar de inventario
                cur.execute("""
                    SELECT * FROM lotes
                    WHERE Producto=%s AND Stock_Lote > 0 AND Estado='Activo' AND tenant_id=%s
                    ORDER BY Fecha_Entrada ASC
                    FOR UPDATE
                """, (v["producto"], tenant_id))
                lotes = [dict(row) for row in cur.fetchall()]
                
                stock_disponible = sum(int(l["stock_lote"]) for l in lotes)
                if stock_disponible < dif:
                    conn.rollback()
                    return {"ok": False, "mensaje": f"Stock insuficiente para editar la venta. Faltan {dif - stock_disponible} unidades de '{v['producto']}'."}
                    
                restante = dif
                for lote in lotes:
                    if restante <= 0: break
                    consumir = min(restante, int(lote["stock_lote"]))
                    nuevo_stock = int(lote["stock_lote"]) - consumir
                    cur.execute("UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id=%s", (nuevo_stock, lote["id_lote"], tenant_id))
                    restante -= consumir
                    
            elif dif < 0:
                # Disminuyó la cantidad vendida, restaurar al inventario
                cant_a_restaurar = abs(dif)
                # Buscamos lote existente en esta misma transacción
                cur.execute("""
                    SELECT id_lote, stock_lote FROM lotes
                    WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id=%s
                    FOR UPDATE LIMIT 1
                """, (v["producto"], v["costo_unitario"], v["precio_lista"], tenant_id))
                lote_existente = cur.fetchone()

                if lote_existente:
                    cur.execute(
                        "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id=%s",
                        (cant_a_restaurar, lote_existente["id_lote"], tenant_id)
                    )
                else:
                    import uuid
                    import datetime
                    id_lote = str(uuid.uuid4())[:12]
                    fecha_lote = str(datetime.datetime.now())
                    cur.execute("""
                        INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta,
                                           Stock_Lote, Fecha_Entrada, Estado, tenant_id)
                        VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)
                    """, (id_lote, v["producto"], v["costo_unitario"], v["precio_lista"], cant_a_restaurar, fecha_lote, tenant_id))

            # 3. Guardar los cambios en la venta
            cur.execute("""
                UPDATE ventas
                SET Fecha=%s, Cantidad=%s, Precio_Real=%s, Total_Venta=%s, Ganancia_Bruta=%s
                WHERE id=%s AND tenant_id=%s
            """, (fecha, cantidad, precio_real, total_venta, ganancia_bruta, venta_id, tenant_id))
            
            conn.commit()
            return {"ok": True, "id": venta_id}
            
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error interno: {str(e)}"}
    finally:
        release_conn(conn)


def eliminar_venta(venta_id: int, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """Anula una venta por id y restaura el stock al inventario sin borrar el registro."""
    # 1. Leer los datos de la venta
    venta_rows = query("SELECT * FROM ventas WHERE id=%s AND tenant_id=%s", (venta_id, tenant_id))
    if not venta_rows:
        return {"ok": False, "mensaje": "Venta no encontrada"}
    
    v = venta_rows[0]
    if v.get("estado") == "Inactivo":
        return {"ok": False, "mensaje": "La venta ya está anulada"}

    # 2. Restaurar el stock
    # Importamos aquí para evitar referencias circulares
    from database.lotes import agregar_lote
    
    agregar_lote(
        producto=v["producto"],
        descripcion="", 
        costo=v["costo_unitario"],
        precio_venta=v["precio_lista"],
        stock=v["cantidad"],
        tenant_id=tenant_id
    )

    # 3. Anular el registro en lugar de eliminarlo
    execute("UPDATE ventas SET Estado='Inactivo' WHERE id=%s AND tenant_id=%s", (venta_id, tenant_id))
    return {"ok": True, "id": venta_id, "stock_restaurado": v["cantidad"]}