# ==============================================================================
# backend/routers/publico.py
# Router PÚBLICO para el catálogo compartible.
#
# SEGURIDAD:
#   - NO usa Depends(get_tenant_id) — no requiere JWT
#   - El cliente manda un slug (UUID aleatorio), no el tenant_id
#   - El backend resuelve slug → tenant_id internamente (database/catalogo.py)
#     y NUNCA lo devuelve
#   - Solo expone campos públicos: nombre, descripción, imagen, precio, stock, categoría
#   - No expone: costo, ganancia_bruta, codigo_interno, tenant_id
#   - Solo tiene un endpoint GET — no hay POST/PUT/DELETE aquí
#   - Cache-Control: no-store para garantizar datos siempre frescos
#
# El ensamblado del catálogo (SQL + filtro de agotados) vive en
# database/catalogo.py (obtener_catalogo_publico).
# ==============================================================================

from fastapi import APIRouter, HTTPException, Response
from database.catalogo import obtener_catalogo_publico as obtener_catalogo_publico_dominio

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
      "productos": [{ "producto", "descripcion", "imagen", "imagenes", "precio_venta", "precio_min", "precio_max", "stock_total", "categoria", "sufijo_precio", "tamano_cm", "tipo_producto", "precios", "variaciones" }]
        donde "imagenes" es la galería completa (principal + extras de producto_imagenes),
        "precios" es el desglose [{precio, stock}] de lotes con stock a distinto
        precio (solo productos SIN variaciones — el costo nunca se expone) y
        "variaciones" trae su precio/stock propio por presentación
    }
    """
    # Cache-Control: prohibir caché para garantizar datos frescos
    response.headers["Cache-Control"] = "no-store"

    resultado = obtener_catalogo_publico_dominio(slug)
    if resultado.get("ok") is False:
        raise HTTPException(
            status_code=resultado.get("status", 404),
            detail=resultado.get("mensaje", "Catálogo no encontrado o no activo"),
        )
    return resultado
