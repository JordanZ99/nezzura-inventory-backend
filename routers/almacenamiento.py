# ==============================================================================
# backend/routers/almacenamiento.py
# Medidor de almacenamiento del tenant (cuota por plan, migración 071).
#
# GET /almacenamiento/uso → estado del medidor para el frontend:
#   {plan, nombre, limite_bytes, bytes_usados, bytes_reales, porcentaje,
#    quedan_bytes, excedido}
#
# El bloqueo de subidas NO pasa por aquí: vive en la capa de imágenes
# (database/imagenes.py, _subir_con_cupo), que rechaza con 422 antes de subir.
# Este endpoint solo expone el medidor estilo Google Photos y alimenta el
# pre-check del cliente (file.size vs quedan_bytes).
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import get_tenant_id
from database.almacenamiento import uso_actual, listar_imagenes
from database.imagenes import liberar_urls

router = APIRouter(prefix="/almacenamiento", tags=["Almacenamiento"])


@router.get("/uso")
def uso(tenant_id: str = Depends(get_tenant_id)):
    """Estado del medidor de almacenamiento del tenant según su plan."""
    resultado = uso_actual(tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(
            status_code=resultado.get("status", 422),
            detail=resultado.get("mensaje", "No se pudo leer el medidor"),
        )
    return resultado


@router.get("/imagenes")
def imagenes(tenant_id: str = Depends(get_tenant_id)):
    """
    Inventario de imágenes del tenant para el gestor de espacio, ordenadas por
    peso: [{url, bytes, fuente, contexto, liberable}]. Las fotos del catálogo
    publicado (Modo Local) vienen con liberable=false: se gestionan
    republicando, no borrando aquí.
    """
    resultado = listar_imagenes(tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(
            status_code=resultado.get("status", 500),
            detail=resultado.get("mensaje", "No se pudo leer el inventario"),
        )
    return resultado


class LiberaSolicitud(BaseModel):
    urls: list[str]


@router.post("/liberar")
def liberar(data: LiberaSolicitud, tenant_id: str = Depends(get_tenant_id)):
    """
    Gestor de espacio: libera imágenes (borrado en lote). Limpia las
    referencias del tenant, destruye los assets y la cuota se descuenta sola.
    Máximo 50 URLs por llamada (el frontend pagina la selección).
    """
    if not data.urls:
        raise HTTPException(status_code=400, detail="No se recibieron URLs")
    if len(data.urls) > 50:
        raise HTTPException(
            status_code=400,
            detail="Máximo 50 imágenes por lote. Libera en tandas más pequeñas.",
        )
    return liberar_urls(data.urls, tenant_id)
