# ==============================================================================
# backend/database/mcp_catalogo.py
# Queries de SOLO LECTURA para el servidor MCP (backend/muse/server.py).
#
# REGLA DE ORO: columnas EXPLÍCITAS, nunca SELECT *. Los costos (lotes.Costo,
# costo_promedio, costo_servicio) y los rangos internos (precio_min/max) NO se
# seleccionan: no es que el agente "prometa" ignorarlos — directamente no
# salen de la base de datos.
#
# Precio y stock usan la MISMA fórmula del catálogo público
# (database/catalogo.py, obtener_catalogo_publico) para que el agente reporte
# exactamente el mismo número que ve el dueño en el gestor y sus clientes en
# el catálogo web.
#
# Alcance definido con el dueño:
#   - LECTURAS (buscar/listar/stock): físicos + COMPUESTOS. Un restaurante
#     vende hamburguesas, no sus ingredientes por separado: si el agente no
#     ve el compuesto no puede responder nada sobre él. Los compuestos no
#     tienen stock propio: se informa `disponibilidad_estimada` (el motor de
#     recetas del gestor) y su precio vive en productos.precio_servicio.
#   - PUBLICACIÓN (datos_para_publicar / obtener_producto / obtener_fotos):
#     sigue siendo SOLO físicos. Marketplace es de bienes; que los compuestos
#     se publiquen es una decisión de negocio aparte.
#   - Stock en unidades exactas: el agente puede responder "cuánto queda".
#   - Sin filtro visible_en_catalogo: publicar en Marketplace es otro canal
#     distinto del catálogo web; el agente ve todos los activos.
# ==============================================================================

import unicodedata

from database.conexion import query
from database.helpers import _obtener_categorias_subquery

# Misma expresión de precio del catálogo público: MAX entre lotes CON stock;
# si ningún lote tiene stock cae al MAX histórico de los lotes activos.
_PRECIO_VENTA = (
    "COALESCE(MAX(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), "
    "MAX(l.Precio_Venta), 0)"
)

# JOIN de lotes activos, idéntico al del catálogo público.
_JOIN_LOTES = (
    "LEFT JOIN lotes l ON l.producto_id = p.id AND l.tenant_id = p.tenant_id "
    "AND l.Estado = 'Activo'"
)

# Filtro de alcance para PUBLICAR: solo productos físicos activos del tenant.
# No se toca: datos_para_publicar es el canal de Marketplace.
_FILTRO_ALCANCE = (
    "p.Estado = 'Activo' AND p.tenant_id = %s AND p.tipo_producto = 'stock'"
)

# Filtro de las LECTURAS del agente: físicos + compuestos (restaurantes). Los
# servicios siguen fuera: su cobro es otro flujo.
_ALCANCE_LECTURA = (
    "p.Estado = 'Activo' AND p.tenant_id = %s "
    "AND p.tipo_producto IN ('stock', 'compuesto')"
)

# Precio de las lecturas con compuestos: un compuesto no tiene lotes, así que
# su precio de venta vive en productos.precio_servicio (igual que el gestor:
# database/productos.py). Los físicos siguen usando sus lotes y solo caen a
# precio_servicio si no tienen ningún lote, igual que la pantalla de inventario.
_PRECIO_VENTA_LECTURA = (
    "COALESCE(MAX(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), "
    "MAX(l.Precio_Venta), p.precio_servicio, 0)"
)

# Costo promedio ponderado de los lotes ACTIVOS con stock (espejo del
# inventario del gestor). SOLO se agrega al SELECT cuando la clave tiene el
# permiso ver_costos (migrations/068): apagado, el costo ni siquiera viaja.
_COSTO_PROMEDIO = (
    "CASE WHEN COALESCE(SUM(CASE WHEN l.Stock_Lote > 0 THEN l.Stock_Lote END), 0) > 0 "
    "THEN SUM(CASE WHEN l.Stock_Lote > 0 THEN l.Costo * l.Stock_Lote END) "
    "/ SUM(CASE WHEN l.Stock_Lote > 0 THEN l.Stock_Lote END) ELSE 0 END"
)

# Categorías del producto (todas, no solo las visibles en catálogo).
_CATEGORIAS = _obtener_categorias_subquery("p", visible_only=False)


