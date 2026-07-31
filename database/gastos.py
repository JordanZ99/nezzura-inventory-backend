# ==============================================================================
# backend/database/gastos.py
# CRUD sobre la tabla gastos.
# ==============================================================================

from database.conexion import query, execute


# ==============================================================================
# CRUD para categorías de gasto (editables por el usuario)
# Sigue el mismo patrón que categorias.py de inventario.
# ==============================================================================


def listar_categorias_gasto(tenant_id: str) -> list[dict]:
    """
    Devuelve todas las categorías de gasto del tenant.
    """
    return query(
        "SELECT id, nombre FROM gastos_categorias WHERE tenant_id = %s ORDER BY nombre ASC",
        (tenant_id,)
    )


def crear_categoria_gasto(nombre: str, tenant_id: str) -> dict:
    """
    Crea una categoría de gasto nueva para el tenant.
    Si ya existe, retorna la existente.
    """
    nombre = nombre.strip()
    result = query("""
        INSERT INTO gastos_categorias (tenant_id, nombre)
        VALUES (%s, %s)
        ON CONFLICT (tenant_id, nombre) DO NOTHING
        RETURNING id, nombre
    """, (tenant_id, nombre))

    if not result:
        existente = query(
            "SELECT id, nombre FROM gastos_categorias WHERE nombre = %s AND tenant_id = %s",
            (nombre, tenant_id)
        )
        if existente:
            return {"ok": True, "categoria": existente[0], "mensaje": f"La categoría '{nombre}' ya existía"}
        return {"ok": False, "mensaje": "Error al crear la categoría"}

    return {"ok": True, "categoria": result[0], "mensaje": f"Categoría '{nombre}' creada"}


def renombrar_categoria_gasto(anterior_nombre: str, nuevo_nombre: str, tenant_id: str) -> dict:
    """
    Cambia el nombre de una categoría de gasto y actualiza todos los gastos que la usan.
    La categoría 'Otros' está protegida y no puede renombrarse.
    """
    anterior_nombre = anterior_nombre.strip()
    nuevo_nombre = nuevo_nombre.strip()

    # Proteger la categoría 'Otros' de ser renombrada
    if anterior_nombre == "Otros":
        return {"ok": False, "mensaje": "La categoría 'Otros' no puede renombrarse"}

    result = query("""
        UPDATE gastos_categorias
        SET nombre = %s
        WHERE nombre = %s AND tenant_id = %s
        RETURNING id, nombre
    """, (nuevo_nombre, anterior_nombre, tenant_id))

    if not result:
        return {"ok": False, "mensaje": f"Categoría '{anterior_nombre}' no encontrada"}

    # Actualizar también los gastos existentes que usaban el nombre anterior
    execute(
        "UPDATE gastos SET Categoria = %s WHERE Categoria = %s AND Tenant_ID = %s",
        (nuevo_nombre, anterior_nombre, tenant_id)
    )

    return {"ok": True, "categoria": result[0]}


def eliminar_categoria_gasto(nombre: str, tenant_id: str) -> dict:
    """
    Elimina una categoría de gasto del tenant.
    Los gastos que la usaban se reasignan a 'Otros'.
    La categoría 'Otros' está protegida y no puede eliminarse.
    """
    nombre = nombre.strip()

    # Proteger la categoría 'Otros' de ser eliminada
    if nombre == "Otros":
        return {"ok": False, "mensaje": "La categoría 'Otros' no puede eliminarse"}

    # Reasignar gastos existentes a 'Otros' antes de eliminar
    execute(
        "UPDATE gastos SET Categoria = 'Otros' WHERE Categoria = %s AND Tenant_ID = %s",
        (nombre, tenant_id)
    )

    execute(
        "DELETE FROM gastos_categorias WHERE nombre = %s AND tenant_id = %s",
        (nombre, tenant_id)
    )

    return {"ok": True, "categoria_eliminada": nombre}


def get_gastos(tenant_id: str) -> list[dict]:
    """Lee todos los gastos ordenados por fecha descendente."""
    return query(
        "SELECT id, Fecha, Categoria, Descripcion, Monto, Estado, Gasto_Programado_ID "
        "FROM gastos WHERE Tenant_ID = %s ORDER BY Fecha DESC",
        (tenant_id,)
    )


def insertar_gasto(
    fecha: str,
    categoria: str,
    descripcion: str,
    monto: float,
    tenant_id: str,
    estado: str = "pagado",
    gasto_programado_id: str | None = None
) -> dict:
    """Inserta un nuevo gasto."""
    cat = categoria.strip()
    desc = descripcion.strip()
    execute(
        "INSERT INTO gastos (Fecha, Categoria, Descripcion, Monto, Tenant_ID, Estado, Gasto_Programado_ID) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (fecha, cat, desc, monto, tenant_id, estado, gasto_programado_id)
    )
    return {"ok": True}


def eliminar_gasto(gasto_id: int, tenant_id: str) -> dict:
    """Elimina un gasto por id."""
    execute("DELETE FROM gastos WHERE id=%s AND Tenant_ID = %s", (gasto_id, tenant_id))
    return {"ok": True, "id": gasto_id}


def confirmar_gasto(gasto_id: int, tenant_id: str) -> dict:
    """Cambia el estado del gasto a 'pagado'."""
    execute(
        "UPDATE gastos SET Estado = 'pagado' WHERE id = %s AND Tenant_ID = %s",
        (gasto_id, tenant_id)
    )
    return {"ok": True, "id": gasto_id, "estado": "pagado"}


def descartar_gasto(gasto_id: int, tenant_id: str) -> dict:
    """Cambia el estado del gasto a 'descartado'."""
    execute(
        "UPDATE gastos SET Estado = 'descartado' WHERE id = %s AND Tenant_ID = %s",
        (gasto_id, tenant_id)
    )
    return {"ok": True, "id": gasto_id, "estado": "descartado"}


def actualizar_gasto(gasto_id: int, monto: float, categoria: str, descripcion: str, tenant_id: str) -> dict:
    """Actualiza monto, categoría y descripción de un gasto existente."""
    execute(
        "UPDATE gastos SET Monto = %s, Categoria = %s, Descripcion = %s WHERE id = %s AND Tenant_ID = %s",
        (monto, categoria.strip(), descripcion.strip(), gasto_id, tenant_id)
    )
    return {"ok": True, "id": gasto_id, "monto": monto, "categoria": categoria.strip(), "descripcion": descripcion.strip()}