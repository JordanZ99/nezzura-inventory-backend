# ==============================================================================
# backend/database/stats.py
# Agregados para el panel de Estadísticas: aquí la BDD calcula las respuestas
# (SUM/COUNT/GROUP BY) y el frontend recibe números y series, nunca el dump
# de renglones. Las ventanas [desde, hasta) ya llegan resueltas por el router:
#   *_ts  → instantes tz-aware para TIMESTAMPTZ (ventas/ordenes)
#   *_d   → días contables (gastos.fecha_negocio)
# ==============================================================================

from datetime import date

from database.conexion import query
from database.helpers import zona_tenant


def _filtro_ventana(desde, hasta, columna: str) -> tuple[str, tuple]:
    """Cláusula AND para ventana [desde, hasta) sobre una columna; None = abierto."""
    sql = ""
    params: list = []
    if desde is not None:
        sql += f" AND {columna} >= %s"
        params.append(desde)
    if hasta is not None:
        sql += f" AND {columna} < %s"
        params.append(hasta)
    return sql, tuple(params)


def _pagos_json(alias: str) -> str:
    """Expresión JSONB segura de o.pagos (NULL o no-array → arreglo vacío)."""
    return (
        f"CASE WHEN {alias}.pagos IS NOT NULL AND jsonb_typeof({alias}.pagos) = 'array' "
        f"THEN {alias}.pagos ELSE '[]'::jsonb END"
    )


# La API habla español ('dia'|'semana'|'mes'); date_trunc exige inglés
_MAP_GRANULARIDAD = {"dia": "day", "semana": "week", "mes": "month"}


