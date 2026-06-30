# ==============================================================================
# backend/database/gastos.py
# CRUD sobre la tabla gastos.
# ==============================================================================

from database.conexion import query, execute


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