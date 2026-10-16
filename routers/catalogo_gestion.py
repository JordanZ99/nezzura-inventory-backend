# ==============================================================================
# backend/routers/catalogo_gestion.py
# Router PRIVADO para gestionar la configuración del catálogo público.
#
# Todos los endpoints requieren JWT (Depends(get_tenant_id)). El SQL y las
# validaciones viven en database/catalogo.py (config del catálogo) y
# database/posts.py (defaults de posts); este router solo rutea HTTP y mapea
# los dicts de dominio {ok, status, mensaje} a HTTPException.
#
# El usuario configura su catálogo desde /personalizacion:
#   - Activar/desactivar
#   - Elegir tema prehecho (default, midnightBlack, strawberry, cozyYellow)
#   - Elegir template (grid-clasico, grid-card, menu)
#   - Editar título y subtítulo
#   - Mostrar/ocultar precios, stock, categorías
#   - Defaults de las tarjetas de post (post_config)
#   - Obtener su link y slug
#
# El slug se genera automáticamente al crear el registro (gen_random_uuid).
# ==============================================================================

from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_tenant_id
from database.catalogo import obtener_config, actualizar_config
from database.posts import obtener_post_config, actualizar_post_config
from schemas.catalogo import ActualizarCatalogo, ActualizarPostConfig

router = APIRouter(prefix="/catalogo_gestion", tags=["Gestión de Catálogo"])


def _traducir_error(resultado: dict) -> None:
    """Convierte el {ok: False, status, mensaje} del dominio en HTTPException."""
    raise HTTPException(
        status_code=resultado.get("status", 422),
        detail=resultado.get("mensaje", "Configuración inválida"),
    )


@router.get("")
def obtener_config_catalogo(tenant_id: str = Depends(get_tenant_id)):
    """
    Retorna la configuración del catálogo del tenant (incluye el logo del
    negocio, que vive en la tabla tenants).
    Si no existe, crea un registro con valores por defecto (slug auto-generado).
    """
    return obtener_config(tenant_id)


@router.put("")
def actualizar_config_catalogo(
    data: ActualizarCatalogo,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Actualiza la configuración del catálogo (PATCH parcial).
    Solo actualiza los campos que vengan en el body.
    Si no existe el registro, lo crea primero.
    """
    # mode="json": los Enum (template, fondo_modo) llegan a dominio como su
    # valor string, igual que el .value que se usaba antes en el router.
    resultado = actualizar_config(data.model_dump(mode="json"), tenant_id)
    if resultado.get("ok") is False:
        _traducir_error(resultado)
    return resultado


@router.get("/post_config")
def obtener_config_post(tenant_id: str = Depends(get_tenant_id)):
    """
    Retorna los defaults de posts del negocio (post_config).
    Si no existe la fila, la crea con los valores por defecto de la migración 026.
    """
    return obtener_post_config(tenant_id)


@router.put("/post_config")
def actualizar_config_post(
    data: ActualizarPostConfig,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Actualiza los defaults de posts del negocio (PATCH parcial).
    Valida los valores contra las listas permitidas (422 si son inválidos).
    """
    resultado = actualizar_post_config(data.model_dump(mode="json"), tenant_id)
    if resultado.get("ok") is False:
        _traducir_error(resultado)
    return resultado
