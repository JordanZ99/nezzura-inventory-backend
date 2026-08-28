import psycopg2.extras
import json
import re
import uuid
from datetime import date
from database.conexion import query, execute, get_conn, release_conn
from database.lotes import descontar_stock_peps
from database.helpers import ahora_negocio, _parsear_ts, zona_tenant, hoy_negocio

def get_ventas(tenant_id: str, limit: int = 500) -> list[dict]:
    """Lee ventas del usuario ordenadas por fecha descendente."""
    return query(
        "SELECT id, n_ticket, fecha, producto, cantidad, precio_lista, precio_real, costo_unitario, total_venta, ganancia_bruta, estado, tipo_producto, variacion, consumo, orden_id FROM ventas WHERE tenant_id = %s ORDER BY Fecha DESC LIMIT %s",
        (tenant_id, limit)
    )


def _recalcular_orden(cur, orden_id, tenant_id: str) -> None:
    """
    Recalcula los agregados de una orden desde SUS RENGLONES activos
    (total, ganancia, unidades y estado). Se llama tras toda mutación de
    ventas (editar/anular) para que la cabecera nunca quede desfasada.
    Con orden_id NULL (venta legada sin orden) no hace nada.
    """
    if not orden_id:
        return
    cur.execute(
        """
        UPDATE ordenes o
        SET total = COALESCE(ag.total, 0),
            ganancia = COALESCE(ag.ganancia, 0),
            cantidad_items = COALESCE(ag.unidades, 0),
            estado = CASE WHEN COALESCE(ag.activos, 0) = 0 THEN 'Anulada' ELSE 'Activa' END
        FROM (
            SELECT SUM(total_venta) FILTER (WHERE estado != 'Inactivo') AS total,
                   SUM(ganancia_bruta) FILTER (WHERE estado != 'Inactivo') AS ganancia,
                   SUM(cantidad) FILTER (WHERE estado != 'Inactivo') AS unidades,
                   COUNT(*) FILTER (WHERE estado != 'Inactivo') AS activos
            FROM ventas
            WHERE orden_id = %s AND tenant_id = %s
        ) ag
        WHERE o.id = %s AND o.tenant_id = %s
        """,
        (orden_id, tenant_id, orden_id, tenant_id)
    )


