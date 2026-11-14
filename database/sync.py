# ==============================================================================
# backend/database/sync.py
# Sincronización del Modo Offline (híbrido): plan básico/plus usa repos locales
# y al reconectar sube su delta / baja el delta de la nube.
#
# Estrategia (por tipo de dato, no un único "merge"):
#   * ordenes/ventas/gastos  → append-only con id generada por el dispositivo;
#     idempotencia por client_uid (reenviar = no-op). Dos cajas vendiendo
#     a la vez es una UNIÓN, no un conflicto.
#   * lotes/stock            → NUNCA se sube el stock como valor: por cada
#     venta nueva se repite la salida con descontar_stock_peps usando el
#     id_lote que PEPS local ya asignó. Lote en negativo = alerta, no error
#     (es la verdad del negocio; el conteo de auditoría la arregla).
#   * productos/ediciones    → LWW por llegada al servidor, con auditoría en
#     sync_log (valor previo + resolución). Si la nube tenía un cambio más
#     nuevo que el cursor del dispositivo (divergencia real), se marca.
#   * resoluciones por NOMBRE: local usa nombres (productos/variaciones);
#     el backend resuelve ids al aplicar (variaciones por (producto, nombre),
#     que tiene índice único).
#
# Autenticación: sesión Supabase (Depends(get_tenant_id)) — el modo híbrido
# no usa licencias de dispositivo.
# ==============================================================================

import base64
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor

from database.conexion import get_conn, release_conn
from database.helpers import _parsear_ts, fecha_negocio_de
from database.lotes import descontar_stock_peps
from database.ventas import _registrar_ledger_inventario


class _ErrorSync(Exception):
    """Error de dominio: status HTTP + mensaje para el usuario."""

    def __init__(self, status: int, mensaje: str):
        self.status = status
        self.mensaje = mensaje
        super().__init__(mensaje)


# Mapeo de estados de gasto: local (Activo/Pendiente) → nube (pagado/pendiente)
_MAP_GASTO_ESTADO = {"Activo": "pagado", "Pendiente": "pendiente"}


def _decodificar_imagen(dataurl: str) -> bytes | None:
    """dataURL (data:image/...;base64,XXX) → bytes. None si no es dataURL."""
    if not dataurl or not str(dataurl).startswith("data:"):
        return None
    try:
        base64_data = str(dataurl).split(",", 1)[1]
        return base64.b64decode(base64_data)
    except Exception:
        return None


def _subir_imagen_nube(producto: str, valor, tenant_id: str, conn) -> str:
    """Si el valor es dataURL lo sube a Cloudinary y devuelve la URL; si ya es
    URL lo devuelve tal cual; si falla, 'No hay foto' (nunca dataURL en la DB
    de la nube: rompería el catálogo público)."""
    from database.imagenes import subir_foto, _es_imagen_valida

    contenido = _decodificar_imagen(valor or "")
    if contenido is None:
        # URL existente (sync de ida y vuelta) o vacío
        return str(valor or "") if valor and str(valor).startswith("http") else "No hay foto"
    if not _es_imagen_valida(contenido):
        return "No hay foto"
    resultado = subir_foto(producto, contenido, tenant_id)
    if resultado.get("ok"):
        return resultado.get("ruta") or "No hay foto"
    return "No hay foto"


def _producto_id(tenant_id: str, producto: str, cur) -> int | None:
    cur.execute(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto, tenant_id),
    )
    fila = cur.fetchone()
    return fila[0] if fila else None


def _variacion_id(tenant_id: str, producto_id: int, nombre: str, cur) -> int | None:
    if not nombre:
        return None
    cur.execute(
        "SELECT id FROM producto_variaciones WHERE producto_id = %s AND nombre = %s AND tenant_id = %s",
        (producto_id, nombre, tenant_id),
    )
    fila = cur.fetchone()
    return fila[0] if fila else None


