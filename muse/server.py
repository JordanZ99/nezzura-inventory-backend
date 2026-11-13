# ==============================================================================
# backend/muse/server.py
# Servidor MCP (Model Context Protocol) — conector para agentes IA como Muse
# de Meta, con NIVELES de permiso por clave.
#
# Usa el SDK oficial `mcp` v2 (MCPServer, antes FastMCP). Transporte:
# streamable HTTP (el que soportan los custom connectors de Muse).
#
#   URL:     https://<backend>/mcp
#   Header:  X-API-Key: <clave del tenant>
#
# NIVELES (el dueño elige al crear la clave, con advertencia en el gestor):
#   1 "Lectura básica"    → catálogo para publicar (6 tools). Nunca costos.
#   2 "Lectura avanzada"  → + análisis del negocio con datos REALES
#                           (resumen_negocio, hallazgos_inteligentes). Agregados
#                           sí, costos unitarios nunca. Requiere plan Plus.
#   3 "Escritura básica"  → + crear productos con patrón borrador→confirmar
#                           (proponer_producto, confirmar_producto). La
#                           confirmación es destructiveHint: Muse debe pedir
#                           aprobación humana en cada llamada, y cada alta
#                           queda en el Historial como hecha por el agente.
#
# Modo del transporte:
#   - stateless_http=True: cada POST es autosuficiente (sin sesiones en el
#     servidor). Robusto detrás de Render: si el servicio duerme o reinicia
#     entre llamadas del agente, nada se pierde.
#   - json_response=True: respuestas JSON planas en lugar de SSE. Cualquier
#     cliente HTTP las entiende; es el subconjunto más compatible del
#     estándar streamable HTTP.
#   - Sin protección DNS-rebinding: es un endpoint público de API (esa
#     protección es para servidores locales); el SDK la activa por defecto
#     para localhost y rompería requests con el Host de producción.
#
# Seguridad (en capas):
#   1. Nivel de la clave: el tenant y el nivel se resuelven en muse/auth.py
#      desde la clave, NUNCA desde datos que envíe el agente.
#   2. tools/list filtrado por nivel (ServidorMuse) + re-verificación DENTRO
#      de cada tool (defensa en profundidad: si el filtro falla, el tool
#      responde error en vez de ejecutar).
#   3. Las queries usan columnas explícitas: los costos unitarios nunca salen
#      de la base de datos (el nivel 2 solo expone agregados).
#   4. La escritura (nivel 3) pasa por borrador con token de un solo uso:
#      confirmar crea SOLO desde lo guardado, el agente no puede alterar
#      datos entre mensajes.
#
# Las descripciones de los tools están en español y son explícitas para que
# el agente sepa CUÁNDO usar cada uno sin adivinar.
# ==============================================================================

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import TransportSecuritySettings
from mcp.types import Tool, ToolAnnotations

from database import mcp_analisis
from database import mcp_borradores
from database import mcp_catalogo as cat
from database import mcp_escritura
from database.mcp_claves import contar_escritura
from muse.auth import (
    AutenticacionAPIKey,
    clave_de_request,
    nivel_de_request,
    tenant_de_request,
)

# Nivel mínimo por tool (los 6 de catálogo son nivel 1: los ve cualquier clave).
_NIVEL_MINIMO = {
    "resumen_negocio": 2,
    "hallazgos_inteligentes": 2,
    "proponer_producto": 3,
    "confirmar_producto": 3,
}


class ServidorMuse(MCPServer):
    """MCPServer que filtra tools/list según el nivel de la clave: una clave
    nivel 1 (catálogo) jamás ve los tools de análisis o escritura, así el
    agente solo conoce lo que puede usar."""

    async def list_tools(self) -> list[Tool]:
        nivel = nivel_de_request() or 1
        tools = await super().list_tools()
        return [t for t in tools if _NIVEL_MINIMO.get(t.name, 1) <= nivel]


