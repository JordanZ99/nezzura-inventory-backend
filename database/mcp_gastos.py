# ==============================================================================
# backend/database/mcp_gastos.py
# Registrar GASTOS desde el agente IA (rol Cajero): proponer_gasto →
# confirmar_gasto. El dueño aprueba el desglose (monto, categoría, descripción,
# fecha) antes de que toque su caja contable.
#
# Reglas de diseño:
#   - Reutiliza el MISMO motor del gestor: insertar_gasto (gastos.py) con su
#     dual-write de fecha (fecha_negocio_de: 'YYYY-MM-DD' tal cual).
#   - Estado SIEMPRE 'pagado' en v1: el gasto que el agente registra ya salió
#     de la caja (gastos programados/pendientes son flujo del gestor).
#   - La categoría es texto del negocio: si la propuesta no coincide con las
#     categorías existentes del tenant, AVISO (no bloqueo) con la lista — el
#     dueño decide si era una categoría nueva legítima.
#   - Fail-closed: todo se valida ANTES de crear el borrador. El Historial
#     queda en registrar_actividad (los gastos creados a mano no lo tienen:
#     el agente es el que necesita auditoría).
# ==============================================================================

from database import mcp_borradores
from database.actividad import registrar_actividad
from database.gastos import insertar_gasto, listar_categorias_gasto
from database.helpers import hoy_negocio
from database.mcp_cobro import _validar_fecha


def proponer_gasto(
    tenant_id: str, clave_id: int,
    monto: float, categoria: str, descripcion: str,
    fecha: str | None = None, nota: str = "",
) -> dict:
    """Valida el gasto (fail-early) y guarda el borrador tipo 'registrar_gasto'."""
    try:
        monto = round(float(monto), 2)
    except (TypeError, ValueError):
        return {"error": "El monto debe ser un número mayor a 0."}
    if monto <= 0:
        return {"error": "El monto debe ser mayor a 0."}

    descripcion = str(descripcion or "").strip()
    if not descripcion or len(descripcion) > 200:
        return {"error": "La descripción es obligatoria (1-200 caracteres): qué se gastó."}
    categoria = str(categoria or "").strip()
    if not categoria or len(categoria) > 60:
        return {"error": "La categoría es obligatoria (1-60 caracteres): ej. 'Renta', 'Empaque'."}

    error_fecha = _validar_fecha(fecha, tenant_id)
    if error_fecha:
        return {"error": error_fecha}
    fecha = str(fecha).strip() if fecha else None

    nota = str(nota or "").strip()
    if len(nota) > 500:
        return {"error": "La nota no puede pasar de 500 caracteres."}

    # Categorías del negocio (texto libre, sin FK): si la propuesta no coincide
    # con ninguna existente, aviso — el dueño ve al aprobar si era legítima.
    existentes = listar_categorias_gasto(tenant_id)
    nombres_existentes = [str(c.get("nombre") or "").strip() for c in existentes]
    avisos: list[str] = []
    if categoria not in nombres_existentes:
        avisos.append(
            f"⚠ La categoría '{categoria}' no existe en tu gestor aún: se usará "
            "como categoría nueva."
        )

    borrador = mcp_borradores.crear_borrador(tenant_id, clave_id, {
        "tipo": "registrar_gasto",
        "monto": monto,
        "categoria": categoria,
        "descripcion": descripcion,
        "fecha": fecha,
        "nota": nota,
    })

    desglose = {
        "monto": monto,
        "categoria": categoria,
        "descripcion": descripcion,
        "estado": "pagado",
    }
    if fecha:
        desglose["fecha"] = fecha

    return {
        "borrador_id": borrador["token"],
        "expira_minutos": mcp_borradores.TTL_MINUTOS,
        "desglose": desglose,
        "categorias_del_negocio": [n for n in nombres_existentes if n],
        "avisos": avisos,
        "siguiente_paso": (
            "Nada se ha registrado todavía. Muestra ESTE desglose al dueño y "
            "haz UNA sola pregunta: '¿lo registro así o corriges algo?'. Solo "
            "si aprueba, llama a confirmar_gasto con este borrador_id."
        ),
    }


def confirmar_gasto(tenant_id: str, datos: dict) -> dict:
    """Registra el gasto con el motor del gestor y lo deja en el Historial."""
    if datos.get("tipo") != "registrar_gasto":
        tipo = datos.get("tipo") or "crear_producto"
        return {
            "error": (
                f"Este borrador no es un gasto (es '{tipo}'): confírmalo con la "
                "herramienta correspondiente, no con confirmar_gasto."
            )
        }

    fecha = datos.get("fecha") or str(hoy_negocio(tenant_id))
    try:
        insertar_gasto(
            fecha=fecha,
            categoria=datos["categoria"],
            descripcion=datos["descripcion"],
            monto=float(datos["monto"]),
            tenant_id=tenant_id,
            estado="pagado",
        )
    except Exception as e:
        return {"error": f"No se pudo registrar el gasto: {e}"}

    detalle = {"origen": "agente_ia", "categoria": datos["categoria"], "fecha": fecha}
    if datos.get("nota"):
        detalle["nota"] = datos["nota"]
    try:
        registrar_actividad(
            tenant_id, "gasto", "gasto", "crear",
            resumen=(
                f"El agente IA registró el gasto: '{datos['descripcion']}' "
                f"(${datos['monto']}, {datos['categoria']})"
            ),
            detalle=detalle,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "monto": float(datos["monto"]),
        "categoria": datos["categoria"],
        "descripcion": datos["descripcion"],
        "fecha_registrada": fecha,
        "siguiente_paso": (
            "Gasto registrado en el gestor. Dile al dueño el monto y la "
            "categoría; queda en su Historial como hecho por el agente IA."
        ),
    }
