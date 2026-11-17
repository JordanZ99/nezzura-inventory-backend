# ==============================================================================
# backend/database/mcp_compras.py
# ENTRADAS de inventario (restock) desde el agente IA: proponer_entrada →
# confirmar_entrada. El dueño aprueba el desglose (cantidad, costo, precio y
# stock resultante) ANTES de que la entrada toque su inventario valorado.
#
# Reglas de diseño:
#   - Reutiliza el MISMO motor del gestor: agregar_lote (lotes.py), que fusiona
#     con el lote existente si coincide costo+precio (+etiqueta+variación) o
#     crea un lote nuevo, y registra el ledger de movimientos de inventario.
#   - El producto DEBE existir (id o nombre exacto resuelto por el backend):
#     el agente no crea productos por esta vía (para eso está proponer_producto)
#     y servicios/compuestos quedan fuera (el stock del compuesto son sus
#     materiales; el servicio no tiene inventario).
#   - El COSTO es fail-closed: si el agente no lo sabe y la clave no tiene
#     permiso 'ver costos', se le pide que pregunte al dueño — un costo
#     equivocado corrompe el inventario valorado (PEPS). Solo una clave con
#     'ver costos' puede proponer la entrada con el costo del último lote.
#   - El upsert de agregar_lote pisa campos del producto: confirmar_entrada le
#     pasa los valores ACTUALES (descripcion, imagen) para que sea no-op en
#     productos y solo toque lotes.
# ==============================================================================

from database import mcp_borradores
from database.actividad import registrar_actividad
from database.conexion import query
from database.helpers import hoy_negocio
from database.lotes import agregar_lote
# La resolución del producto y la validación de fecha son EXACTAMENTE las de
# las ventas (misma expresión de precio de lotes, misma regla de fecha): una
# sola fuente de verdad para ambos flujos de escritura del conector.
from database.mcp_cobro import _SQL_PRODUCTO, _SQL_PRODUCTO_POR_NOMBRE, _validar_fecha

# Precio/costo del lote ACTIVO más reciente del producto: es el supuesto
# natural del restock ("me llegó igual que la última vez") y es lo que decide
# si la entrada suma al lote existente o crea uno nuevo.
_SQL_ULTIMO_LOTE = """
    SELECT l.Costo AS costo, l.Precio_Venta AS precio_venta
    FROM lotes l
    WHERE l.producto_id = %s AND l.tenant_id = %s AND l.Estado = 'Activo'
    ORDER BY l.fecha_entrada_ts DESC NULLS LAST
    LIMIT 1
"""

# ¿El producto maneja stock por variación? (Fase 6: con variaciones, TODO
# lote pertenece a UNA variación concreta — espejo de agregar_lote).
_SQL_TIENE_VARIACIONES = """
    SELECT 1 FROM producto_variaciones v
    WHERE v.producto_id = %s AND v.tenant_id = %s LIMIT 1
"""

_SQL_VARIACION = """
    SELECT v.id FROM producto_variaciones v
    WHERE v.producto_id = %s AND v.nombre = %s AND v.tenant_id = %s
"""

# Vista previa de la fusión (misma regla de agregar_lote: mismo costo+precio,
# etiqueta compatible y misma variación → suma al lote existente).
_SQL_FUSION = """
    SELECT ID_Lote AS id_lote, Stock_Lote AS stock
    FROM lotes
    WHERE producto_id = %s AND Costo = %s AND Precio_Venta = %s
      AND Estado = 'Activo' AND tenant_id = %s
      AND (%s = '' OR COALESCE(etiqueta, '') = %s)
      AND variacion_id IS NOT DISTINCT FROM %s
    LIMIT 1
"""

# Stock del producto tras la entrada (lotes activos, incluye negativos).
_SQL_STOCK = """
    SELECT COALESCE(SUM(l.Stock_Lote), 0) AS stock
    FROM lotes l
    WHERE l.producto_id = %s AND l.tenant_id = %s AND l.Estado = 'Activo'
"""

