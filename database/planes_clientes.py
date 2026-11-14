# ==============================================================================
# backend/database/planes_clientes.py
# "Mis cuentas" (giro 'servicios', migración 075): registro MANUAL de qué plan
# del gestor tiene cada cliente y si se le está cobrando.
#
# Es un cuaderno de control personal, NO un panel administrativo del gestor:
# - Nunca genera cobros ni toca dinero (eso vive en cuotas_clientes/cobros).
# - El dueño lo llena a mano: "Le di el Plan Plus a Alejandra, de momento gratis".
#
# Reglas clave (iguales que el resto del backend):
# - El tenant_id SIEMPRE llega del JWT; aquí solo se usa como filtro.
# - Contrato de dominio: {"ok": True, ...} en éxito; {"ok": False,
#   "tipo": "validacion"|"no_encontrado", "mensaje": ...} si algo falla.
# - Alta/edición/baja se registran en el Historial (database/actividad.py).
# ==============================================================================

from datetime import date

from database.conexion import execute, query
from database.actividad import registrar_actividad

ESTADOS_COBRO = ("cobrando", "gratis", "pausado")

_SELECT = """
SELECT p.id, p.tenant_id, p.cliente_id, p.plan, p.estado_cobro,
       p.monto_acordado, p.otorgado, p.notas, p.creado_en,
       cl.nombre AS cliente_nombre
FROM clientes_planes p
JOIN clientes cl ON cl.id = p.cliente_id
"""


def _plan_por_id(tenant_id: str, plan_id: str) -> dict | None:
    filas = query(_SELECT + " WHERE p.tenant_id = %s AND p.id = %s", (tenant_id, plan_id))
    return filas[0] if filas else None


# ── Listado ───────────────────────────────────────────────────────────────────

def listar_planes(tenant_id: str, estado: str | None = None) -> list[dict]:
    """Cuaderno completo: quién tiene qué plan y si se le cobra.
    Con `estado` ('cobrando'|'gratis'|'pausado') filtra una vista."""
    where = " WHERE p.tenant_id = %s"
    params: list = [tenant_id]
    if estado:
        where += " AND p.estado_cobro = %s"
        params.append(estado)
    filas = query(_SELECT + where + " ORDER BY cl.nombre, p.creado_en DESC", tuple(params))
    for f in filas:
        if f.get("otorgado"):
            f["otorgado"] = f["otorgado"].isoformat()
        if f.get("creado_en"):
            f["creado_en"] = f["creado_en"].isoformat()
        if f.get("monto_acordado") is not None:
            f["monto_acordado"] = round(float(f["monto_acordado"]), 2)
    return filas


# ── Alta / edición / baja ─────────────────────────────────────────────────────

def crear_plan(
    tenant_id: str, cliente_id: str, plan: str,
    estado_cobro: str = "cobrando", monto_acordado: float | None = None,
    otorgado: date | None = None, notas: str | None = None,
) -> dict:
    plan_n = (plan or "").strip()
    if not plan_n:
        return {"ok": False, "tipo": "validacion", "mensaje": "El nombre del plan es obligatorio"}
    if estado_cobro not in ESTADOS_COBRO:
        return {"ok": False, "tipo": "validacion",
                "mensaje": "El estado debe ser cobrando, gratis o pausado"}
    if monto_acordado is not None and float(monto_acordado) < 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "El monto acordado no puede ser negativo"}
    if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}

    filas = query(
        "INSERT INTO clientes_planes (tenant_id, cliente_id, plan, estado_cobro, "
        "monto_acordado, otorgado, notas) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, cliente_id, plan_n, estado_cobro,
         round(float(monto_acordado), 2) if monto_acordado is not None else None,
         otorgado or date.today(), (notas or "").strip() or None)
    )
    registro = _plan_por_id(tenant_id, filas[0]["id"])
    # Registro de actividad: alta del plan otorgado.
    try:
        registrar_actividad(
            tenant_id, "plan", "plan_cliente", "crear",
            resumen=f"Anotó el plan {plan_n} de {registro['cliente_nombre']}"
                    f" ({'sin cobro' if estado_cobro == 'gratis' else estado_cobro})",
            entidad_id=filas[0]["id"],
            detalle={"plan": plan_n, "estado_cobro": estado_cobro},
        )
    except Exception:
        pass
    return {"ok": True, "plan": registro}


