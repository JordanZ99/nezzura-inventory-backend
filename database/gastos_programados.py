# ==============================================================================
# backend/database/gastos_programados.py
# CRUD sobre la tabla gastos_programados + motor de verificación automática.
#
# Bugs corregidos en esta versión:
#   1. Zona horaria: ultima_ejecucion ahora usa zona Cancún, no UTC del servidor
#   2. Automático ya no inserta gastos de $0 (solo avanza fecha)
#   4. fin_periodo unificado a _hoy() en ambos caminos (manual y automático)
#   5. Avance de fecha unificado a _avanzar_fecha() en Python en ambos caminos
#   8. Idempotencia: no se puede ejecutar si proxima_fecha > hoy
#   9. Categoría consistente: siempre "Gasto Programado"
# ==============================================================================

import calendar
from zoneinfo import ZoneInfo
from database.conexion import execute, get_conn, release_conn, query
from psycopg2.extras import RealDictCursor
from datetime import date, datetime, timedelta


# ── Zona horaria del negocio (Cancún, UTC-5) ──
_TZ = ZoneInfo("America/Cancun")


def _hoy() -> date:
    """Retorna la fecha actual en la zona horaria de Cancún."""
    return datetime.now(_TZ).date()


def _ahora_cancun_str() -> str:
    """
    Retorna el timestamp actual en zona Cancún como string ISO.
    Se usa para ultima_ejecucion en vez de CURRENT_TIMESTAMP (que usa UTC
    del servidor de Supabase). Esto evita el desfase de un día entero
    cuando se ejecutan gastos cerca de medianoche hora Cancún.
    """
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")


def crear_gasto_programado(
    nombre: str,
    tipo: str,
    valor: float,
    frecuencia: str,
    proxima_fecha: str,
    tenant_id: str,
) -> dict:
    """Inserta una nueva regla de gasto programado."""
    execute(
        "INSERT INTO gastos_programados "
        "(tenant_id, nombre, tipo, valor, frecuencia, proxima_fecha) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (tenant_id, nombre.strip(), tipo, valor, frecuencia, proxima_fecha)
    )
    return {"ok": True}


def _avanzar_fecha(fecha: date, frecuencia: str) -> date:
    """
    Calcula la próxima fecha sumando el intervalo según la frecuencia.
    Usa timedelta para semanas y lógica manual para meses/años
    evitando dependencias externas como dateutil.
    """
    if frecuencia == "semanal":
        return fecha + timedelta(weeks=1)
    elif frecuencia == "mensual":
        mes = fecha.month + 1
        anio = fecha.year + (mes - 1) // 12
        mes = ((mes - 1) % 12) + 1
        dia = min(fecha.day, calendar.monthrange(anio, mes)[1])
        return date(anio, mes, dia)
    elif frecuencia == "anual":
        dia = min(fecha.day, calendar.monthrange(fecha.year + 1, fecha.month)[1])
        return date(fecha.year + 1, fecha.month, dia)
    else:
        return fecha + timedelta(days=30)


def _aplicar_formato_fecha(valor) -> str:
    """
    Convierte cualquier tipo de fecha a string ISO YYYY-MM-DD.
    - datetime.date/date → .isoformat()
    - datetime.datetime → .strftime('%Y-%m-%d')
    - str → se usa directo (ya viene de columna TEXT)
    - None → retorna string vacío
    """
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, date):
        return valor.isoformat()
    return str(valor)[:10]  # str directo, truncar por si trae hora


def _calcular_inicio_periodo(ultima_ejecucion_raw, tenant_id, cur, fallback_str: str) -> str:
    """
    Calcula la fecha de inicio del período para el Corte de Caja.
    - Si existe ultima_ejecucion: día siguiente (+1 día calendario, sin solapamiento).
    - Si no existe: MIN(fecha) de ventas, o el fallback_str si no hay ventas.
    """
    if ultima_ejecucion_raw:
        ue_str = _aplicar_formato_fecha(ultima_ejecucion_raw)
        if ue_str:
            ue_date = date.fromisoformat(ue_str)
            return (ue_date + timedelta(days=1)).isoformat()

    # Sin ejecución previa → desde la primera venta registrada
    cur.execute(
        "SELECT MIN(fecha) FROM ventas WHERE tenant_id = %s",
        (tenant_id,)
    )
    row = cur.fetchone()
    min_fecha = row["min"] if row and "min" in row else None
    return min_fecha if min_fecha else fallback_str


