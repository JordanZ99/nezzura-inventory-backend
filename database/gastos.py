# ==============================================================================
# backend/database/gastos.py
# CRUD sobre la tabla gastos.
# ==============================================================================

from database.conexion import query, execute, DEFAULT_TENANT_ID


def get_gastos(tenant_id: str = DEFAULT_TENANT_ID) -> list[dict]:
    """Lee todos los gastos ordenados por fecha descendente y filtrados por tenant."""
    return query("SELECT * FROM gastos WHERE tenant_id = %s ORDER BY Fecha DESC", (tenant_id,))


def insertar_gasto(fecha: str, categoria: str, descripcion: str, monto: float, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """Inserta un nuevo gasto vinculado a un tenant."""
    cat = categoria.strip()
    desc = descripcion.strip()
    execute(
        "INSERT INTO gastos (Fecha, Categoria, Descripcion, Monto, tenant_id) VALUES (%s, %s, %s, %s, %s)",
        (fecha, cat, desc, monto, tenant_id)
    )
    return {"ok": True}


def eliminar_gasto(gasto_id: int, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    """Elimina un gasto por id y tenant."""
    execute("DELETE FROM gastos WHERE id=%s AND tenant_id=%s", (gasto_id, tenant_id))
    return {"ok": True, "id": gasto_id}