# ==============================================================================
# backend/database/clientes.py
# Cartera de clientes por tenant (Fase A del sistema de puntos).
#
# Reglas clave:
# - El nombre SIEMPRE es obligatorio (no configurable). email / telefono / pin
#   se piden según tenants.cliente_campos ({activo, requerido} por campo).
# - email/telefono son únicos por tenant SOLO cuando existen (índices parciales).
# - La contraseña del cliente (pin) se guarda hasheada con pbkdf2_sha256:
#   jamás en texto plano y jamás sale de la BDD en respuestas.
# - El saldo de puntos NO vive aquí: es SUM(puntos_movimientos) (database/puntos.py).
# ==============================================================================

import hashlib
import secrets

from database.conexion import query, execute

# Defaults de la migración 038 (nombre siempre obligatorio, fuera de este dict).
CAMPOS_DEFAULT = {
    "email": {"activo": True, "requerido": True},
    "telefono": {"activo": True, "requerido": True},
    "pin": {"activo": False, "requerido": False},
}
_CAMPOS_VALIDOS = ("email", "telefono", "pin")

_PBKDF2_ITERACIONES = 100_000


# ── PIN (contraseña de identificación del cliente en el POS) ─────────────────

def _hash_pin(pin: str) -> str:
    """pbkdf2_sha256 con salt aleatorio por cliente (formato 'algo$salt$digest')."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERACIONES
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def _verificar_pin(pin: str, almacenado) -> bool:
    """Comparación en tiempo constante; False si el cliente no tiene pin."""
    if not almacenado or not pin:
        return False
    try:
        _, salt, digest = str(almacenado).split("$", 2)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERACIONES
    ).hex()
    return secrets.compare_digest(calc, digest)


# ── Config de campos (cliente_campos en tenants) ─────────────────────────────

def normalizar_campos_cliente(bruto) -> dict:
    """
    Normaliza el JSONB cliente_campos a {email, telefono, pin} con {activo,
    requerido}, aplicando defaults para lo ausente. 'requerido' implica activo.
    """
    campos = {k: dict(v) for k, v in CAMPOS_DEFAULT.items()}
    if isinstance(bruto, dict):
        for k in _CAMPOS_VALIDOS:
            cfg = bruto.get(k)
            if isinstance(cfg, dict):
                activo = bool(cfg.get("activo", CAMPOS_DEFAULT[k]["activo"]))
                requerido = bool(cfg.get("requerido", CAMPOS_DEFAULT[k]["requerido"]))
                campos[k] = {"activo": (True if requerido else activo), "requerido": requerido}
    return campos


def _leer_config(tenant_id: str) -> dict:
    """{activo: cartera encendida, campos: config normalizada, valor_punto...}."""
    filas = query(
        "SELECT clientes_activos, cliente_campos, puntos_activos, puntos_valor_punto, "
        "puntos_modo, puntos_gasto_monto, puntos_gasto_pts, puntos_fijos "
        "FROM tenants WHERE id = %s",
        (tenant_id,)
    )
    f = filas[0] if filas else {}
    return {
        "activo": bool(f.get("clientes_activos")),
        "campos": normalizar_campos_cliente(f.get("cliente_campos")),
        "puntos_activos": bool(f.get("puntos_activos")),
        "puntos_valor_punto": float(f.get("puntos_valor_punto") or 1.0),
        "puntos_modo": f.get("puntos_modo") or "por_gasto",
        "puntos_gasto_monto": float(f.get("puntos_gasto_monto") or 10.0),
        "puntos_gasto_pts": int(f.get("puntos_gasto_pts") or 1),
        "puntos_fijos": int(f["puntos_fijos"]) if f.get("puntos_fijos") is not None else None,
    }


def _validar_contra_config(campos: dict, email: str | None, telefono: str | None, pin: str | None) -> list[str]:
    """Devuelve la lista de campos obligatorios faltantes según la config."""
    faltantes = []
    if campos["email"]["requerido"] and not (email or "").strip():
        faltantes.append("correo")
    if campos["telefono"]["requerido"] and not (telefono or "").strip():
        faltantes.append("número")
    if campos["pin"]["requerido"] and not (pin or "").strip():
        faltantes.append("contraseña")
    return faltantes


def _duplicado(tenant_id: str, email: str | None, telefono: str | None, excluir_id: str | None = None) -> str | None:
    """Detecta email/telefono repetidos dentro del tenant. Devuelve el campo o None."""
    condiciones, params = [], []
    if email:
        condiciones.append("email = %s")
        params.append(email)
    if telefono:
        condiciones.append("telefono = %s")
        params.append(telefono)
    if not condiciones:
        return None
    extra = " AND id <> %s" if excluir_id else ""
    if excluir_id:
        params.append(excluir_id)
    filas = query(
        f"SELECT email, telefono FROM clientes WHERE tenant_id = %s AND ({' OR '.join(condiciones)}){extra}",
        (tenant_id, *params)
    )
    for f in filas:
        if email and (f.get("email") or "").strip().lower() == email.lower():
            return "correo"
        if telefono and (f.get("telefono") or "").strip() == telefono:
            return "número"
    return None


# ── CRUD ──────────────────────────────────────────────────────────────────────

_SELECT_LISTA = """
SELECT c.id, c.nombre, c.email, c.telefono, c.notas, c.activo, c.fecha_registro,
       COALESCE(p.saldo, 0) AS saldo_puntos,
       COALESCE(e.compras, 0) AS compras,
       COALESCE(e.total_gastado, 0) AS total_gastado,
       CASE WHEN COALESCE(e.compras, 0) > 0 THEN e.total_gastado / e.compras ELSE 0 END AS ticket_promedio,
       e.ultima_compra