# Valores ACTUALES del producto: los que se reenvían a agregar_lote para que
# su upsert no pise nada (el router del gestor pasa descripcion='' y por eso
# cada restock manual blanqueaba la descripción — aquí va la real).
_SQL_PRODUCTO_COMPLETO = """
    SELECT p.Producto AS producto,
           COALESCE(p.Descripcion, '') AS descripcion,
           COALESCE(p.Imagen, 'No hay foto') AS imagen,
           p.tipo_producto, p.Estado AS estado
    FROM productos p
    WHERE p.id = %s AND p.tenant_id = %s
"""


def proponer_entrada(
    tenant_id: str, clave_id: int,
    producto_id: int | None = None, producto: str | None = None,
    cantidad: float = 0, costo: float | None = None,
    precio_venta: float | None = None,
    variacion: str | None = None, etiqueta: str | None = None,
    con_costos: bool = False, nota: str = "",
) -> dict:
    """
    Valida la entrada de inventario (fail-early) y guarda el borrador
    tipo 'entrada_inventario'. Nada toca el inventario en este paso.
    """
    # ── 1: producto resuelto por el backend (id o nombre exacto) ──
    nombre = str(producto or "").strip()
    if producto_id is not None and nombre:
        return {"error": "Envía producto_id O producto (nombre exacto), no ambos."}
    if producto_id is not None:
        try:
            producto_id = int(producto_id)
        except (TypeError, ValueError):
            return {"error": "El producto_id debe ser un número."}
        filas = query(_SQL_PRODUCTO, (producto_id, tenant_id))
        referencia = f"id {producto_id}"
    elif nombre:
        filas = query(_SQL_PRODUCTO_POR_NOMBRE, (nombre, tenant_id))
        referencia = f"'{nombre}'"
    else:
        return {"error": "Indica el producto — envía producto_id o el nombre exacto."}
    if not filas:
        return {
            "error": (
                f"No existe el producto {referencia} en el inventario. Usa "
                "buscar_productos para encontrar el producto_id exacto; si es "
                "un producto NUEVO, créalo con proponer_producto (esta "
                "herramienta solo agrega stock a productos existentes)."
            )
        }
    prod = filas[0]
    nombre_real = prod["producto"]
    if (prod.get("tipo_producto") or "stock") != "stock":
        return {
            "error": (
                f"'{nombre_real}' es {prod.get('tipo_producto')}: no recibe "
                "entradas de inventario (el servicio no tiene stock y el "
                "compuesto se reponen por sus materiales desde el gestor)."
            )
        }
    if (prod.get("estado") or "Activo") != "Activo":
        return {"error": f"'{nombre_real}' está inactivo: actívalo en el gestor antes de restockear."}

    # ── 2: cantidad (float permitido: hay productos fraccionables) ──
    try:
        cantidad = float(cantidad)
    except (TypeError, ValueError):
        return {"error": "La cantidad debe ser un número mayor a 0."}
    if cantidad <= 0:
        return {"error": "La cantidad debe ser mayor a 0."}

    # ── 3: costo (fail-closed del inventario valorado) ──
    costo_default = None
    if costo is None:
        ultimo = query(_SQL_ULTIMO_LOTE, (prod["id"], tenant_id))
        if ultimo:
            costo_default = round(float(ultimo[0].get("costo") or 0), 2)
        if costo_default is None or costo_default <= 0 or not con_costos:
            return {
                "error": (
                    "La entrada necesita el COSTO unitario de compra (>0): es el "
                    "que alimenta el inventario valorado (PEPS). Pregúntale al "
                    "dueño cuánto le costó y envíalo en 'costo'."
                    + (
                        " (Con el permiso 'ver costos' de la clave podrías usar "
                        "el costo del último lote automáticamente.)"
                        if con_costos is False and costo_default and costo_default > 0
                        else ""
                    )
                )
            }
        costo = costo_default  # default solo alcanzable con ver_costos
    else:
        try:
            costo = round(float(costo), 2)
        except (TypeError, ValueError):
            return {"error": "El costo debe ser un número mayor a 0."}
        if costo <= 0:
            return {"error": "El costo debe ser mayor a 0."}
        # Para el aviso: si el costo del dueño difiere del último lote, avisar.
        ultimo = query(_SQL_ULTIMO_LOTE, (prod["id"], tenant_id))
        if ultimo:
            costo_default = round(float(ultimo[0].get("costo") or 0), 2)

    # ── 4: precio de venta (default: el del último lote; es público) ──
    precio_default = None
    if precio_venta is None:
        ultimo = query(_SQL_ULTIMO_LOTE, (prod["id"], tenant_id))
        if ultimo:
            precio_default = round(float(ultimo[0].get("precio_venta") or 0), 2)
        if precio_default is None or precio_default <= 0:
            return {
                "error": (
                    f"'{nombre_real}' no tiene lotes previos con precio: envía "
                    "el precio_venta al que se venderá este stock."
                )
            }
        precio_venta = precio_default
    else:
        try:
            precio_venta = round(float(precio_venta), 2)
        except (TypeError, ValueError):
            return {"error": "El precio_venta debe ser un número mayor a 0."}
        if precio_venta <= 0:
            return {"error": "El precio_venta debe ser mayor a 0."}
        # Para el aviso: si el precio propuesto difiere del último lote, avisar.
        ultimo = query(_SQL_ULTIMO_LOTE, (prod["id"], tenant_id))
        if ultimo:
            precio_default = round(float(ultimo[0].get("precio_venta") or 0), 2)

    # ── 5: variación (espejo de agregar_lote, pero fail-early) ──
    variacion = str(variacion or "").strip()
    variacion_id = None
    tiene_variaciones = bool(query(_SQL_TIENE_VARIACIONES, (prod["id"], tenant_id)))
    if tiene_variaciones:
        if not variacion:
            return {
                "error": (
                    f"'{nombre_real}' tiene variaciones: indica a cuál llegó el "
                    f"stock (ej. 'Grande')."
                )
            }
        var = query(_SQL_VARIACION, (prod["id"], variacion, tenant_id))
        if not var:
            return {"error": f"La variación '{variacion}' no existe para '{nombre_real}'."}
        variacion_id = var[0]["id"]
    elif variacion:
        return {
            "error": (
                f"'{nombre_real}' no tiene variaciones: quita la variación "
                "(el stock general es para el producto completo)."
            )
        }

    etiqueta = str(etiqueta or "").strip()
    if len(etiqueta) > 60:
        return {"error": "La etiqueta no puede pasar de 60 caracteres."}
    nota = str(nota or "").strip()
    if len(nota) > 500:
        return {"error": "La nota no puede pasar de 500 caracteres."}

    # ── 6: vista previa de la fusión (misma regla del motor) ──
    fusion = query(
        _SQL_FUSION,
        (prod["id"], costo, precio_venta, tenant_id, etiqueta, etiqueta, variacion_id),
    )
    stock_actual = float(query(_SQL_STOCK, (prod["id"], tenant_id))[0].get("stock") or 0)
    accion_prevista = "suma_al_lote_existente" if fusion else "lote_nuevo"

    borrador = mcp_borradores.crear_borrador(tenant_id, clave_id, {
        "tipo": "entrada_inventario",
        "producto_id": prod["id"],
        "producto": nombre_real,
        "cantidad": cantidad,
        "costo": costo,
        "precio_venta": precio_venta,
        "variacion": variacion or None,
        "etiqueta": etiqueta or None,
        "nota": nota,
    })

    desglose = {
        "producto": nombre_real,
        "cantidad": cantidad,
        "costo_unitario": costo,
        "costo_total": round(costo * cantidad, 2),
        "precio_venta": precio_venta,
        "stock_actual": stock_actual,
        "stock_despues": round(stock_actual + cantidad, 2),
        "accion_prevista": (
            "suma al lote existente (mismo costo y precio)"
            if accion_prevista == "suma_al_lote_existente"
            else "crea un lote nuevo"
        ),
    }
    if variacion:
        desglose["variacion"] = variacion
    if etiqueta:
        desglose["etiqueta"] = etiqueta

    avisos: list[str] = []
    if precio_default is not None and precio_venta != precio_default:
        avisos.append(
            f"⚠ El precio de venta de '{nombre_real}' era ${precio_default:.2f} "
            f"y esta entrada llega a ${precio_venta:.2f}: se creará un lote "
            "nuevo con ese precio (el stock anterior conserva el suyo)."
        )
    if costo_default is not None and costo != costo_default:
        avisos.append(
            f"⚠ El costo del último lote de '{nombre_real}' fue "
            f"${costo_default:.2f} y esta entrada declara ${costo:.2f}: "
            "confírmalo con el dueño si es correcto."
        )

    return {
        "borrador_id": borrador["token"],
        "expira_minutos": mcp_borradores.TTL_MINUTOS,
        "desglose": desglose,
        "avisos": avisos,
        "siguiente_paso": (
            "Nada se ha agregado todavía. Muestra ESTE desglose al dueño y haz "
            "UNA sola pregunta: '¿aplico la entrada?'. Solo si aprueba, llama "
            "a confirmar_entrada con este borrador_id."
        ),
    }


