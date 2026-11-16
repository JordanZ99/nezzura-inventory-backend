# ==============================================================================
# backend/database/mcp_cobro.py
# Registrar VENTAS desde el agente IA (rol Cajero): proponer_venta →
# confirmar_venta.
#
# El humano es el guardián: proponer arma el carrito, corre TODA la validación
# y devuelve el desglose REAL (nombres y precios de la BD) para que el dueño
# lo apruebe en el chat; confirmar lo registra con el MISMO motor del POS
# (cobrar_carrito: PEPS con bloqueo de lotes, pago, orden con n_ticket, turno
# abierto si hay caja). El agente nunca toca lotes ni totales por su cuenta.
#
# Reglas de diseño:
#   - El borrador solo guarda productos RESUELTOS por el backend (id + nombre
#     real): un producto inventado es estructuralmente imposible de registrar.
#   - El agente NUNCA calcula precios: si no envía precio_real se usa el de los
#     lotes con la MISMA expresión del catálogo público (mcp_catalogo).
#   - Venta libre: exige descripcion + precio_real + motivo (el motivo queda
#     en el Historial); aviso (no bloqueo) si la descripcion parece estar en
#     el inventario — contención de tokens normalizados, cero heurística.
#   - Fail-closed: cualquier dato que no resuelva aborta ANTES de crear el
#     borrador. Sin costos ni ganancia en la respuesta (nunca viajan — el
#     motor los captura internamente y esta capa no los expone).
#   - Alcance v1: solo productos físicos ('stock'); compuestos/servicios,
#     clientes y canje de puntos quedan fuera (igual que el resto del MCP).
# ==============================================================================

import re
from datetime import date

from database import mcp_borradores
from database.actividad import registrar_actividad
from database.conexion import query
from database.helpers import hoy_negocio
from database.mcp_catalogo import _PRECIO_VENTA, _normalizar
from database.ventas import METODOS_PAGO, actualizar_orden, cobrar_carrito

# Cota anti-abuso: un ticket razonable no tiene más renglones que esto.
MAX_ITEMS = 50

# Métodos de pago que el agente puede declarar: los del POS menos 'puntos'
# (el canje exige cliente y saldo del ledger — fuera del alcance v1).
METODOS_VENTA = ("efectivo", "tarjeta_debito", "tarjeta_credito", "mixto")

# Resolución de UN producto por id (o nombre exacto) con su precio default
# de lotes: MAX(Precio_Venta) entre lotes CON stock; si ninguno tiene stock,
# el MAX histórico de los lotes activos (idéntico al catálogo público).
_SQL_PRODUCTO = f"""
    SELECT p.id, p.Producto AS producto, p.tipo_producto, p.fraccionable,
           p.Estado AS estado, {_PRECIO_VENTA} AS precio
    FROM productos p
    LEFT JOIN lotes l ON l.producto_id = p.id AND l.tenant_id = p.tenant_id
                      AND l.Estado = 'Activo'
    WHERE p.id = %s AND p.tenant_id = %s
    GROUP BY p.id, p.Producto, p.tipo_producto, p.fraccionable, p.Estado
"""

_SQL_PRODUCTO_POR_NOMBRE = f"""
    SELECT p.id, p.Producto AS producto, p.tipo_producto, p.fraccionable,
           p.Estado AS estado, {_PRECIO_VENTA} AS precio
    FROM productos p
    LEFT JOIN lotes l ON l.producto_id = p.id AND l.tenant_id = p.tenant_id
                      AND l.Estado = 'Activo'
    WHERE p.Producto = %s AND p.tenant_id = %s
    GROUP BY p.id, p.Producto, p.tipo_producto, p.fraccionable, p.Estado
"""

# Lote del producto que el agente pidió explícitamente (id_lote): debe existir,
# pertenecer a ese producto/tenant y estar activo; el costo JAMÁS se selecciona.
_SQL_LOTE = """
    SELECT l.id, l.Stock_Lote AS stock
    FROM lotes l
    WHERE l.id = %s AND l.producto_id = %s AND l.tenant_id = %s
      AND l.Estado = 'Activo'
"""

