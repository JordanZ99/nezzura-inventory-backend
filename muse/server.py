# ==============================================================================
# backend/muse/server.py
# Servidor MCP (Model Context Protocol) de SOLO LECTURA — conector para
# agentes IA como Muse de Meta.
#
# Usa el SDK oficial `mcp` v2 (MCPServer, antes FastMCP). Transporte:
# streamable HTTP (el que soportan los custom connectors de Muse).
#
#   URL:     https://<backend>/mcp
#   Header:  X-API-Key: <clave del tenant>
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
#   1. TODOS los tools son de solo lectura (readOnlyHint) — no escriben nada.
#      La publicación en Marketplace/redes es acción sensible del LADO del
#      agente, con su gate de aprobación: nunca escondemos un "publish"
#      dentro de un tool descrito como lectura.
#   2. El tenant se resuelve desde la clave (muse/auth.py), nunca desde datos
#      que envíe el agente.
#   3. Las queries (database/mcp_catalogo.py) usan columnas explícitas: los
#      costos internos no salen de la base de datos.
#
# Las descripciones de los tools están en español y son explícitas para que
# el agente sepa CUÁNDO usar cada uno sin adivinar.
# ==============================================================================

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import TransportSecuritySettings
from mcp.types import ToolAnnotations

from database import mcp_catalogo as cat
from muse.auth import AutenticacionAPIKey, tenant_de_request

servidor = MCPServer(
    "Goyangi Inventario",
    instructions=(
        "Catálogo de SOLO LECTURA del inventario de este negocio. Contiene "
        "productos físicos con su precio de venta al público, descripción, "
        "tamaño, stock disponible y fotos. NO incluye costos internos. "
        "Cuando el dueño pida publicar un producto en Marketplace o redes "
        "sociales, obtén los datos con estos tools en lugar de pedírselos: "
        "busca el producto, luego usa datos_para_publicar con su id."
    ),
)

# Anotación MCP estándar: tools 100% de lectura (las guías de conectores de
# Meta clasifican los tools en Read / Write / Sensitive write).
SOLO_LECTURA = ToolAnnotations(readOnlyHint=True)


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
