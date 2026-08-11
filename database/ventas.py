import psycopg2.extras
import datetime
import json
import uuid
from zoneinfo import ZoneInfo
from database.conexion import query, execute, get_conn, release_conn
from database.lotes import descontar_stock_peps


# ── Zona horaria del negocio (Cancún, UTC-5) ──
_TZ = ZoneInfo("America/Cancun")

def get_ventas(tenant_id: str, limit: int = 500) -> list[dict]:
    """Lee ventas del usuario ordenadas por fecha descendente."""
    return query(
        "SELECT id, n_ticket, fecha, producto, cantidad, precio_lista, precio_real, costo_unitario, total_venta, ganancia_bruta, estado, tipo_producto, variacion, consumo FROM ventas WHERE tenant_id = %s ORDER BY Fecha DESC LIMIT %s",
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
             Precio_Real, Costo_Unitario, Total_Venta, Ganancia_Bruta, Estado, ID_Lote, tenant_id,
             tipo_producto, variacion, consumo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s, %s, %s)
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
        tenant_id,
        venta.get("tipo_producto", "stock"),
        venta.get("variacion", ""),
        psycopg2.extras.Json(venta.get("consumo") or None)
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
        fe = str(datetime.datetime.now(_TZ))
        precio_recreado = costo
        try:
            cur.execute(
                "SELECT MAX(Precio_Venta) AS pv FROM lotes "
                "WHERE Producto = %s AND tenant_id = %s",
                (material, tenant_id)
            )
            fila_pv = cur.fetchone()
            if fila_pv and fila_pv.get("pv"):
                precio_recreado = float(fila_pv["pv"])
        except Exception:
            pass
        cur.execute(
            "INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)",
            (nuevo_id, material, costo, precio_recreado, cantidad, fe, tenant_id)
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
                    "UPDATE ventas SET Fecha=%s, Cantidad=%s, Precio_Real=%s, Costo_Unitario=%s, "
                    "Total_Venta=%s, Ganancia_Bruta=%s, consumo=%s "
                    "WHERE id=%s AND tenant_id=%s",
                    (fecha, cantidad, precio_real, costo_unitario, total_venta,
                     ganancia_bruta, psycopg2.extras.Json(consumo_nuevo or None),
                     venta_id, tenant_id)
                )
                conn.commit()
                return {"ok": True, "id": venta_id}
            elif dif > 0:
                cur.execute("SELECT * FROM lotes WHERE Producto=%s AND Estado='Activo' AND tenant_id=%s ORDER BY Fecha_Entrada ASC FOR UPDATE", (v["producto"], tenant_id))
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
                cur.execute("SELECT id_lote, stock_lote FROM lotes WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id=%s FOR UPDATE LIMIT 1", (v["producto"], v["costo_unitario"], v["precio_lista"], tenant_id))
                lote_exist = cur.fetchone()
                if lote_exist:
                    cur.execute("UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id=%s", (restaurar, lote_exist["id_lote"], tenant_id))
                else:
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
    """
    Anula una venta y restaura el stock en la MISMA transacción:
    - Compuesto: revierte su `consumo` (los lotes EXACTOS registrados).
    - Stock: restaura al lote EXACTO que se vendió (ventas.ID_Lote), en vez de
      buscar por costo+precio (corrige el bug de "restaurar al lote
      equivocado" cuando había varios lotes con el mismo costo/precio).
      Si ese lote ya no existe, se recrea con los datos de la venta.
    - Servicio: sin inventario, solo se anula.

    Todo ocurre en un solo commit: nunca puede quedar el stock restaurado sin
    la venta anulada (ni al revés), aunque el proceso muera a mitad.
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
            tipo = v.get("tipo_producto")
            stock_restaurado = 0

            if tipo == "compuesto":
                consumo = v.get("consumo")
                if isinstance(consumo, str):
                    try:
                        consumo = json.loads(consumo)
                    except Exception:
                        consumo = None
                _revertir_consumo(cur, consumo, tenant_id)
                stock_restaurado = v["cantidad"]
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
                        cur.execute(
                            "INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, "
                            "Stock_Lote, Fecha_Entrada, Estado, tenant_id) "
                            "VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)",
                            (id_lote, v["producto"], v.get("costo_unitario") or 0,
                             v.get("precio_lista") or 0, cantidad,
                             str(datetime.datetime.now(_TZ)), tenant_id)
                        )
                    stock_restaurado = v["cantidad"]
                else:
                    # Venta antigua sin id_lote: fallback al comportamiento previo
                    # (sumar al lote que coincida por costo+precio o crear uno
                    # nuevo), pero DENTRO de la transacción con el cursor `cur`
                    # para no romper la atomicidad (agregar_lote usa el pool con
                    # auto-commit aparte y reintroduciría el Bug #3).
                    cur.execute(
                        "SELECT ID_Lote FROM lotes "
                        "WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s "
                        "AND Estado='Activo' AND tenant_id=%s LIMIT 1",
                        (v["producto"], v.get("costo_unitario") or 0,
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
                        fe = str(datetime.datetime.now(_TZ))
                        cur.execute(
                            "INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, "
                            "Stock_Lote, Fecha_Entrada, Estado, tenant_id) "
                            "VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)",
                            (nuevo_id, v["producto"], v.get("costo_unitario") or 0,
                             v.get("precio_lista") or 0, cantidad, fe, tenant_id)
                        )
                    stock_restaurado = v["cantidad"]

            # Marcar la venta anulada en la MISMA transacción (antes del commit)
            cur.execute(
                "UPDATE ventas SET Estado='Inactivo' WHERE id=%s AND tenant_id=%s",
                (venta_id, tenant_id)
            )
        conn.commit()
        return {"ok": True, "id": venta_id, "stock_restaurado": stock_restaurado}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al anular la venta: {str(e)}"}
    finally:
        release_conn(conn)


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
    conn = get_conn()
    try:
        ventas_a_guardar = []
        for item in items:
            # Consultar el tipo del producto DENTRO de la transacción, para
            # decidir si se descuenta inventario o es un servicio (sin stock).
            tipo = "stock"
            costo_srv = 0.0
            precio_srv = 0.0
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT tipo_producto, costo_servicio, precio_servicio "
                    "FROM productos WHERE Producto = %s AND tenant_id = %s",
                    (item["producto"], tenant_id)
                )
                fila = cur.fetchone()
                if fila:
                    tipo = fila.get("tipo_producto") or "stock"
                    costo_srv = float(fila.get("costo_servicio") or 0)
                    precio_srv = float(fila.get("precio_servicio") or 0)

            variacion = str(item.get("variacion") or "").strip()

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
                    "fecha"         : str(datetime.datetime.now(_TZ)),
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
                fecha = str(datetime.datetime.now(_TZ))
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
            resultado = descontar_stock_peps(
                item["producto"],
                item["cantidad"],
                item["precio_real"],
                tenant_id,
                id_lote=item.get("id_lote"),
                conn=conn,
            )
            for v in resultado:
                v["tipo_producto"] = "stock"
                v["variacion"] = variacion
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
