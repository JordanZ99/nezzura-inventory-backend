# ==============================================================================
# backend/database/mesas.py
# Mesas del preset restaurante (Fase 2, migración 037).
#
# La orden abierta de una mesa es IMPLÍCITA: mesa en estado 'Ocupada'/'Cuenta'
# tiene renglones en mesa_items; 'Libre' no tiene nada. Al cobrar,
# cobrar_carrito (database/ventas.py) convierte esos renglones en ventas de una
# orden normal DENTRO de su misma transacción y libera la mesa — así stats,
# anulación y turnos no sufren ningún cambio.
# ==============================================================================

import psycopg2.extras
from psycopg2.errors import UniqueViolation
from database.conexion import get_conn, release_conn, query
from database.helpers import ahora_negocio, _parsear_ts


def listar_mesas(tenant_id: str) -> list[dict]:
    """
    Todas las mesas del tenant con sus renglones abiertos anidados y agregados
    (num_items, total) para pintar la parrilla sin peticiones extra.
    """
    filas = query(
        """
        SELECT m.id, m.nombre, m.capacidad, m.orden, m.estado, m.abierta_en, m.creada_en,
               COUNT(i.id)                                        AS num_items,
               COALESCE(SUM(i.cantidad * i.precio_unitario), 0)   AS total
        FROM mesas m
        LEFT JOIN mesa_items i ON i.mesa_id = m.id AND i.tenant_id = m.tenant_id
        WHERE m.tenant_id = %s
        GROUP BY m.id
        ORDER BY m.orden ASC, m.nombre ASC
        """,
        (tenant_id,)
    )
    if not filas:
        return []

    por_mesa = {m["id"]: m for m in filas}
    for m in filas:
        m["items"] = []
    items = query(
        "SELECT id, mesa_id, producto, descripcion, variacion, cantidad, "
        "       precio_unitario, costo, notas, creado_en "
        "FROM mesa_items WHERE tenant_id = %s "
        "ORDER BY creado_en ASC, id ASC",
        (tenant_id,)
    )
    for it in items:
        m = por_mesa.get(it.get("mesa_id"))
        if m is not None:
            m["items"].append(it)
    return filas


def _validar_nombre(conn, nombre: str, tenant_id: str, excluir_id: str | None = None) -> str | None:
    """Devuelve mensaje de error si el nombre choca con otra mesa del tenant."""
    cur = conn.cursor()
    if excluir_id:
        cur.execute(
            "SELECT 1 FROM mesas WHERE tenant_id = %s AND LOWER(nombre) = LOWER(%s) AND id != %s::uuid",
            (tenant_id, nombre, excluir_id)
        )
    else:
        cur.execute(
            "SELECT 1 FROM mesas WHERE tenant_id = %s AND LOWER(nombre) = LOWER(%s)",
            (tenant_id, nombre)
        )
    return "Ya existe una mesa con ese nombre" if cur.fetchone() else None


def crear_mesa(tenant_id: str, nombre: str, capacidad: int | None, orden: int | None = None) -> dict:
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "tipo": "validacion", "mensaje": "El nombre de la mesa es obligatorio"}

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            error = _validar_nombre(conn, nombre, tenant_id)
            if error:
                conn.rollback()
                return {"ok": False, "tipo": "validacion", "mensaje": error}
            # Nueva mesa al final de la parrilla (máx orden + 1) si no viene orden
            if orden is None:
                cur.execute("SELECT COALESCE(MAX(orden), 0) AS mx FROM mesas WHERE tenant_id = %s", (tenant_id,))
                orden = int(cur.fetchone()["mx"]) + 1
            cur.execute(
                "INSERT INTO mesas (tenant_id, nombre, capacidad, orden) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (tenant_id, nombre, capacidad, orden)
            )
            fila = cur.fetchone()
        conn.commit()
        return {"ok": True, "id": str(fila["id"]), "nombre": nombre}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al crear la mesa: {str(e)}"}
    finally:
        release_conn(conn)


