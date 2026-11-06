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
from datetime import date, timedelta

import psycopg2.extras

from database.conexion import execute, query
from database.actividad import registrar_actividad

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

def _fechas_de_cuota_en_mes(cuota: dict, anio: int, mes: int) -> list:
    """Fechas de vencimiento de UNA cuota DENTRO del mes (mig 063):
      mensual   → su dia_cobro (comportamiento de siempre).
      semanal   → cada aparición del dia_semana en el mes (4-5 fechas).
      quincenal → día 1 y día 15.
    Cada fecha es una deuda concreta con su propio vencimiento; el índice
    único (tenant, cuota, vencimiento) garantiza que no se dupliquen."""
    f = cuota.get("frecuencia") or "mensual"
    if f == "semanal":
        ds = int(cuota["dia_semana"]) if cuota.get("dia_semana") is not None else 0
        fecha = date(anio, mes, 1)
        fecha += timedelta(days=(ds - fecha.weekday()) % 7)  # primer día que cae
        fechas = []
        while fecha.month == mes:
            fechas.append(fecha)
            fecha += timedelta(days=7)
        return fechas
    if f == "quincenal":
        return [date(anio, mes, 1), date(anio, mes, 15)]
    return [date(anio, mes, int(cuota["dia_cobro"]))]