def get_ordenes(tenant_id: str, limit: int = 500) -> list[dict]:
    """
    Órdenes (tickets) con sus renglones anidados, más recientes primero.
    El ordenamiento usa fecha_ts (instante canónico), no el TEXT legado.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, n_ticket, fecha_ts, total, ganancia, cantidad_items, estado, "
                "       metodo_pago, pagos, propina, monto_recibido, cambio, comision_total, turno_id "
                "FROM ordenes WHERE tenant_id = %s "
                "ORDER BY fecha_ts DESC LIMIT %s",
                (tenant_id, limit)
            )
            ordenes = [dict(o) for o in cur.fetchall()]
            if not ordenes:
                return []
            ids = [o["id"] for o in ordenes]
            cur.execute(
                "SELECT id, n_ticket, fecha, producto, cantidad, precio_lista, precio_real, "
                "       costo_unitario, total_venta, ganancia_bruta, estado, tipo_producto, "
                "       variacion, consumo, orden_id "
                "FROM ventas WHERE tenant_id = %s AND orden_id = ANY(%s::uuid[]) "
                "ORDER BY fecha_ts ASC, id ASC",
                (tenant_id, ids)
            )
            renglones = [dict(r) for r in cur.fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)

    por_orden: dict = {}
    for o in ordenes:
        o["ventas"] = []
        # `fecha` = alias de fecha_ts para el contrato del frontend (ISO con zona).
        # fecha_ts se conserva como instante canónico.
        o["fecha"] = o["fecha_ts"]
        por_orden[o["id"]] = o
    for r in renglones:
        o = por_orden.get(r.get("orden_id"))
        if o is not None:
            o["ventas"].append(r)
    return ordenes

def insertar_venta(venta: dict, tenant_id: str, conn=None) -> None:
    """
    Inserta una venta etiquetada con el tenant_id.

    Si se pasa `conn`, la inserción se ejecuta sobre esa conexión (para usarse
    dentro de una transacción atómica junto con el descuento de stock);
    si no se pasa, usa la conexión del pool global.
    """
    p_name = str(venta.get("producto", "")).strip()
    if conn is not None:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s", (p_name, tenant_id))
            producto_row = cur.fetchone()
    else:
        producto_rows = query("SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s", (p_name, tenant_id))
        producto_row = producto_rows[0] if producto_rows else None
    if not producto_row:
        raise ValueError(f"Producto no encontrado: {p_name}")

    # Dual-write (migración 031): fecha TEXT = copia de display legada;
    # fecha_ts = instante canónico TIMESTAMPTZ.
    fecha_ts = _parsear_ts(venta["fecha"])
    sql = """
        INSERT INTO ventas
            (Fecha, fecha_ts, Producto, producto_id, Cantidad, Precio_Lista,
             Precio_Real, Costo_Unitario, Total_Venta, Ganancia_Bruta, Estado, ID_Lote, tenant_id,
             tipo_producto, variacion, consumo, orden_id, n_ticket)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s, %s, %s, %s, %s)
    """
    params = (
        venta["fecha"],
        fecha_ts,
        p_name,
        producto_row["id"],
        venta["cantidad"],
        venta["precio_lista"],
        venta["precio_real"],
        venta["costo_unitario"],
        venta["total_venta"],
        venta["ganancia_bruta"],
        venta.get("id_lote"),
        tenant_id,
        venta.get("tipo_producto", "stock"),
        venta.get("variacion", ""),
        psycopg2.extras.Json(venta.get("consumo") or None),
        venta.get("orden_id"),
        venta.get("n_ticket"),
    )
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    else:
        execute(sql, params)

def _resolver_receta_compuesto(cur, producto: str, variacion: str | None, tenant_id: str) -> list[dict]:
    """
    Resuelve la receta de un compuesto según la VARIACIÓN vendida (Fase 4).

    1. Si la venta lleva variación (ej. "Doble"), busca la receta específica
       de esa variación (producto_recetas.variacion_id = su id).
    2. Si esa variación no tiene receta propia, cae a la receta BASE
       (variacion_id IS NULL).

    Devuelve [{material, cantidad}] por unidad del compuesto.
    """
    # Resolver el id del compuesto
    cur.execute(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    fila_comp = cur.fetchone()
    if not fila_comp:
        return []
    pid = fila_comp["id"]

    # Resolver el id de la variación (si viene)
    variacion_id = None
    if variacion:
        cur.execute(
            "SELECT id FROM producto_variaciones "
            "WHERE producto_id = %s AND nombre = %s AND tenant_id = %s",
            (pid, variacion, tenant_id)
        )
        fila_var = cur.fetchone()
        if fila_var:
            variacion_id = fila_var["id"]

    # 1) Receta específica de la variación
    if variacion_id is not None:
        cur.execute(
            "SELECT m.Producto AS material, r.cantidad "
            "FROM producto_recetas r "
            "JOIN productos m ON m.id = r.material_id "
            "WHERE r.producto_id = %s AND r.tenant_id = %s "
            "AND r.variacion_id = %s "
            "ORDER BY m.Producto ASC",
            (pid, tenant_id, variacion_id)
        )
        receta = [dict(row) for row in cur.fetchall()]
        if receta:
            return receta

    # 2) Fallback: receta base (variacion_id IS NULL)
    cur.execute(
        "SELECT m.Producto AS material, r.cantidad "
        "FROM producto_recetas r "
        "JOIN productos m ON m.id = r.material_id "
        "WHERE r.producto_id = %s AND r.tenant_id = %s "
        "AND r.variacion_id IS NULL "
        "ORDER BY m.Producto ASC",
        (pid, tenant_id)
    )
    return [dict(row) for row in cur.fetchall()]


def _resolver_variacion_venta(cur, v: dict, tenant_id: str) -> int | None:
    """
    Resuelve el id de variación de una venta de STOCK: si la venta trae el
    nombre de una variación que EXISTE para el producto, descuenta de los
    lotes de ESA variación. None = sin variación (stock base del producto).
    """
    variacion = str(v.get("variacion") or "").strip()
    if not variacion:
        return None
    cur.execute(
        "SELECT vv.id AS vid "
        "FROM producto_variaciones vv "
        "JOIN productos p ON p.id = vv.producto_id "
        "WHERE vv.nombre = %s AND p.Producto = %s AND p.tenant_id = %s",
        (variacion, v["producto"], tenant_id)
    )
    fila = cur.fetchone()
    if fila and fila.get("vid"):
        return fila["vid"]
    return None


def _revertir_consumo(cur, consumo: list[dict] | None, tenant_id: str) -> None:
    """
    Devuelve el stock a los lotes EXACTOS que una venta compuesta consumió.
    Usa el cursor `cur` (debe llamarse dentro de una transacción).

    Esto corrige el bug conocido de "restaurar al lote equivocado": en vez de
    buscar un lote por costo+precio (agregar_lote), se suma la cantidad al
    id_lote exacto registrado en el consumo. Si ese lote ya no existe
    (fue eliminado), se crea uno nuevo con el costo de ese consumo.
    """
    if not consumo:
        return
    for c in consumo:
        material = (c.get("material") or "").strip()
        cantidad = float(c.get("cantidad") or 0)
        costo = float(c.get("costo") or 0)
        id_lote = c.get("id_lote")
        if cantidad <= 0 or not material:
            continue
        if id_lote:
            # ¿Existe el lote exacto? (aunque esté Inactivo: si fue eliminado
            # pero sigue en la tabla, igual lo reactivamos sumando stock)
            cur.execute(
                "SELECT 1 FROM lotes WHERE ID_Lote = %s AND tenant_id = %s",
                (id_lote, tenant_id)
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE lotes SET Stock_Lote = Stock_Lote + %s, Estado = 'Activo' "
                    "WHERE ID_Lote = %s AND tenant_id = %s",
                    (cantidad, id_lote, tenant_id)
                )
                continue
        # El lote ya no existe: recrearlo con el costo de ese consumo. Para el
        # Precio_Venta usamos el precio vigente del material (best-effort): si
        # hay otros lotes del mismo producto, tomamos el máximo; si no, cae al
        # costo (no inventar un precio).
        nuevo_id = str(uuid.uuid4())[:12]
        fe = str(ahora_negocio(tenant_id))
        precio_recreado = costo
        try:
            cur.execute(
                "SELECT MAX(Precio_Venta) AS pv FROM lotes "
                "WHERE producto_id = (SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s) AND tenant_id = %s",
                (material, tenant_id, tenant_id)
            )
            fila_pv = cur.fetchone()
            if fila_pv and fila_pv.get("pv"):
                precio_recreado = float(fila_pv["pv"])
        except Exception:
            pass
        cur.execute(
            "INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id) "
            "VALUES (%s, %s, (SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s), %s, %s, %s, %s, %s, 'Activo', %s)",
            (nuevo_id, material, material, tenant_id, costo, precio_recreado, cantidad, fe, _parsear_ts(fe), tenant_id)
        )


def actualizar_venta(venta_id: int, fecha: str, cantidad: float, precio_real: float, costo_unitario: float, total_venta: float, ganancia_bruta: float, tenant_id: str) -> dict:
    """Modifica una venta asegurando pertenencia del tenant y re-calculando stocks."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Bloquear y verificar propiedad de la venta
            cur.execute("SELECT * FROM ventas WHERE id=%s AND tenant_id=%s FOR UPDATE", (venta_id, tenant_id))
            v = cur.fetchone()
            if not v or v.get("estado") == "Inactivo":
                return {"ok": False, "mensaje": "Venta no encontrada"}
                
            vieja_cantidad = float(v["cantidad"])
            dif = cantidad - vieja_cantidad

            # 2. Ajustes de inventario según diferencia de cantidad.
            # Los SERVICIOS no tienen inventario: si la venta fue de un servicio,
            # se salta todo el bloque de stock (no hay lotes que ajustar).
            # NOTA: Ya no se valida stock insuficiente. Si no hay suficiente,
            # el stock del lote más reciente quedará en negativo (ver descontar_stock_peps).
            if v.get("tipo_producto") == "servicio":
                pass
            elif v.get("tipo_producto") == "compuesto":
                # ── Compuesto: revertir el consumo viejo (lotes exactos) y
                # re-descontar los materiales según la NUEVA cantidad. El costo
                # unitario se recalcula EN VIVO desde la receta.

                # 2a. Revertir el consumo registrado en la venta (stock exacto)
                consumo_viejo = v.get("consumo")
                if isinstance(consumo_viejo, str):
                    try:
                        consumo_viejo = json.loads(consumo_viejo)
                    except Exception:
                        consumo_viejo = None
                _revertir_consumo(cur, consumo_viejo, tenant_id)

                # 2b. Resolver la receta del compuesto SEGÚN la variación de la
                #     venta (v["variacion"]) y re-descontar con la nueva cantidad
                receta = _resolver_receta_compuesto(
                    cur, v["producto"], v.get("variacion"), tenant_id
                )
                if not receta:
                    # El compuesto ya no tiene materiales en su receta: 2a ya
                    # devolvió el stock, pero sin receta no hay nada que
                    # re-descontar. Rollback para NO dejar el inventario
                    # inflado de forma silenciosa.
                    conn.rollback()
                    return {
                        "ok": False,
                        "mensaje": "Este compuesto ya no tiene materiales en su receta. "
                                   "Agrega materiales antes de editar la venta."
                    }

                consumo_nuevo: list[dict] = []
                costo_total = 0.0
                for mat in receta:
                    cant_mat = float(mat["cantidad"] or 0) * cantidad
                    if cant_mat <= 0:
                        continue
                    resultado = descontar_stock_peps(
                        mat["material"], cant_mat, 0.0, tenant_id, conn=conn
                    )
                    for r in resultado:
                        c = float(r.get("costo_unitario") or 0)
                        cant_r = float(r.get("cantidad") or 0)
                        consumo_nuevo.append({
                            "material": mat["material"],
                            "id_lote": r.get("id_lote"),
                            "cantidad": cant_r,
                            "costo": c,
                        })
                        costo_total += c * cant_r

                costo_unitario = costo_total / cantidad if cantidad else 0.0
                ganancia_bruta = (precio_real - costo_unitario) * cantidad
                total_venta = precio_real * cantidad

                # 2c. Guardar el nuevo consumo en la venta
                cur.execute(
                    "UPDATE ventas SET Fecha=%s, fecha_ts=%s, Cantidad=%s, Precio_Real=%s, Costo_Unitario=%s, "
                    "Total_Venta=%s, Ganancia_Bruta=%s, consumo=%s "
                    "WHERE id=%s AND tenant_id=%s",
                    (fecha, _parsear_ts(fecha), cantidad, precio_real, costo_unitario, total_venta,
                     ganancia_bruta, psycopg2.extras.Json(consumo_nuevo or None),
                     venta_id, tenant_id)
                )
                # La orden debe reflejar el renglón editado
                _recalcular_orden(cur, v.get("orden_id"), tenant_id)
                conn.commit()
                return {"ok": True, "id": venta_id}
            elif dif > 0:
                # Stock por variación: descuenta solo de los lotes de ESA variación
                vid = _resolver_variacion_venta(cur, v, tenant_id)
                cur.execute("SELECT * FROM lotes WHERE producto_id=%s AND Estado='Activo' AND tenant_id=%s AND variacion_id IS NOT DISTINCT FROM %s ORDER BY Fecha_Entrada ASC FOR UPDATE", (v["producto_id"], tenant_id, vid))
                lotes = [dict(row) for row in cur.fetchall()]
                restante = dif
                for lote in lotes:
                    if restante <= 0: break
                    if float(lote["stock_lote"]) <= 0:
                        continue
                    cons = min(restante, float(lote["stock_lote"]))
                    cur.execute("UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id=%s", (round(float(lote["stock_lote"]) - cons, 3), lote["id_lote"], tenant_id))
                    restante -= cons
                # Si aún falta, descontamos del lote más reciente (stock negativo)
                if restante > 0 and lotes:
                    ultimo = lotes[-1]
                    cur.execute("UPDATE lotes SET Stock_Lote = Stock_Lote - %s WHERE ID_Lote=%s AND tenant_id=%s", (restante, ultimo["id_lote"], tenant_id))
            elif dif < 0:
                restaurar = abs(dif)
                # Stock por variación: restaurar al lote de ESA variación
                vid = _resolver_variacion_venta(cur, v, tenant_id)
                cur.execute("SELECT id_lote, stock_lote FROM lotes WHERE producto_id=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id=%s AND variacion_id IS NOT DISTINCT FROM %s FOR UPDATE LIMIT 1", (v["producto_id"], v["costo_unitario"], v["precio_lista"], tenant_id, vid))
                lote_exist = cur.fetchone()
                if lote_exist:
                    cur.execute("UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id=%s", (restaurar, lote_exist["id_lote"], tenant_id))
                else:
                    id_l = str(uuid.uuid4())[:12]
                    fe = str(ahora_negocio(tenant_id))
                    cur.execute("INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, variacion_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s)", (id_l, v["producto"], v["producto_id"], v["costo_unitario"], v["precio_lista"], restaurar, fe, _parsear_ts(fe), tenant_id, vid))

            # 3. Guardar cambios en la venta
            cur.execute("""
                UPDATE ventas
                SET Fecha=%s, fecha_ts=%s, Cantidad=%s, Precio_Real=%s, Costo_Unitario=%s, Total_Venta=%s, Ganancia_Bruta=%s
                WHERE id=%s AND tenant_id=%s
            """, (fecha, _parsear_ts(fecha), cantidad, precio_real, costo_unitario, total_venta, ganancia_bruta, venta_id, tenant_id))

            # 4. La orden debe reflejar el renglón editado
            _recalcular_orden(cur, v.get("orden_id"), tenant_id)
            conn.commit()
            return {"ok": True, "id": venta_id}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error: {str(e)}"}
    finally:
        release_conn(conn)