def _num(valor) -> float:
    try:
        return round(float(valor or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _stock(valor) -> float:
    """Stock como float: los fraccionables manejan 0.5 kg, 1.5 m, etc."""
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _con_disponibilidad(items: list[dict], tenant_id: str) -> None:
    """
    Añade `disponibilidad_estimada` a los COMPUESTOS del resultado mutando la
    lista. Reutiliza el motor de recetas del gestor (database/recetas.py): no
    se recalcula nada aquí, así el agente ve el mismo número que el dueño.

    Los productos físicos quedan igual (su dato es stock_disponible). Un
    compuesto sin receta queda en None.
    """
    compuestos = [
        {"producto": it["nombre"], "tipo_producto": "compuesto"}
        for it in items if it.get("tipo") == "compuesto"
    ]
    if not compuestos:
        return

    from database.recetas import (  # import local: evita ciclo al cargar el MCP
        _adjuntar_recetas,
        _calcular_disponibilidad_compuestos,
    )

    _adjuntar_recetas(compuestos, tenant_id)
    _calcular_disponibilidad_compuestos(compuestos, tenant_id)
    disponibles = {
        c["producto"]: c.get("disponibilidad_estimada") for c in compuestos
    }
    for item in items:
        if item.get("tipo") == "compuesto":
            item["disponibilidad_estimada"] = disponibles.get(item["nombre"])


def _con_variaciones(items: list[dict], tenant_id: str) -> None:
    """
    Añade a los FÍSICOS: `variaciones` [{nombre, precio, stock, tamano_cm}],
    `tiene_variaciones` y, si hay precios válidos, `precio_desde`/`precio_hasta`.

    Sin esto el agente solo ve el precio agregado (MAX de lotes) y no puede
    responder "¿cuánto cuesta la begonia grande?": el tamaño, su precio y su
    stock viven en producto_variaciones y en los lotes de ESA variación. Se
    usa el mismo criterio del catálogo público (variaciones.py), nunca los
    lotes base (en un producto con variaciones no hay lote base).

    Los COMPUESTOS quedan fuera a propósito: su disponibilidad se calcula con
    la receta (ver_receta) y su variación se resuelve al vender.
    """
    ids = [it["id"] for it in items if it.get("tipo") == "stock"]
    if not ids:
        return
    filas = query(
        """
        SELECT v.producto_id, v.nombre, v.precio, v.tamano_cm,
               COALESCE((SELECT SUM(l.Stock_Lote) FROM lotes l
                         WHERE l.variacion_id = v.id AND l.Estado = 'Activo'
                           AND l.tenant_id = v.tenant_id), 0) AS stock
        FROM producto_variaciones v
        WHERE v.tenant_id = %s AND v.producto_id = ANY(%s)
        ORDER BY v.nombre ASC
        """,
        (tenant_id, ids),
    )
    por_producto: dict[int, list[dict]] = {}
    for f in filas:
        por_producto.setdefault(f["producto_id"], []).append({
            "nombre": f["nombre"],
            "precio": _num(f["precio"]),
            "stock": _stock(f["stock"]),
            "tamano_cm": (
                float(f["tamano_cm"]) if f.get("tamano_cm") is not None else None
            ),
        })
    for it in items:
        if it.get("tipo") != "stock":
            continue
        variaciones = por_producto.get(it["id"], [])
        it["tiene_variaciones"] = bool(variaciones)
        it["variaciones"] = variaciones
        precios = [v["precio"] for v in variaciones if v["precio"] > 0]
        if precios:
            it["precio_desde"] = min(precios)
            it["precio_hasta"] = max(precios)


# ── Búsqueda tolerante ────────────────────────────────────────────────────────
# El dueño escribe "My Melody" y el producto se llama "MyMelody", o escribe
# "pokemon" y el producto dice "Pokémon". Normalizamos AMBOS lados:
#   - En Python: la consulta del agente (minúsculas, sin acentos, por palabras).
#   - En SQL: los campos del catálogo (translate baja acentos sin extensiones).
# Cada palabra debe aparecer en ALGUNO de estos campos: nombre, descripción,
# código interno, código de barras o categoría. Así "Sanrio" y "FNAF" traen
# todo lo de esa categoría aunque no tengan la palabra en el nombre.

_ACCENTES = "áàäâãéèëêíìïîóòöôõúùüûñçÁÀÄÂÃÉÈÍÌÏÎÓÒÖÔÕÚÙÜÛÑÇ"
_PLANO = "aaaaaeeeeiiiiooooouuuunccAAAAAEEEEIIIIOOOOOUUUUNCC"


def _norm(expr: str) -> str:
    """Expresión SQL que da la versión minúscula y sin acentos de un campo."""
    return f"translate(lower({expr}), '{_ACCENTES}', '{_PLANO}')"


def _normalizar(texto: str) -> str:
    """La consulta del agente: minúsculas y sin acentos (lado Python)."""
    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _match_token(token: str) -> tuple[str, list[str]]:
    """Condición SQL: la palabra aparece en cualquier campo de búsqueda."""
    patron = f"%{token}%"
    campo_desc = "COALESCE(p.descripcion, '')"
    campo_codigo = "COALESCE(p.codigo_interno, '')"
    sql = (
        f"({_norm('p.producto')} LIKE %s"
        f" OR {_norm(campo_desc)} LIKE %s"
        f" OR {_norm(campo_codigo)} LIKE %s"
        f" OR COALESCE(p.codigo_barras, '') LIKE %s"
        " OR EXISTS (SELECT 1 FROM producto_categorias pcx"
        "             JOIN categorias cx ON cx.id = pcx.categoria_id"
        "             WHERE pcx.producto_id = p.id"
        f"              AND {_norm('cx.nombre')} LIKE %s))"
    )
    return sql, [patron, patron, patron, patron, patron]


def _condicion_busqueda(consulta: str) -> tuple[str, list[str], str, list[str]]:
    """
    Devuelve (filtro_where, params_where, orden, params_orden):
      - filtro_where: AND de tokens; cada token en cualquier campo.
      - orden: relevancia — empieza igual > contiene la frase > el resto,
        y con stock antes que agotados.
    """
    tokens = [t for t in _normalizar(consulta).split() if t]
    frase = _normalizar(consulta).replace(" ", "")
    partes: list[str] = []
    params: list[str] = []
    for t in tokens:
        sql_t, params_t = _match_token(t)
        partes.append(sql_t)
        params += params_t
    nombre_plano = f"replace({_norm('p.producto')}, ' ', '')"
    orden = (
        "CASE "
        f"WHEN {nombre_plano} LIKE %s THEN 0 "  # empieza igual a la frase
        f"WHEN {nombre_plano} LIKE %s THEN 1 "  # la contiene
        "ELSE 2 END, "
        "CASE WHEN COALESCE(SUM(l.Stock_Lote), 0) > 0 THEN 0 ELSE 1 END, "
        "p.producto ASC"
    )
    params_orden = [f"%{frase}%", f"%{frase}%"]
    return " AND ".join(partes), params, orden, params_orden


# ── Tool: buscar_productos ────────────────────────────────────────────────────

def buscar_productos(
    tenant_id: str, consulta: str, solo_con_stock: bool = False, limite: int = 15,
    con_costos: bool = False,
) -> list[dict]:
    """
    Búsqueda tolerante: por palabras (no frase completa), en nombre,
    descripción, códigos y categoría, ignorando acentos, mayúsculas y
    espacios. Ordenada por relevancia. Incluye productos físicos Y compuestos
    (los compuestos traen `tipo` y `disponibilidad_estimada`; su stock propio
    es null porque se agota por ingredientes).

    con_costos (migrations/068): SOLO claves con el permiso ver_costos ven
    el costo promedio ponderado y el margen. Default False: el costo no se
    selecciona en la query.
    """
    filtro_tokens, params_tokens, orden, params_orden = _condicion_busqueda(consulta)
    filtro_stock = "HAVING COALESCE(SUM(l.Stock_Lote), 0) > 0" if solo_con_stock else ""
    campo_costo = f"{_COSTO_PROMEDIO} AS costo_unitario," if con_costos else ""
    filas = query(
        f"""
        SELECT p.id, p.producto AS nombre, p.sufijo_precio, p.tipo_producto,
               {_PRECIO_VENTA_LECTURA} AS precio_venta,
               {campo_costo}
               COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible,
               {_CATEGORIAS}
        FROM productos p
        {_JOIN_LOTES}
        WHERE {_ALCANCE_LECTURA}
          {f"AND {filtro_tokens}" if filtro_tokens else ""}
        GROUP BY p.id
        {filtro_stock}
        ORDER BY {orden}
        LIMIT %s
        """,
        tuple([tenant_id] + params_tokens + params_orden + [limite]),
    )
    resultado = []
    for f in filas:
        tipo = f.get("tipo_producto") or "stock"
        item = {
            "id": f["id"],
            "nombre": f["nombre"],
            "tipo": tipo,
            "precio_venta": _num(f["precio_venta"]),
            "sufijo_precio": f.get("sufijo_precio") or "",
            "stock_disponible": (
                None if tipo == "compuesto" else _stock(f["stock_disponible"])
            ),
            "categoria": f.get("categoria") or ["General"],
        }
        if tipo == "compuesto":
            item["disponibilidad_estimada"] = None
        if con_costos:
            costo = _num(f.get("costo_unitario"))
            item["costo_unitario"] = costo
            item["margen"] = round(item["precio_venta"] - costo, 2)
        resultado.append(item)
    _con_variaciones(resultado, tenant_id)
    _con_disponibilidad(resultado, tenant_id)
    return resultado


# ── Ficha interna de datos_para_publicar ─────────────────────────────────────
# obtener_producto y obtener_fotos NO son tools del MCP (se retiraron por
# redundantes): son las funciones que datos_para_publicar compone en un solo
# paquete. Al ser la única vía, el paquete es SIEMPRE público: jamás lleva
# costos (el costo individual con permiso ver_costos se lee en
# buscar_productos / listar_productos).

def obtener_producto(tenant_id: str, producto_id: int) -> dict | None:
    """Ficha completa del producto. None si no existe / no es del tenant.
    Nunca incluye costos: es la base del paquete de publicación."""
    filas = query(
        f"""
        SELECT p.id, p.producto AS nombre, p.descripcion AS descripcion,
               p.sufijo_precio, p.tamano_cm,
               {_PRECIO_VENTA} AS precio_venta,
               COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible,
               {_CATEGORIAS}
        FROM productos p
        {_JOIN_LOTES}
        WHERE {_FILTRO_ALCANCE} AND p.id = %s
        GROUP BY p.id
        """,
        (tenant_id, producto_id),
    )
    if not filas:
        return None

    fila = filas[0]

    # Variaciones con su precio y stock propio (misma query del catálogo
    # público: lotes activos ligados a cada variación).
    variaciones = query(
        """
        SELECT v.nombre, v.precio, v.tamano_cm,
               COALESCE((SELECT SUM(l2.Stock_Lote) FROM lotes l2
                         WHERE l2.variacion_id = v.id AND l2.Estado = 'Activo'
                           AND l2.tenant_id = v.tenant_id), 0) AS stock
        FROM producto_variaciones v
        JOIN productos p ON p.id = v.producto_id
        WHERE v.producto_id = %s AND p.tenant_id = %s
        ORDER BY v.nombre ASC
        """,
        (producto_id, tenant_id),
    )

    tamano = fila.get("tamano_cm")
    ficha = {
        "id": fila["id"],
        "nombre": fila["nombre"],
        "descripcion": fila.get("descripcion") or "",
        "precio_venta": _num(fila["precio_venta"]),
        "sufijo_precio": fila.get("sufijo_precio") or "",
        "stock_disponible": _stock(fila["stock_disponible"]),
        "tamano_cm": float(tamano) if tamano is not None else None,
        "categoria": fila.get("categoria") or ["General"],
        # Marketplace pregunta la condición: sugerimos "nuevo" y el dueño
        # la corrige al revisar la publicación.
        "condicion_sugerida": "nuevo",
        "variaciones": [
            {"nombre": v["nombre"], "precio": _num(v["precio"]),
             "tamano_cm": float(v["tamano_cm"]) if v.get("tamano_cm") is not None else None,
             "stock": _stock(v["stock"])}
            for v in variaciones
        ],
    }
    return ficha


# ── Galería interna de datos_para_publicar ───────────────────────────────────

def obtener_fotos(tenant_id: str, producto_id: int) -> dict | None:
    """
    Galería del producto: foto principal (productos.Imagen) + extras
    (producto_imagenes, tenants Plus), sin duplicados ni "No hay foto".
    Son URLs públicas de Cloudinary: el agente las descarga directo.

    Cada foto llega como objeto {"id", "url", "orden", "es_principal"}: el
    "id" es el de producto_imagenes y es lo que las tools del rol Diseñador
    (eliminar_imagen / reordenar_imagenes) piden. La foto principal sin fila
    en la galería (plan básico o desync pre-galería) llega con id null —
    para esa existe eliminar_imagen con destino='principal'.
    """
    filas = query(
        """
        SELECT p.imagen AS imagen
        FROM productos p
        WHERE p.id = %s AND p.tenant_id = %s
          AND p.Estado = 'Activo' AND p.tipo_producto = 'stock'
        """,
        (producto_id, tenant_id),
    )
    if not filas:
        return None

    principal_url = filas[0].get("imagen")

    extras = query(
        "SELECT id, url, orden FROM producto_imagenes "
        "WHERE producto_id = %s AND tenant_id = %s ORDER BY orden ASC",
        (producto_id, tenant_id),
    )

    galeria: list[dict] = []
    vistas: set[str] = set()
    foto_principal_id: int | None = None

    # La fila orden=1 es la principal oficial de la galería (invariante Plus:
    # se sincroniza con productos.Imagen). Si la URL no coincide (desync
    # pre-galería), la primera foto es la de productos.Imagen SIN id.
    fila1 = next((e for e in extras if e["orden"] == 1), None)
    if principal_url and principal_url != "No hay foto":
        if fila1 and fila1["url"] == principal_url:
            foto_principal_id = fila1["id"]
        galeria.append({
            "id": foto_principal_id,
            "url": principal_url,
            "orden": fila1["orden"] if foto_principal_id is not None else None,
            "es_principal": True,
        })
        vistas.add(principal_url)

    for e in extras:
        url = e.get("url")
        if not url or url in vistas:
            continue
        galeria.append({
            "id": e["id"],
            "url": url,
            "orden": e["orden"],
            "es_principal": False,
        })
        vistas.add(url)

    return {
        "foto_principal": galeria[0]["url"] if galeria else None,
        "foto_principal_id": foto_principal_id,
        "galeria": galeria,
        "total": len(galeria),
        "nota": (
            "Usa el id de cada foto de galeria para eliminar_imagen y "
            "reordenar_imagenes; la foto con id null (solo principal, sin "
            "fila de galería) se elimina con eliminar_imagen y "
            "destino='principal'."
        ),
    }


# ── Tool: stock_actual ────────────────────────────────────────────────────────

def stock_actual(
    tenant_id: str, producto_id: int | None = None, consulta: str | None = None,
    limite: int = 10,
) -> list[dict]:
    """Stock por id exacto o por búsqueda de nombre (todas las coincidencias).
    Incluye compuestos: en ellos `stock_disponible` es None y el dato real es
    `disponibilidad_estimada` (cuántas unidades alcanzan con los ingredientes)."""
    if producto_id is not None:
        filas = query(
            f"""
            SELECT p.id, p.producto AS nombre, p.tipo_producto,
                   COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible
            FROM productos p
            {_JOIN_LOTES}
            WHERE {_ALCANCE_LECTURA} AND p.id = %s
            GROUP BY p.id
            """,
            (tenant_id, producto_id),
        )
    else:
        filtro_tokens, params_tokens, orden, params_orden = _condicion_busqueda(consulta or "")
        filas = query(
            f"""
            SELECT p.id, p.producto AS nombre, p.tipo_producto,
                   COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible
            FROM productos p
            {_JOIN_LOTES}
            WHERE {_ALCANCE_LECTURA}
              {f"AND {filtro_tokens}" if filtro_tokens else ""}
            GROUP BY p.id
            ORDER BY {orden}
            LIMIT %s
            """,
            tuple([tenant_id] + params_tokens + params_orden + [limite]),
        )
    resultado = []
    for f in filas:
        tipo = f.get("tipo_producto") or "stock"
        item = {
            "id": f["id"],
            "nombre": f["nombre"],
            "tipo": tipo,
            "stock_disponible": (
                None if tipo == "compuesto" else _stock(f["stock_disponible"])
            ),
        }
        if tipo == "compuesto":
            item["disponibilidad_estimada"] = None
        resultado.append(item)
    _con_variaciones(resultado, tenant_id)
    _con_disponibilidad(resultado, tenant_id)
    return resultado


# ── Tool: listar_categorias ───────────────────────────────────────────────────

def listar_categorias(tenant_id: str) -> list[str]:
    """Categorías con al menos un producto activo (físico o compuesto)."""
    filas = query(
        """
        SELECT DISTINCT c.nombre
        FROM categorias c
        JOIN producto_categorias pc ON pc.categoria_id = c.id
        JOIN productos p ON pc.producto_id = p.id
        WHERE c.tenant_id = %s AND p.tenant_id = %s
          AND p.Estado = 'Activo'
          AND p.tipo_producto IN ('stock', 'compuesto')
        ORDER BY c.nombre ASC
        """,
        (tenant_id, tenant_id),
    )
    return [f["nombre"] for f in filas]


# ── Tool: datos_para_publicar ─────────────────────────────────────────────────

def datos_para_publicar(tenant_id: str, producto_id: int) -> dict | None:
    """
    Paquete completo en una llamada: ficha + fotos. Reduce round-trips y
    garantiza que el "paquete de publicación" lleve exactamente los campos
    permitidos (nunca costos).
    """
    ficha = obtener_producto(tenant_id, producto_id)
    if ficha is None:
        return None
    fotos = obtener_fotos(tenant_id, producto_id) or {
        "foto_principal": None, "galeria": [], "total": 0,
    }
    ficha["fotos"] = fotos
    return ficha


# ── Tool: listar_productos (nivel 1) ─────────────────────────────────────────

TOPE_PAGINA = 50  # protege la ventana de contexto del agente, no la base


def listar_productos(
    tenant_id: str,
    categoria: str | None = None,
    estado_stock: str | None = None,
    umbral_stock_bajo: int = 3,
    sin_datos: str | None = None,
    orden: str | None = None,
    pagina: int = 1,
    limite: int = 20,
    con_costos: bool = False,
) -> dict:
    """
    Listado paginado del catálogo activo con filtros. Cubre lo que la
    búsqueda por términos no puede: "todo el stock bajo", "lo de Sanrio",
    "productos sin foto o sin descripción". Incluye físicos Y compuestos; los
    compuestos traen `tipo` y `disponibilidad_estimada` (su stock se agota
    por ingredientes, no por lotes propios).

    estado_stock: 'disponible' (>0) · 'stock_bajo' (0 < stock <= umbral) ·
                  'sin_stock' (<= 0). 'disponible' incluye a los bajos. El
                  filtro aplica SOLO a físicos: un compuesto no tiene lotes.
    sin_datos:    'sin_foto' · 'sin_descripcion' · 'sin_tamanio' ·
                  'incompletos' (cualquiera de las tres).
    orden:        'nombre' (default) · 'stock_asc' (lo que falta primero) ·
                  'recientes' (altas nuevas primero).
    """
    limite = max(1, min(int(limite or 20), TOPE_PAGINA))
    pagina = max(1, int(pagina or 1))
    try:
        umbral = max(1, int(umbral_stock_bajo))
    except (TypeError, ValueError):
        umbral = 3

    filtros: list[str] = []
    params: list = [tenant_id]
    havings: list[str] = []

    categoria = (categoria or "").strip()
    if categoria:
        patron = f"%{_normalizar(categoria)}%"
        filtros.append(
            "EXISTS (SELECT 1 FROM producto_categorias pcx"
            "         JOIN categorias cx ON cx.id = pcx.categoria_id"
            "         WHERE pcx.producto_id = p.id"
            f"          AND {_norm('cx.nombre')} LIKE %s)"
        )
        params.append(patron)

    estado = (estado_stock or "").strip().lower()
    if estado in ("disponible", "stock_bajo", "sin_stock"):
        # El stock propio solo existe en físicos: un compuesto sin lotes no es
        # "agotado" (su disponibilidad sale de la receta, en Python).
        filtros.append("p.tipo_producto = 'stock'")
        # Agregados → van en HAVING, no en WHERE.
        stock = "COALESCE(SUM(l.Stock_Lote), 0)"
        if estado == "sin_stock":
            havings.append(f"{stock} <= 0")
        elif estado == "disponible":
            havings.append(f"{stock} > 0")
        else:  # stock_bajo
            havings.append(f"{stock} > 0 AND {stock} <= {int(umbral)}")

    calidad = (sin_datos or "").strip().lower()
    if calidad:
        sin_foto = "(p.imagen IS NULL OR p.imagen = 'No hay foto')"
        sin_desc = "COALESCE(p.descripcion, '') = ''"
        sin_tam = "p.tamano_cm IS NULL"
        if calidad == "sin_foto":
            filtros.append(sin_foto)
        elif calidad == "sin_descripcion":
            filtros.append(sin_desc)
        elif calidad == "sin_tamanio":
            filtros.append(sin_tam)
        elif calidad in ("incompletos", "incompleto"):
            filtros.append(f"({sin_foto} OR {sin_desc} OR {sin_tam})")

    orden_sql = "p.producto ASC"
    if estado in ("stock_bajo", "sin_stock") and not orden:
        # Los compuestos no tienen lote: no son "lo que falta primero".
        orden_sql = (
            "CASE WHEN p.tipo_producto = 'compuesto' THEN 1 ELSE 0 END, "
            "COALESCE(SUM(l.Stock_Lote), 0) ASC, p.producto ASC"
        )
    elif orden == "stock_asc":
        orden_sql = (
            "CASE WHEN p.tipo_producto = 'compuesto' THEN 1 ELSE 0 END, "
            "COALESCE(SUM(l.Stock_Lote), 0) ASC, p.producto ASC"
        )
    elif orden == "recientes":
        orden_sql = "p.id DESC"

    campo_costo = f"{_COSTO_PROMEDIO} AS costo_unitario," if con_costos else ""
    where = f"WHERE {_ALCANCE_LECTURA}" + (" AND " + " AND ".join(filtros) if filtros else "")
    having = f" HAVING {' AND '.join(havings)}" if havings else ""
    base = f"FROM productos p {_JOIN_LOTES} {where} GROUP BY p.id{having}"

    total = query(
        f"SELECT COUNT(*) AS c FROM (SELECT p.id {base}) sub",
        tuple(params),
    )[0]["c"]

    filas = query(
        f"""
        SELECT p.id, p.producto AS nombre, p.sufijo_precio, p.descripcion,
               p.imagen, p.tamano_cm, p.tipo_producto,
               {_PRECIO_VENTA_LECTURA} AS precio_venta,
               {campo_costo}
               COALESCE(SUM(l.Stock_Lote), 0) AS stock_disponible,
               {_CATEGORIAS}
        {base}
        ORDER BY {orden_sql}
        LIMIT %s OFFSET %s
        """,
        tuple(params + [limite, (pagina - 1) * limite]),
    )

    def _tiene_foto(imagen) -> bool:
        return bool(imagen) and imagen != "No hay foto"

    productos = []
    for f in filas:
        tipo = f.get("tipo_producto") or "stock"
        item = {
            "id": f["id"],
            "nombre": f["nombre"],
            "tipo": tipo,
            "precio_venta": _num(f["precio_venta"]),
            "sufijo_precio": f.get("sufijo_precio") or "",
            "stock_disponible": (
                None if tipo == "compuesto" else _stock(f["stock_disponible"])
            ),
            "categoria": f.get("categoria") or ["General"],
            "tiene_foto": _tiene_foto(f.get("imagen")),
            "sin_descripcion": not (f.get("descripcion") or "").strip(),
            "sin_tamano": f.get("tamano_cm") is None,
        }
        if tipo == "compuesto":
            item["disponibilidad_estimada"] = None
        if con_costos:
            costo = _num(f.get("costo_unitario"))
            item["costo_unitario"] = costo
            item["margen"] = round(item["precio_venta"] - costo, 2)
        productos.append(item)
    _con_variaciones(productos, tenant_id)
    _con_disponibilidad(productos, tenant_id)

    return {
        "total": int(total),
        "pagina": pagina,
        "paginas": max(1, -(-int(total) // limite)),
        "limite": limite,
        "productos": productos,
    }
