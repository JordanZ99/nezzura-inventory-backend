# ==============================================================================
# backend/database/ordenes.py
# Órdenes de servicio (giro 'servicios', migración 077): el registro del
# TRABAJO — reparaciones, instalaciones, consultoría, mantenimiento o lo que
# salga. El dinero no vive aquí: si se cobra, el cobro/cuota se crea aparte
# (cobros o cuotas_clientes) y `cobro_id` queda enlazado por trazabilidad.
#
# Reglas clave (iguales que el resto del backend):
# - El tenant_id SIEMPRE llega del JWT; aquí solo se usa como filtro.
# - Contrato de dominio: {"ok": True, ...} en éxito; {"ok": False,
#   "tipo": "validacion"|"no_encontrado", "mensaje": ...} si algo falla.
# - Al pasar a 'entregado', el backend sella fecha_entregado (si no traía una).
# - Altas/ediciones/bajas en el Historial (dominio 'orden', mig 075).
# ==============================================================================

from datetime import date

from database.conexion import execute, query
from database.actividad import registrar_actividad

TIPOS = ("reparacion", "instalacion", "consultoria", "mantenimiento", "otro")
# Flujo del trabajo (patrón de los sistemas de reparación): intake → en proceso
# → esperando (cliente/insumo/aprobación) → listo → entregado (+ cancelado).
ESTADOS = ("recibido", "en_proceso", "esperando", "listo", "entregado", "cancelado")

_SELECT = """
SELECT o.id, o.tenant_id, o.cliente_id, o.tipo, o.titulo, o.equipo,
       o.detalle_reportado, o.solucion, o.estado, o.fecha_recibido,
       o.fecha_entregado, o.precio, o.cobro_id, o.cuota_id, o.notas, o.creado_en,
       cl.nombre AS cliente_nombre,
       -- El enlace puede ser un COBRO (puntual) o una CUOTA (venta a plazos):
       -- se resuelve el que matchee y su saldo real.
       CASE WHEN b.id IS NOT NULL THEN b.concepto
            WHEN q.id IS NOT NULL THEN q.concepto END AS cobro_concepto,
       CASE
         WHEN b.id IS NOT NULL THEN
           b.monto - COALESCE((SELECT SUM((p->>'monto')::numeric)
                     FROM jsonb_array_elements(b.pagos) AS p), 0)
         WHEN q.id IS NOT NULL AND q.monto_meta IS NOT NULL THEN
           q.monto_meta - COALESCE((SELECT SUM((pg->>'monto')::numeric)
                     FROM cobros y, jsonb_array_elements(y.pagos) AS pg
                    WHERE y.cuota_id = q.id), 0)
       END AS cobro_saldo
FROM ordenes_servicio o
LEFT JOIN clientes cl ON cl.id = o.cliente_id
LEFT JOIN cobros b ON b.id = o.cobro_id
LEFT JOIN cuotas_clientes q ON q.id = o.cuota_id
"""


def _orden_por_id(tenant_id: str, orden_id: str) -> dict | None:
    filas = query(_SELECT + " WHERE o.tenant_id = %s AND o.id = %s", (tenant_id, orden_id))
    return filas[0] if filas else None


def _normalizar(fila: dict) -> dict:
    for k in ("fecha_recibido", "fecha_entregado"):
        if fila.get(k):
            fila[k] = fila[k].isoformat()
    if fila.get("creado_en"):
        fila["creado_en"] = fila["creado_en"].isoformat()
    if fila.get("precio") is not None:
        fila["precio"] = round(float(fila["precio"]), 2)
    if fila.get("cobro_saldo") is not None:
        fila["cobro_saldo"] = round(float(fila["cobro_saldo"]), 2)
    return fila


# ── Listado ───────────────────────────────────────────────────────────────────

def listar_ordenes(
    tenant_id: str,
    estado: str | None = None,
    cliente_id: str | None = None,
    solo_abiertas: bool = False,
) -> list[dict]:
    """Órdenes del negocio. `solo_abiertas` = TODO menos entregado/cancelado
    (la cola de trabajo vivo). Recibidas primero (la cola), luego por fecha."""
    where = " WHERE o.tenant_id = %s"
    params: list = [tenant_id]
    if estado:
        where += " AND o.estado = %s"
        params.append(estado)
    if cliente_id:
        where += " AND o.cliente_id = %s"
        params.append(cliente_id)
    if solo_abiertas:
        where += " AND o.estado NOT IN ('entregado', 'cancelado')"
    filas = query(
        _SELECT + where + " ORDER BY "
        "(o.estado IN ('entregado', 'cancelado')), o.fecha_recibido DESC, o.creado_en DESC",
        tuple(params)
    )
    return [_normalizar(f) for f in filas]