def resumen_periodo(
    tenant_id: str,
    desde_ts, hasta_ts,
    desde_d, hasta_d,
    limite_top: int = 10,
) -> dict:
    """
    Respuesta única con los KPIs del período: totales de venta/ganancia,
    gastos, ticket promedio, desglose de cobros por método, propinas,
    depósitos por terminal y top de productos. Todo agregado en SQL.
    """
    f_v, p_v = _filtro_ventana(desde_ts, hasta_ts, "fecha_ts")

    # ── Ventas (renglones) ──
    v = query(
        "SELECT "
        "COUNT(*) FILTER (WHERE estado != 'Inactivo') AS num_ventas, "
        "COUNT(*) FILTER (WHERE estado = 'Inactivo') AS num_anuladas, "
        "COALESCE(SUM(total_venta) FILTER (WHERE estado != 'Inactivo'), 0) AS total_vendido, "
        "COALESCE(SUM(ganancia_bruta) FILTER (WHERE estado != 'Inactivo'), 0) AS ganancia_bruta, "
        "COALESCE(SUM(cantidad) FILTER (WHERE estado != 'Inactivo'), 0) AS unidades "
        "FROM ventas WHERE tenant_id = %s" + f_v,
        (tenant_id,) + p_v
    )[0]

    # ── Gastos (por día contable) ──
    f_g, p_g = _filtro_ventana(desde_d, hasta_d, "fecha_negocio")
    g = query(
        "SELECT COALESCE(SUM(Monto), 0) AS total_gastos "
        "FROM gastos WHERE Tenant_ID = %s" + f_g,
        (tenant_id,) + p_g
    )[0]

    # ── Tickets activos (para ticket promedio) ──
    tickets = query(
        "SELECT COUNT(*) AS tickets FROM ordenes o "
        "WHERE o.tenant_id = %s AND o.estado != 'Anulada'" + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[0],
        (tenant_id,) + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[1]
    )[0]["tickets"]

    # ── Cobros por método (orden activa = cliente suma cada pago) ──
    pg_expr = _pagos_json("o")
    c = query(
        "SELECT "
        "COALESCE(SUM((pg->>'monto')::numeric) FILTER (WHERE pg->>'metodo' = 'efectivo'), 0) AS efectivo, "
        "COALESCE(SUM((pg->>'monto')::numeric) FILTER (WHERE pg->>'metodo' = 'tarjeta_debito'), 0) AS tarjeta_debito, "
        "COALESCE(SUM((pg->>'monto')::numeric) FILTER (WHERE pg->>'metodo' = 'tarjeta_credito'), 0) AS tarjeta_credito, "
        "COALESCE(SUM((pg->>'monto')::numeric) FILTER (WHERE pg->>'metodo' = 'puntos'), 0) AS puntos, "
        "COALESCE(SUM((pg->>'monto')::numeric) FILTER (WHERE pg IS NOT NULL AND COALESCE(pg->>'metodo', '') NOT IN "
        "('efectivo', 'tarjeta_debito', 'tarjeta_credito')), 0) AS otros_metodos "
        "FROM ordenes o LEFT JOIN LATERAL jsonb_array_elements(" + pg_expr + ") AS pg ON true "
        "WHERE o.tenant_id = %s AND o.estado != 'Anulada'" + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[0],
        (tenant_id,) + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[1]
    )[0]

    # ── Legado sin pagos, propinas y bandera con_metodo (sin el JOIN lateral:
    # multiplicaría propinas por número de pagos) ──
    base = query(
        "SELECT "
        "COALESCE(SUM(o.total) FILTER (WHERE o.pagos IS NULL OR jsonb_array_length(" + pg_expr + ") = 0), 0) AS total_legado, "
        "COALESCE(SUM(o.propina), 0) AS propinas, "
        "(COUNT(*) FILTER (WHERE o.metodo_pago IS NOT NULL) > 0) AS con_metodo "
        "FROM ordenes o WHERE o.tenant_id = %s AND o.estado != 'Anulada'" + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[0],
        (tenant_id,) + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[1]
    )[0]

    # ── Desglose por terminal (depósito esperado = cobrado − comisión) ──
    por_terminal = query(
        "SELECT pg->>'terminal_nombre' AS nombre, "
        "COALESCE(SUM((pg->>'monto')::numeric), 0) AS cobrado, "
        "COALESCE(SUM(COALESCE((pg->>'comision')::numeric, 0)), 0) AS comision "
        "FROM ordenes o, LATERAL jsonb_array_elements(" + pg_expr + ") AS pg "
        "WHERE o.tenant_id = %s AND o.estado != 'Anulada' "
        "AND pg->>'terminal_nombre' IS NOT NULL AND COALESCE((pg->>'comision')::numeric, 0) > 0"
        + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[0] +
        " GROUP BY 1 ORDER BY cobrado DESC",
        (tenant_id,) + _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")[1]
    )

    # ── Top productos del período ──
    top = query(
        "SELECT producto, "
        "COALESCE(SUM(total_venta), 0) AS total, "
        "COALESCE(SUM(cantidad), 0) AS unidades, "
        "COALESCE(SUM(ganancia_bruta), 0) AS ganancia, "
        "COUNT(*) AS num_ventas "
        "FROM ventas WHERE tenant_id = %s AND estado != 'Inactivo'" + f_v +
        " GROUP BY producto ORDER BY total DESC LIMIT %s",
        (tenant_id,) + p_v + (limite_top,)
    )

    total_vendido = float(v["total_vendido"])
    tickets_n = int(tickets or 0)
    return {
        "total_vendido": total_vendido,
        "ganancia_bruta": float(v["ganancia_bruta"]),
        "unidades": float(v["unidades"]),
        "num_ventas": int(v["num_ventas"]),
        "num_anuladas": int(v["num_anuladas"]),
        "tickets": tickets_n,
        "ticket_promedio": (total_vendido / tickets_n) if tickets_n > 0 else 0.0,
        "total_gastos": float(g["total_gastos"]),
        "cobros_por_metodo": {
            "efectivo": float(c["efectivo"]),
            "tarjeta_debito": float(c["tarjeta_debito"]),
            "tarjeta_credito": float(c["tarjeta_credito"]),
            "puntos": float(c["puntos"]),
            "no_registrado": float(c["otros_metodos"]) + float(base["total_legado"]),
        },
        "propinas": float(base["propinas"]),
        "con_metodo": bool(base["con_metodo"]),
        "por_terminal": [
            {"nombre": t["nombre"], "cobrado": float(t["cobrado"]), "comision": float(t["comision"])}
            for t in por_terminal
        ],
        "top_productos": [
            {
                "producto": p["producto"],
                "total": float(p["total"]),
                "unidades": float(p["unidades"]),
                "ganancia": float(p["ganancia"]),
                "num_ventas": int(p["num_ventas"]),
            }
            for p in top
        ],
    }