def actualizar_mesa(mesa_id: str, tenant_id: str, nombre: str | None = None,
                    capacidad: int | None = None) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM mesas WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                (mesa_id, tenant_id)
            )
            if not cur.fetchone():
                conn.rollback()
                return {"ok": False, "mensaje": "Mesa no encontrada"}

            campos: list[str] = []
            params: list = []
            if nombre is not None:
                nombre = nombre.strip()
                if not nombre:
                    conn.rollback()
                    return {"ok": False, "tipo": "validacion", "mensaje": "El nombre no puede quedar vacío"}
                error = _validar_nombre(conn, nombre, tenant_id, excluir_id=mesa_id)
                if error:
                    conn.rollback()
                    return {"ok": False, "tipo": "validacion", "mensaje": error}
                campos.append("nombre = %s")
                params.append(nombre)
            if capacidad is not None:
                campos.append("capacidad = %s")
                params.append(capacidad)
            if campos:
                params.append(mesa_id)
                params.append(tenant_id)
                cur.execute(f"UPDATE mesas SET {', '.join(campos)} WHERE id = %s::uuid AND tenant_id = %s", tuple(params))
        conn.commit()
        return {"ok": True, "id": mesa_id}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al actualizar la mesa: {str(e)}"}
    finally:
        release_conn(conn)


