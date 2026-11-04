# ==============================================================================
# backend/database/cobros.py
# Cobros del giro 'servicios': mensualidades por cliente (cuotas) y trabajos
# extra (dev/consultoría/lo que salga). Migración 061.
#
# Reglas clave (iguales que el resto del backend):
# - El tenant_id SIEMPRE llega del JWT; aquí solo se usa como filtro.
# - El saldo NUNCA se almacena: se deriva de monto - SUM(pagos) (ledgers, como
#   el saldo de puntos vive en puntos_movimientos y las ventas en pagos JSONB).
# - El estado (pendiente|parcial|pagado) y el flag vencido también derivan:
#   estado del cruce monto↔pagos, vencido de la fecha/vencimiento vs hoy.
# - El generador de periodos es IDEMPOTENTE: re-generar el mismo mes no duplica.
# ==============================================================================

import json
import re
from datetime import date

import psycopg2.extras

from database.conexion import execute, query

_EPSILON = 0.005  # tolerancia de centavos para 'saldo 0' (floats + NUMERIC)
_REGEX_PERIODO = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Las filas se leen con el saldo ya calculado en SQL (CORRelated subquery por
# cobro — el volumen de un giro de servicios es pequeño, no hace falta CTE).
_SELECT = """
SELECT b.id, b.tenant_id, b.cliente_id, b.cuota_id, b.origen, b.concepto,
       b.periodo, b.monto, b.vencimiento, b.programa, b.notas, b.pagos, b.creado_en,
       cl.nombre AS cliente_nombre,
       COALESCE((SELECT SUM((p->>'monto')::numeric)
                 FROM jsonb_array_elements(b.pagos) AS p), 0) AS pagado,
       b.monto - COALESCE((SELECT SUM((p->>'monto')::numeric)
                 FROM jsonb_array_elements(b.pagos) AS p), 0) AS saldo
FROM cobros b
JOIN clientes cl ON cl.id = b.cliente_id
"""


def _derivar(fila: dict) -> dict:
    """Estado y vencido derivados (nunca almacenados): la única verdad es
    monto↔pagos y las fechas comprometidas. Con campos SQL (pagado/saldo) usa
    los volidos de la BDD; sin ellos, los reconstruye del JSONB (fallback
    para pruebas y llamadas directas)."""
    pagos = fila.get("pagos") or []
    pagado = fila.get("pagado")
    if pagado is None:
        pagado = sum(float(p.get("monto") or 0) for p in pagos)
    pagado = float(pagado)
    monto = float(fila.get("monto") or 0)
    saldo = fila.get("saldo")
    saldo = float(saldo) if saldo is not None else monto - pagado

    if saldo <= _EPSILON:
        estado = "pagado"
    elif pagado > _EPSILON:
        estado = "parcial"
    else:
        estado = "pendiente"
    fila["estado"] = estado

    vencido = False
    dias_vencido = 0
    if estado != "pagado":
        ref = fila.get("vencimiento") or (
            fila["creado_en"].date() if fila.get("creado_en") else None
        )
        if isinstance(ref, str):
            ref = date.fromisoformat(ref[:10])
        if ref and ref < date.today():
            vencido = True
            dias_vencido = (date.today() - ref).days
    fila["vencido"] = vencido
    fila["dias_vencido"] = dias_vencido

    fila["pagos"] = pagos
    fila["programa"] = fila.get("programa") or []
    fila["saldo"] = round(max(saldo, 0), 2)
    fila["pagado"] = round(pagado, 2)
    fila["monto"] = round(monto, 2)
    return fila


def _cobro_por_id(tenant_id: str, cobro_id: str) -> dict | None:
    filas = query(_SELECT + " WHERE b.tenant_id = %s AND b.id = %s", (tenant_id, cobro_id))
    return _derivar(filas[0]) if filas else None


# ── Listado ───────────────────────────────────────────────────────────────────

def listar_cobros(
    tenant_id: str,
    periodo: str | None = None,
    cliente_id: str | None = None,
    solo_pendientes: bool = False,
) -> list[dict]:
    """Lista de cobros (incluye pagados; el front filtra lo que muestre).
    Pendientes primero por antigüedad de vencimiento (AR aging visual)."""
    where = " WHERE b.tenant_id = %s"
    params: list = [tenant_id]
    if periodo:
        where += " AND b.periodo = %s"
        params.append(periodo)
    if cliente_id:
        where += " AND b.cliente_id = %s"
        params.append(cliente_id)
    if solo_pendientes:
        where += (" AND (b.monto - COALESCE((SELECT SUM((p->>'monto')::numeric) "
                  "FROM jsonb_array_elements(b.pagos) AS p), 0)) > %s")
        params.append(_EPSILON)
    filas = query(_SELECT + where + " ORDER BY b.vencimiento ASC NULLS LAST, b.creado_en DESC", tuple(params))
    return [_derivar(f) for f in filas]


# ── Resumen (dashboard financiero) ────────────────────────────────────────────

def resumen(tenant_id: str, periodo: str | None = None) -> dict:
    """KPIs del home: pendiente total por antigüedad (AR aging buckets
    0-30/31-60/61-90/+90 días de VENCIDO), cobrado del periodo, deudores y
    los próximos pagos por vencer (no vencidos, vencimiento ascendente)."""
    if not periodo:
        hoy = date.today()
        periodo = f"{hoy.year}-{hoy.month:02d}"

    cobros = listar_cobros(tenant_id)  # todos; el aging necesita también pagados
    hoy = date.today()

    pendiente_total = 0.0
    num_pendientes = 0
    aging = {"actual": 0.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "mas90": 0.0}
    cobrado_periodo = 0.0
    deudores: dict[str, dict] = {}
    proximos: list[dict] = []  # próximos pagos por vencer (card del dashboard)

    for c in cobros:
        # Dinero entrado en el periodo: los pagos cuya fecha cae en ese mes
        # (de cualquier cobro, pagado o parcial — así es el ingreso real).
        for p in c["pagos"]:
            fecha_pago = str(p.get("fecha") or "")
            if fecha_pago[:7] == periodo:
                cobrado_periodo += float(p.get("monto") or 0)

        if c["estado"] == "pagado":
            continue
        pendiente_total += c["saldo"]
        num_pendientes += 1

        d = deudores.setdefault(
            c["cliente_id"],
            {"cliente_id": c["cliente_id"], "nombre": c["cliente_nombre"], "saldo": 0.0, "cobros_pendientes": 0},
        )
        d["saldo"] += c["saldo"]
        d["cobros_pendientes"] += 1

        ref = c.get("vencimiento") or (
            c["creado_en"].date() if c.get("creado_en") else None
        )
        if isinstance(ref, str):
            ref = date.fromisoformat(ref[:10])
        if c["vencido"] and ref:
            dias = (hoy - ref).days
            bucket = "d30" if dias <= 30 else ("d60" if dias <= 60 else ("d90" if dias <= 90 else "mas90"))
            aging[bucket] += c["saldo"]
        else:
            aging["actual"] += c["saldo"]

        # Próximos pagos: solo los AÚN NO vencidos (los vencidos viven en el
        # aging y en deudores). El dashboard muestra los 6 más cercanos.
        if not c["vencido"]:
            proximos.append({
                "cliente_id": c["cliente_id"],
                "cliente_nombre": c["cliente_nombre"],
                "concepto": c["concepto"],
                "saldo": c["saldo"],
                "vencimiento": ref.isoformat() if ref else None,
                "vencido": False,
                "dias_vencido": 0,
            })

    deudores_list = sorted(deudores.values(), key=lambda d: -d["saldo"])
    for d in deudores_list:
        d["saldo"] = round(d["saldo"], 2)

    proximos_list = sorted(
        proximos,
        key=lambda p: (p["vencimiento"] is None, p["vencimiento"] or "9999-12-31"),
    )[:6]

    return {
        "periodo": periodo,
        "pendiente_total": round(pendiente_total, 2),
        "num_pendientes": num_pendientes,
        "cobrado_periodo": round(cobrado_periodo, 2),
        "aging": {k: round(v, 2) for k, v in aging.items()},
        "deudores": deudores_list,
        "proximos": proximos_list,
    }


# ── Generador de periodos (cuotas → cobros del mes) ──────────────────────────

def generar_cobros(tenant_id: str, periodo: str) -> dict:
    """Crea el cobro del periodo para cada cuota activa. Idempotente: si el
    cobro ya existe (índice parcial único), no se duplica."""
    if not _REGEX_PERIODO.match(periodo or ""):
        return {"ok": False, "tipo": "validacion", "mensaje": "El periodo debe ser 'YYYY-MM' (ej. 2026-11)"}

    anio, mes = int(periodo[:4]), int(periodo[5:7])
    cuotas = query(
        "SELECT id, cliente_id, concepto, monto, dia_cobro, notas "
        "FROM cuotas_clientes WHERE tenant_id = %s AND activo",
        (tenant_id,)
    )
    if not cuotas:
        return {"ok": True, "creados": 0, "omitidos": 0, "periodo": periodo,
                "mensaje": "No hay cuotas activas que generar"}

    existentes = {f["cuota_id"] for f in query(
        "SELECT cuota_id FROM cobros WHERE tenant_id = %s AND periodo = %s AND cuota_id IS NOT NULL",
        (tenant_id, periodo)
    )}

    creados, omitidos = 0, 0
    for cuota in cuotas:
        if cuota["id"] in existentes:
            omitidos += 1
            continue
        vencimiento = date(anio, mes, cuota["dia_cobro"])
        execute(
            "INSERT INTO cobros (tenant_id, cliente_id, cuota_id, origen, concepto, "
            "periodo, monto, vencimiento, notas) "
            "VALUES (%s, %s, %s, 'cuota', %s, %s, %s, %s, %s)",
            (tenant_id, cuota["cliente_id"], cuota["id"], cuota["concepto"],
             periodo, cuota["monto"], vencimiento, cuota["notas"])
        )
        creados += 1

    return {
        "ok": True,
        "creados": creados,
        "omitidos": omitidos,
        "periodo": periodo,
        "mensaje": f"{creados} cobro(s) generado(s), {omitidos} ya existían.",
    }


# ── Cobros extra (trabajos puntuales) y edición de cobros ────────────────────

def _validar_programa(programa) -> tuple[list[dict] | None, str | None]:
    """Normaliza el plan de pagos [{etiqueta, monto, fecha}] o None si inválido."""
    if programa is None:
        return [], None
    if not isinstance(programa, list):
        return None, "El programa de pagos debe ser una lista"
    normalizado = []
    for i, entrada in enumerate(programa):
        try:
            etiqueta = str(entrada.get("etiqueta") or "").strip() or f"Pago {i + 1}"
            monto = float(entrada.get("monto") or 0)
            fecha = str(entrada.get("fecha") or "").strip()[:10]
            date.fromisoformat(fecha)
        except (ValueError, TypeError, AttributeError):
            return None, f"Entrada {i + 1} del programa inválida (etiqueta, monto>0 y fecha 'YYYY-MM-DD')"
        if monto <= 0:
            return None, f"La entrada {i + 1} del programa debe tener monto mayor a 0"
        normalizado.append({"etiqueta": etiqueta, "monto": round(monto, 2), "fecha": fecha})
    return normalizado, None


def _crear_cobro(
    tenant_id: str, cliente_id: str, origen: str, concepto: str, monto: float,
    periodo: str | None = None, vencimiento: date | None = None,
    programa=None, notas: str | None = None,
) -> dict:
    concepto = (concepto or "").strip()
    if not concepto:
        return {"ok": False, "tipo": "validacion", "mensaje": "El concepto es obligatorio"}
    if monto is None or monto <= 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "El monto debe ser mayor a 0"}
    if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}

    programa_n, error = _validar_programa(programa)
    if error:
        return {"ok": False, "tipo": "validacion", "mensaje": error}

    filas = query(
        "INSERT INTO cobros (tenant_id, cliente_id, origen, concepto, periodo, "
        "monto, vencimiento, programa, notas) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, cliente_id, origen, concepto, periodo,
         round(monto, 2), vencimiento, psycopg2.extras.Json(programa_n),
         (notas or "").strip() or None)
    )
    return {"ok": True, "cobro": _cobro_por_id(tenant_id, filas[0]["id"])}


