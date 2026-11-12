# ==============================================================================
# backend/database/mcp_catalogo.py
# Queries de SOLO LECTURA para el servidor MCP (backend/muse/server.py).
#
# REGLA DE ORO: columnas EXPLÍCITAS, nunca SELECT *. Los costos (lotes.Costo,
# costo_promedio, costo_servicio) y los rangos internos (precio_min/max) NO se
# seleccionan: no es que el agente "prometa" ignorarlos — directamente no
# salen de la base de datos.
#
# Precio y stock usan la MISMA fórmula del catálogo público
# (database/catalogo.py, obtener_catalogo_publico) para que el agente reporte
# exactamente el mismo número que ve el dueño en el gestor y sus clientes en
# el catálogo web.
#
# Alcance definido con el dueño:
#   - Solo productos físicos (tipo_producto = 'stock'): Marketplace es de
#     bienes, servicios y compuestos quedan fuera.
#   - Stock en unidades exactas: el agente puede responder "cuánto queda".
#   - Sin filtro visible_en_catalogo: publicar en Marketplace es otro canal
#     distinto del catálogo web; el agente ve todos los físicos activos.
# ==============================================================================

from database.conexion import query
from database.helpers import _obtener_categorias_subquery

# Misma expresión de precio del catálogo público: MAX entre lotes CON stock;
# si ningún lote tiene stock cae al MAX histórico de los lotes activos.
_PRECIO_VENTA = (
    "COALESCE(MAX(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), "
    "MAX(l.Precio_Venta), 0)"
)

# JOIN de lotes activos, idéntico al del catálogo público.
_JOIN_LOTES = (
    "LEFT JOIN lotes l ON l.producto_id = p.id AND l.tenant_id = p.tenant_id "
    "AND l.Estado = 'Activo'"
)

# Filtro de alcance: productos físicos activos del tenant dueño de la clave.
_FILTRO_ALCANCE = (
    "p.Estado = 'Activo' AND p.tenant_id = %s AND p.tipo_producto = 'stock'"
)

# Categorías del producto (todas, no solo las visibles en catálogo).
_CATEGORIAS = _obtener_categorias_subquery("p", visible_only=False)


