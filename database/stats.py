# ==============================================================================
# backend/database/stats.py
# Agregados para el panel de Estadísticas: aquí la BDD calcula las respuestas
# (SUM/COUNT/GROUP BY) y el frontend recibe números y series, nunca el dump
# de renglones. Las ventanas [desde, hasta) ya llegan resueltas por el router:
#   *_ts  → instantes tz-aware para TIMESTAMPTZ (ventas/ordenes)
#   *_d   → días contables (gastos.fecha_negocio)
# ==============================================================================

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