def crear_cobro_extra(
    tenant_id: str, cliente_id: str, concepto: str, monto: float,
    vencimiento: date | None = None, programa=None, notas: str | None = None,
) -> dict:
    """Trabajo puntual ("App de inventarios v1", $15,000, anticipo 50%...)."""
    return _crear_cobro(tenant_id, cliente_id, "extra", concepto, monto,
                        vencimiento=vencimiento, programa=programa, notas=notas)


def actualizar_cobro(
    tenant_id: str, cobro_id: str,
    concepto: str | None = None, monto: float | None = None,
    vencimiento=None, programa=None, notas: str | None = None,
) -> dict:
    """Edición de deuda: concepto, monto, vencimiento, plan y notas.
    `pagos` NO se toca aquí: el dinero entra SOLO por agregar_pago/quitar_pago."""
    actual = _cobro_por_id(tenant_id, cobro_id)
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cobro no encontrado"}

    sets, params = [], []

    if concepto is not None:
        concepto_n = concepto.strip()
        if not concepto_n:
            return {"ok": False, "tipo": "validacion", "mensaje": "El concepto no puede quedar vacío"}
        sets.append("concepto = %s")
        params.append(concepto_n)

    if monto is not None:
        if monto <= 0:
            return {"ok": False, "tipo": "validacion", "mensaje": "El monto debe ser mayor a 0"}
        sets.append("monto = %s")
        params.append(round(monto, 2))

    if vencimiento is not None:
        # '' = sin fecha comprometida; 'YYYY-MM-DD' = fijar (misma semántica CRM).
        sets.append("vencimiento = %s")
        params.append(None if vencimiento == "" else vencimiento)

    if programa is not None:
        programa_n, error = _validar_programa(programa)
        if error:
            return {"ok": False, "tipo": "validacion", "mensaje": error}
        sets.append("programa = %s")
        params.append(psycopg2.extras.Json(programa_n))

    if notas is not None:
        sets.append("notas = %s")
        params.append((notas or "").strip() or None)

    if sets:
        params += [tenant_id, cobro_id]
        execute(f"UPDATE cobros SET {', '.join(sets)} WHERE tenant_id = %s AND id = %s", tuple(params))
    return {"ok": True, "cobro": _cobro_por_id(tenant_id, cobro_id)}


