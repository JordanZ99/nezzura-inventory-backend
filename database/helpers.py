# ==============================================================================
# backend/database/helpers.py
# Helpers compartidos por los módulos de datos del inventario (productos,
# lotes, variaciones, recetas, categorías). Antes vivían en lotes.py; se
# extrajeron aquí para que cada dominio sea un módulo independiente.
# ==============================================================================

import re
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from psycopg2.extras import RealDictCursor
from database.conexion import query, execute


# ── Zona horaria del negocio (Cancún, UTC-5) ──
_TZ = ZoneInfo("America/Cancun")

# ── Zona horaria POR TENANT (migración 031) ──
# El instante real vive en columnas TIMESTAMPTZ; el "día de negocio" se deriva
# con la zona IANA configurada del tenant. Cache con TTL corta: si el dueño
# cambia la zona vía Supabase directo, el backend se sincroniza solo.
_ZONA_DEFAULT = _TZ
_ZONA_TTL_SEGUNDOS = 300.0
_zonas_cache: dict[str, tuple[ZoneInfo, float]] = {}


def zona_tenant(tenant_id: str) -> ZoneInfo:
    """Zona horaria IANA del negocio; cachea 5 min para no consultar en cada write."""
    if not tenant_id:
        return _ZONA_DEFAULT
    entrada = _zonas_cache.get(tenant_id)
    if entrada and (time.monotonic() - entrada[1]) < _ZONA_TTL_SEGUNDOS:
        return entrada[0]
    try:
        filas = query("SELECT zona_horaria FROM tenants WHERE id = %s", (tenant_id,))
        nombre = (filas[0]["zona_horaria"] if filas else "") or "America/Cancun"
    except Exception:
        nombre = "America/Cancun"
    try:
        tz = ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        tz = _ZONA_DEFAULT
    _zonas_cache[tenant_id] = (tz, time.monotonic())
    return tz


def invalidar_zona_tenant(tenant_id: str) -> None:
    """Fuerza relectura de la zona (lo llama PATCH /me tras actualizarla)."""
    _zonas_cache.pop(tenant_id, None)


def ahora_negocio(tenant_id: str) -> datetime:
    """Instante actual en la zona del negocio (tz-aware, listo para TIMESTAMPTZ)."""
    return datetime.now(zona_tenant(tenant_id))


def hoy_negocio(tenant_id: str) -> date:
    """Día contable actual según la zona del negocio."""
    return ahora_negocio(tenant_id).date()


def _parsear_ts(valor) -> datetime:
    """
    Convierte cualquier representación legada de fecha a un instante tz-aware.
    - datetime → se respeta; si es naive se asume UTC (era la hora del servidor)
    - TEXT con offset o Z → se parsea como instante
    - TEXT naive ('2026-03-29 12:49:18.125243') → se asume UTC
    """
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    s = str(valor).strip()
    if not s:
        raise ValueError("Fecha vacía")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fecha_negocio_de(valor, tenant_id: str) -> date:
    """
    Día contable de un valor de fecha:
    - 'YYYY-MM-DD' (fecha capturada por humano) → tal cual
    - cualquier timestamp/TEXT con hora → instante → día en la zona del negocio
    """
    s = str(valor).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    return _parsear_ts(s).astimezone(zona_tenant(tenant_id)).date()


def _q(conn, sql: str, params: tuple = ()) -> list[dict]:
    """
    Ejecuta un SELECT sobre la conexión `conn` si se provee (para usarse dentro
    de una transacción atómica) o sobre la conexión del pool global si no.
    """
    if conn is None:
        return query(sql, params)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _e(conn, sql: str, params: tuple = ()) -> None:
    """
    Ejecuta un INSERT/UPDATE/DELETE sobre la conexión `conn` si se provee
    (para usarse dentro de una transacción atómica) o el pool global si no.
    """
    if conn is None:
        execute(sql, params)
        return
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _slugify(texto: str) -> str:
    """
    Convierte un nombre de categoría en un slug URL-amigable.
    Ej: 'Accesorios de Moda' → 'accesorios-de-moda'
    """
    texto = texto.lower().strip()
    # Reemplazar espacios y caracteres no alfanuméricos (excepto guiones) por guiones
    texto = re.sub(r'[^a-z0-9áéíóúüñ\s-]', '', texto)
    texto = re.sub(r'[\s-]+', '-', texto)
    return texto.strip('-')


def _sincronizar_categorias(producto_id: int, categorias: list[str], tenant_id: str, conn=None) -> None:
    """
    Sincroniza las categorías de un producto en la tabla pivote (producto_categorias).
    
    Estrategia:
    1. Elimina todas las relaciones existentes para este producto en producto_categorias.
    2. Por cada nombre de categoría, hace un upsert en la tabla 'categorias' y
       crea la relación en 'producto_categorias'.
    
    Args:
        producto_id: ID numérico del producto (productos.id)
        categorias: Lista de nombres de categorías a asignar
        tenant_id: UUID del tenant propietario
        conn: si se provee, todo se ejecuta sobre esa conexión (para usarse
              dentro de una transacción atómica, ej. crear_producto_completo).
    """
    # Normalizar: limpiar espacios y eliminar duplicados preservando orden
    categorias = list(dict.fromkeys([c.strip() for c in categorias if c.strip()]))
    if not categorias:
        categorias = ["General"]

    # 1. Eliminar relaciones existentes para este producto
    _e(conn,
        "DELETE FROM producto_categorias WHERE producto_id = %s",
        (producto_id,)
    )

    # 2. Upsert cada categoría y crear la relación
    for nombre in categorias:
        slug = _slugify(nombre)

        # Upsert: si ya existe (tenant_id, nombre), devuelve el id existente
        result = _q(conn, """
            INSERT INTO categorias (tenant_id, nombre, slug)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id, nombre) DO UPDATE SET
                slug = EXCLUDED.slug
            RETURNING id
        """, (tenant_id, nombre, slug))

        categoria_id = result[0]["id"]

        # Insertar en la tabla pivote (ignorar si ya existe por alguna razón)
        _e(conn,
            "INSERT INTO producto_categorias (producto_id, categoria_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (producto_id, categoria_id)
        )


def _obtener_categorias_subquery(alias: str, visible_only: bool = False) -> str:
    """
    Genera una subquery SQL correlacionada para obtener las categorías
    de un producto como un array de nombres.
    
    Úsala en cualquier SELECT que necesite incluir categorías sin importar
    la columna antigua Categoria de la tabla productos.
    
    Args:
        alias: Alias de la tabla productos (e.g. 'p')
        visible_only: Si True, solo incluye categorías con visible_en_catalogo = true
    """
    filtro_visible = "AND c.visible_en_catalogo = true" if visible_only else ""
    return f"""COALESCE(
        (SELECT array_agg(c.nombre ORDER BY c.nombre)
         FROM producto_categorias pc
         JOIN categorias c ON pc.categoria_id = c.id
         WHERE pc.producto_id = {alias}.id {filtro_visible}),
        ARRAY['General']
    ) AS categoria"""


def _resolver_producto_id(producto: str, tenant_id: str) -> int | None:
    """Resuelve el ID numérico de un producto por su nombre (o None)."""
    r = query(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    return r[0]["id"] if r else None
