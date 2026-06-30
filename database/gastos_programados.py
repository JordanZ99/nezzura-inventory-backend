# ==============================================================================
# backend/database/gastos_programados.py
# CRUD sobre la tabla gastos_programados + motor de verificación automática.
# ==============================================================================

from database.conexion import execute, get_conn, release_conn
from psycopg2.extras import RealDictCursor
from datetime import date, timedelta


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


def _calcular_periodo(proxima_fecha: str, frecuencia: str) -> tuple[str, str]:
    """
    Calcula la ventana de tiempo (inicio, fin) para determinar
    la ganancia neta según la frecuencia.
    Retorna (fecha_inicio, fecha_fin) como strings ISO.
    """
    pf = date.fromisoformat(proxima_fecha[:10])

    if frecuencia == "semanal":
        inicio = pf - timedelta(days=7)
    elif frecuencia == "mensual":
        inicio = pf - timedelta(days=30)
    elif frecuencia == "anual":
        inicio = pf - timedelta(days=365)
    else:
        inicio = pf - timedelta(days=30)

    fin = pf
    return inicio.isoformat(), fin.isoformat()


def _intervalo_sql(frecuencia: str) -> str:
    """Retorna el intervalo PostgreSQL correspondiente a la frecuencia."""
    mapping = {
        "semanal": "1 week",
        "mensual": "1 month",
        "anual": "1 year",
    }
    return mapping.get(frecuencia, "1 month")


def ejecutar_gasto_programado(regla_id: str, tenant_id: str) -> dict:
    """
    Ejecuta una regla de gasto programado de forma manual.
    - Calcula el monto según el tipo (fijo o porcentaje sobre ventas)
    - Inserta un gasto con estado 'pendiente'
    - Actualiza ultima_ejecucion y proxima_fecha de la regla
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Obtener la regla
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
            proxima_fecha = str(regla["proxima_fecha"]) if regla["proxima_fecha"] else ""
            ultima_ejecucion = regla.get("ultima_ejecucion")

            # ── Calcular monto ──
            if tipo == "fijo":
                monto = valor
            else:  # porcentaje
                # Determinar fecha de inicio del período
                if ultima_ejecucion:
                    inicio = ultima_ejecucion.isoformat() if hasattr(ultima_ejecucion, 'isoformat') else str(ultima_ejecucion)[:19]
                else:
                    # Buscar la venta más antigua registrada
                    cur.execute(
                        "SELECT MIN(fecha) FROM ventas WHERE tenant_id = %s",
                        (tenant_id,)
                    )
                    row = cur.fetchone()
                    min_fecha = row["min"] if row and "min" in row else None
                    inicio = min_fecha.isoformat() if min_fecha else proxima_fecha

                fin = date.today().isoformat()

                # SUM de ventas en el rango
                cur.execute(
                    "SELECT COALESCE(SUM(total_venta), 0) AS total FROM ventas "
                    "WHERE tenant_id = %s AND fecha::date >= %s::date AND fecha::date <= %s::date",
                    (tenant_id, inicio, fin)
                )
                row = cur.fetchone()
                total_ventas = float(row["total"]) if row and row["total"] is not None else 0.0

                monto = (valor / 100.0) * total_ventas if total_ventas > 0 else 0.0

            # ── Si el monto es 0, no insertar gasto basura ──
            if monto <= 0:
                intervalo = _intervalo_sql(frecuencia)
                cur.execute(
                    "UPDATE gastos_programados "
                    "SET ultima_ejecucion = CURRENT_TIMESTAMP, "
                    "    proxima_fecha = (proxima_fecha::date + INTERVAL %s)::text "
                    "WHERE id = %s",
                    (intervalo, rid)
                )
                conn.commit()
                return {
                    "ok": True,
                    "monto": 0,
                    "nombre": nombre,
                    "mensaje": f"Sin ventas en el período para '{nombre}'. Se actualizó la próxima fecha sin generar gasto."
                }

            # ── Insertar gasto (pagado directamente por ser ejecución manual) ──
            descripcion = f"Pago de regla: {nombre}"
            fecha_hoy = date.today().isoformat()
            cur.execute(
                "INSERT INTO gastos "
                "(Fecha, Categoria, Descripcion, Monto, Tenant_ID, Estado, Gasto_Programado_ID) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (fecha_hoy, "Otros", descripcion, monto, tenant_id, "pagado", rid)
            )

            # ── Actualizar la regla ──
            intervalo = _intervalo_sql(frecuencia)
            cur.execute(
                "UPDATE gastos_programados "
                "SET ultima_ejecucion = CURRENT_TIMESTAMP, "
                "    proxima_fecha = (proxima_fecha::date + INTERVAL %s)::text "
                "WHERE id = %s",
                (intervalo, rid)
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
    Motor de verificación:
    1. Busca reglas cuya proxima_fecha <= hoy
    2. Para cada regla vencida:
       - Calcula el monto (fijo o porcentaje)
       - Inserta un gasto con estado 'pendiente'
       - Avanza la proxima_fecha según su frecuencia
    Todo dentro de una sola transacción.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # ── Paso A: Reglas vencidas ──
            cur.execute(
                "SELECT id, nombre, tipo, valor, frecuencia, proxima_fecha "
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
                    inicio_periodo, fin_periodo = _calcular_periodo(proxima_fecha, frecuencia)

                    # Total de ventas en el período
                    cur.execute(
                        "SELECT COALESCE(SUM(total_venta), 0) AS total FROM ventas "
                        "WHERE tenant_id = %s AND fecha::date >= %s AND fecha::date < %s",
                        (tenant_id, inicio_periodo, fin_periodo)
                    )
                    row = cur.fetchone()
                    total_ventas = float(row["total"]) if row and row["total"] is not None else 0.0

                    # Total de gastos pagados en el período
                    cur.execute(
                        "SELECT COALESCE(SUM(monto), 0) AS total FROM gastos "
                        "WHERE tenant_id = %s AND fecha::date >= %s AND fecha::date < %s "
                        "AND estado = 'pagado'",
                        (tenant_id, inicio_periodo, fin_periodo)
                    )
                    row = cur.fetchone()
                    total_gastos = float(row["total"]) if row and row["total"] is not None else 0.0

                    ganancia_neta = total_ventas - total_gastos
                    monto = (valor / 100.0) * ganancia_neta if ganancia_neta > 0 else 0.0

                # ── Paso C: Insertar gasto pendiente ──
                descripcion_auto = f"{nombre} ({frecuencia.capitalize()} - Automático)"

                cur.execute(
                    "INSERT INTO gastos "
                    "(Fecha, Categoria, Descripcion, Monto, Tenant_ID, Estado, Gasto_Programado_ID) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (proxima_fecha, "Gasto Programado", descripcion_auto,
                     monto, tenant_id, "pendiente", rid)
                )

                # ── Paso D: Avanzar proxima_fecha ──
                intervalo = _intervalo_sql(frecuencia)
                cur.execute(
                    "UPDATE gastos_programados "
                    "SET proxima_fecha = (proxima_fecha::date + INTERVAL %s)::text "
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
