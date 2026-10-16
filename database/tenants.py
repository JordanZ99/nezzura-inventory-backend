# ==============================================================================
# backend/database/tenants.py
# Configuración del perfil del tenant (tabla tenants): lectura y PATCH parcial.
#
# Refactor de routers/inventario.py: el SQL y las validaciones de negocio del
# perfil viven aquí; el router solo rutea HTTP y mapea los dicts de dominio a
# HTTPException (mismo patrón que database/imagenes.py y contracts/README.md).
#
# Contrato de dominio: {"ok": True, ...} en éxito; {"ok": False, "status": 400|422,
# "mensaje": ...} si una validación falla. El status conserva los códigos HTTP
# originales del router para no romper el contrato del frontend.
# ==============================================================================

import json
from zoneinfo import ZoneInfo

from database.conexion import query, execute
from database.clientes import normalizar_campos_cliente
from database.helpers import invalidar_zona_tenant

# Whitelist de días de descanso (migración 051): mismos nombres (sin acento,
# minúscula) que ataja la analítica.
DIAS_VALIDOS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")

# Campos editables del perfil (PATCH parcial: None = no tocar).
CAMPOS_PERFIL = (
    "modo_precio_sugerido", "zona_horaria", "metodo_pago_default",
    "gasto_comision_automatico", "dias_cerrados",
    "clientes_activos", "cliente_campos",
    "puntos_activos", "puntos_valor_punto", "puntos_modo",
    "puntos_gasto_monto", "puntos_gasto_pts", "puntos_fijos",
)


def leer_config_perfil(tenant_id: str) -> dict:
    """Lee modo_precio_sugerido, zona horaria, método de pago, flag de comisiones
    y la config de la cartera de clientes + sistema de puntos (migraciones 038/039)."""
    fila = query(
        "SELECT modo_precio_sugerido, zona_horaria, metodo_pago_default, gasto_comision_automatico, dias_cerrados, "
        "clientes_activos, cliente_campos, puntos_activos, puntos_valor_punto, puntos_modo, "
        "puntos_gasto_monto, puntos_gasto_pts, puntos_fijos "
        "FROM tenants WHERE id = %s",
        (tenant_id,)
    )
    f = fila[0] if fila else {}
    return {
        "modo_precio_sugerido": (f.get("modo_precio_sugerido") if fila else None) or "antiguo",
        "zona_horaria": (f.get("zona_horaria") if fila else None) or "America/Cancun",
        "metodo_pago_default": (f.get("metodo_pago_default") if fila else None) or "efectivo",
        "gasto_comision_automatico": bool(f["gasto_comision_automatico"]) if fila else False,
        # ── Días de descanso (051): JSONB → lista limpia o lista vacía
        "dias_cerrados": [d for d in (f.get("dias_cerrados") or []) if isinstance(d, str)] if fila else [],
        # ── Cartera de clientes (038) ──
        "clientes_activos": bool(f.get("clientes_activos")) if fila else False,
        "cliente_campos": normalizar_campos_cliente(f.get("cliente_campos")),
        # ── Sistema de puntos (039) ──
        "puntos_activos": bool(f.get("puntos_activos")) if fila else False,
        "puntos_valor_punto": float(f.get("puntos_valor_punto") or 1) if fila else 1.0,
        "puntos_modo": (f.get("puntos_modo") or "por_gasto") if fila else "por_gasto",
        "puntos_gasto_monto": float(f.get("puntos_gasto_monto") or 10) if fila else 10.0,
        "puntos_gasto_pts": int(f.get("puntos_gasto_pts") or 1) if fila else 1,
        "puntos_fijos": (int(f["puntos_fijos"]) if f.get("puntos_fijos") is not None else None) if fila else None,
    }


