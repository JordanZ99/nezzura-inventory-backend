# ==============================================================================
# backend/database/catalogo.py
# Configuración del catálogo público (catalogo_config): lectura, PATCH parcial
# del gestor y ensamblado del catálogo público por slug.
#
# Refactor de routers/catalogo_gestion.py y routers/publico.py: el SQL y las
# validaciones viven aquí; los routers solo rutean HTTP y mapean los dicts de
# dominio a HTTPException (mismo patrón que database/imagenes.py y tenants.py).
#
# Contrato de dominio: el payload en éxito; {"ok": False, "status": 422|404,
# "mensaje": ...} en error de validación / no encontrado.
# ==============================================================================

import json

from database.conexion import query, execute
from database.helpers import _obtener_categorias_subquery
from database.posts import _validar_color_texto_post as _validar_color_texto

# Claves de fuente display del catálogo (migración 044). Coinciden con
# frontend/src/lib/catalogo-fuentes.ts — claves nuevas = actualizar ambos lados.
FUENTES_CATALOGO = ("serif", "sistema", "playfair", "cormorant", "poppins", "quicksand", "oswald", "baloo2")

# SELECT compartido de la config del gestor: config del catálogo + logo y
# contacto del negocio (tenants). Antes estaba duplicado literal en el GET.
_SELECT_CONFIG_GESTION = (
    "SELECT cc.id, cc.slug, cc.activo, cc.tema, cc.template, cc.titulo, cc.subtitulo, cc.fuente, "
    "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, cc.columnas_movil, cc.permitir_descarga, cc.ocultar_agotados, cc.relacion_imagen, "
    "       cc.banner_url, cc.banner_url_movil, cc.hero_estilo, cc.banner_texto_color, cc.banner_mostrar_texto, cc.banner_mostrar_logo, cc.anuncio_texto, "
    "       cc.fondo_url, cc.fondo_modo, cc.fondo_opacidad, cc.fondo_color, "
    "       cc.hero_url, cc.hero_url_movil, cc.hero_color, cc.hero_opacidad, cc.hero_layout, "
    "       cc.created_at, "
    "       t.logo AS logo, "
    "       t.telefono, t.instagram, t.facebook, t.tiktok, t.maps "
    "FROM catalogo_config cc "
    "LEFT JOIN tenants t ON cc.tenant_id = t.id "
    "WHERE cc.tenant_id = %s"
)


def _asegurar_config(tenant_id: str) -> None:
    """
    Crea la fila de catalogo_config con los valores por defecto si aún no
    existe. El slug lo genera la BD (gen_random_uuid).
    """
    existe = query("SELECT id FROM catalogo_config WHERE tenant_id = %s", (tenant_id,))
    if not existe:
        execute("INSERT INTO catalogo_config (tenant_id) VALUES (%s)", (tenant_id,))


def obtener_config(tenant_id: str) -> dict:
    """
    Config del catálogo del tenant (incluye el logo del negocio, que vive en
    la tabla tenants). Si no existe, crea un registro con valores por defecto
    (slug auto-generado).
    """
    resultado = query(_SELECT_CONFIG_GESTION, (tenant_id,))
    if not resultado:
        _asegurar_config(tenant_id)
        resultado = query(_SELECT_CONFIG_GESTION, (tenant_id,))
    return resultado[0] if resultado else {}


