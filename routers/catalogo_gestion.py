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
import re
from fastapi import APIRouter, Depends, HTTPException
from dependencies import get_tenant_id
from database.conexion import query, execute
from schemas.catalogo import TemplateEnum, ActualizarCatalogo, ActualizarPostConfig

router = APIRouter(prefix="/catalogo_gestion", tags=["Gestión de Catálogo"])

# Claves de fuente display del catálogo (migración 044). Coinciden con
# frontend/src/lib/catalogo-fuentes.ts — claves nuevas = actualizar ambos lados.
_FUENTES_CATALOGO = ("serif", "sistema", "playfair", "cormorant", "poppins", "quicksand", "oswald", "baloo2")

# Valores permitidos para la config de posts (Fase 1 — Posts Automáticos)
TEMPLATES_POST = ("marco", "overlay")  # 'tarjeta' se eliminó (el render la trata como Marco)
FUENTES_POST = ("moderna", "elegante", "redondeada")
POSICIONES_POST = ("arriba", "abajo")

@router.get("")
def obtener_config_catalogo(tenant_id: str = Depends(get_tenant_id)):
    """
    Retorna la configuración del catálogo del tenant (incluye el logo del
    negocio, que vive en la tabla tenants).
    Si no existe, crea un registro con valores por defecto (slug auto-generado).
    """
    resultado = query(
        "SELECT cc.id, cc.slug, cc.activo, cc.tema, cc.template, cc.titulo, cc.subtitulo, cc.fuente, "
        "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, cc.columnas_movil, cc.permitir_descarga, cc.ocultar_agotados, cc.relacion_imagen, "
        "       cc.banner_url, cc.banner_url_movil, cc.hero_estilo, cc.banner_texto_color, cc.banner_mostrar_texto, cc.banner_mostrar_logo, cc.anuncio_texto, "
        "       cc.fondo_url, cc.fondo_modo, cc.fondo_opacidad, cc.fondo_color, "
        "       cc.hero_url, cc.hero_url_movil, cc.hero_color, cc.hero_opacidad, cc.hero_layout, "
        "       cc.created_at, "
        "       t.logo AS logo, "
        "       t.telefono, t.instagram, t.facebook, t.tiktok "
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
            "SELECT cc.id, cc.slug, cc.activo, cc.tema, cc.template, cc.titulo, cc.subtitulo, cc.fuente, "
            "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, cc.columnas_movil, cc.permitir_descarga, cc.ocultar_agotados, cc.relacion_imagen, "
            "       cc.banner_url, cc.banner_url_movil, cc.hero_estilo, cc.banner_texto_color, cc.banner_mostrar_texto, cc.banner_mostrar_logo, cc.anuncio_texto, "
            "       cc.fondo_url, cc.fondo_modo, cc.fondo_opacidad, cc.fondo_color, "
        "       cc.hero_url, cc.hero_url_movil, cc.hero_color, cc.hero_opacidad, cc.hero_layout, "
        "       cc.created_at, "
        "       t.logo AS logo, "
        "       t.telefono, t.instagram, t.facebook, t.tiktok "
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
    if data.fuente is not None:
        # Fuente display: whitelist de claves del frontend (migración 044)
        if data.fuente not in _FUENTES_CATALOGO:
            raise HTTPException(status_code=422, detail="Fuente del catálogo no reconocida")
        campos.append("fuente = %s")
        valores.append(data.fuente)
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
    if data.fondo_url is not None:
        campos.append("fondo_url = %s")
        valores.append(data.fondo_url)
    if data.fondo_modo is not None:
        campos.append("fondo_modo = %s")
        valores.append(data.fondo_modo.value)  # whitelist del Enum ('cover' | 'repeat')
    if data.fondo_opacidad is not None:
        campos.append("fondo_opacidad = %s")
        valores.append(data.fondo_opacidad)
    if data.fondo_color is not None:
        # Color de fondo: hex #RRGGBB o cadena vacía (= color del tema)
        _validar_color_texto(data.fondo_color, "fondo_color")
        campos.append("fondo_color = %s")
        valores.append(data.fondo_color)
    if data.relacion_imagen is not None:
        campos.append("relacion_imagen = %s")
        valores.append(data.relacion_imagen)
    # Modo Hero de la portada (migración 047)
    if data.hero_url is not None:
        campos.append("hero_url = %s")
        valores.append(data.hero_url)
    if data.hero_url_movil is not None:
        campos.append("hero_url_movil = %s")
        valores.append(data.hero_url_movil)
    if data.hero_opacidad is not None:
        campos.append("hero_opacidad = %s")
        valores.append(data.hero_opacidad)
    if data.hero_color is not None:
        # Color del velo: hex #RRGGBB o cadena vacía (= gradiente del tema)
        _validar_color_texto(data.hero_color, "hero_color")
        campos.append("hero_color = %s")
        valores.append(data.hero_color)
    # Layout personalizable del hero (migración 049, Fase 1)
    if data.hero_layout is not None:
        layout = data.hero_layout
        if not isinstance(layout, dict):
            raise HTTPException(status_code=422, detail="hero_layout debe ser un objeto")
        # Solo se guardan las claves conocidas del bloque hero (esto explica el
        # subset check): el editor manda el layout COMPLETO al instante, claves
        # nuevas de Fase 2 se agregan aquí. texto_posicion con whitelist.
        permitidas = {"texto_posicion", "mostrar_logo", "mostrar_redes", "mostrar_boton", "elementos", "elementos_movil"}
        if not set(layout.keys()).issubset(permitidas):
            raise HTTPException(status_code=422, detail="hero_layout contiene claves no permitidas")
        if layout.get("texto_posicion") is not None and layout["texto_posicion"] not in ("centro", "arriba-izq", "abajo-izq"):
            raise HTTPException(status_code=422, detail="texto_posicion debe ser centro, arriba-izq o abajo-izq")
        for clave in ("mostrar_logo", "mostrar_redes", "mostrar_boton"):
            if layout.get(clave) is not None and not isinstance(layout[clave], bool):
                raise HTTPException(status_code=422, detail=f"{clave} debe ser booleano")
        # Mini-canva (Fase 2): elementos posicionados libremente. EL body manda
        # el array completo (estado final tras cada gesto del editor).
        # Coordenadas en % del hero (0-100). Hay DOS sets: 'elementos' (el
        # canva de escritorio) y 'elementos_movil' (vista teléfono, opcional;
        # ausente = hereda el de escritorio escalado).
        for clave_set in ("elementos", "elementos_movil"):
            if clave_set not in layout:
                continue
            elementos = layout[clave_set]
            if not isinstance(elementos, list) or len(elementos) > 30:
                raise HTTPException(status_code=422, detail=f"{clave_set} debe ser una lista de hasta 30 elementos")
            claves_elem = {"id", "tipo", "x", "y", "w", "texto", "fuente", "tamano", "color", "peso", "align", "red"}
            tipos_elem = ("texto", "redes", "boton", "logo", "red")
            redes_validas = ("instagram", "facebook", "tiktok", "whatsapp")
            vistos: set = set()
            for el in elementos:
                if not isinstance(el, dict) or not set(el.keys()).issubset(claves_elem):
                    raise HTTPException(status_code=422, detail=f"{clave_set} contiene campos no permitidos")
                if el.get("tipo") not in tipos_elem:
                    raise HTTPException(status_code=422, detail="tipo debe ser texto, logo, redes, red o boton")
                if el.get("tipo") == "red" and el.get("red") not in redes_validas:
                    raise HTTPException(status_code=422, detail="red debe ser instagram, facebook, tiktok o whatsapp")
                try:
                    x = float(el["x"]); y = float(el["y"])
                except (TypeError, KeyError, ValueError):
                    raise HTTPException(status_code=422, detail="cada elemento necesita x e y numéricos")
                if not (0 <= x <= 100 and 0 <= y <= 100):
                    raise HTTPException(status_code=422, detail="x e y deben estar entre 0 y 100")
                if el.get("w") is not None:
                    w = float(el["w"])
                    if not (2 <= w <= 100):
                        raise HTTPException(status_code=422, detail="w debe estar entre 2 y 100")
                if el.get("tamano") is not None:
                    t = float(el["tamano"])
                    if not (8 <= t <= 320):
                        raise HTTPException(status_code=422, detail="tamano debe estar entre 8 y 320")
                if el.get("texto") is not None:
                    if not isinstance(el["texto"], str) or len(el["texto"]) > 300:
                        raise HTTPException(status_code=422, detail="texto demasiado largo")
                if el.get("fuente") is not None and el["fuente"] not in _FUENTES_CATALOGO:
                    raise HTTPException(status_code=422, detail="Fuente del elemento no reconocida")
                if el.get("color") is not None:
                    _validar_color_texto(el["color"], "color del elemento")
                if el.get("peso") is not None:
                    if not (isinstance(el["peso"], str) and str(el["peso"]) in ("400", "600", "700", "800")):
                        raise HTTPException(status_code=422, detail="peso debe ser 400, 600, 700 u 800")
                if el.get("align") is not None:
                    if el["align"] not in ("left", "center", "right"):
                        raise HTTPException(status_code=422, detail="align debe ser left, center o right")
                eid = el.get("id")
                if eid in vistos:
                    raise HTTPException(status_code=422, detail="ids de elementos duplicados")
                if eid is not None:
                    vistos.add(eid)
        campos.append("hero_layout = COALESCE(hero_layout, '{}'::jsonb) || %s::jsonb")
        valores.append(json.dumps(layout))

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

def _crear_post_config_default(tenant_id: str) -> None:
    """Crea la fila de post_config con los defaults si aún no existe."""
    existe = query("SELECT tenant_id FROM post_config WHERE tenant_id = %s", (tenant_id,))
    if not existe:
        execute(
            "INSERT INTO post_config (tenant_id) VALUES (%s)",
            (tenant_id,)
        )


_RE_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validar_color_texto(valor, campo: str) -> None:
    """Valida un color de texto: hex #RRGGBB o cadena vacía (= automático)."""
    if not isinstance(valor, str) or not (valor == "" or _RE_HEX_COLOR.match(valor)):
        raise HTTPException(
            status_code=422,
            detail=f"{campo} debe ser un color hex (#RRGGBB) o una cadena vacía para usar el color por defecto",
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
        "SELECT tenant_id, template_default, color, font, posicion, mostrar, cta_texto, cta_url, "
        "color_primario, color_secundario "
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
    if data.color_primario is not None:
        _validar_color_texto(data.color_primario, "color_primario")
        campos.append("color_primario = %s")
        valores.append(data.color_primario)
    if data.color_secundario is not None:
        _validar_color_texto(data.color_secundario, "color_secundario")
        campos.append("color_secundario = %s")
        valores.append(data.color_secundario)

    if not campos:
        return {"ok": True, "mensaje": "Nada que actualizar"}

    campos.append("updated_at = now()")
    valores.append(tenant_id)
    execute(
        f"UPDATE post_config SET {', '.join(campos)} WHERE tenant_id = %s",
        tuple(valores)
    )
    return {"ok": True, "mensaje": "Defaults de posts actualizados"}
