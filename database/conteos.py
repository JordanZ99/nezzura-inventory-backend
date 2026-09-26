# ==============================================================================
# backend/database/conteos.py
# Conteos de auditoría: conteo físico vs sistema (migración 041).
#
# Ciclo: abrir (snapshot por producto+variación) → capturar (autosave
# acumulativo de lo contado) → cerrar (resolver cada diferencia).
#
# El cierre compara contra el stock VIVO re-leído en la MISMA transacción, no
# contra el snapshot: las ventas y restocks hechos mientras el conteo estaba
# abierto quedan absorbidos y solo se resuelve lo que realmente no cuadra.
# Cada resolución escribe al ledger (040) con origen 'conteo' y
# referencia_id = id del conteo:
#   - merma / error_sistema (faltante)  → salida PEPS sin venta
#   - venta                              → orden + ventas retroactivas reales
#   - entrada_no_registrada              → lote nuevo con el costo indicado
#   - error_sistema (sobrante)           → ajuste +stock sobre el lote activo
#
# Mismo patrón transaccional que cobrar_carrito: todo el cierre ocurre en UNA
# conexión con commit al final; si algo falla, rollback y nada quedó a medias.
# ==============================================================================

import re
import uuid
from datetime import date, datetime, time

import psycopg2
import psycopg2.extras
from psycopg2.errors import UniqueViolation

from database.conexion import query, get_conn, release_conn
from database.helpers import _q, _e, ahora_negocio, hoy_negocio, zona_tenant, _parsear_ts
from database.lotes import descontar_stock_peps
from database.ventas import insertar_venta
from database.movimientos import registrar_movimiento_inventario

# Dos décimas de kilo y un redondeo no deben contar como "diferencia".
_EPSILON = 1e-3

# Stock consolidado por (producto, variación) de los productos de stock ACTIVOS.
# Incluye los de stock 0: puede "aparecer" mercancía que el sistema ya daba
# por agotada. Compuestos y servicios no se cuentan (los compuestos se miden
# por sus materiales).
_AGRUPADO_SQL = """
    SELECT p.id AS producto_id,
           p.Producto AS producto,
           COALESCE(v.nombre, '') AS variacion,
           ROUND(COALESCE(SUM(l.Stock_Lote), 0)::numeric, 3) AS stock
    FROM productos p
    LEFT JOIN lotes l
        ON l.producto_id = p.id AND l.tenant_id = p.tenant_id AND l.Estado = 'Activo'
    LEFT JOIN producto_variaciones v ON v.id = l.variacion_id
    WHERE p.tenant_id = %s AND p.Estado = 'Activo' AND p.tipo_producto = 'stock'
    GROUP BY p.id, p.Producto, COALESCE(v.nombre, '')
"""


def _uuid_valido(valor) -> bool:
    try:
        uuid.UUID(str(valor))
        return True
    except Exception:
        return False


def _resolver_variacion_id(conn, producto_id: int, variacion: str, tenant_id: str) -> int | None:
    """Id de la variación por nombre dentro del producto (None = lote base).
    Si la variación desapareció a mitad del conteo, se frená el cierre: aplicar
    el descuento a los lotes base corrompería el stock."""
    nombre = (variacion or "").strip()
    if not nombre:
        return None
    filas = _q(conn,
        "SELECT id FROM producto_variaciones "
        "WHERE producto_id = %s AND nombre = %s AND tenant_id = %s",
        (producto_id, nombre, tenant_id))
    if not filas:
        raise ValueError(
            f"La variación '{nombre}' ya no existe. Quita esa captura del conteo "
            "o vuelve a crear la variación antes de cerrar."
        )
    return filas[0]["id"]


def _resolver_fecha_venta(fecha: str | None, tenant_id: str) -> datetime | None:
    """
    Fecha de las ventas declaradas: 'YYYY-MM-DD' (día del bazar, se toma a
    medianoche en la zona del negocio para caer en el día contable correcto),
    un ISO completo, o None = ahora. Nunca futura.
    """
    if not fecha or not str(fecha).strip():
        return None
    s = str(fecha).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        dt = datetime.combine(date.fromisoformat(s), time.min, tzinfo=zona_tenant(tenant_id))
    else:
        dt = _parsear_ts(s)
    if dt > ahora_negocio(tenant_id):
        raise ValueError("La fecha de las ventas declaradas no puede ser futura")
    return dt