def generar_cobros(tenant_id: str, periodo: str) -> dict:
    """Crea los cobros del periodo para cada cuota activa (mig 063: mensuales,
    semanales y quincenales). Idempotente: el par cuota+vencimiento que ya
    existe no se duplica (índice parcial único).

    Mig 065 (comportamiento, sin columnas): cada corrida emite solo lo
    VENCIDO/HOY más el PRÓXIMO vencimiento por venir — el resto del calendario
    no existe hasta que llega su fecha. Y nunca cobra hacia atrás: los
    vencimientos ANTERIORES a la fecha de alta de la cuota se descartan."""
    if not _REGEX_PERIODO.match(periodo or ""):
        return {"ok": False, "tipo": "validacion", "mensaje": "El periodo debe ser 'YYYY-MM' (ej. 2026-11)"}

    anio, mes = int(periodo[:4]), int(periodo[5:7])
    hoy = date.today()
    cuotas = query(
        "SELECT id, cliente_id, concepto, monto, dia_cobro, frecuencia, dia_semana, "
        "monto_meta, creado_en, notas "
        "FROM cuotas_clientes WHERE tenant_id = %s AND activo",
        (tenant_id,)
    )
    if not cuotas:
        return {"ok": True, "creados": 0, "omitidos": 0, "periodo": periodo,
                "mensaje": "No hay cuotas activas que generar"}

    # existentes por (cuota, fecha): cubre mensual (una fecha al mes) y el
    # resto (cada vencimiento concreto del mes). El monto se usa para descontar
    # del restante de las cuotas con meta (064).
    existentes = {(f["cuota_id"], f["vencimiento"]): float(f["monto"] or 0) for f in query(
        "SELECT cuota_id, vencimiento, monto FROM cobros "
        "WHERE tenant_id = %s AND periodo = %s AND cuota_id IS NOT NULL",
        (tenant_id, periodo)
    )}
    # Lo ya PROGRAMADO por cuota (todas las generadas a la cuenta, cualquier
    # mes): el restante contra la meta.
    ya_programado = {f["cuota_id"]: round(float(f["ya"] or 0), 2) for f in query(
        "SELECT cuota_id, SUM(monto) AS ya FROM cobros "
        "WHERE tenant_id = %s AND cuota_id IS NOT NULL GROUP BY cuota_id",
        (tenant_id,)
    )}

    creados, omitidos = 0, 0
    for cuota in cuotas:
        meta = cuota["monto_meta"]
        restante = round(float(meta), 2) if meta is not None else None
        # ── Mig 065: fechas a emitir ──
        # 1) Descarta los vencimientos ANTERIORES al alta de la cuota:
        #    crearla hoy no genera deudas de días que ya pasaron.
        creado_en = cuota.get("creado_en")
        alta = creado_en.date() if creado_en is not None else None
        fechas = [f for f in _fechas_de_cuota_en_mes(cuota, anio, mes)
                  if alta is None or f >= alta]
        # 2) Emite lo ya vencido/hoy + SOLO el próximo pago por venir; los
        #    futuros más allá del primero no existen hasta que su fecha llegue.
        ya_pasadas = [f for f in fechas if f <= hoy]
        emision = list(ya_pasadas)
        if len(ya_pasadas) < len(fechas):
            emision.append(fechas[len(ya_pasadas)])  # primera fecha futura
        for vencimiento in emision:
            if restante is not None and restante <= _EPSILON:
                # Cuenta finita cubierta: no genera más (064).
                break
            if (cuota["id"], vencimiento) in existentes:
                omitidos += 1
                if restante is not None:
                    restante = round(restante - existentes[(cuota["id"], vencimiento)], 2)
                continue
            monto_cobro = float(cuota["monto"])
            if restante is not None:
                # Último abono del plan finito: se recorta al restante para
                # que el total programado sea EXACTAMENTE la meta.
                monto_cobro = min(monto_cobro, restante)
            execute(
                "INSERT INTO cobros (tenant_id, cliente_id, cuota_id, origen, concepto, "
                "periodo, monto, vencimiento, notas) "
                "VALUES (%s, %s, %s, 'cuota', %s, %s, %s, %s, %s)",
                (tenant_id, cuota["cliente_id"], cuota["id"], cuota["concepto"],
                 periodo, round(monto_cobro, 2), vencimiento, cuota["notas"])
            )
            creados += 1
            if restante is not None:
                restante = round(restante - monto_cobro, 2)

    # Registro de actividad (062): generación del periodo (un solo renglón
    # resumido; cada cobro ya vive en la tabla cobros).
    try:
        if creados or omitidos:
            detalle_gen: dict = {"cuotas": len(cuotas)}
            if omitidos:
                detalle_gen["omitidos"] = omitidos
            registrar_actividad(
                tenant_id, "cobro", "cobro", "generar",
                resumen=f"Generó {creados} cobro(s) de cuota(s) del periodo {periodo}",
                detalle=detalle_gen if creados else None,
            )
    except Exception:
        pass

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
    # Registro de actividad (062): alta de cobro (extra o manual).
    try:
        nombre_cliente = query(
            "SELECT nombre FROM clientes WHERE tenant_id = %s AND id = %s",
            (tenant_id, cliente_id)
        )
        registrar_actividad(
            tenant_id, "cobro", "cobro", "crear",
            resumen=(f"Creó el cobro {concepto} por {round(monto, 2):.2f} para "
                     f"{nombre_cliente[0]['nombre']}") if nombre_cliente
                    else f"Creó el cobro {concepto} por {round(monto, 2):.2f}",
            entidad_id=filas[0]["id"],
            detalle={"monto": round(monto, 2),
                     **({"vencimiento": str(vencimiento)} if vencimiento else {}),
                     **({"programa": len(programa_n)} if programa_n else {})},
        )
    except Exception:
        pass
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
        # Registro de actividad (062): diff de la edición del cobro.
        try:
            diff: dict = {}
            if concepto is not None and concepto.strip() and concepto.strip() != actual["concepto"]:
                diff["concepto"] = [actual["concepto"], concepto.strip()]
            if monto is not None and round(float(monto), 2) != float(actual["monto"]):
                diff["monto"] = [float(actual["monto"]), round(float(monto), 2)]
            if vencimiento is not None:
                vieja_venc = str(actual["vencimiento"])[:10] if actual["vencimiento"] else None
                nueva_venc = None if vencimiento == "" else str(vencimiento)[:10]
                if vieja_venc != nueva_venc:
                    diff["vencimiento"] = [vieja_venc, nueva_venc]
            if notas is not None:
                viejas_notas = actual.get("notas") or None
                nuevas_notas = (notas or "").strip() or None
                if viejas_notas != nuevas_notas:
                    diff["notas"] = [viejas_notas, nuevas_notas]
            if programa is not None:
                diff["programa"] = [[
                    {k: (float(p.get("monto", 0)) if k == "monto" else p.get(k)) for k in p}
                    for p in (actual["programa"] or [])], programa_n]
            if diff:
                registrar_actividad(
                    tenant_id, "cobro", "cobro", "editar",
                    resumen=f"Editó el cobro {concepto if concepto is not None else actual['concepto']}",
                    entidad_id=cobro_id, detalle=diff,
                )
        except Exception:
            pass
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
    # Registro de actividad (062): baja del cobro.
    try:
        registrar_actividad(
            tenant_id, "cobro", "cobro", "eliminar",
            resumen=f"Eliminó el cobro {filas[0]['concepto']}",
            entidad_id=cobro_id,
        )
    except Exception:
        pass
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
    # Registro de actividad (062): abono al cobro (el dinero vive en `pagos`).
    try:
        registrar_actividad(
            tenant_id, "cobro", "abono", "abonar",
            resumen=(f"Abonó {round(monto, 2):.2f} al cobro {actual['concepto']} "
                     f"de {actual['cliente_nombre']}"),
            entidad_id=cobro_id,
            detalle={"monto": round(monto, 2),
                     "fecha": entrada["fecha"],
                     **({"metodo": entrada["metodo"]} if entrada["metodo"] else {}),
                     "saldo_despues": cobro["saldo"] if cobro else None,
                     "estado_despues": cobro["estado"] if cobro else None},
        )
    except Exception:
        pass
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
    # Registro de actividad (062): corrección — el abono se quitó de `pagos`.
    try:
        registrar_actividad(
            tenant_id, "cobro", "abono", "quitar_abono",
            resumen=(f"Quitó el abono de {float(eliminado.get('monto') or 0):.2f} del cobro "
                     f"{actual['concepto']} de {actual['cliente_nombre']}"),
            entidad_id=cobro_id,
            detalle={"quitar": eliminado,
                     "saldo_despues": cobro["saldo"] if cobro else None},
        )
    except Exception:
        pass
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
       q.frecuencia, q.dia_semana, q.monto_meta,
       q.activo, q.notas, q.creado_en, cl.nombre AS cliente_nombre,
       -- Derivados para la UI (cuentas finitas, 064): SOLO lectura.
       (SELECT COALESCE(SUM(x.monto), 0) FROM cobros x WHERE x.cuota_id = q.id) AS programado_meta,
       (SELECT COALESCE(SUM((pg->>'monto')::numeric), 0)
          FROM cobros y, jsonb_array_elements(y.pagos) AS pg
         WHERE y.cuota_id = q.id) AS abonado_meta,
       -- Mig 065: cobros generados de esta cuota SIN abonos (borrables junto
       -- con la cuota; los que ya recibieron dinero se conservan siempre).
       (SELECT COUNT(*) FROM cobros z
         WHERE z.cuota_id = q.id
           AND (z.pagos IS NULL OR jsonb_array_length(z.pagos) = 0)) AS cobros_pendientes