def _asegurar_producto(tenant_id: str, fila: dict, cur, resultados: dict) -> int | None:
    """Inserta (o actualiza superficialmente) un producto creado offline.
    Devuelve su id de nube (None si la fila es inválida)."""
    nombre = str(fila.get("producto") or "").strip()
    if not nombre:
        return None

    imagen = _subir_imagen_nube(nombre, fila.get("imagen"), tenant_id, cur)
    existente = _producto_id(tenant_id, nombre, cur)
    if existente is None:
        cur.execute(
            """
            INSERT INTO productos
                (Producto, Descripcion, Imagen, Estado, categoria, tenant_id,
                 codigo_interno, codigo_barras, ubicacion, compuesto,
                 visible_en_catalogo, sufijo_precio, tipo_producto,
                 costo_servicio, precio_servicio, fraccionable, tamano_cm)
            VALUES (%s, %s, %s, %s, %s::text[], %s, %s, %s, %s, FALSE,
                    %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                nombre,
                str(fila.get("descripcion") or ""),
                imagen,
                "Activo",
                fila.get("categoria") or [],
                tenant_id,
                fila.get("codigo_interno") or None,
                fila.get("codigo_barras") or None,
                fila.get("ubicacion") or None,
                bool(fila.get("visible_en_catalogo", True)),
                fila.get("sufijo_precio") or None,
                fila.get("tipo_producto") or "stock",
                fila.get("costo_servicio"),
                fila.get("precio_servicio"),
                bool(fila.get("fraccionable", False)),
                fila.get("tamano_cm"),
            ),
        )
        producto_id = cur.fetchone()[0]
    else:
        producto_id = existente

    # Pivot de categorías (el catálogo público lee de aquí): nombres → ids.
    for cat_nombre in fila.get("categoria") or []:
        if not cat_nombre:
            continue
        cur.execute(
            "SELECT id FROM categorias WHERE nombre = %s AND tenant_id = %s",
            (cat_nombre, tenant_id),
        )
        cat = cur.fetchone()
        if cat is None:
            cur.execute(
                "INSERT INTO categorias (tenant_id, nombre, slug) VALUES (%s, %s, %s) RETURNING id",
                (tenant_id, cat_nombre, cat_nombre.lower().replace(" ", "-")),
            )
            cat = cur.fetchone()
        cur.execute(
            "SELECT 1 FROM producto_categorias WHERE producto_id = %s AND categoria_id = %s",
            (producto_id, cat[0]),
        )
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO producto_categorias (producto_id, categoria_id) VALUES (%s, %s)",
                (producto_id, cat[0]),
            )

    resultados["productos"] += 1
    return producto_id


def _aplicar_variacion(tenant_id: str, fila: dict, cur, resultados: dict) -> None:
    nombre = str(fila.get("producto") or "").strip()
    nombre_var = str(fila.get("nombre") or "").strip()
    producto_id = _producto_id(tenant_id, nombre, cur)
    if not producto_id or not nombre_var:
        return
    foto = _subir_imagen_nube(f"{nombre}_{nombre_var}", fila.get("foto"), tenant_id, cur)
    if foto == "No hay foto":
        foto = None
    cur.execute(
        """
        INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id, foto, tamano_cm)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (producto_id, nombre) DO UPDATE
            SET precio = EXCLUDED.precio, foto = EXCLUDED.foto, tamano_cm = EXCLUDED.tamano_cm
        """,
        (
            producto_id, nombre_var, float(fila.get("precio") or 0), tenant_id,
            foto, fila.get("tamano_cm"),
        ),
    )
    resultados["variaciones"] += 1


def _aplicar_lote(tenant_id: str, fila: dict, cur, resultados: dict) -> str | None:
    """Upsert por (tenant_id, id_lote): el restock offline entra como lote
    nuevo; reenviar el mismo = no-op. Devuelve el id_lote o None si se omitió."""
    id_lote = str(fila.get("id_lote") or "").strip()
    nombre = str(fila.get("producto") or "").strip()
    if not id_lote or not nombre:
        return None
    producto_id = _producto_id(tenant_id, nombre, cur)
    if producto_id is None:
        return None  # el producto llega en su sección; sin él el lote no aplica
    variacion_id = _variacion_id(tenant_id, producto_id, str(fila.get("variacion") or ""), cur)
    cur.execute(
        """
        INSERT INTO lotes
            (ID_Lote, Producto, producto_id, Costo, Precio_Venta, Stock_Lote,
             Fecha_Entrada, fecha_entrada_ts, Estado, tenant_id, etiqueta, variacion_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Activo', %s, %s, %s)
        ON CONFLICT (tenant_id, id_lote) DO NOTHING
        """,
        (
            id_lote, nombre, producto_id,
            float(fila.get("costo") or 0), float(fila.get("precio_venta") or 0),
            float(fila.get("stock_lote") or 0),
            fila.get("fecha_entrada") or "",
            _parsear_ts(fila.get("fecha_entrada") or None),
            tenant_id, fila.get("etiqueta") or None, variacion_id,
        ),
    )
    if cur.rowcount:
        resultados["lotes"] += 1
    return id_lote


def _aplicar_orden(tenant_id: str, paquete: dict, cur, resultados: dict, alertas: list, omitidas: dict) -> None:
    orden = paquete.get("orden") or {}
    ventas = paquete.get("ventas") or []
    orden_id = str(orden.get("orden_id") or "").strip()
    try:
        orden_uuid = uuid.UUID(orden_id)
    except ValueError:
        return  # orden_id inválido: se omite el paquete (nunca debe ocurrir)
    if not ventas:
        return

    # 1. Orden: el id ES el UUID del dispositivo (INSERT ON CONFLICT = no-op
    #    en reenvíos). Turnos/clientes quedan fuera del MVP de la sync.
    cur.execute(
        """
        INSERT INTO ordenes
            (id, tenant_id, n_ticket, fecha_ts, total, ganancia, cantidad_items,
             estado, metodo_pago, pagos, propina, monto_recibido, cambio,
             comision_total, mesa_nombre)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'Activa', %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (
            orden_uuid, tenant_id, orden.get("n_ticket"),
            _parsear_ts(orden.get("fecha") or None),
            float(orden.get("total") or 0), float(orden.get("ganancia") or 0),
            float(orden.get("cantidad_items") or 0),
            orden.get("metodo_pago") or None,
            psycopg2.extras.Json(orden.get("pagos") or None),
            orden.get("propina"), orden.get("monto_recibido"),
            orden.get("cambio"), orden.get("comision_total"),
            orden.get("mesa_nombre") or None,
        ),
    )
    if cur.rowcount:
        resultados["ordenes"] += 1

    # 2. Ventas + replay de stock (solo si la venta es nueva).
    for idx, v in enumerate(ventas):
        producto = str(v.get("producto") or "").strip()
        uid = str(v.get("uid") or "").strip()
        if not producto or not uid:
            continue
        producto_id = _producto_id(tenant_id, producto, cur)
        if producto_id is None:
            omitidas["ventas_sin_producto"] += 1
            continue
        variacion_id = _variacion_id(tenant_id, producto_id, str(v.get("variacion") or ""), cur)

        cur.execute(
            """
            INSERT INTO ventas
                (client_uid, Fecha, fecha_ts, Producto, producto_id, Cantidad,
                 Precio_Lista, Precio_Real, Costo_Unitario, Total_Venta,
                 Ganancia_Bruta, Estado, ID_Lote, tenant_id, tipo_producto,
                 variacion, consumo, orden_id, n_ticket, descripcion, categoria)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Activa', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, client_uid) WHERE client_uid IS NOT NULL DO NOTHING
            """,
            (
                uid, v.get("fecha") or "", _parsear_ts(v.get("fecha") or None),
                producto, producto_id, float(v.get("cantidad") or 0),
                float(v.get("precio_lista") or 0), float(v.get("precio_real") or 0),
                float(v.get("costo_unitario") or 0), float(v.get("total_venta") or 0),
                float(v.get("ganancia_bruta") or 0),
                v.get("id_lote") or None, tenant_id,
                v.get("tipo_producto") or "stock",
                v.get("variacion") or None,
                psycopg2.extras.Json(v.get("consumo") or None),
                orden_uuid, v.get("n_ticket"),
                v.get("descripcion") or None, v.get("categoria") or None,
            ),
        )
        if not cur.rowcount:
            continue  # ya estaba subida (reintento) → stock ya descontado

        resultados["ventas"] += 1
        tipo_v = v.get("tipo_producto") or "stock"

        # 3. Replay del stock con el MISMO lote que PEPS local asignó offline.
        if tipo_v == "compuesto":
            for c in v.get("consumo") or []:
                descontar_stock_peps(
                    c.get("material") or "", float(c.get("cantidad") or 0),
                    float(v.get("precio_real") or 0), tenant_id,
                    id_lote=c.get("id_lote"), conn=cur.connection,
                )
        elif tipo_v != "servicio":
            descontar_stock_peps(
                producto, float(v.get("cantidad") or 0),
                float(v.get("precio_real") or 0), tenant_id,
                id_lote=v.get("id_lote") or None, variacion_id=variacion_id,
                conn=cur.connection,
            )

        # 4. Ledger (inventario_movimientos): misma forma que el cobro en nube.
        _registrar_ledger_inventario(cur.connection, tenant_id, [v], orden_id, v.get("n_ticket"))

    # 5. Alertas: lotes en negativo = la verdad (dos cajas vendieron de fondo).
    cur.execute(
        "SELECT ID_Lote, Producto, Stock_Lote FROM lotes WHERE tenant_id = %s AND Stock_Lote < 0",
        (tenant_id,),
    )
    for fila in cur.fetchall():
        alertas.append({
            "tipo": "stock_negativo", "id_lote": fila[0],
            "producto": fila[1], "stock": float(fila[2] or 0),
        })


