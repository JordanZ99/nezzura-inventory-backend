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
# ROLES (el dueño elige al crear la clave, con toggles en el gestor — 081;
# muse/roles.py es la fuente única de verdad; el nivel se deriva del máximo):
#   "publicador"  → catálogo para publicar en redes/Marketplace (4 tools).
#   "analista"    → + análisis del negocio con datos REALES (6 tools, plan Plus).
#   "disenador"   → fotos: adjuntar/encajar/eliminar/reordenar y ver la config
#                   del catálogo (7 tools).
#   "inventario"  → + crear y editar productos con patrón borrador→confirmar
#                   (9 tools).
#   "cajero"      → registrar ventas y gastos por chat (proponer_venta →
#                   confirmar_venta, proponer_gasto → confirmar_gasto; 5
#                   tools). El dueño aprueba el desglose antes de que se
#                   registre nada.
#   "compras"     → restock: entradas de inventario vía lotes con el costo
#                   real (proponer_entrada → confirmar_entrada; 5 tools).
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
#   2. tools/list filtrado por ROLES de la clave (ServidorMuse) +
#      re-verificación DENTRO de cada tool (_requiere_permiso: defensa en
#      profundidad — si el filtro falla, el tool responde error en vez de
#      ejecutar).
#   3. Las queries usan columnas explícitas: los costos unitarios nunca salen
#      de la base de datos (el nivel 2 solo expone agregados).
#   4. La escritura (roles disenador/inventario) pasa por borrador con token
#      de un solo uso:
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
from database import mcp_cobro
from database import mcp_escritura
from database import mcp_gastos
from database import mcp_compras
from database import mcp_imagenes
from database import mcp_ventas
from database.mcp_claves import contar_escritura
from muse.auth import (
    AutenticacionAPIKey,
    clave_de_request,
    roles_de_request,
    tenant_de_request,
    ver_costos_de_request,
)
from muse.roles import (
    ROLES,
    herramienta_en_roles,
    instrucciones_para,
    nombres_de_rol,
    tools_de_roles,
)


class ServidorMuse(MCPServer):
    """MCPServer que filtra tools/list según los ROLES de la clave (081): el
    dueño activa toggles por clave (publicador, analista, disenador,
    inventario) y el agente solo conoce la UNIÓN de las tools de esos roles —
    modelo 'toolsets' como el servidor MCP oficial de GitHub. Menos tools
    visibles = mejor selección y menos alucinación. Roles vacíos (claves
    pre-081) = todas las tools."""

    async def list_tools(self) -> list[Tool]:
        roles = roles_de_request()
        tools = await super().list_tools()
        return [t for t in tools if herramienta_en_roles(t.name, roles)]


servidor = ServidorMuse(
    "Nezzura Inventario",
    instructions=(
        "Conector del gestor de inventario de este negocio. Las herramientas "
        "que ves son EXACTAMENTE las de tu rol: úsalas todas con confianza y "
        "no asumas que existen otras (si algo no está en tu lista, dilo y "
        "pediráselo al dueño). El catálogo son productos físicos con precio "
        "de venta al público, descripción, tamaño, stock y fotos. Algunas "
        "claves tienen el permiso 'ver costos': si una respuesta incluye "
        "campos de costo o margen, son datos internos del dueño — NUNCA los "
        "menciones en publicaciones, frente a compradores ni en el catálogo "
        "público; datos_para_publicar nunca los incluye. Para VER el detalle "
        "de un producto o PUBLICARLO, la única vía es datos_para_publicar "
        "con el id que te dio la búsqueda o el listado: incluye ficha "
        "completa y fotos en una llamada. Nunca inventes precios, stock ni "
        "cifras: todo sale de las herramientas."
    ),
)

# Anotación MCP estándar: tools 100% de lectura (las guías de conectores de
# Meta clasifican los tools en Read / Write / Sensitive write).
SOLO_LECTURA = ToolAnnotations(readOnlyHint=True)

