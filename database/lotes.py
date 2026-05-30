# ==============================================================================
# backend/database/lotes.py
# Lógica de inventario y algoritmo PEPS — sin Streamlit.
# ==============================================================================

import uuid
import datetime
import re
from database.conexion import query, execute

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


def _sincronizar_categorias(producto_id: int, categorias: list[str], tenant_id: str) -> None:
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
    """
    # Normalizar: limpiar espacios y eliminar duplicados preservando orden
    categorias = list(dict.fromkeys([c.strip() for c in categorias if c.strip()]))
    if not categorias:
        categorias = ["General"]

    # 1. Eliminar relaciones existentes para este producto
    execute(
        "DELETE FROM producto_categorias WHERE producto_id = %s",
        (producto_id,)
    )

    # 2. Upsert cada categoría y crear la relación
    for nombre in categorias:
        slug = _slugify(nombre)

        # Upsert: si ya existe (tenant_id, nombre), devuelve el id existente
        result = query("""
            INSERT INTO categorias (tenant_id, nombre, slug)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id, nombre) DO UPDATE SET
                slug = EXCLUDED.slug
            RETURNING id
        """, (tenant_id, nombre, slug))

        categoria_id = result[0]["id"]

        # Insertar en la tabla pivote (ignorar si ya existe por alguna razón)
        execute(
            "INSERT INTO producto_categorias (producto_id, categoria_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (producto_id, categoria_id)
        )


def _obtener_categorias_subquery(alias: str) -> str:
    """
    Genera una subquery SQL correlacionada para obtener las categorías
    de un producto como un array de nombres.
    
    Úsala en cualquier SELECT que necesite incluir categorías sin importar
    la columna antigua Categoria de la tabla productos.
    
    Args:
        alias: Alias de la tabla productos (e.g. 'p')
    """
    return f"""COALESCE(
        (SELECT array_agg(c.nombre ORDER BY c.nombre)
         FROM producto_categorias pc
         JOIN categorias c ON pc.categoria_id = c.id
         WHERE pc.producto_id = {alias}.id),
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
            p.Imagen as imagen, 
            p.Descripcion as descripcion,
            {cat_subquery}
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto AND l.tenant_id = p.tenant_id
        WHERE l.Estado = 'Activo' AND l.tenant_id = %s
        ORDER BY l.Producto, l.Fecha_Entrada ASC
    """, (tenant_id,))


def get_productos_meta(tenant_id: str) -> list[dict]:
    """Lee la tabla productos (metadatos) con sus categorías desde la relación Many-to-Many."""
    cat_subquery = _obtener_categorias_subquery("productos")
    return query(f"""
        SELECT 
            Producto as producto, 
            Descripcion as descripcion, 
            Imagen as imagen, 
            Estado as estado, 
            {cat_subquery}
        FROM productos 
        WHERE Tenant_ID = %s
        ORDER BY Producto ASC
    """,  (tenant_id,))


def get_inventario_consolidado(tenant_id: str) -> list[dict]:
    """
    Devuelve una fila por producto con stock total,
    precio del lote más reciente, costo promedio ponderado y categorías.
    """
    cat_subquery = _obtener_categorias_subquery("p")
    return query(f"""
        SELECT
            l.Producto                                               AS producto,
            p.Descripcion                                            AS descripcion,
            p.Imagen                                                 AS imagen,
            p.Estado                                                 AS estado,
            {cat_subquery},
            SUM(l.Stock_Lote)                                        AS stock_total,
            MAX(l.Precio_Venta)                                      AS precio_venta,
            SUM(l.Costo * l.Stock_Lote) / NULLIF(SUM(l.Stock_Lote), 0) AS costo_promedio
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto AND l.Tenant_ID = p.Tenant_ID
        WHERE l.Estado = 'Activo' AND l.Tenant_ID = %s
        GROUP BY l.Producto, p.Descripcion, p.Imagen, p.Estado, p.id
        ORDER BY l.Producto ASC
    """, (tenant_id,))


def get_detalle_lotes(producto: str, tenant_id: str) -> list[dict]:
    """Devuelve los lotes activos de un producto específico."""
    return query("""
        SELECT 
            id, id_lote, producto, costo, precio_venta, stock_lote, fecha_entrada, estado 
        FROM lotes
        WHERE Producto = %s AND Estado = 'Activo' AND tenant_id = %s
        ORDER BY Fecha_Entrada DESC
    """, (producto, tenant_id))


def agregar_lote(
    producto: str,
    descripcion: str,
    costo: float,
    precio_venta: float,
    stock: int,
    imagen: str = "No hay foto",
    categoria: list[str] = ["General"],
    tenant_id: str = ""
) -> dict:
    producto = producto.strip()
    descripcion = descripcion.strip()
    """
    Crea producto si no existe, luego inserta un lote nuevo
    o suma stock si ya existe uno con el mismo costo y precio.
    Las categorías se guardan en la tabla pivote producto_categorias.
    """
    # Upsert en productos (YA NO incluye Categoria, se maneja aparte)
    result = query("""
        INSERT INTO productos (Producto, Descripcion, Imagen, Estado, tenant_id)
        VALUES (%s, %s, %s, 'Activo', %s)
        ON CONFLICT(Producto, tenant_id) DO UPDATE SET
            Descripcion = EXCLUDED.Descripcion,
            Imagen = CASE WHEN EXCLUDED.Imagen != 'No hay foto'
                         THEN EXCLUDED.Imagen ELSE productos.Imagen END
        RETURNING id
    """, (producto, descripcion, imagen, tenant_id))

    product_id = result[0]["id"]

    # Sincronizar categorías en la tabla pivote (Many-to-Many)
    _sincronizar_categorias(product_id, categoria, tenant_id)

    # Buscar lote existente con mismo costo y precio
    existente = query("""
        SELECT id_lote FROM lotes
        WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id = %s
        LIMIT 1
    """, (producto, costo, precio_venta, tenant_id))

    if existente:
        execute(
            "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s AND tenant_id = %s",
            (stock, existente[0]["id_lote"], tenant_id)
        )
        return {"accion": "stock_sumado", "producto": producto, "cantidad": stock}
    else:
        id_lote = str(uuid.uuid4())[:12]
        fecha   = str(datetime.datetime.now())
        execute("""
            INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta,
                               Stock_Lote, Fecha_Entrada, Estado, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)
        """, (id_lote, producto, costo, precio_venta, stock, fecha, tenant_id))
        return {"accion": "lote_creado", "producto": producto, "id_lote": id_lote}


def actualizar_producto(
    producto: str,
    descripcion: str,
    imagen: str,
    estado: str,
    categoria: list[str],
    costo: float | None = None,
    precio_venta: float | None = None,
    tenant_id: str = "",
    nuevo_producto: str | None = None
) -> dict:
    producto = producto.strip()
    descripcion = descripcion.strip()
    """
    Actualiza metadatos de un producto y sus categorías (Many-to-Many).
    Si pasa a Inactivo, desactiva todos sus lotes.
    Si se proporcionan costo y/o precio_venta, actualiza todos los lotes activos.
    Si se proporciona nuevo_producto, renombra el producto en todas las tablas.
    """
    nombre_final = producto
    if nuevo_producto is not None:
        nombre_final = nuevo_producto.strip()
        if nombre_final and nombre_final != producto:
            # Renombrar en la tabla productos
            execute(
                "UPDATE productos SET Producto=%s, Descripcion=%s, Imagen=%s, Estado=%s WHERE Producto=%s AND tenant_id=%s",
                (nombre_final, descripcion, imagen, estado, producto, tenant_id)
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
                UPDATE productos SET Descripcion=%s, Imagen=%s, Estado=%s
                WHERE Producto=%s AND tenant_id = %s
            """, (descripcion, imagen, estado, producto, tenant_id))
    else:
        # Actualizar producto sin renombrar
        execute("""
            UPDATE productos SET Descripcion=%s, Imagen=%s, Estado=%s
            WHERE Producto=%s AND tenant_id = %s
        """, (descripcion, imagen, estado, producto, tenant_id))

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
            COUNT(pc.producto_id) AS total_productos
        FROM categorias c
        LEFT JOIN producto_categorias pc ON pc.categoria_id = c.id
        WHERE c.tenant_id = %s
        GROUP BY c.id, c.nombre, c.slug
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