# Nombres del inventario físico activo (para el aviso anti-flojera de la venta
# libre): si la descripcion contiene TODOS los tokens del nombre de un
# producto, probablemente ese producto sí existía. Solo se ejecuta si hay
# renglones de venta libre; es la única consulta extra y el precio sale gratis
# de la misma expresión del catálogo (para mostrarlo en el aviso).
_SQL_NOMBRES = f"""
    SELECT p.id, p.Producto AS producto, {_PRECIO_VENTA} AS precio
    FROM productos p
    LEFT JOIN lotes l ON l.producto_id = p.id AND l.tenant_id = p.tenant_id
                      AND l.Estado = 'Activo'
    WHERE p.tenant_id = %s AND p.Estado = 'Activo' AND p.tipo_producto = 'stock'
    GROUP BY p.id, p.Producto
"""

# El producto genérico 'Venta libre' del tenant (migración 036): el motor de
# cobro lo resuelve por NOMBRE dentro de su transacción, así que aquí se
# traduce el item de venta libre al nombre real de ese producto en el momento
# del cobro (si el dueño lo borró o lo desactivó → error, no inventario sucio).
_SQL_GENERICO = """
    SELECT p.Producto AS producto
    FROM productos p
    WHERE p.tenant_id = %s AND p.es_generico = true AND p.Estado = 'Activo'
    LIMIT 1
"""


# ── Validación de pago (fail-early: espejo de _procesar_pago de ventas.py) ────

def _validar_pago(pago, total_estimado: float) -> str | None:
    """
    Devuelve el mensaje de error si el pago no pasaría el motor de cobro; None
    si es válido (o si no viene pago: venta sin método, como el papel).
    El motor recalcula el total REAL al cobrar; aquí se valida contra el
    desglose resuelto para que el error salga ANTES de crear el borrador.
    """
    if pago is None:
        return None
    if not isinstance(pago, dict):
        return "El pago debe ser un objeto {metodo, monto_recibido?, propina?, pagos?}."

    metodo = str(pago.get("metodo") or "").strip()
    if metodo not in METODOS_VENTA:
        return (
            f"Método de pago inválido: '{metodo}'. Usa 'efectivo', "
            "'tarjeta_debito', 'tarjeta_credito' o 'mixto' (deja el pago "
            "completo vacío para registrar la venta sin método)."
        )

    propina = pago.get("propina")
    if propina is not None:
        try:
            propina = float(propina)
        except (TypeError, ValueError):
            return "La propina debe ser un número."
        if propina < 0:
            return "La propina no puede ser negativa."
    propina = round(propina or 0, 2)
    total_a_pagar = round(total_estimado + propina, 2)

    if metodo == "efectivo":
        if pago.get("monto_recibido") is None:
            return (
                "El pago en efectivo requiere monto_recibido (cuánto le dio el "
                "cliente): sin eso no se puede calcular el cambio. Si no lo "
                "sabes, no envíes el pago y el ticket queda sin método."
            )
        try:
            recibido = round(float(pago["monto_recibido"]), 2)
        except (TypeError, ValueError):
            return "El monto_recibido debe ser un número."
        if recibido < total_a_pagar:
            return (
                f"El monto recibido (${recibido:.2f}) es menor al total a "
                f"pagar (${total_a_pagar:.2f})."
            )

    elif metodo == "mixto":
        pagos = pago.get("pagos") or []
        if not isinstance(pagos, list) or len(pagos) < 2:
            return "El pago mixto requiere una lista 'pagos' con al menos dos pagos."
        suma = 0.0
        for p in pagos:
            if not isinstance(p, dict):
                return "Cada pago de la lista 'pagos' debe ser un objeto {metodo, monto}."
            m = str(p.get("metodo") or "").strip()
            if m not in METODOS_PAGO:
                return (
                    f"Método de pago inválido dentro de 'pagos': '{m}'. Usa "
                    "'efectivo', 'tarjeta_debito' o 'tarjeta_credito'."
                )
            try:
                monto = round(float(p.get("monto")), 2)
            except (TypeError, ValueError):
                return "El monto de cada pago debe ser un número mayor a 0."
            if monto <= 0:
                return "Los montos de cada pago deben ser mayores a 0."
            suma += monto
        if abs(suma - total_a_pagar) > 0.01:
            return (
                f"La suma de los pagos (${suma:.2f}) no coincide con el total "
                f"a pagar (${total_a_pagar:.2f}): debe cuadrar (±1 centavo)."
            )

    return None