def _validar_hero_layout(layout) -> str | None:
    """
    Valida el layout del bloque hero (migración 049, Fases 1/2): posiciones,
    flags y el mini-canva de elementos posicionados libremente.
    Devuelve el mensaje de error, o None si el layout es válido.
    """
    if not isinstance(layout, dict):
        return "hero_layout debe ser un objeto"
    # Solo se guardan las claves conocidas del bloque hero (esto explica el
    # subset check): el editor manda el layout COMPLETO al instante, claves
    # nuevas de Fase 2 se agregan aquí. texto_posicion con whitelist.
    permitidas = {"texto_posicion", "mostrar_logo", "mostrar_redes", "mostrar_boton", "elementos", "elementos_movil"}
    if not set(layout.keys()).issubset(permitidas):
        return "hero_layout contiene claves no permitidas"
    if layout.get("texto_posicion") is not None and layout["texto_posicion"] not in ("centro", "arriba-izq", "abajo-izq"):
        return "texto_posicion debe ser centro, arriba-izq o abajo-izq"
    for clave in ("mostrar_logo", "mostrar_redes", "mostrar_boton"):
        if layout.get(clave) is not None and not isinstance(layout[clave], bool):
            return f"{clave} debe ser booleano"
    # Mini-canva (Fase 2): elementos posicionados libremente. El body manda
    # el array completo (estado final tras cada gesto del editor).
    # Coordenadas en % del hero (0-100). Hay DOS sets: 'elementos' (el
    # canva de escritorio) y 'elementos_movil' (vista teléfono, opcional;
    # ausente = hereda el de escritorio escalado).
    for clave_set in ("elementos", "elementos_movil"):
        if clave_set not in layout:
            continue
        elementos = layout[clave_set]
        if not isinstance(elementos, list) or len(elementos) > 30:
            return f"{clave_set} debe ser una lista de hasta 30 elementos"
        claves_elem = {"id", "tipo", "x", "y", "w", "texto", "fuente", "tamano", "color", "peso", "align", "red",
                       "radio", "accion", "estilo", "color_texto"}
        tipos_elem = ("texto", "redes", "boton", "logo", "red")
        redes_validas = ("instagram", "facebook", "tiktok", "whatsapp")
        vistos: set = set()
        for el in elementos:
            if not isinstance(el, dict) or not set(el.keys()).issubset(claves_elem):
                return f"{clave_set} contiene campos no permitidos"
            if el.get("tipo") not in tipos_elem:
                return "tipo debe ser texto, logo, redes, red o boton"
            if el.get("tipo") == "red" and el.get("red") not in redes_validas:
                return "red debe ser instagram, facebook, tiktok o whatsapp"
            try:
                x = float(el["x"]); y = float(el["y"])
            except (TypeError, KeyError, ValueError):
                return "cada elemento necesita x e y numéricos"
            if not (0 <= x <= 100 and 0 <= y <= 100):
                return "x e y deben estar entre 0 y 100"
            if el.get("w") is not None:
                w = float(el["w"])
                if not (2 <= w <= 100):
                    return "w debe estar entre 2 y 100"
            if el.get("tamano") is not None:
                t = float(el["tamano"])
                if not (8 <= t <= 320):
                    return "tamano debe estar entre 8 y 320"
            if el.get("texto") is not None:
                if not isinstance(el["texto"], str) or len(el["texto"]) > 300:
                    return "texto demasiado largo"
            if el.get("fuente") is not None and el["fuente"] not in FUENTES_CATALOGO:
                return "Fuente del elemento no reconocida"
            if el.get("color") is not None:
                err = _validar_color_texto(el["color"], "color del elemento")
                if err:
                    return err
            if el.get("color_texto") is not None:
                err = _validar_color_texto(el["color_texto"], "color_texto del elemento")
                if err:
                    return err
            # Solo boton (migración 049): redondeo, función, estilo y color del texto
            if el.get("radio") is not None:
                try:
                    r = float(el["radio"])
                except (TypeError, ValueError):
                    return "radio debe ser numérico"
                if not (0 <= r <= 999):
                    return "radio debe estar entre 0 y 999 (999 = píldora)"
            if el.get("accion") is not None and el["accion"] not in ("catalogo", "maps"):
                return "accion debe ser catalogo o maps"
            if el.get("estilo") is not None and el["estilo"] not in ("solido", "outline"):
                return "estilo debe ser solido o outline"
            if el.get("peso") is not None:
                if not (isinstance(el["peso"], str) and str(el["peso"]) in ("400", "600", "700", "800")):
                    return "peso debe ser 400, 600, 700 u 800"
            if el.get("align") is not None:
                if el["align"] not in ("left", "center", "right"):
                    return "align debe ser left, center o right"
            eid = el.get("id")
            if eid in vistos:
                return "ids de elementos duplicados"
            if eid is not None:
                vistos.add(eid)
    return None