# confirmar_producto modifica el inventario: la flag le indica al cliente
# (Muse) que pida aprobación humana ANTES de cada llamada — la red anti-bucle
# de los roles con escritura. proponer_producto NO es destructive: solo
# guarda un borrador.
CONFIRMACION = ToolAnnotations(destructiveHint=True)


def _requiere_permiso(tool: str) -> dict | None:
    """Verificación interna por tool (defensa en profundidad): si el filtro
    de tools/list fallara o el agente llama una tool que no le corresponde,
    esta guarda responde error en vez de ejecutar."""
    if not herramienta_en_roles(tool, roles_de_request()):
        return {
            "error": (
                f"Tu clave no tiene el rol para '{tool}' (lo dan: "
                f"{nombres_de_rol(tool)}). Pídele al dueño activar ese rol "
                "en Ajustes → Negocio → Conectar mi IA."
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
    disponible y categoría. Si la clave tiene el permiso 'ver costos',
    incluye costo_unitario y margen (datos internos: nunca los muestres a
    compradores). Úsalo para encontrar el id de un producto antes de obtener
    sus detalles o fotos."""
    tenant = tenant_de_request()
    try:
        productos = cat.buscar_productos(
            tenant, consulta, solo_con_stock, limite, con_costos=ver_costos_de_request()
        )
        return {"productos": productos, "total": len(productos)}
    except Exception as e:
        return {"error": f"No se pudo buscar: {e}"}


@servidor.tool(annotations=SOLO_LECTURA)
def listar_productos(
    categoria: str = "",
    estado_stock: str = "",
    umbral_stock_bajo: int = 3,
    sin_datos: str = "",
    orden: str = "",
    pagina: int = 1,
    limite: int = 20,
) -> dict:
    """Lista el inventario completo paginado (máximo 50 por página), con
    filtros combinables — cubre lo que la búsqueda por términos no puede:
    - categoria: todo lo de una categoría ("Sanrio", "Pokémon"...).
    - estado_stock: 'stock_bajo' (para saber qué reponer), 'sin_stock' (lo
      que NO conviene publicar) o 'disponible'.
    - sin_datos: 'sin_foto', 'sin_descripcion', 'sin_tamanio' o
      'incompletos' (cualquiera de las tres) — para limpiar el catálogo y
      mejorar las publicaciones.
    - orden: 'nombre' (default), 'stock_asc' (lo que falta primero) o
      'recientes'.
    Cada producto trae tiene_foto / sin_descripcion / sin_tamano para que
    puedas reportar al dueño qué le falta a cada publicación. Itera con
    'pagina' para recorrer todo. Nunca incluye costos (salvo permiso
    'ver costos' de la clave)."""
    tenant = tenant_de_request()
    try:
        return cat.listar_productos(
            tenant,
            categoria=categoria or None,
            estado_stock=estado_stock or None,
            umbral_stock_bajo=umbral_stock_bajo,
            sin_datos=sin_datos or None,
            orden=orden or None,
            pagina=pagina,
            limite=limite,
            con_costos=ver_costos_de_request(),
        )
    except Exception as e:
        return {"error": f"No se pudo listar el inventario: {e}"}


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
    """Única vía para VER el detalle completo de un producto o PUBLICARLO:
    agrega en UNA llamada todo lo necesario — ficha completa (nombre,
    descripción, precio de venta, stock, tamaño, categoría, variaciones) más
    sus fotos con URLs descargables e ids (el 'id' de cada foto de
    fotos.galeria es el que piden eliminar_imagen y reordenar_imagenes).
    Nunca incluye costos internos (es el canal público); el costo individual
    con permiso 'ver costos' se lee con buscar_productos o listar_productos."""
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
# solo narra. Agregados sí; costos por producto/venta solo con el permiso
# 'ver costos' de la clave (068).

@servidor.tool(annotations=SOLO_LECTURA)
def ventas_recientes(periodo: str = "30dias", pagina: int = 1, limite: int = 20) -> dict:
    """Lista las VENTAS del negocio una por una, la más nueva primero, en la
    ventana pedida ('mes', '30dias' —default—, '90dias' o 'todo'). Cada venta
    trae fecha, producto, cantidad, precio unitario y total. Paginado
    (máximo 50 por página): itera con 'pagina' para recorrer el periodo.
    Úsalo para responder 'qué se vendió ayer', 'qué se llevó el último
    cliente' o revisar ventas registradas. Requiere el rol Analista y plan
    Plus. Si la clave tiene permiso 'ver costos', incluye costo_unitario y
    ganancia por venta (datos internos)."""
    permiso = _requiere_permiso("ventas_recientes")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    try:
        return mcp_ventas.ventas_recientes(
            tenant, periodo, pagina, limite, con_costos=ver_costos_de_request()
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        print(f"[MCP] ventas_recientes fallo: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


@servidor.tool(annotations=SOLO_LECTURA)
def top_vendidos(periodo: str = "30dias", limite: int = 10) -> dict:
    """Ranking de los productos MÁS VENDIDOS del periodo ('mes', '30dias' —
    default—, '90dias' o 'todo'), ordenado por ingresos, con unidades
    vendidas y el stock ACTUAL de cada uno ('agotado' y 'por_agotarse'
    marcados). Úsalo para decirle al dueño qué repetir, qué reponer antes
    de volver a publicar y por qué un mes fue mejor que otro. Requiere
    el rol Analista y plan Plus. Si la clave tiene permiso 'ver costos',
    incluye costo_unitario y margen del ranking (datos internos)."""
    permiso = _requiere_permiso("top_vendidos")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    try:
        return mcp_ventas.top_vendidos(
            tenant, periodo, limite, con_costos=ver_costos_de_request()
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        print(f"[MCP] top_vendidos fallo: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


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
    permiso = _requiere_permiso("resumen_negocio")
    if permiso:
        return permiso
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
    permiso = _requiere_permiso("hallazgos_inteligentes")
    if permiso:
        return permiso
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
    """PRIMER paso para crear un producto nuevo (rol Administrador de inventario):
    valida los datos y guarda un borrador — NO crea nada todavía. Si algún
    dato falta o es inválido, devuelve el error exacto para corregirlo.
    Devuelve un borrador_id que DEBE confirmarse con confirmar_producto.
    Reglas: solo productos físicos; si defines variaciones [{nombre, precio,
    stock_inicial, costo}], el stock general debe ir en cada variación, no en
    'stock'. El costo es interno y solo se usa para el lote: nunca lo cites
    al público. El campo 'imagen' solo acepta una URL pública http(s); si
    tienes la foto como archivo/base64, crea primero el producto y luego
    adjúntala con adjuntar_imagen."""
    permiso = _requiere_permiso("proponer_producto")
    if permiso:
        return permiso
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
    """SEGUNDO y último paso para crear un producto (rol Administrador de inventario):
    crea el producto en el inventario DESDE el borrador que el dueño ya
    revisó. Usa SIEMPRE el borrador_id que devolvió proponer_producto — no
    reenvíes los datos. El borrador se consume: cada uno solo puede
    confirmarse una vez y expira a los 15 minutos. El alta queda registrada
    en el Historial del gestor como creada por el agente IA."""
    permiso = _requiere_permiso("confirmar_producto")
    if permiso:
        return permiso
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


# ── Rol Cajero: registrar ventas por chat (borrador → confirmar) ──────────────
# El humano es el guardián: proponer_venta resuelve cada producto contra la BD,
# arma el desglose REAL (nombres/precios del inventario) y lo devuelve para que
# el dueño lo apruebe en el chat; confirmar_venta registra con el MISMO motor
# del POS (cobrar_carrito: PEPS, pago, orden con n_ticket). El borrador solo
# guarda productos resueltos por el backend — un producto inventado es
# estructuralmente imposible de registrar. La venta queda en el Historial como
# hecha por el agente IA (origen 'agente_ia'), sin costos en la respuesta.

@servidor.tool(annotations=SOLO_LECTURA)
def proponer_venta(
    items: list[dict],
    pago: dict | None = None,
    fecha: str | None = None,
    nota: str = "",
) -> dict:
    """PRIMER paso para registrar una VENTA (rol Cajero): arma el carrito, lo
    valida y guarda un borrador — NO registra nada todavía. Úsalo cuando el
    dueño te diga qué vendió o te pase la FOTO de un ticket.
    Cada item de 'items' es un dict:
    - producto_id: el id que devuelve buscar_productos, con cantidad (número
      > 0). También acepta 'producto' (nombre EXACTO del inventario), pero el
      id es más seguro. Opcionales: precio_real (si no lo envías se usa el
      precio de los lotes — nunca calcules precios tú), variacion ('Grande')
      e id_lote (deja vacío y el sistema elige el lote más viejo, PEPS).
    - Venta libre (lo vendido NO está en el inventario): venta_libre=true con
      descripcion (qué se vendió), precio_real (>0) y motivo (por qué no está
      en el inventario — queda en el Historial del gestor).
    Opcionales a nivel de venta: pago ({metodo: 'efectivo' (con
    monto_recibido), 'tarjeta_debito', 'tarjeta_credito' o 'mixto' (con
    pagos: [{metodo, monto}, ...] que cuadren con el total); déjalo vacío para
    un ticket sin método), fecha del ticket ('YYYY-MM-DD', para tickets de
    otro día; nunca futura) y nota libre para el Historial.
    REGLAS DE PROPUESTA (propón, no interrogues): ante un renglón ambiguo
    arma el carrito con tu MEJOR interpretación usando señales reales —
    precio del ticket = precio del inventario, nombre parcial, stock que el
    desglose te devuelve — y márcala. Corregir es solo volver a proponer
    (nada se registra sin la aprobación del dueño): cierra con UNA sola
    pregunta, '¿lo registro así o corriges algo?'. Precio del ticket = precio
    unitario por default. Un nombre que no existe: propón el producto cuyo
    precio coincida o sea el más cercano (di por qué) o regístralo como venta
    libre con motivo. Vender más del stock está permitido y el desglose lo
    avisa. Con tickets ilegibles: registra solo lo que puedas leer con
    certeza y di en la MISMA pregunta qué no pudiste leer — nunca inventes
    un renglón, un producto_id ni un precio.
    Devuelve el desglose REAL del carrito (nombres, precios y stock del
    inventario): muéstraselo al dueño tal cual. Si aprueba, llama
    confirmar_venta con el borrador_id; si pide corregir, vuelve a proponer la
    venta corregida (el borrador anterior expira solo)."""
    permiso = _requiere_permiso("proponer_venta")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    try:
        return mcp_cobro.proponer_venta(
            tenant, clave_de_request(), items or [], pago, fecha, nota
        )
    except Exception as e:
        print(f"[MCP] proponer_venta fallo: {type(e).__name__}: {e}")
        return {"error": f"No se pudo proponer la venta: {e}"}


@servidor.tool(annotations=CONFIRMACION)
def confirmar_venta(borrador_id: str) -> dict:
    """SEGUNDO y último paso para registrar una venta (rol Cajero): cobra el
    carrito DESDE el borrador que el dueño ya aprobó — descuenta el stock
    (PEPS), genera el ticket y el pago, igual que el POS. Usa SIEMPRE el
    borrador_id que devolvió proponer_venta — no reenvíes los items. Llámalo
    SOLO cuando el dueño haya visto el desglose y lo haya aprobado: cada
    confirmación registra una venta real con su ticket. El borrador se consume
    (una sola vez, expira a los 15 minutos). La venta queda en el Historial
    del gestor como registrada por el agente IA; si el ticket era de otro día,
    queda con ESA fecha."""
    permiso = _requiere_permiso("confirmar_venta")
    if permiso:
        return permiso
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
                "Vuelve a proponer la venta con proponer_venta."
            )
        }
    # Cuota de escrituras ANTES de ejecutar (patrón de confirmar_producto):
    # cada venta registrada gasta una escritura de la clave.
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarla en Ajustes → Negocio → Conectar mi IA."
            )
        }
    return mcp_cobro.confirmar_venta(tenant, datos)


# ── Rol Cajero: registrar GASTOS por chat (borrador → confirmar) ─────────────
# El que opera la caja registra lo que sale: monto, categoría, descripción y
# fecha con el MISMO motor del gestor (insertar_gasto). El dueño aprueba el
# desglose antes; el gasto queda en el Historial como hecho por el agente IA.

@servidor.tool(annotations=SOLO_LECTURA)
def proponer_gasto(
    monto: float, categoria: str, descripcion: str,
    fecha: str | None = None, nota: str = "",
) -> dict:
    """PRIMER paso para registrar un GASTO (rol Cajero): valida los datos y
    guarda un borrador — NO registra nada todavía. Úsalo cuando el dueño diga
    que gastó algo (ej. 'gasté 80 pesos en bolsas').
    - monto: cuánto se gastó (número > 0).
    - categoria: categoría del gasto ('Renta', 'Empaque', 'Insumos'...). Si no
      existe en el gestor se usa como categoría nueva y te lo aviso.
    - descripcion: qué se gastó (1-200 caracteres).
    - fecha: opcional 'YYYY-MM-DD' si el gasto fue de otro día (nunca futura;
      sin fecha queda con la de hoy). nota: libre para el Historial.
    Devuelve el desglose y las categorías del negocio: muéstraselo al dueño y
    haz UNA sola pregunta ('¿lo registro así o corriges algo?'). Si aprueba,
    llama confirmar_gasto con el borrador_id; si pide corregir, vuelve a
    proponer (el borrador anterior expira solo)."""
    permiso = _requiere_permiso("proponer_gasto")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    try:
        return mcp_gastos.proponer_gasto(
            tenant, clave_de_request(), monto, categoria, descripcion, fecha, nota
        )
    except Exception as e:
        print(f"[MCP] proponer_gasto fallo: {type(e).__name__}: {e}")
        return {"error": f"No se pudo proponer el gasto: {e}"}


@servidor.tool(annotations=CONFIRMACION)
def confirmar_gasto(borrador_id: str) -> dict:
    """SEGUNDO y último paso para registrar un gasto (rol Cajero): lo registra
    en el gestor DESDE el borrador que el dueño ya aprobó — queda como 'pagado'
    en su contabilidad. Usa SIEMPRE el borrador_id que devolvió proponer_gasto
    — no reenvíes los datos. Llámalo SOLO cuando el dueño haya visto el
    desglose y lo haya aprobado. El borrador se consume (una sola vez, expira
    a los 15 minutos). El gasto queda en el Historial como registrado por el
    agente IA."""
    permiso = _requiere_permiso("confirmar_gasto")
    if permiso:
        return permiso
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
                "Vuelve a proponer el gasto con proponer_gasto."
            )
        }
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarla en Ajustes → Negocio → Conectar mi IA."
            )
        }
    return mcp_gastos.confirmar_gasto(tenant, datos)


# ── Rol Compras: ENTRADAS de inventario (restock) por chat ───────────────────
# Agrega stock vía lotes con el MISMO motor del gestor (agregar_lote: fusiona
# con el lote existente si coincide costo+precio, o crea lote nuevo, y registra
# el ledger de movimientos). El borrador solo lleva productos resueltos por el
# backend y el COSTO real de la compra (el inventario valorado lo exige): el
# dueño aprueba el desglose antes de que toque sus números.

@servidor.tool(annotations=SOLO_LECTURA)
def proponer_entrada(
    producto_id: int | None = None,
    producto: str | None = None,
    cantidad: float = 0,
    costo: float | None = None,
    precio_venta: float | None = None,
    variacion: str | None = None,
    etiqueta: str | None = None,
    nota: str = "",
) -> dict:
    """PRIMER paso para AGREGAR STOCK a un producto existente (restock, rol
    Compras): valida la entrada y guarda un borrador — NO toca el inventario
    todavía. Úsalo cuando el dueño diga qué le llegó (ej. 'llegaron 10 Kuromi
    a $150').
    - producto_id: el id que devuelve buscar_productos (o 'producto' con el
      nombre EXACTO). El producto DEBE existir: si es nuevo, créalo primero
      con proponer_producto.
    - cantidad: cuántas unidades llegaron (>0).
    - costo: el costo unitario de COMPRA (>0) — alimenta el inventario
      valorado, así que debe ser el real: si no lo sabes, pregúntale al dueño.
    - precio_venta: opcional (default: el del último lote del producto).
    - variacion: si el producto tiene variaciones, a cuál llegó el stock
      ('Grande'); si no tiene, déjala vacía. etiqueta: presentación opcional
      ('Caja de 12'). nota: libre para el Historial.
    Devuelve el desglose (cuánto subirá el stock, a qué costo y precio, si
    suma al lote existente o crea uno nuevo): muéstraselo al dueño y haz UNA
    sola pregunta ('¿aplico la entrada?'). Si aprueba, llama confirmar_entrada
    con el borrador_id; si pide corregir, vuelve a proponer."""
    permiso = _requiere_permiso("proponer_entrada")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    try:
        return mcp_compras.proponer_entrada(
            tenant, clave_de_request(), producto_id, producto, cantidad,
            costo, precio_venta, variacion, etiqueta,
            con_costos=ver_costos_de_request(), nota=nota,
        )
    except Exception as e:
        print(f"[MCP] proponer_entrada fallo: {type(e).__name__}: {e}")
        return {"error": f"No se pudo proponer la entrada: {e}"}


@servidor.tool(annotations=CONFIRMACION)
def confirmar_entrada(borrador_id: str) -> dict:
    """SEGUNDO y último paso para restockear (rol Compras): aplica la entrada
    con el MISMO motor del gestor — suma al lote existente o crea un lote
    nuevo con el costo que el dueño aprobó, y queda en su Historial como
    hecha por el agente IA. Usa SIEMPRE el borrador_id que devolvió
    proponer_entrada — no reenvíes los datos. Llámalo SOLO cuando el dueño
    haya visto el desglose y lo haya aprobado: cada confirmación mueve su
    inventario valorado. El borrador se consume (una sola vez, expira a los
    15 minutos)."""
    permiso = _requiere_permiso("confirmar_entrada")
    if permiso:
        return permiso
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
                "Vuelve a proponer la entrada con proponer_entrada."
            )
        }
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarla en Ajustes → Negocio → Conectar mi IA."
            )
        }
    return mcp_compras.confirmar_entrada(tenant, datos)


# ── Nivel 3: EDICIONES (mismo patrón borrador → confirmar) ───────────────────
# proponer_edicion lee los valores VIEJOS de la base y guarda el diff
# antes→después en el borrador; confirmar_edicion aplica SOLO lo guardado y
# cada edición queda en el Historial como hecha por el agente IA (068).

@servidor.tool(annotations=SOLO_LECTURA)
def proponer_edicion(
    tipo: str, entidad_id: int, cambios: dict
) -> dict:
    """PRIMER paso para EDITAR algo del inventario (rol Administrador de inventario):
    valida los cambios y guarda un borrador con el diff antes→después — NO
    edita nada todavía. tipos y campos:
    - tipo 'producto' (entidad_id = id del producto): descripcion, nombre,
      precio_venta, estado ('Activo'/'Inactivo'), visible_en_catalogo,
      tamano_cm, codigo_interno, codigo_barras, ubicacion, categoria (lista
      de hasta 5). El campo 'costo' solo si la clave tiene permiso 'ver
      costos'. Para agregar o cambiar la FOTO usa la herramienta adjuntar_imagen.
    - tipo 'venta' (id de la venta): fecha ('2026-11-12 15:30'), cantidad
      (>0) y precio_real. Los totales y el stock se recalculan solos.
    - tipo 'gasto' (id del gasto): monto, categoria, descripcion.
    Devuelve el diff legible y un borrador_id que DEBE confirmarse con
    confirmar_edicion. Si un campo no aplica, devuelve el error exacto."""
    permiso = _requiere_permiso("proponer_edicion")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    try:
        return mcp_escritura.proponer_edicion(
            tenant, clave_de_request(), tipo, entidad_id, cambios or {},
            con_costos=ver_costos_de_request(),
        )
    except Exception as e:
        return {"error": f"No se pudo proponer la edición: {e}"}


@servidor.tool(annotations=CONFIRMACION)
def confirmar_edicion(borrador_id: str) -> dict:
    """SEGUNDO y último paso para EDITAR (rol Administrador de inventario): aplica los
    cambios que el dueño ya revisó. Usa SIEMPRE el borrador_id que devolvió
    proponer_edicion — no reenvíes los cambios. El borrador se consume (una
    sola vez, expira a los 15 minutos). La edición queda registrada en el
    Historial del gestor como hecha por el agente IA ('El agente IA editó
    el producto/la venta/el gasto...')."""
    permiso = _requiere_permiso("confirmar_edicion")
    if permiso:
        return permiso
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
                "Vuelve a proponer los cambios con proponer_edicion."
            )
        }
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarla en Ajustes → Negocio → Conectar mi IA."
            )
        }
    return mcp_escritura.confirmar_edicion(tenant, datos)


# ── Nivel 3: IMÁGENES (efecto directo, con cuota de escrituras) ───────────────
# Adjuntar una foto no corrompe datos de negocio como crear/editar producto,
# así que NO pasa por borrador→confirmar: se aplica directo, cuenta para la
# cuota de escrituras del día y queda en el Historial como hecho por el agente.
# El pipeline es el MISMO del gestor (reductor de peso + Cloudinary + cuota de
# almacenamiento): la imagen entra como base64 y sale hospedada en el CDN.

@servidor.tool(annotations=CONFIRMACION)
def adjuntar_imagen(
    producto_id: int, imagen_base64: str, destino: str = "principal",
    encaje: str = "marco", fondo: str = "blanco",
) -> dict:
    """Adjunta una IMAGEN a un producto del inventario (roles Diseñador o Administrador de inventario).
    Úsala SIEMPRE que tengas una foto de un producto como archivo/base64, sea
    para un producto recién creado o para cambiar/agregar la foto de uno que
    ya existe — es la única vía para fotos que no son una URL pública.
    Parámetros:
    - producto_id: id del producto (el que devuelven buscar/listar o
      confirmar_producto).
    - imagen_base64: el archivo de la imagen codificado en base64 (acepta
      también la data URL 'data:image/...;base64,...'). El gestor la reduce de
      peso automáticamente, no hace falta comprimirla antes.
    - destino: 'principal' (default: reemplaza la foto principal) o 'galeria'
      (agrega una foto extra al carrusel; requiere plan Plus y el producto no
      puede superar las 5 fotos).
    - encaje: 'marco' (default: la imagen COMPLETA rellenando el lado corto
      hasta la proporción del catálogo del negocio — RECOMENDADO para que la
      foto llene la tarjeta del catálogo sin franjas), 'recorte' (llena el
      marco recortando el centro; solo si el producto ocupa toda la escena) o
      'libre' (sin marco, tal cual).
    - fondo: color del relleno del marco: 'blanco' (default), 'negro' o
      'rojo'. Solo aplica con encaje 'marco'.
    Efecto inmediato: la foto queda en el producto en esta misma llamada y en
    el Historial del gestor como adjuntada por el agente IA. Cuenta para la
    cuota diaria de escrituras de la clave."""
    permiso = _requiere_permiso("adjuntar_imagen")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarla en Ajustes → Negocio → Conectar mi IA."
            )
        }
    try:
        return mcp_imagenes.adjuntar(
            tenant, producto_id, imagen_base64, destino, encaje, fondo
        )
    except Exception as e:
        print(f"[MCP] adjuntar_imagen fallo: {type(e).__name__}: {e}")
        return {"error": f"No se pudo adjuntar la imagen: {e}"}


