# ==============================================================================
# backend/database/lotes.py
# Lógica de inventario y algoritmo PEPS — sin Streamlit.
# ==============================================================================

import uuid
import datetime
import re
from zoneinfo import ZoneInfo
from psycopg2.errors import UniqueViolation


# ── Zona horaria del negocio (Cancún, UTC-5) ──
_TZ = ZoneInfo("America/Cancun")
from database.conexion import query, execute
from psycopg2.extras import RealDictCursor


def _q(conn, sql: str, params: tuple = ()) -> list[dict]:
    """
    Ejecuta un SELECT sobre la conexión `conn` si se provee (para usarse dentro
    de una transacción atómica) o sobre la conexión del pool global si no.
    """
    if conn is None:
        return query(sql, params)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _e(conn, sql: str, params: tuple = ()) -> None:
    """
    Ejecuta un INSERT/UPDATE/DELETE sobre la conexión `conn` si se provee
    (para usarse dentro de una transacción atómica) o el pool global si no.
    """
    if conn is None:
        execute(sql, params)
        return
    with conn.cursor() as cur:
        cur.execute(sql, params)

# ==============================================================================
# Funciones auxiliares para el manejo de categorías (Many-to-Many)
# ==============================================================================


def _slugify(texto: str) -> str:
    """
    Convierte un nombre de categoría en un slug URL-amigable.
    Ej: 'Accesorios de Moda' → 'accesorios-de-moda'
    """
    texto = texto.lower().strip()
    # Reemplazar espacios y caracteres no alfanuméricos (excepto guiones) por guiones
    texto = re.sub(r'[^a-z0-9áéíóúüñ\s-]', '', texto)
    texto = re.sub(r'[\s-]+', '-', texto)
    return texto.strip('-')


def _sincronizar_categorias(producto_id: int, categorias: list[str], tenant_id: str, conn=None) -> None:
    """
    Sincroniza las categorías de un producto en la tabla pivote (producto_categorias).
    
    Estrategia:
    1. Elimina todas las relaciones existentes para este producto en producto_categorias.
    2. Por cada nombre de categoría, hace un upsert en la tabla 'categorias' y
       crea la relación en 'producto_categorias'.
    
    Args:
        producto_id: ID numérico del producto (productos.id)
        categorias: Lista de nombres de categorías a asignar
        tenant_id: UUID del tenant propietario
        conn: si se provee, todo se ejecuta sobre esa conexión (para usarse
              dentro de una transacción atómica, ej. crear_producto_completo).
    """
    # Normalizar: limpiar espacios y eliminar duplicados preservando orden
    categorias = list(dict.fromkeys([c.strip() for c in categorias if c.strip()]))
    if not categorias:
        categorias = ["General"]

    # 1. Eliminar relaciones existentes para este producto
    _e(conn,
        "DELETE FROM producto_categorias WHERE producto_id = %s",
        (producto_id,)
    )

    # 2. Upsert cada categoría y crear la relación
    for nombre in categorias:
        slug = _slugify(nombre)

        # Upsert: si ya existe (tenant_id, nombre), devuelve el id existente
        result = _q(conn, """
            INSERT INTO categorias (tenant_id, nombre, slug)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id, nombre) DO UPDATE SET
                slug = EXCLUDED.slug
            RETURNING id
        """, (tenant_id, nombre, slug))

        categoria_id = result[0]["id"]

        # Insertar en la tabla pivote (ignorar si ya existe por alguna razón)
        _e(conn,
            "INSERT INTO producto_categorias (producto_id, categoria_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (producto_id, categoria_id)
        )


def _obtener_categorias_subquery(alias: str, visible_only: bool = False) -> str:
    """
    Genera una subquery SQL correlacionada para obtener las categorías
    de un producto como un array de nombres.
    
    Úsala en cualquier SELECT que necesite incluir categorías sin importar
    la columna antigua Categoria de la tabla productos.
    
    Args:
        alias: Alias de la tabla productos (e.g. 'p')
        visible_only: Si True, solo incluye categorías con visible_en_catalogo = true
    """
    filtro_visible = "AND c.visible_en_catalogo = true" if visible_only else ""
    return f"""COALESCE(
        (SELECT array_agg(c.nombre ORDER BY c.nombre)
         FROM producto_categorias pc
         JOIN categorias c ON pc.categoria_id = c.id
         WHERE pc.producto_id = {alias}.id {filtro_visible}),
        ARRAY['General']
    ) AS categoria"""


def get_lotes(tenant_id: str) -> list[dict]:
    """Lee lotes + metadatos de productos en un JOIN."""
    cat_subquery = _obtener_categorias_subquery("p")
    return query(f"""
        SELECT 
            l.id as id,
            l.id_lote as id_lote,
            l.producto as producto,
            l.costo as costo,
            l.precio_venta as precio_venta,
            l.stock_lote as stock_lote,
            l.fecha_entrada as fecha_entrada,
            l.estado as estado,
            l.etiqueta as etiqueta,
            l.variacion_id as variacion_id,
            v.nombre as variacion,
            p.Imagen as imagen, 
            p.Descripcion as descripcion,
            p.visible_en_catalogo as visible_en_catalogo,
            {cat_subquery}
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto AND l.tenant_id = p.tenant_id
        LEFT JOIN producto_variaciones v ON v.id = l.variacion_id
        WHERE l.Estado = 'Activo' AND l.tenant_id = %s
        ORDER BY l.Producto, l.Fecha_Entrada ASC
    """, (tenant_id,))


def _adjuntar_variaciones(filas: list[dict], tenant_id: str) -> None:
    """
    Adjunta las variaciones de cada producto a una lista de filas que ya
    contienen la llave `producto` (nombre). Muta las filas in-place añadiendo
    el campo `variaciones` = [{id, nombre, precio}, ...].

    Los productos sin variaciones quedan con lista vacía (el frontend lo trata
    como "sin selector").
    """
    if not filas:
        return
    variaciones = query("""
        SELECT p.Producto AS producto, v.id, v.nombre, v.precio, v.foto
        FROM producto_variaciones v
        JOIN productos p ON p.id = v.producto_id
        WHERE p.tenant_id = %s
        ORDER BY p.Producto ASC, v.nombre ASC
    """, (tenant_id,))
    # Stock por variación (Fase 6): suma el stock de los lotes ligados a cada
    # variación. Solo es relevante si el producto activó stock_por_variacion;
    # si no, las variaciones comparten el stock del producto y esto queda 0.
    stocks = query("""
        SELECT l.Producto AS producto, l.variacion_id AS variacion_id,
               SUM(l.Stock_Lote) AS stock
        FROM lotes l
        WHERE l.tenant_id = %s AND l.Estado = 'Activo' AND l.variacion_id IS NOT NULL
        GROUP BY l.Producto, l.variacion_id
    """, (tenant_id,))
    stock_por_var: dict = {}
    for s in stocks:
        stock_por_var[(s["producto"], s["variacion_id"])] = float(s["stock"] or 0)
    por_producto: dict = {}
    for v in variaciones:
        por_producto.setdefault(v["producto"], []).append({
            "id": v["id"],
            "nombre": v["nombre"],
            "precio": float(v["precio"] or 0),
            "foto": v.get("foto") or "",
            "stock": stock_por_var.get((v["producto"], v["id"]), 0),
        })
    for f in filas:
        f["variaciones"] = por_producto.get(f["producto"], [])