# ==============================================================================
# Lectura
# ==============================================================================


def _items_de_conteo(conteo_id: str, tenant_id: str) -> list[dict]:
    items = query(
        "SELECT id, producto_id, producto, variacion, esperado, contado, vivo, delta, resolucion "
        "FROM conteo_items WHERE conteo_id = %s AND tenant_id = %s "
        "ORDER BY producto, variacion",
        (conteo_id, tenant_id))
    for it in items:
        for col in ("esperado", "contado", "vivo", "delta"):
            if it.get(col) is not None:
                it[col] = float(it[col])
    return items


def get_conteo_activo(tenant_id: str) -> dict:
    """Sesión abierta (si existe) con sus renglones de captura."""
    filas = query(
        "SELECT id, estado, abierto_at FROM conteos "
        "WHERE tenant_id = %s AND estado = 'abierto' ORDER BY abierto_at DESC LIMIT 1",
        (tenant_id,))
    if not filas:
        return {"ok": True, "conteo": None, "items": []}
    return {"ok": True, "conteo": filas[0], "items": _items_de_conteo(filas[0]["id"], tenant_id)}


def get_conteo(conteo_id: str, tenant_id: str) -> dict:
    """Detalle de una sesión (abierta o cerrada)."""
    if not _uuid_valido(conteo_id):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Conteo no encontrado"}
    filas = query(
        "SELECT id, estado, abierto_at, cerrado_at, resumen FROM conteos "
        "WHERE id = %s::uuid AND tenant_id = %s",
        (conteo_id, tenant_id))
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Conteo no encontrado"}
    return {"ok": True, "conteo": filas[0], "items": _items_de_conteo(conteo_id, tenant_id)}


def historial_conteos(tenant_id: str, limit: int = 30) -> list[dict]:
    """Sesiones cerradas, la más reciente primero (con su resumen)."""
    return query(
        "SELECT id, estado, abierto_at, cerrado_at, resumen FROM conteos "
        "WHERE tenant_id = %s AND estado = 'cerrado' "
        "ORDER BY cerrado_at DESC LIMIT %s",
        (tenant_id, min(max(1, limit), 100)))


# ==============================================================================
# Abrir + capturar
# ==============================================================================


def abrir_conteo(tenant_id: str) -> dict:
    """
    Abre una sesión y congela el snapshot: un renglón por producto+variación
    con el stock consolidado actual (lotes activos).
    """
    if query("SELECT 1 FROM conteos WHERE tenant_id = %s AND estado = 'abierto'", (tenant_id,)):
        return {"ok": False, "tipo": "validacion",
                "mensaje": "Ya hay un conteo abierto: captura y ciérralo antes de iniciar otro."}
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO conteos (tenant_id) VALUES (%s) RETURNING id, abierto_at",
                (tenant_id,))
            conteo = dict(cur.fetchone())
            # Snapshot en un solo INSERT...SELECT: atómico y sin lectura previa.
            cur.execute(
                "INSERT INTO conteo_items (conteo_id, tenant_id, producto_id, producto, variacion, esperado) "
                f"SELECT %s, %s, s.producto_id, s.producto, s.variacion, s.stock FROM ({_AGRUPADO_SQL}) s",
                (conteo["id"], tenant_id, tenant_id))
            cur.execute(
                "SELECT id, producto_id, producto, variacion, esperado, contado "
                "FROM conteo_items WHERE conteo_id = %s ORDER BY producto, variacion",
                (conteo["id"],))
            items = [dict(f) for f in cur.fetchall()]
        for it in items:
            it["esperado"] = float(it["esperado"])
        conn.commit()
        return {"ok": True, "conteo": {**conteo, "estado": "abierto"}, "items": items}
    except UniqueViolation:
        # Carrera con otro "abrir" simultáneo: el índice único parcial protege.
        conn.rollback()
        return {"ok": False, "tipo": "validacion",
                "mensaje": "Ya hay un conteo abierto: captura y ciérralo antes de iniciar otro."}
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


