# ==============================================================================
# backend/database/lotes.py
# Inventario físico: lotes (stock, costo, precio) y algoritmo PEPS.
#
# Refactor Fase 1/2: este módulo quedó solo con lo de LOTES. Los demás
# dominios viven en:
#   - productos.py      → get_productos_meta, get_inventario_consolidado,
#                         crear_producto_completo, actualizar_producto
#   - variaciones.py    → CRUD de variaciones
#   - recetas.py        → recetas (BOM) de compuestos
#   - categorias.py     → CRUD de categorías
#   - helpers.py        → helpers compartidos (_q, _e, zona horaria del negocio, categorías, ...)
#
# Refactor Fase 3: descontar_stock_peps se dividió en _registro_venta,
# _crear_lote_virtual y _consumir_lotes_peps (misma lógica, sin duplicación
# del dict de venta que se construía 5 veces).
# ==============================================================================

import uuid
from database.conexion import query, execute
from database.helpers import _q, _e, _obtener_categorias_subquery, _sincronizar_categorias, _resolver_producto_id, ahora_negocio, _parsear_ts


def get_lotes(tenant_id: str) -> list[dict]:
    """Lee lotes + metadatos de productos en un JOIN."""
    cat_subquery = _obtener_categorias_subquery("p")
    return query(f"""
        SELECT 
            l.id as id,
            l.id_lote as id_lote,
            l.producto_id as producto_id,
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
        JOIN productos p ON p.id = l.producto_id AND p.tenant_id = l.tenant_id
        LEFT JOIN producto_variaciones v ON v.id = l.variacion_id
        WHERE l.Estado = 'Activo' AND l.tenant_id = %s
        ORDER BY l.Producto, l.Fecha_Entrada ASC
    """, (tenant_id,))