def actualizar_plan(
    tenant_id: str, plan_id: str,
    cliente_id: str | None = None, plan: str | None = None,
    estado_cobro: str | None = None, monto_acordado: float | None = None,
    otorgado: date | None = None, notas: str | None = None,
) -> dict:
    """PATCH parcial. monto_acordado: 0 = quitarlo (plan sin monto, ej. gratis)."""
    actual = query(
        "SELECT id, cliente_id, plan, estado_cobro, monto_acordado, otorgado, notas "
        "FROM clientes_planes WHERE tenant_id = %s AND id = %s",
        (tenant_id, plan_id)
    )
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Registro no encontrado"}
    actual = actual[0]

    sets, params = [], []
    if cliente_id is not None:
        if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
            return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
        sets.append("cliente_id = %s")
        params.append(cliente_id)
    if plan is not None:
        if not plan.strip():
            return {"ok": False, "tipo": "validacion", "mensaje": "El nombre del plan no puede quedar vacío"}
        sets.append("plan = %s")
        params.append(plan.strip())
    if estado_cobro is not None:
        if estado_cobro not in ESTADOS_COBRO:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": "El estado debe ser cobrando, gratis o pausado"}
        sets.append("estado_cobro = %s")
        params.append(estado_cobro)
    if monto_acordado is not None:
        if float(monto_acordado) < 0:
            return {"ok": False, "tipo": "validacion", "mensaje": "El monto acordado no puede ser negativo"}
        # Sentinel de la UI: 0 = quitar el monto (NULL); >0 = fijarlo.
        sets.append("monto_acordado = %s")
        params.append(round(float(monto_acordado), 2) if float(monto_acordado) > 0 else None)
    if otorgado is not None:
        sets.append("otorgado = %s")
        params.append(otorgado)
    if notas is not None:
        sets.append("notas = %s")
        params.append((notas or "").strip() or None)

    if sets:
        params += [tenant_id, plan_id]
        execute(f"UPDATE clientes_planes SET {', '.join(sets)} WHERE tenant_id = %s AND id = %s", tuple(params))
        # Registro de actividad: diff de la edición.
        try:
            diff: dict = {}
            if plan is not None and plan.strip() != actual["plan"]:
                diff["plan"] = [actual["plan"], plan.strip()]
            if estado_cobro is not None and estado_cobro != actual["estado_cobro"]:
                diff["estado_cobro"] = [actual["estado_cobro"], estado_cobro]
            if monto_acordado is not None:
                viejo = float(actual["monto_acordado"]) if actual["monto_acordado"] is not None else None
                nuevo = round(float(monto_acordado), 2) if float(monto_acordado) > 0 else None
                if viejo != nuevo:
                    diff["monto_acordado"] = [viejo, nuevo]
            if notas is not None and ((notas or "").strip() or None) != actual["notas"]:
                diff["notas"] = [actual["notas"], (notas or "").strip() or None]
            if diff:
                registro = _plan_por_id(tenant_id, plan_id)
                registrar_actividad(
                    tenant_id, "plan", "plan_cliente", "editar",
                    resumen=f"Editó el plan {plan if plan is not None else actual['plan']}"
                            f" de {registro['cliente_nombre'] if registro else '?'}",
                    entidad_id=plan_id, detalle=diff,
                )
        except Exception:
            pass
    return {"ok": True, "plan": _plan_por_id(tenant_id, plan_id)}


def eliminar_plan(tenant_id: str, plan_id: str) -> dict:
    """Borrado duro: es un cuaderno personal, no un ledger financiero.
    (La cuota y sus cobros, si existen, no se tocan aquí.)"""
    filas = query(
        "DELETE FROM clientes_planes WHERE tenant_id = %s AND id = %s RETURNING plan",
        (tenant_id, plan_id)
    )
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Registro no encontrado"}
    try:
        registrar_actividad(
            tenant_id, "plan", "plan_cliente", "eliminar",
            resumen=f"Borró la anotación del plan {filas[0]['plan']}",
            entidad_id=plan_id,
        )
    except Exception:
        pass
    return {"ok": True, "mensaje": f"Anotación del plan '{filas[0]['plan']}' eliminada"}
