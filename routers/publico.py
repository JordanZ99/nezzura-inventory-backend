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

    # 1. Una sola query: obtiene config Y tenant_id al mismo tiempo.
    #    Solo retorna resultado si activo = true (catálogo habilitado).
    config_rows = query(
        "SELECT tenant_id, tema, template, titulo, subtitulo, "
        "       mostrar_precios, mostrar_stock, mostrar_categorias "
        "FROM catalogo_config WHERE slug = %s AND activo = true",
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
    productos = query(
        "SELECT producto, descripcion, imagen, precio_venta, "
        "       stock_total, categoria "
        "FROM productos WHERE tenant_id = %s AND estado = 'Activo' "
        "ORDER BY producto ASC",
        (tenant_id,)
    )

    return {
        "config": {
            "tema": cfg["tema"],
            "template": cfg["template"],
            "titulo": cfg["titulo"],
            "subtitulo": cfg.get("subtitulo", ""),
            "mostrar_precios": cfg["mostrar_precios"],
            "mostrar_stock": cfg["mostrar_stock"],
            "mostrar_categorias": cfg["mostrar_categorias"],
        },
        "productos": productos
    }
