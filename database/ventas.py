import logging
import psycopg2.extras
import json
import re
import uuid
from datetime import date, datetime
from database.conexion import query, execute, get_conn, release_conn
from database.lotes import descontar_stock_peps
from database.helpers import ahora_negocio, _parsear_ts, zona_tenant, hoy_negocio
from database.puntos import registrar_movimiento

logger = logging.getLogger(__name__)

def get_ventas(
    tenant_id: str,
    limit: int = 500,
    desde_ts: datetime | None = None,
    hasta_ts: datetime | None = None,
) -> list[dict]:
    """
    Renglones de venta ordenados por instante descendente (fecha_ts, con
    índice idx_ventas_tenant_fecha_ts). desde_ts/hasta_ts acotan la ventana
    [inclusive, exclusiva); None = límite abierto. Sin filtros el router
    pasa el mes contable actual, evitando transportar todo el historial.
    """
    sql = (
        "SELECT id, n_ticket, fecha, producto, descripcion, cantidad, precio_lista, precio_real, "
        "costo_unitario, total_venta, ganancia_bruta, estado, tipo_producto, variacion, consumo, orden_id "
        "FROM ventas WHERE tenant_id = %s"
    )
    params: list = [tenant_id]
    if desde_ts is not None:
        sql += " AND fecha_ts >= %s"
        params.append(desde_ts)
    if hasta_ts is not None:
        sql += " AND fecha_ts < %s"
        params.append(hasta_ts)
    sql += " ORDER BY fecha_ts DESC LIMIT %s"
    params.append(limit)
    return query(sql, tuple(params))


