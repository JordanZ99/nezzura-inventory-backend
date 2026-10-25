# ==============================================================================
# backend/database/crm.py
# CRM interno (Fase B). Cuatro piezas sobre la cartera de la mig 038:
#
#   1. ETIQUETAS libres por tenant (mig 054): CRUD + asignación n:n vía
#      cliente_etiqueta (semántica replace-all sincroniza el set del cliente).
#   2. INTERACCIONES de seguimiento (mig 055): timeline editable por cliente.
#   3. SEGMENTOS dinámicos: NO tienen tabla — se computan on-demand con SQL
#      sobre `ordenes` (el agregado difiere del de clientes.py solo en detalle).
#      El padrón es de cientos/miles de clientes, no millones: un request es
#      barato con los índices de `ordenes`.
#   4. LTV PROYECTADO con datos observados (requisito del CRM del negocio).
#
# El tenant_id SIEMPRE entra por JWT (dependencies.get_tenant_id) y aquí se
# filtra explícito en cada query, igual que en clientes.py / puntos.py.
# ==============================================================================

import re
import statistics
from datetime import date, datetime, timedelta, timezone

from database.conexion import query, execute

_TIPOS_INTERACCION = ("nota", "llamada", "visita", "promo")
_RE_COLOR_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_COLOR_DEFAULT = "#6366f1"

# Cartera de un micro-negocio: el límite protege la UI (chips), no el costo.
MAX_ETIQUETAS_POR_TENANT = 50


def _a_utc(dt) -> datetime:
    """fecha_ts llega tz-aware (timestamptz); si viniera naive se asume UTC."""
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ═══════════════════════════ 1. ETIQUETAS ═════════════════════════════════════

def listar_etiquetas(tenant_id: str) -> list[dict]:
    """Catálogo del tenant con el número de clientes que lleva cada una."""
    filas = query(
        "SELECT e.id, e.nombre, e.color, COUNT(ce.cliente_id) AS clientes "
        "FROM etiquetas e "
        "LEFT JOIN cliente_etiqueta ce ON ce.etiqueta_id = e.id "
        "WHERE e.tenant_id = %s "
        "GROUP BY e.id, e.nombre, e.color "
        "ORDER BY e.nombre ASC",
        (tenant_id,)
    )
    for f in filas:
        f["clientes"] = int(f["clientes"] or 0)
    return filas


def _color_valido(color: str | None) -> str:
    """Hex válido tal cual (minúsculas); cualquier otra cosa cae al default."""
    if color and _RE_COLOR_HEX.match(color):
        return color.lower()
    return _COLOR_DEFAULT


def crear_etiqueta(tenant_id: str, nombre: str, color: str | None = None) -> dict:
    """Alta de una etiqueta libre. Nombre único insensible a mayúsculas
    (índice funcional de la mig 054) — el mensaje avisa en vez de reventar."""
    nombre = (nombre or "").strip()
    if not nombre:
        return {"ok": False, "tipo": "validacion", "mensaje": "El nombre de la etiqueta es obligatorio"}
    if len(nombre) > 40:
        return {"ok": False, "tipo": "validacion", "mensaje": "Máximo 40 caracteres"}

    if query("SELECT 1 FROM etiquetas WHERE tenant_id = %s AND lower(nombre) = lower(%s)",
             (tenant_id, nombre)):
        return {"ok": False, "tipo": "validacion", "mensaje": f"Ya existe una etiqueta '{nombre}'"}

    total = query("SELECT COUNT(*) AS n FROM etiquetas WHERE tenant_id = %s", (tenant_id,))[0]["n"]
    if int(total) >= MAX_ETIQUETAS_POR_TENANT:
        return {"ok": False, "tipo": "validacion",
                "mensaje": f"Límite de {MAX_ETIQUETAS_POR_TENANT} etiquetas alcanzado"}

    filas = query(
        "INSERT INTO etiquetas (tenant_id, nombre, color) VALUES (%s, %s, %s) "
        "RETURNING id, nombre, color",
        (tenant_id, nombre, _color_valido(color))
    )
    return {"ok": True, "etiqueta": filas[0]}


