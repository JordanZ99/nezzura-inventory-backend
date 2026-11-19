# ==============================================================================
# backend/database/mcp_recetas.py
# Recetas de productos COMPUESTOS para el conector MCP (rol inventario).
#
# Un compuesto (ej. una hamburguesa) no tiene stock propio: se vende contra
# los materiales de su receta (BOM). Hasta ahora el agente no los veía porque
# TODAS las tools filtraban tipo_producto = 'stock'. Este módulo abre el caso
# del restaurante SIN duplicar el motor de recetas del gestor:
#
#   - ver_receta: lectura pura (materiales + disponibilidad estimada).
#   - proponer_receta / confirmar_receta: MISMO patrón borrador→confirmar del
#     resto de escrituras. proponer valida y guarda los cambios exactos
#     (agregar / cambiar cantidad / quitar); confirmar aplica SOLO lo guardado
#     con los helpers probados de database/recetas.py (los mismos que usa la
#     pantalla de inventario) y deja el cambio en el Historial con origen
#     'agente_ia'.
#
# Lo que NO hace (a propósito, v1): crear compuestos ni venderlos por chat.
# Los compuestos no tienen lotes: su precio vive en productos.precio_servicio.
# ==============================================================================

from database import mcp_borradores
from database.actividad import registrar_actividad
from database.conexion import query
from database.recetas import (
    _calcular_disponibilidad_compuestos,
    actualizar_material_receta,
    agregar_material_receta,
    eliminar_material_receta,
    listar_recetas_producto,
)

_ACCIONES = ("agregar", "cambiar", "quitar")


def _texto(valor) -> str:
    return (str(valor) if valor is not None else "").strip()


