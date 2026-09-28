# ==============================================================================
# backend/database/terminales.py
# Terminales bancarias del negocio con sus comisiones reales (Fase B).
# Cada terminal guarda % de débito, % de crédito y cuota fija: el cobro calcula
# la comisión con la tarifa de la terminal usada.
# ==============================================================================

from database.conexion import query, execute


def listar_terminales(tenant_id: str, solo_activas: bool = False) -> list[dict]:
    filtro = "AND activo = true" if solo_activas else ""
    return query(
        "SELECT id, nombre, banco, comision_debito_pct, comision_credito_pct, "
        "       comision_fija, activo "
        f"FROM terminales WHERE tenant_id = %s {filtro} ORDER BY nombre ASC",
        (tenant_id,)
    )


def crear_terminal(tenant_id: str, nombre: str, banco: str | None,
                   comision_debito_pct: float, comision_credito_pct: float,
                   comision_fija: float) -> dict:
    nombre = nombre.strip()
    if not nombre:
        return {"ok": False, "mensaje": "El nombre de la terminal es obligatorio"}
    existe = query(
        "SELECT 1 FROM terminales WHERE tenant_id = %s AND nombre = %s",
        (tenant_id, nombre)
    )
    if existe:
        return {"ok": False, "mensaje": f"Ya existe una terminal llamada '{nombre}'"}
    execute(
        "INSERT INTO terminales (tenant_id, nombre, banco, comision_debito_pct, "
        "                       comision_credito_pct, comision_fija) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (tenant_id, nombre, (banco or "").strip() or None,
         float(comision_debito_pct or 0), float(comision_credito_pct or 0),
         float(comision_fija or 0))
    )
    return {"ok": True, "nombre": nombre}


def actualizar_terminal(terminal_id: str, tenant_id: str, nombre: str | None = None,
                        banco: str | None = None, comision_debito_pct: float | None = None,
                        comision_credito_pct: float | None = None,
                        comision_fija: float | None = None,
                        activo: bool | None = None) -> dict:
    """Actualiza solo los campos enviados (PATCH parcial)."""
    campos, valores = [], []
    if nombre is not None:
        n = nombre.strip()
        if not n:
            return {"ok": False, "mensaje": "El nombre no puede estar vacío"}
        campos.append("nombre = %s"); valores.append(n)
    if banco is not None:
        campos.append("banco = %s"); valores.append(banco.strip() or None)
    if comision_debito_pct is not None:
        campos.append("comision_debito_pct = %s"); valores.append(float(comision_debito_pct))
    if comision_credito_pct is not None:
        campos.append("comision_credito_pct = %s"); valores.append(float(comision_credito_pct))
    if comision_fija is not None:
        campos.append("comision_fija = %s"); valores.append(float(comision_fija))
    if activo is not None:
        campos.append("activo = %s"); valores.append(bool(activo))
    if not campos:
        return {"ok": False, "mensaje": "Nada que actualizar"}
    valores.extend([terminal_id, tenant_id])
    filas = query(
        f"UPDATE terminales SET {', '.join(campos)} "
        "WHERE id = %s::uuid AND tenant_id = %s RETURNING id",
        tuple(valores)
    )
    if not filas:
        return {"ok": False, "mensaje": "Terminal no encontrada"}
    return {"ok": True, "id": terminal_id}


def eliminar_terminal(terminal_id: str, tenant_id: str) -> dict:
    filas = query(
        "DELETE FROM terminales WHERE id = %s::uuid AND tenant_id = %s RETURNING id",
        (terminal_id, tenant_id)
    )
    if not filas:
        return {"ok": False, "mensaje": "Terminal no encontrada"}
    return {"ok": True, "id": terminal_id}