servidor = ServidorMuse(
    "Nezzura Inventario",
    instructions=(
        "Conector del gestor de inventario de este negocio. Las herramientas "
        "disponibles dependen del nivel de permiso de la clave: TODAS las "
        "claves pueden leer el catálogo (productos físicos con precio de "
        "venta al público, descripción, tamaño, stock y fotos). Las claves "
        "nivel 2 pueden además leer el análisis del negocio: métricas y "
        "hallazgos calculados sobre datos reales — úsalos cuando el dueño "
        "pregunte cómo va el negocio, nunca inventes cifras. Las claves "
        "nivel 3 pueden además crear productos nuevos, SIEMPRE con "
        "proponer_producto primero y confirmar_producto después. Ninguna "
        "clave ve costos unitarios por producto. Cuando el dueño pida "
        "publicar un producto en Marketplace o redes sociales, obtén los "
        "datos con estos tools en lugar de pedírselos: busca el producto, "
        "luego usa datos_para_publicar con su id."
    ),
)

# Anotación MCP estándar: tools 100% de lectura (las guías de conectores de
# Meta clasifican los tools en Read / Write / Sensitive write).
SOLO_LECTURA = ToolAnnotations(readOnlyHint=True)

# confirmar_producto modifica el inventario: la flag le indica al cliente
# (Muse) que pida aprobación humana ANTES de cada llamada — la red anti-bucle
# del nivel 3. proponer_producto NO es destructive: solo guarda un borrador.
CONFIRMACION = ToolAnnotations(destructiveHint=True)


def _requiere_nivel(minimo: int) -> dict | None:
    """Verificación interna por tool (defensa en profundidad)."""
    if nivel_de_request() < minimo:
        etiqueta = "2 (lectura avanzada)" if minimo == 2 else "3 (escritura básica)"
        return {
            "error": (
                "Tu clave no tiene permiso para esta herramienta. Pídele al "
                f"dueño una clave de nivel {etiqueta} en Ajustes → Negocio → "
                "Conectar mi IA."
            )
        }
    return None


@servidor.tool(annotations=SOLO_LECTURA)
def buscar_productos(consulta: str, solo_con_stock: bool = False, limite: int = 15) -> dict:
    """Busca productos físicos del inventario. La búsqueda es tolerante:
    ignora mayúsculas, acentos y espacios ("My Melody" encuentra "MyMelody"),
    divide la consulta en palabras y busca cada una en el nombre, la
    descripción, la categoría y los códigos ("Sanrio" trae todo lo de esa
    categoría). Devuelve id, nombre, precio de venta al público, stock
    disponible y categoría. NUNCA incluye costos internos. Úsalo para
    encontrar el id de un producto antes de obtener sus detalles o fotos."""
    tenant = tenant_de_request()
    try:
        productos = cat.buscar_productos(tenant, consulta, solo_con_stock, limite)
        return {"productos": productos, "total": len(productos)}
    except Exception as e:
        return {"error": f"No se pudo buscar: {e}"}


@servidor.tool(annotations=SOLO_LECTURA)
def obtener_producto(producto_id: int) -> dict:
    """Devuelve la ficha completa de un producto para publicarlo: nombre,
    descripción, precio de venta al público, stock disponible, tamaño en cm,
    categoría y variaciones (cada una con su precio y stock). No incluye
    costos ni datos internos del negocio."""
    tenant = tenant_de_request()
    try:
        ficha = cat.obtener_producto(tenant, producto_id)
    except Exception as e:
        return {"error": f"No se pudo obtener el producto: {e}"}
    if ficha is None:
        return {"error": f"No existe un producto físico activo con id {producto_id}."}
    return ficha


@servidor.tool(annotations=SOLO_LECTURA)
def obtener_fotos_producto(producto_id: int) -> dict:
    """Devuelve las URLs públicas de las fotos de un producto (foto principal
    y galería, ordenadas, sin duplicados). Las URLs son de Cloudinary y se
    pueden descargar directamente para adjuntarlas a una publicación."""
    tenant = tenant_de_request()
    try:
        fotos = cat.obtener_fotos(tenant, producto_id)
    except Exception as e:
        return {"error": f"No se pudo obtener las fotos: {e}"}
    if fotos is None:
        return {"error": f"No existe un producto físico activo con id {producto_id}."}
    if fotos["total"] == 0:
        return {
            "error": (
                f"El producto {producto_id} no tiene fotos cargadas. "
                "Pídele al dueño que le tome una foto."
            )
        }
    return fotos