def _calcular_monto_porcentaje(tenant_id: str, regla: dict, cur) -> float:
    """
    Calcula el monto de un gasto programado porcentual usando el Corte de Caja.

    Corte de Caja:
      1. inicio = día siguiente de ultima_ejecucion (o primera venta)
      2. fin = hoy (zona Cancún)
      3. ganancia_neta = SUM(ganancia_bruta ventas activas) - SUM(todos los gastos del período)
      4. monto = (valor / 100) * ganancia_neta si > 0, sino 0

    Se restan TODOS los gastos del período (manuales y de otras reglas)
    porque la ganancia neta refleja lo que realmente quedó después de gastos.
    Si el negocio tuvo muchos gastos, no hay ganancia para dar diezmo.
    """
    ue_raw = regla.get("ultima_ejecucion")
    inicio = _calcular_inicio_periodo(
        ue_raw, tenant_id, cur, _aplicar_formato_fecha(regla["proxima_fecha"])
    )
    fin = _hoy().isoformat()

    # 1. SUM(ganancia_bruta) de ventas activas en el período
    cur.execute(
        "SELECT COALESCE(SUM(ganancia_bruta), 0) AS total FROM ventas "
        "WHERE tenant_id = %s AND estado != 'Inactivo' "
        "AND fecha::date >= %s::date AND fecha::date <= %s::date",
        (tenant_id, inicio, fin)
    )
    row = cur.fetchone()
    ganancia_bruta = float(row["total"]) if row and row["total"] is not None else 0.0

    # 2. SUM(gastos.monto) histórico del período (ANTES de este gasto)
    cur.execute(
        "SELECT COALESCE(SUM(monto), 0) AS total FROM gastos "
        "WHERE tenant_id = %s "
        "AND fecha::date >= %s::date AND fecha::date <= %s::date",
        (tenant_id, inicio, fin)
    )
    row = cur.fetchone()
    total_gastos = float(row["total"]) if row and row["total"] is not None else 0.0

    # 3. Ganancia neta del período
    ganancia_neta = ganancia_bruta - total_gastos

    # 4. Monto = % × ganancia_neta (solo si hay ganancia positiva)
    valor = float(regla["valor"])
    return (valor / 100.0) * ganancia_neta if ganancia_neta > 0 else 0.0