def _restaurar_stock_venta(cur, v: dict, tenant_id: str) -> float:
    """
    Devuelve al inventario el stock consumido por UN renglón de venta
    (dentro de una transacción). Compartido por eliminar_venta y anular_orden.

    - Compuesto: revierte su `consumo` (los lotes EXACTOS registrados).
    - Stock: restaura al lote EXACTO que se vendió (ventas.ID_Lote); si ese
      lote ya no existe, se recrea con los datos de la venta. Ventas antiguas
      sin id_lote caen al match por costo+precio.
    - Servicio: sin inventario, no restaura nada.

    Devuelve las unidades restauradas.
    """
    stock_restaurado = 0.0
    tipo = v.get("tipo_producto")

    if tipo == "compuesto":
        consumo = v.get("consumo")
        if isinstance(consumo, str):
            try:
                consumo = json.loads(consumo)
            except Exception:
                consumo = None
        _revertir_consumo(cur, consumo, tenant_id)
        stock_restaurado = float(v["cantidad"] or 0)
    elif tipo != "servicio":
        # ── Producto de stock: restaurar al lote EXACTO de la venta ──
        id_lote = v.get("id_lote")
        cantidad = float(v.get("cantidad") or 0)
        if id_lote and cantidad > 0:
            cur.execute(
                "SELECT 1 FROM lotes WHERE ID_Lote = %s AND tenant_id = %s",
                (id_lote, tenant_id)
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE lotes SET Stock_Lote = Stock_Lote + %s, Estado = 'Activo' "
                    "WHERE ID_Lote = %s AND tenant_id = %s",
                    (cantidad, id_lote, tenant_id)
                )
            else:
                # El lote fue eliminado: recrearlo con costo y precio de
                # la venta original (no con precio = costo).
                fe_recreacion = str(ahora_negocio(tenant_id))
                cur.execute(
                    "INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta, "
                    "Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s)",
                    (id_lote, v["producto"], v["producto_id"], v.get("costo_unitario") or 0,
                     v.get("precio_lista") or 0, cantidad,
                     fe_recreacion, _parsear_ts(fe_recreacion), tenant_id)
                )
            stock_restaurado = cantidad
        else:
            # Venta antigua sin id_lote: fallback al comportamiento previo
            # (sumar al lote que coincida por costo+precio o crear uno
            # nuevo), pero DENTRO de la transacción con el cursor `cur`
            # para no romper la atomicidad (agregar_lote usa el pool con
            # auto-commit aparte y reintroduciría el Bug #3).
            cur.execute(
                "SELECT ID_Lote FROM lotes "
                "WHERE producto_id=%s AND Costo=%s AND Precio_Venta=%s "
                "AND Estado='Activo' AND tenant_id=%s LIMIT 1",
                (v.get("producto_id"), v.get("costo_unitario") or 0,
                 v.get("precio_lista") or 0, tenant_id)
            )
            fila_lote = cur.fetchone()
            if fila_lote:
                cur.execute(
                    "UPDATE lotes SET Stock_Lote = Stock_Lote + %s "
                    "WHERE ID_Lote=%s AND tenant_id=%s",
                    (cantidad, fila_lote["ID_Lote"], tenant_id)
                )
            else:
                nuevo_id = str(uuid.uuid4())[:12]
                fe = str(ahora_negocio(tenant_id))
                cur.execute(
                    "INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta, "
                    "Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s)",
                    (nuevo_id, v["producto"], v.get("producto_id"), v.get("costo_unitario") or 0,
                     v.get("precio_lista") or 0, cantidad, fe, _parsear_ts(fe), tenant_id)
                )
            stock_restaurado = cantidad

    return stock_restaurado