# ── Rol Diseñador: gestión de las fotos del catálogo ──────────────────────────
# Solo el rol disenador las ve. eliminar y reordenar son escritura: destructiveHint
# (aprobación humana por llamada) + cuota de escrituras, mismo patrón que el resto.

@servidor.tool(annotations=SOLO_LECTURA)
def config_catalogo() -> dict:
    """Configuración VISUAL del catálogo del negocio (rol Diseñador): la
    relación de aspecto con la que se muestran las fotos de producto
    ('1:1' cuadrada o '4:5' vertical), el color de fondo del catálogo, el
    tema visual y el título. CONSÚLTALA ANTES de generar o adjuntar imágenes:
    las fotos que generes deben usar esa MISMA relación de aspecto y combinar
    con el fondo/tema — adjuntar_imagen con encaje 'marco' ya rellena el
    borde automáticamente hasta esa proporción."""
    permiso = _requiere_permiso("config_catalogo")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    try:
        return mcp_imagenes.config_catalogo(tenant)
    except Exception as e:
        return {"error": f"No se pudo leer la configuración del catálogo: {e}"}


@servidor.tool(annotations=CONFIRMACION)
def eliminar_imagen(producto_id: int, imagen_id: int | None = None, destino: str | None = None) -> dict:
    """Elimina una imagen de un producto (rol Diseñador). Úsala cuando el
    dueño diga que una foto se ve mal, quedó repetida o hay que sacar la que
    se reemplazó:
    - imagen_id (id de una foto de la GALERÍA, el que devuelve
      datos_para_publicar en fotos.galeria): borra esa foto, y si era la
      principal promueve la siguiente automáticamente.
    - destino 'principal': elimina SOLO la foto principal del producto (la
      siguiente de la galería asciende a principal si existe).
    Envía imagen_id O destino, no ambos. El borrado es definitivo (la foto
    desaparece del producto y del almacenamiento)."""
    permiso = _requiere_permiso("eliminar_imagen")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarla en Ajustes → Negocio → Conectar mi IA."
            )
        }
    try:
        return mcp_imagenes.eliminar(tenant, producto_id, imagen_id, destino)
    except Exception as e:
        print(f"[MCP] eliminar_imagen fallo: {type(e).__name__}: {e}")
        return {"error": f"No se pudo eliminar la imagen: {e}"}


@servidor.tool(annotations=CONFIRMACION)
def reordenar_imagenes(producto_id: int, orden_ids: list[int]) -> dict:
    """Reordena las fotos de la galería de un producto (rol Diseñador, plan
    Plus). Recibe TODOS los ids de las fotos (los de datos_para_publicar en
    fotos.galeria) en el orden nuevo: la PRIMERA del array pasa a ser la foto
    principal del producto, el resto le sigue. Úsala cuando el dueño diga
    'que la foto X sea la principal' o 'pon esta foto primero'."""
    permiso = _requiere_permiso("reordenar_imagenes")
    if permiso:
        return permiso
    tenant = tenant_de_request()
    if not contar_escritura(clave_de_request()):
        return {
            "error": (
                "Límite diario de escrituras alcanzado para esta clave. "
                "El dueño puede ajustarla en Ajustes → Negocio → Conectar mi IA."
            )
        }
    try:
        return mcp_imagenes.reordenar(tenant, producto_id, orden_ids or [])
    except Exception as e:
        print(f"[MCP] reordenar_imagenes fallo: {type(e).__name__}: {e}")
        return {"error": f"No se pudo reordenar las imágenes: {e}"}


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