def _decimal(valor) -> float:
    try:
        return round(float(valor or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _fmt(cantidad: float) -> str:
    """Cantidad legible: 2 en vez de 2.0, 0.5 tal cual."""
    return f"{cantidad:g}"


# ── Resolución de productos (id o nombre, tolerante a mayúsculas) ────────────
# El agente normalmente pasa el nombre que devolvió buscar_productos, pero a
# veces recibe un id. La resolución es estricta al ESCRIBIR (un compuesto
# equivocado sería un desastre): nombre exacto o único parcial; si hay varias
# coincidencias, devolvemos la lista para que el agente pregunte.

_SELECT_PRODUCTO = (
    "SELECT p.id, p.Producto AS producto, p.tipo_producto, "
    "COALESCE(p.precio_servicio, 0) AS precio_venta "
    "FROM productos p "
    "WHERE p.tenant_id = %s AND p.Estado = 'Activo'"
)


def _buscar_producto(tenant_id: str, producto) -> tuple[dict | None, str | None]:
    """Resuelve un producto ACTIVO del tenant por id o por nombre."""
    pid = None
    if isinstance(producto, int):
        pid = producto
    else:
        texto = _texto(producto)
        if not texto:
            return None, "Indica el producto (nombre o id)."
        if texto.isdigit():
            pid = int(texto)

    if pid is not None:
        filas = query(f"{_SELECT_PRODUCTO} AND p.id = %s", (tenant_id, pid))
        if not filas:
            return None, f"No existe un producto activo con id {pid}."
        return filas[0], None

    texto = _texto(producto)
    filas = query(
        f"{_SELECT_PRODUCTO} AND lower(p.Producto) = lower(%s) ORDER BY p.id LIMIT 2",
        (tenant_id, texto),
    )
    if len(filas) == 1:
        return filas[0], None
    if len(filas) > 1:
        return None, (
            f"Hay más de un producto llamado '{texto}'. Pásame el id: "
            + ", ".join(f"{f['producto']} (#{f['id']})" for f in filas)
            + "."
        )

    parciales = query(
        f"{_SELECT_PRODUCTO} AND lower(p.Producto) LIKE lower(%s) "
        "ORDER BY p.Producto LIMIT 6",
        (tenant_id, f"%{texto}%"),
    )
    if len(parciales) == 1:
        return parciales[0], None
    if parciales:
        return None, (
            f"No encontré '{texto}' con exactitud. ¿Cuál es? "
            + ", ".join(f"{f['producto']} (#{f['id']})" for f in parciales)
            + "."
        )
    return None, f"No existe un producto activo llamado '{texto}'."


def _materiales(producto: str, tenant_id: str) -> list[dict]:
    """Receta normalizada para el agente (receta_id incluido)."""
    return [
        {
            "receta_id": m["id"],
            "material": m["material"],
            "cantidad": _decimal(m["cantidad"]),
            "variacion_id": m.get("variacion_id"),
            "variacion": m.get("variacion"),
        }
        for m in listar_recetas_producto(producto, tenant_id)
    ]


def _disponibilidad(tenant_id: str, producto: str, materiales: list[dict]) -> int | None:
    """Disponibilidad estimada reutilizando el motor del gestor: cuántas
    unidades se pueden vender con el stock actual de los ingredientes."""
    fila = [{
        "producto": producto,
        "tipo_producto": "compuesto",
        "recetas": materiales,
    }]
    _calcular_disponibilidad_compuestos(fila, tenant_id)
    return fila[0].get("disponibilidad_estimada")


def _variacion_de(
    tenant_id: str, producto_id: int, variacion,
) -> tuple[int | None, str | None]:
    """Resuelve la variación de una receta por nombre o id. None = base."""
    if variacion in (None, ""):
        return None, None
    texto = _texto(variacion)

    if texto.isdigit():
        vid = int(texto)
        filas = query(
            "SELECT id FROM producto_variaciones "
            "WHERE id = %s AND producto_id = %s AND tenant_id = %s",
            (vid, producto_id, tenant_id),
        )
        if not filas:
            return None, f"La variación #{vid} no pertenece a este producto."
        return filas[0]["id"], None

    filas = query(
        "SELECT id FROM producto_variaciones "
        "WHERE producto_id = %s AND tenant_id = %s AND lower(nombre) = lower(%s)",
        (producto_id, tenant_id, texto),
    )
    if filas:
        return filas[0]["id"], None

    opciones = query(
        "SELECT nombre FROM producto_variaciones "
        "WHERE producto_id = %s AND tenant_id = %s ORDER BY nombre",
        (producto_id, tenant_id),
    )
    if opciones:
        return None, (
            f"'{texto}' no es una variación de este producto. Opciones: "
            + ", ".join(o["nombre"] for o in opciones)
            + "."
        )
    return None, "Este producto no tiene variaciones: deja 'variacion' vacía."


def _resolver_material(
    tenant_id: str, material, material_id,
) -> tuple[dict | None, str | None]:
    """Resuelve el material de una receta por id (material_id) o por nombre."""
    mid = None
    if material_id not in (None, ""):
        try:
            mid = int(material_id)
        except (TypeError, ValueError):
            return None, "'material_id' debe ser un número."
    elif _texto(material).isdigit():
        mid = int(_texto(material))

    if mid is not None:
        filas = query(
            "SELECT id, Producto AS producto FROM productos "
            "WHERE id = %s AND tenant_id = %s AND Estado = 'Activo'",
            (mid, tenant_id),
        )
        if not filas:
            return None, f"No existe un material activo con id {mid}."
        return filas[0], None

    texto = _texto(material)
    if not texto:
        return None, "Falta el material (nombre o material_id)."

    filas = query(
        "SELECT id, Producto AS producto FROM productos "
        "WHERE tenant_id = %s AND Estado = 'Activo' AND lower(Producto) = lower(%s)",
        (tenant_id, texto),
    )
    if len(filas) == 1:
        return filas[0], None
    if len(filas) > 1:
        return None, f"Hay más de un producto llamado '{texto}'. Pásame el material_id."

    parciales = query(
        "SELECT id, Producto AS producto FROM productos "
        "WHERE tenant_id = %s AND Estado = 'Activo' "
        "AND lower(Producto) LIKE lower(%s) ORDER BY Producto LIMIT 6",
        (tenant_id, f"%{texto}%"),
    )
    if len(parciales) == 1:
        return parciales[0], None
    if parciales:
        return None, (
            f"No encontré un material '{texto}' exacto. ¿Cuál es? "
            + ", ".join(f"{f['producto']} (#{f['id']})" for f in parciales)
            + "."
        )
    return None, f"No existe un material activo llamado '{texto}'."


# ── Tool: ver_receta ─────────────────────────────────────────────────────────

def ver_receta(tenant_id: str, producto) -> dict:
    """Materiales de un compuesto + disponibilidad. No lanza: los errores son
    mensajes que el agente puede usar para corregirse solo."""
    fila, error = _buscar_producto(tenant_id, producto)
    if error:
        return {"error": error}

    if fila.get("tipo_producto") != "compuesto":
        return {"error": (
            f"'{fila['producto']}' es un producto simple "
            f"({fila.get('tipo_producto') or 'stock'}), no un compuesto: no "
            "tiene receta. Su stock se gestiona con lotes (proponer_entrada)."
        )}

    materiales = _materiales(fila["producto"], tenant_id)
    disponible = _disponibilidad(tenant_id, fila["producto"], materiales)
    return {
        "producto": fila["producto"],
        "producto_id": fila["id"],
        "tipo": "compuesto",
        "precio_venta": _decimal(fila.get("precio_venta")),
        "disponibilidad_estimada": disponible,
        "materiales": materiales,
        "nota": (
            "disponibilidad_estimada = unidades que se pueden vender con el "
            "stock actual de los ingredientes (el mínimo entre la receta base "
            "y las variaciones). Las filas con 'variacion' son la receta de "
            "esa presentación; las que la tienen en null son la receta base. "
            "Usa 'receta_id' para cambiar la cantidad o quitar un material "
            "con proponer_receta."
            if materiales else
            "Este compuesto no tiene receta: no se puede vender ni estimar su "
            "disponibilidad. Agrégale materiales con proponer_receta."
        ),
    }


# ── Tool: proponer_receta → confirmar_receta (borrador → confirmar) ─────────

def _resumen_operacion(op: dict) -> str:
    if op.get("accion") == "agregar":
        return f"agregó {op.get('material')} x{_fmt(_decimal(op.get('cantidad')))}"
    if op.get("accion") == "cambiar":
        return (
            f"{op.get('material')}: {_fmt(_decimal(op.get('antes')))} → "
            f"{_fmt(_decimal(op.get('cantidad')))}"
        )
    return f"quitó {op.get('material')}"


def proponer_receta(tenant_id: str, clave_id: int, producto, cambios) -> dict:
    """
    Valida los cambios contra la receta ACTUAL (leída del servidor, nunca del
    agente) y guarda el borrador. No modifica nada. Cada cambio es:
      {accion: 'agregar', material | material_id, cantidad, variacion?}
      {accion: 'cambiar', receta_id, cantidad}
      {accion: 'quitar', receta_id}
    """
    fila, error = _buscar_producto(tenant_id, producto)
    if error:
        return {"error": error}
    if fila.get("tipo_producto") != "compuesto":
        return {"error": (
            f"'{fila['producto']}' es un producto simple: no tiene receta que "
            "editar. Las recetas son de productos compuestos."
        )}
    if not isinstance(cambios, list) or not cambios:
        return {"error": (
            "Envía 'cambios' como lista: {accion:'agregar', material, "
            "cantidad}, {accion:'cambiar', receta_id, cantidad} o "
            "{accion:'quitar', receta_id}."
        )}

    actuales = _materiales(fila["producto"], tenant_id)
    por_id = {m["receta_id"]: m for m in actuales}
    por_material = {
        (m["material"].strip().lower(), m.get("variacion_id")): m
        for m in actuales
    }

    operaciones: list[dict] = []
    vistos: set = set()

    for i, cambio in enumerate(cambios, start=1):
        if not isinstance(cambio, dict):
            return {"error": f"El cambio #{i} debe ser un objeto."}

        accion = _texto(cambio.get("accion")).lower()
        if accion not in _ACCIONES:
            return {"error": (
                f"Acción inválida '{accion}' en el cambio #{i}. Usa: "
                "agregar, cambiar o quitar."
            )}

        if accion == "agregar":
            material, err = _resolver_material(
                tenant_id, cambio.get("material"), cambio.get("material_id")
            )
            if err:
                return {"error": err}
            if material["id"] == fila["id"]:
                return {"error": "Un producto no puede ser ingrediente de sí mismo."}

            try:
                cantidad = round(float(cambio.get("cantidad")), 2)
            except (TypeError, ValueError):
                return {"error": f"El cambio #{i}: 'cantidad' debe ser un número."}
            if cantidad <= 0:
                return {"error": f"El cambio #{i}: la cantidad debe ser mayor a 0."}

            vid, err = _variacion_de(tenant_id, fila["id"], cambio.get("variacion"))
            if err:
                return {"error": err}

            clave = (material["producto"].strip().lower(), vid)
            if clave in vistos:
                return {"error": (
                    f"'{material['producto']}' aparece dos veces en los cambios "
                    "para la misma receta: deja un solo cambio para ese material."
                )}
            vistos.add(clave)

            existente = por_material.get(clave)
            if existente:
                # Ya está en la receta: agregar equivale a cambiar la cantidad
                # (misma semántica que agregar_material_receta del gestor).
                vistos.add(existente["receta_id"])
                if existente["cantidad"] == cantidad:
                    continue
                operaciones.append({
                    "accion": "cambiar",
                    "receta_id": existente["receta_id"],
                    "material": existente["material"],
                    "variacion_id": vid,
                    "antes": existente["cantidad"],
                    "cantidad": cantidad,
                })
            else:
                operaciones.append({
                    "accion": "agregar",
                    "material": material["producto"],
                    "material_id": material["id"],
                    "variacion_id": vid,
                    "antes": None,
                    "cantidad": cantidad,
                })
            continue

        # ── cambiar / quitar: la fila se identifica por receta_id ──
        try:
            receta_id = int(cambio.get("receta_id"))
        except (TypeError, ValueError):
            return {"error": (
                f"El cambio #{i}: 'receta_id' es obligatorio y debe ser un "
                "número (sale de ver_receta)."
            )}
        actual = por_id.get(receta_id)
        if not actual:
            ids = ", ".join(
                f"#{m['receta_id']} ({m['material']})" for m in actuales
            ) or "ninguno"
            return {"error": (
                f"El cambio #{i}: receta_id {receta_id} no pertenece a la receta "
                f"de {fila['producto']}. Ids válidos: {ids}."
            )}
        if receta_id in vistos:
            return {"error": (
                f"El cambio #{i}: la fila {receta_id} aparece dos veces en los "
                "cambios."
            )}
        vistos.add(receta_id)

        if accion == "quitar":
            operaciones.append({
                "accion": "quitar",
                "receta_id": receta_id,
                "material": actual["material"],
                "variacion_id": actual.get("variacion_id"),
                "antes": actual["cantidad"],
            })
            continue

        try:
            cantidad = round(float(cambio.get("cantidad")), 2)
        except (TypeError, ValueError):
            return {"error": f"El cambio #{i}: 'cantidad' debe ser un número."}
        if cantidad <= 0:
            return {"error": f"El cambio #{i}: la cantidad debe ser mayor a 0."}
        if actual["cantidad"] == cantidad:
            continue
        operaciones.append({
            "accion": "cambiar",
            "receta_id": receta_id,
            "material": actual["material"],
            "variacion_id": actual.get("variacion_id"),
            "antes": actual["cantidad"],
            "cantidad": cantidad,
        })

    if not operaciones:
        return {"error": (
            "Nada que cambiar: la receta ya está así (o los cambios no "
            "modifican ninguna cantidad)."
        )}

    datos = {
        "tipo": "editar_receta",
        "producto": fila["producto"],
        "producto_id": fila["id"],
        "operaciones": operaciones,
    }
    borrador = mcp_borradores.crear_borrador(tenant_id, clave_id, datos)
    resumen = (
        f"Receta de {fila['producto']}: "
        + "; ".join(_resumen_operacion(op) for op in operaciones)
    )
    return {
        "borrador_id": borrador["token"],
        "expira_minutos": mcp_borradores.TTL_MINUTOS,
        "producto": fila["producto"],
        "producto_id": fila["id"],
        "receta_actual": actuales,
        "cambios_propuestos": operaciones,
        "resumen": resumen,
        "siguiente_paso": (
            "Nada se ha cambiado todavía. Muéstrale este resumen al dueño y, "
            "si lo aprueba, llama a confirmar_receta con este borrador_id."
        ),
    }


def confirmar_receta(tenant_id: str, datos: dict) -> dict:
    """Aplica los cambios guardados en el borrador consumido. Los helpers de
    database/recetas.py validan tenant y pertenencia en cada fila."""
    if not isinstance(datos, dict) or datos.get("tipo") != "editar_receta":
        return {"error": "Este borrador no es una edición de receta."}

    producto = datos.get("producto") or ""
    operaciones = datos.get("operaciones") or []
    aplicados: list[str] = []
    errores: list[str] = []

    for op in operaciones:
        accion = op.get("accion")
        try:
            if accion == "agregar":
                resultado = agregar_material_receta(
                    producto, op.get("material"),
                    float(op.get("cantidad") or 0), tenant_id,
                    op.get("variacion_id"),
                )
            elif accion == "cambiar":
                resultado = actualizar_material_receta(
                    int(op.get("receta_id")),
                    float(op.get("cantidad") or 0), tenant_id,
                )
            elif accion == "quitar":
                resultado = eliminar_material_receta(
                    int(op.get("receta_id")), tenant_id
                )
            else:
                errores.append(f"Acción desconocida: {accion}")
                continue
        except Exception as e:
            errores.append(f"{op.get('material') or op.get('receta_id')}: {e}")
            continue

        if not isinstance(resultado, dict) or not resultado.get("ok"):
            mensaje = (
                resultado.get("mensaje") if isinstance(resultado, dict)
                else "error desconocido"
            )
            errores.append(
                f"{op.get('material') or op.get('receta_id')}: {mensaje}"
            )
            continue
        aplicados.append(_resumen_operacion(op))

    if errores:
        return {
            "ok": False,
            "aplicados": aplicados,
            "errores": errores,
            "nota": (
                "Algunos cambios no se pudieron aplicar (los demás SÍ quedaron). "
                "Revisa la receta actual con ver_receta antes de volver a "
                "proponer."
            ),
        }

    materiales = _materiales(producto, tenant_id)
    disponible = _disponibilidad(tenant_id, producto, materiales)
    resumen = f"El agente IA editó la receta de {producto}: " + "; ".join(aplicados)
    registrar_actividad(
        tenant_id, "producto", "receta", "editar",
        resumen=resumen,
        entidad_id=datos.get("producto_id"),
        detalle={"origen": "agente_ia", "producto": producto,
                 "cambios": operaciones},
    )
    return {
        "ok": True,
        "producto": producto,
        "producto_id": datos.get("producto_id"),
        "materiales": materiales,
        "disponibilidad_estimada": disponible,
        "resumen": resumen,
        "nota": (
            "La receta ya está actualizada y el cambio quedó en el Historial "
            "del gestor como hecho por el agente IA. La próxima venta "
            "descontará estos materiales."
        ),
    }
