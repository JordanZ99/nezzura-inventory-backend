from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel


class TemplateEnum(str, Enum):
    grid_clasico = "grid-clasico"
    menu_carta   = "menu-carta"


class ActualizarCatalogo(BaseModel):
    activo: Optional[bool] = None
    tema: Optional[str] = None
    template: Optional[TemplateEnum] = None
    titulo: Optional[str] = None
    subtitulo: Optional[str] = None
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


class ActualizarPostConfig(BaseModel):
    template_default: Optional[str] = None
    font: Optional[str] = None
    posicion: Optional[str] = None
    mostrar: Optional[dict] = None
    color_primario: Optional[str] = None
    color_secundario: Optional[str] = None
