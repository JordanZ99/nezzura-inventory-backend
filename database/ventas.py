# Reemplaza todo backend/database/ventas.py por esto:

import psycopg2.extras
from database.conexion import query, execute

def get_ventas(tenant_id: str, limit: int = 500) -> list[dict]:
    """Lee ventas del usuario ordenadas por fecha descendente."""
    return query(
        "SELECT id, fecha, producto, cantidad, precio_lista, precio_real, costo_unitario, total_venta, ganancia_bruta, estado FROM ventas WHERE tenant_id = %s ORDER BY Fecha DESC LIMIT %s",
        (tenant_id, limit)
    )

def insertar_venta(venta: dict, tenant_id: str) -> None:
    """Inserta una venta etiquetada con el tenant_id."""
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

def actualizar_venta(venta_id: int, fecha: str, cantidad: int, precio_real: float, total_venta: float, ganancia_bruta: float, tenant_id: str) -> dict:
    """Modifica una venta asegurando pertenencia del tenant y re-calculando stocks."""
    from database.conexion import get_conn, release_conn
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Bloquear y verificar propiedad de la venta
            cur.execute("SELECT * FROM ventas WHERE id=%s AND tenant_id=%s FOR UPDATE", (venta_id, tenant_id))
            v = cur.fetchone()
            if not v or v.get("estado") == "Inactivo":
                return {"ok": False, "mensaje": "Venta no encontrada"}
                
            vieja_cantidad = int(v["cantidad"])
            dif = cantidad - vieja_cantidad
            
            # 2. Ajustes de inventario según diferencia de cantidad
            # NOTA: Ya no se valida stock insuficiente. Si no hay suficiente,
            # el stock del lote más reciente quedará en negativo (ver descontar_stock_peps).
            if dif > 0:
                cur.execute("SELECT * FROM lotes WHERE Producto=%s AND Estado='Activo' AND tenant_id=%s ORDER BY Fecha_Entrada ASC FOR UPDATE", (v["producto"], tenant_id))
                lotes = [dict(row) for row in cur.fetchall()]
                restante = dif
                for lote in lotes:
                    if restante <= 0: break
                    if int(lote["stock_lote"]) <= 0:
                        continue
                    cons = min(restante, int(lote["stock_lote"]))
                    cur.execute("UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id=%s", (int(lote["stock_lote"]) - cons, lote["id_lote"], tenant_id))
                    restante -= cons
                # Si aún falta, descontamos del lote más reciente (stock negativo)
                if restante > 0 and lotes:
                    ultimo = lotes[-1]
                    cur.execute("UPDATE lotes SET Stock_Lote = Stock_Lote - %s WHERE ID_Lote=%s AND tenant_id=%s", (restante, ultimo["id_lote"], tenant_id))
            elif dif < 0:
                restaurar = abs(dif)
                cur.execute("SELECT id_lote, stock_lote FROM lotes WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id=%s FOR UPDATE LIMIT 1", (v["producto"], v["costo_unitario"], v["precio_lista"], tenant_id))
                lote_exist = cur.fetchone()
                if lote_exist:
                    cur.execute("UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id=%s", (restaurar, lote_exist["id_lote"], tenant_id))
                else:
                    import uuid, datetime
                    id_l = str(uuid.uuid4())[:12]
                    fe = str(datetime.datetime.now())
                    cur.execute("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id) VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)", (id_l, v["producto"], v["costo_unitario"], v["precio_lista"], restaurar, fe, tenant_id))

            # 3. Guardar cambios en la venta
            cur.execute("""
                UPDATE ventas
                SET Fecha=%s, Cantidad=%s, Precio_Real=%s, Total_Venta=%s, Ganancia_Bruta=%s
                WHERE id=%s AND tenant_id=%s
            """, (fecha, cantidad, precio_real, total_venta, ganancia_bruta, venta_id, tenant_id))
            conn.commit()
            return {"ok": True, "id": venta_id}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error: {str(e)}"}
    finally:
        release_conn(conn)

def eliminar_venta(venta_id: int, tenant_id: str) -> dict:
    """Anula venta y devuelve stock al tenant correspondiente."""
    v_rows = query("SELECT * FROM ventas WHERE id=%s AND tenant_id=%s", (venta_id, tenant_id))
    if not v_rows: return {"ok": False, "mensaje": "No encontrada"}
    v = v_rows[0]
    if v.get("estado") == "Inactivo": return {"ok": False, "mensaje": "Ya anulada"}
    
    from database.lotes import agregar_lote
    agregar_lote(producto=v["producto"], descripcion="", costo=v["costo_unitario"], precio_venta=v["precio_lista"], stock=v["cantidad"], tenant_id=tenant_id)
    execute("UPDATE ventas SET Estado='Inactivo' WHERE id=%s AND tenant_id=%s", (venta_id, tenant_id))
    return {"ok": True, "id": venta_id, "stock_restaurado": v["cantidad"]}