def serie_periodo(tenant_id: str, granularidad: str, desde_ts, hasta_ts, desde_d, hasta_d) -> list[dict]:
    """
    Serie temporal [{periodo, ventas, ganancia, gastos}] en cubos de
    granularidad ('dia' | 'semana' | 'mes') elegida por el router.     Ventas se
    agrupa por el día contable del tenant (fecha_ts AT TIME ZONE zona);
    gastos por fecha_negocio. El frontend nunca ve renglones.
    """
    # granularidad ya viene validada/whitelisted por el router
    gran_sql = _MAP_GRANULARIDAD[granularidad]
    tz = zona_tenant(tenant_id).key

    f_v, p_v = _filtro_ventana(desde_ts, hasta_ts, "fecha_ts")
    filas_ventas = query(
        "SELECT date_trunc('" + gran_sql + "', fecha_ts AT TIME ZONE %s) AS bucket, "
        "COALESCE(SUM(total_venta) FILTER (WHERE estado != 'Inactivo'), 0) AS ventas, "
        "COALESCE(SUM(ganancia_bruta) FILTER (WHERE estado != 'Inactivo'), 0) AS ganancia "
        "FROM ventas WHERE tenant_id = %s" + f_v + " GROUP BY 1",
        (tz, tenant_id) + p_v
    )

    f_g, p_g = _filtro_ventana(desde_d, hasta_d, "fecha_negocio")
    filas_gastos = query(
        "SELECT date_trunc('" + gran_sql + "', fecha_negocio::timestamp) AS bucket, "
        "COALESCE(SUM(Monto), 0) AS gastos "
        "FROM gastos WHERE Tenant_ID = %s" + f_g + " GROUP BY 1",
        (tenant_id,) + p_g
    )

    por_bucket: dict[str, dict] = {}
    for r in filas_ventas:
        e = por_bucket.setdefault(r["bucket"].date().isoformat(), {"ventas": 0.0, "ganancia": 0.0, "gastos": 0.0})
        e["ventas"] = float(r["ventas"])
        e["ganancia"] = float(r["ganancia"])
    for r in filas_gastos:
        e = por_bucket.setdefault(r["bucket"].date().isoformat(), {"ventas": 0.0, "ganancia": 0.0, "gastos": 0.0})
        e["gastos"] = float(r["gastos"])

    return [{"periodo": k, **v} for k, v in sorted(por_bucket.items())]


def stats_productos_periodo(tenant_id: str, desde_ts, hasta_ts) -> list[dict]:
    """Un renglón por producto vendido en el período (para el catálogo de
    estadísticas y las gráficas de contribución marginal)."""
    f_v, p_v = _filtro_ventana(desde_ts, hasta_ts, "fecha_ts")
    filas = query(
        "SELECT producto, "
        "COALESCE(SUM(total_venta), 0) AS total, "
        "COALESCE(SUM(cantidad), 0) AS unidades, "
        "COALESCE(SUM(ganancia_bruta), 0) AS ganancia, "
        "COUNT(*) AS num_ventas "
        "FROM ventas WHERE tenant_id = %s AND estado != 'Inactivo'" + f_v +
        " GROUP BY producto ORDER BY total DESC",
        (tenant_id,) + p_v
    )
    return [
        {
            "producto": p["producto"],
            "total": float(p["total"]),
            "unidades": float(p["unidades"]),
            "ganancia": float(p["ganancia"]),
            "num_ventas": int(p["num_ventas"]),
        }
        for p in filas
    ]


def ventas_producto_periodo(tenant_id: str, producto: str, desde_ts, hasta_ts) -> list[dict]:
    """Renglones de venta de UN producto en el período (modal de detalle).
    Mismas columnas que get_ventas para respetar el contrato del frontend."""
    f_v, p_v = _filtro_ventana(desde_ts, hasta_ts, "fecha_ts")
    return query(
        "SELECT id, n_ticket, fecha, producto, cantidad, precio_lista, precio_real, "
        "costo_unitario, total_venta, ganancia_bruta, estado, tipo_producto, variacion, consumo, orden_id "
        "FROM ventas WHERE tenant_id = %s AND producto = %s" + f_v +
        " ORDER BY fecha_ts DESC",
        (tenant_id, producto) + p_v
    )


