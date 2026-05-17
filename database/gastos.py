# ==============================================================================
# backend/database/gastos.py
# CRUD sobre la tabla gastos.
# ==============================================================================

from database.conexion import query, execute


def get_gastos(tenant_id: str) -> list[dict]:
    """Lee todos los gastos ordenados por fecha descendente."""
    return query("SELECT * FROM gastos WHERE Tenant_ID = %s ORDER BY Fecha DESC", (tenant_id,))


def insertar_gasto(fecha: str, categoria: str, descripcion: str, monto: float, tenant_id: str) -> dict:
    """Inserta un nuevo gasto."""
    cat = categoria.strip()
    desc = descripcion.strip()
    execute(
        "INSERT INTO gastos (Fecha, Categoria, Descripcion, Monto, Tenant_ID) VALUES (%s, %s, %s, %s, %s)",
        (fecha, cat, desc, monto, tenant_id)
    )
    return {"ok": True}


def eliminar_gasto(gasto_id: int, tenant_id: str) -> dict:
    """Elimina un gasto por id."""
    execute("DELETE FROM gastos WHERE id=%s AND Tenant_ID = %s", (gasto_id, tenant_id))
    return {"ok": True, "id": gasto_id}