def ejecutar_gasto_programado(regla_id: str, tenant_id: str) -> dict:
    """
    Ejecuta una regla de gasto programado de forma manual.
    - Calcula el monto según el tipo (fijo o porcentaje sobre ganancia neta)
    - Inserta un gasto con estado 'pagado'
    - Actualiza ultima_ejecucion y proxima_fecha de la regla

    Idempotencia: si proxima_fecha > hoy, la regla no está vencida
    y no se puede ejecutar manualmente (ya se pagó adelantado).
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # ── Obtener la regla ──
            cur.execute(
                "SELECT id, nombre, tipo, valor, frecuencia, "
                "       proxima_fecha, ultima_ejecucion "
                "FROM gastos_programados "
                "WHERE id = %s AND tenant_id = %s",
                (regla_id, tenant_id)
            )
            regla = cur.fetchone()
            if not regla:
                return {"ok": False, "mensaje": "Regla no encontrada"}

            rid = str(regla["id"])
            nombre = regla["nombre"]
            tipo = regla["tipo"]

            # ── Idempotencia: no ejecutar si la regla no está vencida ──
            pf_str = _aplicar_formato_fecha(regla["proxima_fecha"])
            if pf_str:
                pf_date = date.fromisoformat(pf_str)
                if pf_date > _hoy():
                    return {
                        "ok": False,
                        "mensaje": f"La regla '{nombre}' vence el {pf_str}. Aún no es la fecha de pago."
                    }

            # ── Calcular monto ──
            if tipo == "fijo":
                monto = float(regla["valor"])
            else:  # porcentaje
                monto = _calcular_monto_porcentaje(tenant_id, regla, cur)

            # ── Calcular próxima fecha (en Python, zona Cancún) ──
            pf_date = date.fromisoformat(pf_str) if pf_str else _hoy()
            nueva_proxima = _avanzar_fecha(pf_date, regla["frecuencia"])
            nueva_proxima_str = nueva_proxima.isoformat()

            # ── Timestamp de ejecución en zona Cancún (no UTC del servidor) ──
            ts_ejecucion = _ahora_cancun_str()

            # ── Si monto <= 0, solo actualizar fechas sin insertar gasto ──
            if monto <= 0:
                cur.execute(
                    "UPDATE gastos_programados "
                    "SET ultima_ejecucion = %s::timestamp, "
                    "    proxima_fecha = %s::date "
                    "WHERE id = %s",
                    (ts_ejecucion, nueva_proxima_str, rid)
                )
                conn.commit()
                return {
                    "ok": True,
                    "monto": 0,
                    "nombre": nombre,
                    "mensaje": f"Sin ganancia neta positiva en el período para '{nombre}'. Se actualizó la próxima fecha sin generar gasto."
                }

            # ── Insertar gasto como 'pagado' ──
            fecha_hoy = _hoy().isoformat()
            cur.execute(
                "INSERT INTO gastos "
                "(Fecha, Categoria, Descripcion, Monto, Tenant_ID, Estado, Gasto_Programado_ID) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (fecha_hoy, "Gasto Programado", f"Pago: {nombre}",
                 monto, tenant_id, "pagado", rid)
            )

            # ── Actualizar la regla ──
            cur.execute(
                "UPDATE gastos_programados "
                "SET ultima_ejecucion = %s::timestamp, "
                "    proxima_fecha = %s::date "
                "WHERE id = %s",
                (ts_ejecucion, nueva_proxima_str, rid)
            )

            conn.commit()
            return {
                "ok": True,
                "monto": round(monto, 2),
                "nombre": nombre,
                "mensaje": f"Gasto de ${monto:.2f} generado para '{nombre}'."
            }

    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error: {str(e)}"}
    finally:
        release_conn(conn)


def verificar_y_generar_gastos_programados(tenant_id: str) -> dict:
    """
    Motor de verificación automática:
    1. Busca reglas cuya proxima_fecha <= hoy
    2. Para cada regla vencida:
       - Calcula el monto (fijo o porcentaje) usando el mismo Corte de Caja
         que la ejecución manual, con fin_periodo = _hoy() (no proxima_fecha)
       - Si monto > 0: inserta gasto 'pendiente'
       - Si monto <= 0: NO inserta gasto, solo avanza fecha
       - Avanza proxima_fecha según frecuencia
    Todo dentro de una sola transacción.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # ── Paso A: Reglas vencidas ──
            cur.execute(
                "SELECT id, nombre, tipo, valor, frecuencia, "
                "       proxima_fecha, ultima_ejecucion "
                "FROM gastos_programados "
                "WHERE tenant_id = %s "
                "AND proxima_fecha::date <= %s::date",
                (tenant_id, _hoy().isoformat())
            )
            reglas = cur.fetchall()

            if not reglas:
                return {"ok": True, "generados": 0, "mensaje": "Sin reglas vencidas"}

            generados = 0
            ts_ejecucion = _ahora_cancun_str()

            for regla in reglas:
                rid = regla["id"]
                nombre = regla["nombre"]
                tipo = regla["tipo"]
                frecuencia = regla["frecuencia"]

                # ── Paso B: Calcular monto (mismo Corte de Caja que manual) ──
                if tipo == "fijo":
                    monto = float(regla["valor"])
                else:  # porcentaje
                    monto = _calcular_monto_porcentaje(tenant_id, regla, cur)

                # ── Paso C: Avanzar próxima fecha (Python, zona Cancún) ──
                pf_str = _aplicar_formato_fecha(regla["proxima_fecha"])
                pf_date = date.fromisoformat(pf_str) if pf_str else _hoy()
                nueva_proxima = _avanzar_fecha(pf_date, frecuencia)
                nueva_proxima_str = nueva_proxima.isoformat()

                # ── Paso D: Insertar gasto SOLO si monto > 0 ──
                if monto > 0:
                    descripcion_auto = f"{nombre} ({frecuencia.capitalize()} - Automático)"
                    fecha_hoy = _hoy().isoformat()

                    cur.execute(
                        "INSERT INTO gastos "
                        "(Fecha, Categoria, Descripcion, Monto, Tenant_ID, Estado, Gasto_Programado_ID) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (fecha_hoy, "Gasto Programado", descripcion_auto,
                         monto, tenant_id, "pendiente", rid)
                    )
                    generados += 1

                # ── Paso E: Avanzar fecha SIEMPRE (haya o no gasto) ──
                cur.execute(
                    "UPDATE gastos_programados "
                    "SET proxima_fecha = %s::date, "
                    "    ultima_ejecucion = %s::timestamp "
                    "WHERE id = %s AND tenant_id = %s",
                    (nueva_proxima_str, ts_ejecucion, rid, tenant_id)
                )

            conn.commit()
            return {
                "ok": True,
                "generados": generados,
                "mensaje": f"{generados} gasto(s) programado(s) generado(s)."
            }

    except Exception as e:
        conn.rollback()
        return {"ok": False, "generados": 0, "mensaje": f"Error: {str(e)}"}
    finally:
        release_conn(conn)