def get_detalle_lotes(producto: str, tenant_id: str) -> list[dict]:
    """Devuelve los lotes activos de un producto específico, con la variación
    a la que pertenece cada lote (None = stock base del producto)."""
    return query("""
        SELECT 
            l.id, l.id_lote, l.producto_id, l.producto, l.costo, l.precio_venta, l.stock_lote,
            l.fecha_entrada, l.estado, l.etiqueta,
            l.variacion_id, v.nombre AS variacion
        FROM lotes l
        LEFT JOIN producto_variaciones v ON v.id = l.variacion_id
        WHERE l.producto_id = (
            SELECT p.id FROM productos p
            WHERE p.Producto = %s AND p.tenant_id = %s
        ) AND l.Estado = 'Activo' AND l.tenant_id = %s
        ORDER BY l.Fecha_Entrada DESC
    """, (producto, tenant_id, tenant_id))


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
    fraccionable: bool = False,
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
    # ¿El producto ya existía? Las categorías solo se sincronizan al CREAR un
    # producto nuevo; en un restock de un producto existente NO se tocan (el
    # default ["General"] antes borraba las categorías en cada restock — bug).
    producto_existia = bool(query(
        "SELECT 1 FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)))
    result = query("""
        INSERT INTO productos (Producto, Descripcion, Imagen, Estado, tenant_id,
                               codigo_interno, codigo_barras, ubicacion, sufijo_precio,
                               fraccionable, tipo_producto, costo_servicio, precio_servicio)
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
            fraccionable = CASE WHEN EXCLUDED.fraccionable
                                THEN EXCLUDED.fraccionable ELSE productos.fraccionable END,
            -- El tipo NO se pisa en restock: se define al crear el producto
            tipo_producto = productos.tipo_producto,
            costo_servicio = CASE WHEN EXCLUDED.costo_servicio > 0
                                  THEN EXCLUDED.costo_servicio ELSE productos.costo_servicio END,
            precio_servicio = CASE WHEN EXCLUDED.precio_servicio > 0
                                   THEN EXCLUDED.precio_servicio ELSE productos.precio_servicio END
        RETURNING id
    """, (producto, descripcion, imagen, tenant_id, codigo_interno, codigo_barras, ubicacion,
          sufijo_limpio, bool(fraccionable), tipo, costo_srv, precio_srv))

    product_id = result[0]["id"]

    # Sincronizar categorías SOLO si el producto es nuevo: el restock de un
    # producto existente conserva las categorías que ya tenía (evita que el
    # default ["General"] las borre en cada restock).
    if not producto_existia:
        _sincronizar_categorias(product_id, categoria, tenant_id)

    # ── Servicio/Compuesto: no tienen inventario propio → no se crea lote ──
    if tipo == "servicio":
        return {"accion": "servicio_creado", "producto": producto}
    if tipo == "compuesto":
        return {"accion": "compuesto_creado", "producto": producto}

    # ── Variaciones ──
    # Si el producto tiene variaciones, TODO lote DEBE pertenecer a una
    # variación concreta (cada variación lleva su propio inventario). Si no
    # tiene variaciones, no se permiten lotes ligados a una variación.
    vnombre = (variacion or "").strip()
    variacion_id = None
    tiene_variaciones = query(
        "SELECT 1 FROM producto_variaciones v "
        "JOIN productos p ON p.id = v.producto_id "
        "WHERE p.Producto = %s AND p.tenant_id = %s LIMIT 1",
        (producto, tenant_id)
    )
    if tiene_variaciones:
        if not vnombre:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": "Este producto tiene variaciones: indica a cuál variación llegó el stock."}
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
                "mensaje": "Este producto no tiene variaciones: quita la variación o créala primero en Editar producto."}

    # Buscar lote existente con mismo costo, precio y VARIACIÓN.
    # Si la etiqueta nueva está vacía, se fusiona con cualquiera (comportamiento
    # actual); si trae etiqueta, solo se fusiona con un lote de la MISMA
    # presentación — si no hay match, se crea un lote nuevo con su etiqueta
    # (evita que la etiqueta se pierda al restockear al mismo precio).
    existente = query("""
        SELECT id_lote FROM lotes
        WHERE producto_id=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id = %s
          AND (%s = '' OR COALESCE(etiqueta, '') = %s)
          AND variacion_id IS NOT DISTINCT FROM %s
        LIMIT 1
    """, (product_id, costo, precio_venta, tenant_id, etiqueta_limpia, etiqueta_limpia, variacion_id))

    if existente:
        execute(
            "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id = %s",
            (stock, existente[0]["id_lote"], tenant_id)
        )
        return {"accion": "stock_sumado", "producto": producto, "cantidad": stock}
    else:
        id_lote = str(uuid.uuid4())[:12]
        fecha   = str(ahora_negocio(tenant_id))
        execute("""
            INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta,
                               Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, etiqueta, variacion_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s)
        """, (id_lote, producto, product_id, costo, precio_venta, stock, fecha, _parsear_ts(fecha), tenant_id, etiqueta_limpia, variacion_id))
        return {"accion": "lote_creado", "producto": producto, "id_lote": id_lote}


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

    # 3. Las órdenes que contienen esas ventas deben reflejar la nueva ganancia
    execute("""
        UPDATE ordenes o
        SET total = COALESCE(ag.total, 0),
            ganancia = COALESCE(ag.ganancia, 0),
            cantidad_items = COALESCE(ag.unidades, 0),
            estado = CASE WHEN COALESCE(ag.activos, 0) = 0 THEN 'Anulada' ELSE 'Activa' END
        FROM (
            SELECT orden_id,
                   SUM(total_venta) FILTER (WHERE estado != 'Inactivo') AS total,
                   SUM(ganancia_bruta) FILTER (WHERE estado != 'Inactivo') AS ganancia,
                   SUM(cantidad) FILTER (WHERE estado != 'Inactivo') AS unidades,
                   COUNT(*) FILTER (WHERE estado != 'Inactivo') AS activos
            FROM ventas
            WHERE ID_Lote = %s AND tenant_id = %s
            GROUP BY orden_id
        ) ag
        WHERE o.id = ag.orden_id AND o.tenant_id = %s
    """, (id_lote, tenant_id, tenant_id))

    return {"ok": True, "id_lote": id_lote}


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
    producto_id = query(
        "SELECT producto_id FROM lotes WHERE ID_Lote = %s AND tenant_id = %s",
        (id_lote, tenant_id)
    )[0]["producto_id"]

    # Marcar el lote como Inactivo y stock en 0
    execute(
        "UPDATE lotes SET Estado = 'Inactivo', Stock_Lote = 0 WHERE ID_Lote = %s AND tenant_id = %s",
        (id_lote, tenant_id)
    )

    # Verificar si quedan lotes activos para este producto
    # (descontando el lote que acabamos de desactivar)
    activos_restantes = query(
        "SELECT COUNT(*) as total FROM lotes WHERE producto_id = %s AND Estado = 'Activo' AND tenant_id = %s",
        (producto_id, tenant_id)
    )
    producto_desactivado = False

    if activos_restantes and activos_restantes[0]["total"] == 0:
        # No quedan lotes activos → desactivar el producto también
        execute(
            "UPDATE productos SET Estado = 'Inactivo' WHERE id = %s AND tenant_id = %s",
            (producto_id, tenant_id)
        )
        producto_desactivado = True

    return {
        "ok": True,
        "id_lote": id_lote,
        "producto": producto,
        "producto_desactivado": producto_desactivado
    }


# ==============================================================================
# Helpers de PEPS (Fase 3): descontar_stock_peps usa estas piezas. El dict de
# venta se construía 5 veces idéntico → ahora lo arma _registro_venta.
# ==============================================================================


def _registro_venta(
    producto: str,
    cantidad: float,
    precio_lista: float,
    precio_real: float,
    costo_unitario: float,
    id_lote: str,
    tenant_id: str,
    fecha: str | None = None,
) -> dict:
    """Construye el registro de venta que devuelve descontar_stock_peps."""
    fecha = fecha or str(ahora_negocio(tenant_id))
    return {
        "fecha"          : fecha,
        "producto"       : producto,
        "cantidad"       : cantidad,
        "precio_lista"   : float(precio_lista),
        "precio_real"    : precio_real,
        "costo_unitario" : float(costo_unitario),
        "total_venta"    : precio_real * cantidad,
        "ganancia_bruta" : (precio_real - float(costo_unitario)) * cantidad,
        "id_lote"        : id_lote
    }


def _crear_lote_virtual(conn, producto: str, precio_real: float, cantidad: float, tenant_id: str, variacion_id: int | None, fecha: str | None = None) -> tuple[str, str]:
    """
    Crea un lote virtual con stock negativo (no hay stock físico que descontar)
    y devuelve (id_lote, fecha). La fecha se reutiliza en el registro de venta.
    """
    nuevo_id = str(uuid.uuid4())[:12]
    fecha = fecha or str(ahora_negocio(tenant_id))
    producto_row = _q(conn,
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    if not producto_row:
        raise ValueError(f"Producto no encontrado: {producto}")
    producto_id = producto_row[0]["id"]
    _e(conn,
        "INSERT INTO lotes (ID_Lote, Producto, producto_id, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, variacion_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s)",
        (nuevo_id, producto, producto_id, 0, precio_real, -cantidad, fecha, _parsear_ts(fecha), tenant_id, variacion_id)
    )
    return nuevo_id, fecha


def _consumir_lotes_peps(conn, lotes: list[dict], producto: str, cantidad_total: float, precio_real: float, tenant_id: str) -> tuple[list[dict], float]:
    """
    Primera pasada del PEPS: consume stock de los lotes con inventario positivo
    en orden de antigüedad. Muta `lotes` in-place (actualiza stock_lote en
    memoria) para que la segunda pasada vea los valores correctos.
    Devuelve (ventas_generadas, restante).
    """
    ventas_generadas = []
    restante = cantidad_total

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

        ventas_generadas.append(_registro_venta(
            producto, consumir, lote["precio_venta"], precio_real, lote["costo"], lote["id_lote"], tenant_id
        ))
        restante -= consumir

    return ventas_generadas, restante


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
            WHERE ID_Lote=%s AND producto_id=(SELECT id FROM productos WHERE Producto=%s AND tenant_id=%s)
              AND Estado='Activo' AND tenant_id = %s
            FOR UPDATE
        """, (id_lote, producto, tenant_id, tenant_id))

        if not lotes:
            # Si no existe el lote, crear uno virtual con stock negativo
            # (comportamiento consistente con el PEPS normal)
            nuevo_id, fecha = _crear_lote_virtual(conn, producto, precio_real, cantidad_total, tenant_id, variacion_id)
            return [_registro_venta(producto, cantidad_total, precio_real, precio_real, 0, nuevo_id, tenant_id, fecha=fecha)]

        lote = lotes[0]
        nuevo_stock = round(float(lote["stock_lote"]) - cantidad_total, 3)
        _e(conn,
            "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id = %s",
            (nuevo_stock, id_lote, tenant_id)
        )

        return [_registro_venta(
            producto, cantidad_total, lote["precio_venta"], precio_real, lote["costo"], lote["id_lote"], tenant_id
        )]

    # ── PEPS normal (sin lote específico) ──
    # Obtenemos TODOS los lotes activos de esa variación (o base si no se
    # indica), incluso con stock 0 o negativo para seguir el orden PEPS.
    producto_row = _q(conn,
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    if not producto_row:
        raise ValueError(f"Producto no encontrado: {producto}")
    producto_id = producto_row[0]["id"]

    lotes = _q(conn, """
        SELECT * FROM lotes
        WHERE producto_id=%s AND Estado='Activo' AND tenant_id = %s
          AND variacion_id IS NOT DISTINCT FROM %s
        ORDER BY Fecha_Entrada ASC
        FOR UPDATE
    """, (producto_id, tenant_id, variacion_id))

    # --- Primera pasada: consumir stock de lotes con inventario positivo ---
    ventas_generadas, restante = _consumir_lotes_peps(conn, lotes, producto, cantidad_total, precio_real, tenant_id)

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

            ventas_generadas.append(_registro_venta(
                producto, restante, lote_destino["precio_venta"], precio_real, lote_destino["costo"], lote_destino["id_lote"], tenant_id
            ))
        else:
            # No existe ningún lote para este producto — creamos uno virtual con stock negativo
            nuevo_id_l, fecha = _crear_lote_virtual(conn, producto, precio_real, restante, tenant_id, variacion_id)
            ventas_generadas.append(_registro_venta(producto, restante, precio_real, precio_real, 0, nuevo_id_l, tenant_id, fecha=fecha))

    return ventas_generadas
