# ==============================================================================
# backend/database/actividad.py
# Ledger de actividad general (migración 062) — historial de cambios que NO
# son stock: ediciones de producto (foto, precio, nombre...), CRM (clientes,
# etiquetas, interacciones, puntos) y cobros del giro 'servicios'.
#
# Mismo espíritu que movimientos.py (040): append-only, snapshot sincroniza
# cuando fue, y `registrar` NUNCA lanza — un fallo del log jamás debe romper
# la operación de negocio que lo está registrando.
#
# Diferencia clave con 040: esto es observación pura (no alimenta ningún
# saldo), así que es el único ledger podable cuando se implemente retención.
# ==============================================================================

import json

from psycopg2.extras import Json

from database.conexion import query
from database.helpers import _q, _e, zona_tenant

_tabla_verificada: bool | None = None


def _tabla_existe(conn=None) -> bool:
    global _tabla_verificada
    if _tabla_verificada is not None:
        return _tabla_verificada
    try:
        filas = _q(conn, "SELECT to_regclass('public.registro_actividad') AS t")
        _tabla_verificada = bool(filas and filas[0]["t"])
    except Exception:
        _tabla_verificada = False
    return _tabla_verificada


def registrar_actividad(
    tenant_id: str,
    dominio: str,
    entidad: str,
    accion: str,
    resumen: str,
    entidad_id=None,
    detalle: dict | None = None,
    conn=None,
) -> None:
    """
    Escribe un renglón del registro de actividad. Nunca lanza: si la tabla no
    existe (migra pendiente) o el INSERT falla, el historial se salta y la
    operación de negocio sigue. detalle debe ser JSON-serializable.
    """
    if not _tabla_existe(conn):
        return
    # Limpieza defensiva: detalle vacío se guarda como NULL, no {}.
    detalle_limpio = detalle if detalle else None
    try:
        _e(conn, """
            INSERT INTO registro_actividad
                (tenant_id, dominio, entidad, accion, entidad_id, resumen, detalle)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (tenant_id, dominio, entidad, accion,
              str(entidad_id) if entidad_id is not None else None,
              resumen, Json(detalle_limpio) if detalle_limpio else None))
    except Exception:
        pass


def get_actividad(
    tenant_id: str,
    limit: int = 50,
    offset: int = 0,
    dominio: str | None = None,
    texto: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
) -> dict:
    """
    Feed paginado para la tab 'Historial' de Estadísticas. Filtros opcionales:
    dominio ('producto'|'cliente'|'cobro'), texto (ILIKE sobre resumen) y
    rango de fechas (días en la zona horaria del NEGOCIO).
    """
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    where = ["tenant_id = %s"]
    params: list = [tenant_id]
    if dominio:
        where.append("dominio = %s")
        params.append(dominio)
    if texto and texto.strip():
        where.append("resumen ILIKE %s")
        params.append(f"%{texto.strip()}%")
    if desde or hasta:
        tz = str(zona_tenant(tenant_id))
        if desde:
            where.append("(fecha AT TIME ZONE %s)::date >= %s")
            params.extend([tz, desde])
        if hasta:
            where.append("(fecha AT TIME ZONE %s)::date <= %s")
            params.extend([tz, hasta])
    clausula = " AND ".join(where)

    total = query(
        f"SELECT COUNT(*) AS total FROM registro_actividad WHERE {clausula}",
        tuple(params)
    )[0]["total"]

    eventos = query(f"""
        SELECT id, fecha, dominio, entidad, accion, entidad_id, resumen, detalle
        FROM registro_actividad
        WHERE {clausula}
        ORDER BY fecha DESC, id DESC
        LIMIT %s OFFSET %s
    """, tuple(params) + (limit, offset))

    # El JSONB llega de Postgres como dict/list ya parseado por psycopg2;
    # normalizo a JSON-texto plano para el transporte (el front lo re-parsea
    # sólo si viene string).
    for e in eventos:
        d = e.get("detalle")
        if isinstance(d, (dict, list)):
            e["detalle"] = json.loads(json.dumps(d, default=str))
    return {"eventos": eventos, "total": int(total or 0), "limit": limit, "offset": offset}