def _num(valor) -> float:
    try:
        return round(float(valor or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _stock(valor) -> float:
    """Stock como float: los fraccionables manejan 0.5 kg, 1.5 m, etc."""
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Tool: buscar_productos ────────────────────────────────────────────────────

def buscar_productos(
    tenant_id: str, consulta: str, solo_con_stock: bool = False, limite: int = 15
) -> list[dict]:
    """Búsqueda por nombre o código (interno / barras)."""
    consulta = (consulta or "").strip()
    patron = f"%{consulta}%"
    filtro_stock = "HAVING COALESCE(SUM(l.Stock_Lote), 0) > 0" if solo_con_stock else ""
    filas = query(
        f"""
        SELECT p.id, p.producto AS nombre, p.sufijo_precio,
               {_PRECIO_VENTA} AS precio_venta,
               COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible,
               {_CATEGORIAS}
        FROM productos p
        {_JOIN_LOTES}
        WHERE {_FILTRO_ALCANCE}
          AND (p.producto ILIKE %s OR p.codigo_interno ILIKE %s OR p.codigo_barras ILIKE %s)
        GROUP BY p.id
        {filtro_stock}
        ORDER BY p.producto ASC
        LIMIT %s
        """,
        (tenant_id, patron, patron, patron, limite),
    )
    return [
        {
            "id": f["id"],
            "nombre": f["nombre"],
            "precio_venta": _num(f["precio_venta"]),
            "sufijo_precio": f.get("sufijo_precio") or "",
            "stock_disponible": _stock(f["stock_disponible"]),
            "categoria": f.get("categoria") or ["General"],
        }
        for f in filas
    ]


# ── Tool: obtener_producto ────────────────────────────────────────────────────

def obtener_producto(tenant_id: str, producto_id: int) -> dict | None:
    """Ficha completa para publicar. None si no existe / no es del tenant."""
    filas = query(
        f"""
        SELECT p.id, p.producto AS nombre, p.descripcion AS descripcion,
               p.sufijo_precio, p.tamano_cm,
               {_PRECIO_VENTA} AS precio_venta,
               COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible,
               {_CATEGORIAS}
        FROM productos p
        {_JOIN_LOTES}
        WHERE {_FILTRO_ALCANCE} AND p.id = %s
        GROUP BY p.id
        """,
        (tenant_id, producto_id),
    )
    if not filas:
        return None

    fila = filas[0]

    # Variaciones con su precio y stock propio (misma query del catálogo
    # público: lotes activos ligados a cada variación).
    variaciones = query(
        """
        SELECT v.nombre, v.precio,
               COALESCE((SELECT SUM(l2.Stock_Lote) FROM lotes l2
                         WHERE l2.variacion_id = v.id AND l2.Estado = 'Activo'
                           AND l2.tenant_id = v.tenant_id), 0) AS stock
        FROM producto_variaciones v
        JOIN productos p ON p.id = v.producto_id
        WHERE v.producto_id = %s AND p.tenant_id = %s
        ORDER BY v.nombre ASC
        """,
        (producto_id, tenant_id),
    )

    tamano = fila.get("tamano_cm")
    return {
        "id": fila["id"],
        "nombre": fila["nombre"],
        "descripcion": fila.get("descripcion") or "",
        "precio_venta": _num(fila["precio_venta"]),
        "sufijo_precio": fila.get("sufijo_precio") or "",
        "stock_disponible": _stock(fila["stock_disponible"]),
        "tamano_cm": float(tamano) if tamano is not None else None,
        "categoria": fila.get("categoria") or ["General"],
        # Marketplace pregunta la condición: sugerimos "nuevo" y el dueño
        # la corrige al revisar la publicación.
        "condicion_sugerida": "nuevo",
        "variaciones": [
            {"nombre": v["nombre"], "precio": _num(v["precio"]), "stock": _stock(v["stock"])}
            for v in variaciones
        ],
    }


# ── Tool: obtener_fotos_producto ──────────────────────────────────────────────

def obtener_fotos(tenant_id: str, producto_id: int) -> dict | None:
    """
    Galería del producto: foto principal (productos.Imagen) + extras
    (producto_imagenes, tenants Plus), sin duplicados ni "No hay foto".
    Son URLs públicas de Cloudinary: el agente las descarga directo.
    """
    filas = query(
        """
        SELECT p.imagen AS imagen
        FROM productos p
        WHERE p.id = %s AND p.tenant_id = %s
          AND p.Estado = 'Activo' AND p.tipo_producto = 'stock'
        """,
        (producto_id, tenant_id),
    )
    if not filas:
        return None

    galeria: list[str] = []
    principal = filas[0].get("imagen")
    if principal and principal != "No hay foto":
        galeria.append(principal)

    extras = query(
        "SELECT url FROM producto_imagenes "
        "WHERE producto_id = %s AND tenant_id = %s ORDER BY orden ASC",
        (producto_id, tenant_id),
    )
    for e in extras:
        if e.get("url") and e["url"] not in galeria:
            galeria.append(e["url"])

    return {
        "foto_principal": galeria[0] if galeria else None,
        "galeria": galeria,
        "total": len(galeria),
    }


# ── Tool: stock_actual ────────────────────────────────────────────────────────

def stock_actual(
    tenant_id: str, producto_id: int | None = None, consulta: str | None = None,
    limite: int = 10,
) -> list[dict]:
    """Stock por id exacto o por búsqueda de nombre (todas las coincidencias)."""
    if producto_id is not None:
        filas = query(
            f"""
            SELECT p.id, p.producto AS nombre,
                   COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible
            FROM productos p
            {_JOIN_LOTES}
            WHERE {_FILTRO_ALCANCE} AND p.id = %s
            GROUP BY p.id
            """,
            (tenant_id, producto_id),
        )
    else:
        patron = f"%{(consulta or '').strip()}%"
        filas = query(
            f"""
            SELECT p.id, p.producto AS nombre,
                   COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible
            FROM productos p
            {_JOIN_LOTES}
            WHERE {_FILTRO_ALCANCE} AND p.producto ILIKE %s
            GROUP BY p.id
            ORDER BY p.producto ASC
            LIMIT %s
            """,
            (tenant_id, patron, limite),
        )
    return [
        {
            "id": f["id"],
            "nombre": f["nombre"],
            "stock_disponible": _stock(f["stock_disponible"]),
        }
        for f in filas
    ]


# ── Tool: listar_categorias ───────────────────────────────────────────────────

def listar_categorias(tenant_id: str) -> list[str]:
    """Categorías con al menos un producto físico activo."""
    filas = query(
        """
        SELECT DISTINCT c.nombre
        FROM categorias c
        JOIN producto_categorias pc ON pc.categoria_id = c.id
        JOIN productos p ON pc.producto_id = p.id
        WHERE c.tenant_id = %s AND p.tenant_id = %s
          AND p.Estado = 'Activo' AND p.tipo_producto = 'stock'
        ORDER BY c.nombre ASC
        """,
        (tenant_id, tenant_id),
    )
    return [f["nombre"] for f in filas]


# ── Tool: datos_para_publicar ─────────────────────────────────────────────────

def datos_para_publicar(tenant_id: str, producto_id: int) -> dict | None:
    """
    Paquete completo en una llamada: ficha + fotos. Reduce round-trips y
    garantiza que el "paquete de publicación" lleve exactamente los campos
    permitidos (nunca costos).
    """
    ficha = obtener_producto(tenant_id, producto_id)
    if ficha is None:
        return None
    fotos = obtener_fotos(tenant_id, producto_id) or {
        "foto_principal": None, "galeria": [], "total": 0,
    }
    ficha["fotos"] = fotos
    return ficha