def eliminar_venta(venta_id: int, tenant_id: str) -> dict:
    """
    Anula una venta y restaura el stock en la MISMA transacción (usa el helper
    compartido _restaurar_stock_venta). La orden se recalcula; si era el último
    renglón activo, la orden queda Anulada.
    """
    v_rows = query("SELECT * FROM ventas WHERE id=%s AND tenant_id=%s", (venta_id, tenant_id))
    if not v_rows:
        return {"ok": False, "mensaje": "No encontrada"}
    v = v_rows[0]
    if v.get("estado") == "Inactivo":
        return {"ok": False, "mensaje": "Ya anulada"}

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            stock_restaurado = _restaurar_stock_venta(cur, v, tenant_id)

            # Marcar la venta anulada en la MISMA transacción (antes del commit)
            cur.execute(
                "UPDATE ventas SET Estado='Inactivo' WHERE id=%s AND tenant_id=%s",
                (venta_id, tenant_id)
            )
            # La orden se recalcula: si era el último renglón activo, queda Anulada
            _recalcular_orden(cur, v.get("orden_id"), tenant_id)
        conn.commit()
        return {"ok": True, "id": venta_id, "stock_restaurado": stock_restaurado}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al anular la venta: {str(e)}"}
    finally:
        release_conn(conn)