def _recalcular_orden(cur, orden_id, tenant_id: str) -> None:
    """
    Recalcula los agregados de una orden desde SUS RENGLONES activos
    (total, ganancia, unidades y estado) y después SINCRONIZA sus pagos.
    Se llama tras toda mutación de ventas (editar/anular) para que la
    cabecera nunca quede desfasada. Con orden_id NULL no hace nada.
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
    _sincronizar_pagos(cur, orden_id, tenant_id)


def _comision_de_pagos(cur, tenant_id: str, pagos: list[dict]) -> float:
    """
    Recalcula la comisión de cada pago A TERMINAL con los montos actuales
    (misma fórmula del cobro: monto × pct + fija) y devuelve comision_total.
    Efectivo siempre 0. Terminal inexistente → comision 0.
    """
    terminales: dict[str, dict] = {}
    for p in pagos:
        metodo = str(p.get("metodo") or "")
        monto = round(float(p.get("monto") or 0), 2)
        if metodo in ("tarjeta_debito", "tarjeta_credito") and p.get("terminal_id"):
            tid = str(p["terminal_id"])
            if tid not in terminales:
                cur.execute(
                    "SELECT nombre, comision_debito_pct, comision_credito_pct, comision_fija "
                    "FROM terminales WHERE id = %s AND tenant_id = %s",
                    (tid, tenant_id)
                )
                t = cur.fetchone()
                terminales[tid] = dict(t) if t else {}
            t = terminales[tid]
            if t:
                pct = t["comision_debito_pct"] if metodo == "tarjeta_debito" else t["comision_credito_pct"]
                p["comision"] = round(monto * float(pct or 0) / 100.0 + float(t["comision_fija"] or 0), 2)
                p["terminal_nombre"] = t["nombre"]
            else:
                p["comision"] = 0.0
                p.pop("terminal_nombre", None)
        else:
            p["comision"] = 0.0
    return round(sum(float(p.get("comision") or 0) for p in pagos), 2)


def _sincronizar_pagos(cur, orden_id, tenant_id: str) -> None:
    """
    Tras recalcular el total de una orden, reajusta los pagos registrados
    para conservar el invariante sum(pagos) == total + propina (invariante
    que el cobro mixto valida al vender, ventas.py _procesar_pago).

    ¿Por qué importa? 'Cobros del Periodo' suma pg->>'monto' desde este
    JSON de pagos: sin esta sincronización, editar/anular un renglón
    corregiría el total del ticket pero los cobros seguirían mostrando
    el monto ORIGINAL del POS (p.ej. cobró $200 → corrigió a $185 → el
    reporte seguiría mostrando $200 en efectivo).

    Reglas: 1 pago → su monto pasa a ser total + propina (mismo significado
    que al cobrar). Mixto → el delta se aplica al ÚLTIMO pago (si con él
    algún monto quedaría ≤ 0, no se toca nada). En ambos casos se
    recalculan las comisiones de terminal con los nuevos montos.
    Efectivo: monto_recibido se conserva (dinero que entró a caja);
    el cambio se reconsidera contra el monto nuevo.
    Órdenes legadas sin pagos: nada que hacer (sus cobros usan o.total,
    que ya quedó sincronizado por el UPDATE de arriba).
    """
    cur.execute(
        "SELECT total, propina, pagos, monto_recibido FROM ordenes "
        "WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
        (orden_id, tenant_id)
    )
    fila = cur.fetchone()
    if not fila:
        return
    pagos = fila["pagos"]
    if not pagos:
        return
    pagos = [dict(p) for p in pagos]
    esperado = round(float(fila["total"] or 0) + float(fila["propina"] or 0), 2)
    suma = round(sum(float(p.get("monto") or 0) for p in pagos), 2)
    if abs(suma - esperado) <= 0.01:
        return
    if len(pagos) == 1:
        pagos[0]["monto"] = esperado
    else:
        delta = round(esperado - suma, 2)
        # El canje de puntos NO se re-ajusta (los puntos ya se consumieron):
        # el delta va al ÚLTIMO pago de dinero (no 'puntos').
        idx_delta = max((i for i, p in enumerate(pagos) if p.get("metodo") != "puntos"),
                        default=len(pagos) - 1)
        ultimo = float(pagos[idx_delta].get("monto") or 0) + delta
        if ultimo <= 0:
            # Sin reparación silenciosa: el desglose mixto no se puede
            # reajustar sin dejar montos ≤ 0. Se dejan los pagos desfasados
            # Y se avisa (el aviso es la señal, la edición manual del cobro
            # es la ruta de reparación).
            logger.warning(
                "Pagos desfasados en orden %s (tenant %s): esperado=%.2f, "
                "registrado=%.2f, mixto sin reparto posible (el último pago "
                "quedaría ≤ %.2f). 'Cobros del Periodo' seguirá mostrando el "
                "desglose original hasta corregir el cobro manualmente.",
                orden_id, tenant_id, esperado, suma, ultimo
            )
            return
        pagos[idx_delta]["monto"] = round(ultimo, 2)
    comision_total = _comision_de_pagos(cur, tenant_id, pagos)
    recibido = fila["monto_recibido"]
    cambio = None
    if recibido is not None:
        recibido = float(recibido)
        if recibido < esperado:
            recibido = esperado
        cambio = round(recibido - esperado, 2)
    cur.execute(
        "UPDATE ordenes SET pagos = %s, comision_total = %s, monto_recibido = %s, cambio = %s "
        "WHERE id = %s::uuid AND tenant_id = %s",
        (psycopg2.extras.Json(pagos), comision_total, recibido, cambio, orden_id, tenant_id)
    )


def get_ordenes(
    tenant_id: str,
    limit: int = 500,
    desde_ts: datetime | None = None,
    hasta_ts: datetime | None = None,
) -> list[dict]:
    """
    Órdenes (tickets) con sus renglones anidados, más recientes primero.
    El ordenamiento usa fecha_ts (instante canónico), no el TEXT legado.
    desde_ts/hasta_ts acotan la ventana [inclusive, exclusiva) sobre
    o.fecha_ts (índice idx_ordenes_tenant_fecha_ts); None = abierto.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = (
                "SELECT id, n_ticket, fecha_ts, total, ganancia, cantidad_items, estado, "
                "       metodo_pago, pagos, propina, monto_recibido, cambio, comision_total, turno_id, "
                "       mesa_id, mesa_nombre "
                "FROM ordenes WHERE tenant_id = %s"
            )
            params: list = [tenant_id]
            if desde_ts is not None:
                sql += " AND fecha_ts >= %s"
                params.append(desde_ts)
            if hasta_ts is not None:
                sql += " AND fecha_ts < %s"
                params.append(hasta_ts)
            sql += " ORDER BY fecha_ts DESC LIMIT %s"
            params.append(limit)
            cur.execute(sql, tuple(params))
            ordenes = [dict(o) for o in cur.fetchall()]
            if not ordenes:
                return []
            ids = [o["id"] for o in ordenes]
            cur.execute(
                "SELECT id, n_ticket, fecha, producto, descripcion, cantidad, precio_lista, precio_real, "
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


_ORDENES_ORDEN_SQL = {
    "fecha-desc": "o.fecha_ts DESC",
    "fecha-asc": "o.fecha_ts ASC",
    "monto-desc": "o.total DESC",
    "monto-asc": "o.total ASC",
    "ganancia-desc": "o.ganancia DESC",
}


def get_ordenes_paginadas(
    tenant_id: str,
    desde_ts=None,
    hasta_ts=None,
    pagina: int = 1,
    por_pagina: int = 10,
    busqueda: str | None = None,
    orden: str = "fecha-desc",
) -> dict:
    """
    Historial de tickets PAGINADO en servidor (una página = un payload chico,
    el historial puede crecer por años sin saturar la red ni el celular).
    Ventana [desde_ts, hasta_ts) sobre o.fecha_ts (índice); búsqueda por folio
    o nombre de producto; orden whitelisted. Devuelve {ordenes, total,
    pagina, por_pagina, total_paginas}.
    """
    orden_sql = _ORDENES_ORDEN_SQL.get(orden, "o.fecha_ts DESC")
    pagina = max(1, pagina)
    por_pagina = min(max(1, por_pagina), 100)

    where = "o.tenant_id = %s"
    params: list = [tenant_id]
    if desde_ts is not None:
        where += " AND o.fecha_ts >= %s"
        params.append(desde_ts)
    if hasta_ts is not None:
        where += " AND o.fecha_ts < %s"
        params.append(hasta_ts)
    if busqueda and busqueda.strip():
        patron = f"%{busqueda.strip()}%"
        where += " AND (o.n_ticket::text ILIKE %s OR EXISTS (SELECT 1 FROM ventas v WHERE v.orden_id = o.id AND v.producto ILIKE %s))"
        params.extend([patron, patron])

    total = query(f"SELECT COUNT(*) AS total FROM ordenes o WHERE {where}", tuple(params))[0]["total"]

    filas = query(
        "SELECT o.id, o.n_ticket, o.fecha_ts, o.total, o.ganancia, o.cantidad_items, o.estado, "
        "o.metodo_pago, o.pagos, o.propina, o.monto_recibido, o.cambio, o.comision_total, o.turno_id, "
        "o.mesa_id, o.mesa_nombre "
        f"FROM ordenes o WHERE {where} "
        f"ORDER BY {orden_sql} LIMIT %s OFFSET %s",
        tuple(params) + (por_pagina, (pagina - 1) * por_pagina)
    )
    # `fecha` = alias de fecha_ts para el contrato del frontend (ISO con zona),
    # igual que en get_ordenes; sin esto el historial muestra "Invalid Date".
    ordenes = [dict(o, fecha=o["fecha_ts"]) for o in filas]
    if ordenes:
        _anidar_renglones(tenant_id, ordenes)

    return {
        "ordenes": ordenes,
        "total": int(total or 0),
        "pagina": pagina,
        "por_pagina": por_pagina,
        "total_paginas": max(1, -(-int(total or 0) // por_pagina)),
    }


def _anidar_renglones(tenant_id: str, ordenes: list[dict]) -> None:
    """Adjunta ventas[] a cada orden (renglones de la página, no del historial)."""
    if not ordenes:
        return
    ids = [o["id"] for o in ordenes]
    renglones = query(
        "SELECT id, n_ticket, fecha, producto, descripcion, cantidad, precio_lista, precio_real, "
        "       costo_unitario, total_venta, ganancia_bruta, estado, tipo_producto, "
        "       variacion, consumo, orden_id "
        "FROM ventas WHERE tenant_id = %s AND orden_id = ANY(%s::uuid[]) "
        "ORDER BY fecha_ts ASC, id ASC",
        (tenant_id, ids)
    )
    por_orden: dict = {o["id"]: o for o in ordenes}
    for o in ordenes:
        o["ventas"] = []
    for r in renglones:
        o = por_orden.get(r.get("orden_id"))
        if o is not None:
            o["ventas"].append(r)

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
             tipo_producto, variacion, consumo, orden_id, n_ticket, descripcion)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s, %s, %s, %s, %s, %s)
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
        (str(venta["descripcion"]).strip() or None) if venta.get("descripcion") else None,
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

            # ── Reversa de movimientos de puntos (ledger, migraciones 038/039) ──
            # Nunca se edita ni borra un movimiento previo: se escribe el
            # movimiento espejo (tipo 'ajuste') para que el saldo quede exacto.
            cur.execute(
                "SELECT cliente_id, tipo, puntos FROM puntos_movimientos "
                "WHERE orden_id = %s::uuid AND tenant_id = %s",
                (orden_id, tenant_id)
            )
            movs = cur.fetchall()
            if movs:
                cliente_id_rev = movs[0]["cliente_id"]
                for m in movs:
                    registrar_movimiento(
                        tenant_id, str(cliente_id_rev), "ajuste", -int(m["puntos"] or 0),
                        concepto=f"Reversa por anulación del ticket #{orden.get('n_ticket')}",
                        orden_id=orden_id, conn=conn,
                    )
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
METODOS_ORDEN = METODOS_PAGO + ("mixto", "puntos")  # 'puntos' = canje cubre todo el ticket


def actualizar_pago_orden(orden_id: str, metodo: str, pagos_in: list[dict] | None, propina_nueva: float | None, tenant_id: str) -> dict:
    """
    Edita el COBRO de un ticket Activo (tipo de pago y/o propina). El total
    NO se toca: si llega un reparto completo de pagos, se valida contra
    total + propina; si solo cambia el método, el único pago toma el monto
    total + propina (mismo significado que al cobrar). Recalcula comisiones
    de terminal con los nuevos montos. Regla: 'un solo método' reconstruye
    los pagos desde cero (el terminal anterior se pierde; comisiones
    recalculadas con el terminal nuevo si pagos_in trae terminal_id).
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT total, propina, estado, metodo_pago, monto_recibido, pagos FROM ordenes "
                "WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                (orden_id, tenant_id)
            )
            orden = cur.fetchone()
            if not orden:
                return {"ok": False, "mensaje": "Ticket no encontrado"}
            if orden.get("estado") == "Anulada":
                return {"ok": False, "tipo": "validacion",
                        "mensaje": "No se puede editar el pago de un ticket anulado"}

            total = float(orden["total"] or 0)
            propina = round(float(propina_nueva), 2) if propina_nueva is not None else float(orden["propina"] or 0)
            if propina < 0:
                return {"ok": False, "tipo": "validacion", "mensaje": "La propina no puede ser negativa"}
            esperado = round(total + propina, 2)
            pagos_actuales = orden["pagos"]

            if pagos_in:
                pagos: list[dict] = []
                for p in pagos_in:
                    m = str(p.get("metodo") or "").strip()
                    if m not in METODOS_PAGO:
                        return {"ok": False, "tipo": "validacion", "mensaje": f"Método de pago inválido: '{m}'"}
                    monto = round(float(p.get("monto") or 0), 2)
                    if monto <= 0:
                        return {"ok": False, "tipo": "validacion", "mensaje": "Los montos de cada pago deben ser mayores a 0"}
                    pagos.append({
                        "metodo": m,
                        "monto": monto,
                        "referencia": (str(p.get("referencia")) or "").strip() or None,
                        "terminal_id": p.get("terminal_id") or None,
                    })
                if abs(sum(p["monto"] for p in pagos) - esperado) > 0.01:
                    return {"ok": False, "tipo": "validacion", "mensaje": (
                        f"La suma de los pagos (${sum(p['monto'] for p in pagos):.2f}) "
                        f"no coincide con el total del ticket (${esperado:.2f})"
                    )}
                metodo_orden = "mixto" if len(pagos) > 1 else pagos[0]["metodo"]
            elif metodo in METODOS_PAGO:
                metodo_orden = metodo
                pagos = [{"metodo": metodo, "monto": esperado, "referencia": None, "terminal_id": None}]
            elif propina_nueva is not None:
                # Edición de SOLO propina: conserva el cobro actual y reajusta
                # sus montos a total + propina (misma regla de _sincronizar_pagos).
                metodo_orden = orden.get("metodo_pago")
                if not pagos_actuales:
                    # Legado sin pagos: solo cambia la propina registrada.
                    cur.execute(
                        "UPDATE ordenes SET propina = %s WHERE id = %s::uuid AND tenant_id = %s",
                        (propina, orden_id, tenant_id)
                    )
                    conn.commit()
                    return {"ok": True, "metodo_pago": metodo_orden}
                pagos = [dict(p) for p in pagos_actuales]
                if len(pagos) == 1:
                    pagos[0]["monto"] = esperado
                else:
                    suma = round(sum(float(p.get("monto") or 0) for p in pagos), 2)
                    ultimo = float(pagos[-1].get("monto") or 0) + round(esperado - suma, 2)
                    if ultimo <= 0:
                        return {"ok": False, "tipo": "validacion",
                                "mensaje": "El desglose mixto actual no permite subir la propina (un pago quedaría ≤ 0). Edita el pago completo con 'pagos'"}
                    pagos[-1]["monto"] = round(ultimo, 2)
            else:
                return {"ok": False, "tipo": "validacion", "mensaje": f"Método de pago inválido: '{metodo}'"}

            comision_total = _comision_de_pagos(cur, tenant_id, pagos)
            # Reglas de monto_recibido/cambio: en la edición de SOLO propina
            # se conserva el efectivo que se recibió (dinero entrado a caja;
            # el cambio se reconsidera contra el monto nuevo). En el resto de
            # ediciones se reconstruye el cobro: efectivo simple asume recibido
            # exacto; tarjetas/mixto no registran recibido.
            solo_propina = (propina_nueva is not None and not pagos_in and metodo not in METODOS_PAGO)
            if solo_propina and orden.get("monto_recibido") is not None:
                recibido_n = float(orden["monto_recibido"])
                if recibido_n < esperado:
                    recibido_n = esperado
                cambio_n = round(recibido_n - esperado, 2)
            elif metodo_orden == "efectivo":
                recibido_n, cambio_n = esperado, 0.0
            else:
                recibido_n, cambio_n = None, None

            cur.execute(
                "UPDATE ordenes SET metodo_pago = %s, pagos = %s, propina = %s, "
                "monto_recibido = %s, cambio = %s, comision_total = %s "
                "WHERE id = %s::uuid AND tenant_id = %s",
                (metodo_orden, psycopg2.extras.Json(pagos), propina, recibido_n, cambio_n,
                 comision_total, orden_id, tenant_id)
            )
        conn.commit()
        return {"ok": True, "metodo_pago": metodo_orden}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al editar el pago: {str(e)}"}
    finally:
        release_conn(conn)


def reparar_pagos_desfasados() -> dict:
    """
    Backfill idempotente (corre en el arranque, tras las migraciones): los
    tickets editados/anulados ANTES de que existiera _sincronizar_pagos
    quedaron con pagos desfasados del total, y lo seguirán hasta que alguien
    los re-edite (backlog invisible en 'Cobros del Periodo').

    Repara en bulk las ordenes ACTIVAS con exactamente 1 pago desfasado:
    monto = total + propina (conservando terminal/referencia del pago),
    comision recalculada con la config viva de la terminal y cambio
    reconsiderado. Las MIXTAS desfasadas NO se reparan (el reparto correcto
    no se puede inferir) y se reportan en el log como pendientes manuales.
    Se ejecuta en cada boot: solo toca filas desfasadas, así que después de
    la primera pasada no vuelve a reparar nada.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id::text AS id_str, tenant_id, total, propina, pagos, monto_recibido "
                "FROM ordenes WHERE estado = 'Activa' AND pagos IS NOT NULL "
                "AND jsonb_array_length(pagos) = 1 "
                "AND abs(COALESCE(pagos->0->>'monto', '0')::numeric "
                "        - (total + COALESCE(propina, 0))) > 0.01 "
                "FOR UPDATE"
            )
            filas = cur.fetchall()
            reparadas = 0
            for fila in filas:
                esperado = round(float(fila["total"] or 0) + float(fila["propina"] or 0), 2)
                pagos = [dict(fila["pagos"][0])]
                pagos[0]["monto"] = esperado
                comision_total = _comision_de_pagos(cur, str(fila["tenant_id"]), pagos)
                recibido = fila["monto_recibido"]
                cambio = None
                if recibido is not None:
                    recibido = float(recibido)
                    if recibido < esperado:
                        recibido = esperado
                    cambio = round(recibido - esperado, 2)
                cur.execute(
                    "UPDATE ordenes SET pagos = %s, comision_total = %s, monto_recibido = %s, cambio = %s "
                    "WHERE id = %s::uuid AND tenant_id = %s",
                    (psycopg2.extras.Json(pagos), comision_total, recibido, cambio,
                     fila["id_str"], str(fila["tenant_id"]))
                )
                reparadas += 1

            cur.execute(
                "SELECT id, total, propina FROM ordenes WHERE estado = 'Activa' "
                "AND pagos IS NOT NULL AND jsonb_array_length(pagos) > 1 "
                "AND abs(COALESCE((SELECT SUM((p->>'monto')::numeric) "
                "                  FROM jsonb_array_elements(pagos) p), 0) "
                "        - (total + COALESCE(propina, 0))) > 0.01"
            )
            mixtos = cur.fetchall()
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning("Backfill de pagos desfasados falló (no bloquea el arranque): %s", e)
        return {"ok": False, "mensaje": str(e)}
    finally:
        release_conn(conn)
    for fila in mixtos:
        logger.warning(
            "Ticket MIXTO desfasado pendiente de corrección manual: orden %s "
            "(total+propina=%.2f). 'Cobros del Periodo' sigue mostrando el "
            "desglose original.", fila["id"],
            float(fila["total"] or 0) + float(fila["propina"] or 0)
        )
    return {"ok": True, "reparadas": reparadas, "mixtos_pendientes": len(mixtos)}