def _aplicar_gasto(tenant_id: str, fila: dict, cur, resultados: dict) -> None:
    uid = str(fila.get("uid") or "").strip()
    if not uid:
        return
    fecha = str(fila.get("fecha") or "")
    cur.execute(
        """
        INSERT INTO gastos
            (client_uid, Fecha, fecha_negocio, Categoria, Descripcion, Monto,
             Tenant_ID, Estado)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, client_uid) WHERE client_uid IS NOT NULL DO NOTHING
        """,
        (
            uid, fecha, fecha_negocio_de(fecha, tenant_id),
            fila.get("categoria") or "General",
            fila.get("descripcion") or "",
            float(fila.get("monto") or 0), tenant_id,
            _MAP_GASTO_ESTADO.get(fila.get("estado") or "Activo", "pagado"),
        ),
    )
    if cur.rowcount:
        resultados["gastos"] += 1


_CAMPOS_PRODUCTO_EDITABLES = (
    "descripcion", "sufijo_precio", "tamano_cm", "visible_en_catalogo",
    "ubicacion", "codigo_interno", "codigo_barras",
)


def _aplicar_edicion_producto(
    tenant_id: str, fila: dict, cursor_dispositivo, cur, resultados: dict, divergencias: list,
) -> None:
    """LWW por llegada: gana el último edit que llegó al servidor. El valor
    previo queda en sync_log; si la nube tenía un cambio más nuevo que el
    cursor del dispositivo (no lo había visto al editar) se marca divergencia."""
    nombre = str(fila.get("producto") or "").strip()
    campos = fila.get("campos") or {}
    if not nombre or not campos:
        return
    producto_id = _producto_id(tenant_id, nombre, cur)
    if producto_id is None:
        return

    cur.execute(
        "SELECT updated_at, Descripcion, Imagen, sufijo_precio, tamano_cm, "
        "visible_en_catalogo, ubicacion, codigo_interno, codigo_barras "
        "FROM productos WHERE id = %s AND tenant_id = %s FOR UPDATE",
        (producto_id, tenant_id),
    )
    previa = cur.fetchone()
    previa_ts = previa[0] if previa else None
    if previa and previa_ts and cursor_dispositivo and previa_ts > cursor_dispositivo:
        divergencias.append({"tabla": "productos", "clave": nombre})

    valores = {}
    if "imagen" in campos and campos["imagen"]:
        valores["Imagen"] = _subir_imagen_nube(nombre, campos["imagen"], tenant_id, cur)
    for campo, columna in [
        ("descripcion", "Descripcion"), ("sufijo_precio", "sufijo_precio"),
        ("tamano_cm", "tamano_cm"), ("visible_en_catalogo", "visible_en_catalogo"),
        ("ubicacion", "ubicacion"), ("codigo_interno", "codigo_interno"),
        ("codigo_barras", "codigo_barras"),
    ]:
        if campo in campos:
            valores[columna] = campos[campo]

    if valores:
        sets = ", ".join(f"{col} = %s" for col in valores)
        cur.execute(
            f"UPDATE productos SET {sets} WHERE id = %s AND tenant_id = %s",
            (*valores.values(), producto_id, tenant_id),
        )
        prev_dict = {
            "descripcion": previa[1], "imagen": previa[2], "sufijo_precio": previa[3],
            "tamano_cm": float(previa[4]) if previa[4] is not None else None,
            "visible_en_catalogo": previa[5], "ubicacion": previa[6],
            "codigo_interno": previa[7], "codigo_barras": previa[8],
        } if previa else {}
        cur.execute(
            "INSERT INTO sync_log (tenant_id, tabla, clave, valor_previo, valor_nuevo, resolucion) "
            "VALUES (%s, 'productos', %s, %s::jsonb, %s::jsonb, 'lww_dispositivo_gana')",
            (tenant_id, nombre, psycopg2.extras.Json(prev_dict),
             psycopg2.extras.Json(valores)),
        )
        resultados["ediciones_productos"] += 1


