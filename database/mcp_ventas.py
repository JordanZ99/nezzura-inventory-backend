# ==============================================================================
# backend/database/mcp_ventas.py
# Tools de VENTAS para claves de nivel 2 (solo lectura): ventas_recientes y
# top_vendidos. Cierran lo que el análisis agregado no da: la lista venta por
# venta ("qué se vendió ayer") y el ranking ("qué repetir").
#
# Reutiliza los aliases de periodo y el chequeo de plan Plus del motor de
# análisis (database/mcp_analisis.py): mismas reglas, mismo mensaje de error.
#
# Privacidad (migrations/068): el costo unitario y la ganancia de CADA venta
# son datos internos — solo viajan si la clave tiene el permiso ver_costos.
# Sin el permiso, las columnas ni siquiera se seleccionan (misma defensa a
# nivel SQL que el catálogo).
# ==============================================================================

from database import mcp_analisis
from database.analitica import _ACTIVO
from database.conexion import query
from database.helpers import ventana_ts_de_rango
from database.imagenes import _get_tenant_plan

_TOPE_PAGINA = 50


def _num(valor) -> float:
    try:
        return round(float(valor or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _stock(valor) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _ventana(tenant_id: str, periodo: str) -> tuple:
    """(desde_ts, hasta_ts) del periodo normalizado. Exige plan Plus."""
    if _get_tenant_plan(tenant_id) != "plus":
        raise ValueError("El análisis del negocio es exclusivo del plan Plus.")
    periodo = mcp_analisis._normalizar_periodo(periodo)
    if periodo not in mcp_analisis.PERIODOS:
        raise ValueError(
            f"Periodo inválido: '{periodo}'. Usa 'mes' (mes contable actual), "
            "'30dias', '90dias' o 'todo'."
        )
    desde_d, hasta_d = mcp_analisis._rango_periodo(tenant_id, periodo)
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, desde_d, hasta_d)
    return periodo, desde_ts, hasta_ts


# ── Tool: ventas_recientes ───────────────────────────────────────────────────

def ventas_recientes(
    tenant_id: str, periodo: str = "30dias", pagina: int = 1, limite: int = 20,
    con_costos: bool = False,
) -> dict:
    """
    Venta por venta, la más nueva primero. columna_costo: sin el permiso
    ver_costos, costo_unitario y ganancia NO se seleccionan de la base.
    """
    periodo, desde_ts, hasta_ts = _ventana(tenant_id, periodo)
    limite = max(1, min(int(limite or 20), _TOPE_PAGINA))
    pagina = max(1, int(pagina or 1))

    filtros = ["v.tenant_id = %s", _ACTIVO.replace("estado", "v.Estado")]
    params: list = [tenant_id]
    if desde_ts is not None:
        filtros.append("v.fecha_ts >= %s")
        params.append(desde_ts)
    if hasta_ts is not None:
        filtros.append("v.fecha_ts < %s")
        params.append(hasta_ts)
    where = " AND ".join(filtros)

    # La columna Estado es minúscula en producción; el replace de arriba la
    # normaliza (estado → v.Estado) sin romper el literal 'Inactivo'.
    total = query(
        f"SELECT COUNT(*) AS c FROM ventas v WHERE {where}",
        tuple(params),
    )[0]["c"]

    # Sin el permiso, costo y ganancia ni siquiera salen de la base (068).
    cols_costo = "v.Costo_Unitario AS costo_unitario, v.Ganancia_Bruta AS ganancia," if con_costos else ""
    filas = query(
        f"""
        SELECT v.id, v.n_ticket, v.fecha_ts, v.Fecha AS fecha_texto,
               v.Producto AS producto, v.Cantidad AS cantidad,
               v.Precio_Real AS precio_real, v.Total_Venta AS total,
               {cols_costo}
               v.tipo_producto, v.variacion
        FROM ventas v
        WHERE {where}
        ORDER BY v.fecha_ts DESC, v.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params + [limite, (pagina - 1) * limite]),
    )

    ventas = []
    for f in filas:
        item = {
            "id": f["id"],
            "ticket": f.get("n_ticket"),
            "fecha": f.get("fecha_ts").isoformat() if f.get("fecha_ts") else f.get("fecha_texto"),
            "producto": f.get("producto"),
            "variacion": f.get("variacion") or None,
            "cantidad": _stock(f.get("cantidad")),
            "precio_unitario": _num(f.get("precio_real")),
            "total": _num(f.get("total")),
            "tipo": f.get("tipo_producto") or "stock",
        }
        if con_costos:
            item["costo_unitario"] = _num(f.get("costo_unitario"))
            item["ganancia"] = _num(f.get("ganancia"))
        ventas.append(item)

    return {
        "periodo": periodo,
        "total": int(total),
        "pagina": pagina,
        "paginas": max(1, -(-int(total) // limite)),
        "limite": limite,
        "ventas": ventas,
    }


# ── Tool: top_vendidos ───────────────────────────────────────────────────────

def top_vendidos(
    tenant_id: str, periodo: str = "30dias", limite: int = 10,
    con_costos: bool = False,
) -> dict:
    """
    Ranking por ingresos del periodo, con stock ACTUAL de cada producto para
    detectar best sellers agotados o por agotarse (lo que el dueño quiere
    saber: qué repetir y qué reponer antes de volver a publicar).
    """
    periodo, desde_ts, hasta_ts = _ventana(tenant_id, periodo)
    limite = max(1, min(int(limite or 10), 50))

    filtros = ["v.tenant_id = %s", _ACTIVO.replace("estado", "v.Estado")]
    params: list = [tenant_id]
    if desde_ts is not None:
        filtros.append("v.fecha_ts >= %s")
        params.append(desde_ts)
    if hasta_ts is not None:
        filtros.append("v.fecha_ts < %s")
        params.append(hasta_ts)

    # Ranking por ventas SOLO (sin joins que multipliquen), y el stock actual
    # se une DESPUÉS con un lateral (una fila por producto, sin duplicar).
    filas = query(
        f"""
        SELECT t.producto, t.unidades, t.ingresos, t.producto_id,
               {("st.costo_unitario," if con_costos else "")}
               COALESCE(st.stock, 0) AS stock_actual
        FROM (
            SELECT v.Producto AS producto,
                   SUM(v.Cantidad) AS unidades,
                   SUM(v.Total_Venta) AS ingresos,
                   MAX(p.id) AS producto_id
            FROM ventas v
            LEFT JOIN productos p ON p.tenant_id = v.tenant_id AND p.Producto = v.Producto
            WHERE {' AND '.join(filtros)}
            GROUP BY v.Producto
            ORDER BY SUM(v.Total_Venta) DESC, SUM(v.Cantidad) DESC
            LIMIT %s
        ) t
        LEFT JOIN LATERAL (
            SELECT SUM(l.Stock_Lote) AS stock,
                   {("CASE WHEN COALESCE(SUM(CASE WHEN l.Stock_Lote > 0 THEN l.Stock_Lote END), 0) > 0 "
                     "THEN SUM(CASE WHEN l.Stock_Lote > 0 THEN l.Costo * l.Stock_Lote END) "
                     "/ SUM(CASE WHEN l.Stock_Lote > 0 THEN l.Stock_Lote END) ELSE 0 END AS costo_unitario"
                     if con_costos else "1::int AS uno")}
            FROM lotes l
            WHERE l.producto_id = t.producto_id AND l.tenant_id = %s
              AND l.Estado = 'Activo'
        ) st ON TRUE
        """,
        tuple(params + [limite, tenant_id]),
    )

    ranking = []
    for f in filas:
        stock = _stock(f.get("stock_actual"))
        item = {
            "producto": f.get("producto"),
            "producto_id": f.get("producto_id"),
            "unidades": _stock(f.get("unidades")),
            "ingresos": _num(f.get("ingresos")),
            "stock_actual": stock,
            "agotado": stock <= 0,
            "por_agotarse": 0 < stock <= 3,
        }
        if con_costos:
            costo = _num(f.get("costo_unitario"))
            item["costo_unitario"] = costo
            item["margen"] = round(item["ingresos"] - costo * item["unidades"], 2)
        ranking.append(item)

    return {
        "periodo": periodo,
        "total": len(ranking),
        "top": ranking,
    }