def guardar_capturas(conteo_id: str, tenant_id: str, items: list[dict]) -> dict:
    """
    Autosave del conteo: fija el total contado de cada (producto, variación).
    El valor es ABSOLUTO (no incremental): idempotente y seguro ante reintentos
    de red. contado None = borrar la captura (volver a "sin contar").
    """
    if not _uuid_valido(conteo_id):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Conteo no encontrado"}
    for it in items:
        if it.get("contado") is not None and float(it["contado"]) < 0:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": f"'{it.get('producto')}': lo contado no puede ser negativo"}
    estado = query(
        "SELECT estado FROM conteos WHERE id = %s::uuid AND tenant_id = %s",
        (conteo_id, tenant_id))
    if not estado:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Conteo no encontrado"}
    if estado[0]["estado"] != "abierto":
        return {"ok": False, "tipo": "cerrado", "mensaje": "El conteo ya está cerrado"}

    guardados, no_encontrados = 0, []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for it in items:
                contado = it.get("contado")
                if contado is not None:
                    contado = round(float(contado), 3)
                cur.execute(
                    "UPDATE conteo_items SET contado = %s, actualizado_at = now() "
                    "WHERE conteo_id = %s AND tenant_id = %s AND producto = %s AND variacion = %s",
                    (contado, conteo_id, tenant_id, it.get("producto"),
                     (it.get("variacion") or "").strip()))
                if cur.rowcount:
                    guardados += 1
                else:
                    no_encontrados.append({"producto": it.get("producto"),
                                            "variacion": (it.get("variacion") or "").strip()})
        conn.commit()
        return {"ok": True, "guardados": guardados, "no_encontrados": no_encontrados}
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


# ==============================================================================
# Cierre: resolución de diferencias
# ==============================================================================


def _registrar_entrada_no_registrada(conn, item: dict, cantidad: float, resolucion: dict,
                                     variacion_id: int | None, tenant_id: str, conteo_id: str) -> None:
    """Sobrante clasificado como mercancía que llegó sin alta: crea un lote
    nuevo con el costo/precio indicados (o los del último lote activo)."""
    ultimo = _q(conn,
        "SELECT Costo, Precio_Venta FROM lotes "
        "WHERE producto_id = %s AND Estado = 'Activo' AND tenant_id = %s "
        "AND variacion_id IS NOT DISTINCT FROM %s "
        "ORDER BY Fecha_Entrada DESC, id DESC LIMIT 1",
        (item["producto_id"], tenant_id, variacion_id))
    costo = resolucion.get("costo")
    if costo is None:
        costo = float(ultimo[0]["costo"]) if ultimo else 0.0
    precio = resolucion.get("precio_venta")
    if precio is None:
        precio = float(ultimo[0]["precio_venta"]) if ultimo else 0.0

    id_lote = str(uuid.uuid4())[:12]
    fecha = str(ahora_negocio(tenant_id))
    _e(conn, """
        INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta, Stock_Lote,
                           Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, etiqueta, variacion_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, 'Conteo', %s)
    """, (id_lote, item["producto"], item["producto_id"], float(costo or 0), float(precio or 0),
          cantidad, fecha, _parsear_ts(fecha), tenant_id, variacion_id))
    registrar_movimiento_inventario(
        tenant_id, item["producto"], "entrada", "conteo", cantidad,
        id_lote=id_lote, referencia_id=conteo_id,
        concepto="Mercancía hallada en conteo (ingreso no registrado)", conn=conn)