def _procesar_pago(
    pago: dict | None,
    total_venta: float,
    terminales_map: dict | None = None,
    pago_puntos: dict | None = None,
) -> dict:
    """
    Valida y normaliza el pago de un carrito; devuelve las columnas para ordenes.

    Reglas:
      - Sin pago (API legada) → metodo_pago/pagos NULL, propina 0.
      - total_a_pagar = total_venta (productos) + propina − canje de puntos.
        El canje entra como SU PROPIO pago (metodo 'puntos') al inicio de `pagos`,
        así se conserva el invariante sum(pagos) == total + propina y los
        "Cobros del Periodo" muestran cuánto se pagó con puntos.
      - Método simple: se genera UN pago DENTERO por el total_a_pagar; en
        efectivo monto_recibido >= total_a_pagar y cambio = recibido - total_a_pagar.
      - Mixto: lista de pagos reales cuya suma (dinero) debe cuadrar (±1 centavo).
      - Pagos con tarjeta pueden llevar terminal_id: la comisión se calcula con
        la tarifa de ESA terminal (pct débito/crédito + cuota fija) y queda
        dentro de cada pago + comision_total de la orden (Fase B).
      - Canje total (sin dinero): metodo "puntos" con pagos = [pago_puntos].
    Errores de regla de negocio → ValueError (el router responde 422).
    """
    valor_canje = round(float(pago_puntos["monto"]), 2) if pago_puntos else 0.0

    if not pago:
        if valor_canje > 0:
            # Todo el ticket se cubre con puntos (no hay método extra elegido)
            return {"metodo_pago": "puntos", "pagos": [pago_puntos], "propina": 0.0,
                    "monto_recibido": None, "cambio": None, "comision_total": 0.0}
        return {"metodo_pago": None, "pagos": None, "propina": 0.0,
                "monto_recibido": None, "cambio": None, "comision_total": 0.0}

    metodo = str(pago.get("metodo") or "").strip()
    if metodo not in METODOS_ORDEN:
        raise ValueError(f"Método de pago inválido: '{metodo}'")

    propina = round(float(pago.get("propina") or 0), 2)
    if propina < 0:
        raise ValueError("La propina no puede ser negativa")

    recibido = None
    cambio = None

    if metodo == "puntos":
        # Canje cubre todo: solo válido si no queda dinero pendiente
        total_a_pagar = round(total_venta + propina - valor_canje, 2)
        if abs(total_a_pagar) > 0.01:
            raise ValueError(
                f"Pagar solo con puntos requiere que el canje (${valor_canje:.2f}) "
                f"cubra todo el ticket (${total_a_pagar + valor_canje:.2f})"
            )
        pagos = [pago_puntos]
    elif metodo == "mixto":
        propina_ok = round(propina, 2)
        total_a_pagar = round(total_venta + propina_ok - valor_canje, 2)
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
                f"no coincide con el total a pagar en dinero (${total_a_pagar:.2f})"
            )
    else:
        if metodo == "efectivo":
            recibido_raw = pago.get("monto_recibido")
            total_a_pagar = round(total_venta + propina - valor_canje, 2)
            recibido = round(float(recibido_raw), 2) if recibido_raw is not None else total_a_pagar
            if recibido < total_a_pagar:
                raise ValueError(
                    f"El monto recibido (${recibido:.2f}) es menor al total a pagar en dinero (${total_a_pagar:.2f})"
                )
            cambio = round(recibido - total_a_pagar, 2)
        else:
            total_a_pagar = round(total_venta + propina - valor_canje, 2)
        pagos = [{"metodo": metodo, "monto": total_a_pagar, "referencia":
                  ((pago.get("referencia") and str(pago.get("referencia")).strip()) or None),
                  "terminal_id": pago.get("terminal_id") or None}]

    # El canje entra SIEMPRE al frente de los pagos (invariante sum(pagos) == total + propina)
    if valor_canje > 0 and metodo != "puntos":
        pagos.insert(0, pago_puntos)

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