def eliminar_mesa(mesa_id: str, tenant_id: str) -> dict:
    """Solo se puede eliminar una mesa Libre (sin orden abierta)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT estado FROM mesas WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                (mesa_id, tenant_id)
            )
            fila = cur.fetchone()
            if not fila:
                conn.rollback()
                return {"ok": False, "mensaje": "Mesa no encontrada"}
            if fila["estado"] != "Libre":
                conn.rollback()
                return {"ok": False, "tipo": "validacion",
                        "mensaje": "La mesa tiene una orden abierta. Cancélala o cóbrala antes de eliminarla."}
            cur.execute("DELETE FROM mesas WHERE id = %s::uuid AND tenant_id = %s", (mesa_id, tenant_id))
        conn.commit()
        return {"ok": True, "id": mesa_id}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al eliminar la mesa: {str(e)}"}
    finally:
        release_conn(conn)


def reordenar_mesas(tenant_id: str, ordenes: list[dict]) -> dict:
    """Guarda la posición de la parrilla tras un drag & drop: [{id, orden}]."""
    if not ordenes:
        return {"ok": True}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for fila in ordenes:
                cur.execute(
                    "UPDATE mesas SET orden = %s WHERE id = %s::uuid AND tenant_id = %s",
                    (int(fila["orden"]), fila["id"], tenant_id)
                )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al reordenar las mesas: {str(e)}"}
    finally:
        release_conn(conn)


def _validar_item(cur, tenant_id: str, it: dict) -> str | None:
    """
    Valida un renglón que se quiere agregar a la mesa: el producto debe existir
    (incluido el genérico 'Venta libre' con su descripción) y cantidad/precio
    deben ser coherentes. Devuelve mensaje de error o None.
    """
    producto = (it.get("producto") or "").strip()
    if not producto:
        return "El renglón no tiene producto"
    if float(it.get("cantidad") or 0) <= 0:
        return f"La cantidad de '{producto}' debe ser mayor a 0"
    if float(it.get("precio_unitario") or 0) < 0:
        return f"El precio de '{producto}' no puede ser negativo"
    cur.execute(
        "SELECT 1 FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id)
    )
    if not cur.fetchone():
        return f"El producto '{producto}' no existe"
    return None


def agregar_items_mesa(mesa_id: str, tenant_id: str, items: list[dict]) -> dict:
    """
    Agrega renglones a la orden abierta de la mesa. Si la mesa está Libre, se
    considera ABIERTA la orden: pasa a Ocupada con su instante (abierta_en).
    Todos los renglones se validan ANTES de insertar (todo-o-nada).
    """
    if not items:
        return {"ok": False, "tipo": "validacion", "mensaje": "No hay artículos que agregar"}

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, estado FROM mesas WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                (mesa_id, tenant_id)
            )
            mesa = cur.fetchone()
            if not mesa:
                conn.rollback()
                return {"ok": False, "mensaje": "Mesa no encontrada"}

            for it in items:
                error = _validar_item(cur, tenant_id, it)
                if error:
                    conn.rollback()
                    return {"ok": False, "tipo": "validacion", "mensaje": error}

            if mesa["estado"] == "Libre":
                cur.execute(
                    "UPDATE mesas SET estado = 'Ocupada', abierta_en = %s "
                    "WHERE id = %s::uuid AND tenant_id = %s",
                    (_parsear_ts(str(ahora_negocio(tenant_id))), mesa_id, tenant_id)
                )
            elif mesa["estado"] == "Cuenta":
                conn.rollback()
                return {"ok": False, "tipo": "validacion",
                        "mensaje": "La mesa está en cuenta. Regrésala a Ocupada para seguir agregando."}

            for it in items:
                cur.execute(
                    "INSERT INTO mesa_items (tenant_id, mesa_id, producto, descripcion, variacion, "
                    "                        cantidad, precio_unitario, costo, notas) "
                    "VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        tenant_id,
                        mesa_id,
                        str(it["producto"]).strip(),
                        (str(it.get("descripcion")).strip() or None) if it.get("descripcion") else None,
                        (str(it.get("variacion")).strip() or None) if it.get("variacion") else None,
                        float(it["cantidad"]),
                        float(it.get("precio_unitario") or 0),
                        (float(it["costo"]) if it.get("costo") is not None else None),
                        (str(it.get("notas")).strip() or None) if it.get("notas") else None,
                    )
                )
        conn.commit()
        return {"ok": True, "mesa_id": mesa_id, "agregados": len(items)}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al agregar a la mesa: {str(e)}"}
    finally:
        release_conn(conn)


def editar_item_mesa(mesa_id: str, item_id: str, tenant_id: str,
                     cantidad: float | None = None, precio_unitario: float | None = None,
                     notas: str | None = None) -> dict:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM mesas WHERE id = %s::uuid AND tenant_id = %s",
                (mesa_id, tenant_id)
            )
            if not cur.fetchone():
                conn.rollback()
                return {"ok": False, "mensaje": "Mesa no encontrada"}
            if cantidad is not None and float(cantidad) <= 0:
                conn.rollback()
                return {"ok": False, "tipo": "validacion", "mensaje": "La cantidad debe ser mayor a 0"}
            if precio_unitario is not None and float(precio_unitario) < 0:
                conn.rollback()
                return {"ok": False, "tipo": "validacion", "mensaje": "El precio no puede ser negativo"}
            cur.execute(
                "UPDATE mesa_items SET "
                "cantidad = COALESCE(%s, cantidad), "
                "precio_unitario = COALESCE(%s, precio_unitario), "
                "notas = COALESCE(%s, notas) "
                "WHERE id = %s::uuid AND mesa_id = %s::uuid AND tenant_id = %s",
                (cantidad, precio_unitario, notas, item_id, mesa_id, tenant_id)
            )
            if cur.rowcount == 0:
                conn.rollback()
                return {"ok": False, "mensaje": "Renglón no encontrado"}
        conn.commit()
        return {"ok": True, "id": item_id}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al editar el renglón: {str(e)}"}
    finally:
        release_conn(conn)


def quitar_item_mesa(mesa_id: str, item_id: str, tenant_id: str) -> dict:
    """
    Quita un renglón de la orden abierta. Si la mesa queda sin renglones, la
    orden se considera nunca abierta: mesa Libre de nuevo.
    """
    conn = get_conn()
    try:
        # RealDictCursor: el COUNT se lee como fetchone()["c"]; con el cursor
        # pelado de psycopg2 fetchone() devuelve tuplas y lanzaría TypeError
        # (bug del mismo origen que cancelar orden).
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "DELETE FROM mesa_items WHERE id = %s::uuid AND mesa_id = %s::uuid AND tenant_id = %s",
                (item_id, mesa_id, tenant_id)
            )
            if cur.rowcount == 0:
                conn.rollback()
                return {"ok": False, "mensaje": "Renglón no encontrado"}
            cur.execute("SELECT COUNT(*) AS c FROM mesa_items WHERE mesa_id = %s::uuid AND tenant_id = %s", (mesa_id, tenant_id))
            if int(cur.fetchone()["c"]) == 0:
                cur.execute(
                    "UPDATE mesas SET estado = 'Libre', abierta_en = NULL "
                    "WHERE id = %s::uuid AND tenant_id = %s",
                    (mesa_id, tenant_id)
                )
        conn.commit()
        return {"ok": True, "id": item_id}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al quitar el renglón: {str(e)}"}
    finally:
        release_conn(conn)


def pedir_cuenta(mesa_id: str, tenant_id: str) -> dict:
    """Marca la mesa en 'Cuenta' (para que el mesero sepa que ya pidieron la bill)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mesas SET estado = 'Cuenta' "
                "WHERE id = %s::uuid AND tenant_id = %s AND estado = 'Ocupada'",
                (mesa_id, tenant_id)
            )
            if cur.rowcount == 0:
                conn.rollback()
                return {"ok": False, "tipo": "validacion",
                        "mensaje": "Solo una mesa Ocupada puede pasar a Cuenta"}
        conn.commit()
        return {"ok": True, "id": mesa_id, "estado": "Cuenta"}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al pedir la cuenta: {str(e)}"}
    finally:
        release_conn(conn)