# ── Alta / edición / baja ─────────────────────────────────────────────────────

def crear_orden(
    tenant_id: str, titulo: str, tipo: str = "otro",
    cliente_id: str | None = None, equipo: str | None = None,
    detalle_reportado: str | None = None, solucion: str | None = None,
    estado: str = "recibido", fecha_recibido: date | None = None,
    precio: float | None = None, notas: str | None = None,
) -> dict:
    titulo_n = (titulo or "").strip()
    if not titulo_n:
        return {"ok": False, "tipo": "validacion", "mensaje": "El título del trabajo es obligatorio"}
    if tipo not in TIPOS:
        return {"ok": False, "tipo": "validacion",
                "mensaje": f"El tipo debe ser uno de: {', '.join(TIPOS)}"}
    if estado not in ESTADOS:
        return {"ok": False, "tipo": "validacion",
                "mensaje": f"El estado debe ser uno de: {', '.join(ESTADOS)}"}
    if cliente_id and not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
    precio_n = round(float(precio), 2) if precio is not None else None
    if precio_n is not None and precio_n < 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "El precio no puede ser negativo"}

    filas = query(
        "INSERT INTO ordenes_servicio (tenant_id, cliente_id, tipo, titulo, equipo, "
        "detalle_reportado, solucion, estado, fecha_recibido, precio, notas) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, cliente_id, tipo, titulo_n, (equipo or "").strip() or None,
         (detalle_reportado or "").strip() or None, (solucion or "").strip() or None,
         estado, fecha_recibido or date.today(), precio_n, (notas or "").strip() or None)
    )
    orden = _orden_por_id(tenant_id, filas[0]["id"])
    # Registro de actividad (062/075): alta de orden.
    try:
        quien = f" de {orden['cliente_nombre']}" if orden and orden.get("cliente_nombre") else ""
        registrar_actividad(
            tenant_id, "orden", "orden_servicio", "crear",
            resumen=f"Abrió la orden '{titulo_n}' ({tipo}){quien}",
            entidad_id=filas[0]["id"],
            detalle={"tipo": tipo, "estado": estado,
                     **({"precio": precio_n} if precio_n is not None else {})},
        )
    except Exception:
        pass
    return {"ok": True, "orden": _normalizar(orden)}