def actualizar_config_perfil(campos: dict, tenant_id: str) -> dict:
    """
    Actualiza la configuración del perfil del tenant (PATCH parcial, campo por
    campo). `campos` trae las claves de CAMPOS_PERFIL (p. ej. el model_dump del
    schema ActualizarPerfil); None = no tocar.

    - modo_precio_sugerido: cómo el POS sugiere el precio.
    - zona_horaria: nombre IANA del negocio; define el día contable de
      ventas, gastos y cortes de caja (migración 031).
    - clientes_* / puntos_*: cartera de clientes y sistema de puntos
      (migraciones 038/039, doc sistemaPuntos.md).

    Los UPDATE se aplican en el orden de los campos, igual que el PATCH
    original: si una validación falla a mitad de camino, los campos previos
    ya quedaron guardados (comportamiento conservado a propósito para no
    cambiar el contrato observable de la API).
    """
    if not any(campos.get(k) is not None for k in CAMPOS_PERFIL):
        # Sin cambios: devolver el valor actual persistido
        return {"ok": True, **leer_config_perfil(tenant_id)}

    respuesta = {"ok": True}

    if campos.get("gasto_comision_automatico") is not None:
        execute("UPDATE tenants SET gasto_comision_automatico = %s WHERE id = %s",
                (bool(campos["gasto_comision_automatico"]), tenant_id))
        respuesta["gasto_comision_automatico"] = bool(campos["gasto_comision_automatico"])

    if campos.get("dias_cerrados") is not None:
        dias_cerrados = campos["dias_cerrados"]
        if not isinstance(dias_cerrados, list):
            return {"ok": False, "status": 400, "mensaje": "dias_cerrados debe ser una lista"}
        dias = [str(d).strip().lower() for d in dias_cerrados]
        desconocidos = [d for d in dias if d not in DIAS_VALIDOS]
        if desconocidos:
            return {"ok": False, "status": 400,
                    "mensaje": f"dias_cerrados: días inválidos {desconocidos}; usa: {', '.join(DIAS_VALIDOS)}"}
        dias = sorted(set(dias))
        execute("UPDATE tenants SET dias_cerrados = %s::jsonb WHERE id = %s",
                (json.dumps(dias), tenant_id))
        respuesta["dias_cerrados"] = dias

    if campos.get("metodo_pago_default") is not None:
        metodo = campos["metodo_pago_default"].strip().lower()
        if metodo not in ("efectivo", "tarjeta_debito", "tarjeta_credito"):
            return {"ok": False, "status": 400,
                    "mensaje": "metodo_pago_default debe ser 'efectivo', 'tarjeta_debito' o 'tarjeta_credito'"}
        execute("UPDATE tenants SET metodo_pago_default = %s WHERE id = %s", (metodo, tenant_id))
        respuesta["metodo_pago_default"] = metodo

    if campos.get("modo_precio_sugerido") is not None:
        modo = campos["modo_precio_sugerido"].strip().lower()
        if modo not in ("antiguo", "maximo", "reciente"):
            return {"ok": False, "status": 400,
                    "mensaje": "modo_precio_sugerido debe ser 'antiguo', 'maximo' o 'reciente'"}
        execute("UPDATE tenants SET modo_precio_sugerido = %s WHERE id = %s", (modo, tenant_id))
        respuesta["modo_precio_sugerido"] = modo

    if campos.get("zona_horaria") is not None:
        zona = campos["zona_horaria"].strip()
        try:
            ZoneInfo(zona)
        except Exception:
            return {"ok": False, "status": 400,
                    "mensaje": f"'{zona}' no es una zona horaria IANA válida (ej. 'America/Cancun')"}
        execute("UPDATE tenants SET zona_horaria = %s WHERE id = %s", (zona, tenant_id))
        # El backend cachea la zona 5 min: invalidarla para que el cambio
        # surta efecto de inmediato en ventas/gastos/cortes.
        invalidar_zona_tenant(tenant_id)
        respuesta["zona_horaria"] = zona

    # ── Cartera de clientes (038) ──
    if campos.get("clientes_activos") is not None:
        execute("UPDATE tenants SET clientes_activos = %s WHERE id = %s",
                (bool(campos["clientes_activos"]), tenant_id))
        respuesta["clientes_activos"] = bool(campos["clientes_activos"])

    if campos.get("cliente_campos") is not None:
        try:
            campos_cliente = normalizar_campos_cliente(campos["cliente_campos"])
        except Exception:
            return {"ok": False, "status": 422, "mensaje": "cliente_campos debe ser un objeto"}
        execute("UPDATE tenants SET cliente_campos = %s::jsonb WHERE id = %s",
                (json.dumps(campos_cliente), tenant_id))
        respuesta["cliente_campos"] = campos_cliente

    # ── Sistema de puntos (039) ──
    if campos.get("puntos_activos") is not None:
        execute("UPDATE tenants SET puntos_activos = %s WHERE id = %s",
                (bool(campos["puntos_activos"]), tenant_id))
        respuesta["puntos_activos"] = bool(campos["puntos_activos"])

    if campos.get("puntos_valor_punto") is not None:
        valor = float(campos["puntos_valor_punto"])
        if valor <= 0:
            return {"ok": False, "status": 422,
                    "mensaje": "puntos_valor_punto debe ser mayor a 0 (ej. 1 = '1 punto vale $1'; 0.01 = '100 puntos valen $1')"}
        execute("UPDATE tenants SET puntos_valor_punto = %s WHERE id = %s", (valor, tenant_id))
        respuesta["puntos_valor_punto"] = valor

    if campos.get("puntos_modo") is not None:
        modo = campos["puntos_modo"].strip().lower()
        if modo not in ("por_gasto", "fijo"):
            return {"ok": False, "status": 422, "mensaje": "puntos_modo debe ser 'por_gasto' o 'fijo'"}
        execute("UPDATE tenants SET puntos_modo = %s WHERE id = %s", (modo, tenant_id))
        respuesta["puntos_modo"] = modo

    if campos.get("puntos_gasto_monto") is not None:
        monto = float(campos["puntos_gasto_monto"])
        if monto <= 0:
            return {"ok": False, "status": 422,
                    "mensaje": "puntos_gasto_monto debe ser mayor a 0 (ej. 10 en '1 punto por cada $10')"}
        execute("UPDATE tenants SET puntos_gasto_monto = %s WHERE id = %s", (monto, tenant_id))
        respuesta["puntos_gasto_monto"] = monto

    if campos.get("puntos_gasto_pts") is not None:
        pts = int(campos["puntos_gasto_pts"])
        if pts < 1:
            return {"ok": False, "status": 422, "mensaje": "puntos_gasto_pts debe ser al menos 1"}
        execute("UPDATE tenants SET puntos_gasto_pts = %s WHERE id = %s", (pts, tenant_id))
        respuesta["puntos_gasto_pts"] = pts

    if campos.get("puntos_fijos") is not None:
        fijos = int(campos["puntos_fijos"])
        if fijos < 0:
            return {"ok": False, "status": 422, "mensaje": "puntos_fijos no puede ser negativo"}
        execute("UPDATE tenants SET puntos_fijos = %s WHERE id = %s", (fijos, tenant_id))
        respuesta["puntos_fijos"] = fijos

    respuesta.update(leer_config_perfil(tenant_id))
    return respuesta