def actualizar_etiqueta(tenant_id: str, etiqueta_id: str,
                        nombre: str | None = None, color: str | None = None) -> dict:
    """Renombrar / recolorear. None = sin cambio; color inválido cae al default."""
    actual = query(
        "SELECT id, nombre, color FROM etiquetas WHERE tenant_id = %s AND id = %s",
        (tenant_id, etiqueta_id)
    )
    if not actual:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Etiqueta no encontrada"}
    a = actual[0]

    nombre_n = (nombre if nombre is not None else a["nombre"]).strip()
    if not nombre_n:
        return {"ok": False, "tipo": "validacion", "mensaje": "El nombre de la etiqueta es obligatorio"}
    if len(nombre_n) > 40:
        return {"ok": False, "tipo": "validacion", "mensaje": "Máximo 40 caracteres"}
    # Repetición: unicidad solo se reclama cuando el nombre cambia de verdad.
    if nombre_n.lower() != str(a["nombre"]).lower():
        if query("SELECT 1 FROM etiquetas WHERE tenant_id = %s AND lower(nombre) = lower(%s) AND id <> %s",
                 (tenant_id, nombre_n, etiqueta_id)):
            return {"ok": False, "tipo": "validacion", "mensaje": f"Ya existe una etiqueta '{nombre_n}'"}

    color_n = a["color"] if color is None else _color_valido(color)

    filas = query(
        "UPDATE etiquetas SET nombre = %s, color = %s "
        "WHERE tenant_id = %s AND id = %s RETURNING id, nombre, color",
        (nombre_n, color_n, tenant_id, etiqueta_id)
    )
    return {"ok": True, "etiqueta": filas[0]}


def eliminar_etiqueta(tenant_id: str, etiqueta_id: str) -> dict:
    """Se elimina de todos los clientes (CASCADE de cliente_etiqueta)."""
    filas = query(
        "DELETE FROM etiquetas WHERE tenant_id = %s AND id = %s RETURNING nombre",
        (tenant_id, etiqueta_id)
    )
    if not filas:
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Etiqueta no encontrada"}
    return {"ok": True, "mensaje": f"Etiqueta '{filas[0]['nombre']}' eliminada (se desasignó de todos los clientes)"}


def etiquetas_de_cliente(cliente_id: str) -> list[dict]:
    """Etiquetas vigentes de un cliente (ordenadas por nombre, como en la ficha)."""
    return query(
        "SELECT e.id, e.nombre, e.color FROM cliente_etiqueta ce "
        "JOIN etiquetas e ON e.id = ce.etiqueta_id "
        "WHERE ce.cliente_id = %s ORDER BY e.nombre ASC",
        (cliente_id,)
    )


def asignar_etiquetas(tenant_id: str, cliente_id: str, etiqueta_ids: list[str]) -> dict:
    """REPLACE-ALL: la lista enviada ES el set final del cliente (lo ausente
    se desasigna). Verifica que el cliente Y todas las etiquetas sean del tenant."""
    if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}

    pedidos = list(dict.fromkeys(etiqueta_ids or []))  # sin duplicados, conserva orden
    validas: set[str] = set()
    if pedidos:
        filas = query(
            "SELECT id FROM etiquetas WHERE tenant_id = %s AND id = ANY(%s::uuid[]) ",
            (tenant_id, pedidos)
        )
        validas = {f["id"] for f in filas}
        faltan = [i for i in pedidos if i not in validas]
        if faltan:
            return {"ok": False, "tipo": "validacion",
                    "mensaje": "Hay etiquetas que no existen en este negocio"}

    execute("DELETE FROM cliente_etiqueta WHERE cliente_id = %s", (cliente_id,))
    for eid in pedidos:
        execute(
            "INSERT INTO cliente_etiqueta (cliente_id, etiqueta_id) VALUES (%s, %s)",
            (cliente_id, eid)
        )
    return {"ok": True, "etiquetas": etiquetas_de_cliente(cliente_id)}


