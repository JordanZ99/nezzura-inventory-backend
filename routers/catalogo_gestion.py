# ==============================================================================
# backend/routers/catalogo_gestion.py
# Router PRIVADO para gestionar la configuración del catálogo público.
#
# Todos los endpoints requieren JWT (Depends(get_tenant_id)).
# El usuario configura su catálogo desde /personalizacion:
#   - Activar/desactivar
#   - Elegir tema prehecho (default, midnightBlack, strawberry, cozyYellow)
#   - Elegir template (grid-clasico, menu-carta)
#   - Editar título y subtítulo
#   - Mostrar/ocultar precios, stock, categorías
#   - Obtener su link y slug
#
# El slug se genera automáticamente al crear el registro (gen_random_uuid).
# ==============================================================================

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from enum import Enum
from dependencies import get_tenant_id
from database.conexion import query, execute

router = APIRouter(prefix="/catalogo_gestion", tags=["Gestión de Catálogo"])


class TemplateEnum(str, Enum):
    """Enum de templates disponibles para el catálogo público.
    FastAPI rechaza automáticamente cualquier valor que no esté en esta lista (HTTP 422).
    Añadir aquí los nuevos templates a medida que se implementen."""
    grid_clasico = "grid-clasico"
    menu_carta   = "menu-carta"


class ActualizarCatalogo(BaseModel):
    """Modelo para actualizar la configuración del catálogo. PATCH parcial."""
    activo: Optional[bool] = None
    tema: Optional[str] = None        # 'default' | 'midnightBlack' | 'strawberry' | 'cozyYellow'
    template: Optional[TemplateEnum] = None  # Validado por el Enum
    titulo: Optional[str] = None
    subtitulo: Optional[str] = None
    mostrar_precios: Optional[bool] = None
    mostrar_stock: Optional[bool] = None
    mostrar_categorias: Optional[bool] = None


@router.get("")
def obtener_config_catalogo(tenant_id: str = Depends(get_tenant_id)):
    """
    Retorna la configuración del catálogo del tenant.
    Si no existe, crea un registro con valores por defecto (slug auto-generado).
    """
    resultado = query(
        "SELECT id, slug, activo, tema, template, titulo, subtitulo, "
        "       mostrar_precios, mostrar_stock, mostrar_categorias, created_at "
        "FROM catalogo_config WHERE tenant_id = %s",
        (tenant_id,)
    )
    if not resultado:
        # Auto-crear registro por defecto con slug aleatorio
        execute(
            "INSERT INTO catalogo_config (tenant_id) VALUES (%s)",
            (tenant_id,)
        )
        resultado = query(
            "SELECT id, slug, activo, tema, template, titulo, subtitulo, "
            "       mostrar_precios, mostrar_stock, mostrar_categorias, created_at "
            "FROM catalogo_config WHERE tenant_id = %s",
            (tenant_id,)
        )
    return resultado[0] if resultado else {}


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
    # Verificar si existe el registro
    existe = query(
        "SELECT id FROM catalogo_config WHERE tenant_id = %s",
        (tenant_id,)
    )
    if not existe:
        execute(
            "INSERT INTO catalogo_config (tenant_id) VALUES (%s)",
            (tenant_id,)
        )

    campos = []
    valores = []
    if data.activo is not None:
        campos.append("activo = %s")
        valores.append(data.activo)
    if data.tema is not None:
        campos.append("tema = %s")
        valores.append(data.tema)
    if data.template is not None:
        campos.append("template = %s")
        valores.append(data.template.value)  # El Enum devuelve el string
    if data.titulo is not None:
        campos.append("titulo = %s")
        valores.append(data.titulo)
    if data.subtitulo is not None:
        campos.append("subtitulo = %s")
        valores.append(data.subtitulo)
    if data.mostrar_precios is not None:
        campos.append("mostrar_precios = %s")
        valores.append(data.mostrar_precios)
    if data.mostrar_stock is not None:
        campos.append("mostrar_stock = %s")
        valores.append(data.mostrar_stock)
    if data.mostrar_categorias is not None:
        campos.append("mostrar_categorias = %s")
        valores.append(data.mostrar_categorias)

    if not campos:
        return {"ok": True, "mensaje": "Nada que actualizar"}

    campos.append("updated_at = now()")
    valores.append(tenant_id)
    execute(
        f"UPDATE catalogo_config SET {', '.join(campos)} WHERE tenant_id = %s",
        tuple(valores)
    )
    return {"ok": True, "mensaje": "Configuración del catálogo actualizada"}