FROM cuotas_clientes q
JOIN clientes cl ON cl.id = q.cliente_id
"""

# Frecuencias soportadas (mig 063). Semanal: un cobro por cada dia_semana
# (0=Lunes..6=Domingo, mismo orden que date.weekday() de Python). Quincenal:
# dos cobros al mes, día 1 y día 15.
_FRECUENCIAS = ("mensual", "semanal", "quincenal")
DIAS_SEMANA = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


def _resumen_cuota(cuota: dict) -> str:
    """Texto corto legible: 'el día 10 de cada mes', 'cada Lunes',
    'los días 1 y 15 de cada mes'."""
    f = cuota.get("frecuencia") or "mensual"
    if f == "semanal":
        ds = int(cuota["dia_semana"]) if cuota.get("dia_semana") is not None else 0
        return f"cada {DIAS_SEMANA[ds]}"
    if f == "quincenal":
        return "los días 1 y 15 de cada mes"
    return f"el día {int(cuota['dia_cobro'])} de cada mes"


def listar_cuotas(tenant_id: str, solo_activas: bool = False) -> list[dict]:
    """Tus suscripciones: a quién, cuánto, con qué frecuencia y si sigue vigente."""
    where = " WHERE q.tenant_id = %s"
    if solo_activas:
        where += " AND q.activo"
    return query(_SELECT_CUOTAS + where + " ORDER BY cl.nombre, q.concepto", (tenant_id,))


def crear_cuota(tenant_id: str, cliente_id: str, concepto: str, monto: float,
                dia_cobro: int = 1, notas: str | None = None,
                frecuencia: str = "mensual", dia_semana: int | None = None,
                monto_meta: float | None = None) -> dict:
    concepto = (concepto or "").strip()
    if not concepto:
        return {"ok": False, "tipo": "validacion", "mensaje": "El concepto es obligatorio"}
    if monto is None or monto <= 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "El monto debe ser mayor a 0"}
    if frecuencia not in _FRECUENCIAS:
        return {"ok": False, "tipo": "validacion",
                "mensaje": "La frecuencia debe ser mensual, semanal o quincenal"}
    meta_n = round(float(monto_meta), 2) if monto_meta is not None else None
    if meta_n is not None and meta_n <= 0:
        return {"ok": False, "tipo": "validacion",
                "mensaje": "La meta total debe ser mayor a 0 (déjala vacía para cuota infinita)"}
    dia_semana_n = None
    if frecuencia == "mensual":
        if dia_cobro is None or not (1 <= int(dia_cobro) <= 28):
            return {"ok": False, "tipo": "validacion", "mensaje": "El día de cobro debe ser entre 1 y 28"}
    elif frecuencia == "semanal":
        if dia_semana is None or int(dia_semana) not in (0, 1, 2, 3, 4, 5, 6):
            return {"ok": False, "tipo": "validacion",
                    "mensaje": "Elige el día de la semana (Lunes a Domingo)"}
        dia_semana_n = int(dia_semana)
        dia_cobro = 1  # placeholder de la col NOT NULL; no se usa en semanal
    else:  # quincenal: fijo (1 y 15 de cada mes)
        dia_cobro = 1
    if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
    filas = query(
        "INSERT INTO cuotas_clientes (tenant_id, cliente_id, concepto, monto, dia_cobro, "
        "frecuencia, dia_semana, monto_meta, notas) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (tenant_id, cliente_id, concepto, round(monto, 2), int(dia_cobro),
         frecuencia, dia_semana_n, meta_n, (notas or "").strip() or None)
    )
    cuota = query(_SELECT_CUOTAS + " WHERE q.tenant_id = %s AND q.id = %s", (tenant_id, filas[0]["id"]))[0]
    # Registro de actividad (062): alta de cuota (suscripción recurrente).
    try:
        registrar_actividad(
            tenant_id, "cobro", "cuota", "crear",
            resumen=f"Creó la cuota {concepto} de {cuota['cliente_nombre']} "
                    f"({round(monto, 2):.2f}, {_resumen_cuota(cuota)}{f', hasta {meta_n:.2f}' if meta_n is not None else ''})",
            entidad_id=filas[0]["id"],
            detalle={"monto": round(monto, 2), "frecuencia": frecuencia,
                     "dia_cobro": int(cuota["dia_cobro"]),
                     **({"dia_semana": dia_semana_n} if dia_semana_n is not None else {}),
                     **({"monto_meta": meta_n} if meta_n is not None else {})},
        )
    except Exception:
        pass
    return {"ok": True, "cuota": cuota}


def actualizar_cuota(
    tenant_id: str, cuota_id: str,
    cliente_id: str | None = None, concepto: str | None = None,
    monto: float | None = None, dia_cobro: int | None = None,
    activo: bool | None = None, notas: str | None = None,
    frecuencia: str | None = None, dia_semana: int | None = None,
    monto_meta: float | None = None,
) -> dict:
    actual = query(
        "SELECT id, cliente_id, concepto, monto, dia_cobro, frecuencia, dia_semana, "
        "monto_meta, activo, notas "
        "FROM cuotas_clientes WHERE tenant_id = %s AND id = %s",
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

    # ── Frecuencia (063): valida el PAR frecuencia+días que quedará final ──
    frec_final = (frecuencia if frecuencia is not None else actual["frecuencia"]) or "mensual"
    if frecuencia is not None and frecuencia not in _FRECUENCIAS:
        return {"ok": False, "tipo": "validacion",
                "mensaje": "La frecuencia debe ser mensual, semanal o quincenal"}
    if frec_final == "semanal":
        ds_final = dia_semana if dia_semana is not None else actual["dia_semana"]
        if ds_final is None or int(ds_final) not in (0, 1, 2, 3, 4, 5, 6):
            return {"ok": False, "tipo": "validacion",
                    "mensaje": "Elige el día de la semana (Lunes a Domingo)"}
    if frecuencia is not None:
        sets.append("frecuencia = %s")
        params.append(frecuencia)
    if dia_semana is not None:
        sets.append("dia_semana = %s")
        params.append(int(dia_semana))
    if dia_cobro is not None:
        if not (1 <= int(dia_cobro) <= 28):
            return {"ok": False, "tipo": "validacion", "mensaje": "El día de cobro debe ser entre 1 y 28"}
        sets.append("dia_cobro = %s")
        params.append(int(dia_cobro))
    if monto_meta is not None:
        # Sentinel de la UI: 0 = quitar la meta (cuota vuelve a ser infinita);
        # >0 = fijar/ajustar la meta del plan finito. Nunca negativo.
        meta_nueva = round(float(monto_meta), 2)
        if meta_nueva < 0:
            return {"ok": False, "tipo": "validacion", "mensaje": "La meta no puede ser negativa"}
        sets.append("monto_meta = %s")
        params.append(meta_nueva if meta_nueva > 0 else None)
    if frec_final != "semanal" and (dia_semana is not None or actual["dia_semana"] is not None):
        # Al salir de semanal se limpia el día de semana (CHECK-friendly y
        # sin datos huérfanos aunque el cliente no mande dia_semana).
        sets.append("dia_semana = NULL")
    if activo is not None:
        sets.append("activo = %s")
        params.append(bool(activo))
    if notas is not None:
        sets.append("notas = %s")
        params.append((notas or "").strip() or None)

    if sets:
        params += [tenant_id, cuota_id]
        execute(f"UPDATE cuotas_clientes SET {', '.join(sets)} WHERE tenant_id = %s AND id = %s", tuple(params))
        # Registro de actividad (062): diff de la cuota.
        try:
            diff: dict = {}
            if cliente_id is not None:
                viejo_nombre = query(
                    "SELECT nombre FROM clientes WHERE tenant_id = %s AND id = %s",
                    (tenant_id, actual["cliente_id"])
                )
                nuevo_nombre = query(
                    "SELECT nombre FROM clientes WHERE tenant_id = %s AND id = %s",
                    (tenant_id, cliente_id)
                )
                viejo_n = viejo_nombre[0]["nombre"] if viejo_nombre else str(actual["cliente_id"])
                nuevo_n = nuevo_nombre[0]["nombre"] if nuevo_nombre else str(cliente_id)
                if viejo_n != nuevo_n:
                    diff["cliente"] = [viejo_n, nuevo_n]
            if concepto is not None and concepto.strip() != actual["concepto"]:
                diff["concepto"] = [actual["concepto"], concepto.strip()]
            if monto is not None and round(float(monto), 2) != round(float(actual["monto"]), 2):
                diff["monto"] = [round(float(actual["monto"]), 2), round(float(monto), 2)]
            if dia_cobro is not None and int(dia_cobro) != int(actual["dia_cobro"]):
                diff["dia_cobro"] = [int(actual["dia_cobro"]), int(dia_cobro)]
            if frecuencia is not None and frecuencia != actual["frecuencia"]:
                diff["frecuencia"] = [actual["frecuencia"], frecuencia]
            vieja_ds = int(actual["dia_semana"]) if actual["dia_semana"] is not None else None
            nueva_ds = int(dia_semana) if (dia_semana is not None and frec_final == "semanal") else None
            if vieja_ds != nueva_ds:
                # Siempre legible: 'Lunes' etc.; NULL = (sin día de semana).
                diff["dia_semana"] = [DIAS_SEMANA[vieja_ds] if vieja_ds is not None else None,
                                      DIAS_SEMANA[nueva_ds] if nueva_ds is not None else None]
            if activo is not None and bool(activo) != bool(actual["activo"]):
                diff["activo"] = [bool(actual["activo"]), bool(activo)]
            if notas is not None and ((notas or "").strip() or None) != actual["notas"]:
                diff["notas"] = [actual["notas"], (notas or "").strip() or None]
            if monto_meta is not None:
                vieja_meta = float(actual["monto_meta"]) if actual["monto_meta"] is not None else None
                nueva_meta = float(monto_meta)
                if vieja_meta != nueva_meta:
                    diff["monto_meta"] = [
                        f"{vieja_meta:.2f}" if vieja_meta is not None else "sin límite",
                        f"{nueva_meta:.2f}" if nueva_meta > 0 else "sin límite",
                    ]
            if diff:
                registrar_actividad(
                    tenant_id, "cobro", "cuota", "editar",
                    resumen=f"Editó la cuota {concepto if concepto is not None else actual['concepto']}",
                    entidad_id=cuota_id, detalle=diff,
                )
        except Exception:
            pass

    cuota = query(_SELECT_CUOTAS + " WHERE q.tenant_id = %s AND q.id = %s", (tenant_id, cuota_id))[0]
    return {"ok": True, "cuota": cuota}


def eliminar_cuota(tenant_id: str, cuota_id: str, borrar_pendientes: bool = False) -> dict:
    """Baja de la cuota. El dinero histórico nunca se borra:
      - Los cobros que YA recibieron abonos quedan huérfanos (cuota_id pasa a
        NULL por el ON DELETE SET NULL) con su saldo y pagos intactos.
      - mig 065: si borrar_pendientes, los cobros SIN abonos se eliminan
        (borrado duro) — deudas de una suscripción que ya no existe.
    Borrar primero los cobros y DESPUÉS la cuota: el SET NULL del FK se
    aplicaría antes de poder distinguirlos."""
    filas = query(
        "SELECT concepto FROM cuotas_clientes WHERE tenant_id = %s AND id = %s",
        (tenant_id, cuota_id)
    )
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cuota no encontrada"}
    concepto = filas[0]["concepto"]

    borrados = 0
    if borrar_pendientes:
        filas_b = query(
            "DELETE FROM cobros WHERE tenant_id = %s AND cuota_id = %s "
            "AND (pagos IS NULL OR jsonb_array_length(pagos) = 0) RETURNING id",
            (tenant_id, cuota_id)
        )
        borrados = len(filas_b)

    query(
        "DELETE FROM cuotas_clientes WHERE tenant_id = %s AND id = %s",
        (tenant_id, cuota_id)
    )
    # Registro de actividad (062): baja de cuota.
    try:
        detalle = {"borrados_pendientes": borrados} if borrar_pendientes else None
        registrar_actividad(
            tenant_id, "cobro", "cuota", "eliminar",
            resumen=(f"Eliminó la cuota {concepto}" +
                     (f" (y {borrados} cobro(s) pendientes sin abonos)" if borrar_pendientes else "")),
            entidad_id=cuota_id,
            detalle=detalle,
        )
    except Exception:
        pass
    mensaje = f"Cuota '{concepto}' eliminada"
    if borrar_pendientes:
        mensaje += (f" con {borrados} cobro(s) pendientes borrados"
                    if borrados else "; sus cobros con abonos se conservan")
    else:
        mensaje += " (los cobros generados se conservan)"
    return {"ok": True, "cobros_borrados": borrados, "mensaje": mensaje}
