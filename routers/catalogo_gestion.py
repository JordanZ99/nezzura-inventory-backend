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

import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Literal
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


# Valores permitidos para la config de posts (Fase 1 — Posts Automáticos)
TEMPLATES_POST = ("marco", "overlay", "tarjeta")
COLORES_POST = ("default", "midnightBlack", "strawberry", "cozyYellow", "white")
FUENTES_POST = ("moderna", "elegante", "redondeada")
POSICIONES_POST = ("arriba", "abajo")


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
    agrupar_por_categoria: Optional[bool] = None  # Separar productos por secciones de categoría
    columnas_movil: Optional[Literal[1, 2]] = None  # 1 o 2 productos por fila en móvil (422 si es inválido)
    relacion_imagen: Optional[str] = None  # '1:1' | '4:5' — relación global de las fotos de producto (catálogo, POS, gestor y crops)
    permitir_descarga: Optional[bool] = None  # Permitir a los clientes descargar las fotos del catálogo
    ocultar_agotados: Optional[bool] = None  # Ocultar los productos sin stock del catálogo público
    # Hero + Anuncios (006_catalogo_hero_anuncios.sql)
    banner_url: Optional[str] = None      # URL de la imagen de banner/hero (Cloudinary)
    banner_url_movil: Optional[str] = None  # URL del banner específico para móviles (Cloudinary)
    hero_estilo: Optional[str] = None     # 'gradiente' | 'imagen'
    banner_texto_color: Optional[str] = None  # Color del título/subtítulo sobre el banner en modo imagen (hex)
    banner_mostrar_texto: Optional[bool] = None  # Mostrar título/subtítulo sobre el banner en modo imagen
    banner_mostrar_logo: Optional[bool] = None  # Mostrar el logo del negocio sobre el banner (hero)
    anuncio_texto: Optional[str] = None   # texto de la barra de anuncios (vacío = oculta)