def _reservar_cliente_puntos(cur, tenant_id: str, cliente_id: str | None) -> tuple[dict, dict, int]:
    """
    Bloquea al cliente (FOR UPDATE) dentro de la transacción del cobro, lee su
    saldo REAL del ledger y la config del programa de puntos del tenant.
    Devuelve (cliente, config, saldo). Pasa ValueError si el cliente no existe,
    está de baja o el sistema de puntos está mal configurado.
    """
    if not cliente_id:
        return {}, {}, 0
    cur.execute(
        "SELECT id, nombre, activo FROM clientes WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
        (cliente_id, tenant_id)
    )
    cliente = cur.fetchone()
    if not cliente:
        raise ValueError("El cliente seleccionado no existe en tu cartera")
    if not cliente["activo"]:
        raise ValueError(f"El cliente '{cliente['nombre']}' está dado de baja")

    cur.execute(
        "SELECT puntos_activos, puntos_valor_punto, puntos_modo, "
        "puntos_gasto_monto, puntos_gasto_pts, puntos_fijos "
        "FROM tenants WHERE id = %s", (tenant_id,)
    )
    config = cur.fetchone() or {}
    cur.execute(
        "SELECT COALESCE(SUM(puntos), 0) AS saldo FROM puntos_movimientos "
        "WHERE tenant_id = %s AND cliente_id = %s::uuid",
        (tenant_id, cliente_id)
    )
    saldo = int(cur.fetchone()["saldo"] or 0)
    return dict(cliente), dict(config), saldo