def regresar_a_ocupada(mesa_id: str, tenant_id: str) -> dict:
    """Saca la mesa de 'Cuenta' de vuelta a 'Ocupada' (se equivocaron de mesa)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mesas SET estado = 'Ocupada' "
                "WHERE id = %s::uuid AND tenant_id = %s AND estado = 'Cuenta'",
                (mesa_id, tenant_id)
            )
            if cur.rowcount == 0:
                conn.rollback()
                return {"ok": False, "tipo": "validacion",
                        "mensaje": "Solo una mesa en Cuenta puede regresar a Ocupada"}
        conn.commit()
        return {"ok": True, "id": mesa_id, "estado": "Ocupada"}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al actualizar la mesa: {str(e)}"}
    finally:
        release_conn(conn)


def cancelar_orden_mesa(mesa_id: str, tenant_id: str) -> dict:
    """
    Cancela la orden abierta SIN cobrar: borra sus renglones y libera la mesa.
    Nada llegó a ventas/ordenes (el stock no se tocó: se descuenta al cobrar),
    así que no hay nada que revertir.
    """
    conn = get_conn()
    try:
        # RealDictCursor: fetchone() debe devolver un dict para poder leer
        # fila["estado"]; el cursor pelado de psycopg2 devuelve tuplas y
        # lanzaría TypeError al indexar con texto (bug de cancelar orden).
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT estado FROM mesas WHERE id = %s::uuid AND tenant_id = %s FOR UPDATE",
                (mesa_id, tenant_id)
            )
            fila = cur.fetchone()
            if not fila:
                conn.rollback()
                return {"ok": False, "mensaje": "Mesa no encontrada"}
            if fila["estado"] == "Libre":
                conn.rollback()
                return {"ok": False, "tipo": "validacion", "mensaje": "La mesa ya está libre"}
            cur.execute("DELETE FROM mesa_items WHERE mesa_id = %s::uuid AND tenant_id = %s", (mesa_id, tenant_id))
            cancelados = cur.rowcount
            cur.execute(
                "UPDATE mesas SET estado = 'Libre', abierta_en = NULL WHERE id = %s::uuid AND tenant_id = %s",
                (mesa_id, tenant_id)
            )
        conn.commit()
        return {"ok": True, "id": mesa_id, "cancelados": cancelados}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "mensaje": f"Error al cancelar la orden: {str(e)}"}
    finally:
        release_conn(conn)