def anular_orden(orden_id: str, tenant_id: str) -> dict:
    """
    Anula un TICKET completo en UNA transacción: restaura el stock de todos sus
    renglones activos (por lote exacto o revirtiendo el consumo de compuestos),
    los marca Inactivo y deja la orden en estado 'Anulada'.
    Idempotente: anular un ticket ya anulado no hace nada.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, n_ticket, estado FROM ordenes WHERE id=%s::uuid AND tenant_id=%s FOR UPDATE",
                (orden_id, tenant_id)
            )
            orden = cur.fetchone()
            if not orden:
                return {"ok": False, "mensaje": "Ticket no encontrado"}
            if orden.get("estado") == "Anulada":
                return {"ok": True, "anuladas": 0, "n_ticket": orden.get("n_ticket"),
                        "mensaje": "El ticket ya estaba anulado"}

            cur.execute(
                "SELECT * FROM ventas WHERE orden_id=%s::uuid AND tenant_id=%s "
                "AND estado != 'Inactivo' FOR UPDATE",
                (orden_id, tenant_id)
            )
            renglones = cur.fetchall()

            stock_total = 0.0
            for v in renglones:
                stock_total += _restaurar_stock_venta(cur, v, tenant_id)
                cur.execute(
                    "UPDATE ventas SET Estado='Inactivo' WHERE id=%s AND tenant_id=%s",
                    (v["id"], tenant_id)
                )

            # Recalcula agregados (total/ganancia/unidades = 0) y pone estado 'Anulada'
            _recalcular_orden(cur, orden_id, tenant_id)
        conn.commit()
        return {
            "ok": True,
            "anuladas": len(renglones),
            "stock_restaurado": stock_total,
            "n_ticket": orden.get("n_ticket"),
        }
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al anular el ticket: {str(e)}"}
    finally:
        release_conn(conn)


def actualizar_orden(orden_id: str, fecha: str, tenant_id: str) -> dict:
    """
    Edita la FECHA de un ticket: cambia el día contable de la orden y de TODOS
    sus renglones, conservando la hora original. No se permiten editar tickets
    anulados (el stock ya fue devuelto).
    """
    fecha = (fecha or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
        return {"ok": False, "tipo": "validacion", "mensaje": "Fecha inválida (formato YYYY-MM-DD)"}
    try:
        nuevo_dia = date.fromisoformat(fecha)
    except ValueError:
        return {"ok": False, "tipo": "validacion", "mensaje": "Fecha inválida"}

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, n_ticket, fecha_ts, estado FROM ordenes "
                "WHERE id=%s::uuid AND tenant_id=%s FOR UPDATE",
                (orden_id, tenant_id)
            )
            orden = cur.fetchone()
            if not orden:
                return {"ok": False, "mensaje": "Ticket no encontrado"}
            if orden.get("estado") == "Anulada":
                return {"ok": False, "tipo": "validacion",
                        "mensaje": "No se puede editar un ticket anulado"}

            # Conservar la hora original; cambiar solo el día (zona del negocio)
            tz = zona_tenant(tenant_id)
            viejo = _parsear_ts(orden["fecha_ts"]).astimezone(tz)
            nuevo_ts = viejo.replace(year=nuevo_dia.year, month=nuevo_dia.month, day=nuevo_dia.day)

            # Dual-write: fecha TEXT de los renglones + instante canónico
            fecha_str = str(nuevo_ts)
            cur.execute(
                "UPDATE ordenes SET fecha_ts=%s WHERE id=%s::uuid AND tenant_id=%s",
                (nuevo_ts, orden_id, tenant_id)
            )
            cur.execute(
                "UPDATE ventas SET Fecha=%s, fecha_ts=%s "
                "WHERE orden_id=%s::uuid AND tenant_id=%s",
                (fecha_str, nuevo_ts, orden_id, tenant_id)
            )
        conn.commit()
        return {"ok": True, "orden_id": str(orden_id), "n_ticket": orden.get("n_ticket"), "fecha": fecha}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al editar el ticket: {str(e)}"}
    finally:
        release_conn(conn)


# Métodos de pago (Fase A de cobro). En las órdenes 'mixto' también existe;
# en cada pago individual solo los métodos reales.
METODOS_PAGO = ("efectivo", "tarjeta_debito", "tarjeta_credito")
METODOS_ORDEN = METODOS_PAGO + ("mixto",)


def _procesar_pago(pago: dict | None, total_venta: float, terminales_map: dict | None = None) -> dict:
    """
    Valida y normaliza el pago de un carrito; devuelve las columnas para ordenes.

    Reglas:
      - Sin pago (API legada) → metodo_pago/pagos NULL, propina 0.
      - total_a_pagar = total_venta (productos) + propina.
      - Método simple: se genera UN pago por el total_a_pagar; en efectivo
        monto_recibido >= total_a_pagar y cambio = recibido - total_a_pagar.
      - Mixto: lista de pagos reales cuya suma debe cuadrar (±1 centavo).
      - Pagos con tarjeta pueden llevar terminal_id: la comisión se calcula con
        la tarifa de ESA terminal (pct débito/crédito + cuota fija) y queda
        dentro de cada pago + comision_total de la orden (Fase B).
    Errores de regla de negocio → ValueError (el router responde 422).
    """
    if not pago:
        return {"metodo_pago": None, "pagos": None, "propina": 0.0,
                "monto_recibido": None, "cambio": None, "comision_total": 0.0}

    metodo = str(pago.get("metodo") or "").strip()
    if metodo not in METODOS_ORDEN:
        raise ValueError(f"Método de pago inválido: '{metodo}'")

    propina = round(float(pago.get("propina") or 0), 2)
    if propina < 0:
        raise ValueError("La propina no puede ser negativa")
    total_a_pagar = round(total_venta + propina, 2)
    recibido = None
    cambio = None

    if metodo == "mixto":
        pagos_in = pago.get("pagos") or []
        if len(pagos_in) < 2:
            raise ValueError("El pago mixto requiere al menos dos pagos")
        pagos = []
        for p in pagos_in:
            m = str(p.get("metodo") or "").strip()
            if m not in METODOS_PAGO:
                raise ValueError(f"Método de pago inválido: '{m}'")
            monto = round(float(p.get("monto") or 0), 2)
            if monto <= 0:
                raise ValueError("Los montos de cada pago deben ser mayores a 0")
            pagos.append({"metodo": m, "monto": monto,
                          "referencia": (str(p.get("referencia")) or "").strip() or None,
                          "terminal_id": p.get("terminal_id") or None})
        if abs(sum(p["monto"] for p in pagos) - total_a_pagar) > 0.01:
            raise ValueError(
                f"La suma de los pagos (${sum(p['monto'] for p in pagos):.2f}) "
                f"no coincide con el total a pagar (${total_a_pagar:.2f})"
            )
    else:
        if metodo == "efectivo":
            recibido_raw = pago.get("monto_recibido")
            recibido = round(float(recibido_raw), 2) if recibido_raw is not None else total_a_pagar
            if recibido < total_a_pagar:
                raise ValueError(
                    f"El monto recibido (${recibido:.2f}) es menor al total a pagar (${total_a_pagar:.2f})"
                )
            cambio = round(recibido - total_a_pagar, 2)
        pagos = [{"metodo": metodo, "monto": total_a_pagar, "referencia":
                  (str(pago.get("referencia")) or "").strip() or None,
                  "terminal_id": pago.get("terminal_id") or None}]

    # ── Comisiones de terminal (Fase B) ──
    comision_total = 0.0
    for p in pagos:
        tid = p.get("terminal_id")
        t = terminales_map.get(tid) if (tid and terminales_map) else None
        if p["metodo"] in ("tarjeta_debito", "tarjeta_credito") and t:
            pct = t["comision_debito_pct"] if p["metodo"] == "tarjeta_debito" else t["comision_credito_pct"]
            comision = round(p["monto"] * float(pct) / 100.0 + float(t["comision_fija"] or 0), 2)
            p["comision"] = comision
            p["terminal_nombre"] = t["nombre"]
            comision_total += comision
        else:
            p["comision"] = 0.0

    return {"metodo_pago": metodo, "pagos": pagos, "propina": propina,
            "monto_recibido": recibido, "cambio": cambio,
            "comision_total": round(comision_total, 2)}


def cobrar_carrito(items: list[dict], tenant_id: str, pago: dict | None = None) -> dict:
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
    conn = get_conn()
    try:
        ventas_a_guardar = []
        for item in items:
            # Consultar el tipo del producto DENTRO de la transacción, para
            # decidir si se descuenta inventario o es un servicio (sin stock).
            tipo = "stock"
            costo_srv = 0.0
            precio_srv = 0.0
            fraccionable = False
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT tipo_producto, costo_servicio, precio_servicio, fraccionable "
                    "FROM productos WHERE Producto = %s AND tenant_id = %s",
                    (item["producto"], tenant_id)
                )
                fila = cur.fetchone()
                if fila:
                    tipo = fila.get("tipo_producto") or "stock"
                    costo_srv = float(fila.get("costo_servicio") or 0)
                    precio_srv = float(fila.get("precio_servicio") or 0)
                    fraccionable = bool(fila.get("fraccionable"))

            variacion = str(item.get("variacion") or "").strip()

            # Validación de cantidad fraccionaria: si el producto NO está
            # marcado como fraccionable (por defecto), solo se vende por
            # unidades enteras. Así un llavero nunca se vende a 0.5, aunque se
            # intente por API (protección real, no solo UX).
            cantidad_item = float(item["cantidad"])
            if not fraccionable and cantidad_item != int(cantidad_item):
                raise ValueError(
                    f"'{item['producto']}' solo se vende por unidades enteras. "
                    "Corrige la cantidad e inténtalo de nuevo."
                )

            if tipo == "compuesto":
                # ── Compuesto: consume stock de sus MATERIALES (receta BOM) ──
                # 1. Resolver la receta SEGÚN LA VARIACIÓN vendida (si la variación
                #    no tiene receta propia, cae a la receta base).
                # 2. Por cada material → descontar PEPS y capturar qué lote/costo
                #    salió (descontar_stock_peps devuelve los registros de venta,
                #    aquí NO se insertan como ventas: solo descuentan lotes).
                # 3. costo_unitario = Σ(costo×cantidad) / unidades (costo EN VIVO).
                # 4. Insertar UNA sola venta del compuesto + consumo JSON.
                cant_comp = float(item["cantidad"])
                precio_real = float(item["precio_real"])
                consumo: list[dict] = []
                costo_total = 0.0
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    receta = _resolver_receta_compuesto(
                        cur, item["producto"], item.get("variacion"), tenant_id
                    )

                if not receta:
                    # Compuesto sin receta: no se puede cobrar (vendería sin
                    # consumir nada y con costo 0). Frena TODO el carrito.
                    raise ValueError(
                        f"El compuesto '{item['producto']}' no tiene materiales en su receta. "
                        "Agrega materiales antes de cobrarlo."
                    )

                for mat in receta:
                    nombre_mat = mat["material"]
                    # Cantidad de material necesaria: receta × unidades vendidas
                    cant_mat = float(mat["cantidad"] or 0) * cant_comp
                    if cant_mat <= 0:
                        continue
                    # precio_real=0: solo nos interesa descontar lotes y saber cuál
                    # salió; la venta del compuesto se registra aparte con su precio.
                    resultado = descontar_stock_peps(
                        nombre_mat, cant_mat, 0.0, tenant_id, conn=conn
                    )
                    for r in resultado:
                        c = float(r.get("costo_unitario") or 0)
                        cant_r = float(r.get("cantidad") or 0)
                        consumo.append({
                            "material": nombre_mat,
                            "id_lote": r.get("id_lote"),
                            "cantidad": cant_r,
                            "costo": c,
                        })
                        costo_total += c * cant_r

                costo_unitario = costo_total / cant_comp if cant_comp else 0.0
                ventas_a_guardar.append({
                    "fecha"         : str(ahora_negocio(tenant_id)),
                    "producto"      : item["producto"],
                    "cantidad"      : cant_comp,
                    "precio_lista"  : precio_srv,      # precio de venta del compuesto
                    "precio_real"   : precio_real,
                    "costo_unitario": costo_unitario,
                    "total_venta"   : precio_real * cant_comp,
                    "ganancia_bruta": (precio_real - costo_unitario) * cant_comp,
                    "id_lote"       : None,
                    "tipo_producto" : "compuesto",
                    "variacion"     : variacion,
                    "consumo"       : consumo,
                })
                continue

            if tipo == "servicio":
                # ── Servicio: no tiene inventario ──
                # Se vende infinito; se registra la venta con el costo/precio del
                # servicio. Nada se descuenta de lotes.
                fecha = str(ahora_negocio(tenant_id))
                cant = float(item["cantidad"])
                precio_real = float(item["precio_real"])
                ventas_a_guardar.append({
                    "fecha"         : fecha,
                    "producto"      : item["producto"],
                    "cantidad"      : cant,
                    "precio_lista"  : precio_srv,
                    "precio_real"   : precio_real,
                    "costo_unitario": costo_srv,
                    "total_venta"   : precio_real * cant,
                    "ganancia_bruta": (precio_real - costo_srv) * cant,
                    "id_lote"       : None,
                    "tipo_producto" : "servicio",
                    "variacion"     : variacion,
                })
                continue

            # ── Producto con stock: PEPS normal ──
            # ¿Stock por variación? Si el producto activó el flag, el descuento
            # va SOLO a los lotes de ESA variación (Fase 6).
            variacion_id = None
            if variacion:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    variacion_id = _resolver_variacion_venta(
                        cur, {"producto": item["producto"], "variacion": variacion}, tenant_id
                    )
            resultado = descontar_stock_peps(
                item["producto"],
                item["cantidad"],
                item["precio_real"],
                tenant_id,
                id_lote=item.get("id_lote"),
                variacion_id=variacion_id,
                conn=conn,
            )
            for v in resultado:
                v["tipo_producto"] = "stock"
                v["variacion"] = variacion
            ventas_a_guardar.extend(resultado)

        # ── Cabecera de la ORDEN (migración 032): un cobro = un ticket ──
        # El folio lo asigna el trigger trigger_folio_ordenes (advisory lock por
        # tenant, concurrente-seguro). Los agregados salen de los renglones.
        orden_id = None
        n_ticket = None
        if ventas_a_guardar:
            fecha_orden = ventas_a_guardar[0]["fecha"]
            # Pago (Fase A/B): método, propina, mixto, cambio, comisiones de terminal.
            total_productos = sum(float(v["total_venta"] or 0) for v in ventas_a_guardar)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, nombre, comision_debito_pct, comision_credito_pct, comision_fija "
                    "FROM terminales WHERE tenant_id = %s AND activo = true", (tenant_id,)
                )
                terminales_map = {str(t["id"]): t for t in cur.fetchall()}
            pago_cols = _procesar_pago(pago, total_productos, terminales_map)

            # Turno abierto (Fase C): si existe, el ticket se adscribe a él
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id FROM turnos WHERE tenant_id = %s AND estado = 'Abierto' "
                    "ORDER BY abierta_en DESC LIMIT 1", (tenant_id,)
                )
                fila_turno = cur.fetchone()
                turno_id = fila_turno["id"] if fila_turno else None

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO ordenes (tenant_id, n_ticket, fecha_ts, total, ganancia, cantidad_items, estado, "
                    "metodo_pago, pagos, propina, monto_recibido, cambio, comision_total, turno_id) "
                    "VALUES (%s, NULL, %s, %s, %s, %s, 'Activa', %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id, n_ticket",
                    (
                        tenant_id,
                        _parsear_ts(fecha_orden),
                        total_productos,
                        sum(float(v["ganancia_bruta"] or 0) for v in ventas_a_guardar),
                        sum(float(v["cantidad"] or 0) for v in ventas_a_guardar),
                        pago_cols["metodo_pago"],
                        psycopg2.extras.Json(pago_cols["pagos"]),
                        pago_cols["propina"],
                        pago_cols["monto_recibido"],
                        pago_cols["cambio"],
                        pago_cols["comision_total"],
                        turno_id,
                    )
                )
                fila_orden = cur.fetchone()
                orden_id = fila_orden["id"]
                n_ticket = fila_orden["n_ticket"]

            # Gasto automático de comisiones (si el tenant lo activó)
            if pago_cols["comision_total"] > 0:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT gasto_comision_automatico FROM tenants WHERE id = %s", (tenant_id,)
                    )
                    fila_flag = cur.fetchone()
                if fila_flag and fila_flag["gasto_comision_automatico"]:
                    with conn.cursor() as cur:
                        fecha_hoy = hoy_negocio(tenant_id).isoformat()
                        cur.execute(
                            "INSERT INTO gastos (Fecha, fecha_negocio, Categoria, Descripcion, Monto, Tenant_ID, Estado) "
                            "VALUES (%s, %s, %s, %s, %s, %s, 'pagado')",
                            (fecha_hoy, fecha_hoy, "Comisiones bancarias",
                             f"Comisión de terminal — ticket #{n_ticket}",
                             pago_cols["comision_total"], tenant_id)
                        )

        for venta in ventas_a_guardar:
            venta["orden_id"] = orden_id
            venta["n_ticket"] = n_ticket
            insertar_venta(venta, tenant_id, conn=conn)

        total = sum(v["total_venta"] for v in ventas_a_guardar)

        # Commit AL FINAL: así nada puede fallar después del commit y provocar
        # un falso error con la venta ya guardada (evita cobros duplicados).
        conn.commit()

        return {
            "ok": True,
            "ventas": len(ventas_a_guardar),
            "total_cobrado": total,
            "orden_id": str(orden_id) if orden_id else None,
            "n_ticket": n_ticket,
            "metodo_pago": pago_cols["metodo_pago"] if ventas_a_guardar else None,
            "propina": pago_cols["propina"] if ventas_a_guardar else 0,
            "cambio": pago_cols["cambio"] if ventas_a_guardar else None,
        }
    except ValueError as e:
        # Error de REGLA DE NEGOCIO (ej. compuesto sin receta): se distingue de
        # los errores internos para que el router responda 422 y no 500.
        conn.rollback()
        return {"ok": False, "tipo": "validacion", "mensaje": str(e)}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al cobrar el carrito: {str(e)}"}
    finally:
        release_conn(conn)
