# ==============================================================================
# backend/database/helpers.py
# Helpers compartidos por los módulos de datos del inventario (productos,
# lotes, variaciones, recetas, categorías). Antes vivían en lotes.py; se
# extrajeron aquí para que cada dominio sea un módulo independiente.
# ==============================================================================

import re
from zoneinfo import ZoneInfo
from psycopg2.extras import RealDictCursor
from database.conexion import query, execute


# ── Zona horaria del negocio (Cancún, UTC-5) ──
_TZ = ZoneInfo("America/Cancun")


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
