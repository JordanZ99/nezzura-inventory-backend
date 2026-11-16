# ==============================================================================
# backend/muse/roles.py
# ROLES del conector MCP: la fuente única de verdad de qué tools ve cada clave.
#
# Reemplaza la escalera de niveles (1/2/3) como criterio de filtrado: una clave
# tiene ROLES funcionales (toggles que el dueño elige) y el agente ve la UNIÓN
# de las tools de sus roles activos. Modelo "toolsets" como el servidor MCP
# oficial de GitHub: menos tools visibles = mejor selección y menos
# alucinación (la precisión de tool-selection cae con decenas de candidates;
# el punto dulce por rol son 4-9 tools).
#
# El campo `nivel` NO desaparece: se DERIVA de los roles (máx) porque sigue
# siendo la llave de los rate limits (database/mcp_claves.py) y del permiso
# 'ver costos' (068). Las tools de análisis siguen requiriendo plan Plus
# (se valida en la capa de datos, igual que antes).
#
# Por qué NO alternar toolsets en runtime: el cliente (Muse) cachea tools/list
# al conectar; una meta-tool "cámbiate de rol" dejaría al agente imaginando
# tools que su cliente nunca registró — la alucinación exacta que esto evita.
# El dueño crea una clave por agente/rol; si quiere otro rol, activa el toggle
# y la UNIÓN se aplica en el próximo tools/list.
#
# MIGRACIÓN 081: mcp_api_keys.roles (texto, 'rol1,rol2'). Las claves existentes
# se mapean por su nivel viejo (1→publicador, 2→publicador+analista,
# 3→inventario+disenador) para que queden afiladas, no todoterreno.
# ==============================================================================

ROLES = {
    # ── PUBLICADOR: publicar productos en Marketplace / redes (el nivel 1 de
    #    antes). Nunca costos, nunca escritura.
    "publicador": {
        "nombre": "Publicador",
        "nivel": 1,
        "tools": (
            "buscar_productos",
            "listar_productos",
            "listar_categorias",
            "datos_para_publicar",
        ),
        "instrucciones": (
            "Tu rol es PUBLICAR: cuando el dueño pida publicar o promocionar "
            "un producto en Marketplace o redes sociales, busca el producto y "
            "arma la publicación con datos_para_publicar (ficha completa + "
            "fotos en una llamada) en lugar de pedirle los datos. Nunca "
            "inventes precios, stock ni características: todo sale de los tools."
        ),
    },
    # ── ANALISTA: cómo va el negocio (antes nivel 2, requiere plan Plus).
    "analista": {
        "nombre": "Analista",
        "nivel": 2,
        "tools": (
            "resumen_negocio",
            "hallazgos_inteligentes",
            "ventas_recientes",
            "top_vendidos",
            "stock_actual",
            "listar_productos",
        ),
        "instrucciones": (
            "Tu rol es ANALIZAR el negocio con datos REALES del gestor: usa "
            "resumen_negocio, hallazgos_inteligentes, ventas_recientes y "
            "top_vendidos para responder cómo va el negocio, qué reponer y "
            "qué repetir. NUNCA inventes cifras: si un periodo no tiene "
            "ventas, la respuesta es que no hay registros. Los costos por "
            "producto solo aparecen si la clave tiene permiso 'ver costos'."
        ),
    },
    # ── DISEÑADOR: fotos del catálogo (nuevo). Escritura SOLO de imágenes.
    "disenador": {
        "nombre": "Diseñador",
        "nivel": 3,
        "tools": (
            "buscar_productos",
            "listar_productos",
            "datos_para_publicar",
            "config_catalogo",
            "adjuntar_imagen",
            "eliminar_imagen",
            "reordenar_imagenes",
        ),
        "instrucciones": (
            "Tu rol es CUIDAR LAS FOTOS del catálogo: antes de generar o "
            "adjuntar una imagen, consulta config_catalogo para conocer la "
            "relación de aspecto (1:1 o 4:5) y el fondo del catálogo, y "
            "encaja las fotos a esa proporción (adjuntar_imagen lo hace "
            "solo con encaje 'marco'). Puedes reemplazar la foto principal, "
            "agregar a la galería, eliminar la que se ve mal y reordenar "
            "cuál va primero. Nunca tocas precios, stock ni textos."
        ),
    },
    # ── INVENTARIO: crear y mantener productos (antes nivel 3).
    "inventario": {
        "nombre": "Administrador de inventario",
        "nivel": 3,
        "tools": (
            "buscar_productos",
            "listar_productos",
            "stock_actual",
            "listar_categorias",
            "proponer_producto",
            "confirmar_producto",
            "proponer_edicion",
            "confirmar_edicion",
            "adjuntar_imagen",
        ),
        "instrucciones": (
            "Tu rol es CREAR Y MANTENER productos: SIEMPRE proponer_* "
            "primero (guarda el borrador) y confirmar_* después con el "
            "borrador_id. Si un dato falta, el tool te dirá exactamente qué "
            "corregir. Al crear un producto con foto, confírmalo y adjunta "
            "la imagen con adjuntar_imagen. Cada cambio queda en el "
            "Historial como hecho por el agente IA."
        ),
    },
    # ── CAJERO: registrar ventas por chat (el dueño aprueba el desglose). ──
    # Escritura de VENTAS (nivel 3): descuenta stock y genera tickets con el
    # MISMO motor del POS, pero SIEMPRE vía borrador→confirmar; el dueño ve
    # el desglose real del carrito antes de aprobar.
    "cajero": {
        "nombre": "Cajero",
        "nivel": 3,
        "tools": (
            "buscar_productos",
            "proponer_venta",
            "confirmar_venta",
        ),
        "instrucciones": (
            "Tu rol es REGISTRAR VENTAS: cuando el dueño te diga qué vendió o "
            "te pase la FOTO de un ticket, identifica cada producto con "
            "buscar_productos y arma la venta con proponer_venta (envía el "
            "producto_id de cada renglón). PROPÓN, NO INTERROGUES: si un "
            "renglón es ambiguo, elige tu MEJOR interpretación con señales "
            "reales (precio del ticket = precio del inventario, nombre "
            "parcial, stock del desglose) y márcala — corregir es solo volver "
            "a proponer, nada se registra sin la aprobación del dueño. "
            "Muestra el desglose tal cual y haz UNA sola pregunta: '¿lo "
            "registro así o corriges algo?'. Precio del ticket = precio "
            "unitario por default. Un nombre que no existe: propón el "
            "producto cuyo precio coincida o sea el más cercano (di por qué) "
            "o regístralo como venta libre con su motivo. Vender más del "
            "stock está permitido y el desglose lo avisa. Nunca inventes un "
            "renglón, un producto_id ni un precio; si un renglón es "
            "ilegible, dilo en la misma pregunta única."
        ),
    },
}