def eliminar_cobro(tenant_id: str, cobro_id: str) -> dict:
    """Borrado duro: una deuda capturada mal se elimina (el abono mal hecho se
    corrige con quitar_pago, no borrando el cobro)."""
    filas = query(
        "DELETE FROM cobros WHERE tenant_id = %s AND id = %s RETURNING concepto",
        (tenant_id, cobro_id)
    )
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cobro no encontrado"}
    return {"ok": True, "mensaje": f"Cobro '{filas[0]['concepto']}' eliminado"}


# ── Abonos (pagos parciales/totales) ─────────────────────────────────────────

def agregar_pago(tenant_id: str, cobro_id: str, monto: float,
                 metodo: str | None = None, fecha: date | None = None) -> dict:
    """Registra un abono (parcial o total). Append-only al JSONB `pagos`:
    el saldo y el estado se recalculan de inmediato. Sobre-pago permitido
    (quedará 'pagado' con saldo 0; corregible quitando el abono)."""
    if monto is None or monto <= 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "El abono debe ser mayor a 0"}
    actual = _cobro_por_id(tenant_id, cobro_id)
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cobro no encontrado"}

    entrada = {
        "fecha": (fecha.isoformat() if isinstance(fecha, date) else date.today().isoformat()),
        "monto": round(monto, 2),
        "metodo": (metodo or "").strip() or None,
    }
    pagos_nuevos = actual["pagos"] + [entrada]
    execute(
        "UPDATE cobros SET pagos = %s WHERE tenant_id = %s AND id = %s",
        (psycopg2.extras.Json(pagos_nuevos), tenant_id, cobro_id)
    )
    cobro = _cobro_por_id(tenant_id, cobro_id)
    return {"ok": True, **_pago_payload(cobro, entrada)}


