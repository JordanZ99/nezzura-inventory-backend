# ==============================================================================
# backend/database/analitica.py
# Agregados de solo lectura para el Análisis Inteligente. Toda fecha sale de
# las columnas canónicas de la migración 031 (fecha_ts / fecha_negocio), nunca
# del TEXT legado. Cero escrituras.
#
# Latencia: la BD está a ~175 ms por round trip, así que el costo dominante son
# los viajes, no el cálculo. Por eso las métricas van agrupadas en pocas
# sentencias (7 en total) y se lanzan en paralelo sobre el pool de conexiones.
# ==============================================================================

from datetime import date, datetime, timedelta

from database.conexion import query
from database.helpers import hoy_negocio, ventana_ts_de_rango, zona_tenant

_DIAS = ("domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado")
_ACTIVO = "estado != 'Inactivo'"
_VENTAS_COLD_START = 20


def _f(valor) -> float:
    return float(valor or 0)


def _i(valor) -> int:
    return int(valor or 0)


def _ventana(columna: str, desde, hasta) -> tuple[str, tuple]:
    """Cláusula AND de la ventana [desde, hasta) — 'hasta' exclusivo."""
    sql = ""
    params: list = []
    if desde is not None:
        sql += f" AND {columna} >= %s"
        params.append(desde)
    if hasta is not None:
        sql += f" AND {columna} < %s"
        params.append(hasta)
    return sql, tuple(params)


def _gasto_real() -> str:
    """Gasto que ya salió de caja: excluye pendientes y descartados."""
    return "lower(COALESCE(estado, '')) NOT IN ('pendiente', 'descartado')"


def _pendiente() -> str:
    return "lower(COALESCE(estado, '')) = 'pendiente'"


