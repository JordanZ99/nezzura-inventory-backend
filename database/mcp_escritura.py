# ==============================================================================
# backend/database/mcp_escritura.py
# Escritura básica para claves de nivel 3: crear productos desde borradores.
#
# Reglas:
#   - El único punto de entrada es crear_producto_desde_borrador(datos): los
#     datos vienen de mcp_borradores (guardados por proponer_producto), nunca
#     re-enviados por el agente en la confirmación.
#   - Solo productos físicos ('stock'): servicios y compuestos quedan fuera
#     del alcance del agente (los compuestos manejan recetas; servicio es otro
#     flujo de cobro).
#   - Cada creación queda en registro_actividad con detalle.origen =
#     'agente_ia': el Historial del gestor muestra qué hizo el agente.
#   - validar_datos_producto NO acepta campos que el dueño controla a nivel
#     negocio (visible_en_catalogo, etiqueta, tipo): el agente crea el
#     producto simple; lo fino se ajusta desde el gestor.
# ==============================================================================

from database import mcp_borradores
from database.conexion import query
from database.productos import actualizar_producto, crear_producto_completo


def _num(valor, campo: str, minimo: float = 0.0) -> float:
    try:
        n = float(valor)
    except (TypeError, ValueError):
        raise ValueError(f"'{campo}' debe ser un número.")
    if n < minimo:
        raise ValueError(f"'{campo}' no puede ser negativo.")
    return n


def _texto(valor) -> str:
    return (str(valor) if valor is not None else "").strip()


def _imagen_segura(valor) -> str:
    """Solo URLs http(s); cualquier otra cosa = 'No hay foto'."""
    url = _texto(valor)
    if url.startswith(("http://", "https://")):
        return url
    return "No hay foto"


def validar_datos_producto(datos: dict) -> dict:
    """
    Normaliza y valida el borrador. Lanza ValueError con mensaje claro
    (orientado al agente, que debe poder corregirse solo).
    """
    if not isinstance(datos, dict):
        raise ValueError("Datos de producto inválidos.")

    nombre = _texto(datos.get("nombre"))
    if not nombre:
        raise ValueError("Falta el nombre del producto: es el único dato obligatorio.")
    if len(nombre) > 200:
        raise ValueError("El nombre es demasiado largo (máximo 200 caracteres).")

    precio_venta = _num(datos.get("precio_venta"), "precio_venta")
    costo = _num(datos.get("costo") or 0, "costo")
    stock = _num(datos.get("stock") or 0, "stock")

    descripcion = _texto(datos.get("descripcion"))
    if len(descripcion) > 2000:
        raise ValueError("La descripción es demasiado larga (máximo 2000 caracteres).")

    categoria = datos.get("categoria")
    if isinstance(categoria, str):
        categoria = [c.strip() for c in categoria.split(",") if c.strip()]
    elif isinstance(categoria, list):
        categoria = [_texto(c) for c in categoria if _texto(c)]
    else:
        categoria = []
    categoria = categoria[:5] or ["General"]

    variaciones = datos.get("variaciones") or []
    limpias_var = []
    if not isinstance(variaciones, list):
        raise ValueError("'variaciones' debe ser una lista de {nombre, precio, stock_inicial, costo}.")
    nombres_var: set[str] = set()
    for v in variaciones:
        if not isinstance(v, dict):
            raise ValueError("Cada variación debe ser un objeto {nombre, precio, stock_inicial, costo}.")
        nombre_v = _texto(v.get("nombre"))
        if not nombre_v:
            raise ValueError("Hay una variación sin nombre.")
        if nombre_v.lower() in nombres_var:
            raise ValueError(f"Variación duplicada: '{nombre_v}'.")
        nombres_var.add(nombre_v.lower())
        limpias_var.append({
            "nombre": nombre_v,
            "precio": _num(v.get("precio") or precio_venta, "precio de la variación"),
            "stock_inicial": _num(v.get("stock_inicial") or 0, "stock de la variación"),
            "costo": _num(v.get("costo") or costo, "costo de la variación"),
        })
    if limpias_var and stock:
        raise ValueError(
            "Si defines variaciones, TODO el stock vive por variación: no mezcles "
            "'stock' general con 'variaciones'. Pon el stock en cada variación."
        )

    tamano_cm = None
    if datos.get("tamano_cm") not in (None, ""):
        tamano_cm = _num(datos.get("tamano_cm"), "tamano_cm")

    return {
        "nombre": nombre,
        "descripcion": descripcion,
        "imagen": _imagen_segura(datos.get("imagen")),
        "costo": costo,
        "precio_venta": precio_venta,
        "stock": stock,
        "categoria": categoria,
        "codigo_interno": _texto(datos.get("codigo_interno")) or None,
        "codigo_barras": _texto(datos.get("codigo_barras")) or None,
        "ubicacion": _texto(datos.get("ubicacion")) or None,
        "sufijo_precio": _texto(datos.get("sufijo_precio"))[:20],
        "fraccionable": bool(datos.get("fraccionable")),
        "tamano_cm": tamano_cm,
        "variaciones": limpias_var,
    }


