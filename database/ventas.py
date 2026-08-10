# Reemplaza todo backend/database/ventas.py por esto:

import psycopg2.extras
from zoneinfo import ZoneInfo
from database.conexion import query, execute


# ── Zona horaria del negocio (Cancún, UTC-5) ──
_TZ = ZoneInfo("America/Cancun")

def get_ventas(tenant_id: str, limit: int = 500) -> list[dict]:
    """Lee ventas del usuario ordenadas por fecha descendente."""
    return query(
        "SELECT id, n_ticket, fecha, producto, cantidad, precio_lista, precio_real, costo_unitario, total_venta, ganancia_bruta, estado FROM ventas WHERE tenant_id = %s ORDER BY Fecha DESC LIMIT %s",
        (tenant_id, limit)
    )

def insertar_venta(venta: dict, tenant_id: str, conn=None) -> None:
    """
    Inserta una venta etiquetada con el tenant_id.

    Si se pasa `conn`, la inserción se ejecuta sobre esa conexión (para usarse
    dentro de una transacción atómica junto con el descuento de stock);
    si no se pasa, usa la conexión del pool global.
    """
    p_name = str(venta.get("producto", "")).strip()
    sql = """
        INSERT INTO ventas
            (Fecha, Producto, Cantidad, Precio_Lista,
             Precio_Real, Costo_Unitario, Total_Venta, Ganancia_Bruta, Estado, ID_Lote, tenant_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s)
    """
    params = (
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
    )
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    else:
        execute(sql, params)

def actualizar_venta(venta_id: int, fecha: str, cantidad: int, precio_real: float, costo_unitario: float, total_venta: float, ganancia_bruta: float, tenant_id: str) -> dict:
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
                    fe = str(datetime.datetime.now(_TZ))
                    cur.execute("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id) VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)", (id_l, v["producto"], v["costo_unitario"], v["precio_lista"], restaurar, fe, tenant_id))

            # 3. Guardar cambios en la venta
            cur.execute("""
                UPDATE ventas
                SET Fecha=%s, Cantidad=%s, Precio_Real=%s, Costo_Unitario=%s, Total_Venta=%s, Ganancia_Bruta=%s
                WHERE id=%s AND tenant_id=%s
            """, (fecha, cantidad, precio_real, costo_unitario, total_venta, ganancia_bruta, venta_id, tenant_id))
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


def cobrar_carrito(items: list[dict], tenant_id: str) -> dict:
    """
    Cobra un carrito completo de forma ATÓMICA (todo en una sola transacción).

    - Descuenta el stock de cada item (PEPS, con bloqueo FOR UPDATE de los
      lotes) y registra las ventas generadas en la MISMA transacción.
    - Si cualquier paso falla, se hace rollback: no puede quedar stock
      descontado sin venta registrada, ni venta registrada sin stock
      descontado.
    - Antes esto se hacía en dos fases con commits separados (primero descontar
      stock y luego insertar ventas), lo que podía dejar el inventario corrupto
      si algo fallaba a mitad del proceso.

    Args:
        items: lista de dicts con las llaves producto, cantidad, precio_real
               e id_lote (opcional).
        tenant_id: UUID del tenant.

    Returns:
        dict con ok, ventas (cantidad de registros) y total_cobrado.
        Si falla, ok=False con un mensaje descriptivo.
    """
    from database.lotes import descontar_stock_peps
    from database.conexion import get_conn, release_conn

    conn = get_conn()
    try:
        ventas_a_guardar = []
        for item in items:
            resultado = descontar_stock_peps(
                item["producto"],
                item["cantidad"],
                item["precio_real"],
                tenant_id,
                id_lote=item.get("id_lote"),
                conn=conn,
            )
            ventas_a_guardar.extend(resultado)

        for venta in ventas_a_guardar:
            insertar_venta(venta, tenant_id, conn=conn)

        total = sum(v["total_venta"] for v in ventas_a_guardar)

        # Commit AL FINAL: así nada puede fallar después del commit y provocar
        # un falso error con la venta ya guardada (evita cobros duplicados).
        conn.commit()

        return {
            "ok": True,
            "ventas": len(ventas_a_guardar),
            "total_cobrado": total,
        }
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al cobrar el carrito: {str(e)}"}
    finally:
        release_conn(conn)