# Tool → roles que la incluyen (derivado, nunca se edita a mano).
ROL_DE_TOOL: dict[str, tuple[str, ...]] = {}
for _clave_rol, _def in ROLES.items():
    for _tool in _def["tools"]:
        ROL_DE_TOOL.setdefault(_tool, ())
        ROL_DE_TOOL[_tool] = ROL_DE_TOOL[_tool] + (_clave_rol,)

# Todas las tools conocidas (la unión de todos los roles).
TODAS_LAS_TOOLS = tuple(sorted(ROL_DE_TOOL))


def normalizar_roles(roles) -> tuple[str, ...] | None:
    """
    Limpia una lista de roles (viene del REST o de la BD): dedupe, lowercase,
    orden estable. Devuelve None si hay alguno inválido. Lista vacía →
    ('publicador',) — una clave sin roles no sirve de nada.
    """
    if roles is None:
        return ("publicador",)
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",") if r.strip()]
    limpios = []
    for r in roles or []:
        r = str(r or "").strip().lower()
        if not r:
            continue
        if r not in ROLES:
            return None
        if r not in limpios:
            limpios.append(r)
    return tuple(limpios) or ("publicador",)


def nivel_de_roles(roles) -> int:
    """Nivel EFECTIVO de una clave: el máximo de sus roles (para rate limits)."""
    if not roles:
        return 1
    return max(ROLES[r]["nivel"] for r in roles if r in ROLES) if any(r in ROLES for r in roles) else 1


def tools_de_roles(roles) -> tuple[str, ...]:
    """Unión de tools de los roles activos. Roles vacíos/None = todas
    (defensa para claves pre-081 sin migrar)."""
    if not roles:
        return TODAS_LAS_TOOLS
    vistas: list[str] = []
    for rol in roles:
        for tool in ROLES.get(rol, {}).get("tools", ()):
            if tool not in vistas:
                vistas.append(tool)
    return tuple(vistas)


def herramienta_en_roles(tool: str, roles) -> bool:
    """Filtro de defensa dentro de cada tool (defensa en profundidad)."""
    return tool in tools_de_roles(roles)


def nombres_de_rol(tool: str) -> str:
    """Nombres legibles de los roles que habilitan una tool (para errores)."""
    return ", ".join(ROLES[r]["nombre"] for r in ROL_DE_TOOL.get(tool, ()))


def instrucciones_para(roles) -> str:
    """Instrucciones del servidor para una clave: reglas globales + el brief
    de cada rol activo (cortos a propósito: el contexto largo genérico es lo
    que confunde al agente)."""
    partes = [ROLES[r]["instrucciones"] for r in (roles or ()) if r in ROLES]
    if not partes:
        return ""  # legacy: sin roles se usa el texto global del servidor
    return "\n\n".join(partes)
