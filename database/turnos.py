# ==============================================================================
# backend/database/turnos.py
# Turnos de caja (Fase C): se abren con fondo de cajón, los cobros con turno
# abierto se les adscriben (ordenes.turno_id, asignado en cobrar_carrito) y al
# cerrar se compara el efectivo contado contra el esperado
# (apertura + cobros en efectivo del turno, incluyendo la parte efectiva de
# pagos mixtos y las propinas cobradas en efectivo).
# ==============================================================================

from psycopg2.errors import UniqueViolation
from psycopg2.extras import RealDictCursor
from database.conexion import get_conn, release_conn, query
from database.helpers import ahora_negocio


def turno_abierto(tenant_id: str) -> dict | None:
    filas = query(
        "SELECT id, abierta_en, monto_apertura FROM turnos "
        "WHERE tenant_id = %s AND estado = 'Abierto' "
        "ORDER BY abierta_en DESC LIMIT 1",
        (tenant_id,)
    )
    return filas[0] if filas else None


def abrir_turno(tenant_id: str, monto_apertura: float) -> dict:
    """
    Abre un turno con fondo de cajón. Un solo turno abierto por tenant
    (índice parcial único en la BD): segundo intento → error amable.
    """
    monto = round(float(monto_apertura or 0), 2)
    if monto < 0:
        return {"ok": False, "mensaje": "El fondo de caja no puede ser negativo"}
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO turnos (tenant_id, estado, abierta_en, monto_apertura) "
                "VALUES (%s, 'Abierto', %s, %s) "
                "RETURNING id, abierta_en, monto_apertura",
                (tenant_id, ahora_negocio(tenant_id), monto)
            )
            turno = cur.fetchone()
        conn.commit()
        return {"ok": True, "turno": {**turno, "num_ordenes": 0, "efectivo_cobrado": 0.0}}
    except UniqueViolation:
        conn.rollback()
        return {"ok": False, "mensaje": "Ya hay un turno abierto. Ciérralo antes de abrir otro."}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al abrir el turno: {e}"}
    finally:
        release_conn(conn)


def cerrar_turno(turno_id: str, tenant_id: str, efectivo_contado: float, notas: str | None) -> dict:
    """
    Cierra el turno con arqueo: efectivo esperado = apertura + cobros en
    efectivo del turno (órdenes activas; pagos mixtos aportan su parte
    efectiva). Diferencia = contado - esperado (positivo = sobrante).
    Todo en una transacción para que ningún cobro entre a mitad del conteo.
    """
    contado = round(float(efectivo_contado or 0), 2)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, monto_apertura, estado FROM turnos "
                "WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                (turno_id, tenant_id)
            )
            turno = cur.fetchone()
            if not turno:
                return {"ok": False, "mensaje": "Turno no encontrado"}
            if turno["estado"] != "Abierto":
                return {"ok": False, "mensaje": "El turno ya está cerrado"}

            cur.execute(
                """
                SELECT COALESCE(SUM((p->>'monto')::numeric), 0) AS efectivo
                FROM ordenes o, jsonb_array_elements(o.pagos) p
                WHERE o.turno_id = %s::uuid AND o.tenant_id = %s
                  AND o.estado = 'Activa' AND p->>'metodo' = 'efectivo'
                """,
                (turno_id, tenant_id)
            )
            efectivo_cobrado = float(cur.fetchone()["efectivo"] or 0)
            esperado = round(float(turno["monto_apertura"] or 0) + efectivo_cobrado, 2)
            diferencia = round(contado - esperado, 2)

            cur.execute(
                "UPDATE turnos SET estado = 'Cerrado', cerrada_en = %s, "
                "efectivo_esperado = %s, efectivo_contado = %s, diferencia = %s, notas = %s "
                "WHERE id = %s::uuid AND tenant_id = %s",
                (ahora_negocio(tenant_id), esperado, contado, diferencia,
                 (notas or "").strip() or None, turno_id, tenant_id)
            )
        conn.commit()
        return {"ok": True, "efectivo_esperado": esperado,
                "efectivo_contado": contado, "diferencia": diferencia}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al cerrar el turno: {e}"}
    finally:
        release_conn(conn)


def listar_turnos(tenant_id: str, limit: int = 50) -> list[dict]:
    """
    Historial de turnos con sus agregados. Para los abiertos, el efectivo
    esperado se calcula EN VIVO (apertura + cobros efectivo hasta ahora);
    para los cerrados se devuelve el almacenado en el cierre.
    """
    filas = query(
        """
        SELECT t.id, t.estado, t.abierta_en, t.cerrada_en, t.monto_apertura,
               t.efectivo_esperado, t.efectivo_contado, t.diferencia, t.notas,
               COALESCE(ag.num_ordenes, 0)  AS num_ordenes,
               COALESCE(ag.total_turno, 0)  AS total_turno,
               COALESCE(ag.efectivo_cobrado, 0) AS efectivo_cobrado
        FROM turnos t
        LEFT JOIN (
            SELECT o.turno_id,
                   COUNT(*) AS num_ordenes,
                   SUM(o.total) AS total_turno,
                   SUM((SELECT COALESCE(SUM((p->>'monto')::numeric), 0)
                        FROM jsonb_array_elements(o.pagos) p
                        WHERE p->>'metodo' = 'efectivo')) AS efectivo_cobrado
            FROM ordenes o
            WHERE o.tenant_id = %s AND o.estado = 'Activa'
            GROUP BY o.turno_id
        ) ag ON ag.turno_id = t.id
        WHERE t.tenant_id = %s
        ORDER BY t.abierta_en DESC
        LIMIT %s
        """,
        (tenant_id, tenant_id, limit)
    )
    for t in filas:
        if t["estado"] == "Abierto":
            t["efectivo_esperado"] = round(float(t["monto_apertura"] or 0) + float(t["efectivo_cobrado"] or 0), 2)
    return filas
