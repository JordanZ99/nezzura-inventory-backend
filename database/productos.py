# ==============================================================================
# backend/database/productos.py
# Productos como entidad: lecturas consolidadas (con variaciones, recetas y
# disponibilidad) + alta y edición del producto. El inventario físico vive
# en lotes.py; las variaciones en variaciones.py; las recetas en recetas.py.
#
# Extraído de lotes.py — Fase 1/2 del refactor por dominio.
# Fase 3: crear_producto_completo se dividió en _upsert_producto,
# _crear_lote_inicial, _crear_variaciones_y_lotes y _crear_receta_compuesto
# (misma lógica de transacción todo-o-nada, solo reorganizada).
# ==============================================================================

import uuid
import json
from psycopg2.errors import UniqueViolation
from database.conexion import query, execute
from database.helpers import _q, _e, _obtener_categorias_subquery, _sincronizar_categorias, ahora_negocio, _parsear_ts
from database.variaciones import _adjuntar_variaciones
from database.recetas import _adjuntar_recetas, _calcular_disponibilidad_compuestos


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
            fraccionable,
            tipo_producto,
            costo_servicio,
            precio_servicio,
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
            p.fraccionable                                           AS fraccionable,
            p.tipo_producto                                          AS tipo_producto,
            p.costo_servicio                                         AS costo_servicio,
            p.precio_servicio                                        AS precio_servicio,
            p.post_override                                          AS post_override,
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
        LEFT JOIN lotes l ON l.producto_id = p.id AND l.Tenant_ID = p.Tenant_ID AND l.Estado = 'Activo'
        WHERE p.Tenant_ID = %s AND p.Estado = 'Activo'
        GROUP BY p.Producto, p.Descripcion, p.Imagen, p.Estado, p.id, p.codigo_interno, p.codigo_barras, p.ubicacion, p.visible_en_catalogo, p.sufijo_precio, p.fraccionable, p.tipo_producto, p.costo_servicio, p.precio_servicio, p.post_override
        ORDER BY p.Producto ASC
    """, (tenant_id,))

    # Adjuntar variaciones (POS/catálogo) y recetas (gestor) por producto
    _adjuntar_variaciones(filas, tenant_id)
    _adjuntar_recetas(filas, tenant_id)
    _calcular_disponibilidad_compuestos(filas, tenant_id)

    # Elegir el precio sugerido según el modo del tenant y limpiar las variantes
    for f in filas:
        # post_override (JSONB): psycopg2 lo entrega como texto si no hay
        # typecaster registrado; normalizarlo a dict (o None si es NULL).
        ov = f.get("post_override")
        if isinstance(ov, str) and ov.strip():
            try:
                f["post_override"] = json.loads(ov)
            except Exception:
                f["post_override"] = None
        elif not isinstance(ov, dict):
            f["post_override"] = None

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


# ==============================================================================
# Helpers de creación (Fase 3): piezas de crear_producto_completo. Todas
# operan sobre la conexión de la transacción (todo-o-nada) y devuelven None
# si todo salió bien o un dict de error {"ok": False, ...} para validación.
# ==============================================================================


def _upsert_producto(
    conn,
    *,
    producto: str,
    descripcion: str,
    imagen: str,
    tenant_id: str,
    codigo_interno: str | None,
    codigo_barras: str | None,
    ubicacion: str | None,
    sufijo_limpio: str,
    frac: bool,
    tipo: str,
    costo_srv: float,
    precio_srv: float,
    vis: bool,
) -> int:
    """
    Upsert del producto. En conflicto (producto ya existente) se conserva la
    visibilidad actual y el tipo nunca se pisa. Devuelve el id numérico.
    """
    r = _q(conn, """
        INSERT INTO productos (Producto, Descripcion, Imagen, Estado, tenant_id,
                               codigo_interno, codigo_barras, ubicacion, sufijo_precio,
                               fraccionable, tipo_producto, costo_servicio, precio_servicio, visible_en_catalogo)
        VALUES (%s, %s, %s, 'Activo', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(Producto, tenant_id) DO UPDATE SET
            Descripcion = EXCLUDED.Descripcion,
            Imagen = CASE WHEN EXCLUDED.Imagen != 'No hay foto'
                         THEN EXCLUDED.Imagen ELSE productos.Imagen END,
            codigo_interno = COALESCE(EXCLUDED.codigo_interno, productos.codigo_interno),
            codigo_barras = COALESCE(EXCLUDED.codigo_barras, productos.codigo_barras),
            ubicacion = COALESCE(EXCLUDED.ubicacion, productos.ubicacion),
            sufijo_precio = CASE WHEN EXCLUDED.sufijo_precio != ''
                                 THEN EXCLUDED.sufijo_precio ELSE productos.sufijo_precio END,
            fraccionable = CASE WHEN EXCLUDED.fraccionable
                                THEN EXCLUDED.fraccionable ELSE productos.fraccionable END,
            tipo_producto = productos.tipo_producto,
            costo_servicio = CASE WHEN EXCLUDED.costo_servicio > 0
                                  THEN EXCLUDED.costo_servicio ELSE productos.costo_servicio END,
            precio_servicio = CASE WHEN EXCLUDED.precio_servicio > 0
                                   THEN EXCLUDED.precio_servicio ELSE productos.precio_servicio END,
            -- La visibilidad NO se pisa en restock/conflictos: solo aplica en INSERT
            visible_en_catalogo = productos.visible_en_catalogo
        RETURNING id
    """, (producto, descripcion, imagen, tenant_id, codigo_interno, codigo_barras, ubicacion,
          sufijo_limpio, frac, tipo, costo_srv, precio_srv, vis))
    return r[0]["id"]


def _crear_lote_inicial(
    conn,
    *,
    producto: str,
    costo: float,
    precio_venta: float,
    stock: float,
    tenant_id: str,
    etiqueta_limpia: str,
    product_id: int,
) -> str:
    """
    Crea el lote inicial de un producto tipo 'stock' sin variaciones: si ya
    existe un lote con el mismo costo+precio, suma stock; si no, crea uno
    nuevo. Devuelve la acción realizada ('lote_creado' | 'stock_sumado').
    """
    existente = _q(conn, """
        SELECT id_lote FROM lotes
        WHERE producto_id=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id = %s
          AND (%s = '' OR COALESCE(etiqueta, '') = %s)
        LIMIT 1
    """, (product_id, costo, precio_venta, tenant_id, etiqueta_limpia, etiqueta_limpia))
    if existente:
        _e(conn,
            "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id = %s",
            (stock, existente[0]["id_lote"], tenant_id))
        return "stock_sumado"
    id_lote = str(uuid.uuid4())[:12]
    fecha = str(ahora_negocio(tenant_id))
    _e(conn, """
        INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta,
                           Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, etiqueta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s)
    """, (id_lote, producto, product_id, costo, precio_venta, stock, fecha, _parsear_ts(fecha), tenant_id, etiqueta_limpia))
    return "lote_creado"


def _crear_variaciones_y_lotes(
    conn,
    *,
    product_id: int,
    producto: str,
    costo: float,
    variaciones_list: list[dict],
    tipo: str,
    tenant_id: str,
) -> dict | None:
    """
    Crea las variaciones del producto (aplica a cualquier tipo). Si la
    variación trae stock_inicial y el tipo es 'stock', crea el LOTE de esa
    variación (costo propio opcional; si no, el costo del producto).

    Devuelve un dict de error si una variación duplicada causa rollback,
    o None si todo salió bien.
    """
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
            vfecha = str(ahora_negocio(tenant_id))
            _e(conn, """
                INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta,
                                   Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, variacion_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s)
            """, (str(uuid.uuid4())[:12], producto, product_id, vcosto, vprecio,
                   float(vstock), vfecha, _parsear_ts(vfecha), tenant_id, vid))
    return None


def _crear_receta_compuesto(
    conn,
    *,
    product_id: int,
    recetas: list[dict] | None,
    tenant_id: str,
) -> dict | None:
    """
    Crea la receta (BOM) de un compuesto: valida que cada material exista y
    no sea el propio compuesto. Si un material ya está en la receta base,
    actualiza su cantidad (UniqueViolation → UPDATE).

    Devuelve un dict de error si un material no existe o es el compuesto
    mismo, o None si todo salió bien.
    """
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
    return None


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
    fraccionable: bool | None = None,
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
      - su primer lote (solo tipo 'stock', y solo si NO trae variaciones — si
        trae variaciones, se crean lotes POR VARIACIÓN y el producto maneja
        stock por variación),
      - sus VARIACIONES ({nombre, precio, stock_inicial?, costo?}) si vienen,
      - su RECETA ({material, cantidad}) si es compuesto.

    Si cualquier paso falla (variación duplicada, material inexistente, etc.)
    se hace rollback: no queda el producto a medias.

    visible_en_catalogo:
      True/False → aplica SOLO cuando el producto es NUEVO (INSERT). Si el
      producto ya existía (ON CONFLICT, caso raro en el alta), se conserva la
      visibilidad actual para no pisarla.

    La orquestación de la transacción vive aquí; cada paso está en sus
    helpers: _upsert_producto, _crear_lote_inicial, _crear_variaciones_y_lotes
    y _crear_receta_compuesto.
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
    frac = bool(fraccionable) if fraccionable is not None else False

    conn = get_conn()
    try:
        # 1. Upsert del producto
        product_id = _upsert_producto(
            conn,
            producto=producto, descripcion=descripcion, imagen=imagen, tenant_id=tenant_id,
            codigo_interno=codigo_interno, codigo_barras=codigo_barras, ubicacion=ubicacion,
            sufijo_limpio=sufijo_limpio, frac=frac, tipo=tipo,
            costo_srv=costo_srv, precio_srv=precio_srv, vis=vis,
        )

        # 2. Categorías (dentro de la misma transacción)
        _sincronizar_categorias(product_id, categoria or ["General"], tenant_id, conn=conn)

        # Si el producto trae variaciones, TODO el stock vive en lotes por
        # variación y NO se crea el lote base con `stock`.
        variaciones_list = variaciones or []
        hay_variaciones = any((v.get("nombre") or "").strip() for v in variaciones_list)

        # 3. Lote inicial (solo tipo 'stock' y sin variaciones)
        accion = "lote_creado"
        if tipo == "stock" and not hay_variaciones:
            accion = _crear_lote_inicial(
                conn, producto=producto, costo=costo, precio_venta=precio_venta,
                stock=stock, tenant_id=tenant_id, etiqueta_limpia=etiqueta_limpia,
                product_id=product_id,
            )
        elif tipo == "servicio":
            accion = "servicio_creado"
        elif tipo == "compuesto":
            accion = "compuesto_creado"

        # 4. Variaciones (aplica a cualquier tipo)
        error = _crear_variaciones_y_lotes(
            conn, product_id=product_id, producto=producto, costo=costo,
            variaciones_list=variaciones_list, tipo=tipo, tenant_id=tenant_id,
        )
        if error:
            return error

        # 5. Receta (solo compuestos)
        if tipo == "compuesto":
            error = _crear_receta_compuesto(conn, product_id=product_id, recetas=recetas, tenant_id=tenant_id)
            if error:
                return error

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
    fraccionable: bool | None = None,
    tipo_producto: str | None = None,
    costo_servicio: float | None = None,
    precio_servicio: float | None = None
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
    producto_id_actual = None
    if nuevo_producto is not None:
        fila_producto = query(
            "SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s",
            (producto, tenant_id)
        )
        producto_id_actual = fila_producto[0]["id"] if fila_producto else None
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
                "fraccionable=COALESCE(%s, fraccionable), "
                "tipo_producto=COALESCE(%s, tipo_producto), "
                "costo_servicio=COALESCE(%s, costo_servicio), "
                "precio_servicio=COALESCE(%s, precio_servicio) "
                "WHERE Producto=%s AND tenant_id=%s",
                (nombre_final, descripcion, imagen, estado,
                 codigo_interno, codigo_barras, ubicacion, visible_en_catalogo, sufijo_precio,
                 fraccionable, tipo_producto, costo_servicio, precio_servicio, producto, tenant_id)
            )
            # Renombrar en lotes
            execute(
                "UPDATE lotes SET Producto=%s WHERE producto_id=%s AND tenant_id=%s",
                (nombre_final, producto_id_actual, tenant_id)
            )
            execute(
                "UPDATE ventas SET Producto=%s WHERE producto_id=%s AND tenant_id=%s",
                (nombre_final, producto_id_actual, tenant_id)
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
                    fraccionable=COALESCE(%s, fraccionable),
                    tipo_producto=COALESCE(%s, tipo_producto),
                    costo_servicio=COALESCE(%s, costo_servicio),
                    precio_servicio=COALESCE(%s, precio_servicio)
                WHERE Producto=%s AND tenant_id = %s
            """, (descripcion, imagen, estado, codigo_interno, codigo_barras, ubicacion, visible_en_catalogo, sufijo_precio,
                   fraccionable, tipo_producto, costo_servicio, precio_servicio, producto, tenant_id))
    else:
        # Actualizar producto sin renombrar
        execute("""
            UPDATE productos SET Descripcion=%s, Imagen=%s, Estado=%s,
                codigo_interno=COALESCE(%s, codigo_interno),
                codigo_barras=COALESCE(%s, codigo_barras),
                ubicacion=COALESCE(%s, ubicacion),
                visible_en_catalogo=COALESCE(%s, visible_en_catalogo),
                sufijo_precio=COALESCE(%s, sufijo_precio),
                fraccionable=COALESCE(%s, fraccionable),
                tipo_producto=COALESCE(%s, tipo_producto),
                costo_servicio=COALESCE(%s, costo_servicio),
                precio_servicio=COALESCE(%s, precio_servicio)
            WHERE Producto=%s AND tenant_id = %s
        """, (descripcion, imagen, estado, codigo_interno, codigo_barras, ubicacion, visible_en_catalogo, sufijo_precio,
               fraccionable, tipo_producto, costo_servicio, precio_servicio, producto, tenant_id))

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
            "UPDATE lotes SET Costo=%s WHERE producto_id = "
            "(SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s) AND Estado='Activo' AND tenant_id=%s",
            (costo, producto, tenant_id, tenant_id)
        )
    if precio_venta is not None:
        execute(
            "UPDATE lotes SET Precio_Venta=%s WHERE producto_id = "
            "(SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s) AND Estado='Activo' AND tenant_id=%s",
            (precio_venta, producto, tenant_id, tenant_id)
        )

    if estado == "Inactivo":
        execute(
            "UPDATE lotes SET Estado='Inactivo' WHERE producto_id = "
            "(SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s) AND tenant_id = %s",
            (producto, tenant_id, tenant_id)
        )

    return {"ok": True, "producto": producto, "estado": estado}
