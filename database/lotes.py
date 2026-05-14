# ==============================================================================
# backend/database/lotes.py
# Lógica de inventario y algoritmo PEPS — sin Streamlit.
# ==============================================================================

import uuid
import datetime
from database.conexion import query, execute, DEFAULT_TENANT_ID


def get_lotes(tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """Lee lotes + metadatos de productos en un JOIN, filtrado por tenant."""
    return query("""
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
            p.Categoria as categoria
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto AND l.tenant_id = p.tenant_id
        WHERE l.Estado = 'Activo' AND l.tenant_id = %s
        ORDER BY l.Producto, l.Fecha_Entrada ASC
    """, (tenant_id,))


def get_productos_meta(tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """Lee la tabla productos (metadatos) filtrada por tenant."""
    return query("""
        SELECT Producto as producto, Descripcion as descripcion, Imagen as imagen, Estado as estado, Categoria as categoria 
        FROM productos 
        WHERE tenant_id = %s
        ORDER BY Producto ASC
    """, (tenant_id,))


def get_inventario_consolidado(tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """
    Devuelve una fila por producto con stock total,
    precio del lote más reciente y costo promedio ponderado.
    Filtrado por tenant.
    """
    return query("""
        SELECT
            l.Producto                                               AS producto,
            p.Descripcion                                            AS descripcion,
            p.Imagen                                                 AS imagen,
            p.Estado                                                 AS estado,
            p.Categoria                                              AS categoria,
            SUM(l.Stock_Lote)                                        AS stock_total,
            MAX(l.Precio_Venta)                                      AS precio_venta,
            SUM(l.Costo * l.Stock_Lote) / NULLIF(SUM(l.Stock_Lote), 0) AS costo_promedio
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto AND l.tenant_id = p.tenant_id
        WHERE l.Estado = 'Activo' AND l.Stock_Lote > 0 -- AND l.tenant_id = %s
        GROUP BY l.Producto, p.Descripcion, p.Imagen, p.Estado, p.Categoria
        ORDER BY l.Producto ASC
    """, ()) # (tenant_id,)


def get_detalle_lotes(producto: str, tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """Devuelve los lotes activos de un producto específico y tenant específico."""
    return query("""
        SELECT 
            id, id_lote, producto, costo, precio_venta, stock_lote, fecha_entrada, estado 
        FROM lotes
        WHERE Producto = %s AND Estado = 'Activo' AND Stock_Lote > 0 AND tenant_id = %s
        ORDER BY Fecha_Entrada DESC
    """, (producto, tenant_id))


def agregar_lote(
    producto: str,
    descripcion: str,
    costo: float,
    precio_venta: float,
    stock: int,
    imagen: str = "No hay foto",
    categoria: str = "General",
    tenant_id: str = DEFAULT_TENANT_ID
) -> dict:
    producto = producto.strip()
    descripcion = descripcion.strip()
    """
    Crea producto si no existe, luego inserta un lote nuevo
    o suma stock si ya existe uno con el mismo costo y precio.
    """
    # Upsert en productos
    execute("""
        INSERT INTO productos (Producto, Descripcion, Imagen, Estado, Categoria, tenant_id)
        VALUES (%s, %s, %s, 'Activo', %s, %s)
        ON CONFLICT(Producto) DO UPDATE SET
            Descripcion = EXCLUDED.Descripcion,
            Categoria = EXCLUDED.Categoria,
            Imagen = CASE WHEN EXCLUDED.Imagen != 'No hay foto'
                         THEN EXCLUDED.Imagen ELSE productos.Imagen END
    """, (producto, descripcion, imagen, categoria, tenant_id))

    # Buscar lote existente con mismo costo y precio
    existente = query("""
        SELECT id_lote FROM lotes
        WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo' AND tenant_id=%s
        LIMIT 1
    """, (producto, costo, precio_venta, tenant_id))

    if existente:
        execute(
            "UPDATE lotes SET Stock_Lote = Stock_Lote + %s WHERE ID_Lote = %s",
            (stock, existente[0]["id_lote"])
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
    categoria: str,
    tenant_id: str = DEFAULT_TENANT_ID
) -> dict:
    producto = producto.strip()
    descripcion = descripcion.strip()
    """Actualiza metadatos. Si pasa a Inactivo, desactiva todos sus lotes."""
    execute("""
        UPDATE productos SET Descripcion=%s, Imagen=%s, Estado=%s, Categoria=%s
        WHERE Producto=%s AND tenant_id=%s
    """, (descripcion, imagen, estado, categoria, producto, tenant_id))

    if estado == "Inactivo":
        execute(
            "UPDATE lotes SET Estado='Inactivo' WHERE Producto=%s AND tenant_id=%s",
            (producto, tenant_id)
        )
    return {"ok": True, "producto": producto, "estado": estado}


def actualizar_lote(id_lote: str, costo: float, precio_venta: float, stock: int, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """Actualiza costo, precio de venta y stock de un lote específico."""
    # 1. Actualizar el lote
    execute("""
        UPDATE lotes SET Costo=%s, Precio_Venta=%s, Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id=%s
    """, (costo, precio_venta, stock, id_lote, tenant_id))

    # 2. Recalcular ganancias en ventas asociadas a este lote
    # Ganancia = (Precio_Real - Nuevo_Costo) * Cantidad
    execute("""
        UPDATE ventas 
        SET Costo_Unitario = %s,
            Ganancia_Bruta = (Precio_Real - %s) * Cantidad
        WHERE ID_Lote = %s AND Estado = 'Activo' AND tenant_id=%s
    """, (costo, costo, id_lote, tenant_id))

    return {"ok": True, "id_lote": id_lote}


def descontar_stock_peps(
    producto: str,
    cantidad_total: int,
    precio_real: float,
    tenant_id: str = DEFAULT_TENANT_ID
) -> list[dict] | None:
    producto = producto.strip()
    """
    Algoritmo PEPS: descuenta del lote más antiguo primero.
    Retorna lista de registros de venta o None si no hay stock.
    """
    lotes = query("""
        SELECT * FROM lotes
        WHERE Producto=%s AND Stock_Lote > 0 AND Estado='Activo' AND tenant_id=%s
        ORDER BY Fecha_Entrada ASC
    """, (producto, tenant_id))

    if not lotes:
        return None

    stock_disponible = sum(l["stock_lote"] for l in lotes)
    if stock_disponible < cantidad_total:
        return None

    ventas_generadas = []
    restante = cantidad_total

    for lote in lotes:
        if restante <= 0:
            break

        consumir    = min(restante, int(lote["stock_lote"]))
        nuevo_stock = int(lote["stock_lote"]) - consumir

        execute(
            "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s AND tenant_id=%s",
            (nuevo_stock, lote["id_lote"], tenant_id)
        )

        ventas_generadas.append({
            "fecha"          : str(datetime.datetime.now()),
            "producto"       : producto,
            "cantidad"       : consumir,
            "precio_lista"   : float(lote["precio_venta"]),
            "precio_real"    : precio_real,
            "costo_unitario" : float(lote["costo"]),
            "total_venta"    : precio_real * consumir,
            "ganancia_bruta" : (precio_real - float(lote["costo"])) * consumir,
            "id_lote"        : lote["id_lote"],
            "tenant_id"      : tenant_id
        })
        restante -= consumir

    return ventas_generadas