def _aplicar_edicion_lote(
    tenant_id: str, fila: dict, cursor_dispositivo, cur, resultados: dict, divergencias: list,
) -> None:
    """LWW del precio/estado de un lote por id_lote (las ediciones de precio
    offline editan el lote: el precio de venta vive ahí, no en productos)."""
    id_lote = str(fila.get("id_lote") or "").strip()
    campos = fila.get("campos") or {}
    if not id_lote or not campos:
        return
    cur.execute(
        "SELECT updated_at, Precio_Venta, Estado, Costo FROM lotes "
        "WHERE ID_Lote = %s AND tenant_id = %s FOR UPDATE",
        (id_lote, tenant_id),
    )
    previa = cur.fetchone()
    if previa is None:
        return
    if previa[0] and cursor_dispositivo and previa[0] > cursor_dispositivo:
        divergencias.append({"tabla": "lotes", "clave": id_lote})

    valores = {}
    if "precio_venta" in campos:
        valores["Precio_Venta"] = float(campos["precio_venta"] or 0)
    if "costo" in campos:
        valores["Costo"] = float(campos["costo"] or 0)
    if "estado" in campos:
        valores["Estado"] = campos["estado"]
    if valores:
        sets = ", ".join(f"{col} = %s" for col in valores)
        cur.execute(
            f"UPDATE lotes SET {sets} WHERE ID_Lote = %s AND tenant_id = %s",
            (*valores.values(), id_lote, tenant_id),
        )
        prev_dict = {"precio_venta": float(previa[1] or 0), "estado": previa[2],
                     "costo": float(previa[3] or 0)}
        cur.execute(
            "INSERT INTO sync_log (tenant_id, tabla, clave, valor_previo, valor_nuevo, resolucion) "
            "VALUES (%s, 'lotes', %s, %s::jsonb, %s::jsonb, 'lww_dispositivo_gana')",
            (tenant_id, id_lote, psycopg2.extras.Json(prev_dict),
             psycopg2.extras.Json(valores)),
        )
        resultados["ediciones_lotes"] += 1