def quitar_pago(tenant_id: str, cobro_id: str, indice: int) -> dict:
    """Corrección de errores: elimina el abono por posición (0-indexed)."""
    if indice is None or indice < 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "Índice de abono inválido"}
    actual = _cobro_por_id(tenant_id, cobro_id)
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cobro no encontrado"}
    if indice >= len(actual["pagos"]):
        return {"ok": False, "tipo": "validacion", "mensaje": "Ese abono no existe"}
    eliminado = actual["pagos"].pop(indice)
    execute(
        "UPDATE cobros SET pagos = %s WHERE tenant_id = %s AND id = %s",
        (psycopg2.extras.Json(actual["pagos"]), tenant_id, cobro_id)
    )
    cobro = _cobro_por_id(tenant_id, cobro_id)
    return {"ok": True, **_pago_payload(cobro), "eliminado": eliminado}


def _pago_payload(cobro: dict, entrada: dict | None = None) -> dict:
    return {
        "pagos": cobro["pagos"],
        "pagado": cobro["pagado"],
        "saldo": cobro["saldo"],
        "estado": cobro["estado"],
        "vencido": cobro["vencido"],
        "dias_vencido": cobro["dias_vencido"],
        **({"abono": entrada} if entrada else {}),
    }


# ── Cuotas (las suscripciones mensuales) ─────────────────────────────────────