def actualizar_config(campos: dict, tenant_id: str) -> dict:
    """
    Actualiza la configuración del catálogo (PATCH parcial). `campos` trae las
    claves del schema ActualizarCatalogo (model_dump mode=json); None = no
    tocar. Si no existe el registro, lo crea primero.

    Valida los valores contra las listas permitidas: devuelve
    {"ok": False, "status": 422, "mensaje"} si son inválidos.
    """
    _asegurar_config(tenant_id)

    asignaciones = []
    valores = []
    if campos.get("activo") is not None:
        asignaciones.append("activo = %s")
        valores.append(campos["activo"])
    if campos.get("tema") is not None:
        asignaciones.append("tema = %s")
        valores.append(campos["tema"])
    if campos.get("template") is not None:
        asignaciones.append("template = %s")
        valores.append(campos["template"])
    if campos.get("fuente") is not None:
        # Fuente display: whitelist de claves del frontend (migración 044)
        if campos["fuente"] not in FUENTES_CATALOGO:
            return {"ok": False, "status": 422, "mensaje": "Fuente del catálogo no reconocida"}
        asignaciones.append("fuente = %s")
        valores.append(campos["fuente"])
    if campos.get("titulo") is not None:
        asignaciones.append("titulo = %s")
        valores.append(campos["titulo"])
    if campos.get("subtitulo") is not None:
        asignaciones.append("subtitulo = %s")
        valores.append(campos["subtitulo"])
    if campos.get("mostrar_precios") is not None:
        asignaciones.append("mostrar_precios = %s")
        valores.append(campos["mostrar_precios"])
    if campos.get("mostrar_stock") is not None:
        asignaciones.append("mostrar_stock = %s")
        valores.append(campos["mostrar_stock"])
    if campos.get("mostrar_categorias") is not None:
        asignaciones.append("mostrar_categorias = %s")
        valores.append(campos["mostrar_categorias"])
    if campos.get("agrupar_por_categoria") is not None:
        asignaciones.append("agrupar_por_categoria = %s")
        valores.append(campos["agrupar_por_categoria"])
    if campos.get("columnas_movil") is not None:
        asignaciones.append("columnas_movil = %s")
        valores.append(campos["columnas_movil"])
    if campos.get("permitir_descarga") is not None:
        asignaciones.append("permitir_descarga = %s")
        valores.append(campos["permitir_descarga"])
    if campos.get("ocultar_agotados") is not None:
        asignaciones.append("ocultar_agotados = %s")
        valores.append(campos["ocultar_agotados"])
    if campos.get("banner_url") is not None:
        asignaciones.append("banner_url = %s")
        valores.append(campos["banner_url"])
    if campos.get("banner_url_movil") is not None:
        asignaciones.append("banner_url_movil = %s")
        valores.append(campos["banner_url_movil"])
    if campos.get("hero_estilo") is not None:
        asignaciones.append("hero_estilo = %s")
        valores.append(campos["hero_estilo"])
    if campos.get("banner_texto_color") is not None:
        asignaciones.append("banner_texto_color = %s")
        valores.append(campos["banner_texto_color"])
    if campos.get("banner_mostrar_texto") is not None:
        asignaciones.append("banner_mostrar_texto = %s")
        valores.append(campos["banner_mostrar_texto"])
    if campos.get("banner_mostrar_logo") is not None:
        asignaciones.append("banner_mostrar_logo = %s")
        valores.append(campos["banner_mostrar_logo"])
    if campos.get("anuncio_texto") is not None:
        asignaciones.append("anuncio_texto = %s")
        valores.append(campos["anuncio_texto"])
    if campos.get("fondo_url") is not None:
        asignaciones.append("fondo_url = %s")
        valores.append(campos["fondo_url"])
    if campos.get("fondo_modo") is not None:
        asignaciones.append("fondo_modo = %s")
        valores.append(campos["fondo_modo"])  # whitelist del Enum ('cover' | 'repeat')
    if campos.get("fondo_opacidad") is not None:
        asignaciones.append("fondo_opacidad = %s")
        valores.append(campos["fondo_opacidad"])
    if campos.get("fondo_color") is not None:
        # Color de fondo: hex #RRGGBB o cadena vacía (= color del tema)
        err = _validar_color_texto(campos["fondo_color"], "fondo_color")
        if err:
            return {"ok": False, "status": 422, "mensaje": err}
        asignaciones.append("fondo_color = %s")
        valores.append(campos["fondo_color"])
    if campos.get("relacion_imagen") is not None:
        asignaciones.append("relacion_imagen = %s")
        valores.append(campos["relacion_imagen"])
    # Modo Hero de la portada (migración 047)
    if campos.get("hero_url") is not None:
        asignaciones.append("hero_url = %s")
        valores.append(campos["hero_url"])
    if campos.get("hero_url_movil") is not None:
        asignaciones.append("hero_url_movil = %s")
        valores.append(campos["hero_url_movil"])
    if campos.get("hero_opacidad") is not None:
        asignaciones.append("hero_opacidad = %s")
        valores.append(campos["hero_opacidad"])
    if campos.get("hero_color") is not None:
        # Color del velo: hex #RRGGBB o cadena vacía (= gradiente del tema)
        err = _validar_color_texto(campos["hero_color"], "hero_color")
        if err:
            return {"ok": False, "status": 422, "mensaje": err}
        asignaciones.append("hero_color = %s")
        valores.append(campos["hero_color"])
    # Layout personalizable del hero (migración 049, Fase 1)
    if campos.get("hero_layout") is not None:
        layout = campos["hero_layout"]
        err = _validar_hero_layout(layout)
        if err:
            return {"ok": False, "status": 422, "mensaje": err}
        asignaciones.append("hero_layout = COALESCE(hero_layout, '{}'::jsonb) || %s::jsonb")
        valores.append(json.dumps(layout))

    if not asignaciones:
        return {"ok": True, "mensaje": "Nada que actualizar"}

    asignaciones.append("updated_at = now()")
    valores.append(tenant_id)
    execute(
        f"UPDATE catalogo_config SET {', '.join(asignaciones)} WHERE tenant_id = %s",
        tuple(valores)
    )
    return {"ok": True, "mensaje": "Configuración del catálogo actualizada"}