def subir_delta(tenant_id: str, payload: dict) -> dict:
    """Aplica el delta del dispositivo en UNA transacción (atómica: si algo
    falla a la mitad, nada quedó aplicado y el dispositivo reintenta igual)."""
    desde = payload.get("desde") or None
    resultados = {
        "productos": 0, "variaciones": 0, "lotes": 0, "ordenes": 0,
        "ventas": 0, "gastos": 0, "ediciones_productos": 0, "ediciones_lotes": 0,
    }
    omitidas = {"ventas_sin_producto": 0, "lotes_sin_producto": 0}
    divergencias: list = []
    alertas: list = []

    conn = get_conn()
    try:
        # Cursor de TUPLAS (no RealDict): las funciones de arriba indexan por
        # posición (RETURNING id → fila[0]).
        with conn.cursor() as cur:
            # Orden estricta: productos → variaciones → lotes → ediciones →
            # gastos → órdenes/ventas (el replay de stock necesita lotes ya
            # subidos en esta misma transacción).
            for fila in payload.get("productos") or []:
                _asegurar_producto(tenant_id, fila, cur, resultados)
            for fila in payload.get("variaciones") or []:
                _aplicar_variacion(tenant_id, fila, cur, resultados)
            for fila in payload.get("lotes") or []:
                if _aplicar_lote(tenant_id, fila, cur, resultados) is None:
                    omitidas["lotes_sin_producto"] += 1
            for fila in payload.get("ediciones_productos") or []:
                _aplicar_edicion_producto(tenant_id, fila, desde, cur, resultados, divergencias)
            for fila in payload.get("ediciones_lotes") or []:
                _aplicar_edicion_lote(tenant_id, fila, desde, cur, resultados, divergencias)
            for fila in payload.get("gastos") or []:
                _aplicar_gasto(tenant_id, fila, cur, resultados)
            for paquete in payload.get("ordenes") or []:
                _aplicar_orden(tenant_id, paquete, cur, resultados, alertas, omitidas)

            # Duplicados de alertas (varias ventas tocan el mismo lote):
            vistos = set()
            alertas = [
                a for a in alertas
                if a["id_lote"] not in vistos and not vistos.add(a["id_lote"])
            ]

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise _ErrorSync(500, f"Error aplicando el delta de sincronización: {e}")
    finally:
        release_conn(conn)

    return {"ok": True, "subidas": resultados, "omitidas": omitidas,
            "divergencias": divergencias, "alertas": alertas}


