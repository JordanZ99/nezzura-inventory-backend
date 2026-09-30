# ==============================================================================
# backend/analisis/reglas.py
# Motor determinista del Análisis Inteligente: hechos ya calculados en SQL →
# hallazgos con severidad y recomendación. Sin escrituras y sin LLM.
# Los umbrales viven aquí para poder calibrarlos sin tocar las queries.
# ==============================================================================

from datetime import date

# ── Umbrales (calibrados con el tenant Kali; validar con un segundo giro) ──
VENTAS_COLD_START = 20
TOP_BEST_SELLER = 10
DIAS_VENTANA = 90
STOCK_MUERTO_MIN_COSTO = 100.0
STOCK_MUERTO_PCT_ROJO = 0.30
TENDENCIA_RATIO = 0.70
MARGEN_HOLGURA_PTS = 5.0
TOP_MARGEN = 15
CONCENTRACION_PCT = 0.60
CONCENTRACION_DIAS = 3
GASTOS_OTROS_PCT = 0.30
PENDIENTES_MIN = 5
PENDIENTES_PCT_MES = 0.15
NOCTURNA_PCT = 0.15
NOCTURNA_MIN = 5
TICKET_GAP_RATIO = 1.4

_ORDEN = {"rojo": 0, "amarillo": 1, "verde": 2}
_MESES = (
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _dinero(valor) -> str:
    return f"${float(valor):,.0f}"


def _hallazgo(tipo: str, severidad: str, titulo: str, detalle: str, recomendacion: str) -> dict:
    return {
        "tipo": tipo,
        "severidad": severidad,
        "titulo": titulo,
        "detalle": detalle,
        "recomendacion": recomendacion,
    }


def _mes_legible(iso: str) -> str:
    try:
        anio, mes = iso.split("-")
        return f"{_MESES[int(mes)]} {anio}"
    except (ValueError, IndexError):
        return iso


def evaluar(hechos: dict) -> list[dict]:
    """Devuelve hallazgos ordenados por severidad. Lista vacía en cold start."""
    if hechos.get("cold_start"):
        return []

    hallazgos: list[dict] = []
    hallazgos.extend(_quiebre(hechos))
    muerto = _stock_muerto(hechos)
    if muerto:
        hallazgos.append(muerto)
    tendencia = _tendencia(hechos)
    if tendencia:
        hallazgos.append(tendencia)
    hallazgos.extend(_margen_bajo(hechos))
    concentracion = _concentracion(hechos)
    if concentracion:
        hallazgos.append(concentracion)
    otros = _gastos_otros(hechos)
    if otros:
        hallazgos.append(otros)
    lotes = _lotes(hechos)
    if lotes:
        hallazgos.append(lotes)
    pendientes = _pendientes(hechos)
    if pendientes:
        hallazgos.append(pendientes)
    sin_lote = _sin_lote(hechos)
    if sin_lote:
        hallazgos.append(sin_lote)
    nocturna = _nocturna(hechos)
    if nocturna:
        hallazgos.append(nocturna)
    hallazgos.extend(_informativos(hechos))

    hallazgos.sort(key=lambda h: _ORDEN.get(h["severidad"], 9))
    return hallazgos[:12]


def _quiebre(hechos: dict) -> list[dict]:
    top = sorted(hechos.get("productos_90") or [], key=lambda p: p["ingresos"], reverse=True)[:TOP_BEST_SELLER]
    salida = []
    for p in top:
        if p["ingresos"] <= 0 or p["stock"] > 0:
            continue
        ritmo = p["unidades"] / 3 if p["unidades"] else 0
        cubrir = max(1, round(ritmo * 3)) if ritmo else 1
        ritmo_txt = f"~{ritmo:.1f}/mes" if ritmo else "sin unidades registradas"
        salida.append(_hallazgo(
            "quiebre_best_seller", "rojo",
            f"{p['producto']} agotada",
            f"Vendió {_dinero(p['ingresos'])} en 90 días y stock actual: 0",
            f"Reabastece ~{cubrir} unidades para cubrir el ritmo de venta ({ritmo_txt})",
        ))
        if len(salida) == 3:
            break
    return salida


def _stock_muerto(hechos: dict) -> dict | None:
    muertos = [
        p for p in (hechos.get("productos_90") or [])
        if p["ingresos"] <= 0 and p["valor_costo"] >= STOCK_MUERTO_MIN_COSTO and p["stock"] > 0
    ]
    if not muertos:
        return None
    valor = sum(p["valor_costo"] for p in muertos)
    inventario = float(hechos.get("valor_inventario_costo") or 0)
    pct = (valor / inventario) if inventario > 0 else 0
    nombres = ", ".join(p["producto"] for p in muertos[:4])
    extra = f" y {len(muertos) - 4} más" if len(muertos) > 4 else ""
    rojo = pct >= STOCK_MUERTO_PCT_ROJO
    return _hallazgo(
        "stock_muerto",
        "rojo" if rojo else "amarillo",
        f"Stock sin movimiento: {_dinero(valor)} a costo",
        f"{len(muertos)} producto(s) sin ventas en 90 días ({nombres}{extra}). "
        f"Es el {pct * 100:.0f}% del inventario a costo.",
        "Baja precio, arma un combo o deja de reponer lo que no rota.",
    )


def _tendencia(hechos: dict) -> dict | None:
    semanas: list[float] = hechos.get("semanas_8") or []
    if len(semanas) < 8:
        return None
    # Con pocas semanas con venta el promedio de 8 semanas no significa nada:
    # saldría "caída del 97%" solo porque el negocio lleva semanas sin registrar.
    if sum(1 for s in semanas if s > 0) < 4:
        return None
    prom8 = sum(semanas) / 8
    prom2 = sum(semanas[-2:]) / 2
    if prom8 <= 0 or prom2 >= TENDENCIA_RATIO * prom8:
        return None
    return _hallazgo(
        "tendencia_baja", "amarillo",
        "Las últimas 2 semanas venden menos de lo habitual",
        f"Promedio reciente {_dinero(prom2)}/semana vs {_dinero(prom8)} de las últimas 8 "
        f"({prom2 / prom8 * 100:.0f}%).",
        "Revisa si faltó stock de lo que más rota o si el flujo de clientes bajó esos días.",
    )


def _margen_bajo(hechos: dict) -> list[dict]:
    vendidos = [p for p in (hechos.get("productos_90") or []) if p["ingresos"] > 0]
    if len(vendidos) < 3:
        return []
    ingresos = sum(p["ingresos"] for p in vendidos)
    ganancia = sum(p["ganancia"] for p in vendidos)
    if ingresos <= 0:
        return []
    margen_prom = ganancia / ingresos * 100
    piso = margen_prom - MARGEN_HOLGURA_PTS
    top = sorted(vendidos, key=lambda p: p["ingresos"], reverse=True)[:TOP_MARGEN]
    salida = []
    for p in top:
        margen = p["ganancia"] / p["ingresos"] * 100
        if margen >= piso:
            continue
        salida.append(_hallazgo(
            "margen_bajo", "amarillo",
            f"{p['producto']} deja poco margen",
            f"Margen {margen:.0f}% frente al {margen_prom:.0f}% promedio del negocio "
            f"(vendió {_dinero(p['ingresos'])} en 90 días).",
            "Sube el precio o revisa el costo del lote: está entre lo que más vendes.",
        ))
        if len(salida) == 2:
            break
    return salida


def _concentracion(hechos: dict) -> dict | None:
    dias = [d for d in (hechos.get("ingresos_por_dow") or []) if d["ingresos"] > 0]
    total = sum(d["ingresos"] for d in dias)
    if total <= 0 or len(dias) <= CONCENTRACION_DIAS:
        return None
    top = sorted(dias, key=lambda d: d["ingresos"], reverse=True)[:CONCENTRACION_DIAS]
    pct = sum(d["ingresos"] for d in top) / total
    if pct <= CONCENTRACION_PCT:
        return None
    nombres = ", ".join(d["dia"] for d in top)
    return _hallazgo(
        "concentracion_dias", "amarillo",
        f"El {pct * 100:.0f}% de los ingresos cae en {CONCENTRACION_DIAS} días",
        f"Se concentran en {nombres}. El resto de la semana aporta poco.",
        "Prueba una promo corta en los días flojos antes de asumir que 'no hay gente'.",
    )


def _gastos_otros(hechos: dict) -> dict | None:
    categorias = hechos.get("gastos_por_categoria") or []
    total = sum(c["monto"] for c in categorias)
    if total <= 0:
        return None
    otros = next((c for c in categorias if c["categoria"].strip().lower() in ("otros", "otro")), None)
    if not otros or otros["monto"] / total <= GASTOS_OTROS_PCT:
        return None
    pct = otros["monto"] / total
    return _hallazgo(
        "gastos_comodin", "amarillo",
        f"'Otros' es el {pct * 100:.0f}% de tus gastos",
        f"{_dinero(otros['monto'])} de {_dinero(total)} del período están sin categoría útil.",
        "Reclasifica esos gastos: si no sabes en qué se va, no puedes recortarlo.",
    )


def _lotes(hechos: dict) -> dict | None:
    filas = hechos.get("lotes_inconsistentes") or []
    if not filas:
        return None
    nombres = ", ".join(f["producto"] for f in filas[:4])
    return _hallazgo(
        "lotes_inconsistentes", "amarillo",
        f"{len(filas)} lote(s) con stock o costo negativo",
        f"Revisa: {nombres}. Un stock negativo desalinea el PEPS y el valor del inventario.",
        "Corrige el lote en Inventario antes de seguir vendiendo ese producto.",
    )


def _pendientes(hechos: dict) -> dict | None:
    n = int(hechos.get("pendientes_n") or 0)
    monto = float(hechos.get("pendientes_monto") or 0)
    gastos_mes = float(hechos.get("gastos_mes") or 0)
    pct = (monto / gastos_mes) if gastos_mes > 0 else 0
    if n < PENDIENTES_MIN and pct <= PENDIENTES_PCT_MES:
        return None
    return _hallazgo(
        "gastos_pendientes", "verde",
        f"{n} gasto(s) pendiente(s) este mes",
        f"Sum {_dinero(monto)}" + (f" ({pct * 100:.0f}% del gasto del mes)." if gastos_mes else "."),
        "Confírmalos o descártalos para que el corte de caja no quede inflado.",
    )


def _sin_lote(hechos: dict) -> dict | None:
    nombres = hechos.get("vendidos_sin_lote") or []
    if not nombres:
        return None
    return _hallazgo(
        "vendido_sin_lote", "amarillo",
        "Hay ventas de stock sin lote asociado",
        f"Productos: {', '.join(nombres[:4])}. No restaron inventario de forma rastreable.",
        "Revisa esas ventas: un producto de stock debería salir de un lote (PEPS).",
    )


def _nocturna(hechos: dict) -> dict | None:
    n = int(hechos.get("ventas_nocturnas") or 0)
    total = int(hechos.get("num_ventas") or 0)
    if total <= 0 or n < NOCTURNA_MIN or n / total <= NOCTURNA_PCT:
        return None
    return _hallazgo(
        "venta_nocturna", "verde",
        f"El {n / total * 100:.0f}% de las ventas cae entre 00:00 y 05:59",
        "Puede ser horario real del negocio, o registros capturados al día siguiente.",
        "Si no abres de madrugada, captura la venta el mismo día: mueve el día contable.",
    )


def _informativos(hechos: dict) -> list[dict]:
    salida = []
    mejor = hechos.get("mejor_mes") or {}
    if mejor.get("mes") and mejor.get("ingresos"):
        salida.append(_hallazgo(
            "mes_pico", "verde",
            f"Tu mejor mes fue {_mes_legible(mejor['mes'])}",
            f"Ingresaste {_dinero(mejor['ingresos'])} ese mes, por encima del resto del período.",
            "Replica lo que hiciste ese mes (stock, promo o temporada) en el siguiente pico.",
        ))
    proy = hechos.get("proyeccion_mes_actual")
    hoy: date | None = hechos.get("hoy")
    if proy and hoy and hoy.day >= 3:
        salida.append(_hallazgo(
            "proyeccion_mes", "verde",
            f"A este ritmo el mes cierra en {_dinero(proy)}",
            "Proyección lineal con los días ya transcurridos del mes contable. No es una predicción.",
            "Úsala para decidir si adelantas una compra o esperas al corte.",
        ))
    promedio = float(hechos.get("ticket_promedio") or 0)
    mediano = float(hechos.get("ticket_mediano") or 0)
    if mediano > 0 and promedio > mediano * TICKET_GAP_RATIO and int(hechos.get("num_ventas") or 0) >= VENTAS_COLD_START:
        salida.append(_hallazgo(
            "ticket_gap", "verde",
            "Unas pocas ventas grandes suben el ticket promedio",
            f"El promedio es {_dinero(promedio)} y la mediana {_dinero(mediano)}: "
            "la venta típica es más chica de lo que sugiere el promedio.",
            "No planees compras asumiendo el promedio; la mediana describe mejor al cliente de siempre.",
        ))
    return salida
