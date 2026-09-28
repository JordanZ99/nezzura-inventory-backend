# ==============================================================================
# backend/database/puntos.py
# Libro de movimientos de puntos (ledger). Regla de oro: el saldo SIEMPRE se
# calcula sumando `puntos`, nunca existe una columna "saldo" editable.
#
# Los helpers aceptan `conn` opcional para participar de la transacción atómica
# de cobrar_carrito (Fase B) — mismo patrón que _q/_e de helpers.py.
# ==============================================================================

from psycopg2.extras import RealDictCursor

from database.conexion import query, execute
from database.helpers import _q, _e


def saldo_cliente(tenant_id: str, cliente_id: str, conn=None) -> int:
    """Saldo vigente del cliente (SUM del ledger). Nunca negativo por diseño."""
    filas = _q(conn,
        "SELECT COALESCE(SUM(puntos), 0) AS saldo FROM puntos_movimientos "
        "WHERE tenant_id = %s AND cliente_id = %s",
        (tenant_id, cliente_id)
    )
    return int(filas[0]["saldo"] or 0)


def registrar_movimiento(
    tenant_id: str, cliente_id: str, tipo: str, puntos: int,
    concepto: str | None = None, orden_id: str | None = None,
    valor_monetario=None, conn=None,
) -> None:
    """
    Escribe un movimiento del libro. `puntos` positivo = gana, negativo = gasta.
    `valor_monetario` es el snapshot del valor $ de esos puntos al momento
    (protege la analítica de cambios futuros en el valor del punto).
    """
    _e(conn,
        "INSERT INTO puntos_movimientos (tenant_id, cliente_id, orden_id, tipo, puntos, valor_monetario, concepto) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (tenant_id, cliente_id, orden_id, tipo, int(puntos), valor_monetario, concepto)
    )


def movimientos_cliente(tenant_id: str, cliente_id: str, limite: int = 50) -> list[dict]:
    return query(
        "SELECT id, tipo, puntos, valor_monetario, concepto, fecha, orden_id "
        "FROM puntos_movimientos WHERE tenant_id = %s AND cliente_id = %s "
        "ORDER BY fecha DESC LIMIT %s",
        (tenant_id, cliente_id, limite)
    )


def ajustar_puntos(
    tenant_id: str, cliente_id: str, puntos: int, concepto: str | None = None,
) -> dict:
    """
    Ajuste MANUAL del tenant (desde Estadísticas→Clientes o el POS): dar (+) o
    quitar (−) puntos sueltos, ej. promo especial aplicada a mano. Se valida que
    el saldo NUNCA quede negativo. Tipo 'ajuste' (sin orden asociada).
    """
    puntos = int(puntos)
    if puntos == 0:
        return {"ok": False, "tipo": "validacion", "mensaje": "La cantidad de puntos debe ser distinta de 0"}
    if not concepto or not concepto.strip():
        return {"ok": False, "tipo": "validacion", "mensaje": "Describe el motivo del ajuste (ej. 'Promo 50%')"}

    existe = query(
        "SELECT id, nombre FROM clientes WHERE tenant_id = %s AND id = %s AND activo",
        (tenant_id, cliente_id)
    )
    if not existe:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}

    saldo_actual = saldo_cliente(tenant_id, cliente_id)
    saldo_final = saldo_actual + puntos
    if saldo_final < 0:
        return {
            "ok": False, "tipo": "validacion",
            "mensaje": f"No puedes quitar más puntos de los que tiene (saldo actual: {saldo_actual})",
        }

    registrar_movimiento(
        tenant_id, cliente_id, "ajuste", puntos,
        concepto=concepto.strip(),
    )
    return {
        "ok": True,
        "saldo_anterior": saldo_actual,
        "saldo_nuevo": saldo_final,
        "cliente": existe[0]["nombre"],
    }