def bajar_delta(tenant_id: str, desde) -> dict:
    """Filas de la nube con updated_at > cursor, en la forma que el
    dispositivo ya entiende (productos por NOMBRE, lotes por id_lote).
    El cursor devuelto se captura ANTES de seleccionar: lo que cambie durante
    la respuesta llega en la próxima sincronización."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT clock_timestamp() AS ts")
            cursor = cur.fetchone()["ts"]

            # Primera sincronización (sin cursor): trae TODO el negocio —
            # las filas pre-migración quedaron con updated_at = momento de 070,
            # siempre mayor que epoch.
            desde_eff = desde or "1970-01-01T00:00:00+00:00"

            cur.execute(
                "SELECT Producto, Descripcion, Imagen, Estado, categoria, "
                "codigo_interno, codigo_barras, ubicacion, visible_en_catalogo, "
                "sufijo_precio, tamano_cm, tipo_producto, costo_servicio, "
                "precio_servicio, fraccionable, updated_at "
                "FROM productos WHERE tenant_id = %s AND updated_at > %s AND updated_at <= %s "
                "ORDER BY updated_at ASC",
                (tenant_id, desde_eff, cursor),
            )
            productos = [dict(f) for f in cur.fetchall()]

            cur.execute(
                "SELECT nombre, slug, orden, visible_en_catalogo FROM categorias "
                "WHERE tenant_id = %s AND updated_at > %s AND updated_at <= %s ORDER BY updated_at ASC",
                (tenant_id, desde_eff, cursor),
            )
            categorias = [dict(f) for f in cur.fetchall()]

            cur.execute(
                "SELECT p.Producto AS producto, v.nombre, v.precio, v.foto, v.tamano_cm "
                "FROM producto_variaciones v JOIN productos p ON p.id = v.producto_id "
                "WHERE v.tenant_id = %s AND v.updated_at > %s AND v.updated_at <= %s "
                "ORDER BY v.updated_at ASC",
                (tenant_id, desde_eff, cursor),
            )
            variaciones = [dict(f) for f in cur.fetchall()]

            cur.execute(
                "SELECT l.ID_Lote AS id_lote, p.Producto AS producto, "
                "v.nombre AS variacion, l.Costo AS costo, l.Precio_Venta AS precio_venta, "
                "l.Stock_Lote AS stock_lote, l.Fecha_Entrada AS fecha_entrada, "
                "l.Estado, l.etiqueta "
                "FROM lotes l JOIN productos p ON p.id = l.producto_id "
                "LEFT JOIN producto_variaciones v ON v.id = l.variacion_id "
                "WHERE l.tenant_id = %s AND l.updated_at > %s AND l.updated_at <= %s "
                "ORDER BY l.updated_at ASC",
                (tenant_id, desde_eff, cursor),
            )
            lotes = [dict(f) for f in cur.fetchall()]

            cur.execute(
                "SELECT titulo, subtitulo, template, tema, mostrar_precios, "
                "mostrar_stock, mostrar_categorias, agrupar_por_categoria, "
                "ocultar_agotados FROM catalogo_config "
                "WHERE tenant_id = %s AND updated_at > %s AND updated_at <= %s",
                (tenant_id, desde_eff, cursor),
            )
            config = cur.fetchone()
            config = dict(config) if config else None

            # Categorías por producto (pivot) solo para los productos que van.
            if productos:
                nombres = [p["Producto"] for p in productos]
                cur.execute(
                    """
                    SELECT p.Producto AS producto, COALESCE(
                        (SELECT array_agg(c.nombre ORDER BY c.nombre)
                         FROM producto_categorias pc
                         JOIN categorias c ON pc.categoria_id = c.id
                         WHERE pc.producto_id = p.id), ARRAY['General']
                    ) AS categorias
                    FROM productos p
                    WHERE p.tenant_id = %s AND p.Producto = ANY(%s)
                    """,
                    (tenant_id, nombres),
                )
                por_producto = {f["producto"]: f["categorias"] for f in cur.fetchall()}
                for p in productos:
                    p["categoria"] = por_producto.get(p["Producto"]) or ["General"]
    finally:
        release_conn(conn)

    return {
        "ok": True,
        "cursor": cursor.isoformat(),
        "productos": productos,
        "categorias": categorias,
        "variaciones": variaciones,
        "lotes": lotes,
        "config": config,
    }


def log_sync(tenant_id: str, limite: int = 30) -> list:
    """Últimas decisiones de la sync (auditoría consultable desde el gestor)."""
    return query_log(tenant_id, limite)


def query_log(tenant_id: str, limite: int = 30) -> list:
    from database.conexion import query
    return query(
        "SELECT tabla, clave, campo, valor_previo, valor_nuevo, resolucion, creado_en "
        "FROM sync_log WHERE tenant_id = %s ORDER BY creado_en DESC LIMIT %s",
        (tenant_id, limite),
    )