def _validar_fecha(fecha, tenant_id: str) -> str | None:
    """Formato estricto YYYY-MM-DD y nunca futura (el ticket no se paga mañana)."""
    if fecha is None or not str(fecha).strip():
        return None
    fecha = str(fecha).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
        return (
            f"Fecha del ticket inválida: '{fecha}'. Usa el formato YYYY-MM-DD "
            "(solo el día: la hora la pone el gestor)."
        )
    try:
        dia = date.fromisoformat(fecha)
    except ValueError:
        return f"Fecha del ticket inválida: '{fecha}' no es un día real del calendario."
    if dia > hoy_negocio(tenant_id):
        return "La fecha del ticket no puede ser futura."
    return None


def _avisos_venta_libre(descripciones: list[str], tenant_id: str) -> list[dict]:
    """
    Anti-flojera (aviso, no bloqueo): por cada descripcion de venta libre,
    ¿algún producto del inventario contiene TODOS sus tokens normalizados?
    Devuelve {descripcion, producto, precio} por coincidencia — el dueño decide
    en el desglose si era venta libre o producto del inventario.
    """
    if not descripciones:
        return []
    filas = query(_SQL_NOMBRES, (tenant_id,))
    if not filas:
        return []
    avisos = []
    for desc in descripciones:
        tokens = _normalizar(desc).split()
        if not tokens:
            continue
        for fila in filas:
            nombre_norm = _normalizar(fila["producto"])
            if all(t in nombre_norm for t in tokens):
                precio = float(fila.get("precio") or 0)
                avisos.append({
                    "descripcion": desc,
                    "producto": fila["producto"],
                    "precio": precio,
                })
                break  # una coincidencia por descripcion basta para el aviso
    return avisos


# ── PROPOSER: valida TODO y guarda el borrador (no toca el inventario) ────────