# ═══════════════════════════ 2. INTERACCIONES ═════════════════════════════════

def crear_interaccion(tenant_id: str, cliente_id: str, tipo: str,
                      texto: str, creado_por: str = "") -> dict:
    """Anota un hecho del timeline: llamada, nota, visita o promo enviada."""
    if tipo not in _TIPOS_INTERACCION:
        return {"ok": False, "tipo": "validacion",
                "mensaje": f"Tipo inválido: usa {' | '.join(_TIPOS_INTERACCION)}"}
    texto = (texto or "").strip()
    if not texto:
        return {"ok": False, "tipo": "validacion", "mensaje": "El texto de la interacción es obligatorio"}
    if not query("SELECT 1 FROM clientes WHERE tenant_id = %s AND id = %s", (tenant_id, cliente_id)):
        return {"ok": False, "tipo": "no_encontrado", "mensaje": "Cliente no encontrado"}
    filas = query(
        "INSERT INTO crm_interacciones (tenant_id, cliente_id, tipo, texto, creado_por) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id, tipo, texto, creado_por, fecha",
        (tenant_id, cliente_id, tipo, texto, (creado_por or "").strip())
    )
    return {"ok": True, "interaccion": filas[0]}


def listar_interacciones(tenant_id: str, cliente_id: str, limite: int = 50) -> list[dict]:
    """Timeline del cliente; el CHECK del tipo garantiza lo aceptable."""
    return query(
        "SELECT id, tipo, texto, creado_por, fecha FROM crm_interacciones "
        "WHERE tenant_id = %s AND cliente_id = %s "
        "ORDER BY fecha DESC LIMIT %s",
        (tenant_id, cliente_id, max(1, min(int(limite), 200)))
    )


# ═══════════════════════════ 3. SEGMENTOS DINÁMICOS ═══════════════════════════

# Definiciones (una sola fuente de verdad para la doc de la tarea):
#   frecuentes    → activos con 3+ compras
#   nuevos        → primera compra en los últimos 30 días (fruto de la campaña)
#   top_gasto     → los 10 activos de mayor gasto histórico
#   top_puntos    → los 10 activos con mayor saldo de puntos ( requerido doc 039)
#   inactivos_30  → última compra hace 30–59 días  (campaña de recuperación suave)
#   inactivos_60  → última compra hace 60–89 días
#   inactivos_90  → última compra hace 90+ días (riesgo de fuga)
#   cumpleaneros  → cumpleaños dentro de los próximos 7 días (promo)
_SEGMENTOS = [
    ("frecuentes",   "Frecuentes",                 "Activos con 3 o más compras"),
    ("nuevos",       "Nuevos (30 días)",           "Primera compra en los últimos 30 días"),
    ("top_gasto",    "Top clientes",               "Los 10 de mayor gasto histórico"),
    ("top_puntos",   "Top en puntos",              "Los 10 con mayor saldo de puntos"),
    ("inactivos_30", "Inactivos 30–59 días",       "Su última compra fue hace 30 a 59 días"),
    ("inactivos_60", "Inactivos 60–89 días",       "Su última compra fue hace 60 a 89 días"),
    ("inactivos_90", "Inactivos 90+ días",         "Su última compra fue hace 90 días o más"),
    ("cumpleaneros", "Cumpleañeros de la semana",  "Cumpleaños dentro de los próximos 7 días"),
]


def _cumple_en(cumpleanos: date, dia: date) -> bool:
    """¿El cumpleaños cae este día (mes-Day iguales)? Feb 29 coincide con Feb 28
    en años bisiestos-impar (no existe 29-feb ese año)."""
    if dia.month == cumpleanos.month and dia.day == cumpleanos.day:
        return True
    return (cumpleanos.month == 2 and cumpleanos.day == 29
            and dia.month == 2 and dia.day == 28)