def _q_todo(
    tenant_id, zona, f_v, p_v, f_g, p_g,
    hace_90, hace_56, mes_ts_desde, mes_ts_hasta, mes_d, mes_h,
) -> dict:
    """TODAS las métricas en UN solo viaje a la base de datos.

    Cada sección es una subconsulta que devuelve JSONB y se lee del único
    renglón de resultado. Motivo (medido): a ~175 ms por viaje, 8 consultas
    costaban ~1.5 s; y abrir 8 conexiones en paralelo salía PEOR (~2 s),
    porque Supabase no retiene conexiones ociosas y cada handshake cuesta
    ~500 ms, con el pool serializando la apertura.
    """
    fila = query(
        f"""
        SELECT
        (SELECT row_to_json(x) FROM (
            SELECT
              COUNT(*) FILTER (WHERE {_ACTIVO}) AS num_ventas,
              COALESCE(SUM(total_venta) FILTER (WHERE {_ACTIVO}), 0) AS ingresos,
              COALESCE(SUM(ganancia_bruta) FILTER (WHERE {_ACTIVO}), 0) AS ganancia,
              COALESCE(MAX(total_venta) FILTER (WHERE {_ACTIVO}), 0) AS ticket_max,
              COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY total_venta)
                       FILTER (WHERE {_ACTIVO}), 0) AS mediana,
              COALESCE(SUM(total_venta) FILTER (WHERE {_ACTIVO}
                       AND fecha_ts >= %s AND fecha_ts < %s), 0) AS mes_ingresos,
              COUNT(*) FILTER (WHERE {_ACTIVO}
                       AND EXTRACT(HOUR FROM fecha_ts AT TIME ZONE %s) < 6) AS nocturnas,
              MIN((fecha_ts AT TIME ZONE %s)::date)::text AS desde,
              MAX((fecha_ts AT TIME ZONE %s)::date)::text AS hasta,
              (SELECT COUNT(*) FROM ventas WHERE tenant_id = %s AND {_ACTIVO}) AS ventas_totales
            FROM ventas WHERE tenant_id = %s{f_v}
        ) x) AS ventas,

        (SELECT row_to_json(x) FROM (
            SELECT
              COALESCE(SUM(monto) FILTER (WHERE {_gasto_real()}{f_g}), 0) AS gastos,
              COALESCE(SUM(monto) FILTER (WHERE {_pendiente()}{f_g}), 0) AS pend_monto,
              COUNT(*) FILTER (WHERE {_pendiente()}{f_g}) AS pend_n,
              COALESCE(SUM(monto) FILTER (WHERE {_gasto_real()}
                       AND fecha_negocio >= %s AND fecha_negocio < %s), 0) AS gastos_mes
            FROM gastos WHERE tenant_id = %s
        ) x) AS gastos,

        (SELECT COALESCE(jsonb_agg(to_json(t)), '[]'::jsonb) FROM (
            SELECT EXTRACT(DOW FROM fecha_ts AT TIME ZONE %s)::int AS dow,
                   COALESCE(SUM(total_venta), 0) AS ingresos
            FROM ventas WHERE tenant_id = %s AND {_ACTIVO}{f_v} GROUP BY 1
        ) t) AS dow,

        (SELECT COALESCE(jsonb_agg(to_json(t)), '[]'::jsonb) FROM (
            SELECT date_trunc('week', fecha_ts AT TIME ZONE %s)::date AS semana,
                   COALESCE(SUM(total_venta), 0) AS ingresos
            FROM ventas WHERE tenant_id = %s AND {_ACTIVO} AND fecha_ts >= %s
            GROUP BY 1
        ) t) AS semanas,

        (SELECT row_to_json(x) FROM (
            SELECT to_char(date_trunc('month', fecha_ts AT TIME ZONE %s), 'YYYY-MM') AS mes,
                   COALESCE(SUM(total_venta), 0) AS ingresos
            FROM ventas WHERE tenant_id = %s AND {_ACTIVO}{f_v}
            GROUP BY 1 ORDER BY ingresos DESC LIMIT 1
        ) x) AS mes_pico,

        (SELECT COALESCE(jsonb_agg(to_json(t)), '[]'::jsonb) FROM (
            SELECT categoria, COALESCE(SUM(monto), 0) AS monto
            FROM gastos WHERE tenant_id = %s AND {_gasto_real()}{f_g} GROUP BY categoria
        ) t) AS categorias,

        (SELECT COALESCE(jsonb_agg(to_json(t)), '[]'::jsonb) FROM (
            SELECT 'lote' AS tipo, p.producto FROM lotes l
              JOIN productos p ON p.id = l.producto_id AND p.tenant_id = l.tenant_id
            WHERE l.tenant_id = %s AND l.estado = 'Activo'
              AND (l.stock_lote < 0 OR l.costo < 0)
            GROUP BY p.producto
            UNION
            SELECT 'sin_lote' AS tipo, v.producto FROM ventas v
              JOIN productos p ON p.tenant_id = v.tenant_id AND p.producto = v.producto
            WHERE v.tenant_id = %s AND v.estado != 'Inactivo'
              AND (v.id_lote IS NULL OR v.id_lote = '')
              AND p.es_generico IS NOT TRUE
              AND COALESCE(p.tipo_producto, 'stock') = 'stock'{f_v}
            LIMIT 10
        ) t) AS alertas,

        (SELECT COALESCE(jsonb_agg(to_json(t)), '[]'::jsonb) FROM (
            SELECT p.id AS producto_id, p.producto,
              COALESCE(SUM(l.stock_lote) FILTER (WHERE l.estado = 'Activo'), 0) AS stock,
              COALESCE(SUM(l.costo * l.stock_lote) FILTER (WHERE l.estado = 'Activo'
                       AND l.stock_lote > 0), 0) AS valor_costo,
              COALESCE(v.ingresos, 0) AS ingresos,
              COALESCE(v.ganancia, 0) AS ganancia,
              COALESCE(v.unidades, 0) AS unidades
            FROM productos p
            LEFT JOIN lotes l ON l.producto_id = p.id AND l.tenant_id = p.tenant_id
            LEFT JOIN (
              SELECT producto, SUM(total_venta) AS ingresos, SUM(ganancia_bruta) AS ganancia,
                     SUM(cantidad) AS unidades
              FROM ventas WHERE tenant_id = %s AND estado != 'Inactivo' AND fecha_ts >= %s
              GROUP BY producto
            ) v ON v.producto = p.producto
            WHERE p.tenant_id = %s AND p.estado = 'Activo' AND p.es_generico IS NOT TRUE
              AND COALESCE(p.tipo_producto, 'stock') = 'stock'
            GROUP BY p.id, p.producto, v.ingresos, v.ganancia, v.unidades
        ) t) AS productos,

        (SELECT COALESCE(SUM(l2.costo * l2.stock_lote), 0) FROM lotes l2
           JOIN productos p2 ON p2.id = l2.producto_id AND p2.tenant_id = l2.tenant_id
         WHERE l2.tenant_id = %s AND l2.estado = 'Activo' AND l2.stock_lote > 0
           AND p2.estado = 'Activo' AND p2.es_generico IS NOT TRUE
           AND COALESCE(p2.tipo_producto, 'stock') = 'stock') AS valor_inventario
        """,
        # Los params van en el ORDEN EN QUE APARECEN los %s dentro del SQL:
        (mes_ts_desde, mes_ts_hasta, zona, zona, zona, tenant_id, tenant_id) + p_v          # ventas
        + p_g * 3 + (mes_d, mes_h, tenant_id)                                            # gastos (3 filtros)
        + (zona, tenant_id) + p_v                                                          # dow
        + (zona, tenant_id, hace_56)                                                       # semanas
        + (zona, tenant_id) + p_v                                                          # mes_pico
        + (tenant_id,) + p_g                                                               # categorias
        + (tenant_id, tenant_id) + p_v                                                     # alertas
        + (tenant_id, hace_90, tenant_id)                                                  # productos
        + (tenant_id,)                                                                     # valor_inventario
    )
    return fila[0] if fila else {}