@servidor.tool(annotations=SOLO_LECTURA)
def stock_actual(producto_id: int | None = None, consulta: str | None = None) -> dict:
    """Devuelve el stock disponible actual de uno o varios productos: por id
    exacto o buscando por nombre, descripción o categoría (búsqueda tolerante:
    ignora acentos, mayúsculas y espacios; funciona por palabras). Úsalo
    cuando el dueño pregunte cuántas unidades quedan de algo."""
    tenant = tenant_de_request()
    try:
        if producto_id is None and not (consulta or "").strip():
            return {"error": "Indica un producto_id o una consulta de búsqueda."}
        filas = cat.stock_actual(tenant, producto_id=producto_id, consulta=consulta)
    except Exception as e:
        return {"error": f"No se pudo consultar el stock: {e}"}
    if not filas:
        return {"error": "No se encontró ningún producto físico activo con ese criterio."}
    return {"productos": filas}


@servidor.tool(annotations=SOLO_LECTURA)
def listar_categorias() -> dict:
    """Lista las categorías de productos físicos del negocio. Útil para
    explorar el inventario por categoría antes de buscar productos."""
    tenant = tenant_de_request()
    try:
        return {"categorias": cat.listar_categorias(tenant)}
    except Exception as e:
        return {"error": f"No se pudieron listar las categorías: {e}"}


@servidor.tool(annotations=SOLO_LECTURA)
def datos_para_publicar(producto_id: int) -> dict:
    """Recomendado para publicar: agrega en UNA llamada todo lo necesario
    para anunciar un producto en Marketplace o redes sociales — ficha
    completa (nombre, descripción, precio de venta, stock, tamaño, categoría,
    variaciones) más sus fotos. Nunca incluye costos internos."""
    tenant = tenant_de_request()
    try:
        paquete = cat.datos_para_publicar(tenant, producto_id)
    except Exception as e:
        return {"error": f"No se pudo armar el paquete de publicación: {e}"}
    if paquete is None:
        return {"error": f"No existe un producto físico activo con id {producto_id}."}
    if not paquete["fotos"]["total"]:
        return {
            "error": (
                f"El producto {producto_id} no tiene fotos cargadas. "
                "Pídele al dueño que le tome una foto antes de publicarlo."
            )
        }
    return paquete


# ── Nivel 2: análisis del negocio con datos reales ───────────────────────────
# Los números salen del MISMO motor que el Análisis Inteligente del gestor
# (database/analitica.py + analisis/reglas.py): el agente no calcula nada,
# solo narra. Agregados sí, costos unitarios por producto nunca.