FROM clientes c
LEFT JOIN (
    SELECT cliente_id, SUM(puntos) AS saldo
    FROM puntos_movimientos GROUP BY cliente_id
) p ON p.cliente_id = c.id
LEFT JOIN (
    SELECT cliente_id, COUNT(*) AS compras, SUM(total) AS total_gastado, MAX(fecha_ts) AS ultima_compra
    FROM ordenes WHERE estado != 'Anulada' AND cliente_id IS NOT NULL GROUP BY cliente_id
) e ON e.cliente_id = c.id
"""


def listar_clientes(tenant_id: str, q: str | None = None, incluir_inactivos: bool = False) -> list[dict]:
    """Lista con saldo de puntos y métricas de compra agregadas en SQL."""
    where = "WHERE c.tenant_id = %s"
    params: list = [tenant_id]
    if not incluir_inactivos:
        where += " AND c.activo"
    if q and q.strip():
        termino = f"%{q.strip().lower()}%"
        where += (" AND (LOWER(c.nombre) LIKE %s OR LOWER(COALESCE(c.telefono, '')) LIKE %s "
                  "OR LOWER(COALESCE(c.email, '')) LIKE %s)")
        params += [termino, termino, termino]
    filas = query(_SELECT_LISTA + where + " ORDER BY c.nombre ASC", tuple(params))
    for f in filas:
        f["saldo_puntos"] = int(f.get("saldo_puntos") or 0)
        f["compras"] = int(f.get("compras") or 0)
        f["total_gastado"] = float(f.get("total_gastado") or 0)
        f["ticket_promedio"] = float(f.get("ticket_promedio") or 0)
    return filas


def obtener_cliente(tenant_id: str, cliente_id: str, limite_movimientos: int = 50) -> dict:
    """Detalle de UN cliente: datos + saldo + últimos movimientos y compras."""
    filas = query(
        _SELECT_LISTA + "WHERE c.tenant_id = %s AND c.id = %s",
        (tenant_id, cliente_id)
    )
    if not filas:
        return {}
    cliente = filas[0]
    cliente["saldo_puntos"] = int(cliente.get("saldo_puntos") or 0)
    cliente["compras"] = int(cliente.get("compras") or 0)
    cliente["total_gastado"] = float(cliente.get("total_gastado") or 0)
    cliente["ticket_promedio"] = float(cliente.get("ticket_promedio") or 0)
    cliente["movimientos"] = query(
        "SELECT id, tipo, puntos, valor_monetario, concepto, fecha, orden_id "
        "FROM puntos_movimientos WHERE tenant_id = %s AND cliente_id = %s "
        "ORDER BY fecha DESC LIMIT %s",
        (tenant_id, cliente_id, limite_movimientos)
    )
    cliente["ultimas_compras"] = query(
        "SELECT id, n_ticket, fecha_ts, total, cantidad_items FROM ordenes "
        "WHERE tenant_id = %s AND cliente_id = %s AND estado != 'Anulada' "
        "ORDER BY fecha_ts DESC LIMIT 20",
        (tenant_id, cliente_id)
    )
    return cliente


def crear_cliente(
    tenant_id: str, nombre: str,
    email: str | None = None, telefono: str | None = None,
    pin: str | None = None, notas: str | None = None,
) -> dict:
    """Alta de cliente validando la config de campos del tenant."""
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "tipo": "validacion", "mensaje": "El nombre del cliente es obligatorio"}

    config = _leer_config(tenant_id)
    email_n = (email or "").strip() or None
    tel_n = (telefono or "").strip() or None
    pin_n = (pin or "").strip() or None

    faltantes = _validar_contra_config(config["campos"], email_n, tel_n, pin_n)
    if faltantes:
        return {
            "ok": False, "tipo": "validacion",
            "mensaje": f"Faltan campos obligatorios: {', '.join(faltantes)}",
        }

    dup = _duplicado(tenant_id, email_n, tel_n)
    if dup:
        return {"ok": False, "tipo": "validacion", "mensaje": f"Ya existe un cliente con ese {dup}"}

    filas = query(
        "INSERT INTO clientes (tenant_id, nombre, email, telefono, pin_hash, notas) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "RETURNING id, nombre, email, telefono, notas, activo, fecha_registro",
        (tenant_id, nombre, email_n, tel_n, _hash_pin(pin_n) if pin_n else None, (notas or "").strip() or None)
    )
    return {"ok": True, "cliente": filas[0]}


def actualizar_cliente(
    tenant_id: str, cliente_id: str,
    nombre: str | None = None, email: str | None = None, telefono: str | None = None,
    pin: str | None = None, notas: str | None = None, activo: bool | None = None,
) -> dict:
    """
    Edición de un cliente. Semántica del pin:
    - None  → no se toca    - ""    → se quita la contraseña
    - valor → se re-hashea
    """
    actual = query(
        "SELECT id, nombre, email, telefono, pin_hash, notas, activo FROM clientes "
        "WHERE tenant_id = %s AND id = %s",
        (tenant_id, cliente_id)
    )
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
    a = actual[0]

    nombre_n = (nombre if nombre is not None else a["nombre"]).strip()
    if not nombre_n:
        return {"ok": False, "tipo": "validacion", "mensaje": "El nombre del cliente es obligatorio"}

    email_n = (email if email is not None else a["email"])
    email_n = (email_n or "").strip() or None
    tel_n = (telefono if telefono is not None else a["telefono"])
    tel_n = (tel_n or "").strip() or None

    config = _leer_config(tenant_id)
    # El pin vigente no cambia salvo que el request lo traiga; para validar
    # obligatoriedad: None en el request + cliente sin pin = faltante si requerido.
    if pin is None:
        pin_vigente = bool(a["pin_hash"])
        pin_nuevo = None
    elif pin == "":
        pin_vigente = False
        pin_nuevo = ""
    else:
        pin_vigente = True
        pin_nuevo = pin.strip()

    campos = config["campos"]
    faltantes = _validar_contra_config(campos, email_n, tel_n, "x" if pin_vigente else None)
    if faltantes:
        return {
            "ok": False, "tipo": "validacion",
            "mensaje": f"Faltan campos obligatorios: {', '.join(faltantes)}",
        }

    # Si el campo dejó de estar activo, se limpia el dato guardado.
    if not campos["email"]["activo"]:
        email_n = None
    if not campos["telefono"]["activo"]:
        tel_n = None

    dup = _duplicado(tenant_id, email_n, tel_n, excluir_id=cliente_id)
    if dup:
        return {"ok": False, "tipo": "validacion", "mensaje": f"Otro cliente ya usa ese {dup}"}

    if pin_nuevo == "":
        execute(
            "UPDATE clientes SET nombre=%s, email=%s, telefono=%s, pin_hash=NULL, "
            "notas=%s, activo=COALESCE(%s, activo) WHERE tenant_id=%s AND id=%s",
            (nombre_n, email_n, tel_n, (notas if notas is not None else a["notas"]) or None,
             activo, tenant_id, cliente_id)
        )
    else:
        sets = "nombre=%s, email=%s, telefono=%s, notas=%s, activo=COALESCE(%s, activo)"
        params: list = [nombre_n, email_n, tel_n,
                        (notas if notas is not None else a["notas"]) or None, activo]
        if pin_nuevo is not None:
            sets += ", pin_hash=%s"
            params.append(_hash_pin(pin_nuevo))
        params += [tenant_id, cliente_id]
        execute(f"UPDATE clientes SET {sets} WHERE tenant_id=%s AND id=%s", tuple(params))

    return {"ok": True, "cliente": obtener_cliente(tenant_id, cliente_id)}


def eliminar_cliente(tenant_id: str, cliente_id: str) -> dict:
    """Baja LÓGICA: conserva historial de compras y movimientos de puntos."""
    filas = query(
        "UPDATE clientes SET activo = false WHERE tenant_id = %s AND id = %s RETURNING nombre",
        (tenant_id, cliente_id)
    )
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
    return {"ok": True, "mensaje": f"Cliente {filas[0]['nombre']} dado de baja (historial conservado)"}


# ── Identificación en el POS (Fase B la consume; el endpoint ya queda listo) ──

def verificar_cliente(tenant_id: str, identificador: str, pin: str | None = None) -> dict:
    """
    Busca al cliente por teléfono o email EXACTOS y valida su contraseña SOLO
    si el cliente la tiene configurada. Devuelve datos mínimos + saldo.
    """
    iden = (identificador or "").strip()
    if not iden:
        return {"ok": False, "mensaje": "Indica el número o correo del cliente"}
    filas = query(
        "SELECT id, nombre, email, telefono, pin_hash, activo FROM clientes "
        "WHERE tenant_id = %s AND (telefono = %s OR LOWER(email) = LOWER(%s)) "
        "ORDER BY activo DESC LIMIT 2",
        (tenant_id, iden, iden)
    )
    if not filas:
        return {"ok": False, "mensaje": "Cliente no encontrado — puedes registrarlo al vuelo"}
    c = filas[0]
    if not c["activo"]:
        return {"ok": False, "mensaje": "El cliente está dado de baja"}
    if c["pin_hash"] and not _verificar_pin(pin or "", c["pin_hash"]):
        return {"ok": False, "mensaje": "Contraseña incorrecta"}
    saldo = query(
        "SELECT COALESCE(SUM(puntos), 0) AS saldo FROM puntos_movimientos "
        "WHERE tenant_id = %s AND cliente_id = %s",
        (tenant_id, c["id"])
    )[0]["saldo"]
    return {
        "ok": True,
        "cliente": {
            "id": c["id"], "nombre": c["nombre"], "email": c["email"],
            "telefono": c["telefono"], "saldo_puntos": int(saldo or 0),
        },
    }