def _meses_cubiertos(desde: date, hasta: date) -> int:
    """Cuántos meses calendario toca el rango (inclusivo)."""
    if hasta < desde:
        return 0
    return (hasta.year - desde.year) * 12 + (hasta.month - desde.month) + 1


def _a_fecha(valor) -> date | None:
    """Convierte 'YYYY-MM-DD' (como lo devuelve el SQL) a date; None si no viene."""
    if isinstance(valor, date):
        return valor
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None



def generar(tenant_id: str, desde_ts, hasta_ts, desde_d: date | None, hasta_d: date | None) -> dict:
    """Métricas + hechos del período. Las reglas viven en analisis/reglas.py."""
    tz = zona_tenant(tenant_id)
    zona = tz.key
    hoy = hoy_negocio(tenant_id)
    hace_90 = datetime.combine(hoy - timedelta(days=90), datetime.min.time(), tzinfo=tz)
    hace_56 = datetime.combine(hoy - timedelta(days=56), datetime.min.time(), tzinfo=tz)
    inicio_mes = hoy.replace(day=1)
    fin_mes = date(hoy.year + 1, 1, 1) if hoy.month == 12 else date(hoy.year, hoy.month + 1, 1)
    mes_ts_desde, mes_ts_hasta = ventana_ts_de_rango(tenant_id, inicio_mes, fin_mes)
    mes_d, mes_h = inicio_mes, fin_mes

    f_v, p_v = _ventana("fecha_ts", desde_ts, hasta_ts)
    f_g, p_g = _ventana("fecha_negocio", desde_d, hasta_d)

    r = _q_todo(
        tenant_id, zona, f_v, p_v, f_g, p_g,
        hace_90, hace_56, mes_ts_desde, mes_ts_hasta, mes_d, mes_h,
    )

    v = r.get("ventas") or {}
    g = r.get("gastos") or {}
    dow = r.get("dow") or []
    pico = r.get("mes_pico") or {}
    productos = r.get("productos") or []
    alertas = r.get("alertas") or []
    categorias = r.get("categorias") or []
    semanas = r.get("semanas") or []

    num = _i(v["num_ventas"])
    ingresos = _f(v["ingresos"])
    ganancia = _f(v["ganancia"])
    gastos = _f(g["gastos"])
    mejor_dow = max(dow, key=lambda x: _f(x["ingresos"]), default=None)

    dias_mes = (fin_mes - inicio_mes).days
    dias_pasados = max(1, (hoy - inicio_mes).days + 1)
    proyeccion = _f(v["mes_ingresos"]) / dias_pasados * dias_mes

    valor_inventario = _f(r.get("valor_inventario"))
    foto = [
        {
            "producto_id": p["producto_id"],
            "producto": p["producto"],
            "stock": _f(p["stock"]),
            "valor_costo": _f(p["valor_costo"]),
            "ingresos": _f(p["ingresos"]),
            "ganancia": _f(p["ganancia"]),
            "unidades": _f(p["unidades"]),
        }
        for p in productos
    ]
    # Métrica = TODO el dinero dormido (sin movimiento en 90 días). El umbral de
    # $100 por producto vive en la REGLA (qué vale la pena nombrar), no aquí.
    muertos = [p for p in foto if p["ingresos"] <= 0 and p["valor_costo"] > 0]
    cold_start = _i(v["ventas_totales"]) < _VENTAS_COLD_START

    # Últimas 8 semanas COMPLETAS (la semana en curso no cuenta: siempre está
    # a medias y fingiría una caída). Alineadas a lunes.
    por_semana = {x["semana"]: _f(x["ingresos"]) for x in semanas}
    lunes_pasado = hoy - timedelta(days=hoy.weekday() + 7)
    semanas_8 = [por_semana.get(lunes_pasado - timedelta(weeks=i), 0.0) for i in range(7, -1, -1)]

    # El extremo real de los datos (viene como texto ISO desde el SQL) acota el
    # rango mostrado: si el tenant analyze "todo" pero solo tiene datos de julio,
    # el encabezado dice julio, no "desde el principio".
    desde_rango = _a_fecha(v.get("desde")) or desde_d or hoy
    hasta_rango = _a_fecha(v.get("hasta")) or (hasta_d - timedelta(days=1) if hasta_d else hoy)
    meses = _meses_cubiertos(desde_rango, hasta_rango)

    return {
        "generado_en": datetime.now(tz).isoformat(timespec="seconds"),
        "zona_horaria": zona,
        "rango_analizado": {"desde": desde_rango.isoformat(), "hasta": hasta_rango.isoformat()},
        "cold_start": bool(cold_start),
        "mensaje": "Registra 2 semanas de ventas para desbloquear el análisis" if cold_start else "",
        "metricas": {
            "ingresos_total": round(ingresos, 2),
            "ganancia_bruta_total": round(ganancia, 2),
            "margen_bruto_pct": round(ganancia / ingresos * 100, 1) if ingresos else 0,
            "gastos_total": round(gastos, 2),
            "utilidad_neta": round(ganancia - gastos, 2),
            "margen_neto_pct": round((ganancia - gastos) / ingresos * 100, 1) if ingresos else 0,
            "ticket_promedio": round(ingresos / num, 2) if num else 0,
            "ticket_mediano": round(_f(v["mediana"]), 2),
            "num_ventas": num,
            "mejor_dia": {
                "dia": _DIAS[mejor_dow["dow"]] if mejor_dow else "",
                "ingresos": round(_f(mejor_dow["ingresos"]), 2) if mejor_dow else 0,
            },
            "mejor_mes": {
                "mes": pico["mes"] or "",
                "ingresos": round(_f(pico["ingresos"]), 2),
            },
            "proyeccion_mes_actual": round(proyeccion, 2),
            "valor_inventario_costo": round(valor_inventario, 2),
            "valor_stock_muerto_costo": round(sum(p["valor_costo"] for p in muertos), 2),
        },
        "hechos": {
            "cold_start": bool(cold_start),
            "hoy": hoy,
            "num_ventas": num,
            # Cuántos meses distintos cubre el rango: si es 1, "tu mejor mes"
            # no aporta nada (se compara consigo mismo) y la regla se omite.
            "meses_analizados": meses,
            "ticket_promedio": (ingresos / num) if num else 0,
            "ticket_mediano": _f(v["mediana"]),
            "valor_inventario_costo": valor_inventario,
            "mejor_mes": {"mes": pico["mes"] or "", "ingresos": _f(pico["ingresos"])},
            "proyeccion_mes_actual": proyeccion,
            "productos_90": foto,
            "semanas_8": semanas_8,
            "ingresos_por_dow": [{"dia": _DIAS[x["dow"]], "ingresos": _f(x["ingresos"])} for x in dow],
            "gastos_por_categoria": [
                {"categoria": c.get("categoria") or "", "monto": _f(c.get("monto"))} for c in categorias
            ],
            "pendientes_n": _i(g.get("pend_n")),
            "pendientes_monto": _f(g.get("pend_monto")),
            "gastos_mes": _f(g.get("gastos_mes")),
            "lotes_inconsistentes": [
                {"producto": a["producto"]} for a in alertas if a["tipo"] == "lote"
            ],
            "vendidos_sin_lote": [a["producto"] for a in alertas if a["tipo"] == "sin_lote"],
            "ventas_nocturnas": _i(v.get("nocturnas")),
        },
    }
