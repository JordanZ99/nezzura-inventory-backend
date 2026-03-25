# ==============================================================================
# backend/database/lotes.py
# Lógica de inventario y algoritmo PEPS — sin Streamlit.
# ==============================================================================

import uuid
import datetime
from database.conexion import query, execute


def get_lotes() -> list[dict]:
    """Lee lotes + metadatos de productos en un JOIN."""
    return query("""
        SELECT l.*, p.Imagen, p.Descripcion
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto
        WHERE l.Estado = 'Activo'
        ORDER BY l.Producto, l.Fecha_Entrada ASC
    """)


def get_productos_meta() -> list[dict]:
    """Lee la tabla productos (metadatos)."""
    return query("SELECT * FROM productos ORDER BY Producto ASC")


def get_inventario_consolidado() -> list[dict]:
    """
    Devuelve una fila por producto con stock total,
    precio del lote más reciente y costo promedio ponderado.
    """
    return query("""
        SELECT
            l.Producto,
            p.Descripcion,
            p.Imagen,
            p.Estado,
            SUM(l.Stock_Lote)                                        AS stock_total,
            MAX(l.Precio_Venta)                                      AS precio_venta,
            SUM(l.Costo * l.Stock_Lote) / NULLIF(SUM(l.Stock_Lote), 0) AS costo_promedio
        FROM lotes l
        LEFT JOIN productos p ON l.Producto = p.Producto
        WHERE l.Estado = 'Activo' AND l.Stock_Lote > 0
        GROUP BY l.Producto, p.Descripcion, p.Imagen, p.Estado
        ORDER BY l.Producto ASC
    """)


def get_detalle_lotes(producto: str) -> list[dict]:
    """Devuelve los lotes activos de un producto específico."""
    return query("""
        SELECT * FROM lotes
        WHERE Producto = %s AND Estado = 'Activo' AND Stock_Lote > 0
        ORDER BY Fecha_Entrada DESC
    """, (producto,))


def agregar_lote(
    producto: str,
    descripcion: str,
    costo: float,
    precio_venta: float,
    stock: int,
    imagen: str = "No hay foto"
) -> dict:
    """
    Crea producto si no existe, luego inserta un lote nuevo
    o suma stock si ya existe uno con el mismo costo y precio.
    """
    # Upsert en productos
    execute("""
        INSERT INTO productos (Producto, Descripcion, Imagen, Estado)
        VALUES (%s, %s, %s, 'Activo')
        ON CONFLICT(Producto) DO UPDATE SET
            Descripcion = EXCLUDED.Descripcion,
            Imagen = CASE WHEN EXCLUDED.Imagen != 'No hay foto'
                         THEN EXCLUDED.Imagen ELSE productos.Imagen END
    """, (producto, descripcion, imagen))

    # Buscar lote existente con mismo costo y precio
    existente = query("""
        SELECT id_lote FROM lotes
        WHERE Producto=%s AND Costo=%s AND Precio_Venta=%s AND Estado='Activo'
        LIMIT 1
    """, (producto, costo, precio_venta))

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
                               Stock_Lote, Fecha_Entrada, Estado)
            VALUES (%s, %s, %s, %s, %s, %s, 'Activo')
        """, (id_lote, producto, costo, precio_venta, stock, fecha))
        return {"accion": "lote_creado", "producto": producto, "id_lote": id_lote}


def actualizar_producto(
    producto: str,
    descripcion: str,
    imagen: str,
    estado: str
) -> dict:
    """Actualiza metadatos. Si pasa a Inactivo, desactiva todos sus lotes."""
    execute("""
        UPDATE productos SET Descripcion=%s, Imagen=%s, Estado=%s
        WHERE Producto=%s
    """, (descripcion, imagen, estado, producto))

    if estado == "Inactivo":
        execute(
            "UPDATE lotes SET Estado='Inactivo' WHERE Producto=%s",
            (producto,)
        )
    return {"ok": True, "producto": producto, "estado": estado}


def actualizar_lote(id_lote: str, costo: float, precio_venta: float, stock: int) -> dict:
    """Actualiza costo, precio de venta y stock de un lote específico."""
    execute("""
        UPDATE lotes SET Costo=%s, Precio_Venta=%s, Stock_Lote=%s WHERE ID_Lote=%s
    """, (costo, precio_venta, stock, id_lote))
    return {"ok": True, "id_lote": id_lote}


def descontar_stock_peps(
    producto: str,
    cantidad_total: int,
    precio_real: float
) -> list[dict] | None:
    """
    Algoritmo PEPS: descuenta del lote más antiguo primero.
    Retorna lista de registros de venta o None si no hay stock.
    """
    lotes = query("""
        SELECT * FROM lotes
        WHERE Producto=%s AND Stock_Lote > 0 AND Estado='Activo'
        ORDER BY Fecha_Entrada ASC
    """, (producto,))

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
            "UPDATE lotes SET Stock_Lote=%s WHERE ID_Lote=%s",
            (nuevo_stock, lote["id_lote"])
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
        })
        restante -= consumir

    return ventas_generadas