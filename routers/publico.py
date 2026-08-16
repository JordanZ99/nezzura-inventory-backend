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
      "productos": [{ "producto", "descripcion", "imagen", "imagenes", "precio_venta", "stock_total", "categoria" }]
        donde "imagenes" es la galería completa (principal + extras de producto_imagenes)
    }
    """
    # Cache-Control: prohibir caché para garantizar datos frescos
    response.headers["Cache-Control"] = "no-store"

    # 1. Una sola query: obtiene config + tenant_id + logo al mismo tiempo.
    #    Solo retorna resultado si activo = true (catálogo habilitado).
    #    LEFT JOIN con tenants para obtener el logo personalizado del negocio.
    config_rows = query(
        "SELECT cc.tenant_id, cc.tema, cc.template, cc.titulo, cc.subtitulo, "
        "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, cc.columnas_movil, cc.permitir_descarga, cc.ocultar_agotados, cc.relacion_imagen, "
        "       cc.banner_url, cc.banner_url_movil, cc.hero_estilo, cc.banner_texto_color, cc.banner_mostrar_texto, cc.banner_mostrar_logo, cc.anuncio_texto, "
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
            p.Producto                    AS producto,
            p.Descripcion                 AS descripcion,
            p.Imagen                      AS imagen,
            p.sufijo_precio               AS sufijo_precio,
            p.tipo_producto               AS tipo_producto,
            -- Las variaciones traen su propio `stock` (ver query de variaciones abajo)
            -- Servicios (sin lotes) usan su precio de servicio; productos normales el MAX de lotes
            COALESCE(MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_venta,
            CASE WHEN p.tipo_producto IN ('servicio', 'compuesto') THEN 0
                 ELSE COALESCE(SUM(l.Stock_Lote), 0) END          AS stock_total,
            {cat_subquery}
        FROM productos p
        LEFT JOIN lotes l ON l.Producto = p.Producto AND l.tenant_id = p.tenant_id AND l.Estado = 'Activo'
        WHERE p.Estado = 'Activo' AND p.tenant_id = %s
        AND p.visible_en_catalogo = true
        GROUP BY p.Producto, p.Descripcion, p.Imagen, p.sufijo_precio, p.tipo_producto, p.precio_servicio, p.id
        ORDER BY p.Producto ASC
    """, (tenant_id,))

    # 3. Galería completa por producto: foto principal + extras (producto_imagenes).
    #    producto_imagenes solo existe para tenants Plus; si no hay filas, la
    #    galería queda solo con la foto principal (o vacía si no tiene foto).
    extras = query(
        "SELECT p.Producto AS producto, pi.url AS url "
        "FROM producto_imagenes pi "
        "JOIN productos p ON pi.producto_id = p.id "
        "WHERE p.tenant_id = %s "
        "ORDER BY p.Producto ASC, pi.orden ASC",
        (tenant_id,)
    )
    extras_por_producto: dict = {}
    for fila in extras:
        extras_por_producto.setdefault(fila["producto"], []).append(fila["url"])

    # 3b. Variaciones por producto (nombre + precio propio).
    #     Si un producto tiene variaciones, el catálogo muestra "desde $X"
    #     (precio mínimo) y el modal permite elegir la variación.
    # Suma el stock de los lotes ligados a cada variación (cada variación
    # lleva su propio inventario).
    variaciones = query("""
        SELECT p.Producto AS producto, v.id, v.nombre, v.precio, v.foto,
               COALESCE((SELECT SUM(l.Stock_Lote) FROM lotes l
                         WHERE l.variacion_id = v.id AND l.Estado='Activo'
                           AND l.tenant_id = v.tenant_id), 0) AS stock
        FROM producto_variaciones v
        JOIN productos p ON p.id = v.producto_id
        WHERE p.tenant_id = %s
        ORDER BY p.Producto ASC, v.nombre ASC
    """, (tenant_id,))
    variaciones_por_producto: dict = {}
    for v in variaciones:
        variaciones_por_producto.setdefault(v["producto"], []).append({
            "id": v["id"],
            "nombre": v["nombre"],
            "precio": float(v["precio"] or 0),
            "foto": v.get("foto") or "",
            "stock": float(v["stock"] or 0),
        })

    # Armar el campo imagenes (sin duplicados y sin "No hay foto")
    for p in productos:
        galeria: list[str] = []
        if p.get("imagen") and p["imagen"] != "No hay foto":
            galeria.append(p["imagen"])
        for url in extras_por_producto.get(p["producto"], []):
            if url and url not in galeria:
                galeria.append(url)
        p["imagenes"] = galeria
        p["variaciones"] = variaciones_por_producto.get(p["producto"], [])

    # Si el tenant oculta los productos agotados, excluirlos de la respuesta
    # (el filtro aquí evita descargar datos que el cliente no debe ver).
    # Los servicios y compuestos (stock 0 por diseño) SIEMPRE se muestran: no se agotan.
    if bool(cfg.get("ocultar_agotados")):
        productos = [
            p for p in productos
            if p.get("tipo_producto") in ("servicio", "compuesto") or (p.get("stock_total") or 0) > 0
        ]

    return {
        "config": {
            "tema": cfg["tema"],
            "template": cfg["template"],
            "titulo": cfg["titulo"],
            "subtitulo": cfg.get("subtitulo", ""),
            "mostrar_precios": cfg["mostrar_precios"],
            # El stock se muestra por defecto: null/true → true
            "mostrar_stock": cfg["mostrar_stock"] is not False,
            "mostrar_categorias": cfg["mostrar_categorias"],
            "agrupar_por_categoria": cfg.get("agrupar_por_categoria"),
            "columnas_movil": cfg.get("columnas_movil") or 2,
            "permitir_descarga": bool(cfg.get("permitir_descarga")),
            "ocultar_agotados": bool(cfg.get("ocultar_agotados")),
            "banner_url": cfg.get("banner_url") or "",
            "banner_url_movil": cfg.get("banner_url_movil") or "",
            "hero_estilo": cfg.get("hero_estilo") or "gradiente",
            "banner_texto_color": cfg.get("banner_texto_color") or "#ffffff",
            "banner_mostrar_texto": cfg.get("banner_mostrar_texto") is not False,
            "banner_mostrar_logo": cfg.get("banner_mostrar_logo") is not False,
            "anuncio_texto": cfg.get("anuncio_texto") or "",
            "relacion_imagen": cfg.get("relacion_imagen") or "1:1",
            "logo": cfg.get("logo") or "",
        },
        "productos": productos
    }
