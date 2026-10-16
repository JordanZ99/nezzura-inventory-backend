# ==============================================================================
# backend/database/posts.py
# Posts automáticos (Fase 1): override de la tarjeta de post POR PRODUCTO
# (doc: POSTS_AUTOMATICOS.md). Un producto sin override (post_override NULL)
# usa los defaults del negocio (tabla post_config).
#
# Refactor de routers/inventario.py: la validación y el SQL del override viven
# aquí; el router solo rutea HTTP y mapea los dicts de dominio a HTTPException
# (mismo patrón que database/imagenes.py).
#
# Contrato de dominio: {"ok": True, ...} en éxito; {"ok": False, "status": 422|404,
# "mensaje": ...} en error.
# ==============================================================================

import json
import re

from database.conexion import query, execute

# Valores permitidos (mismos que en routers/catalogo_gestion.py — post_config)
TEMPLATES_POST = ("marco", "overlay")  # 'tarjeta' se eliminó (el render la trata como Marco)
FUENTES_POST = ("moderna", "elegante", "redondeada")
POSICIONES_POST = ("arriba", "abajo")

# Color de texto personalizable (nombre+negocio = primario, precio = secundario):
# hex #RRGGBB o '' = automático por plantilla.
RE_HEX_COLOR_POST = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validar_color_texto_post(valor, campo: str) -> str | None:
    """
    Valida un color de texto del override: hex #RRGGBB o '' (automático).
    Devuelve el mensaje de error, o None si el valor es válido.
    """
    if not isinstance(valor, str) or not (valor == "" or RE_HEX_COLOR_POST.match(valor)):
        return f"{campo} debe ser un color hex (#RRGGBB) o una cadena vacía para usar el color por defecto"
    return None