@servidor.tool(annotations=SOLO_LECTURA)
def resumen_negocio(periodo: str = "mes") -> dict:
    """KPIs del negocio con datos REALES, calculados por el gestor: ingresos,
    ganancia bruta, margen, gastos, utilidad neta, ticket promedio, mejor día
    y mejor mes, proyección del mes y valor del inventario. NUNCA incluye
    costos unitarios por producto. Periodo: 'mes' (contable actual, por
    defecto), '30dias', '90dias' o 'todo'. Ojo: 'rango_analizado' son los días
    que TIENEN ventas dentro de la ventana; 'rango_solicitado' es la ventana
    completa. Si la ventana no tiene ventas, las métricas van en 0 y eso es
    la respuesta correcta — infórmale al dueño que no hay registros del periodo."""
    nivel = _requiere_nivel(2)
    if nivel:
        return nivel
    tenant = tenant_de_request()
    try:
        return mcp_analisis.resumen_negocio(tenant, periodo)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        print(f"[MCP] resumen_negocio fallo: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


@servidor.tool(annotations=SOLO_LECTURA)
def hallazgos_inteligentes(tipo: str = "todos", periodo: str = "mes") -> dict:
    """Puntos ciegos del negocio detectados por el motor de análisis del
    gestor, con severidad (rojo/amarillo/verde), detalle y recomendación:
    quiebres de stock de best sellers, dinero dormido en inventario,
    tendencias a la baja, márgenes bajos, gastos pendientes de confirmar,
    días de concentración de ventas, etc. Cada hallazgo es un cálculo sobre
    datos reales, no una opinión. 'tipo' filtra: 'todos', 'dinero' (márgenes
    y gastos), 'stock' (inventario) o 'ventas' (tendencias). 'periodo' igual
    que resumen_negocio. Úsalo para dar consejos concretos al dueño."""
    nivel = _requiere_nivel(2)
    if nivel:
        return nivel
    tenant = tenant_de_request()
    try:
        return mcp_analisis.hallazgos_inteligentes(tenant, periodo, tipo)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        print(f"[MCP] hallazgos_inteligentes fallo: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


# ── Nivel 3: escritura básica (borrador → confirmar) ─────────────────────────
# Patrón de escritura segura: proponer valida y guarda un borrador con token
# (NADA toca el inventario); confirmar crea el producto DESDE LO GUARDADO en
# el borrador — el agente no puede alterar los datos entre mensajes — y
# devuelve al agente un error si los datos son inválidos en vez de crear algo
# a medias. El alta queda en el Historial con origen 'agente_ia'.

@servidor.tool(annotations=SOLO_LECTURA)
def proponer_producto(
    nombre: str,
    precio_venta: float,
    costo: float = 0.0,
    stock: float = 0.0,
    descripcion: str = "",
    categoria: list[str] | None = None,
    variaciones: list[dict] | None = None,
    imagen: str = "",
    codigo_interno: str = "",
    codigo_barras: str = "",
    tamano_cm: float | None = None,
    ubicacion: str = "",
    sufijo_precio: str = "",
    fraccionable: bool = False,
) -> dict:
    """PRIMER paso para crear un producto nuevo (requiere clave nivel 3):
    valida los datos y guarda un borrador — NO crea nada todavía. Si algún
    dato falta o es inválido, devuelve el error exacto para corregirlo.
    Devuelve un borrador_id que DEBE confirmarse con confirmar_producto.
    Reglas: solo productos físicos; si defines variaciones [{nombre, precio,
    stock_inicial, costo}], el stock general debe ir en cada variación, no en
    'stock'. El costo es interno y solo se usa para el lote: nunca lo cites
    al público."""
    nivel = _requiere_nivel(3)
    if nivel:
        return nivel
    tenant = tenant_de_request()
    try:
        return mcp_escritura.proponer(tenant, clave_de_request(), {
            "nombre": nombre,
            "precio_venta": precio_venta,
            "costo": costo,
            "stock": stock,
            "descripcion": descripcion,
            "categoria": categoria,
            "variaciones": variaciones,
            "imagen": imagen,
            "codigo_interno": codigo_interno,
            "codigo_barras": codigo_barras,
            "tamano_cm": tamano_cm,
            "ubicacion": ubicacion,
            "sufijo_precio": sufijo_precio,
            "fraccionable": fraccionable,
        })
    except Exception as e:
        return {"error": f"No se pudo proponer el producto: {e}"}


@servidor.tool(annotations=CONFIRMACION)
def confirmar_producto(borrador_id: str) -> dict:
    """SEGUNDO y último paso para crear un producto (requiere clave nivel 3):
    crea el producto en el inventario DESDE el borrador que el dueño ya
    revisó. Usa SIEMPRE el borrador_id que devolvió proponer_producto — no
    reenvíes los datos. El borrador se consume: cada uno solo puede
    confirmarse una vez y expira a los 15 minutos. El alta queda registrada
    en el Historial del gestor como creada por el agente IA."""
    nivel = _requiere_nivel(3)
    if nivel:
        return nivel
    tenant = tenant_de_request()
    try:
        datos = mcp_borradores.consumir_borrador(
            tenant, clave_de_request(), (borrador_id or "").strip()
        )
    except Exception as e:
        return {"error": f"No se pudo recuperar el borrador: {e}"}
    if datos is None:
        return {
            "error": (
                "El borrador no existe, ya se confirmó antes o expiró (15 min). "
                "Vuelve a proponer el producto con proponer_producto."
            )
        }
    # Cuota de escrituras: contada desde el día 1 (valor inicial generoso;
    # bajarlo luego es cambiar LIMITE_ESCRITURAS_DIARIO en database/mcp_claves).
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarlo o revocarla."
            )
        }
    return mcp_escritura.confirmar(tenant, datos)


# ── App ASGI: streamable HTTP + middleware de API key ────────────────────────
# Se monta en main.py (app.mount("/", app_mcp)) y el session_manager se corre
# desde el lifespan de FastAPI (main.py) — así lo documenta el SDK para
# montar el servidor dentro de una app FastAPI existente.
#
# add_middleware se llama en import (antes del primer request, cuando el
# stack de middleware todavía no se construye): instala la validación de
# X-API-Key sobre TODOS los requests del servidor MCP.
app_mcp = servidor.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)
app_mcp.add_middleware(AutenticacionAPIKey)