def proponer_venta(tenant_id: str, clave_id: int, items, pago=None, fecha=None, nota="") -> dict:
    """
    Valida el carrito completo (fail-early) y guarda el borrador
    tipo 'registrar_venta'. Devuelve el desglose REAL para que el dueño lo
    apruebe: nada se ha registrado en este paso.
    """
    if not isinstance(items, list) or not items:
        return {"error": "Envía la lista 'items' con al menos un renglón de la venta."}
    if len(items) > MAX_ITEMS:
        return {"error": f"Máximo {MAX_ITEMS} renglones por venta. Divide la venta en varias."}

    # ── 1-4: normalizar y resolver cada item contra la BD ──
    resueltos: list[dict] = []
    desglose: list[dict] = []
    libres_descripcion: list[str] = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return {"error": f"El renglón {i} debe ser un objeto (dict)."}

        cantidad = item.get("cantidad", 1)
        try:
            cantidad = float(cantidad)
        except (TypeError, ValueError):
            return {"error": f"El renglón {i}: la cantidad debe ser un número."}
        if cantidad <= 0:
            return {"error": f"El renglón {i}: la cantidad debe ser mayor a 0."}

        if item.get("venta_libre"):
            # ── Venta libre: lo vendido NO está en el inventario ──
            if item.get("producto") or item.get("producto_id"):
                return {
                    "error": (
                        f"El renglón {i}: una venta libre no lleva producto_id "
                        "ni producto. Si lo que se vendió SÍ está en el "
                        "inventario, quita venta_libre y envía el id."
                    )
                }
            descripcion = str(item.get("descripcion") or "").strip()
            if not descripcion or len(descripcion) > 120:
                return {
                    "error": (
                        f"El renglón {i}: la venta libre exige 'descripcion' "
                        "(qué se vendió, 1-120 caracteres)."
                    )
                }
            motivo = str(item.get("motivo") or "").strip()
            if not motivo or len(motivo) > 200:
                return {
                    "error": (
                        f"El renglón {i}: la venta libre exige 'motivo' (por qué "
                        "no está en el inventario, 1-200 caracteres). Queda en "
                        "el Historial del gestor."
                    )
                }
            if item.get("precio_real") is None:
                return {
                    "error": (
                        f"El renglón {i}: la venta libre exige 'precio_real' "
                        "(lo que se cobró): la venta libre no tiene precio de lista."
                    )
                }
            try:
                precio_real = round(float(item["precio_real"]), 2)
            except (TypeError, ValueError):
                return {"error": f"El renglón {i}: el precio_real debe ser un número."}
            if precio_real <= 0:
                return {"error": f"El renglón {i}: el precio_real de la venta libre debe ser mayor a 0."}
            categoria = str(item.get("categoria") or "").strip() or None
            if categoria and len(categoria) > 60:
                return {"error": f"El renglón {i}: la categoría no puede pasar de 60 caracteres."}

            resueltos.append({
                "tipo_item": "venta_libre",
                "descripcion": descripcion,
                "cantidad": cantidad,
                "precio_real": precio_real,
                "categoria": categoria,
                "motivo": motivo,
            })
            desglose.append({
                "venta_libre": True,
                "descripcion": descripcion,
                "cantidad": cantidad,
                "precio_unitario": precio_real,
                "subtotal": round(precio_real * cantidad, 2),
                "motivo": motivo,
            })
            libres_descripcion.append(descripcion)
            continue

        # ── Producto del inventario: id o nombre exacto, no ambos ──
        producto_id = item.get("producto_id")
        nombre = str(item.get("producto") or "").strip()
        if producto_id is not None and nombre:
            return {"error": f"El renglón {i}: envía producto_id O producto (nombre exacto), no ambos."}
        if producto_id is not None:
            try:
                producto_id = int(producto_id)
            except (TypeError, ValueError):
                return {"error": f"El renglón {i}: el producto_id debe ser un número."}
            filas = query(_SQL_PRODUCTO, (producto_id, tenant_id))
            referencia = f"id {producto_id}"
        elif nombre:
            filas = query(_SQL_PRODUCTO_POR_NOMBRE, (nombre, tenant_id))
            referencia = f"'{nombre}'"
        else:
            return {
                "error": (
                    f"El renglón {i}: falta el producto — envía producto_id (el "
                    "de buscar_productos) o venta_libre=true con descripcion."
                )
            }

        if not filas:
            return {
                "error": (
                    f"No existe el producto {referencia} en el inventario (o no "
                    "es un producto físico activo). Usa buscar_productos para "
                    "encontrar el producto_id exacto y vuelve a proponer la venta."
                )
            }
        prod = filas[0]
        nombre_real = prod["producto"]

        if (prod.get("tipo_producto") or "stock") != "stock":
            return {
                "error": (
                    f"'{nombre_real}' es {prod.get('tipo_producto')}: el conector "
                    "solo vende productos físicos por ahora. Regístralo en el "
                    "gestor o como venta libre con su motivo."
                )
            }
        if (prod.get("estado") or "Activo") != "Activo":
            return {"error": f"'{nombre_real}' está inactivo en el inventario: no se puede vender."}

        # Precio: el del item si lo envió; el default de lotes si no. El agente
        # nunca calcula precios — y si la BD no tiene precio, se pide explícito.
        if item.get("precio_real") is not None:
            try:
                precio_real = round(float(item["precio_real"]), 2)
            except (TypeError, ValueError):
                return {"error": f"El renglón {i}: el precio_real debe ser un número."}
            if precio_real < 0:
                return {"error": f"El renglón {i}: el precio_real no puede ser negativo."}
            precio_default = None
        else:
            precio_default = round(float(prod.get("precio") or 0), 2)
            if precio_default <= 0:
                return {
                    "error": (
                        f"'{nombre_real}' no tiene precio en sus lotes: envía el "
                        "precio_real que se cobró para este renglón."
                    )
                }
            precio_real = precio_default

        if not bool(prod.get("fraccionable")) and cantidad != int(cantidad):
            return {
                "error": (
                    f"'{nombre_real}' solo se vende por unidades enteras "
                    "(no está marcado como fraccionable): corrige la cantidad."
                )
            }

        # Lote pedido explícitamente: validar existencia/propiedad/estado.
        id_lote = item.get("id_lote")
        if id_lote is not None:
            try:
                id_lote = int(id_lote)
            except (TypeError, ValueError):
                return {"error": f"El renglón {i}: el id_lote debe ser un número."}
            lote = query(_SQL_LOTE, (id_lote, prod["id"], tenant_id))
            if not lote:
                return {
                    "error": (
                        f"El lote {id_lote} no existe para '{nombre_real}' (o no "
                        "está activo). Deja id_lote vacío y el sistema elige el "
                        "lote más viejo automáticamente (PEPS)."
                    )
                }
            stock_lote = float(lote[0].get("stock") or 0)

        variacion = str(item.get("variacion") or "").strip() or None

        resueltos.append({
            "tipo_item": "producto",
            "producto_id": prod["id"],
            "producto": nombre_real,
            "cantidad": cantidad,
            "precio_real": precio_real,
            "variacion": variacion,
            "id_lote": id_lote,
        })
        renglon = {
            "producto": nombre_real,
            "cantidad": cantidad,
            "precio_unitario": precio_real,
            "subtotal": round(precio_real * cantidad, 2),
        }
        if precio_default is not None:
            renglon["precio_de_lista"] = True  # el agente no lo inventó: salió de la BD
        if variacion:
            renglon["variacion"] = variacion
        if id_lote is not None:
            renglon["lote_elegido"] = {"id": id_lote, "stock": stock_lote}
        desglose.append(renglon)

    # ── 5: anti-flojera de la venta libre (aviso, no bloqueo) ──
    avisos = [
        (
            f"⚠ '{a['descripcion']}' parece estar en tu inventario como "
            f"'{a['producto']}' (${a['precio']:.2f}): confirma con el dueño que "
            "realmente es venta libre y no un producto del inventario."
        )
        for a in _avisos_venta_libre(libres_descripcion, tenant_id)
    ]

    total_estimado = round(sum(d["subtotal"] for d in desglose), 2)

    # ── 6: pago (validado contra el total estimado, antes del borrador) ──
    error_pago = _validar_pago(pago, total_estimado)
    if error_pago:
        return {"error": error_pago}

    # ── 7: fecha del ticket (día contable; nunca futura) ──
    error_fecha = _validar_fecha(fecha, tenant_id)
    if error_fecha:
        return {"error": error_fecha}
    fecha = str(fecha).strip() if fecha else None

    nota = str(nota or "").strip()
    if len(nota) > 500:
        return {"error": "La nota no puede pasar de 500 caracteres."}

    # ── 8: borrador (TTL 15 min, token de un solo uso, scoped clave+tenant) ──
    borrador = mcp_borradores.crear_borrador(tenant_id, clave_id, {
        "tipo": "registrar_venta",
        "items": resueltos,
        "pago": pago if isinstance(pago, dict) else None,
        "fecha": fecha,
        "nota": nota,
    })

    respuesta = {
        "borrador_id": borrador["token"],
        "expira_minutos": mcp_borradores.TTL_MINUTOS,
        "desglose": desglose,
        "total_estimado": total_estimado,
        "metodo_pago": pago.get("metodo") if isinstance(pago, dict) else None,
        "avisos": avisos,
        "siguiente_paso": (
            "Nada se ha registrado todavía. Muestra ESTE desglose al dueño y "
            "espera su aprobación; solo si la da, llama a confirmar_venta con "
            "este borrador_id. Si pide corregir algo, vuelve a proponer la "
            "venta corregida (este borrador expira solo)."
        ),
    }
    if fecha:
        respuesta["fecha_ticket"] = fecha
    return respuesta