def _ajustar_sobrante(conn, item: dict, cantidad: float, variacion_id: int | None,
                      tenant_id: str, conteo_id: str) -> None:
    """Sobrante clasificado como error del sistema: suma el stock al lote
    activo más reciente de la variación (crea uno si no hay)."""
    lote = _q(conn,
        "SELECT ID_Lote FROM lotes "
        "WHERE producto_id = %s AND Estado = 'Activo' AND tenant_id = %s "
        "AND variacion_id IS NOT DISTINCT FROM %s "
        "ORDER BY Fecha_Entrada DESC, id DESC LIMIT 1",
        (item["producto_id"], tenant_id, variacion_id))
    if lote:
        id_lote = lote[0]["id_lote"]
        _e(conn, "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id = %s",
           (cantidad, id_lote, tenant_id))
    else:
        id_lote = str(uuid.uuid4())[:12]
        fecha = str(ahora_negocio(tenant_id))
        _e(conn, """
            INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta, Stock_Lote,
                               Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, variacion_id)
            VALUES (%s, %s, %s, 0, 0, %s, %s, %s, 'Activo', %s, %s)
        """, (id_lote, item["producto"], item["producto_id"], cantidad,
              fecha, _parsear_ts(fecha), tenant_id, variacion_id))
    registrar_movimiento_inventario(
        tenant_id, item["producto"], "ajuste", "conteo", cantidad,
        id_lote=id_lote, referencia_id=conteo_id,
        concepto="Corrección de stock en conteo (sobrante)", conn=conn)


