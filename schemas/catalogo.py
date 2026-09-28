from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel, Field


class TemplateEnum(str, Enum):
    grid_clasico = "grid-clasico"
    menu_carta   = "menu-carta"


class ModoFondoEnum(str, Enum):
    """Modo de dibujado de la imagen de fondo del catálogo (migración 045)."""
    cover = "cover"    # foto a pantalla completa
    repeat = "repeat"  # textura tileada


class ActualizarCatalogo(BaseModel):
    activo: Optional[bool] = None
    tema: Optional[str] = None
    template: Optional[TemplateEnum] = None
    titulo: Optional[str] = None
    subtitulo: Optional[str] = None
    # Fuente display del catálogo (migración 044). Claves válidas en frontend
    # (lib/catalogo-fuentes.ts) — se valida en el router del gestor.
    fuente: Optional[str] = None
    mostrar_precios: Optional[bool] = None
    mostrar_stock: Optional[bool] = None
    mostrar_categorias: Optional[bool] = None
    agrupar_por_categoria: Optional[bool] = None
    columnas_movil: Optional[Literal[1, 2]] = None
    relacion_imagen: Optional[str] = None
    permitir_descarga: Optional[bool] = None
    ocultar_agotados: Optional[bool] = None
    banner_url: Optional[str] = None
    banner_url_movil: Optional[str] = None
    hero_estilo: Optional[str] = None
    banner_texto_color: Optional[str] = None
    banner_mostrar_texto: Optional[bool] = None
    banner_mostrar_logo: Optional[bool] = None
    anuncio_texto: Optional[str] = None
    # Fondo del catálogo con imagen (migración 045)
    fondo_url: Optional[str] = None
    fondo_modo: Optional[ModoFondoEnum] = None
    fondo_opacidad: Optional[int] = Field(None, ge=0, le=100, description="Opacidad de la imagen sobre el color de fondo (0-100)")
    fondo_color: Optional[str] = None
    # Modo Hero de la portada (migración 047): imagen a pantalla completa
    hero_url: Optional[str] = None
    hero_url_movil: Optional[str] = None
    hero_color: Optional[str] = None
    hero_opacidad: Optional[int] = Field(None, ge=0, le=100, description="Opacidad del velo sobre la imagen hero (0-100)")


class ActualizarPostConfig(BaseModel):
    template_default: Optional[str] = None
    font: Optional[str] = None
    posicion: Optional[str] = None
    mostrar: Optional[dict] = None
    color_primario: Optional[str] = None
    color_secundario: Optional[str] = None