# ── CONFIRMAR: registra DESDE LO GUARDADO con el motor del POS ────────────────

def confirmar_venta(tenant_id: str, datos: dict) -> dict:
    """
    Cobra el carrito del borrador con cobrar_carrito (PEPS, pago, orden,
    turno abierto), ajusta la fecha del ticket si era otro día y lo deja en el
    Historial con origen 'agente_ia'. Sin costos ni ganancia en la respuesta.
    """
    if datos.get("tipo") != "registrar_venta":
        tipo = datos.get("tipo") or "crear_producto"
        return {
            "error": (
                f"Este borrador no es una venta (es '{tipo}'): confírmalo con "
                "la herramienta correspondiente (confirmar_producto o "
                "confirmar_edicion), no con confirmar_venta."
            )
        }

    items_borrador = datos.get("items") or []
    if not items_borrador:
        return {"error": "El borrador no tiene renglones: vuelve a proponer la venta."}

    # Venta libre → resolver el producto genérico del tenant EN EL MOMENTO del
    # cobro (si el dueño lo borró/desactivó, el error sale aquí, limpio).
    necesita_generico = any(
        it.get("tipo_item") == "venta_libre" for it in items_borrador
    )
    nombre_generico = None
    if necesita_generico:
        filas = query(_SQL_GENERICO, (tenant_id,))
        if not filas:
            return {
                "error": (
                    "La venta libre no está disponible: este negocio no tiene "
                    "el producto genérico 'Venta libre' activo. Créalo en el "
                    "gestor o elimina los renglones de venta libre."
                )
            }
        nombre_generico = filas[0]["producto"]

    items_cobro: list[dict] = []
    for it in items_borrador:
        if it.get("tipo_item") == "venta_libre":
            items_cobro.append({
                "producto": nombre_generico,
                "cantidad": it["cantidad"],
                "precio_real": it["precio_real"],
                "descripcion": it["descripcion"],
                "categoria": it.get("categoria"),
                "costo": 0,  # la venta libre no descuenta inventario ni costo
            })
        else:
            item = {
                "producto": it["producto"],
                "cantidad": it["cantidad"],
                "precio_real": it["precio_real"],
            }
            if it.get("id_lote") is not None:
                item["id_lote"] = it["id_lote"]
            if it.get("variacion"):
                item["variacion"] = it["variacion"]
            items_cobro.append(item)

    resultado = cobrar_carrito(items_cobro, tenant_id, pago=datos.get("pago"))
    if not resultado.get("ok"):
        return {"error": resultado.get("mensaje") or "No se pudo registrar la venta."}

    orden_id = resultado.get("orden_id")
    n_ticket = resultado.get("n_ticket")
    total = resultado.get("total_cobrado")
    fecha = datos.get("fecha")

    # ── Fecha del ticket ≠ hoy: mover el DÍA contable (conserva la hora). ──
    # Si falla, aviso y no error: la venta YA está registrada.
    aviso_fecha = None
    if fecha and orden_id and str(fecha) != str(hoy_negocio(tenant_id)):
        res_fecha = actualizar_orden(orden_id, fecha, tenant_id)
        if not res_fecha.get("ok"):
            aviso_fecha = (
                "La venta quedó registrada con la fecha de hoy: no se pudo "
                f"mover al día del ticket ({res_fecha.get('mensaje') or 'error'})."
            )

    # ── Historial: la venta aparece como hecha por el agente IA (062/068) ──
    libre_motivos = [
        f"{it['descripcion']} ({it['motivo']})"
        for it in items_borrador if it.get("tipo_item") == "venta_libre"
    ]
    detalle = {
        "origen": "agente_ia",
        "ticket": n_ticket,
        "orden_id": str(orden_id) if orden_id else None,
    }
    if fecha:
        detalle["fecha_ticket"] = fecha
    if libre_motivos:
        detalle["ventas_libres"] = libre_motivos
    if datos.get("nota"):
        detalle["nota"] = datos["nota"]
    try:
        registrar_actividad(
            tenant_id, "venta", "venta", "crear",
            resumen=(
                f"El agente IA registró la venta (ticket #{n_ticket}): "
                f"{len(items_cobro)} renglones, ${total}"
            ),
            entidad_id=orden_id,
            detalle=detalle,
        )
    except Exception:
        pass

    respuesta = {
        "ok": True,
        "n_ticket": n_ticket,
        "total_cobrado": total,
        "metodo_pago": resultado.get("metodo_pago"),
        "propina": resultado.get("propina"),
        "cambio": resultado.get("cambio"),
        "renglones": len(items_cobro),
        "siguiente_paso": (
            "Venta registrada. Dile al dueño el número de ticket y el total; "
            "queda en su Historial como hecha por el agente IA."
        ),
    }
    if fecha:
        respuesta["fecha_registrada"] = fecha
    if aviso_fecha:
        respuesta["aviso"] = aviso_fecha
    return respuesta