def cerrar_conteo(conteo_id: str, tenant_id: str,
                  resoluciones: list[dict], fecha: str | None = None) -> dict:
    """
    Cierra la sesión resolviendo cada renglón contra el stock VIVO.

    Por renglón:
      - contado NULL → sin_contar (no se toca nada).
      - |contado − vivo| ≤ 0.001 → cuadrado (no se toca nada; si vendió
        durante el conteo, la venta del POS ya bajó el stock y aquí cuadra).
      - Faltante (Δ<0): merma (default) | venta | error_sistema.
      - Sobrante (Δ>0): error_sistema (default) | entrada_no_registrada.

    'venta' genera una orden retroactiva REAL (mismo camino que un cobro del
    POS: PEPS + insertar_venta + ledger), con la fecha indicada — así las
    ventas del bazar caen al día contable correcto y las estadísticas las
    cuentan de forma natural.

    Todo en UNA transacción: cualquier fallo revierte stock, ventas, ledger
    y el cierre completo.
    """
    if not _uuid_valido(conteo_id):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Conteo no encontrado"}
    try:
        fecha_venta = _resolver_fecha_venta(fecha, tenant_id)
    except ValueError as e:
        return {"ok": False, "tipo": "validacion", "mensaje": str(e)}

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, estado FROM conteos WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                (conteo_id, tenant_id))
            fila = cur.fetchone()
        if not fila:
            return {"ok": False, "tipo": "no_encontrado", "mensaje": "Conteo no encontrado"}
        if fila["estado"] != "abierto":
            return {"ok": False, "tipo": "cerrado",
                    "mensaje": "El conteo ya está cerrado; revisa el historial para ver su resultado."}

        items = _q(conn,
            "SELECT id, producto_id, producto, variacion, esperado, contado "
            "FROM conteo_items WHERE conteo_id = %s ORDER BY producto, variacion",
            (conteo_id,))

        # Stock vivo por (producto, variación) al momento del cierre.
        vivo = {}
        for f in _q(conn, _AGRUPADO_SQL, (tenant_id,)):
            vivo[(f["producto"], f["variacion"])] = float(f["stock"])

        pedidas = {}
        for r in resoluciones or []:
            pedidas[(r.get("producto"), (r.get("variacion") or "").strip())] = r

        resumen = {
            "cuadrados": 0, "sin_contar": 0, "omitidos": 0,
            "mermas": 0, "merma_costo": 0.0,
            "ventas": 0, "venta_total": 0.0,
            "entradas": 0, "ajustes": 0,
        }
        ventas_a_guardar: list[dict] = []
        actualizaciones: list[tuple] = []
        fecha_texto = str(fecha_venta) if fecha_venta else None
        conteo_ref = str(conteo_id)

        for it in items:
            key = (it["producto"], it["variacion"])
            contado = float(it["contado"]) if it["contado"] is not None else None

            # Sin captura → no se resuelve (el usuario no llegó a contarlo).
            if contado is None:
                actualizaciones.append((None, None, "sin_contar", it["id"]))
                resumen["sin_contar"] += 1
                continue

            # El producto desapareció durante el conteo (baja/renombre): no se
            # puede resolver contra stock inexistente — se omite y se reporta.
            if key not in vivo:
                actualizaciones.append((None, None, "sin_contar", it["id"]))
                resumen["sin_contar"] += 1
                resumen["omitidos"] += 1
                continue

            v = vivo[key]
            delta = round(contado - v, 3)
            if abs(delta) <= _EPSILON:
                actualizaciones.append((v, delta, "cuadrado", it["id"]))
                resumen["cuadrados"] += 1
                continue

            pedida = pedidas.get(key) or {}
            res = (pedida.get("resolucion") or "").strip().lower()
            if not res:
                res = "merma" if delta < 0 else "error_sistema"
            if delta < 0 and res not in ("merma", "venta", "error_sistema"):
                raise ValueError(
                    f"'{it['producto']}' ({it['variacion'] or 'base'}): faltan {abs(delta)} — "
                    "un faltante solo admite merma, venta o error_sistema")
            if delta > 0 and res not in ("entrada_no_registrada", "error_sistema"):
                raise ValueError(
                    f"'{it['producto']}' ({it['variacion'] or 'base'}): sobran {delta} — "
                    "un sobrante solo admite entrada_no_registrada o error_sistema")

            cantidad = abs(delta)
            variacion_id = _resolver_variacion_id(conn, it["producto_id"], it["variacion"], tenant_id)

            if res == "venta":
                # Venta declarada: descuenta PEPS (bloqueando lotes) y cada
                # renglón se cobra al precio de lista del lote que salió.
                for chunk in descontar_stock_peps(
                        it["producto"], cantidad, 0.0, tenant_id,
                        variacion_id=variacion_id, conn=conn):
                    precio = float(chunk.get("precio_lista") or 0)
                    cant = float(chunk["cantidad"])
                    costo = float(chunk.get("costo_unitario") or 0)
                    ventas_a_guardar.append({
                        "fecha": fecha_texto or str(ahora_negocio(tenant_id)),
                        "producto": it["producto"],
                        "cantidad": cant,
                        "precio_lista": precio,
                        "precio_real": precio,
                        "costo_unitario": costo,
                        "total_venta": precio * cant,
                        "ganancia_bruta": (precio - costo) * cant,
                        "id_lote": chunk.get("id_lote"),
                        "tipo_producto": "stock",
                        "variacion": it["variacion"],
                        "descripcion": "Venta declarada en conteo de auditoría",
                    })
            elif res in ("merma", "error_sistema") and delta < 0:
                # FALTANTE: el stock sale de los lotes (PEPS) pero SIN venta.
                # merma → tipo 'salida' (pérdida); error_sistema → 'ajuste'.
                tipo_ledger = "salida" if res == "merma" else "ajuste"
                concepto = ("Merma detectada en conteo" if res == "merma"
                            else "Corrección de stock en conteo (faltante)")
                for chunk in descontar_stock_peps(
                        it["producto"], cantidad, 0.0, tenant_id,
                        variacion_id=variacion_id, conn=conn):
                    registrar_movimiento_inventario(
                        tenant_id, it["producto"], tipo_ledger, "conteo",
                        -float(chunk["cantidad"]),
                        id_lote=chunk.get("id_lote"), referencia_id=conteo_ref,
                        concepto=concepto, conn=conn)
                    if res == "merma":
                        resumen["merma_costo"] += (float(chunk.get("costo_unitario") or 0)
                                                   * float(chunk["cantidad"]))
            elif res == "entrada_no_registrada":
                _registrar_entrada_no_registrada(
                    conn, it, cantidad, pedida, variacion_id, tenant_id, conteo_ref)
            else:
                # SOBRANTE con error_sistema (delta > 0): ajuste +stock.
                _ajustar_sobrante(conn, it, cantidad, variacion_id, tenant_id, conteo_ref)

            if res == "merma":
                resumen["mermas"] += 1
            elif res == "venta":
                resumen["ventas"] += 1
            elif res == "entrada_no_registrada":
                resumen["entradas"] += 1
            else:
                resumen["ajustes"] += 1
            actualizaciones.append((v, delta, res, it["id"]))

        # ── Orden retroactiva con las ventas declaradas ──
        # Mismo formato de cobro que el POS (migraciones 032/033): folio por
        # trigger, pago en efectivo (el dinero del bazar llegó a la mano) y
        # fecha de la venta declarada para que caiga al día contable correcto.
        if ventas_a_guardar:
            total = round(sum(v["total_venta"] for v in ventas_a_guardar), 2)
            fecha_orden = ventas_a_guardar[0]["fecha"]
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO ordenes (tenant_id, n_ticket, fecha_ts, total, ganancia, cantidad_items, estado, "
                    "metodo_pago, pagos, propina, monto_recibido, cambio, comision_total) "
                    "VALUES (%s, NULL, %s, %s, %s, %s, 'Activa', 'efectivo', %s, 0, %s, 0, 0) "
                    "RETURNING id, n_ticket",
                    (tenant_id, _parsear_ts(fecha_orden), total,
                     round(sum(v["ganancia_bruta"] for v in ventas_a_guardar), 2),
                     round(sum(v["cantidad"] for v in ventas_a_guardar), 3),
                     psycopg2.extras.Json([{
                         "metodo": "efectivo", "monto": total,
                         "referencia": "Declarada en conteo de auditoría",
                         "terminal_id": None, "comision": 0.0,
                     }]),
                     total))
                fila_orden = cur.fetchone()
                orden_id, n_ticket = fila_orden["id"], fila_orden["n_ticket"]
            for v in ventas_a_guardar:
                v["orden_id"] = orden_id
                v["n_ticket"] = n_ticket
                insertar_venta(v, tenant_id, conn=conn)
                registrar_movimiento_inventario(
                    tenant_id, v["producto"], "salida", "venta",
                    -float(v["cantidad"]), id_lote=v.get("id_lote"),
                    referencia_id=str(orden_id),
                    concepto=f"Ticket #{n_ticket} — venta declarada en conteo", conn=conn)
            resumen["venta_total"] = total
            resumen["orden_id"] = str(orden_id)
            resumen["n_ticket"] = n_ticket

        # ── Gasto automático de merma (Fase 3): la pérdida figura en Egresos ──
        # La merma es un egreso real del negocio: se registra como gasto del día
        # contable del cierre, en la categoría "Merma" (auto-creada si no existe).
        # Misma transacción: si algo falla, el gasto no queda huérfano.
        resumen["merma_costo"] = round(resumen["merma_costo"], 2)
        if resumen["merma_costo"] > 0:
            _e(conn,
                "INSERT INTO gastos_categorias (tenant_id, nombre) VALUES (%s, 'Merma') "
                "ON CONFLICT (tenant_id, nombre) DO NOTHING",
                (tenant_id,))
            hoy = hoy_negocio(tenant_id).isoformat()
            fila_gasto = _q(conn, """
                INSERT INTO gastos (Fecha, fecha_negocio, Categoria, Descripcion, Monto, Tenant_ID, Estado)
                VALUES (%s, %s, 'Merma', %s, %s, %s, 'pagado')
                RETURNING id
            """, (hoy, hoy,
                  f"Merma del conteo de auditoría — {resumen['mermas']} producto(s)",
                  resumen["merma_costo"], tenant_id))
            if fila_gasto:
                resumen["gasto_merma_id"] = fila_gasto[0]["id"]

        # ── Persistir el veredicto de cada renglón + el cierre ──
        with conn.cursor() as cur:
            for v, d, res, iid in actualizaciones:
                cur.execute(
                    "UPDATE conteo_items SET vivo = %s, delta = %s, resolucion = %s, "
                    "actualizado_at = now() WHERE id = %s",
                    (v, d, res, iid))
            cur.execute(
                "UPDATE conteos SET estado = 'cerrado', cerrado_at = now(), resumen = %s "
                "WHERE id = %s",
                (psycopg2.extras.Json(resumen), conteo_id))
        conn.commit()
        return {"ok": True, "resumen": resumen}
    except ValueError as e:
        # Error de regla de negocio (resolución inválida, variación borrada…):
        # el router lo traduce a 422 sin dejar nada a medias.
        conn.rollback()
        return {"ok": False, "tipo": "validacion", "mensaje": str(e)}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al cerrar el conteo: {str(e)}"}
    finally:
        release_conn(conn)