def resumen_clientes(tenant_id: str, desde_ts, hasta_ts, granularidad: str = "dia") -> dict:
    """
    KPIs de la cartera de clientes y del programa de puntos (tab "Clientes"
    de Estadísticas). Todo agregado en SQL; el saldo/pasivo es SUM del ledger.
    Incluye sala de honor (top_clientes) y serie temporal de puntos vs ventas
    para ver cómo INFLUYE el programa de fidelidad en las ventas.
    """
    # ── Tickets: identificadas vs sin cliente + ticket promedio comparado ──
    f_o, p_o = _filtro_ventana(desde_ts, hasta_ts, "o.fecha_ts")
    tickets = query(
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE o.cliente_id IS NOT NULL) AS identificadas, "
        "COALESCE(AVG(o.total) FILTER (WHERE o.cliente_id IS NOT NULL), 0) AS ticket_con_cliente, "
        "COALESCE(AVG(o.total) FILTER (WHERE o.cliente_id IS NULL), 0) AS ticket_sin_cliente "
        "FROM ordenes o WHERE o.tenant_id = %s AND o.estado != 'Anulada'" + f_o,
        (tenant_id,) + p_o
    )[0]

    # ── Movimientos de puntos del período, por tipo ──
    # Se excluye la actividad de TICKETS ANULADOS: sus movimientos originales y
    # sus reversas se anulan entre sí en el saldo, pero sin este filtro
    # inflarían los KPIs "otorgados"/"canjeados" del período.
    f_p, p_p = _filtro_ventana(desde_ts, hasta_ts, "pm.fecha")
    movs = query(
        "SELECT pm.tipo, COALESCE(SUM(pm.puntos), 0) AS puntos, COUNT(*) AS movimientos "
        "FROM puntos_movimientos pm LEFT JOIN ordenes o ON o.id = pm.orden_id "
        "WHERE pm.tenant_id = %s AND (pm.orden_id IS NULL OR o.estado != 'Anulada')" + f_p + " GROUP BY pm.tipo",
        (tenant_id,) + p_p
    )
    por_tipo = {m["tipo"]: float(m["puntos"]) for m in movs}

    # ── Saldo total en circulación (histórico) y valor $ del pasivo ──
    # Mismo criterio: los tickets anulados no cuentan (sus reversas del ledger
    # lo harían neto-cero, pero así el pasivo refleja solo actividad real).
    saldo = float(query(
        "SELECT COALESCE(SUM(pm.puntos), 0) AS saldo FROM puntos_movimientos pm "
        "LEFT JOIN ordenes o ON o.id = pm.orden_id "
        "WHERE pm.tenant_id = %s AND (pm.orden_id IS NULL OR o.estado != 'Anulada')",
        (tenant_id,)
    )[0]["saldo"] or 0)
    config = query(
        "SELECT puntos_valor_punto FROM tenants WHERE id = %s", (tenant_id,)
    )
    valor_punto = float(config[0]["puntos_valor_punto"]) if config else 1.0

    # ── Clientes: totales, activos, nuevos del período, recurrentes ──
    # Ventana explícita ([desde, hasta) con hasta opcional) sobre fecha_registro.
    filt_nuevos = " AND fecha_registro >= %s"
    params_nuevos: list = [desde_ts if desde_ts is not None else date(1970, 1, 1)]
    if hasta_ts is not None:
        filt_nuevos += " AND fecha_registro < %s"
        params_nuevos.append(hasta_ts)
    clientes = query(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE activo) AS activos, "
        "COUNT(*) FILTER (WHERE true" + filt_nuevos + ") AS nuevos_periodo "
        "FROM clientes WHERE tenant_id = %s",
        (*params_nuevos, tenant_id)
    )[0]

    recurrentes = int(query(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT cliente_id FROM ordenes"
        "  WHERE tenant_id = %s AND estado != 'Anulada' AND cliente_id IS NOT NULL"
        "  GROUP BY cliente_id HAVING COUNT(*) > 1"
        ") r",
        (tenant_id,)
    )[0]["n"] or 0)

    identificadas = int(tickets["identificadas"] or 0)
    total_tickets = int(tickets["total"] or 0)

    # ── Top clientes (por gasto del período) ──
    top = query(
        "SELECT c.id, c.nombre, c.telefono, c.email, "
        "COUNT(o.id) AS compras, "
        "COALESCE(SUM(o.total), 0) AS total_gastado, "
        "MAX(o.fecha_ts) AS ultima_compra "
        "FROM clientes c JOIN ordenes o ON o.cliente_id = c.id AND o.estado != 'Anulada' "
        "WHERE c.tenant_id = %s" + f_o +
        " GROUP BY c.id, c.nombre, c.telefono, c.email "
        "ORDER BY total_gastado DESC LIMIT 10",
        (tenant_id,) + p_o
    )
    # Saldo neto (sin tickets anulados) para cada cliente del top
    saldos: dict[str, float] = {}
    if top:
        ids_top = [str(t["id"]) for t in top]
        filas_saldo = query(
            "SELECT pm.cliente_id, COALESCE(SUM(pm.puntos), 0) AS s "
            "FROM puntos_movimientos pm LEFT JOIN ordenes o ON o.id = pm.orden_id "
            "WHERE pm.tenant_id = %s AND (pm.orden_id IS NULL OR o.estado != 'Anulada') "
            "AND pm.cliente_id = ANY(%s::uuid[])"
            " GROUP BY pm.cliente_id",
            (tenant_id, ids_top)
        )
        saldos = {str(r["cliente_id"]): float(r["s"] or 0) for r in filas_saldo}

    # ── Serie temporal (granularidad resolveida por el router) ──
    gran_sql = _MAP_GRANULARIDAD.get(granularidad, _MAP_GRANULARIDAD["dia"])
    tz = zona_tenant(tenant_id).key
    # Ventas: tickets por cubo, con cliente vs sin cliente
    filas_ventas = query(
        "SELECT date_trunc('" + gran_sql + "', o.fecha_ts AT TIME ZONE %s) AS bucket, "
        "COUNT(*) AS tickets, "
        "COUNT(*) FILTER (WHERE o.cliente_id IS NOT NULL) AS identificadas, "
        "COALESCE(SUM(o.total), 0) AS total_venta "
        "FROM ordenes o WHERE o.tenant_id = %s AND o.estado != 'Anulada'" + f_o +
        " GROUP BY 1",
        (tz, tenant_id) + p_o
    )
    # Puntos del período en cubos (mismo filtro anti-anulados del KPI)
    filas_puntos = query(
        "SELECT date_trunc('" + gran_sql + "', pm.fecha AT TIME ZONE %s) AS bucket, "
        "pm.tipo, COALESCE(SUM(pm.puntos), 0) AS puntos "
        "FROM puntos_movimientos pm LEFT JOIN ordenes o ON o.id = pm.orden_id "
        "WHERE pm.tenant_id = %s AND (pm.orden_id IS NULL OR o.estado != 'Anulada')" + f_p +
        " GROUP BY 1, pm.tipo",
        (tz, tenant_id) + p_p
    )
    serie: dict[str, dict] = {}
    for r in filas_ventas:
        e = serie.setdefault(r["bucket"].date().isoformat(), {
            "otorgados": 0.0, "canjeados": 0.0, "tickets": 0, "identificadas": 0, "total_venta": 0.0,
        })
        e["tickets"] = int(r["tickets"])
        e["identificadas"] = int(r["identificadas"])
        e["total_venta"] = round(float(r["total_venta"] or 0), 2)
    for r in filas_puntos:
        b = r["bucket"].date().isoformat()
        e = serie.setdefault(b, {"otorgados": 0.0, "canjeados": 0.0, "tickets": 0, "identificadas": 0, "total_venta": 0.0})
        pts = float(r["puntos"] or 0)
        if r["tipo"] == "ganados":
            e["otorgados"] = pts
        elif r["tipo"] == "canjeados":
            e["canjeados"] = abs(pts)

    return {
        "clientes_total": int(clientes["total"] or 0),
        "clientes_activos": int(clientes["activos"] or 0),
        "clientes_nuevos_periodo": int(clientes["nuevos_periodo"] or 0),
        "clientes_recurrentes": recurrentes,
        "pct_recurrentes": (recurrentes / int(clientes["total"] or 1) * 100) if clientes["total"] else 0.0,
        # ── Puntos del período ──
        "puntos_otorgados": por_tipo.get("ganados", 0.0),
        "puntos_canjeados": abs(por_tipo.get("canjeados", 0.0)),
        "puntos_ajustados": por_tipo.get("ajuste", 0.0),
        "pct_canjeados": (abs(por_tipo.get("canjeados", 0.0)) / por_tipo.get("ganados", 0.0) * 100) if por_tipo.get("ganados", 0.0) > 0 else 0.0,
        # ── Pasivo: saldo en circulación que el negocio deberá aceptar ──
        "saldo_puntos": saldo,
        "valor_punto": valor_punto,
        "pasivo_monetario": saldo * valor_punto,
        # ── Influencia en las ventas ──
        "tickets_total": total_tickets,
        "tickets_identificadas": identificadas,
        "pct_identificadas": (identificadas / total_tickets * 100) if total_tickets else 0.0,
        "ticket_con_cliente": float(tickets["ticket_con_cliente"] or 0),
        "ticket_sin_cliente": float(tickets["ticket_sin_cliente"] or 0),
        "diferencia_ticket": float(tickets["ticket_con_cliente"] or 0) - float(tickets["ticket_sin_cliente"] or 0),
        # ── Analítica de la Fase C ──
        "top_clientes": [
            {
                "id": str(t["id"]),
                "nombre": t["nombre"],
                "telefono": t.get("telefono"),
                "email": t.get("email"),
                "compras": int(t["compras"] or 0),
                "total_gastado": round(float(t["total_gastado"] or 0), 2),
                "saldo_puntos": int(saldos.get(str(t["id"]), 0)),
                "ultima_compra": t["ultima_compra"],
            }
            for t in top
        ],
        "serie": [{"periodo": k, **v} for k, v in sorted(serie.items())],
    }
