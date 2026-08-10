# ==============================================================================
# backend/routers/publico.py
# Router PÚBLICO para el catálogo compartible.
#
# SEGURIDAD:
#   - NO usa Depends(get_tenant_id) — no requiere JWT
#   - El cliente manda un slug (UUID aleatorio), no el tenant_id
#   - El backend resuelve slug → tenant_id internamente y NUNCA lo devuelve
#   - Solo expone campos públicos: nombre, descripción, imagen, precio, stock, categoría
#   - No expone: costo, ganancia_bruta, codigo_interno, tenant_id
#   - Solo tiene un endpoint GET — no hay POST/PUT/DELETE aquí
#   - Cache-Control: no-store para garantizar datos siempre frescos
# ==============================================================================

from fastapi import APIRouter, HTTPException, Response
from database.conexion import query
from database.lotes import _obtener_categorias_subquery

router = APIRouter(prefix="/public", tags=["Catálogo Público"])


@router.get("/catalogo/{slug}")
def obtener_catalogo_publico(slug: str, response: Response):
    """
    Endpoint público: devuelve la configuración del catálogo + los productos
    activos del tenant identificado por el slug.

    El slug es un UUID v4 aleatorio generado al activar el catálogo desde
    la sección de ajustes. No es derivable del tenant_id ni secuencial.

    Cache-Control: no-store  →  garantiza que el navegador nunca cachee la
    respuesta. Así, si el tenant cambia precios/stock/activación, el visitante
    siempre verá los datos más recientes.

    Respuesta (sin tenant_id, sin costos, sin datos sensibles):
    {
      "config": { "titulo", "subtitulo", "tema", "template", "mostrar_precios", ... },
      "productos": [{ "producto", "descripcion", "imagen", "precio_venta", "stock_total", "categoria" }]
    }
    """
    # Cache-Control: prohibir caché para garantizar datos frescos
    response.headers["Cache-Control"] = "no-store"

    # 1. Una sola query: obtiene config + tenant_id + logo al mismo tiempo.
    #    Solo retorna resultado si activo = true (catálogo habilitado).
    #    LEFT JOIN con tenants para obtener el logo personalizado del negocio.
    config_rows = query(
        "SELECT cc.tenant_id, cc.tema, cc.template, cc.titulo, cc.subtitulo, "
        "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, "
        "       cc.banner_url, cc.hero_estilo, cc.anuncio_texto, "
        "       t.logo "
        "FROM catalogo_config cc "
        "LEFT JOIN tenants t ON cc.tenant_id = t.id "
        "WHERE cc.slug = %s AND cc.activo = true",
        (slug,)
    )
    if not config_rows:
        raise HTTPException(status_code=404, detail="Catálogo no encontrado o no activo")

    cfg = config_rows[0]

    # tenant_id se usa INTERNAMENTE — nunca se devuelve al cliente
    tenant_id = cfg["tenant_id"]

    # 2. Productos activos: solo campos públicos
    #    Se incluye stock_total para que el cliente sepa si hay disponibilidad,
    #    pero NO se incluye costo, ganancia_bruta, codigo_interno ni tenant_id.
    #
    #    NOTA: precio_venta y stock_total viven en la tabla `lotes`, no en `productos`.
    #    Esta query sigue el mismo patrón que get_inventario_consolidado() en lotes.py.
    # Solo categorías marcadas como visibles en el catálogo
    cat_subquery = _obtener_categorias_subquery("p", visible_only=True)
    productos = query(f"""
        SELECT
            l.Producto                    AS producto,
            p.Descripcion                 AS descripcion,
            p.Imagen                      AS imagen,
            MAX(l.Precio_Venta)           AS precio_venta,
            SUM(l.Stock_Lote)             AS stock_total,
            {cat_subquery}
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto AND l.tenant_id = p.tenant_id
        WHERE l.Estado = 'Activo' AND p.Estado = 'Activo' AND l.tenant_id = %s
        AND p.visible_en_catalogo = true
        GROUP BY l.Producto, p.Descripcion, p.Imagen, p.id
        ORDER BY l.Producto ASC
    """, (tenant_id,))

    return {
        "config": {
            "tema": cfg["tema"],
            "template": cfg["template"],
            "titulo": cfg["titulo"],
            "subtitulo": cfg.get("subtitulo", ""),
            "mostrar_precios": cfg["mostrar_precios"],
            "mostrar_stock": cfg["mostrar_stock"],
            "mostrar_categorias": cfg["mostrar_categorias"],
            "agrupar_por_categoria": cfg.get("agrupar_por_categoria"),
            "banner_url": cfg.get("banner_url") or "",
            "hero_estilo": cfg.get("hero_estilo") or "gradiente",
            "anuncio_texto": cfg.get("anuncio_texto") or "",
            "logo": cfg.get("logo") or "",
        },
        "productos": productos
    }
