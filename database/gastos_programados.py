# ==============================================================================
# backend/database/gastos_programados.py
# CRUD sobre la tabla gastos_programados + motor de verificación automática.
# ==============================================================================

import calendar
from database.conexion import execute, get_conn, release_conn
from psycopg2.extras import RealDictCursor
from datetime import date, datetime, timedelta


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


def _intervalo_sql(frecuencia: str) -> str:
    """Retorna el intervalo PostgreSQL correspondiente a la frecuencia."""
    mapping = {
        "semanal": "1 week",
        "mensual": "1 month",
        "anual": "1 year",
    }
    return mapping.get(frecuencia, "1 month")


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


def ejecutar_gasto_programado(regla_id: str, tenant_id: str) -> dict:
    """
    Ejecuta una regla de gasto programado de forma manual.
    - Calcula el monto según el tipo (fijo o porcentaje sobre ganancia neta)
    - Inserta un gasto con estado 'pagado'
    - Actualiza ultima_ejecucion y proxima_fecha de la regla

    Corte de Caja: cada ejecución considera únicamente el período
    comprendido entre el día después de la última ejecución y hoy,
    sin solapamiento con períodos anteriores.
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
            valor = float(regla["valor"])
            frecuencia = regla["frecuencia"]

            # ── Saneamiento de fechas ──
            pf_raw = regla["proxima_fecha"]
            proxima_fecha_str = _aplicar_formato_fecha(pf_raw)
            ue_raw = regla.get("ultima_ejecucion")

            # ── Calcular monto ──
            if tipo == "fijo":
                monto = valor
            else:  # porcentaje
                # ── Corte de Caja: inicio del período ──
                inicio = _calcular_inicio_periodo(
                    ue_raw, tenant_id, cur, proxima_fecha_str
                )
                fin = date.today().isoformat()

                # ── 1. SUM(ganancia_bruta) de ventas activas en el período ──
                cur.execute(
                    "SELECT COALESCE(SUM(ganancia_bruta), 0) AS total FROM ventas "
                    "WHERE tenant_id = %s AND estado != 'Inactivo' "
                    "AND fecha::date >= %s::date AND fecha::date <= %s::date",
                    (tenant_id, inicio, fin)
                )
                row = cur.fetchone()
                ganancia_bruta = float(row["total"]) if row and row["total"] is not None else 0.0

                # ── 2. SUM(gastos.monto) histórico del período (ANTES de este gasto) ──
                cur.execute(
                    "SELECT COALESCE(SUM(monto), 0) AS total FROM gastos "
                    "WHERE tenant_id = %s "
                    "AND fecha::date >= %s::date AND fecha::date <= %s::date",
                    (tenant_id, inicio, fin)
                )
                row = cur.fetchone()
                total_gastos = float(row["total"]) if row and row["total"] is not None else 0.0

                # ── 3. Ganancia neta del período (Corte de Caja limpio) ──
                ganancia_neta_periodo = ganancia_bruta - total_gastos

                # ── 4. Monto del nuevo gasto = % × ganancia_neta_periodo ──
                monto = (valor / 100.0) * ganancia_neta_periodo if ganancia_neta_periodo > 0 else 0.0

            # ── Calcular próxima fecha (en Python con timedelta/lógica manual) ──
            pf_date = date.fromisoformat(proxima_fecha_str) if proxima_fecha_str else date.today()
            nueva_proxima_fecha = _avanzar_fecha(pf_date, frecuencia)
            nueva_proxima_fecha_str = nueva_proxima_fecha.isoformat()

            # ── Si monto <= 0, solo actualizar fechas sin insertar gasto ──
            if monto <= 0:
                cur.execute(
                    "UPDATE gastos_programados "
                    "SET ultima_ejecucion = CURRENT_TIMESTAMP, "
                    "    proxima_fecha = %s::date "
                    "WHERE id = %s",
                    (nueva_proxima_fecha_str, rid)
                )
                conn.commit()
                return {
                    "ok": True,
                    "monto": 0,
                    "nombre": nombre,
                    "mensaje": f"Sin ganancia neta positiva en el período para '{nombre}'. Se actualizó la próxima fecha sin generar gasto."
                }

            # ── Insertar gasto (pagado directamente por ser ejecución manual) ──
            fecha_hoy = date.today().isoformat()
            cur.execute(
                "INSERT INTO gastos "
                "(Fecha, Categoria, Descripcion, Monto, Tenant_ID, Estado, Gasto_Programado_ID) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (fecha_hoy, "Otros", f"Pago de regla: {nombre}",
                 monto, tenant_id, "pagado", rid)
            )

            # ── Actualizar la regla ──
            cur.execute(
                "UPDATE gastos_programados "
                "SET ultima_ejecucion = CURRENT_TIMESTAMP, "
                "    proxima_fecha = %s::date "
                "WHERE id = %s",
                (nueva_proxima_fecha_str, rid)
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
         que la ejecución manual
       - Inserta un gasto con estado 'pendiente'
       - Avanza la proxima_fecha según su frecuencia
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
                "AND proxima_fecha::date <= CURRENT_DATE",
                (tenant_id,)
            )
            reglas = cur.fetchall()

            if not reglas:
                conn.commit()
                return {"ok": True, "generados": 0, "mensaje": "Sin reglas vencidas"}

            generados = 0

            for regla in reglas:
                rid = regla["id"]
                nombre = regla["nombre"]
                tipo = regla["tipo"]
                valor = float(regla["valor"])
                frecuencia = regla["frecuencia"]
                proxima_fecha = str(regla["proxima_fecha"])

                # ── Paso B: Calcular monto ──
                if tipo == "fijo":
                    monto = valor
                else:  # porcentaje
                    # ── Corte de Caja: inicio del período ──
                    ue_raw = regla.get("ultima_ejecucion")
                    inicio_periodo = _calcular_inicio_periodo(
                        ue_raw, tenant_id, cur, proxima_fecha
                    )
                    fin_periodo = proxima_fecha

                    # ── 1. SUM(ganancia_bruta) de ventas activas en el período ──
                    cur.execute(
                        "SELECT COALESCE(SUM(ganancia_bruta), 0) AS total FROM ventas "
                        "WHERE tenant_id = %s AND estado != 'Inactivo' "
                        "AND fecha::date >= %s::date AND fecha::date <= %s::date",
                        (tenant_id, inicio_periodo, fin_periodo)
                    )
                    row = cur.fetchone()
                    ganancia_bruta = float(row["total"]) if row and row["total"] is not None else 0.0

                    # ── 2. SUM(gastos.monto) histórico del período (ANTES de este gasto) ──
                    cur.execute(
                        "SELECT COALESCE(SUM(monto), 0) AS total FROM gastos "
                        "WHERE tenant_id = %s "
                        "AND fecha::date >= %s::date AND fecha::date <= %s::date",
                        (tenant_id, inicio_periodo, fin_periodo)
                    )
                    row = cur.fetchone()
                    total_gastos = float(row["total"]) if row and row["total"] is not None else 0.0

                    # ── 3. Ganancia neta del período (Corte de Caja limpio) ──
                    ganancia_neta_periodo = ganancia_bruta - total_gastos

                    # ── 4. Monto del nuevo gasto = % × ganancia_neta_periodo ──
                    monto = (valor / 100.0) * ganancia_neta_periodo if ganancia_neta_periodo > 0 else 0.0

                # ── Paso C: Insertar gasto pendiente ──
                descripcion_auto = f"{nombre} ({frecuencia.capitalize()} - Automático)"

                cur.execute(
                    "INSERT INTO gastos "
                    "(Fecha, Categoria, Descripcion, Monto, Tenant_ID, Estado, Gasto_Programado_ID) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (proxima_fecha, "Gasto Programado", descripcion_auto,
                     monto, tenant_id, "pendiente", rid)
                )

                # ── Paso D: Avanzar proxima_fecha y marcar ultima_ejecucion ──
                intervalo = _intervalo_sql(frecuencia)
                cur.execute(
                    "UPDATE gastos_programados "
                    "SET proxima_fecha = (proxima_fecha::date + INTERVAL %s)::text, "
                    "    ultima_ejecucion = CURRENT_TIMESTAMP "
                    "WHERE id = %s AND tenant_id = %s",
                    (intervalo, rid, tenant_id)
                )

                generados += 1

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