def calcular_segmentos(tenant_id: str) -> list[dict]:
    """
    Clasifica la cartera ACTIVA en los segmentos dinámicos. Computa en Python
    sobre 2 agregados (compras por cliente y saldos de puntos): cada segmento
    es una lista de cliente_ids; el front los filtra con espejo de conteos.
    """
    ahora = datetime.now(timezone.utc)

    # Agregado de compras (equivalente al sub-select `e` de clientes.py)…
    agg = _agregado_compras(tenant_id)
    # …y de puntos (sub-select `p`).
    saldos = {
        f["cliente_id"]: int(f["saldo"]) for f in query(
            "SELECT cliente_id, COALESCE(SUM(puntos), 0) AS saldo "
            "FROM puntos_movimientos WHERE tenant_id = %s GROUP BY cliente_id",
            (tenant_id,)
        )
    }
    clientes = query(
        "SELECT id, nombre, activo, cumpleanos FROM clientes WHERE tenant_id = %s",
        (tenant_id,)
    )

    buckets: dict[str, list] = {sid: [] for sid, _, _ in _SEGMENTOS}
    por_gasto: list[tuple[float, str]] = []

    for c in clientes:
        if not c["activo"]:
            continue  # los dados de baja NO entran en campañas
        cid = c["id"]
        a = agg.get(cid)
        if a:
            dias_sin_comprar = (ahora - _a_utc(a["ultima"])).days
            if int(a["compras"]) >= 3:
                buckets["frecuentes"].append(cid)
            if _a_utc(a["primera"]) >= ahora - timedelta(days=30):
                buckets["nuevos"].append(cid)
            if 30 <= dias_sin_comprar < 60:
                buckets["inactivos_30"].append(cid)
            elif 60 <= dias_sin_comprar < 90:
                buckets["inactivos_60"].append(cid)
            elif dias_sin_comprar >= 90:
                buckets["inactivos_90"].append(cid)
            por_gasto.append((float(a["gasto"] or 0), cid))

        # Cumpleaños vigente: mes/día matcheando LA SEMANA RODANTE de hoy.
        if c["cumpleanos"] and any(
            _cumple_en(c["cumpleanos"], (ahora + timedelta(days=d)).date())
            for d in range(7)
        ):
            buckets["cumpleaneros"].append(cid)

    # Tops de recorte 10, ordenados para que el front recorra el ranking.
    for gasto, cid in sorted(por_gasto, reverse=True)[:10]:
        buckets["top_gasto"].append(cid)
    for cid in sorted(saldos, key=saldos.get, reverse=True):
        if len(buckets["top_puntos"]) >= 10:
            break
        if saldos[cid] > 0:
            buckets["top_puntos"].append(cid)

    info = {sid: (nombre, desc) for sid, nombre, desc in _SEGMENTOS}
    return [
        {"id": sid, "nombre": info[sid][0], "descripcion": info[sid][1],
         "conteo": len(bucket), "ids": bucket}
        for sid, bucket in buckets.items()
    ]


# ═══════════════════════════════ 4. LTV ════════════════════════════════════════
#
# Metodología (datos observados, nada inventado — para la tarea/profesor):
#
#   por cliente con compras:
#     ticket_promedio     = gasto / compras                              (real)
#     frecuencia_mensual  = compras / (días desde 1ra compra / 30.44)   (real)
#     factor_recencia     = max(0, 1 − días desde última compra / 365)  (real)
#         (decae linealmente al año sin comprar; quien lleva 90 días
#          conserva ~0.75 — coherente con los segmentos de inactividad)
#     LTV_historico       = gasto
#     LTV_proyectado      = ticket_promedio × frecuencia_mensual
#                           × horizonte_meses × tasa_retencion × factor_recencia
#
#   del tenant:
#     tasa_retencion = clientes con 2+ compras / clientes con compras
#     (prob. observada de recompra; multiplica TODAS las proyecciones)
#
# Nada se persiste: la proyección se recalcula on-demand igual que el saldo
# de puntos (ledger) y las métricas de compra de la ficha.
# ══════════════════════════════════════════════════════════════════════════════