def guardar_post_override(producto: str, ov: dict | None, tenant_id: str) -> dict:
    """
    Guarda (o quita) el override de la tarjeta de post de un producto.

    - ov = {template?, color?, font?, posicion?, mostrar?} → se guarda
      tal cual (las claves ausentes se heredan de los defaults del negocio).
    - ov = null → se quita el override: el producto vuelve a usar los
      defaults del negocio.

    Valida los valores contra las listas permitidas (422 si son inválidos).
    """
    if ov is not None:
        if not isinstance(ov, dict):
            return {"ok": False, "status": 422, "mensaje": "post_override debe ser un objeto o null"}
        if "template" in ov and ov["template"] not in TEMPLATES_POST:
            return {"ok": False, "status": 422, "mensaje": f"template debe ser uno de: {', '.join(TEMPLATES_POST)}"}
        if "font" in ov and ov["font"] not in FUENTES_POST:
            return {"ok": False, "status": 422, "mensaje": f"font debe ser uno de: {', '.join(FUENTES_POST)}"}
        if "posicion" in ov and ov["posicion"] not in POSICIONES_POST:
            return {"ok": False, "status": 422, "mensaje": f"posicion debe ser uno de: {', '.join(POSICIONES_POST)}"}
        if "mostrar" in ov:
            mostrar = ov["mostrar"]
            permitidas = {"nombre", "precio", "negocio"}
            if not isinstance(mostrar, dict) or not set(mostrar.keys()).issubset(permitidas):
                return {"ok": False, "status": 422,
                        "mensaje": "mostrar debe ser un objeto con solo las claves: nombre, precio, negocio"}
        if "color_primario" in ov:
            err = _validar_color_texto_post(ov["color_primario"], "color_primario")
            if err:
                return {"ok": False, "status": 422, "mensaje": err}
        if "color_secundario" in ov:
            err = _validar_color_texto_post(ov["color_secundario"], "color_secundario")
            if err:
                return {"ok": False, "status": 422, "mensaje": err}

    # Verificar que el producto exista y pertenezca al tenant (evita crear
    # overrides sobre productos inexistentes/ajenos)
    existe = query(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    if not existe:
        return {"ok": False, "status": 404, "mensaje": "Producto no encontrado"}

    execute(
        "UPDATE productos SET post_override = %s::jsonb WHERE Producto = %s AND tenant_id = %s",
        (json.dumps(ov) if ov is not None else None, producto, tenant_id)
    )
    return {"ok": True, "producto": producto, "post_override": ov}


# ==============================================================================
# ── Defaults del negocio para las tarjetas de post (post_config) ──────────────
# La configuración en cascada: post_config (defaults) + productos.post_override.
# Un producto sin override usa estos defaults; el override vive por producto.
# Movida desde routers/catalogo_gestion.py (mismas validaciones y SQL).
# ==============================================================================


def _crear_post_config_default(tenant_id: str) -> None:
    """Crea la fila de post_config con los defaults si aún no existe."""
    existe = query("SELECT tenant_id FROM post_config WHERE tenant_id = %s", (tenant_id,))
    if not existe:
        execute(
            "INSERT INTO post_config (tenant_id) VALUES (%s)",
            (tenant_id,)
        )


def _parsear_mostrar(valor) -> dict:
    """
    Normaliza el JSONB 'mostrar' (psycopg2 lo entrega como texto si no hay
    typecaster registrado; si ya es dict, se devuelve tal cual).
    """
    if isinstance(valor, dict):
        return valor
    if isinstance(valor, str) and valor.strip():
        try:
            return json.loads(valor)
        except Exception:
            pass
    return {}


def obtener_post_config(tenant_id: str) -> dict:
    """
    Defaults de posts del negocio (post_config). Si no existe la fila, la
    crea con los valores por defecto de la migración 026.
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


def actualizar_post_config(campos: dict, tenant_id: str) -> dict:
    """
    Actualiza los defaults de posts del negocio (PATCH parcial). `campos` trae
    las claves del schema ActualizarPostConfig (model_dump); None = no tocar.

    Valida los valores contra las listas permitidas: devuelve
    {"ok": False, "status": 422, "mensaje"} si son inválidos.
    """
    _crear_post_config_default(tenant_id)

    asignaciones = []
    valores = []
    if campos.get("template_default") is not None:
        if campos["template_default"] not in TEMPLATES_POST:
            return {"ok": False, "status": 422, "mensaje": f"template_default debe ser uno de: {', '.join(TEMPLATES_POST)}"}
        asignaciones.append("template_default = %s")
        valores.append(campos["template_default"])
    if campos.get("font") is not None:
        if campos["font"] not in FUENTES_POST:
            return {"ok": False, "status": 422, "mensaje": f"font debe ser uno de: {', '.join(FUENTES_POST)}"}
        asignaciones.append("font = %s")
        valores.append(campos["font"])
    if campos.get("posicion") is not None:
        if campos["posicion"] not in POSICIONES_POST:
            return {"ok": False, "status": 422, "mensaje": f"posicion debe ser uno de: {', '.join(POSICIONES_POST)}"}
        asignaciones.append("posicion = %s")
        valores.append(campos["posicion"])
    if campos.get("mostrar") is not None:
        mostrar = campos["mostrar"]
        permitidas = {"nombre", "precio", "negocio"}
        if not isinstance(mostrar, dict) or not set(mostrar.keys()).issubset(permitidas):
            return {"ok": False, "status": 422,
                    "mensaje": "mostrar debe ser un objeto con solo las claves: nombre, precio, negocio"}
        asignaciones.append("mostrar = %s::jsonb")
        valores.append(json.dumps(mostrar))
    if campos.get("color_primario") is not None:
        err = _validar_color_texto_post(campos["color_primario"], "color_primario")
        if err:
            return {"ok": False, "status": 422, "mensaje": err}
        asignaciones.append("color_primario = %s")
        valores.append(campos["color_primario"])
    if campos.get("color_secundario") is not None:
        err = _validar_color_texto_post(campos["color_secundario"], "color_secundario")
        if err:
            return {"ok": False, "status": 422, "mensaje": err}
        asignaciones.append("color_secundario = %s")
        valores.append(campos["color_secundario"])

    if not asignaciones:
        return {"ok": True, "mensaje": "Nada que actualizar"}

    asignaciones.append("updated_at = now()")
    valores.append(tenant_id)
    execute(
        f"UPDATE post_config SET {', '.join(asignaciones)} WHERE tenant_id = %s",
        tuple(valores)
    )
    return {"ok": True, "mensaje": "Defaults de posts actualizados"}