_SELECT_CUOTAS = """
SELECT q.id, q.tenant_id, q.cliente_id, q.concepto, q.monto, q.dia_cobro,
       q.activo, q.notas, q.creado_en, cl.nombre AS cliente_nombre
FROM cuotas_clientes q
JOIN clientes cl ON cl.id = q.cliente_id
"""


def listar_cuotas(tenant_id: str, solo_activas: bool = False) -> list[dict]:
    """Tus suscripciones: a quién, cuánto, qué día del mes y si sigue vigente."""
    where = " WHERE q.tenant_id = %s"
    if solo_activas:
        where += " AND q.activo"
    return query(_SELECT_CUOTAS + where + " ORDER BY cl.nombre, q.concepto", (tenant_id,))


def crear_cuota(tenant_id: str, cliente_id: str, concepto: str, monto: float,
                dia_cobro: int = 1, notas: str | None = None) -> dict:
    concepto = (concepto or "").strip()
    if not concepto:
        return {"ok": False, "tipo": "validacion", "mensaje": "El concepto es obligatorio"}
    if monto is None or monto <= 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "El monto debe ser mayor a 0"}
    if dia_cobro is None or not (1 <= int(dia_cobro) <= 28):
        return {"ok": False, "tipo": "validacion", "mensaje": "El día de cobro debe ser entre 1 y 28"}
    if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
    filas = query(
        "INSERT INTO cuotas_clientes (tenant_id, cliente_id, concepto, monto, dia_cobro, notas) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, cliente_id, concepto, round(monto, 2), int(dia_cobro), (notas or "").strip() or None)
    )
    cuota = query(_SELECT_CUOTAS + " WHERE q.tenant_id = %s AND q.id = %s", (tenant_id, filas[0]["id"]))[0]
    return {"ok": True, "cuota": cuota}


def actualizar_cuota(
    tenant_id: str, cuota_id: str,
    cliente_id: str | None = None, concepto: str | None = None,
    monto: float | None = None, dia_cobro: int | None = None,
    activo: bool | None = None, notas: str | None = None,
) -> dict:
    actual = query(
        "SELECT id, cliente_id, concepto FROM cuotas_clientes WHERE tenant_id = %s AND id = %s",
        (tenant_id, cuota_id)
    )
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cuota no encontrada"}

    sets, params = [], []
    if cliente_id is not None:
        if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
            return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
        sets.append("cliente_id = %s")
        params.append(cliente_id)
    if concepto is not None:
        if not concepto.strip():
            return {"ok": False, "tipo": "validacion", "mensaje": "El concepto no puede quedar vacío"}
        sets.append("concepto = %s")
        params.append(concepto.strip())
    if monto is not None:
        if monto <= 0:
            return {"ok": False, "tipo": "validacion", "mensaje": "El monto debe ser mayor a 0"}
        sets.append("monto = %s")
        params.append(round(monto, 2))
    if dia_cobro is not None:
        if not (1 <= int(dia_cobro) <= 28):
            return {"ok": False, "tipo": "validacion", "mensaje": "El día de cobro debe ser entre 1 y 28"}
        sets.append("dia_cobro = %s")
        params.append(int(dia_cobro))
    if activo is not None:
        sets.append("activo = %s")
        params.append(bool(activo))
    if notas is not None:
        sets.append("notas = %s")
        params.append((notas or "").strip() or None)

    if sets:
        params += [tenant_id, cuota_id]
        execute(f"UPDATE cuotas_clientes SET {', '.join(sets)} WHERE tenant_id = %s AND id = %s", tuple(params))

    cuota = query(_SELECT_CUOTAS + " WHERE q.tenant_id = %s AND q.id = %s", (tenant_id, cuota_id))[0]
    return {"ok": True, "cuota": cuota}


def eliminar_cuota(tenant_id: str, cuota_id: str) -> dict:
    """Borrado temporal: los cobros ya generados quedan (cuota_id pasa a NULL).
    El historial de dinero nunca se borra por editar la suscripción."""
    filas = query(
        "DELETE FROM cuotas_clientes WHERE tenant_id = %s AND id = %s RETURNING concepto",
        (tenant_id, cuota_id)
    )
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cuota no encontrada"}
    return {"ok": True, "mensaje": f"Cuota '{filas[0]['concepto']}' eliminada (los cobros generados se conservan)"}