def _calcular_disponibilidad_compuestos(filas: list[dict], tenant_id: str) -> None:
    """
    Añade `disponibilidad_estimada` (int o None) a cada producto compuesto:
    cuántas unidades se pueden vender con el stock ACTUAL de sus materiales.

    Se calcula POR RECETA (base y cada variación por separado) y se toma el
    MÍNIMO entre ellas: es la cantidad que garantizas poder vender sin
    importar qué presentación pida el cliente.

    Ej: Hamburguesa = 1 pan + 150 carne + 2 queso. Con 4 panes, 600g carne
    y 10 quesos → quedan min(4/1, 600/150, 10/2) = min(4, 4, 5) = 4.
    None = el compuesto no tiene receta (no se puede estimar).
    """
    compuestos = [f for f in filas if f.get("tipo_producto") == "compuesto"]
    if not compuestos:
        return

    # Recetas por compuesto, SIN mezclar: la base es una receta, y cada
    # variación con receta propia es otra receta independiente.
    recetas_por_compuesto: dict[str, list[dict[str, float]]] = {}
    for f in compuestos:
        recetas_por_compuesto[f["producto"]] = []
        base = [r for r in (f.get("recetas") or []) if r.get("variacion_id") is None]
        if base:
            recetas_por_compuesto[f["producto"]].append(
                {r["material"]: float(r["cantidad"] or 0) for r in base}
            )
        por_variacion: dict[int, list[dict]] = {}
        for r in (f.get("recetas") or []):
            if r.get("variacion_id") is not None:
                por_variacion.setdefault(r["variacion_id"], []).append(r)
        for var_recetas in por_variacion.values():
            recetas_por_compuesto[f["producto"]].append(
                {r["material"]: float(r["cantidad"] or 0) for r in var_recetas}
            )

    nombres = sorted({
        m for recetas in recetas_por_compuesto.values() for receta in recetas for m in receta
    })
    if not nombres:
        for f in compuestos:
            f["disponibilidad_estimada"] = None
        return

    stocks = query(
        "SELECT Producto AS producto, SUM(Stock_Lote) AS stock FROM lotes "
        "WHERE tenant_id = %s AND Estado = 'Activo' AND Producto = ANY(%s) "
        "GROUP BY Producto",
        (tenant_id, nombres)
    )
    stock_map = {r["producto"]: float(r["stock"] or 0) for r in stocks}

    for f in compuestos:
        recetas = recetas_por_compuesto.get(f["producto"]) or []
        if not recetas:
            f["disponibilidad_estimada"] = None
            continue
        mejor: int | None = None
        for receta in recetas:
            disp_receta: int | None = None
            for mat, cant in receta.items():
                if cant <= 0:
                    continue
                n = int(stock_map.get(mat, 0) // cant)
                disp_receta = n if disp_receta is None else min(disp_receta, n)
            if disp_receta is not None:
                mejor = disp_receta if mejor is None else min(mejor, disp_receta)
        f["disponibilidad_estimada"] = max(mejor, 0) if mejor is not None else None


def get_productos_meta(tenant_id: str) -> list[dict]:
    """Lee la tabla productos (metadatos) con sus categorías desde la relación Many-to-Many."""
    cat_subquery = _obtener_categorias_subquery("productos")
    filas = query(f"""
        SELECT 
            Producto as producto, 
            Descripcion as descripcion, 
            Imagen as imagen, 
            Estado as estado,
            codigo_interno,
            codigo_barras,
            ubicacion,
            visible_en_catalogo,
            sufijo_precio,
            tipo_producto,
            costo_servicio,
            precio_servicio,
            stock_por_variacion,
            {cat_subquery}
        FROM productos 
        WHERE Tenant_ID = %s
        ORDER BY Producto ASC
    """,  (tenant_id,))
    _adjuntar_variaciones(filas, tenant_id)
    _adjuntar_recetas(filas, tenant_id)
    return filas


def get_inventario_consolidado(tenant_id: str) -> list[dict]:
    """
    Devuelve una fila por producto con stock total,
    precio del lote más reciente, costo promedio ponderado y categorías.

    El precio sugerido (precio_sugerido) se calcula según el modo configurado
    del tenant (tenants.modo_precio_sugerido):
      'antiguo'  → lote más antiguo con stock (coincide con PEPS)
      'maximo'   → máximo precio entre los lotes con stock
      'reciente' → lote más reciente con stock
    """
    # Leer el modo del tenant (fallback: 'antiguo')
    modo = "antiguo"
    try:
        fila_modo = query("SELECT modo_precio_sugerido FROM tenants WHERE id = %s", (tenant_id,))
        if fila_modo and fila_modo[0].get("modo_precio_sugerido"):
            modo = fila_modo[0]["modo_precio_sugerido"]
    except Exception:
        pass

    cat_subquery = _obtener_categorias_subquery("p")
    # Parte de `productos` (LEFT JOIN lotes) para que los SERVICIOS —que no
    # tienen lotes— también aparezcan con stock 0 y su precio de servicio.
    filas = query(f"""
        SELECT
            p.Producto                                               AS producto,
            p.Descripcion                                            AS descripcion,
            p.Imagen                                                 AS imagen,
            p.Estado                                                 AS estado,
            p.codigo_interno,
            p.codigo_barras,
            p.ubicacion,
            p.visible_en_catalogo                                    AS visible_en_catalogo,
            p.sufijo_precio                                          AS sufijo_precio,
            p.tipo_producto                                          AS tipo_producto,
            p.costo_servicio                                         AS costo_servicio,
            p.precio_servicio                                        AS precio_servicio,
            p.stock_por_variacion                                    AS stock_por_variacion,
            {cat_subquery},
            COALESCE(SUM(l.Stock_Lote), 0)                           AS stock_total,
            COALESCE(MAX(l.Precio_Venta), p.precio_servicio, 0)      AS precio_venta,
            -- Precio del lote MÁS ANTIGUO con stock > 0 (el que PEPS va a vender).
            -- Si ningún lote tiene stock, cae al precio máximo (fallback).
            -- Las 3 variantes del precio sugerido; el backend elige según el modo del tenant
            COALESCE((array_agg(l.Precio_Venta ORDER BY l.Fecha_Entrada ASC) FILTER (WHERE l.Stock_Lote > 0))[1], MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_sug_antiguo,
            COALESCE((array_agg(l.Precio_Venta ORDER BY l.Fecha_Entrada DESC) FILTER (WHERE l.Stock_Lote > 0))[1], MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_sug_reciente,
            COALESCE(MAX(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_sug_maximo,
            -- Rango de precios de los lotes CON stock (para la tarjeta del POS)
            COALESCE(MIN(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_min,
            COALESCE(MAX(CASE WHEN l.Stock_Lote > 0 THEN l.Precio_Venta END), MAX(l.Precio_Venta), p.precio_servicio, 0) AS precio_max,
            COALESCE(SUM(l.Costo * l.Stock_Lote) / NULLIF(SUM(l.Stock_Lote), 0), p.costo_servicio, 0) AS costo_promedio
        FROM productos p
        LEFT JOIN lotes l ON l.Producto = p.Producto AND l.Tenant_ID = p.Tenant_ID AND l.Estado = 'Activo'
        WHERE p.Tenant_ID = %s AND p.Estado = 'Activo'
        GROUP BY p.Producto, p.Descripcion, p.Imagen, p.Estado, p.id, p.codigo_interno, p.codigo_barras, p.ubicacion, p.visible_en_catalogo, p.sufijo_precio, p.tipo_producto, p.costo_servicio, p.precio_servicio, p.stock_por_variacion
        ORDER BY p.Producto ASC
    """, (tenant_id,))

    # Adjuntar variaciones (POS/catálogo) y recetas (gestor) por producto
    _adjuntar_variaciones(filas, tenant_id)
    _adjuntar_recetas(filas, tenant_id)
    _calcular_disponibilidad_compuestos(filas, tenant_id)

    # Elegir el precio sugerido según el modo del tenant y limpiar las variantes
    for f in filas:
        # Servicio/Compuesto: no tienen lotes → el precio es su precio propio
        # (precio_servicio se reutiliza como 'precio sin stock' para ambos tipos)
        if f.get("tipo_producto") in ("servicio", "compuesto"):
            f["precio_sugerido"] = f.get("precio_servicio") or 0
            f["precio_venta"] = f.get("precio_servicio") or 0
            f["precio_min"] = f.get("precio_servicio") or 0
            f["precio_max"] = f.get("precio_servicio") or 0
            f["costo_promedio"] = f.get("costo_servicio") or 0
        elif modo == "maximo":
            f["precio_sugerido"] = f["precio_sug_maximo"]
        elif modo == "reciente":
            f["precio_sugerido"] = f["precio_sug_reciente"]
        else:
            f["precio_sugerido"] = f["precio_sug_antiguo"]
        f.pop("precio_sug_antiguo", None)
        f.pop("precio_sug_reciente", None)
        f.pop("precio_sug_maximo", None)
    return filas


def get_detalle_lotes(producto: str, tenant_id: str) -> list[dict]:
    """Devuelve los lotes activos de un producto específico, con la variación
    a la que pertenece cada lote (None = stock base del producto)."""
    return query("""
        SELECT 
            l.id, l.id_lote, l.producto, l.costo, l.precio_venta, l.stock_lote,
            l.fecha_entrada, l.estado, l.etiqueta,
            l.variacion_id, v.nombre AS variacion
        FROM lotes l
        LEFT JOIN producto_variaciones v ON v.id = l.variacion_id
        WHERE l.Producto = %s AND l.Estado = 'Activo' AND l.tenant_id = %s
        ORDER BY l.Fecha_Entrada DESC
    """, (producto, tenant_id))


def agregar_lote(
    producto: str,
    descripcion: str,
    costo: float,
    precio_venta: float,
    stock: float,
    imagen: str = "No hay foto",
    categoria: list[str] = ["General"],
    tenant_id: str = "",
    codigo_interno: str | None = None,
    codigo_barras: str | None = None,
    ubicacion: str | None = None,
    etiqueta: str = "",
    sufijo_precio: str = "",
    tipo_producto: str = "stock",
    costo_servicio: float | None = None,
    precio_servicio: float | None = None,
    variacion: str = ""
) -> dict:
    """
    Crea producto si no existe, luego inserta un lote nuevo
    o suma stock si ya existe uno con el mismo costo y precio.
    Las categorías se guardan en la tabla pivote producto_categorias.

    Si tipo_producto == 'servicio' NO se crea lote: el producto se vende sin
    inventario (ej. corte de cabello) y guarda costo/precio propios en
    productos.costo_servicio / productos.precio_servicio.
    Si tipo_producto == 'compuesto' tampoco se crea lote: su stock son los
    MATERIALES de su receta (producto_recetas). Su precio de venta se guarda
    en productos.precio_servicio (reutilizando la columna).
    """
    producto = producto.strip()
    descripcion = descripcion.strip()
    etiqueta_limpia = (etiqueta or "").strip()
    # Upsert en productos (YA NO incluye Categoria, se maneja aparte)
    sufijo_limpio = (sufijo_precio or "").strip()
    tipo = (tipo_producto or "stock").strip().lower()
    if tipo not in ("stock", "servicio", "compuesto"):
        tipo = "stock"
    costo_srv = float(costo_servicio or 0)
    precio_srv = float(precio_servicio or 0)
    result = query("""
        INSERT INTO productos (Producto, Descripcion, Imagen, Estado, tenant_id,
                               codigo_interno, codigo_barras, ubicacion, sufijo_precio,
                               tipo_producto, costo_servicio, precio_servicio)
        VALUES (%s, %s, %s, 'Activo', %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(Producto, tenant_id) DO UPDATE SET
            Descripcion = EXCLUDED.Descripcion,
            Imagen = CASE WHEN EXCLUDED.Imagen != 'No hay foto'
                         THEN EXCLUDED.Imagen ELSE productos.Imagen END,
            codigo_interno = COALESCE(EXCLUDED.codigo_interno, productos.codigo_interno),
            codigo_barras = COALESCE(EXCLUDED.codigo_barras, productos.codigo_barras),
            ubicacion = COALESCE(EXCLUDED.ubicacion, productos.ubicacion),
            sufijo_precio = CASE WHEN EXCLUDED.sufijo_precio != ''
                                 THEN EXCLUDED.sufijo_precio ELSE productos.sufijo_precio END,
            -- El tipo NO se pisa en restock: se define al crear el producto
            tipo_producto = productos.tipo_producto,
            costo_servicio = CASE WHEN EXCLUDED.costo_servicio > 0
                                  THEN EXCLUDED.costo_servicio ELSE productos.costo_servicio END,
            precio_servicio = CASE WHEN EXCLUDED.precio_servicio > 0
                                   THEN EXCLUDED.precio_servicio ELSE productos.precio_servicio END
        RETURNING id
    """, (producto, descripcion, imagen, tenant_id, codigo_interno, codigo_barras, ubicacion,
          sufijo_limpio, tipo, costo_srv, precio_srv))

    product_id = result[0]["id"]

    # Sincronizar categorías en la tabla pivote (Many-to-Many)
    _sincronizar_categorias(product_id, categoria, tenant_id)

    # ── Servicio/Compuesto: no tienen inventario propio → no se crea lote ──
    if tipo == "servicio":
        return {"accion": "servicio_creado", "producto": producto}
    if tipo == "compuesto":
        return {"accion": "compuesto_creado", "producto": producto}

    # ── Stock por variación (Fase 6) ──
    # Si el producto maneja stock por variación (flag ON), el lote DEBE
    # pertenecer a una variación concreta. Si el flag está OFF, no se permite
    # crear lotes ligados a una variación (evita stock huérfano).
    vnombre = (variacion or "").strip()
    variacion_id = None
    flag_row = query(
        "SELECT stock_por_variacion FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    flag_on = bool(flag_row and flag_row[0].get("stock_por_variacion"))
    if flag_on:
        if not vnombre:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": "Este producto maneja stock por variación: indica a cuál variación llegó el stock."}
        var_row = query("""
            SELECT v.id FROM producto_variaciones v
            JOIN productos p ON p.id = v.producto_id
            WHERE p.Producto = %s AND v.nombre = %s AND v.tenant_id = %s
        """, (producto, vnombre, tenant_id))
        if not var_row:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": f"La variación '{vnombre}' no existe para este producto."}
        variacion_id = var_row[0]["id"]
    elif vnombre:
        return {"ok": False, "tipo": "validacion",
                "mensaje": "Este producto no maneja stock por variación. Actívalo en Editar producto para restockear por variación."}

    # Buscar lote existente con mismo costo, precio y VARIACIÓN.
    # Si la etiqueta nueva está vacía, se fusiona con cualquiera (comportamiento
    # actual); si trae etiqueta, solo se fusiona con un lote de la MISMA
    # presentación — si no hay match, se crea un lote nuevo con su etiqueta
    # (evita que la etiqueta se pierda al restockear al mismo precio).
    existente = query("""
        SELECT id_lote FROM lotes
        WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id = %s
          AND (%s = '' OR COALESCE(etiqueta, '') = %s)
          AND variacion_id IS NOT DISTINCT FROM %s
        LIMIT 1
    """, (producto, costo, precio_venta, tenant_id, etiqueta_limpia, etiqueta_limpia, variacion_id))

    if existente:
        execute(
            "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id = %s",
            (stock, existente[0]["id_lote"], tenant_id)
        )
        return {"accion": "stock_sumado", "producto": producto, "cantidad": stock}
    else:
        id_lote = str(uuid.uuid4())[:12]
        fecha   = str(datetime.datetime.now(_TZ))
        execute("""
            INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta,
                               Stock_Lote, Fecha_Entrada, Estado, tenant_id, etiqueta, variacion_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s)
        """, (id_lote, producto, costo, precio_venta, stock, fecha, tenant_id, etiqueta_limpia, variacion_id))
        return {"accion": "lote_creado", "producto": producto, "id_lote": id_lote}


def crear_producto_completo(
    producto: str,
    descripcion: str,
    costo: float,
    precio_venta: float,
    stock: float,
    imagen: str = "No hay foto",
    categoria: list[str] | None = None,
    tenant_id: str = "",
    codigo_interno: str | None = None,
    codigo_barras: str | None = None,
    ubicacion: str | None = None,
    etiqueta: str = "",
    sufijo_precio: str = "",
    tipo_producto: str = "stock",
    costo_servicio: float | None = None,
    precio_servicio: float | None = None,
    visible_en_catalogo: bool | None = None,
    variaciones: list[dict] | None = None,   # [{nombre, precio, stock_inicial?, costo?}] — aplica a cualquier tipo
    recetas: list[dict] | None = None,       # [{material, cantidad}] — solo compuestos
) -> dict:
    """
    Crea un producto con TODO en una sola transacción (todo-o-nada):
      - el producto (con su visibilidad en catálogo, si se indica),
      - su primer lote (solo tipo 'stock', y solo si NINGUNA variación trae
        stock inicial — si alguna variación trae stock, se crean lotes POR
        VARIACIÓN y el producto queda con stock_por_variacion=true),
      - sus VARIACIONES ({nombre, precio, stock_inicial?, costo?}) si vienen,
      - su RECETA ({material, cantidad}) si es compuesto.

    Si cualquier paso falla (variación duplicada, material inexistente, etc.)
    se hace rollback: no queda el producto a medias.

    visible_en_catalogo:
      True/False → aplica SOLO cuando el producto es NUEVO (INSERT). Si el
      producto ya existía (ON CONFLICT, caso raro en el alta), se conserva la
      visibilidad actual para no pisarla.
    """
    from database.conexion import get_conn, release_conn

    producto = (producto or "").strip()
    descripcion = (descripcion or "").strip()
    etiqueta_limpia = (etiqueta or "").strip()
    sufijo_limpio = (sufijo_precio or "").strip()
    tipo = (tipo_producto or "stock").strip().lower()
    if tipo not in ("stock", "servicio", "compuesto"):
        tipo = "stock"
    costo_srv = float(costo_servicio or 0)
    precio_srv = float(precio_servicio or 0)
    vis = bool(visible_en_catalogo) if visible_en_catalogo is not None else True

    conn = get_conn()
    try:
        # 1. Upsert del producto. En conflicto (producto ya existente) se
        #    conserva la visibilidad actual; el tipo nunca se pisa.
        r = _q(conn, """
            INSERT INTO productos (Producto, Descripcion, Imagen, Estado, tenant_id,
                                   codigo_interno, codigo_barras, ubicacion, sufijo_precio,
                                   tipo_producto, costo_servicio, precio_servicio, visible_en_catalogo)
            VALUES (%s, %s, %s, 'Activo', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(Producto, tenant_id) DO UPDATE SET
                Descripcion = EXCLUDED.Descripcion,
                Imagen = CASE WHEN EXCLUDED.Imagen != 'No hay foto'
                             THEN EXCLUDED.Imagen ELSE productos.Imagen END,
                codigo_interno = COALESCE(EXCLUDED.codigo_interno, productos.codigo_interno),
                codigo_barras = COALESCE(EXCLUDED.codigo_barras, productos.codigo_barras),
                ubicacion = COALESCE(EXCLUDED.ubicacion, productos.ubicacion),
                sufijo_precio = CASE WHEN EXCLUDED.sufijo_precio != ''
                                     THEN EXCLUDED.sufijo_precio ELSE productos.sufijo_precio END,
                tipo_producto = productos.tipo_producto,
                costo_servicio = CASE WHEN EXCLUDED.costo_servicio > 0
                                      THEN EXCLUDED.costo_servicio ELSE productos.costo_servicio END,
                precio_servicio = CASE WHEN EXCLUDED.precio_servicio > 0
                                       THEN EXCLUDED.precio_servicio ELSE productos.precio_servicio END,
                -- La visibilidad NO se pisa en restock/conflictos: solo aplica en INSERT
                visible_en_catalogo = productos.visible_en_catalogo
            RETURNING id
        """, (producto, descripcion, imagen, tenant_id, codigo_interno, codigo_barras, ubicacion,
              sufijo_limpio, tipo, costo_srv, precio_srv, vis))
        product_id = r[0]["id"]

        # 2. Categorías (dentro de la misma transacción)
        _sincronizar_categorias(product_id, categoria or ["General"], tenant_id, conn=conn)

        # ¿Alguna variación trae stock inicial? Si sí, el producto pasa a
        # manejar stock por variación y NO se crea el lote base con `stock`.
        variaciones_list = variaciones or []
        con_stock_variacion = any(
            float((v.get("stock_inicial") or 0)) > 0
            for v in variaciones_list if (v.get("nombre") or "").strip()
        )

        # 3. Lote inicial (solo tipo 'stock' y sin stock por variación)
        accion = "lote_creado"
        if tipo == "stock" and not con_stock_variacion:
            existente = _q(conn, """
                SELECT id_lote FROM lotes
                WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id = %s
                  AND (%s = '' OR COALESCE(etiqueta, '') = %s)
                LIMIT 1
            """, (producto, costo, precio_venta, tenant_id, etiqueta_limpia, etiqueta_limpia))
            if existente:
                _e(conn,
                    "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id = %s",
                    (stock, existente[0]["id_lote"], tenant_id))
                accion = "stock_sumado"
            else:
                id_lote = str(uuid.uuid4())[:12]
                fecha = str(datetime.datetime.now(_TZ))
                _e(conn, """
                    INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta,
                                       Stock_Lote, Fecha_Entrada, Estado, tenant_id, etiqueta)
                    VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s, %s)
                """, (id_lote, producto, costo, precio_venta, stock, fecha, tenant_id, etiqueta_limpia))
        elif tipo == "servicio":
            accion = "servicio_creado"
        elif tipo == "compuesto":
            accion = "compuesto_creado"

        # 4. Variaciones (aplica a cualquier tipo). Si traen stock_inicial y el
        #    tipo es stock, se crea el LOTE de esa variación (costo propio
        #    opcional, si no se usa el costo del producto) y se activa el flag
        #    stock_por_variacion.
        for v in variaciones_list:
            vnombre = (v.get("nombre") or "").strip()
            if not vnombre:
                continue
            vprecio = float(v.get("precio") or 0)
            try:
                r_var = _q(conn,
                    "INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (product_id, vnombre, vprecio, tenant_id))
            except UniqueViolation:
                conn.rollback()
                return {"ok": False, "tipo": "validacion",
                        "mensaje": f"Ya existe una variación llamada '{vnombre}'"}
            vstock = v.get("stock_inicial")
            if tipo == "stock" and float(vstock or 0) > 0:
                vcosto = float(v.get("costo") or 0) or costo
                vid = r_var[0]["id"]
                vfecha = str(datetime.datetime.now(_TZ))
                _e(conn, """
                    INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta,
                                       Stock_Lote, Fecha_Entrada, Estado, tenant_id, variacion_id)
                    VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s, %s)
                """, (str(uuid.uuid4())[:12], producto, vcosto, vprecio,
                       float(vstock), vfecha, tenant_id, vid))
        if con_stock_variacion:
            _e(conn,
                "UPDATE productos SET stock_por_variacion=true WHERE id=%s",
                (product_id,))

        # 5. Receta (solo compuestos)
        if tipo == "compuesto":
            for r_mat in (recetas or []):
                mnombre = (r_mat.get("material") or "").strip()
                mcant = float(r_mat.get("cantidad") or 0)
                if not mnombre or mcant <= 0:
                    continue
                fila_mat = _q(conn,
                    "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
                    (mnombre, tenant_id))
                if not fila_mat:
                    conn.rollback()
                    return {"ok": False, "tipo": "validacion",
                            "mensaje": f"El material '{mnombre}' no existe. Créalo primero como producto con stock."}
                mid = fila_mat[0]["id"]
                if mid == product_id:
                    conn.rollback()
                    return {"ok": False, "tipo": "validacion",
                            "mensaje": f"'{mnombre}' no puede ser material de sí mismo."}
                try:
                    _e(conn,
                        "INSERT INTO producto_recetas (producto_id, material_id, cantidad, tenant_id, variacion_id) "
                        "VALUES (%s, %s, %s, %s, NULL)",
                        (product_id, mid, mcant, tenant_id))
                except UniqueViolation:
                    # Ya existe en la receta base → actualizar la cantidad
                    _e(conn,
                        "UPDATE producto_recetas SET cantidad = %s "
                        "WHERE producto_id = %s AND material_id = %s AND tenant_id = %s "
                        "AND variacion_id IS NULL",
                        (mcant, product_id, mid, tenant_id))

        conn.commit()
        return {
            "ok": True,
            "accion": accion,
            "producto": producto,
            "id": product_id,
            "variaciones_creadas": len(variaciones or []),
            "recetas_creadas": len(recetas or []) if tipo == "compuesto" else 0,
        }
    except Exception as e:
        conn.rollback()
        return {"ok": False, "tipo": "error", "mensaje": f"Error al crear el producto: {str(e)}"}
    finally:
        release_conn(conn)


def actualizar_producto(
    producto: str,
    descripcion: str,
    imagen: str,
    estado: str,
    categoria: list[str],
    costo: float | None = None,
    precio_venta: float | None = None,
    tenant_id: str = "",
    nuevo_producto: str | None = None,
    codigo_interno: str | None = None,
    codigo_barras: str | None = None,
    ubicacion: str | None = None,
    visible_en_catalogo: bool | None = None,
    sufijo_precio: str | None = None,
    tipo_producto: str | None = None,
    costo_servicio: float | None = None,
    precio_servicio: float | None = None,
    stock_por_variacion: bool | None = None
) -> dict:
    producto = producto.strip()
    descripcion = descripcion.strip()
    """
    Actualiza metadatos de un producto y sus categorías (Many-to-Many).
    Si pasa a Inactivo, desactiva todos sus lotes.
    Si se proporcionan costo y/o precio_venta, actualiza todos los lotes activos.
    Si se proporciona nuevo_producto, renombra el producto en todas las tablas.
    También actualiza codigo_interno, codigo_barras y ubicacion si se proporcionan.
    Si se proporciona visible_en_catalogo, actualiza la visibilidad en el catálogo público.
    tipo_producto/costo_servicio/precio_servicio aplican a productos de servicio (sin stock).
    """
    nombre_final = producto
    if nuevo_producto is not None:
        nombre_final = nuevo_producto.strip()
        if nombre_final and nombre_final != producto:
            # Renombrar en la tabla productos
            execute(
                "UPDATE productos SET Producto=%s, Descripcion=%s, Imagen=%s, Estado=%s, "
                "codigo_interno=COALESCE(%s, codigo_interno), "
                "codigo_barras=COALESCE(%s, codigo_barras), "
                "ubicacion=COALESCE(%s, ubicacion), "
                "visible_en_catalogo=COALESCE(%s, visible_en_catalogo), "
                "sufijo_precio=COALESCE(%s, sufijo_precio), "
                "tipo_producto=COALESCE(%s, tipo_producto), "
                "costo_servicio=COALESCE(%s, costo_servicio), "
                "precio_servicio=COALESCE(%s, precio_servicio) "
                "WHERE Producto=%s AND tenant_id=%s",
                (nombre_final, descripcion, imagen, estado,
                 codigo_interno, codigo_barras, ubicacion, visible_en_catalogo, sufijo_precio,
                 tipo_producto, costo_servicio, precio_servicio, producto, tenant_id)
            )
            # Renombrar en lotes
            execute(
                "UPDATE lotes SET Producto=%s WHERE Producto=%s AND tenant_id=%s",
                (nombre_final, producto, tenant_id)
            )
            # Renombrar en ventas
            execute(
                "UPDATE ventas SET Producto=%s WHERE Producto=%s AND tenant_id=%s",
                (nombre_final, producto, tenant_id)
            )
            # Usar el nuevo nombre para el resto de operaciones
            producto = nombre_final
        else:
            # Si nuevo_producto está vacío o es igual, solo actualizar normal
            execute("""
                UPDATE productos SET Descripcion=%s, Imagen=%s, Estado=%s,
                    codigo_interno=COALESCE(%s, codigo_interno),
                    codigo_barras=COALESCE(%s, codigo_barras),
                    ubicacion=COALESCE(%s, ubicacion),
                    visible_en_catalogo=COALESCE(%s, visible_en_catalogo),
                    sufijo_precio=COALESCE(%s, sufijo_precio),
                    tipo_producto=COALESCE(%s, tipo_producto),
                    costo_servicio=COALESCE(%s, costo_servicio),
                    precio_servicio=COALESCE(%s, precio_servicio)
                WHERE Producto=%s AND tenant_id = %s
            """, (descripcion, imagen, estado, codigo_interno, codigo_barras, ubicacion, visible_en_catalogo, sufijo_precio,
                   tipo_producto, costo_servicio, precio_servicio, producto, tenant_id))
    else:
        # Actualizar producto sin renombrar
        execute("""
            UPDATE productos SET Descripcion=%s, Imagen=%s, Estado=%s,
                codigo_interno=COALESCE(%s, codigo_interno),
                codigo_barras=COALESCE(%s, codigo_barras),
                ubicacion=COALESCE(%s, ubicacion),
                visible_en_catalogo=COALESCE(%s, visible_en_catalogo),
                sufijo_precio=COALESCE(%s, sufijo_precio),
                tipo_producto=COALESCE(%s, tipo_producto),
                costo_servicio=COALESCE(%s, costo_servicio),
                precio_servicio=COALESCE(%s, precio_servicio)
            WHERE Producto=%s AND tenant_id = %s
        """, (descripcion, imagen, estado, codigo_interno, codigo_barras, ubicacion, visible_en_catalogo, sufijo_precio,
               tipo_producto, costo_servicio, precio_servicio, producto, tenant_id))

    # Obtener el ID numérico del producto para la tabla pivote
    prod = query(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    if prod:
        # Sincronizar categorías en la tabla pivote
        _sincronizar_categorias(prod[0]["id"], categoria, tenant_id)

    # Actualizar costo y/o precio de venta en todos los lotes activos
    if costo is not None:
        execute(
            "UPDATE lotes SET Costo=%s WHERE Producto=%s AND Estado='Activo' AND tenant_id=%s",
            (costo, producto, tenant_id)
        )
    if precio_venta is not None:
        execute(
            "UPDATE lotes SET Precio_Venta=%s WHERE Producto=%s AND Estado='Activo' AND tenant_id=%s",
            (precio_venta, producto, tenant_id)
        )

    if estado == "Inactivo":
        execute(
            "UPDATE lotes SET Estado='Inactivo' WHERE Producto=%s AND tenant_id = %s",
            (producto, tenant_id)
        )

    # Stock por variación (Fase 6): toggle por producto
    if stock_por_variacion is not None:
        execute(
            "UPDATE productos SET stock_por_variacion=%s WHERE Producto=%s AND tenant_id=%s",
            (bool(stock_por_variacion), producto, tenant_id)
        )
    return {"ok": True, "producto": producto, "estado": estado}


def listar_categorias(tenant_id: str) -> list[dict]:
    """
    Devuelve todas las categorías del tenant con el conteo de productos asociados.
    Útil para mostrar en la gestión de categorías del frontend.
    """
    return query("""
        SELECT
            c.id,
            c.nombre,
            c.slug,
            c.visible_en_catalogo,
            COUNT(pc.producto_id) AS total_productos
        FROM categorias c
        LEFT JOIN producto_categorias pc ON pc.categoria_id = c.id
        WHERE c.tenant_id = %s
        GROUP BY c.id, c.nombre, c.slug, c.visible_en_catalogo
        ORDER BY c.nombre ASC
    """, (tenant_id,))


def crear_categoria(nombre: str, tenant_id: str) -> dict:
    """
    Crea una categoría nueva para el tenant.
    Si ya existe, retorna la existente.
    """
    nombre = nombre.strip()
    slug = _slugify(nombre)

    result = query("""
        INSERT INTO categorias (tenant_id, nombre, slug)
        VALUES (%s, %s, %s)
        ON CONFLICT (tenant_id, nombre) DO NOTHING
        RETURNING id, nombre, slug
    """, (tenant_id, nombre, slug))

    if not result:
        # Si no se insertó es porque ya existe, la recuperamos
        existente = query(
            "SELECT id, nombre, slug FROM categorias WHERE nombre = %s AND tenant_id = %s",
            (nombre, tenant_id)
        )
        if existente:
            return {"ok": True, "categoria": existente[0], "mensaje": f"La categoría '{nombre}' ya existía"}
        return {"ok": False, "mensaje": "Error al crear la categoría"}

    return {"ok": True, "categoria": result[0], "mensaje": f"Categoría '{nombre}' creada"}


def renombrar_categoria(viejo_nombre: str, nuevo_nombre: str, tenant_id: str) -> dict:
    """
    Cambia el nombre de una categoría y actualiza su slug.
    Retorna la categoría actualizada o un error si no existe.
    """
    viejo_nombre = viejo_nombre.strip()
    nuevo_nombre = nuevo_nombre.strip()
    nuevo_slug = _slugify(nuevo_nombre)

    result = query("""
        UPDATE categorias
        SET nombre = %s, slug = %s
        WHERE nombre = %s AND tenant_id = %s
        RETURNING id, nombre, slug
    """, (nuevo_nombre, nuevo_slug, viejo_nombre, tenant_id))

    if not result:
        return {"ok": False, "mensaje": f"Categoría '{viejo_nombre}' no encontrada"}

    return {
        "ok": True,
        "categoria": result[0]
    }


def eliminar_categoria_de_productos(categoria: str, tenant_id: str) -> dict:
    """
    Elimina una categoría de todos los productos del tenant.
    Busca la categoría por nombre en la tabla 'categorias',
    elimina todas las relaciones en 'producto_categorias' y
    luego elimina la categoría misma.
    """
    categoria = categoria.strip()

    # Buscar la categoría por nombre y tenant
    result = query(
        "SELECT id FROM categorias WHERE nombre = %s AND tenant_id = %s",
        (categoria, tenant_id)
    )

    if not result:
        return {
            "ok": True,
            "categoria_eliminada": categoria,
            "productos_actualizados": 0
        }

    cat_id = result[0]["id"]

    # Contar cuántos productos tenían esta categoría
    affected = query(
        "SELECT COUNT(*) as count FROM producto_categorias WHERE categoria_id = %s",
        (cat_id,)
    )
    actualizados = affected[0]["count"] if affected else 0

    # Eliminar relaciones en la tabla pivote (CASCADE también lo haría,
    # pero hacemos DELETE explícito para claridad y control)
    execute(
        "DELETE FROM producto_categorias WHERE categoria_id = %s",
        (cat_id,)
    )

    # Eliminar la categoría de la tabla madre
    execute(
        "DELETE FROM categorias WHERE id = %s AND tenant_id = %s",
        (cat_id, tenant_id)
    )

    return {
        "ok": True,
        "categoria_eliminada": categoria,
        "productos_actualizados": actualizados
    }


def actualizar_lote(id_lote: str, costo: float, precio_venta: float, stock: float, tenant_id: str, etiqueta: str | None = None, variacion: str | None = None) -> dict:
    """
    Actualiza costo, precio de venta, stock y etiqueta de un lote específico.

    variacion: None = conservar la variación actual;
               ''    = desvincular el lote de su variación (pasa a base);
               'Nombre' = reasignar el lote a esa variación del mismo producto.
    """
    # 0. Reasignar/desvincular la variación del lote si se indica
    if variacion is not None:
        vn = (variacion or "").strip()
        info = query(
            "SELECT Producto FROM lotes WHERE ID_Lote=%s AND tenant_id=%s",
            (id_lote, tenant_id)
        )
        if not info:
            return {"ok": False, "mensaje": "Lote no encontrado"}
        if vn:
            pid = _resolver_producto_id(info[0]["producto"], tenant_id)
            var = query(
                "SELECT id FROM producto_variaciones WHERE producto_id=%s AND nombre=%s AND tenant_id=%s",
                (pid, vn, tenant_id)
            ) if pid else []
            if not var:
                return {"ok": False, "mensaje": f"La variación '{vn}' no existe para este producto"}
            execute(
                "UPDATE lotes SET variacion_id=%s WHERE ID_Lote=%s AND tenant_id=%s",
                (var[0]["id"], id_lote, tenant_id)
            )
        else:
            execute(
                "UPDATE lotes SET variacion_id=NULL WHERE ID_Lote=%s AND tenant_id=%s",
                (id_lote, tenant_id)
            )

    # 1. Actualizar el lote (etiqueta: COALESCE mantiene la actual si no se envía)
    execute("""
        UPDATE lotes SET Costo=%s, Precio_Venta=%s, Stock_Lote=%s, etiqueta=COALESCE(%s, etiqueta) WHERE ID_Lote=%s AND tenant_id = %s
    """, (costo, precio_venta, stock, etiqueta, id_lote, tenant_id))

    # 2. Recalcular ganancias en ventas asociadas a este lote
    execute("""
        UPDATE ventas 
        SET Costo_Unitario = %s,
            Ganancia_Bruta = (Precio_Real - %s) * Cantidad
        WHERE ID_Lote = %s AND Estado = 'Activo' AND tenant_id = %s
    """, (costo, costo, id_lote, tenant_id))

    return {"ok": True, "id_lote": id_lote}


def toggle_visibilidad_categoria(categoria: str, tenant_id: str) -> dict:
    """
    Alterna la visibilidad de una categoría en el catálogo público.
    Si estaba visible, la oculta y viceversa.
    """
    categoria = categoria.strip()

    # Obtener estado actual
    actual = query(
        "SELECT visible_en_catalogo FROM categorias WHERE nombre = %s AND tenant_id = %s",
        (categoria, tenant_id)
    )
    if not actual:
        return {"ok": False, "mensaje": f"Categoría '{categoria}' no encontrada"}

    nuevo_valor = not actual[0]["visible_en_catalogo"]
    execute(
        "UPDATE categorias SET visible_en_catalogo = %s WHERE nombre = %s AND tenant_id = %s",
        (nuevo_valor, categoria, tenant_id)
    )

    return {
        "ok": True,
        "categoria": categoria,
        "visible_en_catalogo": nuevo_valor,
        "mensaje": f"Categoría '{categoria}' {'visible' if nuevo_valor else 'oculta'} en el catálogo"
    }


def eliminar_lote(id_lote: str, tenant_id: str) -> dict:
    """
    Da de baja un lote específico: lo marca como Inactivo y pone su stock en 0.
    Si es el último lote activo del producto, también da de baja el producto.
    
    Returns:
        dict con:
        - ok: True si se eliminó correctamente
        - producto: nombre del producto afectado
        - producto_desactivado: True si el producto también fue desactivado
    """
    # Obtener información del lote antes de desactivarlo
    info = query(
        "SELECT Producto FROM lotes WHERE ID_Lote = %s AND tenant_id = %s",
        (id_lote, tenant_id)
    )
    if not info:
        return {"ok": False, "mensaje": "Lote no encontrado"}

    producto = info[0]["producto"]

    # Marcar el lote como Inactivo y stock en 0
    execute(
        "UPDATE lotes SET Estado = 'Inactivo', Stock_Lote = 0 WHERE ID_Lote = %s AND tenant_id = %s",
        (id_lote, tenant_id)
    )

    # Verificar si quedan lotes activos para este producto
    # (descontando el lote que acabamos de desactivar)
    activos_restantes = query(
        "SELECT COUNT(*) as total FROM lotes WHERE Producto = %s AND Estado = 'Activo' AND tenant_id = %s",
        (producto, tenant_id)
    )
    producto_desactivado = False

    if activos_restantes and activos_restantes[0]["total"] == 0:
        # No quedan lotes activos → desactivar el producto también
        execute(
            "UPDATE productos SET Estado = 'Inactivo' WHERE Producto = %s AND tenant_id = %s",
            (producto, tenant_id)
        )
        producto_desactivado = True

    return {
        "ok": True,
        "id_lote": id_lote,
        "producto": producto,
        "producto_desactivado": producto_desactivado
    }


def descontar_stock_peps(
    producto: str,
    cantidad_total: float,
    precio_real: float,
    tenant_id: str,
    id_lote: str | None = None,
    variacion_id: int | None = None,
    conn=None
) -> list[dict]:
    producto = producto.strip()
    """
    Algoritmo PEPS: descuenta del lote más antiguo primero.
    
    Si se proporciona id_lote, descuenta exclusivamente de ese lote específico
    en lugar de seguir el orden PEPS. Si el lote no tiene suficiente stock,
    se permite stock negativo (consistente con el comportamiento general).
    
    Si se proporciona variacion_id, descuenta SOLO de los lotes de esa
    variación (stock por variación, Fase 6). Si es None, descuenta solo de
    los lotes base del producto (variacion_id IS NULL) — el comportamiento
    estándar.
    
    A diferencia de la versión anterior, ahora PERMITE stock negativo.
    Si no hay suficiente stock físico, se consume todo lo disponible
    y el excedente se descuenta del lote más reciente, permitiendo
    que su Stock_Lote quede en negativo.
    Esto es útil para flujos de trabajo donde el usuario quiere
    cobrar aunque falte inventario, con la advertencia correspondiente.
    
    Retorna siempre una lista de registros de venta (nunca None).

    Si se pasa `conn`, todas las operaciones se ejecutan sobre esa conexión
    (para poder usarse dentro de una transacción atómica junto con el INSERT
    de las ventas). Si no se pasa, usa las conexiones del pool global.
    """
    # ── Si se especificó un lote concreto, descontar solo de ese lote ──
    if id_lote is not None:
        lotes = _q(conn, """
            SELECT * FROM lotes
            WHERE ID_Lote=%s AND Producto=%s AND Estado='Activo' AND tenant_id = %s
            FOR UPDATE
        """, (id_lote, producto, tenant_id))

        if not lotes:
            # Si no existe el lote, crear uno virtual con stock negativo
            # (comportamiento consistente con el PEPS normal)
            nuevo_id = str(uuid.uuid4())[:12]
            fecha = str(datetime.datetime.now(_TZ))
            _e(conn,
                "INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id, variacion_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s, %s)",
                (nuevo_id, producto, 0, precio_real, -cantidad_total, fecha, tenant_id, variacion_id)
            )
            return [{
                "fecha"          : fecha,
                "producto"       : producto,
                "cantidad"       : cantidad_total,
                "precio_lista"   : precio_real,
                "precio_real"    : precio_real,
                "costo_unitario" : 0,
                "total_venta"    : precio_real * cantidad_total,
                "ganancia_bruta" : precio_real * cantidad_total,
                "id_lote"        : nuevo_id
            }]

        lote = lotes[0]
        nuevo_stock = round(float(lote["stock_lote"]) - cantidad_total, 3)
        _e(conn,
            "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id = %s",
            (nuevo_stock, id_lote, tenant_id)
        )

        return [{
            "fecha"          : str(datetime.datetime.now(_TZ)),
            "producto"       : producto,
            "cantidad"       : cantidad_total,
            "precio_lista"   : float(lote["precio_venta"]),
            "precio_real"    : precio_real,
            "costo_unitario" : float(lote["costo"]),
            "total_venta"    : precio_real * cantidad_total,
            "ganancia_bruta" : (precio_real - float(lote["costo"])) * cantidad_total,
            "id_lote"        : lote["id_lote"]
        }]

    # ── PEPS normal (sin lote específico) ──
    # Obtenemos TODOS los lotes activos de esa variación (o base si no se
    # indica), incluso con stock 0 o negativo para seguir el orden PEPS.
    lotes = _q(conn, """
        SELECT * FROM lotes
        WHERE Producto=%s AND Estado='Activo' AND tenant_id = %s
          AND variacion_id IS NOT DISTINCT FROM %s
        ORDER BY Fecha_Entrada ASC
        FOR UPDATE
    """, (producto, tenant_id, variacion_id))

    ventas_generadas = []
    restante = cantidad_total

    # --- Primera pasada: consumir stock de lotes con inventario positivo ---
    for lote in lotes:
        if restante <= 0:
            break

        # Solo consumimos de lotes que tengan stock positivo
        if float(lote["stock_lote"]) <= 0:
            continue

        consumir    = min(restante, float(lote["stock_lote"]))
        nuevo_stock = round(float(lote["stock_lote"]) - consumir, 3)

        _e(conn,
            "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id = %s",
            (nuevo_stock, lote["id_lote"], tenant_id)
        )
        
        # Actualizar el valor en memoria para que la segunda pasada (negativos)
        # vea el stock correcto, no el valor original de la consulta.
        lote["stock_lote"] = nuevo_stock

        ventas_generadas.append({
            "fecha"          : str(datetime.datetime.now(_TZ)),
            "producto"       : producto,
            "cantidad"       : consumir,
            "precio_lista"   : float(lote["precio_venta"]),
            "precio_real"    : precio_real,
            "costo_unitario" : float(lote["costo"]),
            "total_venta"    : precio_real * consumir,
            "ganancia_bruta" : (precio_real - float(lote["costo"])) * consumir,
            "id_lote"        : lote["id_lote"]
        })
        restante -= consumir

    # --- Segunda pasada: si aún falta stock, lo descontamos del lote más reciente (stock negativo) ---
    if restante > 0:
        if lotes:
            # Usamos el lote más reciente (último del orden PEPS = último insertado)
            lote_destino = lotes[-1]
            nuevo_stock = round(float(lote_destino["stock_lote"]) - restante, 3)
            
            _e(conn,
                "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id = %s",
                (nuevo_stock, lote_destino["id_lote"], tenant_id)
            )
            
            ventas_generadas.append({
                "fecha"          : str(datetime.datetime.now(_TZ)),
                "producto"       : producto,
                "cantidad"       : restante,
                "precio_lista"   : float(lote_destino["precio_venta"]),
                "precio_real"    : precio_real,
                "costo_unitario" : float(lote_destino["costo"]),
                "total_venta"    : precio_real * restante,
                "ganancia_bruta" : (precio_real - float(lote_destino["costo"])) * restante,
                "id_lote"        : lote_destino["id_lote"]
            })
        else:
            # No existe ningún lote para este producto — creamos uno virtual con stock negativo
            nuevo_id_l = str(uuid.uuid4())[:12]
            fecha      = str(datetime.datetime.now(_TZ))
            
            _e(conn,
                "INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id, variacion_id) VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s, %s)",
                (nuevo_id_l, producto, 0, precio_real, -restante, fecha, tenant_id, variacion_id)
            )
            
            ventas_generadas.append({
                "fecha"          : fecha,
                "producto"       : producto,
                "cantidad"       : restante,
                "precio_lista"   : precio_real,
                "precio_real"    : precio_real,
                "costo_unitario" : 0,
                "total_venta"    : precio_real * restante,
                "ganancia_bruta" : precio_real * restante,
                "id_lote"        : nuevo_id_l
            })

    return ventas_generadas


# ==============================================================================
# Variaciones de producto (Fase 2)
# Una variación es una presentación con su PROPIO precio para un mismo
# producto (ej. Sencilla/Doble, S/M/L, Caballero/Dama). Aplica a cualquier
# tipo de producto (stock, servicio o compuesto). En esta fase no consume
# nada extra: solo cambia el precio.
# ==============================================================================


def _resolver_producto_id(producto: str, tenant_id: str) -> int | None:
    """Resuelve el ID numérico de un producto por su nombre (o None)."""
    r = query(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    return r[0]["id"] if r else None


def listar_variaciones_producto(producto: str, tenant_id: str) -> list[dict]:
    """Variaciones de un producto concreto (nombre + precio propio + foto)."""
    pid = _resolver_producto_id(producto, tenant_id)
    if not pid:
        return []
    return query("""
        SELECT v.id, v.nombre, v.precio, v.foto,
               COALESCE((SELECT SUM(l.Stock_Lote) FROM lotes l
                         WHERE l.variacion_id = v.id AND l.Estado='Activo'
                           AND l.tenant_id = v.tenant_id), 0) AS stock
        FROM producto_variaciones v
        WHERE v.producto_id = %s AND v.tenant_id = %s
        ORDER BY v.nombre ASC
    """, (pid, tenant_id))


def crear_variacion(producto: str, nombre: str, precio: float, tenant_id: str, foto: str = "") -> dict:
    """
    Crea una variación nueva para un producto.
    Si ya existe una variación con el mismo nombre, devuelve error (UNIQUE).
    """
    producto = (producto or "").strip()
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "mensaje": "El nombre de la variación es obligatorio"}
    pid = _resolver_producto_id(producto, tenant_id)
    if not pid:
        return {"ok": False, "mensaje": "Producto no encontrado"}
    try:
        result = query("""
            INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id, foto)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, nombre, precio, foto
        """, (pid, nombre, float(precio or 0), tenant_id, foto or ""))
    except UniqueViolation:
        return {"ok": False, "mensaje": "Ya existe una variación con ese nombre"}
    if not result:
        return {"ok": False, "mensaje": "Ya existe una variación con ese nombre"}
    v = result[0]
    return {"ok": True, "variacion": {"id": v["id"], "nombre": v["nombre"], "precio": float(v["precio"] or 0), "foto": v.get("foto") or ""}}


def actualizar_variacion(variacion_id: int, nombre: str, precio: float, tenant_id: str, foto: str | None = None) -> dict:
    """Actualiza nombre y/o precio de una variación (validando pertenencia del tenant).
    foto: None = conservar la actual; '' = quitar la foto."""
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "mensaje": "El nombre de la variación es obligatorio"}
    try:
        result = query("""
            UPDATE producto_variaciones
            SET nombre = %s, precio = %s, foto = COALESCE(%s, foto)
            WHERE id = %s AND tenant_id = %s
            RETURNING id, nombre, precio, foto
        """, (nombre, float(precio or 0), foto, variacion_id, tenant_id))
    except UniqueViolation:
        return {"ok": False, "mensaje": "Ya existe una variación con ese nombre"}
    if not result:
        return {"ok": False, "mensaje": "Variación no encontrada"}
    v = result[0]
    return {"ok": True, "variacion": {"id": v["id"], "nombre": v["nombre"], "precio": float(v["precio"] or 0), "foto": v.get("foto") or ""}}


def eliminar_variacion(variacion_id: int, tenant_id: str) -> dict:
    """Elimina una variación (validando pertenencia del tenant)."""
    result = query("""
        DELETE FROM producto_variaciones
        WHERE id = %s AND tenant_id = %s
        RETURNING id
    """, (variacion_id, tenant_id))
    if not result:
        return {"ok": False, "mensaje": "Variación no encontrada"}
    return {"ok": True, "id": variacion_id}


# ==============================================================================
# Recetas de productos compuestos (Fase 3)
# Un compuesto (ej. hamburguesa) no tiene stock propio: al venderlo se
# descuentan sus MATERIALES según la receta (BOM). Referencias por ID.
# ==============================================================================


def _adjuntar_recetas(filas: list[dict], tenant_id: str) -> None:
    """
    Adjunta las recetas de cada producto compuesto a una lista de filas que ya
    contienen la llave `producto` (nombre). Muta las filas in-place añadiendo
    `recetas` = [{id, material, cantidad, variacion_id}].

    variacion_id: None = receta base; si no, receta de ESA variación.
    Los que no son compuestos quedan con lista vacía.
    """
    if not filas:
        return
    recetas = query("""
        SELECT c.Producto AS compuesto, r.id, m.Producto AS material, r.cantidad,
               r.variacion_id, v.nombre AS variacion
        FROM producto_recetas r
        JOIN productos c ON c.id = r.producto_id
        JOIN productos m ON m.id = r.material_id
        LEFT JOIN producto_variaciones v ON v.id = r.variacion_id
        WHERE c.tenant_id = %s
        ORDER BY c.Producto ASC, v.nombre ASC NULLS FIRST, m.Producto ASC
    """, (tenant_id,))
    por_producto: dict = {}
    for r in recetas:
        por_producto.setdefault(r["compuesto"], []).append({
            "id": r["id"],
            "material": r["material"],
            "cantidad": float(r["cantidad"] or 0),
            "variacion_id": r.get("variacion_id"),
            "variacion": r.get("variacion"),
        })
    for f in filas:
        f["recetas"] = por_producto.get(f["producto"], [])


def listar_recetas_producto(producto: str, tenant_id: str) -> list[dict]:
    """Materiales de un compuesto (nombre + cantidad por unidad), con la
    variación a la que pertenece cada receta (None = receta base)."""
    pid = _resolver_producto_id(producto, tenant_id)
    if not pid:
        return []
    return query("""
        SELECT r.id, m.Producto AS material, r.cantidad, r.variacion_id,
               v.nombre AS variacion
        FROM producto_recetas r
        JOIN productos m ON m.id = r.material_id
        LEFT JOIN producto_variaciones v ON v.id = r.variacion_id
        WHERE r.producto_id = %s AND r.tenant_id = %s
        ORDER BY v.nombre ASC NULLS FIRST, m.Producto ASC
    """, (pid, tenant_id))


def agregar_material_receta(producto: str, material: str, cantidad: float, tenant_id: str, variacion_id: int | None = None) -> dict:
    """
    Añade (o actualiza la cantidad de) un material a la receta de un compuesto.

    variacion_id:
      None → receta BASE (se usa si la variación vendida no tiene receta propia)
      {id} → receta específica de esa variación (ej. Hamburguesa Doble usa 200g)

    Unicidad por índices parciales: (compuesto+material) para la base y
    (compuesto+variación+material) para cada variación.
    """
    producto = (producto or "").strip()
    material = (material or "").strip()
    cantidad = float(cantidad or 0)
    if not producto or not material:
        return {"ok": False, "mensaje": "El compuesto y el material son obligatorios"}
    if cantidad <= 0:
        return {"ok": False, "mensaje": "La cantidad debe ser mayor a 0"}
    pid = _resolver_producto_id(producto, tenant_id)
    mid = _resolver_producto_id(material, tenant_id)
    if not pid:
        return {"ok": False, "mensaje": "Compuesto no encontrado"}
    if not mid:
        return {"ok": False, "mensaje": "Material no encontrado"}
    if pid == mid:
        return {"ok": False, "mensaje": "Un producto no puede ser material de sí mismo"}
    if variacion_id is not None:
        # Validar que la variación existe y pertenece a este compuesto + tenant
        var = query(
            "SELECT 1 FROM producto_variaciones WHERE id = %s AND producto_id = %s AND tenant_id = %s",
            (variacion_id, pid, tenant_id)
        )
        if not var:
            return {"ok": False, "mensaje": "La variación no pertenece a este compuesto"}

    # ¿Ya existe este material para (compuesto, variación)? → actualizar cantidad
    existente = query("""
        SELECT id FROM producto_recetas
        WHERE producto_id = %s AND material_id = %s AND tenant_id = %s
          AND variacion_id IS NOT DISTINCT FROM %s
        LIMIT 1
    """, (pid, mid, tenant_id, variacion_id))
    if existente:
        result = query("""
            UPDATE producto_recetas SET cantidad = %s
            WHERE id = %s AND tenant_id = %s
            RETURNING id, cantidad
        """, (cantidad, existente[0]["id"], tenant_id))
    else:
        try:
            result = query("""
                INSERT INTO producto_recetas (producto_id, material_id, cantidad, tenant_id, variacion_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, cantidad
            """, (pid, mid, cantidad, tenant_id, variacion_id))
        except UniqueViolation:
            # Carrera (TOCTOU): otro request insertó el mismo material entre el
            # SELECT y el INSERT → convertir en UPDATE (upsert atómico)
            result = query("""
                UPDATE producto_recetas SET cantidad = %s
                WHERE producto_id = %s AND material_id = %s AND tenant_id = %s
                  AND variacion_id IS NOT DISTINCT FROM %s
                RETURNING id, cantidad
            """, (cantidad, pid, mid, tenant_id, variacion_id))
    if not result:
        return {"ok": False, "mensaje": "Error al guardar el material"}
    return {"ok": True, "material": material, "cantidad": float(result[0]["cantidad"] or 0)}


def actualizar_material_receta(receta_id: int, cantidad: float, tenant_id: str) -> dict:
    """Actualiza la cantidad de un material en la receta (validando tenant)."""
    cantidad = float(cantidad or 0)
    if cantidad <= 0:
        return {"ok": False, "mensaje": "La cantidad debe ser mayor a 0"}
    result = query("""
        UPDATE producto_recetas
        SET cantidad = %s
        WHERE id = %s AND tenant_id = %s
        RETURNING id, cantidad
    """, (cantidad, receta_id, tenant_id))
    if not result:
        return {"ok": False, "mensaje": "Material no encontrado en la receta"}
    return {"ok": True, "id": receta_id, "cantidad": float(result[0]["cantidad"] or 0)}


def eliminar_material_receta(receta_id: int, tenant_id: str) -> dict:
    """Elimina un material de la receta (validando tenant)."""
    result = query("""
        DELETE FROM producto_recetas
        WHERE id = %s AND tenant_id = %s
        RETURNING id
    """, (receta_id, tenant_id))
    if not result:
        return {"ok": False, "mensaje": "Material no encontrado en la receta"}
    return {"ok": True, "id": receta_id}