@router.get("")
def obtener_config_catalogo(tenant_id: str = Depends(get_tenant_id)):
    """
    Retorna la configuración del catálogo del tenant (incluye el logo del
    negocio, que vive en la tabla tenants).
    Si no existe, crea un registro con valores por defecto (slug auto-generado).
    """
    resultado = query(
        "SELECT cc.id, cc.slug, cc.activo, cc.tema, cc.template, cc.titulo, cc.subtitulo, "
        "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, cc.columnas_movil, cc.permitir_descarga, cc.ocultar_agotados, cc.relacion_imagen, "
        "       cc.banner_url, cc.banner_url_movil, cc.hero_estilo, cc.banner_texto_color, cc.banner_mostrar_texto, cc.banner_mostrar_logo, cc.anuncio_texto, cc.created_at, "
        "       t.logo AS logo "
        "FROM catalogo_config cc "
        "LEFT JOIN tenants t ON cc.tenant_id = t.id "
        "WHERE cc.tenant_id = %s",
        (tenant_id,)
    )
    if not resultado:
        # Auto-crear registro por defecto con slug aleatorio
        execute(
            "INSERT INTO catalogo_config (tenant_id) VALUES (%s)",
            (tenant_id,)
        )
        resultado = query(
            "SELECT cc.id, cc.slug, cc.activo, cc.tema, cc.template, cc.titulo, cc.subtitulo, "
            "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, cc.columnas_movil, cc.permitir_descarga, cc.ocultar_agotados, cc.relacion_imagen, "
            "       cc.banner_url, cc.banner_url_movil, cc.hero_estilo, cc.banner_texto_color, cc.banner_mostrar_texto, cc.banner_mostrar_logo, cc.anuncio_texto, cc.created_at, "
            "       t.logo AS logo "
            "FROM catalogo_config cc "
            "LEFT JOIN tenants t ON cc.tenant_id = t.id "
            "WHERE cc.tenant_id = %s",
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
    if data.agrupar_por_categoria is not None:
        campos.append("agrupar_por_categoria = %s")
        valores.append(data.agrupar_por_categoria)
    if data.columnas_movil is not None:
        campos.append("columnas_movil = %s")
        valores.append(data.columnas_movil)
    if data.permitir_descarga is not None:
        campos.append("permitir_descarga = %s")
        valores.append(data.permitir_descarga)
    if data.ocultar_agotados is not None:
        campos.append("ocultar_agotados = %s")
        valores.append(data.ocultar_agotados)
    if data.banner_url is not None:
        campos.append("banner_url = %s")
        valores.append(data.banner_url)
    if data.banner_url_movil is not None:
        campos.append("banner_url_movil = %s")
        valores.append(data.banner_url_movil)
    if data.hero_estilo is not None:
        campos.append("hero_estilo = %s")
        valores.append(data.hero_estilo)
    if data.banner_texto_color is not None:
        campos.append("banner_texto_color = %s")
        valores.append(data.banner_texto_color)
    if data.banner_mostrar_texto is not None:
        campos.append("banner_mostrar_texto = %s")
        valores.append(data.banner_mostrar_texto)
    if data.banner_mostrar_logo is not None:
        campos.append("banner_mostrar_logo = %s")
        valores.append(data.banner_mostrar_logo)
    if data.anuncio_texto is not None:
        campos.append("anuncio_texto = %s")
        valores.append(data.anuncio_texto)
    if data.relacion_imagen is not None:
        campos.append("relacion_imagen = %s")
        valores.append(data.relacion_imagen)

    if not campos:
        return {"ok": True, "mensaje": "Nada que actualizar"}

    campos.append("updated_at = now()")
    valores.append(tenant_id)
    execute(
        f"UPDATE catalogo_config SET {', '.join(campos)} WHERE tenant_id = %s",
        tuple(valores)
    )
    return {"ok": True, "mensaje": "Configuración del catálogo actualizada"}


# =============================================================================
# ── POSTS AUTOMÁTICOS (Fase 1): defaults del negocio para las tarjetas ───────
# La configuración en cascada: post_config (defaults) + productos.post_override.
# Un producto sin override usa estos defaults; el override vive por producto.
# =============================================================================

class ActualizarPostConfig(BaseModel):
    """Modelo para actualizar los defaults de posts del negocio. PATCH parcial."""
    template_default: Optional[str] = None  # 'marco' | 'overlay' | 'tarjeta'
    color: Optional[str] = None            # key de paleta (default | midnightBlack | strawberry | cozyYellow | white)
    font: Optional[str] = None             # 'moderna' | 'elegante' | 'redondeada'
    posicion: Optional[str] = None         # 'arriba' | 'abajo' (solo Overlay, Fase 2)
    mostrar: Optional[dict] = None         # {nombre: bool, precio: bool, negocio: bool}


def _crear_post_config_default(tenant_id: str) -> None:
    """Crea la fila de post_config con los defaults si aún no existe."""
    existe = query("SELECT tenant_id FROM post_config WHERE tenant_id = %s", (tenant_id,))
    if not existe:
        execute(
            "INSERT INTO post_config (tenant_id) VALUES (%s)",
            (tenant_id,)
        )


def _parsear_mostrar(valor) -> dict:
    """Normaliza el JSONB 'mostrar' (psycopg2 lo entrega como texto si no hay
    typecaster registrado; si ya es dict, se devuelve tal cual)."""
    if isinstance(valor, dict):
        return valor
    if isinstance(valor, str) and valor.strip():
        try:
            return json.loads(valor)
        except Exception:
            pass
    return {}


@router.get("/post_config")
def obtener_post_config(tenant_id: str = Depends(get_tenant_id)):
    """
    Retorna los defaults de posts del negocio (post_config).
    Si no existe la fila, la crea con los valores por defecto de la migración 026.
    """
    _crear_post_config_default(tenant_id)
    fila = query(
        "SELECT tenant_id, template_default, color, font, posicion, mostrar "
        "FROM post_config WHERE tenant_id = %s",
        (tenant_id,)
    )
    if not fila:
        return {}
    cfg = fila[0]
    cfg["mostrar"] = _parsear_mostrar(cfg.get("mostrar"))
    return cfg


@router.put("/post_config")
def actualizar_post_config(
    data: ActualizarPostConfig,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Actualiza los defaults de posts del negocio (PATCH parcial).
    Valida los valores contra las listas permitidas (422 si son inválidos).
    """
    _crear_post_config_default(tenant_id)

    campos = []
    valores = []
    if data.template_default is not None:
        if data.template_default not in TEMPLATES_POST:
            raise HTTPException(status_code=422, detail=f"template_default debe ser uno de: {', '.join(TEMPLATES_POST)}")
        campos.append("template_default = %s")
        valores.append(data.template_default)
    if data.color is not None:
        if data.color not in COLORES_POST:
            raise HTTPException(status_code=422, detail=f"color debe ser uno de: {', '.join(COLORES_POST)}")
        campos.append("color = %s")
        valores.append(data.color)
    if data.font is not None:
        if data.font not in FUENTES_POST:
            raise HTTPException(status_code=422, detail=f"font debe ser uno de: {', '.join(FUENTES_POST)}")
        campos.append("font = %s")
        valores.append(data.font)
    if data.posicion is not None:
        if data.posicion not in POSICIONES_POST:
            raise HTTPException(status_code=422, detail=f"posicion debe ser uno de: {', '.join(POSICIONES_POST)}")
        campos.append("posicion = %s")
        valores.append(data.posicion)
    if data.mostrar is not None:
        mostrar = data.mostrar
        permitidas = {"nombre", "precio", "negocio"}
        if not isinstance(mostrar, dict) or not set(mostrar.keys()).issubset(permitidas):
            raise HTTPException(status_code=422, detail="mostrar debe ser un objeto con solo las claves: nombre, precio, negocio")
        campos.append("mostrar = %s::jsonb")
        valores.append(json.dumps(mostrar))

    if not campos:
        return {"ok": True, "mensaje": "Nada que actualizar"}

    campos.append("updated_at = now()")
    valores.append(tenant_id)
    execute(
        f"UPDATE post_config SET {', '.join(campos)} WHERE tenant_id = %s",
        tuple(valores)
    )
    return {"ok": True, "mensaje": "Defaults de posts actualizados"}