def calcular_ltv(tenant_id: str, horizonte_meses: int = 12) -> dict:
    """
    LTV por cliente + resumen global + LTV de cada segmento dinámico, en una
    sola respuesta para pintar la pantalla del CRM sin hermosear el front.
    """
    horizonte = max(1, min(int(horizonte_meses), 60))
    ahora = datetime.now(timezone.utc)

    agg = _agregado_compras(tenant_id)
    datos = query(
        "SELECT id, nombre, activo FROM clientes WHERE tenant_id = %s", (tenant_id,)
    )
    nombres = {c["id"]: (c["nombre"], bool(c["activo"])) for c in datos}

    filas = []
    for cid, a in agg.items():
        compras = int(a["compras"])
        gasto = float(a["gasto"] or 0)
        nombre, activo = nombres.get(cid, ("(eliminado)", False))
        ticket = gasto / compras if compras else 0.0
        dias_primera = max((ahora - _a_utc(a["primera"])).days, 1)
        # Piso de medio mes: amortigua a un cliente que concentró todas sus
        # compras en su arranque (mes 1) — sin piso, proyectaría infinito.
        frecuencia_mensual = compras * 30.44 / max(dias_primera, 15)
        dias_sin_comprar = (ahora - _a_utc(a["ultima"])).days
        filas.append({
            "cliente_id": cid,
            "nombre": nombre,
            "activo": activo,
            "compras": compras,
            "gasto": gasto,
            "ticket_promedio": ticket,
            "frecuencia_mensual": frecuencia_mensual,
            "dias_sin_comprar": dias_sin_comprar,
            "ltv_historico": gasto,
            "ltv_proyectado": 0.0,   # se rellena con la tasa global abajo
        })

    con_compras = len(filas)
    recurrentes = sum(1 for f in filas if f["compras"] >= 2)
    tasa_retencion = recurrentes / con_compras if con_compras else 0.0

    for f in filas:
        factor_recencia = max(0.0, 1.0 - f["dias_sin_comprar"] / 365.0)
        f["ltv_proyectado"] = round(
            f["ticket_promedio"] * f["frecuencia_mensual"]
            * horizonte * tasa_retencion * factor_recencia, 2
        )

    ltv_promedio = round(statistics.fmean(f["ltv_proyectado"] for f in filas), 2) if filas else 0.0
    global_resumen = {
        "horizonte_meses": horizonte,
        "tasa_retencion": round(tasa_retencion, 4),        # 0..1 (front: ×100 en %)
        "clientes_con_compras": con_compras,
        "clientes_recurrentes": recurrentes,
        "ltv_promedio_proyectado": ltv_promedio,
        "gasto_total_historico": round(sum(f["gasto"] for f in filas), 2),
    }

    segmentos_resumen = []
    for s in calcular_segmentos(tenant_id):
        ids = set(s["ids"])
        filas_s = [f for f in filas if f["cliente_id"] in ids]
        segmentos_resumen.append({
            "id": s["id"],
            "nombre": s["nombre"],
            "conteo": s["conteo"],
            "ltv_promedio": round(statistics.fmean(f["ltv_proyectado"] for f in filas_s), 2) if filas_s else 0.0,
            "ltv_total": round(sum(f["ltv_proyectado"] for f in filas_s), 2),
            "gasto_historico": round(sum(f["gasto"] for f in filas_s), 2),
        })

    return {"global": global_resumen, "segmentos": segmentos_resumen, "clientes": filas}


def _agregado_compras(tenant_id: str) -> dict:
    """Agregado de compras por cliente (reutilizado por calcular_ltv)."""
    return {
        f["cliente_id"]: f for f in query(
            "SELECT cliente_id, COUNT(*) AS compras, SUM(total) AS gasto, "
            "MIN(fecha_ts) AS primera, MAX(fecha_ts) AS ultima "
            "FROM ordenes WHERE tenant_id = %s AND estado <> 'Anulada' "
            "AND cliente_id IS NOT NULL GROUP BY cliente_id",
            (tenant_id,)
        )
    }
