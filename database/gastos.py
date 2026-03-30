# ==============================================================================
# backend/database/gastos.py
# CRUD sobre la tabla gastos.
# ==============================================================================

from database.conexion import query, execute


def get_gastos() -> list[dict]:
    """Lee todos los gastos ordenados por fecha descendente."""
    return query("SELECT * FROM gastos ORDER BY Fecha DESC")


def insertar_gasto(fecha: str, categoria: str, descripcion: str, monto: float) -> dict:
    """Inserta un nuevo gasto."""
    cat = categoria.strip()
    desc = descripcion.strip()
    execute(
        "INSERT INTO gastos (Fecha, Categoria, Descripcion, Monto) VALUES (%s, %s, %s, %s)",
        (fecha, cat, desc, monto)
    )
    return {"ok": True}


def eliminar_gasto(gasto_id: int) -> dict:
    """Elimina un gasto por id."""
    execute("DELETE FROM gastos WHERE id=%s", (gasto_id,))
    return {"ok": True, "id": gasto_id}