def _resumen_legible(d: dict) -> str:
    variaciones = d.get("variaciones") or []
    parts = [
        f"{d['nombre']} — precio ${d['precio_venta']:g}, costo ${d['costo']:g}, "
        f"stock {d['stock']:g}, categoría {', '.join(d['categoria'])}"
    ]
    if variaciones:
        parts.append("variaciones: " + "; ".join(
            f"{v['nombre']} (${v['precio']:g}, {v['stock_inicial']:g} u.)" for v in variaciones
        ))
    return " | ".join(parts)


def proponer(tenant_id: str, clave_id: int, datos_crudos: dict) -> dict:
    """Valida y guarda el borrador. Devuelve token + resumen. No toca inventario."""
    try:
        datos = validar_datos_producto(datos_crudos)
    except ValueError as e:
        return {"error": str(e)}
    datos["tipo"] = "crear_producto"  # distingue el borrador de una EDICIÓN
    borrador = mcp_borradores.crear_borrador(tenant_id, clave_id, datos)
    return {
        "borrador_id": borrador["token"],
        "expira_minutos": mcp_borradores.TTL_MINUTOS,
        "producto_propuesto": datos,
        "resumen": _resumen_legible(datos),
        "siguiente_paso": (
            "Nada se ha creado todavía. Llama a confirmar_producto con este "
            "borrador_id para crear el producto real."
        ),
    }