def obtener_catalogo_publico(slug: str) -> dict:
    """
    Catálogo público del tenant identificado por su slug (UUID v4 aleatorio
    generado al activar el catálogo; no derivable del tenant_id).

    SEGURIDAD (misma que cuando vivía en routers/publico.py):
      - El tenant_id se resuelve por slug INTERNAMENTE y nunca se devuelve.
      - Solo campos públicos de los productos: sin costo, ganancia_bruta,
        codigo_interno ni tenant_id.
      - Solo expone el catálogo si activo = true; si el tenant oculta los
        agotados, se filtran aquí (el cliente no debe descargar datos que no
        debe ver; servicios y compuestos SIEMPRE se muestran: no se agotan).

    Devuelve {"ok": False, "status": 404, "mensaje"} si el slug no existe o el
    catálogo no está activo.
    """
    # 1. Una sola query: obtiene config + tenant_id + logo al mismo tiempo.
    #    Solo retorna resultado si activo = true (catálogo habilitado).
    #    LEFT JOIN con tenants para obtener el logo personalizado del negocio.
    config_rows = query(
        "SELECT cc.tenant_id, cc.tema, cc.template, cc.titulo, cc.subtitulo, cc.fuente, "
        "       cc.mostrar_precios, cc.mostrar_stock, cc.mostrar_categorias, cc.agrupar_por_categoria, cc.columnas_movil, cc.permitir_descarga, cc.ocultar_agotados, cc.relacion_imagen, "
        "       cc.banner_url, cc.banner_url_movil, cc.hero_estilo, cc.banner_texto_color, cc.banner_mostrar_texto, cc.banner_mostrar_logo, cc.anuncio_texto, "
        "       cc.fondo_url, cc.fondo_modo, cc.fondo_opacidad, cc.fondo_color, "
        "       cc.hero_url, cc.hero_url_movil, cc.hero_color, cc.hero_opacidad, cc.hero_layout, "
        "       t.logo, t.telefono, t.correo, t.instagram, t.facebook, t.tiktok, t.sitio_web, t.maps "
        "FROM catalogo_config cc "
        "LEFT JOIN tenants t ON cc.tenant_id = t.id "
        "WHERE cc.slug = %s AND cc.activo = true",
        (slug,)
    )
    if not config_rows:
        return {"ok": False, "status": 404, "mensaje": "Catálogo no encontrado o no activo"}

    cfg = config_rows[0]

    # tenant_id se usa INTERNAMENTE — nunca se devuelve al cliente
    tenant_id = cfg["tenant_id"]

    # 2. Productos activos: solo campos públicos.
    #    Se incluye stock_total para que el cliente sepa si hay disponibilidad,
    #    pero NO se incluye costo, ganancia_bruta, codigo_interno ni tenant_id.
    #
    #    NOTA: precio_venta y stock_total viven en la tabla `lotes`, no en `productos`.
    #    Esta query sigue el mismo patrón que get_inventario_consolidado() en lotes.py.
    # Solo categorías marcadas como visibles en el catálogo
    cat_subquery = _obtener_categorias_subquery("p", visible_only=True)
    productos = query(f"""
        SELECT
            p.Producto                    AS producto,
            p.Descripcion                 AS descripcion,
            p.Imagen                      AS imagen,
            p.sufijo_precio               AS sufijo_precio,
            p.tamano_cm                   AS tamano_cm,
            p.tipo_producto               AS tipo_producto,
            -- Las variaciones traen su propio `stock` (ver query de variaciones abajo)
            -- Servicios (sin lotes) usan su precio de servicio; productos normales el MAX de lotes
            -- Precio MAX de lotes CON stock; si ninguno tiene, cae al MAX de todos
            COALESCE(MAX(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_venta,
            -- Rango de precios de los lotes CON stock (para mostrar "$X – $Y")
            COALESCE(MIN(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_min,
            COALESCE(MAX(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_max,
            CASE WHEN p.tipo_producto IN ('servicio', 'compuesto') THEN 0
                 ELSE COALESCE(SUM(l.Stock_Lote), 0) END          AS stock_total,
            {cat_subquery}
        FROM productos p
        LEFT JOIN lotes l ON l.producto_id = p.id AND l.tenant_id = p.tenant_id AND l.Estado = 'Activo'
        WHERE p.Estado = 'Activo' AND p.tenant_id = %s
        AND p.visible_en_catalogo = true
        GROUP BY p.Producto, p.Descripcion, p.Imagen, p.sufijo_precio, p.tamano_cm, p.tipo_producto, p.precio_servicio, p.id
        ORDER BY p.Producto ASC
    """, (tenant_id,))

    # 3. Galería completa por producto: foto principal + extras (producto_imagenes).
    #    producto_imagenes solo existe para tenants Plus; si no hay filas, la
    #    galería queda solo con la foto principal (o vacía si no tiene foto).
    extras = query(
        "SELECT p.Producto AS producto, pi.url AS url "
        "FROM producto_imagenes pi "
        "JOIN productos p ON pi.producto_id = p.id "
        "WHERE p.tenant_id = %s "
        "ORDER BY p.Producto ASC, pi.orden ASC",
        (tenant_id,)
    )
    extras_por_producto: dict = {}
    for fila in extras:
        extras_por_producto.setdefault(fila["producto"], []).append(fila["url"])

    # 3b. Variaciones por producto (nombre + precio propio).
    #     Si un producto tiene variaciones, el catálogo muestra "desde $X"
    #     (precio mínimo) y el modal permite elegir la variación.
    # Suma el stock de los lotes ligados a cada variación (cada variación
    # lleva su propio inventario).
    variaciones = query("""
        SELECT p.Producto AS producto, v.id, v.nombre, v.precio, v.foto,
               COALESCE((SELECT SUM(l.Stock_Lote) FROM lotes l
                         WHERE l.variacion_id = v.id AND l.Estado='Activo'
                           AND l.tenant_id = v.tenant_id), 0) AS stock
        FROM producto_variaciones v
        JOIN productos p ON p.id = v.producto_id
        WHERE p.tenant_id = %s
        ORDER BY p.Producto ASC, v.nombre ASC
    """, (tenant_id,))
    variaciones_por_producto: dict = {}
    for v in variaciones:
        variaciones_por_producto.setdefault(v["producto"], []).append({
            "id": v["id"],
            "nombre": v["nombre"],
            "precio": float(v["precio"] or 0),
            "foto": v.get("foto") or "",
            "stock": float(v["stock"] or 0),
        })

    # Armar el campo imagenes (sin duplicados y sin "No hay foto")
    for p in productos:
        galeria: list[str] = []
        if p.get("imagen") and p["imagen"] != "No hay foto":
            galeria.append(p["imagen"])
        for url in extras_por_producto.get(p["producto"], []):
            if url and url not in galeria:
                galeria.append(url)
        p["imagenes"] = galeria
        p["variaciones"] = variaciones_por_producto.get(p["producto"], [])

    # Si el tenant oculta los productos agotados, excluirlos de la respuesta.
    if bool(cfg.get("ocultar_agotados")):
        productos = [
            p for p in productos
            if p.get("tipo_producto") in ("servicio", "compuesto") or (p.get("stock_total") or 0) > 0
        ]

    # 4. Orden manual de las categorías (drag & drop desde Personalización).
    #     El catálogo público agrupa por categoría: el frontend recibe el mapa
    #     nombre→orden y respeta ese orden; todo lo que no esté en el mapa
    #     (ej. "Sin categoría") sigue al final, alfabético.
    ordenes = query(
        "SELECT nombre, orden FROM categorias WHERE tenant_id = %s AND orden IS NOT NULL ORDER BY orden ASC",
        (tenant_id,)
    )
    mapa_orden = {fila["nombre"]: fila["orden"] for fila in ordenes}

    # 5. Datos de contacto del negocio (migración 043): solo columnas públicas
    #    escritas voluntariamente por el tenant. Se normalizan (trim) y se
    #    devuelven como strings vacíos cuando no existe; el footer del catálogo
    #    renderiza únicamente lo que venga lleno.
    contacto = {
        "telefono": (cfg.get("telefono") or "").strip(),
        "correo": (cfg.get("correo") or "").strip(),
        "instagram": (cfg.get("instagram") or "").strip(),
        "facebook": (cfg.get("facebook") or "").strip(),
        "tiktok": (cfg.get("tiktok") or "").strip(),
        "sitio_web": (cfg.get("sitio_web") or "").strip(),
        "maps": (cfg.get("maps") or "").strip(),
    }

    return {
        "config": {
            "tema": cfg["tema"],
            "template": cfg["template"],
            "titulo": cfg["titulo"],
            "subtitulo": cfg.get("subtitulo", ""),
            "fuente": cfg.get("fuente") or "serif",
            "mostrar_precios": cfg["mostrar_precios"],
            # El stock se muestra por defecto: null/true → true
            "mostrar_stock": cfg["mostrar_stock"] is not False,
            "mostrar_categorias": cfg["mostrar_categorias"],
            "agrupar_por_categoria": cfg.get("agrupar_por_categoria"),
            "columnas_movil": cfg.get("columnas_movil") or 2,
            "permitir_descarga": bool(cfg.get("permitir_descarga")),
            "ocultar_agotados": bool(cfg.get("ocultar_agotados")),
            "banner_url": cfg.get("banner_url") or "",
            "banner_url_movil": cfg.get("banner_url_movil") or "",
            "hero_estilo": cfg.get("hero_estilo") or "gradiente",
            "banner_texto_color": cfg.get("banner_texto_color") or "#ffffff",
            "banner_mostrar_texto": cfg.get("banner_mostrar_texto") is not False,
            "banner_mostrar_logo": cfg.get("banner_mostrar_logo") is not False,
            "anuncio_texto": cfg.get("anuncio_texto") or "",
            "relacion_imagen": cfg.get("relacion_imagen") or "1:1",
            "logo": cfg.get("logo") or "",
            # Fondo del catálogo con imagen (migración 045): '' = usa el tema
            "fondo_url": cfg.get("fondo_url") or "",
            "fondo_modo": cfg.get("fondo_modo") if cfg.get("fondo_modo") in ("cover", "repeat") else "cover",
            "fondo_opacidad": int(cfg["fondo_opacidad"]) if cfg.get("fondo_opacidad") is not None else 100,
            "fondo_color": cfg.get("fondo_color") or "",
            # Modo Hero de la portada (migración 047)
            "hero_url": cfg.get("hero_url") or "",
            "hero_url_movil": cfg.get("hero_url_movil") or "",
            "hero_color": cfg.get("hero_color") or "",
            "hero_opacidad": int(cfg["hero_opacidad"]) if cfg.get("hero_opacidad") is not None else 40,
            # Layout personalizable del hero (migración 049): dict parcial;
            # vacío = defaults (texto centro, logo y botón visibles, sin redes)
            "hero_layout": cfg.get("hero_layout") or {},
        },
        "productos": productos,
        "orden_categorias": mapa_orden,
        "contacto": contacto,
    }