def confirmar_entrada(tenant_id: str, datos: dict) -> dict:
    """
    Aplica la entrada con el MISMO motor del gestor (agregar_lote: fusión de
    lote o lote nuevo, ledger de movimientos). El upsert de productos recibe
    los valores ACTUALES del producto para no pisar nada.
    """
    if datos.get("tipo") != "entrada_inventario":
        tipo = datos.get("tipo") or "crear_producto"
        return {
            "error": (
                f"Este borrador no es una entrada de inventario (es '{tipo}'): "
                "confírmalo con la herramienta correspondiente, no con "
                "confirmar_entrada."
            )
        }

    producto_id = datos.get("producto_id")
    fila = query(_SQL_PRODUCTO_COMPLETO, (producto_id, tenant_id))
    if not fila:
        return {"error": "El producto del borrador ya no existe: vuelve a proponer la entrada."}
    prod = fila[0]
    nombre_real = prod["producto"]
    if (prod.get("tipo_producto") or "stock") != "stock":
        return {"error": f"'{nombre_real}' ya no es un producto de stock: vuelve a proponer."}
    if (prod.get("estado") or "Activo") != "Activo":
        return {"error": f"'{nombre_real}' está inactivo: actívalo en el gestor antes de restockear."}

    resultado = agregar_lote(
        producto=nombre_real,
        # Valores ACTUALES: el upsert queda no-op en productos y solo toca lotes.
        descripcion=str(prod.get("descripcion") or "").strip(),
        costo=float(datos["costo"]),
        precio_venta=float(datos["precio_venta"]),
        stock=float(datos["cantidad"]),
        imagen=str(prod.get("imagen") or "No hay foto"),
        categoria=["General"],  # solo aplicaría a productos NUEVOS (aquí imposible)
        tenant_id=tenant_id,
        variacion=str(datos.get("variacion") or ""),
        etiqueta=str(datos.get("etiqueta") or ""),
    )
    if resultado.get("ok") is False:
        return {"error": resultado.get("mensaje") or "No se pudo aplicar la entrada."}

    accion = resultado.get("accion")
    stock_despues = float(query(_SQL_STOCK, (producto_id, tenant_id))[0].get("stock") or 0)
    costo = float(datos["costo"])
    cantidad = float(datos["cantidad"])

    detalle = {
        "origen": "agente_ia",
        "accion_lote": accion,
        "cantidad": cantidad,
        "precio_venta": float(datos["precio_venta"]),
    }
    if resultado.get("id_lote"):
        detalle["id_lote"] = resultado["id_lote"]
    if datos.get("nota"):
        detalle["nota"] = datos["nota"]
    resumen = (
        f"El agente IA restockeó '{nombre_real}': +{cantidad:g} "
        f"({'sumó al lote existente' if accion == 'stock_sumado' else 'nuevo lote'})"
    )
    try:
        registrar_actividad(
            tenant_id, "producto", "lote", "crear",
            resumen=resumen,
            entidad_id=producto_id,
            detalle=detalle,
        )
    except Exception:
        pass

    respuesta = {
        "ok": True,
        "producto": nombre_real,
        "accion": accion,
        "cantidad_agregada": cantidad,
        "stock_despues": stock_despues,
        "siguiente_paso": (
            "Entrada aplicada. Dile al dueño cuánto subió el stock de "
            f"'{nombre_real}' (ahora tiene {stock_despues:g})."
        ),
    }
    return respuesta
