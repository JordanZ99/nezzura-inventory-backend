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
from database.productos import crear_producto_completo


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