def actualizar_lote(id_lote: str, costo: float, precio_venta: float, stock: int, tenant_id: str) -> dict:
    """Actualiza costo, precio de venta y stock de un lote específico."""
    # 1. Actualizar el lote
    execute("""
        UPDATE lotes SET Costo=%s, Precio_Venta=%s, Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id = %s
    """, (costo, precio_venta, stock, id_lote, tenant_id))

    # 2. Recalcular ganancias en ventas asociadas a este lote
    execute("""
        UPDATE ventas 
        SET Costo_Unitario = %s,
            Ganancia_Bruta = (Precio_Real - %s) * Cantidad
        WHERE ID_Lote = %s AND Estado = 'Activo' AND tenant_id = %s
    """, (costo, costo, id_lote, tenant_id))

    return {"ok": True, "id_lote": id_lote}


def descontar_stock_peps(
    producto: str,
    cantidad_total: int,
    precio_real: float,
    tenant_id: str
) -> list[dict]:
    producto = producto.strip()
    """
    Algoritmo PEPS: descuenta del lote más antiguo primero.
    
    A diferencia de la versión anterior, ahora PERMITE stock negativo.
    Si no hay suficiente stock físico, se consume todo lo disponible
    y el excedente se descuenta del lote más reciente, permitiendo
    que su Stock_Lote quede en negativo.
    Esto es útil para flujos de trabajo donde el usuario quiere
    cobrar aunque falte inventario, con la advertencia correspondiente.
    
    Retorna siempre una lista de registros de venta (nunca None).
    """
    # Obtenemos TODOS los lotes activos, incluso con stock 0 o negativo
    # para poder seguir el orden PEPS correctamente
    lotes = query("""
        SELECT * FROM lotes
        WHERE Producto=%s AND Estado='Activo' AND tenant_id = %s
        ORDER BY Fecha_Entrada ASC
    """, (producto, tenant_id))

    ventas_generadas = []
    restante = cantidad_total

    # --- Primera pasada: consumir stock de lotes con inventario positivo ---
    for lote in lotes:
        if restante <= 0:
            break

        # Solo consumimos de lotes que tengan stock positivo
        if int(lote["stock_lote"]) <= 0:
            continue

        consumir    = min(restante, int(lote["stock_lote"]))
        nuevo_stock = int(lote["stock_lote"]) - consumir

        execute(
            "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id = %s",
            (nuevo_stock, lote["id_lote"], tenant_id)
        )
        
        # Actualizar el valor en memoria para que la segunda pasada (negativos)
        # vea el stock correcto, no el valor original de la consulta.
        lote["stock_lote"] = nuevo_stock

        ventas_generadas.append({
            "fecha"          : str(datetime.datetime.now()),
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
            nuevo_stock = int(lote_destino["stock_lote"]) - restante
            
            execute(
                "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id = %s",
                (nuevo_stock, lote_destino["id_lote"], tenant_id)
            )
            
            ventas_generadas.append({
                "fecha"          : str(datetime.datetime.now()),
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
            id_lote   = str(uuid.uuid4())[:12]
            fecha     = str(datetime.datetime.now())
            
            execute(
                "INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id) VALUES (%s, %s, %s, %s, %s, %s, 'Activo', %s)",
                (id_lote, producto, 0, precio_real, -restante, fecha, tenant_id)
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
                "id_lote"        : id_lote
            })

    return ventas_generadas