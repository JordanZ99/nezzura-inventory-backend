# ==============================================================================
# backend/database/movimientos.py
# Libro de movimientos de inventario (ledger append-only, migración 040).
# Cada cambio de stock de un lote deja UN renglón nuevo que nunca se edita ni
# se borra: entradas − salidas ± ajustes = stock verificable del lote.
#
# El helper usa subconsultas para congelar el CONTEXTO al momento del
# movimiento: producto_id, variación y stock_resultante se leen del lote dentro
# de la MISMA transacción (o del mismo instante si va por el pool), así el
# snapshot histórico no cambia aunque el lote siga moviéndose después.
# Acepta `conn` opcional para participar de la transacción atómica del cobro —
# mismo patrón que registrar_movimiento de puntos.py y _q/_e de helpers.py.
# ==============================================================================

from database.conexion import query
from database.helpers import _q, _e, zona_tenant

# Guardia de existencia (cacheada por proceso): evita intentar escribir el
# ledger si la migración 040 aún no corrió. CRÍTICO: un fallo del INSERT
# DENTRO de la transacción del cobro dejaría la conexión en estado
# "aborted" y reventaría el cobro completo aunque la excepción se trague.
_tabla_verificada: bool | None = None


def _tabla_existe(conn=None) -> bool:
    global _tabla_verificada
    if _tabla_verificada is not None:
        return _tabla_verificada
    try:
        filas = _q(conn, "SELECT to_regclass('public.inventario_movimientos') AS t")
        _tabla_verificada = bool(filas and filas[0]["t"])
    except Exception:
        _tabla_verificada = False
    return _tabla_verificada


def registrar_movimiento_inventario(
    tenant_id: str,
    producto: str,
    tipo: str,
    origen: str,
    cantidad: float,
    id_lote: str | None = None,
    referencia_id: str | None = None,
    concepto: str | None = None,
    conn=None,
) -> None:
    """
    Escribe un renglón del libro. `cantidad` lleva signo: + entra, − sale.
    `id_lote` resuelve automáticamente producto_id, variación y el stock
    resultante del lote (subconsultas sobre el lote ya actualizado).
    Nunca lanza: el historial no debe romper la operación de negocio que
    está registrando. Si el lote referenciado no existe, no se escribe nada.
    """
    if not _tabla_existe(conn):
        return
    try:
        _e(conn, """
            INSERT INTO inventario_movimientos
                (tenant_id, producto_id, producto, variacion, id_lote,
                 tipo, origen, cantidad, stock_resultante, referencia_id, concepto)
            SELECT %s,
                   l.producto_id,
                   %s,
                   v.nombre,
                   %s,
                   %s, %s, %s,
                   l.Stock_Lote,
                   %s, %s
            FROM lotes l
            LEFT JOIN producto_variaciones v ON v.id = l.variacion_id
            WHERE l.ID_Lote = %s AND l.tenant_id = %s
        """, (tenant_id, producto, id_lote, tipo, origen, round(float(cantidad), 3),
              referencia_id, concepto, id_lote, tenant_id))
    except Exception:
        pass


def get_movimientos(
    tenant_id: str,
    limit: int = 50,
    offset: int = 0,
    tipo: str | None = None,
    producto: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
) -> dict:
    """
    Historial paginado para la tab 'Historial de cambios' de Estadísticas.
    Filtros opcionales: tipo, texto de producto (ILIKE) y rango de fechas.
    """
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    where = ["tenant_id = %s"]
    params: list = [tenant_id]
    if tipo:
        where.append("tipo = %s")
        params.append(tipo)
    if producto and producto.strip():
        where.append("producto ILIKE %s")
        params.append(f"%{producto.strip()}%")
    if desde or hasta:
        # El día se calcula en la zona horaria del NEGOCIO (no en UTC).
        tz = str(zona_tenant(tenant_id))
        if desde:
            where.append("(fecha AT TIME ZONE %s)::date >= %s")
            params.extend([tz, desde])
        if hasta:
            where.append("(fecha AT TIME ZONE %s)::date <= %s")
            params.extend([tz, hasta])
    clausula = " AND ".join(where)

    total = query(
        f"SELECT COUNT(*) AS total FROM inventario_movimientos WHERE {clausula}",
        tuple(params)
    )[0]["total"]

    movimientos = query(f"""
        SELECT id, producto_id, producto, variacion, id_lote, tipo, origen,
               cantidad, stock_resultante, referencia_id, concepto, fecha
        FROM inventario_movimientos
        WHERE {clausula}
        ORDER BY fecha DESC, id DESC
        LIMIT %s OFFSET %s
    """, tuple(params) + (limit, offset))

    return {"movimientos": movimientos, "total": int(total or 0), "limit": limit, "offset": offset}