def confirmar(tenant_id: str, datos: dict) -> dict:
    """Crea el producto desde el borrador consumido. El Historial queda en
    registrar_actividad (productos.py) con origen 'agente_ia'."""
    from database.actividad import registrar_actividad

    if datos.get("tipo") not in (None, "crear_producto"):
        return {
            "error": (
                "Este borrador es una EDICIÓN, no un producto nuevo: "
                "confírmalo con confirmar_edicion."
            )
        }

    try:
        datos = validar_datos_producto(datos)
    except ValueError as e:
        return {"error": f"El borrador ya no es válido: {e}"}

    try:
        resultado = crear_producto_completo(
            producto=datos["nombre"],
            descripcion=datos["descripcion"],
            costo=datos["costo"],
            precio_venta=datos["precio_venta"],
            stock=datos["stock"],
            imagen=datos["imagen"],
            categoria=datos["categoria"],
            tenant_id=tenant_id,
            codigo_interno=datos["codigo_interno"],
            codigo_barras=datos["codigo_barras"],
            ubicacion=datos["ubicacion"],
            sufijo_precio=datos["sufijo_precio"],
            fraccionable=datos["fraccionable"],
            tamano_cm=datos["tamano_cm"],
            tipo_producto="stock",
            variaciones=datos["variaciones"] or None,
            origen="agente_ia",
        )
    except Exception as e:
        return {"error": f"No se pudo crear el producto: {e}"}

    if not isinstance(resultado, dict) or not resultado.get("ok"):
        mensaje = (resultado or {}).get("mensaje") if isinstance(resultado, dict) else None
        return {"error": f"No se pudo crear el producto: {mensaje or 'error desconocido'}"}

    producto_id = resultado.get("id")
    return {
        "ok": True,
        "producto_id": producto_id,
        "resumen": _resumen_legible(datos),
        "nota": (
            "El producto ya existe en tu inventario y quedó registrado en el "
            "Historial como creado por el agente IA."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# EDICIONES (nivel 3): proponer_edicion → confirmar_edicion.
#
# MISMO patrón que la creación: proponer valida, lee los valores VIEJOS del
# server (nunca del agente) y guarda el diff antes→después en el borrador;
# confirmar aplica SOLO lo guardado. Cada edición queda en registro_actividad
# con atribución "El agente IA editó..." (068: productos, ventas y gastos).
#
# Alcance a propósito (lo que el agente puede tocar sin corromper el negocio):
#   producto → descripción, nombre, precio de venta, costo (solo con permiso
#              ver_costos), estado, visibilidad, tamaño, códigos, ubicación,
#              categorías. NUNCA imagen (binario) ni recetas/variaciones.
#   venta    → fecha, cantidad, precio. Los totales y el stock se recalculan
#              en el server (actualizar_venta ya maneja PEPS y compuestos).
#   gasto    → monto, categoría, descripción.
# ══════════════════════════════════════════════════════════════════════════════

_TIPOS_EDICION = ("producto", "venta", "gasto")

_CAMPOS_PRODUCTO = (
    "nombre", "descripcion", "precio_venta", "costo", "estado",
    "visible_en_catalogo", "tamano_cm", "codigo_interno", "codigo_barras",
    "ubicacion", "categoria",
)
_CAMPOS_VENTA = ("fecha", "cantidad", "precio_real")
_CAMPOS_GASTO = ("monto", "categoria", "descripcion")

_ESTADOS_PRODUCTO = ("Activo", "Inactivo")


def _fmt_valor(v) -> str:
    if v is None or v == "":
        return "(vacío)"
    if isinstance(v, bool):
        return "Sí" if v else "No"
    if isinstance(v, float):
        return f"{v:g}"
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "…"


def _diff_legible(diff: dict) -> str:
    """'campo: antes → después | campo2: ...' — lo que verá el dueño al revisar."""
    return " | ".join(f"{c}: {_fmt_valor(a)} → {_fmt_valor(d)}" for c, (a, d) in diff.items())


def _lista_categoria(valor) -> list[str]:
    if isinstance(valor, str):
        return [c.strip() for c in valor.split(",") if c.strip()]
    if isinstance(valor, list):
        return [_texto(c) for c in valor if _texto(c)]
    raise ValueError("'categoria' debe ser una lista de nombres o un texto separado por comas.")


def _producto_actual(tenant_id: str, producto_id: int) -> dict | None:
    """Fila ACTUAL del producto (físico activo) + costo/precio del lote más
    reciente + categorías vigentes. None si no existe o no es del tenant."""
    filas = query(
        """
        SELECT p.id, p.Producto AS producto, COALESCE(p.Descripcion, '') AS descripcion,
               COALESCE(p.Imagen, 'No hay foto') AS imagen, COALESCE(p.Estado, 'Activo') AS estado,
               p.codigo_interno, p.codigo_barras, p.ubicacion, p.visible_en_catalogo,
               COALESCE(p.sufijo_precio, '') AS sufijo_precio,
               COALESCE(p.fraccionable, false) AS fraccionable, p.tamano_cm,
               (SELECT l.Costo FROM lotes l WHERE l.producto_id = p.id
                  AND l.tenant_id = p.tenant_id AND l.Estado = 'Activo'
                  ORDER BY l.Fecha_Entrada DESC LIMIT 1) AS costo_lote,
               (SELECT l.Precio_Venta FROM lotes l WHERE l.producto_id = p.id
                  AND l.tenant_id = p.tenant_id AND l.Estado = 'Activo'
                  ORDER BY l.Fecha_Entrada DESC LIMIT 1) AS precio_lote,
               COALESCE((SELECT array_agg(c.nombre ORDER BY c.nombre)
                         FROM producto_categorias pc
                         JOIN categorias c ON c.id = pc.categoria_id
                         WHERE pc.producto_id = p.id), '{}') AS categorias
        FROM productos p
        WHERE p.id = %s AND p.tenant_id = %s
          AND p.Estado = 'Activo' AND p.tipo_producto = 'stock'
        """,
        (producto_id, tenant_id),
    )
    return filas[0] if filas else None


def _venta_actual(tenant_id: str, venta_id: int) -> dict | None:
    filas = query(
        "SELECT id, Producto AS producto, Fecha AS fecha, fecha_ts, "
        "Cantidad AS cantidad, Precio_Real AS precio_real, "
        "Costo_Unitario AS costo_unitario, tipo_producto "
        "FROM ventas WHERE id = %s AND tenant_id = %s "
        "AND estado != 'Inactivo'",
        (venta_id, tenant_id),
    )
    return filas[0] if filas else None


def _gasto_actual(tenant_id: str, gasto_id: int) -> dict | None:
    filas = query(
        "SELECT id, Monto AS monto, Categoria AS categoria, "
        "COALESCE(Descripcion, '') AS descripcion "
        "FROM gastos WHERE id = %s AND Tenant_ID = %s",
        (gasto_id, tenant_id),
    )
    return filas[0] if filas else None


def proponer_edicion(
    tenant_id: str, clave_id: int, tipo: str, entidad_id: int,
    cambios: dict, con_costos: bool = False,
) -> dict:
    """
    Valida la edición, calcula el diff antes→después leyendo la base y guarda
    el borrador. NO toca nada todavía. Devuelve token + diff legible.
    """
    tipo = (tipo or "").strip().lower()
    if tipo not in _TIPOS_EDICION:
        return {"error": f"Tipo inválido: '{tipo}'. Usa 'producto', 'venta' o 'gasto'."}
    if not isinstance(cambios, dict) or not cambios:
        return {"error": "Envía al menos un campo a editar en 'cambios'."}
    try:
        entidad_id = int(entidad_id)
    except (TypeError, ValueError):
        return {"error": "El id de la entidad debe ser un número."}

    if tipo == "producto":
        resultado = _proponer_edicion_producto(tenant_id, entidad_id, cambios, con_costos)
    elif tipo == "venta":
        resultado = _proponer_edicion_venta(tenant_id, entidad_id, cambios)
    else:
        resultado = _proponer_edicion_gasto(tenant_id, entidad_id, cambios)

    if "error" in resultado:
        return resultado

    borrador = mcp_borradores.crear_borrador(tenant_id, clave_id, resultado["datos"])
    return {
        "borrador_id": borrador["token"],
        "expira_minutos": mcp_borradores.TTL_MINUTOS,
        "tipo": tipo,
        "entidad_id": entidad_id,
        "cambios_propuestos": resultado["diff"],
        "resumen": resultado["resumen"],
        "siguiente_paso": (
            "Nada se ha editado todavía. Llama a confirmar_edicion con este "
            "borrador_id para aplicar los cambios reales."
        ),
    }


def _proponer_edicion_producto(
    tenant_id: str, producto_id: int, cambios: dict, con_costos: bool,
) -> dict:
    actual = _producto_actual(tenant_id, producto_id)
    if actual is None:
        return {"error": f"No existe un producto físico activo con id {producto_id}."}

    desconocidos = set(cambios) - set(_CAMPOS_PRODUCTO)
    if desconocidos:
        return {"error": (
            f"Campos no editables por el agente: {', '.join(sorted(desconocidos))}. "
            f"Permitidos: {', '.join(_CAMPOS_PRODUCTO)}."
        )}
    if "costo" in cambios and not con_costos:
        return {"error": (
            "Editar el costo requiere que el dueño te dé permiso de ver "
            "costos (switch 'ver costos' de esta clave en el gestor)."
        )}

    base = {
        "producto": actual["producto"],
        "descripcion": actual["descripcion"],
        "imagen": actual["imagen"],
        "estado": actual["estado"],
        "categorias": list(actual["categorias"]) if actual["categorias"] else ["General"],
        "costo_lote": _num_simple(actual.get("costo_lote")),
        "precio_lote": _num_simple(actual.get("precio_lote")),
    }

    resueltos: dict = {}
    diff: dict = {}

    try:
        if "nombre" in cambios:
            nombre = _texto(cambios["nombre"])
            if not nombre:
                return {"error": "El nombre no puede quedar vacío."}
            if len(nombre) > 200:
                return {"error": "El nombre es demasiado largo (máximo 200 caracteres)."}
            resueltos["nombre"] = nombre
        if "descripcion" in cambios:
            desc = _texto(cambios["descripcion"])
            if len(desc) > 2000:
                return {"error": "La descripción es demasiado larga (máximo 2000 caracteres)."}
            resueltos["descripcion"] = desc
        if "precio_venta" in cambios:
            resueltos["precio_venta"] = _num(cambios["precio_venta"], "precio_venta")
        if "costo" in cambios:
            resueltos["costo"] = _num(cambios["costo"], "costo")
        if "estado" in cambios:
            estado = _texto(cambios["estado"]).capitalize()
            if estado not in _ESTADOS_PRODUCTO:
                return {"error": f"Estado inválido: usa 'Activo' o 'Inactivo'."}
            resueltos["estado"] = estado
        if "visible_en_catalogo" in cambios:
            resueltos["visible_en_catalogo"] = bool(cambios["visible_en_catalogo"])
        if "tamano_cm" in cambios:
            v = cambios["tamano_cm"]
            resueltos["tamano_cm"] = None if v in (None, "") else _num(v, "tamano_cm")
        for campo in ("codigo_interno", "codigo_barras", "ubicacion"):
            if campo in cambios:
                resueltos[campo] = _texto(cambios[campo])
        if "categoria" in cambios:
            cats = _lista_categoria(cambios["categoria"])
            if len(cats) > 5:
                return {"error": "Máximo 5 categorías por producto."}
            resueltos["categoria"] = cats or ["General"]
    except ValueError as e:
        return {"error": str(e)}

    # ── Diff antes→después (solo lo que de verdad cambia) ──
    pares = {
        "nombre": (base["producto"], resueltos.get("nombre")),
        "descripcion": (base["descripcion"], resueltos.get("descripcion")),
        "estado": (base["estado"], resueltos.get("estado")),
        "precio_venta": (base["precio_lote"], resueltos.get("precio_venta")),
        "costo": (base["costo_lote"], resueltos.get("costo")),
        "visible_en_catalogo": (bool(actual["visible_en_catalogo"]), resueltos.get("visible_en_catalogo")),
        "tamano_cm": (float(actual["tamano_cm"]) if actual["tamano_cm"] is not None else None,
                      resueltos.get("tamano_cm")),
        "codigo_interno": (actual["codigo_interno"], resueltos.get("codigo_interno")),
        "codigo_barras": (actual["codigo_barras"], resueltos.get("codigo_barras")),
        "ubicacion": (actual["ubicacion"], resueltos.get("ubicacion")),
        "categoria": (base["categorias"], resueltos.get("categoria")),
    }
    for campo, (antes, despues) in pares.items():
        if despues is None or campo not in resueltos:
            continue
        if _round_cmp(antes, despues):
            continue
        diff[campo] = [antes, despues]
    if not diff:
        return {"error": "Nada que editar: los valores nuevos son iguales a los actuales."}

    return {
        "datos": {
            "tipo": "editar_producto",
            "entidad_id": producto_id,
            "base": base,
            "cambios": resueltos,
        },
        "diff": diff,
        "resumen": f"Edición de {base['producto']}: {_diff_legible(diff)}",
    }


def _proponer_edicion_venta(tenant_id: str, venta_id: int, cambios: dict) -> dict:
    actual = _venta_actual(tenant_id, venta_id)
    if actual is None:
        return {"error": f"No existe una venta activa con id {venta_id}."}

    desconocidos = set(cambios) - set(_CAMPOS_VENTA)
    if desconocidos:
        return {"error": (
            f"Campos no editables en una venta: {', '.join(sorted(desconocidos))}. "
            f"Permitidos: {', '.join(_CAMPOS_VENTA)}."
        )}

    fecha = _texto(actual.get("fecha"))
    cantidad = _stock_simple(actual.get("cantidad"))
    precio = _num_simple(actual.get("precio_real"))
    diff: dict = {}

    try:
        if "fecha" in cambios:
            nueva = _texto(cambios["fecha"])
            from database.helpers import _parsear_ts
            _parsear_ts(nueva)  # valida el formato ANTES de guardar el borrador
            resueltos_fecha = nueva
        else:
            resueltos_fecha = fecha
        if "cantidad" in cambios:
            resueltos_cant = _stock_simple(cambios["cantidad"])
            if resueltos_cant is None:
                return {"error": "La cantidad debe ser un número."}
            if resueltos_cant <= 0:
                return {"error": "La cantidad debe ser mayor a 0 (para anular una venta usa el gestor)."}
        else:
            resueltos_cant = cantidad
        if "precio_real" in cambios:
            resueltos_precio = _num(cambios["precio_real"], "precio_real")
        else:
            resueltos_precio = precio
    except ValueError as e:
        return {"error": str(e)}

    if _round_cmp(cantidad, resueltos_cant) is False:
        diff["cantidad"] = [cantidad, resueltos_cant]
    if _round_cmp(precio, resueltos_precio) is False:
        diff["precio_real"] = [precio, resueltos_precio]
    from database.helpers import fecha_negocio_de
    try:
        if fecha_negocio_de(fecha, tenant_id) != fecha_negocio_de(resueltos_fecha, tenant_id):
            diff["fecha"] = [fecha, resueltos_fecha]
    except Exception:
        diff["fecha"] = [fecha, resueltos_fecha]

    if not diff:
        return {"error": "Nada que editar: los valores nuevos son iguales a los actuales."}

    return {
        "datos": {
            "tipo": "editar_venta",
            "entidad_id": venta_id,
            "producto": actual.get("producto"),
            "costo_unitario": _num_simple(actual.get("costo_unitario")),
            "datos": {
                "fecha": resueltos_fecha,
                "cantidad": resueltos_cant,
                "precio_real": resueltos_precio,
            },
        },
        "diff": diff,
        "resumen": f"Edición de la venta #{venta_id} ({actual.get('producto')}): {_diff_legible(diff)}",
    }


def _proponer_edicion_gasto(tenant_id: str, gasto_id: int, cambios: dict) -> dict:
    actual = _gasto_actual(tenant_id, gasto_id)
    if actual is None:
        return {"error": f"No existe un gasto con id {gasto_id}."}

    desconocidos = set(cambios) - set(_CAMPOS_GASTO)
    if desconocidos:
        return {"error": (
            f"Campos no editables en un gasto: {', '.join(sorted(desconocidos))}. "
            f"Permitidos: {', '.join(_CAMPOS_GASTO)}."
        )}

    monto = _num_simple(actual.get("monto"))
    categoria = actual.get("categoria") or ""
    descripcion = actual.get("descripcion") or ""
    diff: dict = {}

    try:
        resueltos_monto = _num(cambios["monto"], "monto") if "monto" in cambios else monto
        resueltos_cat = _texto(cambios["categoria"]) if "categoria" in cambios else categoria
        resueltos_desc = _texto(cambios["descripcion"]) if "descripcion" in cambios else descripcion
    except ValueError as e:
        return {"error": str(e)}
    if not resueltos_cat:
        return {"error": "El gasto necesita una categoría."}

    if _round_cmp(monto, resueltos_monto) is False:
        diff["monto"] = [monto, resueltos_monto]
    if resueltos_cat != categoria:
        diff["categoria"] = [categoria, resueltos_cat]
    if resueltos_desc != descripcion:
        diff["descripcion"] = [descripcion, resueltos_desc]

    if not diff:
        return {"error": "Nada que editar: los valores nuevos son iguales a los actuales."}

    return {
        "datos": {
            "tipo": "editar_gasto",
            "entidad_id": gasto_id,
            "datos": {
                "monto": resueltos_monto,
                "categoria": resueltos_cat,
                "descripcion": resueltos_desc,
            },
        },
        "diff": diff,
        "resumen": f"Edición del gasto #{gasto_id}: {_diff_legible(diff)}",
    }


def _round_cmp(a, b) -> bool:
    """True si 'iguales' para efectos de diff (floats redondeados a 2 dec)."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return round(float(a), 2) == round(float(b), 2)
    return a == b


def _num_simple(valor) -> float | None:
    try:
        return round(float(valor), 2) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _stock_simple(valor) -> float | None:
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def confirmar_edicion(tenant_id: str, datos: dict) -> dict:
    """Aplica la edición guardada en el borrador consumido. Cada edición
    queda en el Historial con atribución 'El agente IA editó...' (068)."""
    tipo = datos.get("tipo")
    if tipo == "crear_producto":
        return {"error": "Este borrador es una CREACIÓN: confírmalo con confirmar_producto."}
    if tipo == "editar_producto":
        return _confirmar_edicion_producto(tenant_id, datos)
    if tipo == "editar_venta":
        return _confirmar_edicion_venta(tenant_id, datos)
    if tipo == "editar_gasto":
        return _confirmar_edicion_gasto(tenant_id, datos)
    return {"error": "Borrador de edición desconocido."}


def _confirmar_edicion_producto(tenant_id: str, datos: dict) -> dict:
    base = datos.get("base") or {}
    cambios = datos.get("cambios") or {}
    producto_id = datos.get("entidad_id")

    nombre_nuevo = cambios.get("nombre")
    resultado = actualizar_producto(
        producto=base.get("producto") or "",
        # Los valores DESPUÉS viven en 'cambios'; 'base' es solo el respaldo
        # de los campos que la edición no toca (imagen, etc.).
        descripcion=(cambios["descripcion"] if "descripcion" in cambios
                     else base.get("descripcion") or ""),
        imagen=base.get("imagen") or "No hay foto",
        estado=cambios.get("estado") or base.get("estado") or "Activo",
        categoria=cambios.get("categoria") or base.get("categorias") or ["General"],
        costo=(None if cambios.get("costo") is None else float(cambios["costo"])),
        precio_venta=(None if cambios.get("precio_venta") is None else float(cambios["precio_venta"])),
        tenant_id=tenant_id,
        nuevo_producto=nombre_nuevo,
        codigo_interno=(None if "codigo_interno" not in cambios else cambios["codigo_interno"]),
        codigo_barras=(None if "codigo_barras" not in cambios else cambios["codigo_barras"]),
        ubicacion=(None if "ubicacion" not in cambios else cambios["ubicacion"]),
        visible_en_catalogo=(None if "visible_en_catalogo" not in cambios else bool(cambios["visible_en_catalogo"])),
        tamano_cm=(None if "tamano_cm" not in cambios else cambios["tamano_cm"]),
        origen="agente_ia",
    )
    if not isinstance(resultado, dict) or not resultado.get("ok"):
        mensaje = (resultado or {}).get("mensaje") if isinstance(resultado, dict) else None
        return {"error": f"No se pudo editar el producto: {mensaje or 'error desconocido'}"}
    return {
        "ok": True,
        "entidad": "producto",
        "entidad_id": producto_id,
        "nombre_final": resultado.get("producto"),
        "nota": "Los cambios ya están aplicados y quedaron en el Historial como hechos por el agente IA.",
    }


def _confirmar_edicion_venta(tenant_id: str, datos: dict) -> dict:
    from database.ventas import actualizar_venta

    venta_id = datos.get("entidad_id")
    valores = datos.get("datos") or {}
    cantidad = float(valores.get("cantidad") or 0)
    precio = float(valores.get("precio_real") or 0)
    if cantidad <= 0:
        return {"error": "La cantidad debe ser mayor a 0."}
    costo = float(datos.get("costo_unitario") or 0)
    resultado = actualizar_venta(
        venta_id=venta_id,
        fecha=valores.get("fecha") or "",
        cantidad=cantidad,
        precio_real=precio,
        costo_unitario=costo,
        total_venta=round(precio * cantidad, 2),
        ganancia_bruta=round((precio - costo) * cantidad, 2),
        tenant_id=tenant_id,
        origen="agente_ia",
    )
    if not isinstance(resultado, dict) or not resultado.get("ok"):
        mensaje = (resultado or {}).get("mensaje") if isinstance(resultado, dict) else None
        return {"error": f"No se pudo editar la venta: {mensaje or 'error desconocido'}"}
    return {
        "ok": True,
        "entidad": "venta",
        "entidad_id": venta_id,
        "nota": (
            "La venta ya está editada: el stock se recalculó solo y quedó en "
            "el Historial como hecha por el agente IA."
        ),
    }


def _confirmar_edicion_gasto(tenant_id: str, datos: dict) -> dict:
    from database.gastos import actualizar_gasto

    gasto_id = datos.get("entidad_id")
    valores = datos.get("datos") or {}
    resultado = actualizar_gasto(
        gasto_id=gasto_id,
        monto=float(valores.get("monto") or 0),
        categoria=valores.get("categoria") or "",
        descripcion=valores.get("descripcion") or "",
        tenant_id=tenant_id,
        origen="agente_ia",
    )
    if not isinstance(resultado, dict) or not resultado.get("ok"):
        return {"error": "No se pudo editar el gasto."}
    return {
        "ok": True,
        "entidad": "gasto",
        "entidad_id": gasto_id,
        "nota": "El gasto ya está editado y quedó en el Historial como hecho por el agente IA.",
    }