def actualizar_orden(
    tenant_id: str, orden_id: str,
    cliente_id: str | None = None, tipo: str | None = None,
    titulo: str | None = None, equipo: str | None = None,
    detalle_reportado: str | None = None, solucion: str | None = None,
    estado: str | None = None, fecha_recibido: date | None = None,
    fecha_entregado: date | str | None = None,
    precio: float | None = None, cobro_id: str | None = None,
    cuota_id: str | None = None, notas: str | None = None,
) -> dict:
    """PATCH parcial. `estado` es la única operación con lógica extra: al pasar
    a 'entregado' sin fecha explícita se sella HOY; salir de 'entregado' la
    limpia. cobro_id/cuota_id: enlace de trazabilidad — el cobro PUNTUAL va en
    cobro_id y la VENTA A PLAZOS (cuota) en cuota_id (078)."""
    actual = query(
        "SELECT id, cliente_id, tipo, titulo, estado, fecha_recibido, fecha_entregado, "
        "precio, cobro_id, cuota_id, notas FROM ordenes_servicio WHERE tenant_id = %s AND id = %s",
        (tenant_id, orden_id)
    )
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Orden no encontrada"}
    actual = actual[0]

    sets, params = [], []
    if cliente_id is not None:
        if cliente_id and not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
            return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
        sets.append("cliente_id = %s")
        params.append(cliente_id or None)
    if tipo is not None:
        if tipo not in TIPOS:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": f"El tipo debe ser uno de: {', '.join(TIPOS)}"}
        sets.append("tipo = %s")
        params.append(tipo)
    if titulo is not None:
        if not titulo.strip():
            return {"ok": False, "tipo": "validacion", "mensaje": "El título no puede quedar vacío"}
        sets.append("titulo = %s")
        params.append(titulo.strip())
    if equipo is not None:
        sets.append("equipo = %s")
        params.append((equipo or "").strip() or None)
    if detalle_reportado is not None:
        sets.append("detalle_reportado = %s")
        params.append((detalle_reportado or "").strip() or None)
    if solucion is not None:
        sets.append("solucion = %s")
        params.append((solucion or "").strip() or None)

    # ── Estado: transición con side-effects de fecha ──
    if estado is not None:
        if estado not in ESTADOS:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": f"El estado debe ser uno de: {', '.join(ESTADOS)}"}
        sets.append("estado = %s")
        params.append(estado)
        if estado == "entregado" and not fecha_entregado:
            sets.append("fecha_entregado = CURRENT_DATE")
        elif estado not in ("entregado",) and actual["estado"] == "entregado":
            # Reabierta: la fecha de entrega pierde sentido.
            sets.append("fecha_entregado = NULL")
    if fecha_entregado is not None:
        # '' = quitarla; 'YYYY-MM-DD' = fijarla (misma semántica CRM).
        sets.append("fecha_entregado = %s")
        params.append(None if fecha_entregado == "" else fecha_entregado)
    if fecha_recibido is not None:
        sets.append("fecha_recibido = %s")
        params.append(fecha_recibido)
    if precio is not None:
        precio_nuevo = round(float(precio), 2)
        if precio_nuevo < 0:
            return {"ok": False, "tipo": "validacion", "mensaje": "El precio no puede ser negativo"}
        # Sentinel de la UI: 0 = quitar el precio; >0 = fijarlo.
        sets.append("precio = %s")
        params.append(precio_nuevo if precio_nuevo > 0 else None)
    if cobro_id is not None:
        # Cobro PUNTUAL enlazado. ""/None = quitar el enlace; y al enlazar uno
        # se suelta el otro (no pueden convivir).
        sets.append("cobro_id = %s")
        params.append(cobro_id or None)
        if cobro_id:
            sets.append("cuota_id = NULL")
    if cuota_id is not None:
        # VENTA A PLAZOS (cuota) enlazada — migración 078.
        sets.append("cuota_id = %s")
        params.append(cuota_id or None)
        if cuota_id:
            sets.append("cobro_id = NULL")
    if notas is not None:
        sets.append("notas = %s")
        params.append((notas or "").strip() or None)

    if sets:
        params += [tenant_id, orden_id]
        execute(f"UPDATE ordenes_servicio SET {', '.join(sets)} WHERE tenant_id = %s AND id = %s", tuple(params))
        # Registro de actividad (062): diff de la edición.
        try:
            diff: dict = {}
            if estado is not None and estado != actual["estado"]:
                diff["estado"] = [actual["estado"], estado]
            if titulo is not None and titulo.strip() != actual["titulo"]:
                diff["titulo"] = [actual["titulo"], titulo.strip()]
            if precio is not None:
                viejo = float(actual["precio"]) if actual["precio"] is not None else None
                nuevo = round(float(precio), 2) if float(precio) > 0 else None
                if viejo != nuevo:
                    diff["precio"] = [viejo, nuevo]
            if cobro_id is not None and (cobro_id or None) != actual["cobro_id"]:
                diff["cobro"] = [str(actual["cobro_id"]) if actual["cobro_id"] else None,
                                 cobro_id or None]
            if cuota_id is not None and (cuota_id or None) != actual["cuota_id"]:
                diff["cuota"] = [str(actual["cuota_id"]) if actual["cuota_id"] else None,
                                 cuota_id or None]
            if diff:
                registrar_actividad(
                    tenant_id, "orden", "orden_servicio", "editar",
                    resumen=f"Editó la orden '{titulo if titulo is not None else actual['titulo']}'",
                    entidad_id=orden_id, detalle=diff,
                )
        except Exception:
            pass
    return {"ok": True, "orden": _normalizar(_orden_por_id(tenant_id, orden_id))}


def eliminar_orden(tenant_id: str, orden_id: str) -> dict:
    """Borrado duro de la orden (es un registro de trabajo, no un ledger).
    El cobro enlazado NO se toca (solo pierde el enlace)."""
    filas = query(
        "DELETE FROM ordenes_servicio WHERE tenant_id = %s AND id = %s RETURNING titulo",
        (tenant_id, orden_id)
    )
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Orden no encontrada"}
    try:
        registrar_actividad(
            tenant_id, "orden", "orden_servicio", "eliminar",
            resumen=f"Eliminó la orden '{filas[0]['titulo']}'",
            entidad_id=orden_id,
        )
    except Exception:
        pass
    return {"ok": True, "mensaje": f"Orden '{filas[0]['titulo']}' eliminada"}