def eliminar_gasto_programado(regla_id: str, tenant_id: str) -> dict:
    """
    Elimina una regla de gasto programado.
    Los gastos ya generados por esta regla NO se eliminan (quedan como
    historial en la tabla gastos).
    """
    # Verificar que la regla pertenece al tenant antes de eliminar
    filas = query(
        "SELECT id FROM gastos_programados WHERE id = %s AND tenant_id = %s",
        (regla_id, tenant_id)
    )
    if not filas:
        return {"ok": False, "mensaje": "Regla no encontrada"}

    execute(
        "DELETE FROM gastos_programados WHERE id = %s AND tenant_id = %s",
        (regla_id, tenant_id)
    )
    return {"ok": True, "mensaje": "Regla eliminada. Los gastos ya generados se conservan en el historial."}


def actualizar_gasto_programado(
    regla_id: str,
    tenant_id: str,
    nombre: str | None = None,
    tipo: str | None = None,
    valor: float | None = None,
    frecuencia: str | None = None,
    proxima_fecha: str | None = None,
) -> dict:
    """
    Actualiza campos de una regla de gasto programado.
    Solo actualiza los campos que no sean None (PATCH parcial).
    """
    filas = query(
        "SELECT id FROM gastos_programados WHERE id = %s AND tenant_id = %s",
        (regla_id, tenant_id)
    )
    if not filas:
        return {"ok": False, "mensaje": "Regla no encontrada"}

    campos = []
    valores = []
    if nombre is not None:
        campos.append("nombre = %s")
        valores.append(nombre.strip())
    if tipo is not None:
        campos.append("tipo = %s")
        valores.append(tipo)
    if valor is not None:
        campos.append("valor = %s")
        valores.append(valor)
    if frecuencia is not None:
        campos.append("frecuencia = %s")
        valores.append(frecuencia)
    if proxima_fecha is not None:
        campos.append("proxima_fecha = %s::date")
        valores.append(proxima_fecha)

    if not campos:
        return {"ok": True, "mensaje": "Nada que actualizar"}

    valores.append(regla_id)
    valores.append(tenant_id)
    execute(
        f"UPDATE gastos_programados SET {', '.join(campos)} "
        f"WHERE id = %s AND tenant_id = %s",
        tuple(valores)
    )
    return {"ok": True, "mensaje": "Regla actualizada"}