def cobrar_carrito(
    items: list[dict],
    tenant_id: str,
    pago: dict | None = None,
    mesa_id: str | None = None,
    cliente_id: str | None = None,
    puntos_usados: int = 0,
    ajuste_puntos: int = 0,
    ajuste_concepto: str | None = None,
) -> dict:
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
        # ── Cobro de mesa (Fase 2, migración 037) ──
        # Si el carrito viene de una mesa, se bloquea y se registra su nombre
        # (snapshot para trazabilidad). La liberación real (borrar mesa_items +
        # estado Libre) ocurre AL FINAL de ESTA misma transacción: si el cobro
        # falla (stock, receta, pago inválido), la orden abierta queda intacta.
        mesa_nombre = None
        if mesa_id:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, nombre, estado FROM mesas WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                    (mesa_id, tenant_id)
                )
                fila_mesa = cur.fetchone()
                if not fila_mesa:
                    raise ValueError("La mesa no existe o ya no está disponible")
                mesa_nombre = fila_mesa["nombre"]

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
                    "SELECT tipo_producto, costo_servicio, precio_servicio, fraccionable, es_generico "
                    "FROM productos WHERE Producto = %s AND tenant_id = %s",
                    (item["producto"], tenant_id)
                )
                fila = cur.fetchone()
                if fila:
                    tipo = fila.get("tipo_producto") or "stock"
                    costo_srv = float(fila.get("costo_servicio") or 0)
                    precio_srv = float(fila.get("precio_servicio") or 0)
                    fraccionable = bool(fila.get("fraccionable"))
                    es_generico = bool(fila.get("es_generico"))
                else:
                    es_generico = False

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
                #
                # EXCEPCIÓN — Venta libre (migración 036): el producto genérico
                # 'Venta libre' es un servicio comodín cuyo costo/precio se
                # capturan EN LA VENTA (no en el producto): costo = item.costo
                # (default 0) y precio_lista = precio_real (cada venta cuesta lo
                # que se cobró; el producto no tiene "precio de lista").
                fecha = str(ahora_negocio(tenant_id))
                cant = float(item["cantidad"])
                precio_real = float(item["precio_real"])
                costo_unit = float(item.get("costo") or 0) if es_generico else costo_srv
                precio_lista = precio_real if es_generico else precio_srv
                ventas_a_guardar.append({
                    "fecha"         : fecha,
                    "producto"      : item["producto"],
                    "cantidad"      : cant,
                    "precio_lista"  : precio_lista,
                    "precio_real"   : precio_real,
                    "costo_unitario": costo_unit,
                    "total_venta"   : precio_real * cant,
                    "ganancia_bruta": (precio_real - costo_unit) * cant,
                    "id_lote"       : None,
                    "tipo_producto" : "servicio",
                    "variacion"     : variacion,
                    "descripcion"   : (str(item.get("descripcion")).strip() or None) if item.get("descripcion") else None,
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
        # ── Estado del bloque cliente/puntos (coeficientes de la venta) ──
        cliente_fila: dict = {}
        config_puntos: dict = {}
        saldo_cliente = 0
        saldo_cliente_final = 0
        valor_canje = 0.0
        puntos_canjeados = 0
        puntos_ganados = 0
        valor_punto = 0.0
        ajuste = int(ajuste_puntos or 0)
        if mesa_id and not ventas_a_guardar:
            raise ValueError("La mesa no tiene artículos que cobrar")
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

            # ── Cliente + canje de puntos (migraciones 038/039, Fase B) ──
            # Todo dentro de la MISMA transacción: bloquea al cliente, valida el
            # saldo del ledger, calcula canje/ganancia según la regla del tenant.
            if cliente_id:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cliente_fila, config_puntos, saldo_cliente = _reservar_cliente_puntos(cur, tenant_id, cliente_id)

                valor_punto = float(config_puntos.get("puntos_valor_punto") or 1.0)
                if valor_punto <= 0:
                    valor_punto = 1.0

                if puntos_usados and puntos_usados > 0:
                    if not config_puntos.get("puntos_activos"):
                        raise ValueError("El sistema de puntos está desactivado — actívalo en Ajustes → Mi Negocio")
                    tope_dinero = int(total_productos / valor_punto + 1e-9)
                    tope = min(saldo_cliente, tope_dinero)
                    if tope <= 0:
                        raise ValueError(
                            f"Este ticket no admite canje de puntos (saldo del cliente: {saldo_cliente} pts)"
                        )
                    # Clamp silencioso: nunca canjea más que el saldo o más que el total
                    puntos_canjeados = min(int(puntos_usados), tope)
                    valor_canje = round(puntos_canjeados * valor_punto, 2)

                # Puntos GANADOS por la regla del negocio, sobre el DINERO pagado
                # (total de productos − lo que se cubrió con canje). Redondeo al
                # entero más cercano; decidió el tenant en panel de config.
                if config_puntos.get("puntos_activos"):
                    neto_dinero = max(0.0, total_productos - valor_canje)
                    if config_puntos.get("puntos_modo") == "fijo":
                        puntos_ganados = int(config_puntos.get("puntos_fijos") or 0)
                    else:
                        monto = float(config_puntos.get("puntos_gasto_monto") or 0)
                        pts_x = int(config_puntos.get("puntos_gasto_pts") or 0)
                        puntos_ganados = int(round(neto_dinero / monto * pts_x)) if monto > 0 and pts_x > 0 else 0
                    if puntos_ganados < 0:
                        puntos_ganados = 0

                # Ajuste manual del ticket (dar ±/− puntos, ej. promo 50% aplicada a mano)
                if ajuste != 0 and not (ajuste_concepto or "").strip():
                    raise ValueError("Indica el motivo del ajuste de puntos")
                saldo_cliente_final = saldo_cliente + puntos_ganados - puntos_canjeados + ajuste
                if saldo_cliente_final < 0:
                    raise ValueError(
                        f"No puedes quitar puntos de más: el saldo de '{cliente_fila['nombre']}' quedaría en {saldo_cliente_final} pts"
                    )

            pago_puntos_dict = None
            if puntos_canjeados > 0:
                pago_puntos_dict = {
                    "metodo": "puntos",
                    "monto": valor_canje,
                    "puntos": puntos_canjeados,
                    "valor_punto": valor_punto,
                    "referencia": f"Canje de {cliente_fila.get('nombre', '')}",
                }
            pago_cols = _procesar_pago(pago, total_productos, terminales_map, pago_puntos_dict)

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
                    "metodo_pago, pagos, propina, monto_recibido, cambio, comision_total, turno_id, mesa_id, mesa_nombre, cliente_id) "
                    "VALUES (%s, NULL, %s, %s, %s, %s, 'Activa', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
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
                        mesa_id,
                        mesa_nombre,
                        cliente_id,
                    )
                )
                fila_orden = cur.fetchone()
                orden_id = fila_orden["id"]
                n_ticket = fila_orden["n_ticket"]

            # ── Movimientos del ledger de puntos (misma transacción) ──
            # canjeados: −pts con snapshot del valor $; ganados: +pts por la regla;
            # ajuste: ±pts manual con motivo. La anulación del ticket los revierte.
            if cliente_id:
                concepto_ticket = f"Ticket #{n_ticket}"
                if puntos_canjeados > 0:
                    registrar_movimiento(
                        tenant_id, cliente_id, "canjeados", -puntos_canjeados,
                        concepto=concepto_ticket, orden_id=orden_id,
                        valor_monetario=valor_canje, conn=conn,
                    )
                if puntos_ganados > 0:
                    registrar_movimiento(
                        tenant_id, cliente_id, "ganados", puntos_ganados,
                        concepto=concepto_ticket, orden_id=orden_id, conn=conn,
                    )
                if ajuste != 0:
                    registrar_movimiento(
                        tenant_id, cliente_id, "ajuste", ajuste,
                        concepto=(ajuste_concepto or "").strip(), orden_id=orden_id, conn=conn,
                    )

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

        # ── Liberar la mesa (misma transacción) ──
        # Solo si el cobro fue EXITOSO hasta aquí: borramos sus renglones
        # abiertos y la dejamos Libre. Si algo de arriba falló, el rollback
        # también revierte esto (la orden abierta sobrevive para reintentar).
        if mesa_id and ventas_a_guardar:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mesa_items WHERE mesa_id = %s::uuid AND tenant_id = %s",
                    (mesa_id, tenant_id)
                )
                cur.execute(
                    "UPDATE mesas SET estado = 'Libre', abierta_en = NULL "
                    "WHERE id = %s::uuid AND tenant_id = %s",
                    (mesa_id, tenant_id)
                )

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
            "mesa_id": mesa_id,
            "mesa_nombre": mesa_nombre,
            # ── Cliente + puntos (para el toast del POS) ──
            "cliente_id": cliente_id,
            "cliente_nombre": cliente_fila.get("nombre"),
            "puntos_ganados": puntos_ganados,
            "puntos_canjeados": puntos_canjeados,
            "saldo_cliente": saldo_cliente_final if cliente_id else None,
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
